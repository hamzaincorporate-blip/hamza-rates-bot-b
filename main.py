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

# Symbol -> CoinGecko ID for the most popular coins.
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

# Currency code (lowercase, sent to CoinGecko) -> (prefix, suffix) for display.
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
    "pln": ("", " zł"),
    "btc": ("₿ ", ""),
    "eth": ("Ξ ", ""),
}
ALLOWED_CURRENCIES = set(CURRENCY_FORMAT.keys())

router = Router()

_price_cache: dict[tuple, dict] = {}
_price_lock = asyncio.Lock()
_user_last_request: dict[int, float] = {}


# ---------- Persistence ----------

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


# ---------- UI ----------

def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Get prices now", callback_data="get_prices_now")]
        ]
    )


# ---------- Formatting ----------

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
    # ✅ ИСПРАВЛЕНО: был r"(\\d{1,2})..." — двойной слэш не матчил цифры
    match = re.fullmatch(r"(\d{1,2})(?::(\d{1,2}))?", raw)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) is not None else 0
    if not (0 <= hour <= 23) or minute != 0:
        return None
    return hour


# ---------- HTTP / Prices ----------

async def http_get_json(url: str) -> dict:
    last_err: Optional[Exception] = None
    for attempt in range(HTTP_RETRY_MAX):
        try:
            async with aiohttp.ClientSession() as session:
                # ✅ ИСПРАВЛЕНО: был timeout=HTTP_TIMEOUT (int) — aiohttp требует ClientTimeout
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as response:
                    response.raise_for_status()
                    return await response.json()
        except Exception as e:
            last_err = e
            if attempt < HTTP_RETRY_MAX - 1:
                delay = 2 ** attempt
                log.warning(
                    "HTTP fetch failed (attempt %d/%d): %s — retrying in %ds",
                    attempt + 1, HTTP_RETRY_MAX, e, delay,
                )
                await asyncio.sleep(delay)
    assert last_err is not None
    raise last_err


def _build_price_url(coin_ids: list[str], vs_currency: str) -> str:
    return (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={','.join(coin_ids)}"
        f"&vs_currencies={vs_currency}"
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


# ---------- Message builders ----------

def build_snapshot_message(data: dict, coins: list[str], currency: str) -> str:
    rows: list[tuple[str, str, str]] = []
    last_updated_ts: Optional[float] = None

    for sym in coins:
        coin_id = COIN_MAP.get(sym)
        info = data.get(coin_id, {}) if coin_id else {}
        price = info.get(currency)
        change = info.get(f"{currency}_24h_change")
        ts = info.get("last_updated_at")
        if isinstance(ts, (int, float)) and (last_updated_ts is None or ts > last_updated_ts):
            last_updated_ts = ts
        rows.append((sym, format_money(price, currency), format_change(change)))

    sym_w = max((len(r[0]) for r in rows), default=4)
    price_w = max((len(r[1]) for r in rows), default=8)
    change_w = max((len(r[2]) for r in rows), default=6)

    table = "\n".join(
        f"{sym:<{sym_w}}  {price:>{price_w}}  {change:>{change_w}}"
        for sym, price, change in rows
    )

    return (
        f"<b>Crypto snapshot</b> ({currency.upper()})\n\n"
        f"<pre>{html.escape(table)}</pre>\n"
        f"<i>Updated: {format_updated_time(last_updated_ts)}</i>"
    )


def build_coin_detail_message(data: dict, sym: str, currency: str) -> str:
    coin_id = COIN_MAP.get(sym)
    info = data.get(coin_id, {}) if coin_id else {}
    price = info.get(currency)
    change = info.get(f
