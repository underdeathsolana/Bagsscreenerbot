#!/usr/bin/env python3
"""
Bags.fm Telegram Scanner Bot
Monitors new tokens on Bags.fm and sends alerts to Telegram
"""

import os
import asyncio
import aiohttp
import logging
import json
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, RetryAfter
import traceback

# ── Load env ──────────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
CHANNEL_ID     = os.getenv("CHANNEL_ID", "")
THREAD_ID      = int(os.getenv("THREAD_ID", "0")) or None
REF_LINK       = os.getenv("REF_LINK", "https://bags.fm")
BAGS_API_KEY   = os.getenv("BAGS_API_KEY", "")
BAGS_API_BASE  = os.getenv("BAGS_API_BASE", "https://api.bags.fm")
MIN_LIQUIDITY  = float(os.getenv("MIN_LIQUIDITY", "1000"))
MIN_VOLUME     = float(os.getenv("MIN_VOLUME", "2000"))
SCAN_INTERVAL  = int(os.getenv("SCAN_INTERVAL", "30"))        # seconds
LEADERBOARD_INTERVAL = int(os.getenv("LEADERBOARD_INTERVAL", "3600"))  # 1 hour

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bags_bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── State ─────────────────────────────────────────────────────────────────────
seen_tokens: set   = set()
tracked_tokens: dict = {}   # address -> {name, symbol, buy_price, alerts_sent, ...}
watchlist: dict    = {}     # address -> {name, symbol, target_price}
paused: bool       = False
stats = {"alerts_sent": 0, "tokens_scanned": 0, "start_time": time.time()}

# ── Bags.fm API ───────────────────────────────────────────────────────────────
HEADERS = {
    "Authorization": f"Bearer {BAGS_API_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

async def bags_get(session: aiohttp.ClientSession, path: str, params: dict = None):
    url = f"{BAGS_API_BASE}{path}"
    try:
        async with session.get(url, headers=HEADERS, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 200:
                return await r.json()
            else:
                log.warning(f"Bags API {path} → {r.status}")
                return None
    except Exception as e:
        log.error(f"Bags API error {path}: {e}")
        return None

async def fetch_new_tokens(session: aiohttp.ClientSession):
    """Fetch newest tokens from Bags.fm"""
    data = await bags_get(session, "/v1/tokens/new", {"limit": 50, "sort": "created_at", "order": "desc"})
    if data and isinstance(data, dict):
        return data.get("tokens", data.get("data", []))
    if data and isinstance(data, list):
        return data
    return []

async def fetch_trending_tokens(session: aiohttp.ClientSession):
    data = await bags_get(session, "/v1/tokens/trending", {"limit": 20})
    if data and isinstance(data, dict):
        return data.get("tokens", data.get("data", []))
    if data and isinstance(data, list):
        return data
    return []

async def fetch_token_detail(session: aiohttp.ClientSession, address: str):
    data = await bags_get(session, f"/v1/tokens/{address}")
    if data and isinstance(data, dict):
        return data.get("token", data)
    return None

async def fetch_token_price(session: aiohttp.ClientSession, address: str):
    data = await bags_get(session, f"/v1/tokens/{address}/price")
    if data and isinstance(data, dict):
        return data.get("price", data.get("priceUsd", 0))
    return 0

async def fetch_leaderboard(session: aiohttp.ClientSession):
    data = await bags_get(session, "/v1/tokens/gainers", {"limit": 10, "period": "24h"})
    if data and isinstance(data, dict):
        return data.get("tokens", data.get("data", []))
    if data and isinstance(data, list):
        return data
    return []

# ── Formatters ────────────────────────────────────────────────────────────────
def fmt_number(n):
    n = float(n or 0)
    if n >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n/1_000:.1f}K"
    return f"${n:.2f}"

def fmt_price(p):
    p = float(p or 0)
    if p < 0.000001:
        return f"${p:.10f}"
    if p < 0.001:
        return f"${p:.8f}"
    if p < 1:
        return f"${p:.6f}"
    return f"${p:.4f}"

def fmt_change(pct):
    pct = float(pct or 0)
    arrow = "🟢" if pct >= 0 else "🔴"
    sign  = "+" if pct >= 0 else ""
    return f"{arrow} {sign}{pct:.2f}%"

def age_str(created_at):
    try:
        if isinstance(created_at, (int, float)):
            ts = created_at / 1000 if created_at > 1e10 else created_at
            created = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - created
        mins  = int(delta.total_seconds() / 60)
        if mins < 60:
            return f"{mins}m ago"
        h = mins // 60
        if h < 24:
            return f"{h}h ago"
        return f"{h//24}d ago"
    except:
        return "unknown"

def safe_get(d, *keys, default=0):
    """Safely get nested dict value"""
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k, default)
        else:
            return default
    return d if d is not None else default

# ── Message Builders ──────────────────────────────────────────────────────────
def build_new_token_message(token: dict) -> tuple[str, InlineKeyboardMarkup]:
    address  = token.get("address", token.get("mint", token.get("id", "")))
    name     = token.get("name", "Unknown")
    symbol   = token.get("symbol", "???")
    price    = safe_get(token, "priceUsd", default=safe_get(token, "price", default=0))
    liq      = safe_get(token, "liquidity", "usd", default=safe_get(token, "liquidityUsd", default=0))
    vol24    = safe_get(token, "volume", "h24", default=safe_get(token, "volume24h", default=0))
    mcap     = safe_get(token, "marketCap", default=safe_get(token, "fdv", default=0))
    ch1h     = safe_get(token, "priceChange", "h1",  default=0)
    ch6h     = safe_get(token, "priceChange", "h6",  default=0)
    ch24h    = safe_get(token, "priceChange", "h24", default=0)
    created  = token.get("createdAt", token.get("created_at", token.get("pairCreatedAt", "")))
    holders  = token.get("holders", token.get("holderCount", "N/A"))

    bags_url   = f"https://bags.fm/t/{address}"
    buy_url    = f"{REF_LINK}"

    text = (
        f"🆕 *NEW TOKEN DETECTED*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷 *{name}* `${symbol}`\n"
        f"📋 `{address[:8]}...{address[-6:]}`\n\n"
        f"💰 *Price:* `{fmt_price(price)}`\n"
        f"💎 *Market Cap:* `{fmt_number(mcap)}`\n"
        f"💧 *Liquidity:* `{fmt_number(liq)}`\n"
        f"📊 *Vol 24h:* `{fmt_number(vol24)}`\n"
        f"👥 *Holders:* `{holders}`\n\n"
        f"📈 *Price Change:*\n"
        f"  1h: {fmt_change(ch1h)}\n"
        f"  6h: {fmt_change(ch6h)}\n"
        f"  24h: {fmt_change(ch24h)}\n\n"
        f"🕐 *Listed:* `{age_str(created)}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 [View on Bags.fm]({bags_url})\n"
        f"📱 *Powered by @underdeathsol*"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💸 BUY NOW", url=f"https://bags.fm/t/{address}?ref=LILFID12"),
            InlineKeyboardButton("📊 Chart", url=bags_url),
        ],
        [
            InlineKeyboardButton("🔍 Birdeye", url=f"https://birdeye.so/token/{address}"),
            InlineKeyboardButton("🦅 DexScreener", url=f"https://dexscreener.com/solana/{address}"),
        ],
        [
            InlineKeyboardButton("📋 Copy CA", callback_data=f"copy_{address}"),
            InlineKeyboardButton("⭐ Watchlist", callback_data=f"watch_{address}_{symbol}_{name[:15]}"),
        ],
    ])
    return text, keyboard

def build_pump_alert_message(token: dict, change_pct: float) -> tuple[str, InlineKeyboardMarkup]:
    address = token.get("address", token.get("mint", ""))
    name    = token.get("name", "Unknown")
    symbol  = token.get("symbol", "???")
    price   = safe_get(token, "priceUsd", default=0)
    liq     = safe_get(token, "liquidity", "usd", default=0)
    vol24   = safe_get(token, "volume", "h24", default=0)

    emoji = "🚀" if change_pct >= 50 else "⚡"
    text = (
        f"{emoji} *PUMP ALERT!*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷 *{name}* `${symbol}`\n"
        f"📋 `{address[:8]}...{address[-6:]}`\n\n"
        f"💰 *Price:* `{fmt_price(price)}`\n"
        f"📈 *Gain:* {fmt_change(change_pct)}\n"
        f"💧 *Liquidity:* `{fmt_number(liq)}`\n"
        f"📊 *Vol 24h:* `{fmt_number(vol24)}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 *@underdeathsol*"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💸 BUY NOW", url=f"https://bags.fm/t/{address}?ref=LILFID12"),
            InlineKeyboardButton("📊 Chart", url=f"https://bags.fm/t/{address}"),
        ],
        [
            InlineKeyboardButton("🦅 DexScreener", url=f"https://dexscreener.com/solana/{address}"),
        ],
    ])
    return text, keyboard

def build_volume_spike_message(token: dict, vol_ratio: float) -> tuple[str, InlineKeyboardMarkup]:
    address = token.get("address", token.get("mint", ""))
    name    = token.get("name", "Unknown")
    symbol  = token.get("symbol", "???")
    price   = safe_get(token, "priceUsd", default=0)
    vol24   = safe_get(token, "volume", "h24", default=0)

    text = (
        f"📊 *VOLUME SPIKE!*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷 *{name}* `${symbol}`\n"
        f"📋 `{address[:8]}...{address[-6:]}`\n\n"
        f"💰 *Price:* `{fmt_price(price)}`\n"
        f"📊 *Vol 24h:* `{fmt_number(vol24)}`\n"
        f"🔥 *Vol Ratio:* `{vol_ratio:.1f}x` spike\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 *@underdeathsol*"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💸 BUY NOW", url=f"https://bags.fm/t/{address}?ref=LILFID12"),
            InlineKeyboardButton("📊 Chart", url=f"https://bags.fm/t/{address}"),
        ],
    ])
    return text, keyboard

def build_leaderboard_message(tokens: list) -> str:
    if not tokens:
        return "📊 *Leaderboard* — No data available right now."

    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 10
    now    = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines  = [f"🏆 *BAGS.FM TOP GAINERS* (24h)\n🕐 `{now}`\n━━━━━━━━━━━━━━━━━━━━━"]

    for i, token in enumerate(tokens[:10]):
        address = token.get("address", token.get("mint", ""))
        name    = token.get("name", "Unknown")[:15]
        symbol  = token.get("symbol", "???")
        ch24    = safe_get(token, "priceChange", "h24", default=safe_get(token, "priceChange24h", default=0))
        price   = safe_get(token, "priceUsd", default=0)
        vol     = safe_get(token, "volume", "h24", default=0)

        lines.append(
            f"{medals[i]} *{name}* `${symbol}`\n"
            f"   Price: `{fmt_price(price)}` | {fmt_change(ch24)}\n"
            f"   Vol: `{fmt_number(vol)}`"
        )

    lines.append(f"━━━━━━━━━━━━━━━━━━━━━\n🔗 [Trade on Bags.fm]({REF_LINK})\n📱 *@underdeathsol*")
    return "\n".join(lines)

# ── Telegram Sender ───────────────────────────────────────────────────────────
async def send_to_channel(bot: Bot, text: str, keyboard=None, retries=3):
    for attempt in range(retries):
        try:
            kwargs = dict(
                chat_id=CHANNEL_ID,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            if THREAD_ID:
                kwargs["message_thread_id"] = THREAD_ID
            if keyboard:
                kwargs["reply_markup"] = keyboard
            msg = await bot.send_message(**kwargs)
            stats["alerts_sent"] += 1
            return msg
        except RetryAfter as e:
            wait = e.retry_after + 1
            log.warning(f"Rate limited, waiting {wait}s")
            await asyncio.sleep(wait)
        except TelegramError as e:
            log.error(f"Telegram error (attempt {attempt+1}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
    return None

# ── Scanner Loop ──────────────────────────────────────────────────────────────
async def scanner_loop(bot: Bot):
    global paused
    log.info("🔍 Scanner loop started")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if paused:
                    await asyncio.sleep(SCAN_INTERVAL)
                    continue

                tokens = await fetch_new_tokens(session)
                stats["tokens_scanned"] += len(tokens)

                for token in tokens:
                    address = token.get("address", token.get("mint", token.get("id", "")))
                    if not address or address in seen_tokens:
                        continue

                    liq   = float(safe_get(token, "liquidity", "usd", default=safe_get(token, "liquidityUsd", default=0)))
                    vol24 = float(safe_get(token, "volume", "h24", default=safe_get(token, "volume24h", default=0)))

                    # Filter
                    if liq < MIN_LIQUIDITY or vol24 < MIN_VOLUME:
                        seen_tokens.add(address)
                        continue

                    seen_tokens.add(address)
                    tracked_tokens[address] = {
                        "name": token.get("name", "Unknown"),
                        "symbol": token.get("symbol", "???"),
                        "price": float(safe_get(token, "priceUsd", default=0)),
                        "pump_alerted": False,
                        "vol_alerted": False,
                        "last_vol": vol24,
                        "data": token,
                    }

                    text, keyboard = build_new_token_message(token)
                    await send_to_channel(bot, text, keyboard)
                    log.info(f"✅ New token alerted: {token.get('symbol')} {address[:8]}")
                    await asyncio.sleep(0.5)  # small delay between sends

                # Check price pumps & volume spikes on tracked tokens
                await check_tracked_alerts(session, bot)

            except Exception as e:
                log.error(f"Scanner error: {e}\n{traceback.format_exc()}")

            await asyncio.sleep(SCAN_INTERVAL)

async def check_tracked_alerts(session: aiohttp.ClientSession, bot: Bot):
    """Check tracked tokens for pump/volume alerts"""
    addresses = list(tracked_tokens.keys())
    for address in addresses:
        try:
            info = tracked_tokens[address]
            detail = await fetch_token_detail(session, address)
            if not detail:
                continue

            cur_price = float(safe_get(detail, "priceUsd", default=0))
            old_price = info.get("price", cur_price)
            cur_vol   = float(safe_get(detail, "volume", "h24", default=0))
            old_vol   = info.get("last_vol", cur_vol) or cur_vol

            if old_price > 0 and not info["pump_alerted"]:
                change_pct = ((cur_price - old_price) / old_price) * 100
                if change_pct >= 50:
                    text, kb = build_pump_alert_message(detail, change_pct)
                    await send_to_channel(bot, text, kb)
                    tracked_tokens[address]["pump_alerted"] = True
                    log.info(f"🚀 Pump alert: {info['symbol']} +{change_pct:.1f}%")

            if old_vol > 0 and not info["vol_alerted"]:
                vol_ratio = cur_vol / old_vol
                if vol_ratio >= 3:
                    text, kb = build_volume_spike_message(detail, vol_ratio)
                    await send_to_channel(bot, text, kb)
                    tracked_tokens[address]["vol_alerted"] = True
                    log.info(f"📊 Volume spike: {info['symbol']} {vol_ratio:.1f}x")

            tracked_tokens[address]["price"]    = cur_price
            tracked_tokens[address]["last_vol"] = cur_vol

            await asyncio.sleep(0.3)
        except Exception as e:
            log.error(f"Alert check error {address}: {e}")

# ── Leaderboard Loop ──────────────────────────────────────────────────────────
async def leaderboard_loop(bot: Bot):
    log.info("🏆 Leaderboard loop started")
    await asyncio.sleep(30)  # initial delay
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                tokens = await fetch_leaderboard(session)
                if not tokens:
                    # fallback: use tracked tokens
                    tokens = [v["data"] for v in list(tracked_tokens.values())[:10]]
                text = build_leaderboard_message(tokens)
                await send_to_channel(bot, text)
                log.info("🏆 Leaderboard sent")
            except Exception as e:
                log.error(f"Leaderboard error: {e}")
            await asyncio.sleep(LEADERBOARD_INTERVAL)

# ── Watchlist Price Alert Loop ────────────────────────────────────────────────
async def watchlist_loop(bot: Bot):
    log.info("⭐ Watchlist loop started")
    async with aiohttp.ClientSession() as session:
        while True:
            for address, info in list(watchlist.items()):
                try:
                    price = await fetch_token_price(session, address)
                    target = info.get("target_price", 0)
                    if target and float(price) >= float(target):
                        name   = info.get("name", "Unknown")
                        symbol = info.get("symbol", "???")
                        text = (
                            f"🎯 *WATCHLIST TARGET HIT!*\n"
                            f"━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🏷 *{name}* `${symbol}`\n"
                            f"💰 Price: `{fmt_price(price)}`\n"
                            f"🎯 Target: `{fmt_price(target)}`\n"
                            f"✅ Target price reached!\n"
                            f"━━━━━━━━━━━━━━━━━━━━━\n"
                            f"[Trade Now]({REF_LINK})"
                        )
                        await send_to_channel(bot, text)
                        del watchlist[address]  # remove after alert
                    await asyncio.sleep(0.3)
                except:
                    pass
            await asyncio.sleep(60)

# ── Command Handlers ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 *Bags.fm Scanner Bot*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👋 Welcome! I scan Bags.fm for new & trending Solana tokens.\n\n"
        "📋 *Commands:*\n"
        "/start — Show this menu\n"
        "/trending — Top trending tokens\n"
        "/new — Latest new tokens\n"
        "/scan \\<address\\> — Scan specific token\n"
        "/watchlist — Show your watchlist\n"
        "/leaderboard — Top gainers\n"
        "/stats — Bot statistics\n"
        "/pause — Pause scanner\n"
        "/resume — Resume scanner\n\n"
        f"🔗 Trade on [Bags.fm]({REF_LINK})\n"
        f"📱 Channel: @underdeathsol"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Trending", callback_data="trending"),
            InlineKeyboardButton("🆕 New Tokens", callback_data="new"),
        ],
        [
            InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard"),
            InlineKeyboardButton("📈 Stats", callback_data="stats"),
        ],
        [InlineKeyboardButton("💸 Trade on Bags.fm", url=REF_LINK)],
    ])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def cmd_trending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Fetching trending tokens...")
    async with aiohttp.ClientSession() as session:
        tokens = await fetch_trending_tokens(session)
    if not tokens:
        await msg.edit_text("❌ Could not fetch trending tokens. Try again later.")
        return

    lines = ["🔥 *TRENDING TOKENS ON BAGS.FM*\n━━━━━━━━━━━━━━━━━━━━━"]
    for i, t in enumerate(tokens[:10], 1):
        address = t.get("address", t.get("mint", ""))
        name    = t.get("name", "Unknown")[:18]
        symbol  = t.get("symbol", "???")
        price   = safe_get(t, "priceUsd", default=0)
        ch24    = safe_get(t, "priceChange", "h24", default=0)
        lines.append(f"{i}. *{name}* `${symbol}`\n   `{fmt_price(price)}` | {fmt_change(ch24)}")

    lines.append(f"━━━━━━━━━━━━━━━━━━━━━\n[View All on Bags.fm]({REF_LINK})")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Fetching new tokens...")
    async with aiohttp.ClientSession() as session:
        tokens = await fetch_new_tokens(session)
    if not tokens:
        await msg.edit_text("❌ Could not fetch new tokens.")
        return

    lines = ["🆕 *NEWEST TOKENS ON BAGS.FM*\n━━━━━━━━━━━━━━━━━━━━━"]
    for t in tokens[:8]:
        address = t.get("address", t.get("mint", ""))
        name    = t.get("name", "Unknown")[:18]
        symbol  = t.get("symbol", "???")
        price   = safe_get(t, "priceUsd", default=0)
        liq     = safe_get(t, "liquidity", "usd", default=0)
        created = t.get("createdAt", t.get("created_at", ""))
        lines.append(
            f"• *{name}* `${symbol}`\n"
            f"  Price: `{fmt_price(price)}` | Liq: `{fmt_number(liq)}`\n"
            f"  Listed: `{age_str(created)}`\n"
            f"  [Buy]({REF_LINK})"
        )
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("⚠️ Usage: /scan \\<token\\_address\\>", parse_mode=ParseMode.MARKDOWN)
        return
    address = ctx.args[0].strip()
    msg = await update.message.reply_text(f"🔍 Scanning `{address[:8]}...`", parse_mode=ParseMode.MARKDOWN)
    async with aiohttp.ClientSession() as session:
        token = await fetch_token_detail(session, address)
    if not token:
        await msg.edit_text("❌ Token not found on Bags.fm.")
        return
    text, keyboard = build_new_token_message(token)
    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard, disable_web_page_preview=True)

async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🏆 Building leaderboard...")
    async with aiohttp.ClientSession() as session:
        tokens = await fetch_leaderboard(session)
    text = build_leaderboard_message(tokens)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="leaderboard")]])
    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard, disable_web_page_preview=True)

async def cmd_watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not watchlist:
        await update.message.reply_text("⭐ Your watchlist is empty.\n\nUse the ⭐ Watchlist button on any token alert to add it.")
        return
    lines = ["⭐ *WATCHLIST*\n━━━━━━━━━━━━━━━━━━━━━"]
    for addr, info in watchlist.items():
        name   = info.get("name", "Unknown")
        symbol = info.get("symbol", "???")
        target = info.get("target_price", 0)
        lines.append(f"• *{name}* `${symbol}`\n  Target: `{fmt_price(target)}`\n  [View](https://bags.fm/t/{addr})")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uptime_s = int(time.time() - stats["start_time"])
    h, rem   = divmod(uptime_s, 3600)
    m, s     = divmod(rem, 60)
    text = (
        f"📈 *BOT STATISTICS*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Uptime: `{h}h {m}m {s}s`\n"
        f"🔍 Tokens Scanned: `{stats['tokens_scanned']}`\n"
        f"📢 Alerts Sent: `{stats['alerts_sent']}`\n"
        f"🎯 Tracked Tokens: `{len(tracked_tokens)}`\n"
        f"⭐ Watchlist: `{len(watchlist)}`\n"
        f"⏸ Status: `{'PAUSED' if paused else 'RUNNING'}`\n"
        f"📋 Min Liquidity: `{fmt_number(MIN_LIQUIDITY)}`\n"
        f"📊 Min Volume: `{fmt_number(MIN_VOLUME)}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 @underdeathsol"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global paused
    paused = True
    await update.message.reply_text("⏸ Scanner *PAUSED*. Use /resume to restart.", parse_mode=ParseMode.MARKDOWN)

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global paused
    paused = False
    await update.message.reply_text("▶️ Scanner *RESUMED*.", parse_mode=ParseMode.MARKDOWN)

async def cmd_setfilter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global MIN_LIQUIDITY, MIN_VOLUME
    usage = "⚠️ Usage: /setfilter \\<min\\_liq\\> \\<min\\_vol\\>\nExample: `/setfilter 5000 10000`"
    if len(ctx.args) < 2:
        await update.message.reply_text(usage, parse_mode=ParseMode.MARKDOWN)
        return
    try:
        MIN_LIQUIDITY = float(ctx.args[0])
        MIN_VOLUME    = float(ctx.args[1])
        await update.message.reply_text(
            f"✅ Filters updated!\nMin Liquidity: `{fmt_number(MIN_LIQUIDITY)}`\nMin Volume: `{fmt_number(MIN_VOLUME)}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except:
        await update.message.reply_text(usage, parse_mode=ParseMode.MARKDOWN)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)

# ── Callback Query Handler ────────────────────────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "trending":
        await query.message.reply_text("🔍 Fetching trending tokens...")
        # Reuse cmd_trending logic
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_trending_tokens(session)
        if not tokens:
            await query.message.reply_text("❌ Could not fetch trending tokens.")
            return
        lines = ["🔥 *TRENDING TOKENS*\n━━━━━━━━━━━━━━━━━━━━━"]
        for i, t in enumerate(tokens[:10], 1):
            name   = t.get("name", "Unknown")[:18]
            symbol = t.get("symbol", "???")
            price  = safe_get(t, "priceUsd", default=0)
            ch24   = safe_get(t, "priceChange", "h24", default=0)
            lines.append(f"{i}. *{name}* `${symbol}` — `{fmt_price(price)}` | {fmt_change(ch24)}")
        await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

    elif data == "new":
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_new_tokens(session)
        if not tokens:
            await query.message.reply_text("❌ No new tokens found.")
            return
        lines = ["🆕 *NEWEST TOKENS*\n━━━━━━━━━━━━━━━━━━━━━"]
        for t in tokens[:5]:
            name   = t.get("name", "Unknown")[:18]
            symbol = t.get("symbol", "???")
            price  = safe_get(t, "priceUsd", default=0)
            lines.append(f"• *{name}* `${symbol}` — `{fmt_price(price)}`")
        await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    elif data == "leaderboard":
        async with aiohttp.ClientSession() as session:
            tokens = await fetch_leaderboard(session)
        text = build_leaderboard_message(tokens)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="leaderboard")]])
        await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard, disable_web_page_preview=True)

    elif data == "stats":
        uptime_s = int(time.time() - stats["start_time"])
        h, rem   = divmod(uptime_s, 3600)
        m, _     = divmod(rem, 60)
        text = (
            f"📈 *STATS*\n"
            f"Uptime: `{h}h {m}m`\n"
            f"Scanned: `{stats['tokens_scanned']}`\n"
            f"Alerts: `{stats['alerts_sent']}`\n"
            f"Tracked: `{len(tracked_tokens)}`"
        )
        await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    elif data.startswith("copy_"):
        address = data[5:]
        await query.answer(f"CA: {address}", show_alert=True)

    elif data.startswith("watch_"):
        parts   = data.split("_", 3)
        address = parts[1] if len(parts) > 1 else ""
        symbol  = parts[2] if len(parts) > 2 else "???"
        name    = parts[3] if len(parts) > 3 else "Unknown"
        if address:
            watchlist[address] = {"name": name, "symbol": symbol, "target_price": 0}
            await query.answer(f"⭐ {symbol} added to watchlist!", show_alert=True)
        else:
            await query.answer("❌ Could not add to watchlist.", show_alert=True)

# ── Main ──────────────────────────────────────────────────────────────────────
async def post_init(application: Application):
    """Send startup message"""
    bot = application.bot
    try:
        await send_to_channel(bot,
            "🤖 *Bags.fm Scanner Bot ONLINE*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Scanning new tokens every `{SCAN_INTERVAL}s`\n"
            f"💧 Min Liquidity: `{fmt_number(MIN_LIQUIDITY)}`\n"
            f"📊 Min Volume: `{fmt_number(MIN_VOLUME)}`\n"
            f"🏆 Leaderboard every `{LEADERBOARD_INTERVAL//60}min`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 @underdeathsol | 🐦 @lilfid12"
        )
        log.info("✅ Startup message sent")
    except Exception as e:
        log.error(f"Startup message failed: {e}")

    # Start background tasks
    asyncio.create_task(scanner_loop(bot))
    asyncio.create_task(leaderboard_loop(bot))
    asyncio.create_task(watchlist_loop(bot))

def main():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN not set in .env!")
        return
    if not BAGS_API_KEY:
        log.warning("BAGS_API_KEY not set – API calls may fail")

    log.info("🚀 Starting Bags.fm Scanner Bot...")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Register handlers
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("trending",   cmd_trending))
    app.add_handler(CommandHandler("new",        cmd_new))
    app.add_handler(CommandHandler("scan",       cmd_scan))
    app.add_handler(CommandHandler("leaderboard",cmd_leaderboard))
    app.add_handler(CommandHandler("watchlist",  cmd_watchlist))
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(CommandHandler("pause",      cmd_pause))
    app.add_handler(CommandHandler("resume",     cmd_resume))
    app.add_handler(CommandHandler("setfilter",  cmd_setfilter))
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info("✅ Bot started. Polling...")
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)

if __name__ == "__main__":
    main()
