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
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
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
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")

SUBSCRIBERS_FILE = "subscribers.json"
DEFAULT_DELIVERY_HOUR = 9
DELIVERY_TIMEZONE = ZoneInfo("Asia/Tashkent")
USER_COOLDOWN_SECONDS = 8
CACHE_TTL = 60

COINS = ["BTC", "ETH", "BNB", "TON", "XRP"]
CG_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "BNB": "binancecoin",
    "TON": "the-open-network",
    "XRP": "ripple",
}

router = Router()
_price_cache: dict = {}
_price_cache_time = 0.0
_user_last_request: dict[int, float] = {}


# ---------------- Persistence ----------------

def load_subscribers() -> dict:
    if not os.path.exists(SUBSCRIBERS_FILE):
        return {}
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception as e:
        log.error("load_subscribers error: %s", e)
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
        log.error("save_subscribers error: %s", e)


registered_users = load_subscribers()


# ---------------- Helpers ----------------

def keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Get prices now", callback_data="get_prices")]
        ]
    )


def check_cooldown(user_id: int) -> float:
    return max(
        0.0,
        USER_COOLDOWN_SECONDS - (time.monotonic() - _user_last_request.get(user_id, 0.0)),
    )


def mark_request(user_id: int) -> None:
    _user_last_request[user_id] = time.monotonic()


def format_price(value: float) -> str:
    if value >= 1000:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:,.2f}"
    if value >= 0.01:
        return f"${value:,.4f}"
    return f"${value:,.6f}"


def build_prices_message(data: dict) -> str:
    lines = []
    for coin in COINS:
        info = data.get(coin)
        if not info:
            lines.append(f"• <b>{coin}</b>: n/a")
            continue

        price = info["price"]
        change = info["change"]
        icon = "🟢" if change >= 0 else "🔴"
        lines.append(
            f"{icon} <b>{coin}</b>  <code>{format_price(price)}</code>  {change:+.2f}%"
        )

    ts = datetime.now(tz=DELIVERY_TIMEZONE).strftime("%H:%M, %d %b")
    return (
        "<b>Crypto prices [USD]</b>\n\n"
        + "\n".join(lines)
        + f"\n\n<i>{ts} Tashkent</i>"
    )


# ---------------- CoinGecko ----------------

async def fetch_prices(force: bool = False) -> dict:
    global _price_cache, _price_cache_time

    now = time.monotonic()
    if not force and _price_cache and now - _price_cache_time < CACHE_TTL:
        return _price_cache

    ids = ",".join(CG_IDS.values())
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={ids}"
        "&vs_currencies=usd"
        "&include_24hr_change=true"
        "&precision=full"
    )

    headers = {}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            raw = await resp.json()

    result = {}
    for symbol, cg_id in CG_IDS.items():
        info = raw.get(cg_id, {})
        price = info.get("usd")
        change = info.get("usd_24h_change")
        if price is not None:
            result[symbol] = {
                "price": float(price),
                "change": float(change or 0.0),
            }

    if result:
        _price_cache = result
        _price_cache_time = time.monotonic()

    return result


# ---------------- Commands ----------------

@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    existing = registered_users.get(user_id)

    if existing and existing.get("active"):
        hour = existing.get("delivery_hour", DEFAULT_DELIVERY_HOUR)
        await message.answer(
            "You are already subscribed.\n\n"
            f"Daily snapshot time: <b>{hour:02d}:00 Asia/Tashkent</b>\n\n"
            "Use /prices to get prices now.",
            reply_markup=keyboard(),
        )
        return

    registered_users[user_id] = {
        "chat_id": message.chat.id,
        "active": True,
        "delivery_hour": DEFAULT_DELIVERY_HOUR,
        "registered_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    save_subscribers()

    await message.answer(
        "<b>Welcome to Hamza Rates!</b>\n\n"
        "Tracked coins: <b>BTC, ETH, BNB, TON, XRP</b>\n"
        "Currency: <b>USD</b>\n"
        f"Daily time: <b>{DEFAULT_DELIVERY_HOUR:02d}:00 Asia/Tashkent</b>\n\n"
        "/prices — prices now\n"
        "/settime 21 — change delivery hour\n"
        "/status — your settings\n"
        "/stop — unsubscribe",
        reply_markup=keyboard(),
    )


@router.message(Command("prices"))
async def cmd_prices(message: Message):
    user_id = message.from_user.id
    wait = check_cooldown(user_id)
    if wait > 0:
        await message.answer(f"Please wait {int(wait) + 1}s.")
        return

    mark_request(user_id)

    try:
        data = await fetch_prices()
        await message.answer(build_prices_message(data), reply_markup=keyboard())
    except Exception as e:
        log.error("fetch error: %s", e)
        await message.answer(
            "Could not get prices right now. Try again in a moment.",
            reply_markup=keyboard(),
        )


@router.message(Command("settime"))
async def cmd_settime(message: Message):
    user = registered_users.get(message.from_user.id)
    if not user or not user.get("active"):
        await message.answer("Send /start first.")
        return

    parts = (message.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Usage: /settime 9")
        return

    hour = int(parts[1])
    if not (0 <= hour <= 23):
        await message.answer("Hour must be from 0 to 23.")
        return

    user["delivery_hour"] = hour
    save_subscribers()

    await message.answer(f"Daily delivery time set to <b>{hour:02d}:00 Asia/Tashkent</b>.")


@router.message(Command("status"))
async def cmd_status(message: Message):
    user = registered_users.get(message.from_user.id)
    if user and user.get("active"):
        hour = user.get("delivery_hour", DEFAULT_DELIVERY_HOUR)
        await message.answer(
            "Subscribed.\n\n"
            "Coins: <b>BTC, ETH, BNB, TON, XRP</b>\n"
            f"Time: <b>{hour:02d}:00 Asia/Tashkent</b>",
            reply_markup=keyboard(),
        )
    else:
        await message.answer("You are not subscribed. Send /start.", reply_markup=keyboard())


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    user = registered_users.get(message.from_user.id)
    if user and user.get("active"):
        user["active"] = False
        save_subscribers()
        await message.answer("You have been unsubscribed. Send /start to subscribe again.")
    else:
        await message.answer("You are not subscribed. Send /start.")


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Hamza Rates</b>\n\n"
        "/prices — get prices now\n"
        "/settime 9 — set daily delivery hour\n"
        "/status — your settings\n"
        "/stop — unsubscribe\n\n"
        "Coins: BTC, ETH, BNB, TON, XRP\n"
        "Source: CoinGecko",
        reply_markup=keyboard(),
    )


@router.callback_query(F.data == "get_prices")
async def cb_get_prices(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass

    if not callback.message or not callback.from_user:
        return

    user_id = callback.from_user.id
    wait = check_cooldown(user_id)
    if wait > 0:
        await callback.message.answer(f"Please wait {int(wait) + 1}s.")
        return

    mark_request(user_id)

    try:
        data = await fetch_prices()
        await callback.message.answer(build_prices_message(data), reply_markup=keyboard())
    except Exception as e:
        log.error("fetch error: %s", e)
        await callback.message.answer(
            "Could not get prices right now. Try again in a moment.",
            reply_markup=keyboard(),
        )


@router.message()
async def fallback(message: Message):
    await message.answer(
        "Use /prices to get crypto prices or /help to see commands.",
        reply_markup=keyboard(),
    )


# ---------------- Scheduler ----------------

async def send_daily_snapshots(bot: Bot):
    current_hour = datetime.now(tz=DELIVERY_TIMEZONE).hour

    targets = [
        info
        for info in registered_users.values()
        if info.get("active") and info.get("delivery_hour", DEFAULT_DELIVERY_HOUR) == current_hour
    ]

    if not targets:
        return

    try:
        data = await fetch_prices(force=True)
        text = build_prices_message(data)
    except Exception as e:
        log.error("daily fetch failed: %s", e)
        return

    sent = 0
    failed = 0

    for user in targets:
        try:
            await bot.send_message(
                chat_id=user["chat_id"],
                text=text,
                reply_markup=keyboard(),
            )
            sent += 1
        except Exception as e:
            failed += 1
            log.error("send failed: %s", e)

    log.info("Daily snapshot done — sent=%d failed=%d", sent, failed)


async def setup_commands(bot: Bot):
    await bot.set_my_commands(
        [
            BotCommand(command="prices", description="Get prices now"),
            BotCommand(command="settime", description="Set daily delivery hour"),
            BotCommand(command="status", description="Your settings"),
            BotCommand(command="stop", description="Unsubscribe"),
            BotCommand(command="help", description="All commands"),
        ]
    )


# ---------------- Health server ----------------

async def start_health_server():
    async def health(_request):
        return aio_web.Response(text="OK")

    app = aio_web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = aio_web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = aio_web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    log.info("Health server started on port %d", port)
    return runner


# ---------------- Main ----------------

async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set")

    runner = await start_health_server()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=DELIVERY_TIMEZONE)
    scheduler.add_job(
        send_daily_snapshots,
        trigger="cron",
        minute=0,
        args=[bot],
        id="daily_snapshot",
        replace_existing=True,
    )
    scheduler.start()

    await setup_commands(bot)

    me = await bot.get_me()
    log.info("Bot started: @%s | Subscribers: %d", me.username, len(registered_users))

    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        scheduler.shutdown(wait=False)
        await runner.cleanup()
        await bot.session.close()
        log.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
