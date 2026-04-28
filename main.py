import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
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
PRICE_CACHE_TTL_SECONDS = 120
DEFAULT_DELIVERY_HOUR = 9
DELIVERY_TIMEZONE = ZoneInfo("Asia/Tashkent")
USER_COOLDOWN_SECONDS = 10
HTTP_RETRY_MAX = 3
HTTP_TIMEOUT = 20

DEFAULT_CURRENCY = "usd"
DEFAULT_COINS = ["BTC", "ETH", "BNB", "TON", "XRP"]

COIN_MAP: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "BNB": "binancecoin",
    "TON": "the-open-network",
    "XRP": "ripple",
}

CURRENCY_FORMAT: dict[str, tuple[str, str]] = {
    "usd": ("$", ""),
    "eur": ("€", ""),
    "rub": ("", " ₽"),
    "uzs": ("", " UZS"),
    "kzt": ("", " ₸"),
}
ALLOWED_CURRENCIES = set(CURRENCY_FORMAT.keys())

router = Router()
_price_cache: dict[tuple, dict] = {}
_price_lock = asyncio.Lock()
_user_last_request: dict[int, float] = {}


# ── Persistence ──────────────────────────────────────────────────────────────

def load_subscribers() -> dict:
    if not os.path.exists(SUBSCRIBERS_FILE):
        return {}
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.error("Failed to load subscribers: %s", e)
        return {}
    users: dict = {}
    for uid, info in data.items():
        try:
            user_id = int(uid)
        except (TypeError, ValueError):
            continue
        if not isinstance(info, dict):
            continue
        if not isinstance(info.get("delivery_hour"), int) or not (0 <= info["delivery_hour"] <= 23):
            info["delivery_hour"] = DEFAULT_DELIVERY_HOUR
        currency = info.get("currency")
        if not isinstance(currency, str) or currency.lower() not in ALLOWED_CURRENCIES:
            info["currency"] = DEFAULT_CURRENCY
        else:
            info["currency"] = currency.lower()
        coins = info.get("coins")
        if isinstance(coins, list):
            cleaned = [c.upper() for c in coins if isinstance(c, str) and c.upper() in COIN_MAP]
            info["coins"] = cleaned if cleaned else list(DEFAULT_COINS)
        else:
            info["coins"] = list(DEFAULT_COINS)
        users[user_id] = info
    return users


def save_subscribers() -> None:
    tmp_path = f"{SUBSCRIBERS_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(registered_users, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, SUBSCRIBERS_FILE)
    except Exception as e:
        log.error("Failed to save subscribers: %s", e)
        try:
            os.remove(tmp_path)
        except OSError:
            pass


registered_users: dict = load_subscribers()


# ── Keyboard ─────────────────────────────────────────────────────────────────

def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Get prices now", callback_data="get_prices_now")]
        ]
    )


# ── Formatting ────────────────────────────────────────────────────────────────

def format_money(value, currency: str) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    abs_v = abs(value)
    if abs_v >= 1:
        s = f"{value:,.2f}"
    elif abs_v >= 0.01:
        s = f"{value:,.4f}"
    elif abs_v > 0:
        s = f"{value:,.8f}"
    else:
        s = "0.00"
    prefix, suffix = CURRENCY_FORMAT.get(currency, ("", f" {currency.upper()}"))
    return f"{prefix}{s}{suffix}"


def format_change(value) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    arrow = "🟢" if value >= 0 else "🔴"
    return f"{arrow} {value:+.2f}%"


def format_compact(value) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    abs_v = abs(value)
    for unit, threshold in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs_v >= threshold:
            return f"{value / threshold:,.2f}{unit}"
    return f"{value:,.2f}"


def format_updated_time(timestamp) -> str:
    if isinstance(timestamp, (int, float)):
        dt_utc = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return dt_utc.strftime("%Y-%m-%d %H:%M UTC")
    return "n/a"


def format_delivery_time(hour: int) -> str:
    return f"{hour:02d}:00 Asia/Tashkent"


def parse_delivery_hour(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    match = re.fullmatch(r"(\d{1,2})(?::(\d{1,2}))?", raw.strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) is not None else 0
    if not (0 <= hour <= 23) or minute != 0:
        return None
    return hour


# ── HTTP / Prices ─────────────────────────────────────────────────────────────

async def http_get_json(url: str) -> dict:
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    last_err: Optional[Exception] = None
    for attempt in range(HTTP_RETRY_MAX):
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        except Exception as e:
            last_err = e
            if attempt < HTTP_RETRY_MAX - 1:
                delay = 15 * (2 ** attempt)
                log.warning("HTTP failed (attempt %d/%d): %s — retry in %ds", attempt + 1, HTTP_RETRY_MAX, e, delay)
                await asyncio.sleep(delay)
    raise last_err


def _build_price_url(coin_ids: list[str], vs_currency: str) -> str:
    ids = ",".join(coin_ids)
    return (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=" + ids +
        "&vs_currencies=" + vs_currency +
        "&include_24hr_change=true"
        "&include_24hr_vol=true"
        "&include_market_cap=true"
        "&include_last_updated_at=true"
        "&precision=full"
    )


async def fetch_prices(coin_ids: list[str], vs_currency: str, force_refresh: bool = False) -> dict:
    cache_key = (tuple(sorted(coin_ids)), vs_currency)
    now = time.monotonic()
    cached = _price_cache.get(cache_key)
    if not force_refresh and cached and now - cached["fetched_at"] < PRICE_CACHE_TTL_SECONDS:
        return cached["data"]
    async with _price_lock:
        now = time.monotonic()
        cached = _price_cache.get(cache_key)
        if not force_refresh and cached and now - cached["fetched_at"] < PRICE_CACHE_TTL_SECONDS:
            return cached["data"]
        data = await http_get_json(_build_price_url(coin_ids, vs_currency))
        _price_cache[cache_key] = {"data": data, "fetched_at": time.monotonic()}
        return data


# ── Message builders ──────────────────────────────────────────────────────────

def build_snapshot_message(data: dict, coins: list[str], currency: str) -> str:
    lines = []
    last_updated_ts: Optional[float] = None
    for sym in coins:
        coin_id = COIN_MAP.get(sym)
        info = data.get(coin_id, {}) if coin_id else {}
        price = info.get(currency)
        change = info.get(currency + "_24h_change")
        ts = info.get("last_updated_at")
        if isinstance(ts, (int, float)) and (last_updated_ts is None or ts > last_updated_ts):
            last_updated_ts = ts
        price_str = format_money(price, currency)
        change_val = change if isinstance(change, (int, float)) else 0
        arrow = "🟢" if change_val >= 0 else "🔴"
        change_str = f"{change_val:+.2f}%" if isinstance(change, (int, float)) else "n/a"
        lines.append(f"{arrow} <b>{sym}</b>  <code>{price_str}</code>  {change_str}")

    body = "\n".join(lines)
    return (
        "📈 <b>Crypto snapshot</b> [" + currency.upper() + "]\n\n"
        + body +
        "\n\n<i>Updated: " + format_updated_time(last_updated_ts) + "</i>"
    )


def build_coin_detail_message(data: dict, sym: str, currency: str) -> str:
    coin_id = COIN_MAP.get(sym)
    info = data.get(coin_id, {}) if coin_id else {}
    price = info.get(currency)
    change = info.get(currency + "_24h_change")
    market_cap = info.get(currency + "_market_cap")
    volume = info.get(currency + "_24h_vol")
    ts = info.get("last_updated_at")
    cur_upper = currency.upper()
    change_val = change if isinstance(change, (int, float)) else 0
    arrow = "🟢" if change_val >= 0 else "🔴"
    return (
        "🪙 <b>" + sym + "</b>  [" + cur_upper + "]\n\n"
        "💵 Price:       <code>" + format_money(price, currency) + "</code>\n"
        + arrow + " 24h change:  <code>" + (f"{change_val:+.2f}%" if isinstance(change, (int, float)) else "n/a") + "</code>\n"
        "📦 Market cap: <code>" + format_compact(market_cap) + " " + cur_upper + "</code>\n"
        "📊 24h volume: <code>" + format_compact(volume) + " " + cur_upper + "</code>\n\n"
        "<i>Updated: " + format_updated_time(ts) + "</i>"
    )


# ── Cooldown ──────────────────────────────────────────────────────────────────

def check_cooldown(user_id: int) -> float:
    elapsed = time.monotonic() - _user_last_request.get(user_id, 0.0)
    return max(0.0, USER_COOLDOWN_SECONDS - elapsed)


def mark_user_request(user_id: int) -> None:
    _user_last_request[user_id] = time.monotonic()


def coin_ids_for(coins: list[str]) -> list[str]:
    return [COIN_MAP[c] for c in coins if c in COIN_MAP]


async def send_snapshot_to_user(message: Message, user: dict) -> None:
    coins = user.get("coins", DEFAULT_COINS)
    currency = user.get("currency", DEFAULT_CURRENCY)
    coin_ids = coin_ids_for(coins)
    if not coin_ids:
        await message.answer("Your coin list is empty. Use /resetcoins to restore defaults.", reply_markup=get_main_keyboard())
        return
    try:
        data = await fetch_prices(coin_ids, currency)
    except Exception as e:
        log.error("Failed to fetch prices: %s", e)
        await message.answer("⚠️ Could not fetch prices right now. Please try again in a moment.", reply_markup=get_main_keyboard())
        return
    await message.answer(build_snapshot_message(data, coins, currency), reply_markup=get_main_keyboard())


async def safe_callback_answer(callback: CallbackQuery, text: Optional[str] = None) -> None:
    try:
        await callback.answer(text) if text else await callback.answer()
    except TelegramBadRequest as e:
        log.warning("Could not answer callback: %s", e)


# ── Handlers ──────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    existing = registered_users.get(user_id)
    if existing and existing.get("active"):
        await message.answer(
            "✅ You are already subscribed.\n\n"
            "Coins: <b>" + ", ".join(existing.get("coins", DEFAULT_COINS)) + "</b>\n"
            "Currency: <b>" + existing.get("currency", DEFAULT_CURRENCY).upper() + "</b>\n"
            "Daily at: <b>" + format_delivery_time(existing.get("delivery_hour", DEFAULT_DELIVERY_HOUR)) + "</b>\n\n"
            "Use /help to see all commands.",
            reply_markup=get_main_keyboard()
        )
        return
    registered_users[user_id] = {
        "chat_id": message.chat.id,
        "registered_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "active": True,
        "delivery_hour": DEFAULT_DELIVERY_HOUR,
        "currency": DEFAULT_CURRENCY,
        "coins": list(DEFAULT_COINS),
    }
    save_subscribers()
    await message.answer(
        "<b>👋 Welcome to Hamza Rates!</b>\n\n"
        "You are subscribed. Daily snapshot: <b>" + ", ".join(DEFAULT_COINS) + "</b>\n"
        "Currency: <b>" + DEFAULT_CURRENCY.upper() + "</b> · Time: <b>" + format_delivery_time(DEFAULT_DELIVERY_HOUR) + "</b>\n\n"
        "/prices — prices right now\n"
        "/setcurrency USD — change currency (USD, EUR, RUB, UZS, KZT)\n"
        "/settime 21 — change delivery hour\n"
        "/status — your settings\n"
        "/help — all commands",
        reply_markup=get_main_keyboard()
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    user = registered_users.get(message.from_user.id)
    if user and user.get("active"):
        user["active"] = False
        save_subscribers()
        await message.answer("❌ Unsubscribed. Send /start to subscribe again.")
    else:
        await message.answer("You are not subscribed. Send /start.")


@router.message(Command("prices"))
async def cmd_prices(message: Message):
    user_id = message.from_user.id
    cooldown = check_cooldown(user_id)
    if cooldown > 0:
        await message.answer(f"⏳ Please wait {int(cooldown) + 1}s.", reply_markup=get_main_keyboard())
        return
    user = registered_users.get(user_id) or {"coins": list(DEFAULT_COINS), "currency": DEFAULT_CURRENCY}
    mark_user_request(user_id)
    await send_snapshot_to_user(message, user)


@router.message(Command("coin"))
async def cmd_coin(message: Message, command: CommandObject):
    user_id = message.from_user.id
    cooldown = check_cooldown(user_id)
    if cooldown > 0:
        await message.answer(f"⏳ Please wait {int(cooldown) + 1}s.", reply_markup=get_main_keyboard())
        return
    arg = (command.args or "").strip().upper()
    if not arg or arg not in COIN_MAP:
        supported = ", ".join(COIN_MAP.keys())
        await message.answer("Usage: /coin SYMBOL\n\nSupported: <code>" + supported + "</code>", reply_markup=get_main_keyboard())
        return
    user = registered_users.get(user_id)
    currency = (user.get("currency") if user else None) or DEFAULT_CURRENCY
    mark_user_request(user_id)
    try:
        data = await fetch_prices([COIN_MAP[arg]], currency)
    except Exception as e:
        log.error("Failed to fetch /coin %s: %s", arg, e)
        await message.answer("⚠️ Could not fetch prices. Try again in a moment.", reply_markup=get_main_keyboard())
        return
    await message.answer(build_coin_detail_message(data, arg, currency), reply_markup=get_main_keyboard())


@router.message(Command("settime"))
async def cmd_settime(message: Message, command: CommandObject):
    user = registered_users.get(message.from_user.id)
    if not user or not user.get("active"):
        await message.answer("Send /start first.")
        return
    hour = parse_delivery_hour(command.args)
    if hour is None:
        await message.answer("Usage: /settime 9  or  /settime 21\nHour must be 0–23.")
        return
    user["delivery_hour"] = hour
    save_subscribers()
    await message.answer("✅ Delivery time set to <b>" + format_delivery_time(hour) + "</b>.")


@router.message(Command("setcurrency"))
async def cmd_setcurrency(message: Message, command: CommandObject):
    user = registered_users.get(message.from_user.id)
    if not user or not user.get("active"):
        await message.answer("Send /start first.")
        return
    arg = (command.args or "").strip().lower()
    if not arg or arg not in ALLOWED_CURRENCIES:
        supported = ", ".join(sorted(c.upper() for c in ALLOWED_CURRENCIES))
        await message.answer("Usage: /setcurrency CODE\n\nSupported: " + supported)
        return
    user["currency"] = arg
    save_subscribers()
    await message.answer("✅ Currency set to <b>" + arg.upper() + "</b>.")


@router.message(Command("resetcoins"))
async def cmd_resetcoins(message: Message):
    user = registered_users.get(message.from_user.id)
    if not user or not user.get("active"):
        await message.answer("Send /start first.")
        return
    user["coins"] = list(DEFAULT_COINS)
    save_subscribers()
    await message.answer("✅ Coins reset to: <b>" + ", ".join(DEFAULT_COINS) + "</b>")


@router.message(Command("status"))
async def cmd_status(message: Message):
    user = registered_users.get(message.from_user.id)
    if user and user.get("active"):
        await message.answer(
            "<b>⚙️ Your settings</b>\n\n"
            "Coins: <b>" + ", ".join(user.get("coins", DEFAULT_COINS)) + "</b>\n"
            "Currency: <b>" + user.get("currency", DEFAULT_CURRENCY).upper() + "</b>\n"
            "Daily at: <b>" + format_delivery_time(user.get("delivery_hour", DEFAULT_DELIVERY_HOUR)) + "</b>\n"
            "Since: <code>" + html.escape(user.get("registered_at", "unknown")) + "</code>",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer("Not subscribed. Send /start.", reply_markup=get_main_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message):
    coins_list = ", ".join(COIN_MAP.keys())
    await message.answer(
        "<b>Hamza Rates — commands</b>\n\n"
        "/start — subscribe\n"
        "/stop — unsubscribe\n"
        "/prices — snapshot now\n"
        "/coin BTC — detail for one coin\n"
        "/setcurrency USD — change currency\n"
        "/settime 9 — change delivery hour\n"
        "/resetcoins — reset coin list\n"
        "/status — your settings\n\n"
        "Tracked coins: <code>" + coins_list + "</code>\n"
        "Currencies: <code>USD, EUR, RUB, UZS, KZT</code>\n\n"
        "Data: CoinGecko",
        reply_markup=get_main_keyboard()
    )


@router.callback_query(F.data == "get_prices_now")
async def cb_get_prices_now(callback: CallbackQuery):
    await safe_callback_answer(callback)
    if not callback.message or not callback.from_user:
        return
    user_id = callback.from_user.id
    cooldown = check_cooldown(user_id)
    if cooldown > 0:
        await callback.message.answer(f"⏳ Please wait {int(cooldown) + 1}s.", reply_markup=get_main_keyboard())
        return
    user = registered_users.get(user_id) or {"coins": list(DEFAULT_COINS), "currency": DEFAULT_CURRENCY}
    mark_user_request(user_id)
    await send_snapshot_to_user(callback.message, user)


@router.message()
async def fallback_message(message: Message):
    await message.answer("❓ Unknown command. Use /help.", reply_markup=get_main_keyboard())


# ── Scheduled job ─────────────────────────────────────────────────────────────

async def send_hourly_snapshots(bot: Bot):
    current_hour = datetime.now(tz=DELIVERY_TIMEZONE).hour
    targets = [
        (uid, info) for uid, info in registered_users.items()
        if info.get("active") and int(info.get("delivery_hour", DEFAULT_DELIVERY_HOUR)) == current_hour
    ]
    if not targets:
        return
    sent = failed = 0
    for user_id, user_data in targets:
        coins = user_data.get("coins", DEFAULT_COINS)
        currency = user_data.get("currency", DEFAULT_CURRENCY)
        coin_ids = coin_ids_for(coins)
        if not coin_ids:
            continue
        try:
            data = await fetch_prices(coin_ids, currency, force_refresh=True)
            await bot.send_message(
                chat_id=user_data["chat_id"],
                text=build_snapshot_message(data, coins, currency),
                reply_markup=get_main_keyboard()
            )
            sent += 1
        except Exception as e:
            failed += 1
            log.error("Failed delivery to user %s: %s", user_id, e)
    log.info("Hourly %02d:00 Tashkent — sent: %d, failed: %d", current_hour, sent, failed)


async def setup_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Subscribe"),
        BotCommand(command="prices", description="Get prices now"),
        BotCommand(command="coin", description="Detail for one coin (e.g. /coin BTC)"),
        BotCommand(command="setcurrency", description="Change currency"),
        BotCommand(command="settime", description="Change delivery hour"),
        BotCommand(command="resetcoins", description="Reset coin list"),
        BotCommand(command="status", description="Your settings"),
        BotCommand(command="stop", description="Unsubscribe"),
        BotCommand(command="help", description="All commands"),
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception as e:
        log.error("Failed to set commands: %s", e)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set in environment variables")
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    scheduler = AsyncIOScheduler(timezone=DELIVERY_TIMEZONE)
    scheduler.add_job(send_hourly_snapshots, trigger="cron", minute=0, args=[bot],
                      id="hourly_snapshot", replace_existing=True)
    scheduler.start()
    await setup_bot_commands(bot)
    me = await bot.get_me()
    log.info("Bot started: @%s", me.username)
    log.info("Subscribers loaded: %d", len(registered_users))
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        log.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
