import asyncio
import html
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
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
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hamza-rates")

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUBSCRIBERS_FILE = "subscribers.json"
DEFAULT_DELIVERY_HOUR = 9
DELIVERY_TIMEZONE = ZoneInfo("Asia/Tashkent")
USER_COOLDOWN_SECONDS = 10
CACHE_TTL = 60

COINS = ["BTC", "ETH", "BNB", "TON", "XRP"]
BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "BNB": "BNBUSDT",
    "TON": "TONUSDT",
    "XRP": "XRPUSDT",
}

router = Router()
_cache: dict = {}
_cache_time: float = 0.0
_user_last_request: dict[int, float] = {}


# Persistence

def load_subscribers() -> dict:
    if not os.path.exists(SUBSCRIBERS_FILE):
        return {}
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception as e:
        log.error("Failed to load subscribers: %s", e)
        return {}


def save_subscribers() -> None:
    tmp = SUBSCRIBERS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(registered_users, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SUBSCRIBERS_FILE)
    except Exception as e:
        log.error("Failed to save subscribers: %s", e)


registered_users: dict = load_subscribers()


# Binance API

async def fetch_prices(force: bool = False) -> dict:
    global _cache, _cache_time
    now = time.monotonic()
    if not force and _cache and now - _cache_time < CACHE_TTL:
        return _cache

    symbols = list(BINANCE_SYMBOLS.values())
    url = "https://api.binance.com/api/v3/ticker/24hr?symbols=" + json.dumps(symbols)

    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            data = await resp.json()

    result = {}
    for item in data:
        sym = item["symbol"]
        coin = next((c for c, s in BINANCE_SYMBOLS.items() if s == sym), None)
        if coin:
            result[coin] = {
                "price": float(item["lastPrice"]),
                "change": float(item["priceChangePercent"]),
                "volume": float(item["quoteVolume"]),
            }
    _cache = result
    _cache_time = time.monotonic()
    return result


# Message builder

def build_prices_message(data: dict) -> str:
    lines = []
    for coin in COINS:
        info = data.get(coin)
        if not info:
            lines.append("- <b>" + coin + "</b>: n/a")
            continue
        price = info["price"]
        change = info["change"]
        arrow = "🟢" if change >= 0 else "🔴"
        if price >= 1:
            price_str = "$" + f"{price:,.2f}"
        else:
            price_str = "$" + f"{price:,.4f}"
        change_str = f"{change:+.2f}%"
        lines.append(arrow + " <b>" + coin + "</b>  <code>" + price_str + "</code>  " + change_str)

    now_str = datetime.now(tz=DELIVERY_TIMEZONE).strftime("%H:%M, %d %b")
    return "📊 <b>Crypto prices [USD]</b>\n\n" + "\n".join(lines) + "\n\n<i>" + now_str + " Tashkent</i>"


# Keyboard

def kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Update prices", callback_data="get_prices")]
    ])


# Cooldown

def check_cooldown(user_id: int) -> float:
    return max(0.0, USER_COOLDOWN_SECONDS - (time.monotonic() - _user_last_request.get(user_id, 0.0)))


def mark_request(user_id: int) -> None:
    _user_last_request[user_id] = time.monotonic()


# Handlers

@router.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id
    existing = registered_users.get(uid)
    if existing and existing.get("active"):
        hour = existing.get("delivery_hour", DEFAULT_DELIVERY_HOUR)
        await message.answer(
            "You are already subscribed.\n"
            "Daily snapshot at <b>" + f"{hour:02d}:00 Tashkent</b>.\n\n"
            "Use /prices to get prices now.",
            reply_markup=kb()
        )
        return
    registered_users[uid] = {
        "chat_id": message.chat.id,
        "active": True,
        "delivery_hour": DEFAULT_DELIVERY_HOUR,
        "registered_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    save_subscribers()
    await message.answer(
        "<b>Welcome to Hamza Rates!</b>\n\n"
        "I send BTC, ETH, BNB, TON, XRP prices every day at "
        "<b>" + f"{DEFAULT_DELIVERY_HOUR:02d}:00 Tashkent</b>.\n\n"
        "/prices — prices right now\n"
        "/settime 21 — change delivery hour\n"
        "/stop — unsubscribe",
        reply_markup=kb()
    )


@router.message(Command("prices"))
async def cmd_prices(message: Message):
    uid = message.from_user.id
    wait = check_cooldown(uid)
    if wait > 0:
        await message.answer("Please wait " + str(int(wait) + 1) + "s and try again.")
        return
    mark_request(uid)
    try:
        data = await fetch_prices()
        await message.answer(build_prices_message(data), reply_markup=kb())
    except Exception as e:
        log.error("fetch error: %s", e)
        await message.answer("Could not get prices. Try again in a moment.", reply_markup=kb())


@router.message(Command("settime"))
async def cmd_settime(message: Message):
    uid = message.from_user.id
    user = registered_users.get(uid)
    if not user or not user.get("active"):
        await message.answer("Send /start first.")
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit() or not (0 <= int(parts[1]) <= 23):
        await message.answer("Usage: /settime 9  (hour 0-23)")
        return
    hour = int(parts[1])
    user["delivery_hour"] = hour
    save_subscribers()
    await message.answer("Daily snapshot set to <b>" + f"{hour:02d}:00 Tashkent</b>.")


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    user = registered_users.get(message.from_user.id)
    if user and user.get("active"):
        user["active"] = False
        save_subscribers()
        await message.answer("Unsubscribed. Send /start to subscribe again.")
    else:
        await message.answer("You are not subscribed. Send /start.")


@router.message(Command("status"))
async def cmd_status(message: Message):
    user = registered_users.get(message.from_user.id)
    if user and user.get("active"):
        hour = user.get("delivery_hour", DEFAULT_DELIVERY_HOUR)
        await message.answer(
            "Subscribed.\nDaily at: <b>" + f"{hour:02d}:00 Tashkent</b>",
            reply_markup=kb()
        )
    else:
        await message.answer("Not subscribed. Send /start.", reply_markup=kb())


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Hamza Rates</b>\n\n"
        "/prices — BTC ETH BNB TON XRP prices now\n"
        "/settime 9 — set daily delivery hour (0-23)\n"
        "/status — subscription info\n"
        "/stop — unsubscribe\n\n"
        "Data: Binance",
        reply_markup=kb()
    )


@router.callback_query(F.data == "get_prices")
async def cb_prices(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    if not callback.message or not callback.from_user:
        return
    uid = callback.from_user.id
    wait = check_cooldown(uid)
    if wait > 0:
        await callback.message.answer("Please wait " + str(int(wait) + 1) + "s and try again.")
        return
    mark_request(uid)
    try:
        data = await fetch_prices()
        await callback.message.answer(build_prices_message(data), reply_markup=kb())
    except Exception as e:
        log.error("fetch error: %s", e)
        await callback.message.answer("Could not get prices. Try again in a moment.", reply_markup=kb())


@router.message()
async def fallback(message: Message):
    await message.answer("Use /prices for prices or /help for commands.", reply_markup=kb())


# Scheduled job

async def send_daily_snapshots(bot: Bot):
    current_hour = datetime.now(tz=DELIVERY_TIMEZONE).hour
    targets = [
        (uid, info) for uid, info in registered_users.items()
        if info.get("active") and info.get("delivery_hour", DEFAULT_DELIVERY_HOUR) == current_hour
    ]
    if not targets:
        return
    try:
        data = await fetch_prices(force=True)
        text = build_prices_message(data)
    except Exception as e:
        log.error("Daily snapshot fetch failed: %s", e)
        return
    sent = failed = 0
    for uid, info in targets:
        try:
            await bot.send_message(chat_id=info["chat_id"], text=text, reply_markup=kb())
            sent += 1
        except Exception as e:
            failed += 1
            log.error("Failed to send to %s: %s", uid, e)
    log.info("Daily %02d:00 — sent: %d, failed: %d", current_hour, sent, failed)


async def setup_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="prices", description="Get prices now"),
        BotCommand(command="settime", description="Set daily delivery hour"),
        BotCommand(command="status", description="Subscription info"),
        BotCommand(command="stop", description="Unsubscribe"),
        BotCommand(command="help", description="All commands"),
    ])


async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set")
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    scheduler = AsyncIOScheduler(timezone=DELIVERY_TIMEZONE)
    scheduler.add_job(send_daily_snapshots, "cron", minute=0, args=[bot],
                      id="daily_snapshot", replace_existing=True)
    scheduler.start()
    await setup_commands(bot)
    me = await bot.get_me()
    log.info("Started: @%s | Subscribers: %d", me.username, len(registered_users))
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        log.info("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())
