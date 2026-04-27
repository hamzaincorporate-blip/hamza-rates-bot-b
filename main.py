import asyncio
import logging
import os
from datetime import datetime, timezone

import aiohttp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")

router = Router()
registered_users = {}


def get_main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Get prices now", callback_data="get_prices_now")]
        ]
    )


def format_price(value):
    if isinstance(value, (int, float)):
        return f"{value:,.2f}"
    return "n/a"


def format_change(value):
    if isinstance(value, (int, float)):
        return f"{value:+.2f}%"
    return "n/a"


def format_updated_time(timestamp):
    if isinstance(timestamp, (int, float)):
        dt_utc = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return dt_utc.strftime("%Y-%m-%d %H:%M UTC")
    return "n/a"


async def fetch_prices():
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin,ethereum,binancecoin,the-open-network"
        "&vs_currencies=usd"
        "&include_24hr_change=true"
        "&include_last_updated_at=true"
        "&precision=full"
    )

    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=15) as response:
            response.raise_for_status()
            return await response.json()


def build_daily_message(data):
    btc = data.get("bitcoin", {})
    eth = data.get("ethereum", {})
    bnb = data.get("binancecoin", {})
    ton = data.get("the-open-network", {})

    last_updated = (
        btc.get("last_updated_at")
        or eth.get("last_updated_at")
        or bnb.get("last_updated_at")
        or ton.get("last_updated_at")
    )

    lines = [
        "Daily crypto snapshot (USD)",
        "",
        f"BTC  — ${format_price(btc.get('usd'))}   24h: {format_change(btc.get('usd_24h_change'))}",
        f"ETH  — ${format_price(eth.get('usd'))}   24h: {format_change(eth.get('usd_24h_change'))}",
        f"BNB  — ${format_price(bnb.get('usd'))}   24h: {format_change(bnb.get('usd_24h_change'))}",
        f"TON  — ${format_price(ton.get('usd'))}   24h: {format_change(ton.get('usd_24h_change'))}",
        "",
        f"Updated: {format_updated_time(last_updated)}",
    ]

    return "\n".join(lines)


@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    registered_users[user_id] = {
        "chat_id": chat_id,
        "registered_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "active": True,
    }

    text = (
        "Welcome to Hamza Rates.\n\n"
        "You are subscribed.\n"
        "You will receive a daily crypto snapshot with BTC, ETH, BNB and TON prices in USD.\n\n"
        "You can also check the latest prices anytime using the button below."
    )

    await message.answer(
        text,
        reply_markup=get_main_keyboard()
    )


@router.callback_query(F.data == "get_prices_now")
async def get_prices_now(callback: CallbackQuery):
    try:
        data = await fetch_prices()
        text = build_daily_message(data)

        await callback.message.answer(
            text,
            reply_markup=get_main_keyboard()
        )
        await callback.answer()
    except Exception as e:
        logging.error(f"Failed to fetch prices on demand: {e}")
        await callback.answer("Could not fetch prices right now.", show_alert=True)


@router.message()
async def fallback_message(message: Message):
    await message.answer(
        "Send /start to subscribe.",
        reply_markup=get_main_keyboard()
    )


async def send_daily_snapshot(bot: Bot):
    if not registered_users:
        logging.info("No registered users yet.")
        return

    try:
        data = await fetch_prices()
        text = build_daily_message(data)

        for _, user_data in registered_users.items():
            if not user_data.get("active"):
                continue

            chat_id = user_data["chat_id"]

            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=get_main_keyboard()
                )
            except Exception as e:
                logging.error(f"Failed to send message to {chat_id}: {e}")

    except Exception as e:
        logging.error(f"Daily snapshot job failed: {e}")


async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not found in environment variables")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone="Asia/Tashkent")
    scheduler.add_job(
        send_daily_snapshot,
        trigger="cron",
        hour=9,
        minute=0,
        args=[bot],
        id="daily_crypto_snapshot",
        replace_existing=True,
    )
    scheduler.start()

    me = await bot.get_me()
    logging.info(f"Bot started: @{me.username}")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
