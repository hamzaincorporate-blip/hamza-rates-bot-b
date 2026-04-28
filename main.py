import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web as aio_web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('hamza-rates')

BOT_TOKEN = os.getenv('BOT_TOKEN')
SUBSCRIBERS_FILE = 'subscribers.json'
DEFAULT_DELIVERY_HOUR = 9
DELIVERY_TIMEZONE = ZoneInfo('Asia/Tashkent')
USER_COOLDOWN_SECONDS = 8
CACHE_TTL = 60

COINS = ['BTC', 'ETH', 'BNB', 'TON', 'XRP']
PAPRIKA_IDS = {
    'BTC': 'btc-bitcoin',
    'ETH': 'eth-ethereum',
    'BNB': 'bnb-binance-coin',
    'TON': 'ton-toncoin',
    'XRP': 'xrp-xrp',
}

router = Router()
_price_cache: dict = {}
_price_cache_time: float = 0.0
_user_last_request: dict[int, float] = {}


# ---- Persistence ----

def load_subscribers() -> dict:
    if not os.path.exists(SUBSCRIBERS_FILE):
        return {}
    try:
        with open(SUBSCRIBERS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception as e:
        log.error('load error: %s', e)
        return {}


def save_subscribers() -> None:
    tmp = SUBSCRIBERS_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(registered_users, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SUBSCRIBERS_FILE)
    except Exception as e:
        log.error('save error: %s', e)


registered_users: dict = load_subscribers()


# ---- CoinPaprika API ----

async def _fetch_one(session: aiohttp.ClientSession, coin: str) -> tuple[str, dict]:
    pid = PAPRIKA_IDS[coin]
    url = f'https://api.coinpaprika.com/v1/tickers/{pid}'
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        data = await resp.json()
    usd = data['quotes']['USD']
    return coin, {'price': float(usd['price']), 'change': float(usd['percent_change_24h'])}


async def fetch_prices(force: bool = False) -> dict:
    global _price_cache, _price_cache_time
    now = time.monotonic()
    if not force and _price_cache and now - _price_cache_time < CACHE_TTL:
        return _price_cache

    async with aiohttp.ClientSession() as session:
        tasks = [_fetch_one(session, coin) for coin in COINS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    data = {}
    for r in results:
        if isinstance(r, Exception):
            log.error('coin fetch error: %s', r)
            continue
        coin, info = r
        data[coin] = info

    if data:
        _price_cache = data
        _price_cache_time = time.monotonic()
        log.info('Prices fetched: %s', list(data.keys()))

    return data


# ---- Formatting ----

def fmt(v: float) -> str:
    if v >= 1000:
        return f'${v:,.2f}'
    if v >= 1:
        return f'${v:,.4f}'
    return f'${v:,.6f}'


def build_msg(data: dict) -> str:
    lines = []
    for coin in COINS:
        info = data.get(coin)
        if not info:
            lines.append(f'- <b>{coin}</b>: n/a')
            continue
        p, c = info['price'], info['change']
        icon = '🟢' if c >= 0 else '🔴'
        lines.append(f'{icon} <b>{coin}</b>  <code>{fmt(p)}</code>  {c:+.2f}%')
    ts = datetime.now(tz=DELIVERY_TIMEZONE).strftime('%H:%M, %d %b')
    return (
        '<b>Crypto prices [USD]</b>\n\n'
        + '\n'.join(lines)
        + f'\n\n<i>{ts} Tashkent</i>\n'
        + '<a href="https://t.me/hamza_rates_bot">@hamza_rates_bot</a>'
    )


# ---- Keyboard ----

def kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📊 Get prices now', callback_data='get_prices')]
    ])


# ---- Cooldown ----

def cooldown(uid: int) -> float:
    return max(0.0, USER_COOLDOWN_SECONDS - (time.monotonic() - _user_last_request.get(uid, 0.0)))

def mark(uid: int) -> None:
    _user_last_request[uid] = time.monotonic()


# ---- Price sender ----

async def send_prices(message: Message, uid: int) -> None:
    w = cooldown(uid)
    if w > 0:
        await message.answer(f'Please wait {int(w)+1}s.')
        return
    mark(uid)
    try:
        data = await fetch_prices()
        if not data:
            await message.answer('Could not get prices. Try again in a moment.', reply_markup=kb())
            return
        await message.answer(build_msg(data), reply_markup=kb())
    except Exception as e:
        log.error('send_prices error: %s', e)
        await message.answer('Could not get prices. Try again in a moment.', reply_markup=kb())


# ---- Handlers ----

@router.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id
    ex = registered_users.get(uid)
    if ex and ex.get('active'):
        h = ex.get('delivery_hour', DEFAULT_DELIVERY_HOUR)
        await message.answer(
            f'Already subscribed. Daily at <b>{h:02d}:00 Tashkent</b>.\n\n/prices to get prices now.',
            reply_markup=kb()
        )
        return
    registered_users[uid] = {
        'chat_id': message.chat.id,
        'active': True,
        'delivery_hour': DEFAULT_DELIVERY_HOUR,
        'registered_at': datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
    }
    save_subscribers()
    h = DEFAULT_DELIVERY_HOUR
    await message.answer(
        '<b>Welcome to Hamza Rates!</b>\n\n'
        f'Coins: <b>BTC, ETH, BNB, TON, XRP</b>\n'
        f'Daily at: <b>{h:02d}:00 Tashkent</b>\n\n'
        '/prices — prices now\n'
        '/settime 21 — change delivery hour\n'
        '/status — settings\n'
        '/stop — unsubscribe',
        reply_markup=kb()
    )


@router.message(Command('prices'))
async def cmd_prices(message: Message):
    await send_prices(message, message.from_user.id)


@router.message(Command('settime'))
async def cmd_settime(message: Message):
    uid = message.from_user.id
    user = registered_users.get(uid)
    if not user or not user.get('active'):
        await message.answer('Send /start first.')
        return
    parts = (message.text or '').strip().split()
    if len(parts) < 2 or not parts[1].isdigit() or not (0 <= int(parts[1]) <= 23):
        await message.answer('Usage: /settime 9  (hour 0-23)')
        return
    h = int(parts[1])
    user['delivery_hour'] = h
    save_subscribers()
    await message.answer(f'Daily delivery set to <b>{h:02d}:00 Tashkent</b>.')


@router.message(Command('stop'))
async def cmd_stop(message: Message):
    user = registered_users.get(message.from_user.id)
    if user and user.get('active'):
        user['active'] = False
        save_subscribers()
        await message.answer('Unsubscribed. /start to subscribe again.')
    else:
        await message.answer('Not subscribed. /start to subscribe.')


@router.message(Command('status'))
async def cmd_status(message: Message):
    user = registered_users.get(message.from_user.id)
    if user and user.get('active'):
        h = user.get('delivery_hour', DEFAULT_DELIVERY_HOUR)
        await message.answer(
            f'✅ Subscribed\nCoins: <b>BTC, ETH, BNB, TON, XRP</b>\nDaily at: <b>{h:02d}:00 Tashkent</b>',
            reply_markup=kb()
        )
    else:
        await message.answer('Not subscribed. /start to subscribe.', reply_markup=kb())


@router.message(Command('help'))
async def cmd_help(message: Message):
    await message.answer(
        '<b>Hamza Rates</b>\n\n'
        '/prices — prices now\n'
        '/settime 9 — delivery hour (0-23)\n'
        '/status — settings\n'
        '/stop — unsubscribe\n\n'
        'Coins: BTC ETH BNB TON XRP | Source: CoinPaprika',
        reply_markup=kb()
    )


@router.callback_query(F.data == 'get_prices')
async def cb_prices(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    if not callback.message or not callback.from_user:
        return
    await send_prices(callback.message, callback.from_user.id)


@router.message()
async def fallback(message: Message):
    await message.answer('/prices — prices | /help — commands', reply_markup=kb())


# ---- Scheduler ----

async def send_daily(bot: Bot):
    current_hour = datetime.now(tz=DELIVERY_TIMEZONE).hour
    targets = [v for v in registered_users.values()
               if v.get('active') and v.get('delivery_hour', DEFAULT_DELIVERY_HOUR) == current_hour]
    if not targets:
        return
    try:
        data = await fetch_prices(force=True)
        if not data:
            log.error('daily: empty data')
            return
        text = build_msg(data)
    except Exception as e:
        log.error('daily fetch failed: %s', e)
        return
    sent = failed = 0
    for u in targets:
        try:
            await bot.send_message(chat_id=u['chat_id'], text=text, reply_markup=kb())
            sent += 1
        except Exception as e:
            failed += 1
            log.error('send failed: %s', e)
    log.info('Daily %02d:00 sent=%d failed=%d', current_hour, sent, failed)


async def setup_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command='prices', description='Get prices now'),
        BotCommand(command='settime', description='Set delivery hour'),
        BotCommand(command='status', description='Subscription info'),
        BotCommand(command='stop', description='Unsubscribe'),
        BotCommand(command='help', description='All commands'),
    ])


# ---- Health server ----

async def start_health_server():
    async def health(_req):
        return aio_web.Response(text='OK')
    app = aio_web.Application()
    app.router.add_get('/', health)
    app.router.add_get('/health', health)
    runner = aio_web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv('PORT', '10000'))
    site = aio_web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    log.info('Health server on port %d', port)
    return runner


# ---- Main ----

async def main():
    if not BOT_TOKEN:
        raise ValueError('BOT_TOKEN not set')
    runner = await start_health_server()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    scheduler = AsyncIOScheduler(timezone=DELIVERY_TIMEZONE)
    scheduler.add_job(send_daily, 'cron', minute=0, args=[bot], id='daily', replace_existing=True)
    scheduler.start()
    await setup_commands(bot)
    me = await bot.get_me()
    log.info('Bot started: @%s | Subscribers: %d', me.username, len(registered_users))
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        scheduler.shutdown(wait=False)
        await runner.cleanup()
        await bot.session.close()
        log.info('Stopped.')


if __name__ == '__main__':
    asyncio.run(main())
