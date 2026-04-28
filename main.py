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
SUBSCRIBERS_FILE = "subscribers.json"
PRICE_CACHE_TTL_SECONDS = 30
DEFAULT_DELIVERY_HOUR = 9
DELIVERY_TIMEZONE = ZoneInfo("Asia/Tashkent")
USER_COOLDOWN_SECONDS = 5
MAX_COINS_PER_USER = 12
HTTP_RETRY_MAX = 3
HTTP_TIMEOUT = 15

DEFAULT_CURRENCY = "usd"
DEFAULT_COINS = ["BTC", "ETH", "BNB", "TON"]

COIN_MAP: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "BNB": "binancecoin",
    "TON": "the-open-network",
    "SOL": "solana",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "AVAX": "avalanche-2",
    "DOT": "polkadot",
    "MATIC": "matic-network",
    "LTC": "litecoin",
    "TRX": "tron",
    "SHIB": "shiba-inu",
    "LINK": "chainlink",
    "ATOM": "cosmos",
    "NEAR": "near",
    "USDT": "tether",
    "USDC": "usd-coin",
    "ARB": "arbitrum",
    "OP": "optimism",
    "SUI": "sui",
    "APT": "aptos",
    "PEPE": "pepe",
    "XLM": "stellar",
    "BCH": "bitcoin-cash",
    "ETC": "ethereum-classic",
    "FIL": "filecoin",
    "INJ": "injective-protocol",
    "RNDR": "render-token",
    "TIA": "celestia",
    "SEI": "sei-network",
    "FTM": "fantom",
    "ALGO": "algorand",
    "AAVE": "aave",
    "UNI": "uniswap",
    "MKR": "maker",
}

CURRENCY_FORMAT: dict[str, tuple[str, str]] = {
    "usd": ("$", ""),
    "eur": ("€", ""),
    "gbp": ("£", ""),
    "jpy": ("¥", ""),
    "cny": ("¥", ""),
    "rub": ("", " ₽"),
    "uzs": ("", " UZS"),
    "kzt": ("", " ₸"),
    "try": ("", " ₺"),
    "inr": ("₹", ""),
    "brl": ("R$", ""),
    "aud": ("A$", ""),
    "cad": ("C$", ""),
    "chf": ("", " CHF"),
    "krw": ("₩", ""),
    "uah": ("", " ₴"),
    "pln": ("", " zl"),
    "btc": ("₿ ", ""),
    "eth": ("Ξ ", ""),
}
ALLOWED_CURRENCIES = set(CURRENCY_FORMAT.keys())

router = Router()

_price_cache: dict[tuple, dict] = {}
_price_lock = asyncio.Lock()
_user_last_request: dict[int, float] = {}


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
            info["coins"] = cleaned[:MAX_COINS_PER_USER] if cleaned else list(DEFAULT_COINS)
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
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


registered_users: dict = load_subscribers()


def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Get prices now", callback_data="get_prices_now")]
        ]
    )


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
    return f"{value:+.2f}%"


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
    raw = raw.strip()
    match = re.fullmatch(r"(\d{1,2})(?::(\d{1,2}))?", raw)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) is not None else 0
    if not (0 <= hour <= 23) or minute != 0:
        return None
    return hour


async def http_get_json(url: str) -> dict:
    last_err: Optional[Exception] = None
    for attempt in range(HTTP_RETRY_MAX):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as response:
                    response.raise_for_status()
                    return await response.json()
        except Exception as e:
            last_err = e
            if attempt < HTTP_RETRY_MAX - 1:
                delay = 2 ** attempt
                log.warning(
                    "HTTP fetch failed (attempt %d/%d): %s - retrying in %ds",
                    attempt + 1, HTTP_RETRY_MAX, e, delay,
                )
                await asyncio.sleep(delay)
    assert last_err is not None
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


def build_snapshot_message(data: dict, coins: list[str], currency: str) -> str:
    rows: list[tuple[str, str, str]] = []
    last_updated_ts: Optional[float] = None
    for sym in coins:
        coin_id = COIN_MAP.get(sym)
        info = data.get(coin_id, {}) if coin_id else {}
        price = info.get(currency)
        change = info.get(currency + "_24h_change")
        ts = info.get("last_updated_at")
        if isinstance(ts, (int, float)) and (last_updated_ts is None or ts > last_updated_ts):
            last_updated_ts = ts
        rows.append((sym, format_money(price, currency), format_change(change)))
    sym_w = max((len(r[0]) for r in rows), default=4)
    price_w = max((len(r[1]) for r in rows), default=8)
    change_w = max((len(r[2]) for r in rows), default=6)
    lines = []
    for sym, price, change in rows:
        lines.append(f"{sym:<{sym_w}}  {price:>{price_w}}  {change:>{change_w}}")
    table = "\n".join(lines)
    return (
        "<b>Crypto snapshot</b> (" + currency.upper() + ")\n\n"
        "<pre>" + html.escape(table) + "</pre>\n"
        "<i>Updated: " + format_updated_time(last_updated_ts) + "</i>"
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
    return (
        "<b>" + sym + "</b>  (" + cur_upper + ")\n\n"
        "Price:        <code>" + format_money(price, currency) + "</code>\n"
        "24h change:   <code>" + format_change(change) + "</code>\n"
        "Market cap:   <code>" + format_compact(market_cap) + " " + cur_upper + "</code>\n"
        "24h volume:   <code>" + format_compact(volume) + " " + cur_upper + "</code>\n\n"
        "<i>Updated: " + format_updated_time(ts) + "</i>"
    )


def check_cooldown(user_id: int) -> float:
    last = _user_last_request.get(user_id, 0.0)
    elapsed = time.monotonic() - last
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
        await message.answer(
            "Your coin list is empty. Use /addcoin BTC to add one or /resetcoins to restore defaults.",
            reply_markup=get_main_keyboard()
        )
        return
    try:
        data = await fetch_prices(coin_ids, currency)
    except Exception as e:
        log.error("Failed to fetch prices on demand: %s", e)
        await message.answer(
            "Could not fetch prices right now. Please try again in a moment.",
            reply_markup=get_main_keyboard()
        )
        return
    await message.answer(
        build_snapshot_message(data, coins, currency),
        reply_markup=get_main_keyboard()
    )


async def safe_callback_answer(callback: CallbackQuery, text: Optional[str] = None, show_alert: bool = False) -> None:
    try:
        if text is None:
            await callback.answer()
        else:
            await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest as e:
        log.warning("Could not answer callback %s: %s", callback.id, e)


@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    existing = registered_users.get(user_id)
    if existing and existing.get("active"):
        text = (
            "You are already subscribed.\n\n"
            "Coins: <b>" + ", ".join(existing.get("coins", DEFAULT_COINS)) + "</b>\n"
            "Currency: <b>" + existing.get("currency", DEFAULT_CURRENCY).upper() + "</b>\n"
            "Delivery time: <b>" + format_delivery_time(existing.get("delivery_hour", DEFAULT_DELIVERY_HOUR)) + "</b>\n\n"
            "Use /help to see all commands."
        )
    else:
        registered_users[user_id] = {
            "chat_id": chat_id,
            "registered_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "active": True,
            "delivery_hour": DEFAULT_DELIVERY_HOUR,
            "currency": DEFAULT_CURRENCY,
            "coins": list(DEFAULT_COINS),
        }
        save_subscribers()
        text = (
            "<b>Welcome to Hamza Rates.</b>\n\n"
            "You are subscribed.\n"
            "You will receive a daily snapshot of <b>" + ", ".join(DEFAULT_COINS) + "</b> in "
            "<b>" + DEFAULT_CURRENCY.upper() + "</b> every day at "
            "<b>" + format_delivery_time(DEFAULT_DELIVERY_HOUR) + "</b>.\n\n"
            "Quick commands:\n"
            "/prices - get the current snapshot\n"
            "/coin BTC - detailed view of one coin\n"
            "/addcoin SYM - add a coin to your list\n"
            "/removecoin SYM - remove a coin\n"
            "/setcurrency USD - change currency (USD, EUR, RUB, UZS...)\n"
            "/settime 21 - change delivery hour\n"
            "/status - your settings\n"
            "/help - full command list"
        )
    await message.answer(text, reply_markup=get_main_keyboard())


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    user = registered_users.get(message.from_user.id)
    if user and user.get("active"):
        user["active"] = False
        save_subscribers()
        await message.answer(
            "You have been unsubscribed from the daily snapshot.\n"
            "Send /start to subscribe again."
        )
    else:
        await message.answer("You are not subscribed. Send /start to subscribe.")


@router.message(Command("prices"))
async def cmd_prices(message: Message):
    user_id = message.from_user.id
    cooldown = check_cooldown(user_id)
    if cooldown > 0:
        await message.answer(
            f"Please wait {int(cooldown) + 1}s before requesting prices again.",
            reply_markup=get_main_keyboard()
        )
        return
    user = registered_users.get(user_id) or {
        "coins": list(DEFAULT_COINS),
        "currency": DEFAULT_CURRENCY,
    }
    mark_user_request(user_id)
    await send_snapshot_to_user(message, user)


@router.message(Command("coin"))
async def cmd_coin(message: Message, command: CommandObject):
    user_id = message.from_user.id
    cooldown = check_cooldown(user_id)
    if cooldown > 0:
        await message.answer(
            f"Please wait {int(cooldown) + 1}s before requesting prices again.",
            reply_markup=get_main_keyboard()
        )
        return
    arg = (command.args or "").strip().upper()
    if not arg or arg not in COIN_MAP:
        await message.answer(
            "Usage: /coin SYMBOL  (e.g. /coin BTC)\n\n"
            "Use /supportedcoins to see all supported symbols.",
            reply_markup=get_main_keyboard()
        )
        return
    user = registered_users.get(user_id)
    currency = (user.get("currency") if user else None) or DEFAULT_CURRENCY
    mark_user_request(user_id)
    try:
        data = await fetch_prices([COIN_MAP[arg]], currency)
    except Exception as e:
        log.error("Failed to fetch /coin %s: %s", arg, e)
        await message.answer(
            "Could not fetch prices right now. Please try again in a moment.",
            reply_markup=get_main_keyboard()
        )
        return
    await message.answer(
        build_coin_detail_message(data, arg, currency),
        reply_markup=get_main_keyboard()
    )


@router.message(Command("settime"))
async def cmd_settime(message: Message, command: CommandObject):
    user = registered_users.get(message.from_user.id)
    if not user or not user.get("active"):
        await message.answer("You are not subscribed yet. Send /start first, then use /settime.")
        return
    hour = parse_delivery_hour(command.args)
    if hour is None:
        await message.answer(
            "Please send a valid hour from 0 to 23.\n\nExamples:\n/settime 9\n/settime 21\n/settime 07:00"
        )
        return
    user["delivery_hour"] = hour
    save_subscribers()
    await message.answer("Delivery time updated to <b>" + format_delivery_time(hour) + "</b>.")


@router.message(Command("setcurrency"))
async def cmd_setcurrency(message: Message, command: CommandObject):
    user = registered_users.get(message.from_user.id)
    if not user or not user.get("active"):
        await message.answer("You are not subscribed yet. Send /start first, then use /setcurrency.")
        return
    arg = (command.args or "").strip().lower()
    if not arg or arg not in ALLOWED_CURRENCIES:
        supported = ", ".join(sorted(c.upper() for c in ALLOWED_CURRENCIES))
        await message.answer("Usage: /setcurrency CODE\n\nSupported: " + supported)
        return
    user["currency"] = arg
    save_subscribers()
    await message.answer("Currency updated to <b>" + arg.upper() + "</b>.")


@router.message(Command("coins"))
async def cmd_coins(message: Message):
    user = registered_users.get(message.from_user.id)
    if not user or not user.get("active"):
        await message.answer("You are not subscribed yet. Send /start first.")
        return
    coins = user.get("coins", DEFAULT_COINS)
    await message.answer(
        "Your tracked coins: <b>" + ", ".join(coins) + "</b>\n\n"
        "Use /addcoin SYM to add, /removecoin SYM to remove, /resetcoins to restore defaults, "
        "or /supportedcoins to see all supported symbols."
    )


@router.message(Command("supportedcoins"))
async def cmd_supportedcoins(message: Message):
    symbols = ", ".join(sorted(COIN_MAP.keys()))
    await message.answer("Supported coins:\n\n<code>" + html.escape(symbols) + "</code>")


@router.message(Command("addcoin"))
async def cmd_addcoin(message: Message, command: CommandObject):
    user = registered_users.get(message.from_user.id)
    if not user or not user.get("active"):
        await message.answer("You are not subscribed yet. Send /start first.")
        return
    arg = (command.args or "").strip().upper()
    if not arg:
        await message.answer("Usage: /addcoin SYMBOL  (e.g. /addcoin SOL)")
        return
    if arg not in COIN_MAP:
        await message.answer(
            "<b>" + html.escape(arg) + "</b> is not in the supported list. Use /supportedcoins to see all symbols."
        )
        return
    coins = user.setdefault("coins", list(DEFAULT_COINS))
    if arg in coins:
        await message.answer("<b>" + arg + "</b> is already in your list.")
        return
    if len(coins) >= MAX_COINS_PER_USER:
        await message.answer(
            f"You already have {MAX_COINS_PER_USER} coins (the maximum). "
            "Remove one with /removecoin SYMBOL first."
        )
        return
    coins.append(arg)
    save_subscribers()
    await message.answer("Added <b>" + arg + "</b>. Your list: <b>" + ", ".join(coins) + "</b>")


@router.message(Command("removecoin"))
async def cmd_removecoin(message: Message, command: CommandObject):
    user = registered_users.get(message.from_user.id)
    if not user or not user.get("active"):
        await message.answer("You are not subscribed yet. Send /start first.")
        return
    arg = (command.args or "").strip().upper()
    if not arg:
        await message.answer("Usage: /removecoin SYMBOL  (e.g. /removecoin DOGE)")
        return
    coins = user.setdefault("coins", list(DEFAULT_COINS))
    if arg not in coins:
        await message.answer("<b>" + html.escape(arg) + "</b> is not in your list.")
        return
    coins.remove(arg)
    save_subscribers()
    if not coins:
        await message.answer(
            "Removed <b>" + arg + "</b>. Your list is now empty - use /addcoin SYM or /resetcoins."
        )
    else:
        await message.answer("Removed <b>" + arg + "</b>. Your list: <b>" + ", ".join(coins) + "</b>")


@router.message(Command("resetcoins"))
async def cmd_resetcoins(message: Message):
    user = registered_users.get(message.from_user.id)
    if not user or not user.get("active"):
        await message.answer("You are not subscribed yet. Send /start first.")
        return
    user["coins"] = list(DEFAULT_COINS)
    save_subscribers()
    await message.answer("Coin list reset to defaults: <b>" + ", ".join(DEFAULT_COINS) + "</b>")


@router.message(Command("status"))
async def cmd_status(message: Message):
    user = registered_users.get(message.from_user.id)
    if user and user.get("active"):
        registered_at = user.get("registered_at", "unknown")
        text = (
            "<b>Your subscription</b>\n\n"
            "Coins: <b>" + ", ".join(user.get("coins", DEFAULT_COINS)) + "</b>\n"
            "Currency: <b>" + user.get("currency", DEFAULT_CURRENCY).upper() + "</b>\n"
            "Delivery time: <b>" + format_delivery_time(user.get("delivery_hour", DEFAULT_DELIVERY_HOUR)) + "</b>\n"
            "Subscribed at: <code>" + html.escape(registered_at) + "</code>"
        )
    else:
        text = "You are not subscribed. Send /start to subscribe."
    await message.answer(text, reply_markup=get_main_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "<b>Hamza Rates - commands</b>\n\n"
        "/start - subscribe\n"
        "/stop - unsubscribe\n"
        "/status - your settings\n\n"
        "<b>Prices</b>\n"
        "/prices - your snapshot now\n"
        "/coin SYM - detailed view of one coin\n\n"
        "<b>Customize</b>\n"
        "/coins - list your tracked coins\n"
        "/addcoin SYM - add a coin\n"
        "/removecoin SYM - remove a coin\n"
        "/resetcoins - restore default list\n"
        "/supportedcoins - list all supported symbols\n"
        "/setcurrency CODE - change currency (USD, EUR, RUB, UZS...)\n"
        "/settime HH - daily delivery hour, Asia/Tashkent (0-23)\n\n"
        "Data: CoinGecko"
    )
    await message.answer(text, reply_markup=get_main_keyboard())


@router.callback_query(F.data == "get_prices_now")
async def get_prices_now(callback: CallbackQuery):
    await safe_callback_answer(callback)
    if callback.message is None or callback.from_user is None:
        return
    user_id = callback.from_user.id
    cooldown = check_cooldown(user_id)
    if cooldown > 0:
        try:
            await callback.message.answer(
                f"Please wait {int(cooldown) + 1}s before requesting prices again.",
                reply_markup=get_main_keyboard()
            )
        except Exception:
            pass
        return
    user = registered_users.get(user_id) or {
        "coins": list(DEFAULT_COINS),
        "currency": DEFAULT_CURRENCY,
    }
    mark_user_request(user_id)
    await send_snapshot_to_user(callback.message, user)


@router.message()
async def fallback_message(message: Message):
    await message.answer(
        "I did not recognise that. Use /help to see all commands.",
        reply_markup=get_main_keyboard()
    )


async def send_hourly_snapshots(bot: Bot):
    current_hour = datetime.now(tz=DELIVERY_TIMEZONE).hour
    targets = [
        (uid, info)
        for uid, info in registered_users.items()
        if info.get("active") and int(info.get("delivery_hour", DEFAULT_DELIVERY_HOUR)) == current_hour
    ]
    if not targets:
        return
    sent = 0
    failed = 0
    for user_id, user_data in targets:
        coins = user_data.get("coins", DEFAULT_COINS)
        currency = user_data.get("currency", DEFAULT_CURRENCY)
        coin_ids = coin_ids_for(coins)
        chat_id = user_data["chat_id"]
        if not coin_ids:
            continue
        try:
            data = await fetch_prices(coin_ids, currency, force_refresh=True)
            text = build_snapshot_message(data, coins, currency)
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=get_main_keyboard())
            sent += 1
        except Exception as e:
            failed += 1
            log.error("Failed to deliver snapshot to %s (user %s): %s", chat_id, user_id, e)
    log.info(
        "Hourly snapshot for %02d:00 Asia/Tashkent - sent to %d, %d failed.",
        current_hour, sent, failed,
    )


async def setup_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Subscribe"),
        BotCommand(command="prices", description="Get your snapshot"),
        BotCommand(command="coin", description="Detailed view of one coin"),
        BotCommand(command="coins", description="Your tracked coins"),
        BotCommand(command="addcoin", description="Add a coin"),
        BotCommand(command="removecoin", description="Remove a coin"),
        BotCommand(command="setcurrency", description="Change currency"),
        BotCommand(command="settime", description="Change delivery hour"),
        BotCommand(command="status", description="Your settings"),
        BotCommand(command="stop", description="Unsubscribe"),
        BotCommand(command="help", description="All commands"),
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception as e:
        log.error("Failed to set bot commands menu: %s", e)


async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not found in environment variables")
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    scheduler = AsyncIOScheduler(timezone=DELIVERY_TIMEZONE)
    scheduler.add_job(
        send_hourly_snapshots,
        trigger="cron",
        minute=0,
        args=[bot],
        id="hourly_crypto_snapshot",
        replace_existing=True,
    )
    scheduler.start()
    await setup_bot_commands(bot)
    me = await bot.get_me()
    log.info("Bot started: @%s", me.username)
    log.info("Loaded %d subscriber(s) from disk.", len(registered_users))
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        log.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
