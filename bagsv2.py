#!/usr/bin/env python3
"""
╔══════════════════════════════════════════╗
║   🎯 BAGS.FM SCANNER BOT v2.0           ║
║   @underdeathsol | Twitter: @lilfid12   ║
╚══════════════════════════════════════════╝
"""

import os, asyncio, aiohttp, logging, time, traceback, json, random
from datetime import datetime, timezone
from dotenv import load_dotenv
from telegram import (
    Bot, InlineKeyboardButton, InlineKeyboardMarkup,
    Update, WebAppInfo
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, RetryAfter

# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN           = os.getenv("BOT_TOKEN", "")
CHANNEL_ID          = os.getenv("CHANNEL_ID", "")
THREAD_ID           = int(os.getenv("THREAD_ID", "0")) or None
REF_LINK            = os.getenv("REF_LINK", "https://bags.fm/$LILFID12")
BAGS_API_KEY        = os.getenv("BAGS_API_KEY", "")
BAGS_API_BASE       = os.getenv("BAGS_API_BASE", "https://api.bags.fm")
MIN_LIQUIDITY       = float(os.getenv("MIN_LIQUIDITY", "1000"))
MIN_VOLUME          = float(os.getenv("MIN_VOLUME", "2000"))
SCAN_INTERVAL       = int(os.getenv("SCAN_INTERVAL", "30"))
LEADERBOARD_INTERVAL= int(os.getenv("LEADERBOARD_INTERVAL", "3600"))
WEBAPP_URL          = os.getenv("WEBAPP_URL", "https://bags.fm")

# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bags_bot.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
seen_tokens: set     = set()
tracked_tokens: dict = {}
watchlist: dict      = {}
paused: bool         = False
stats = {"alerts": 0, "scanned": 0, "pumps": 0, "volumes": 0, "start": time.time()}

# ══════════════════════════════════════════════════════════════════════════════
#  BAGS.FM  API  —  multiple endpoint fallbacks
# ══════════════════════════════════════════════════════════════════════════════
HEADERS = {
    "Authorization": f"Bearer {BAGS_API_KEY}",
    "x-api-key": BAGS_API_KEY,
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "BagsScanner/2.0",
}

async def _get(session: aiohttp.ClientSession, url: str, params=None):
    try:
        async with session.get(url, headers=HEADERS, params=params,
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 200:
                ct = r.headers.get("Content-Type", "")
                if "json" in ct:
                    return await r.json()
                return None
            log.warning(f"GET {url} → {r.status}")
    except Exception as e:
        log.debug(f"GET error {url}: {e}")
    return None

# ── Token fetch with multiple fallback sources ────────────────────────────────
async def fetch_new_tokens(session):
    """Try multiple Bags.fm / DexScreener endpoints for new tokens"""

    # 1️⃣  Bags.fm official endpoints
    endpoints = [
        f"{BAGS_API_BASE}/v1/tokens/new",
        f"{BAGS_API_BASE}/tokens/new",
        f"{BAGS_API_BASE}/v1/tokens/latest",
        f"{BAGS_API_BASE}/v1/coins/new",
        f"{BAGS_API_BASE}/api/tokens/new",
    ]
    for url in endpoints:
        data = await _get(session, url, {"limit": 50, "sort": "created_at", "order": "desc"})
        tokens = _extract_list(data, ["tokens", "data", "coins", "results", "items"])
        if tokens:
            log.info(f"✅ Got {len(tokens)} new tokens from {url}")
            return tokens

    # 2️⃣  DexScreener fallback — filter for Bags.fm tokens on Solana
    data = await _get(session, "https://api.dexscreener.com/latest/dex/search",
                      {"q": "bags", "chainIds": "solana"})
    if data:
        pairs = data.get("pairs", [])
        solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if solana_pairs:
            log.info(f"✅ Got {len(solana_pairs)} tokens via DexScreener fallback")
            return [_normalize_dex(p) for p in solana_pairs[:30]]

    # 3️⃣  DexScreener latest Solana tokens
    data = await _get(session, "https://api.dexscreener.com/token-profiles/latest/v1")
    if data:
        tokens = [t for t in (data if isinstance(data, list) else []) if t.get("chainId") == "solana"]
        if tokens:
            log.info(f"✅ Got {len(tokens)} tokens from DexScreener profiles")
            return [_normalize_profile(t) for t in tokens[:30]]

    log.warning("⚠️ All token sources returned empty")
    return []

async def fetch_trending_tokens(session):
    endpoints = [
        f"{BAGS_API_BASE}/v1/tokens/trending",
        f"{BAGS_API_BASE}/tokens/trending",
        f"{BAGS_API_BASE}/v1/coins/trending",
    ]
    for url in endpoints:
        data = await _get(session, url, {"limit": 20})
        tokens = _extract_list(data, ["tokens", "data", "coins"])
        if tokens:
            return tokens

    # DexScreener boosted
    data = await _get(session, "https://api.dexscreener.com/token-boosts/top/v1")
    if data:
        items = data if isinstance(data, list) else data.get("data", [])
        sol = [i for i in items if i.get("chainId") == "solana"]
        return [_normalize_profile(i) for i in sol[:15]]
    return []

async def fetch_gainers(session):
    endpoints = [
        f"{BAGS_API_BASE}/v1/tokens/gainers",
        f"{BAGS_API_BASE}/tokens/gainers",
        f"{BAGS_API_BASE}/v1/tokens/top",
    ]
    for url in endpoints:
        data = await _get(session, url, {"limit": 10, "period": "24h"})
        tokens = _extract_list(data, ["tokens", "data", "gainers"])
        if tokens:
            return tokens

    # DexScreener search sorted by priceChange
    data = await _get(session, "https://api.dexscreener.com/latest/dex/search",
                      {"q": "solana", "chainIds": "solana"})
    if data:
        pairs = sorted(data.get("pairs", []),
                       key=lambda p: float(p.get("priceChange", {}).get("h24", 0) or 0),
                       reverse=True)
        return [_normalize_dex(p) for p in pairs[:10]]
    return []

async def fetch_token_detail(session, address):
    endpoints = [
        f"{BAGS_API_BASE}/v1/tokens/{address}",
        f"{BAGS_API_BASE}/tokens/{address}",
    ]
    for url in endpoints:
        data = await _get(session, url)
        if data:
            token = data.get("token", data)
            if isinstance(token, dict) and token.get("address"):
                return token

    # DexScreener fallback
    data = await _get(session, f"https://api.dexscreener.com/latest/dex/tokens/{address}")
    if data and data.get("pairs"):
        return _normalize_dex(data["pairs"][0])
    return None

def _extract_list(data, keys):
    if isinstance(data, list) and data:
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list) and v:
                return v
    return []

def _normalize_dex(pair: dict) -> dict:
    """Convert DexScreener pair format → unified token format"""
    base = pair.get("baseToken", {})
    info = pair.get("info", {})
    return {
        "address": base.get("address", ""),
        "name":    base.get("name", "Unknown"),
        "symbol":  base.get("symbol", "???"),
        "priceUsd": pair.get("priceUsd", 0),
        "liquidity": {"usd": pair.get("liquidity", {}).get("usd", 0)},
        "volume":    {"h24": pair.get("volume", {}).get("h24", 0)},
        "priceChange": {
            "h1":  pair.get("priceChange", {}).get("h1", 0),
            "h6":  pair.get("priceChange", {}).get("h6", 0),
            "h24": pair.get("priceChange", {}).get("h24", 0),
        },
        "marketCap":  pair.get("fdv", 0),
        "createdAt":  pair.get("pairCreatedAt", ""),
        "holders":    info.get("holders", "N/A"),
        "_source":    "dexscreener",
    }

def _normalize_profile(item: dict) -> dict:
    return {
        "address": item.get("tokenAddress", item.get("address", "")),
        "name":    item.get("name", item.get("description", "Unknown")),
        "symbol":  item.get("symbol", "???"),
        "priceUsd": item.get("priceUsd", 0),
        "liquidity": {"usd": item.get("liquidity", 0)},
        "volume":    {"h24": item.get("volume24h", 0)},
        "priceChange": {"h1": 0, "h6": 0, "h24": item.get("priceChange24h", 0)},
        "marketCap": item.get("marketCap", 0),
        "createdAt": item.get("createdAt", ""),
        "holders":   item.get("holders", "N/A"),
        "_source":   "dexscreener_profile",
    }

# ══════════════════════════════════════════════════════════════════════════════
#  FORMATTERS
# ══════════════════════════════════════════════════════════════════════════════
def fmt_usd(n):
    n = float(n or 0)
    if n >= 1_000_000: return f"${n/1_000_000:.2f}M"
    if n >= 1_000:     return f"${n/1_000:.1f}K"
    return f"${n:.2f}"

def fmt_price(p):
    p = float(p or 0)
    if p == 0:          return "$0.00"
    if p < 0.000001:    return f"${p:.10f}"
    if p < 0.001:       return f"${p:.8f}"
    if p < 1:           return f"${p:.6f}"
    return f"${p:.4f}"

def fmt_change(pct):
    pct = float(pct or 0)
    if pct >= 0: return f"🟢 +{pct:.2f}%"
    return f"🔴 {pct:.2f}%"

def age_str(created_at):
    try:
        if isinstance(created_at, (int, float)):
            ts = created_at / 1000 if created_at > 1e10 else created_at
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        elif isinstance(created_at, str) and created_at:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        else:
            return "just now"
        delta = datetime.now(timezone.utc) - dt
        m = int(delta.total_seconds() / 60)
        if m < 1:  return "just now"
        if m < 60: return f"{m}m ago"
        h = m // 60
        if h < 24: return f"{h}h ago"
        return f"{h//24}d ago"
    except:
        return "just now"

def sg(d, *keys, default=0):
    for k in keys:
        if isinstance(d, dict): d = d.get(k, default)
        else: return default
    return d if d is not None else default

def risk_badge(liq, vol, age_raw):
    liq = float(liq or 0)
    vol = float(vol or 0)
    if liq < 5000 or vol < 3000: return "🔴 HIGH RISK"
    if liq < 20000:               return "🟡 MED RISK"
    return "🟢 LOW RISK"

# ══════════════════════════════════════════════════════════════════════════════
#  MESSAGE BUILDERS
# ══════════════════════════════════════════════════════════════════════════════
def build_new_token_msg(token: dict):
    addr    = token.get("address", "")
    name    = token.get("name", "Unknown")[:20]
    sym     = token.get("symbol", "???")
    price   = sg(token, "priceUsd", default=0)
    liq     = sg(token, "liquidity", "usd", default=sg(token, "liquidityUsd", default=0))
    vol24   = sg(token, "volume", "h24",    default=sg(token, "volume24h",    default=0))
    mcap    = sg(token, "marketCap",        default=sg(token, "fdv",          default=0))
    ch1h    = sg(token, "priceChange", "h1",  default=0)
    ch6h    = sg(token, "priceChange", "h6",  default=0)
    ch24h   = sg(token, "priceChange", "h24", default=0)
    created = token.get("createdAt", token.get("created_at", token.get("pairCreatedAt", "")))
    holders = token.get("holders", token.get("holderCount", "—"))
    source  = token.get("_source", "bags.fm")
    risk    = risk_badge(liq, vol24, created)
    short   = f"{addr[:6]}...{addr[-4:]}" if len(addr) > 10 else addr

    buy_url = f"https://bags.fm/t/{addr}?ref=LILFID12"

    text = (
        "╔═══════════════════════════╗\n"
        "║  🚨  NEW TOKEN DETECTED   ║\n"
        "╚═══════════════════════════╝\n\n"
        f"🏷  *{name}*  •  `${sym}`\n"
        f"📋  `{short}`\n"
        f"⚠️  {risk}\n\n"
        "─────────── 💰 PRICE ───────────\n"
        f"💵  *Price:*  `{fmt_price(price)}`\n"
        f"💎  *MCap:*   `{fmt_usd(mcap)}`\n"
        f"💧  *Liq:*    `{fmt_usd(liq)}`\n"
        f"📊  *Vol 24h:* `{fmt_usd(vol24)}`\n"
        f"👥  *Holders:* `{holders}`\n\n"
        "─────────── 📈 CHANGE ──────────\n"
        f"  1H   {fmt_change(ch1h)}\n"
        f"  6H   {fmt_change(ch6h)}\n"
        f"  24H  {fmt_change(ch24h)}\n\n"
        f"🕐  *Listed:* `{age_str(created)}`\n"
        f"🔎  *Source:* `{source}`\n\n"
        "────────────────────────────────\n"
        f"📱  *[@underdeathsol](https://t.me/underdeathsol)* · 🐦 *[@lilfid12](https://x.com/lilfid12)*"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💸 BUY on Bags.fm", url=buy_url),
        ],
        [
            InlineKeyboardButton("📊 Chart", url=f"https://bags.fm/t/{addr}"),
            InlineKeyboardButton("🦅 DexScreener", url=f"https://dexscreener.com/solana/{addr}"),
        ],
        [
            InlineKeyboardButton("🔍 Birdeye", url=f"https://birdeye.so/token/{addr}?chain=solana"),
            InlineKeyboardButton("🛡 RugCheck", url=f"https://rugcheck.xyz/tokens/{addr}"),
        ],
        [
            InlineKeyboardButton("📋 Copy CA", callback_data=f"ca_{addr}"),
            InlineKeyboardButton("⭐ Watchlist", callback_data=f"wl_{addr[:20]}_{sym[:8]}_{name[:12]}"),
        ],
    ])
    return text, kb

def build_pump_msg(token: dict, pct: float):
    addr  = token.get("address", "")
    name  = token.get("name", "Unknown")[:20]
    sym   = token.get("symbol", "???")
    price = sg(token, "priceUsd", default=0)
    liq   = sg(token, "liquidity", "usd", default=0)
    vol   = sg(token, "volume", "h24", default=0)
    emoji = "🚀🚀🚀" if pct >= 100 else ("🚀🚀" if pct >= 50 else "⚡")
    buy_url = f"https://bags.fm/t/{addr}?ref=LILFID12"

    text = (
        f"╔═══════════════════════════╗\n"
        f"║  {emoji}  PUMP ALERT!         ║\n"
        f"╚═══════════════════════════╝\n\n"
        f"🏷  *{name}*  •  `${sym}`\n"
        f"📋  `{addr[:6]}...{addr[-4:]}`\n\n"
        f"💵  *Price:*  `{fmt_price(price)}`\n"
        f"📈  *Gain:*   `+{pct:.1f}%`  {fmt_change(pct)}\n"
        f"💧  *Liq:*    `{fmt_usd(liq)}`\n"
        f"📊  *Vol:*    `{fmt_usd(vol)}`\n\n"
        f"⚡  *Don't miss it — buy now!*\n\n"
        f"📱  *@underdeathsol* · 🐦 *@lilfid12*"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 BUY NOW 🚀", url=buy_url)],
        [
            InlineKeyboardButton("📊 Chart", url=f"https://bags.fm/t/{addr}"),
            InlineKeyboardButton("🦅 DexScreener", url=f"https://dexscreener.com/solana/{addr}"),
        ],
    ])
    return text, kb

def build_volume_msg(token: dict, ratio: float):
    addr  = token.get("address", "")
    name  = token.get("name", "Unknown")[:20]
    sym   = token.get("symbol", "???")
    price = sg(token, "priceUsd", default=0)
    vol   = sg(token, "volume", "h24", default=0)
    buy_url = f"https://bags.fm/t/{addr}?ref=LILFID12"

    text = (
        f"╔═══════════════════════════╗\n"
        f"║  📊  VOLUME SPIKE  🔥     ║\n"
        f"╚═══════════════════════════╝\n\n"
        f"🏷  *{name}*  •  `${sym}`\n\n"
        f"💵  *Price:*  `{fmt_price(price)}`\n"
        f"📊  *Vol 24h:* `{fmt_usd(vol)}`\n"
        f"🔥  *Spike:*  `{ratio:.1f}x` above avg\n\n"
        f"👁  *Unusual activity detected!*\n\n"
        f"📱  *@underdeathsol*"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 BUY on Bags.fm", url=buy_url)],
        [InlineKeyboardButton("📊 View Chart", url=f"https://bags.fm/t/{addr}")],
    ])
    return text, kb

def build_leaderboard_msg(tokens: list) -> str:
    if not tokens:
        return (
            "╔═══════════════════════════╗\n"
            "║  🏆  TOP GAINERS (24H)    ║\n"
            "╚═══════════════════════════╝\n\n"
            "📭  No data available at the moment.\n"
            "Please check back later!\n\n"
            f"🔗  [Trade on Bags.fm]({REF_LINK})"
        )

    medals  = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    now     = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines   = [
        "╔═══════════════════════════╗\n"
        "║  🏆  TOP GAINERS (24H)    ║\n"
        f"╚════════  🕐 {now}  ════════╝\n"
    ]
    for i, t in enumerate(tokens[:10]):
        addr  = t.get("address", t.get("mint", ""))
        name  = t.get("name", "Unknown")[:14]
        sym   = t.get("symbol", "???")
        ch24  = sg(t, "priceChange", "h24", default=sg(t, "priceChange24h", default=0))
        price = sg(t, "priceUsd", default=0)
        vol   = sg(t, "volume", "h24", default=0)
        lines.append(
            f"{medals[i]} *{name}* `${sym}`\n"
            f"   💵 `{fmt_price(price)}`  {fmt_change(ch24)}\n"
            f"   📊 Vol: `{fmt_usd(vol)}`\n"
            f"   [Buy 💸](https://bags.fm/t/{addr}?ref=LILFID12)\n"
        )
    lines.append(
        "────────────────────────────────\n"
        f"🔗  [Trade on Bags.fm]({REF_LINK})\n"
        f"📱  *@underdeathsol* · 🐦 *@lilfid12*"
    )
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM SENDER
# ══════════════════════════════════════════════════════════════════════════════
async def send_ch(bot: Bot, text: str, kb=None, retries=4):
    for attempt in range(retries):
        try:
            kw = dict(
                chat_id=CHANNEL_ID, text=text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            if THREAD_ID: kw["message_thread_id"] = THREAD_ID
            if kb:        kw["reply_markup"] = kb
            msg = await bot.send_message(**kw)
            stats["alerts"] += 1
            return msg
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramError as e:
            log.error(f"TG error attempt {attempt+1}: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
    return None

# ══════════════════════════════════════════════════════════════════════════════
#  BACKGROUND LOOPS
# ══════════════════════════════════════════════════════════════════════════════
async def scanner_loop(bot: Bot):
    log.info("🔍 Scanner loop started")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if not paused:
                    tokens = await fetch_new_tokens(session)
                    stats["scanned"] += len(tokens)

                    for token in tokens:
                        addr  = token.get("address", "")
                        if not addr or addr in seen_tokens:
                            continue
                        liq  = float(sg(token,"liquidity","usd",default=sg(token,"liquidityUsd",default=0)))
                        vol  = float(sg(token,"volume","h24",default=sg(token,"volume24h",default=0)))
                        if liq < MIN_LIQUIDITY or vol < MIN_VOLUME:
                            seen_tokens.add(addr)
                            continue
                        seen_tokens.add(addr)
                        tracked_tokens[addr] = {
                            "name":   token.get("name","?"),
                            "symbol": token.get("symbol","?"),
                            "price":  float(sg(token,"priceUsd",default=0)),
                            "vol":    vol,
                            "pumped": False,
                            "vol_alerted": False,
                            "data":   token,
                        }
                        text, kb = build_new_token_msg(token)
                        await send_ch(bot, text, kb)
                        log.info(f"🆕 Alert: {token.get('symbol')} {addr[:8]}")
                        await asyncio.sleep(0.5)

                    await _check_tracked(session, bot)
            except Exception as e:
                log.error(f"Scanner error: {e}\n{traceback.format_exc()}")
            await asyncio.sleep(SCAN_INTERVAL)

async def _check_tracked(session, bot):
    for addr, info in list(tracked_tokens.items()):
        try:
            detail = await fetch_token_detail(session, addr)
            if not detail:
                continue
            cur_p = float(sg(detail,"priceUsd",default=0))
            old_p = info.get("price", cur_p)
            cur_v = float(sg(detail,"volume","h24",default=0))
            old_v = info.get("vol", cur_v) or cur_v

            if old_p > 0 and not info["pumped"]:
                pct = ((cur_p - old_p) / old_p) * 100
                if pct >= 30:
                    t, kb = build_pump_msg(detail, pct)
                    await send_ch(bot, t, kb)
                    tracked_tokens[addr]["pumped"] = True
                    stats["pumps"] += 1
                    log.info(f"🚀 Pump: {info['symbol']} +{pct:.1f}%")

            if old_v > 0 and not info["vol_alerted"]:
                ratio = cur_v / old_v
                if ratio >= 3:
                    t, kb = build_volume_msg(detail, ratio)
                    await send_ch(bot, t, kb)
                    tracked_tokens[addr]["vol_alerted"] = True
                    stats["volumes"] += 1

            tracked_tokens[addr]["price"] = cur_p
            tracked_tokens[addr]["vol"]   = cur_v
            await asyncio.sleep(0.3)
        except:
            pass

async def leaderboard_loop(bot: Bot):
    log.info("🏆 Leaderboard loop started")
    await asyncio.sleep(60)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                tokens = await fetch_gainers(session)
                text   = build_leaderboard_msg(tokens)
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh", callback_data="lb_refresh")],
                    [InlineKeyboardButton("💸 Trade on Bags.fm", url=REF_LINK)],
                ])
                await send_ch(bot, text, kb)
                log.info("🏆 Leaderboard sent")
            except Exception as e:
                log.error(f"Leaderboard error: {e}")
            await asyncio.sleep(LEADERBOARD_INTERVAL)

async def watchlist_loop(bot: Bot):
    async with aiohttp.ClientSession() as session:
        while True:
            for addr, info in list(watchlist.items()):
                try:
                    detail = await fetch_token_detail(session, addr)
                    if not detail:
                        continue
                    price  = float(sg(detail,"priceUsd",default=0))
                    target = info.get("target_price", 0)
                    if target and price >= float(target):
                        name = info.get("name","?"); sym = info.get("symbol","?")
                        text = (
                            "╔═══════════════════════════╗\n"
                            "║  🎯  WATCHLIST TARGET HIT ║\n"
                            "╚═══════════════════════════╝\n\n"
                            f"🏷  *{name}* `${sym}`\n"
                            f"💵  Price: `{fmt_price(price)}`\n"
                            f"🎯  Target: `{fmt_price(target)}`\n\n"
                            f"✅  Target reached! Time to act!\n\n"
                            f"[💸 Buy Now](https://bags.fm/t/{addr}?ref=LILFID12)"
                        )
                        await send_ch(bot, text)
                        watchlist.pop(addr, None)
                    await asyncio.sleep(0.3)
                except:
                    pass
            await asyncio.sleep(60)

# ══════════════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════════════════
HELP_TEXT = (
    "╔═══════════════════════════╗\n"
    "║  🤖  BAGS.FM SCANNER BOT  ║\n"
    "║     How to Use Guide      ║\n"
    "╚═══════════════════════════╝\n\n"

    "━━━ 📋 COMMANDS ━━━━━━━━━━━━━━━\n\n"

    "🔵  */start*\n"
    "    Open main menu with quick-access buttons\n\n"

    "🔵  */new*\n"
    "    Show the latest new tokens listed on Bags.fm\n\n"

    "🔵  */trending*\n"
    "    Show current trending tokens by volume\n\n"

    "🔵  */scan* `<token_address>`\n"
    "    Deep scan any token by its contract address\n"
    "    _Example: /scan 7xKX..._\n\n"

    "🔵  */leaderboard*\n"
    "    Top 10 gainers in the last 24 hours\n\n"

    "🔵  */watchlist*\n"
    "    View your personal watchlist\n"
    "    _(Add tokens using ⭐ button on alerts)_\n\n"

    "🔵  */setfilter* `<min_liq> <min_vol>`\n"
    "    Change token filter thresholds\n"
    "    _Example: /setfilter 5000 10000_\n\n"

    "🔵  */stats*\n"
    "    View bot performance statistics\n\n"

    "🔵  */pause* & */resume*\n"
    "    Pause or resume the token scanner\n\n"

    "🔵  */help*\n"
    "    Show this guide\n\n"

    "━━━ 🔔 AUTO ALERTS ━━━━━━━━━━━\n\n"

    "🆕  *New Token* — Detected when a new token\n"
    "    meets your liquidity & volume filters\n\n"

    "🚀  *Pump Alert* — Fires when a tracked token\n"
    "    gains +30% or more from detection price\n\n"

    "📊  *Volume Spike* — Fires when 24h volume\n"
    "    surges 3x above the baseline reading\n\n"

    "🏆  *Leaderboard* — Sent automatically every\n"
    "    hour showing top 10 gainers\n\n"

    "🎯  *Watchlist Hit* — Notifies you when a\n"
    "    token reaches your set target price\n\n"

    "━━━ 📌 INLINE BUTTONS ━━━━━━━━\n\n"

    "💸  *BUY on Bags.fm* — Opens buy page with\n"
    "    referral link for best experience\n\n"

    "📋  *Copy CA* — Shows contract address in\n"
    "    a popup (tap to select & copy)\n\n"

    "⭐  *Watchlist* — Adds token to your personal\n"
    "    watchlist for price target alerts\n\n"

    "🦅  *DexScreener* — Live chart & trading data\n\n"

    "🛡  *RugCheck* — Token safety analysis\n\n"

    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    f"📱 [@underdeathsol](https://t.me/underdeathsol)\n"
    f"🐦 [@lilfid12](https://x.com/lilfid12)\n"
    f"🛒 [Trade on Bags.fm]({REF_LINK})"
)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "╔═══════════════════════════╗\n"
        "║  🤖  BAGS.FM SCANNER BOT  ║\n"
        "║   Real-Time Token Radar   ║\n"
        "╚═══════════════════════════╝\n\n"
        "👋  *Welcome!* I scan *Bags.fm* 24/7 and alert\n"
        "you the moment new Solana tokens are detected.\n\n"
        "📡  *What I do:*\n"
        "  • 🆕  Detect new tokens in real-time\n"
        "  • 🚀  Alert on pumps (+30% gain)\n"
        "  • 📊  Alert on volume spikes (3x)\n"
        "  • 🏆  Hourly top-gainers leaderboard\n"
        "  • ⭐  Personal watchlist with targets\n\n"
        "🛒  *Buy via referral = support the channel!*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 [@underdeathsol](https://t.me/underdeathsol)\n"
        f"🐦 [@lilfid12](https://x.com/lilfid12)"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆕 New Tokens",   callback_data="new"),
            InlineKeyboardButton("🔥 Trending",     callback_data="trending"),
        ],
        [
            InlineKeyboardButton("🏆 Leaderboard",  callback_data="lb"),
            InlineKeyboardButton("📈 Stats",         callback_data="stats"),
        ],
        [
            InlineKeyboardButton("📖 How to Use",   callback_data="help"),
            InlineKeyboardButton("⭐ Watchlist",     callback_data="wl_view"),
        ],
        [InlineKeyboardButton("💸 Trade on Bags.fm 🛒", url=REF_LINK)],
    ])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=kb, disable_web_page_preview=True)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Trade on Bags.fm", url=REF_LINK)],
        [InlineKeyboardButton("◀️ Back to Menu", callback_data="start")],
    ])
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=kb, disable_web_page_preview=True)

async def cmd_trending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Fetching trending tokens...")
    async with aiohttp.ClientSession() as session:
        tokens = await fetch_trending_tokens(session)
    if not tokens:
        await msg.edit_text("❌ No trending data available right now. Try again in a moment.")
        return
    lines = [
        "╔═══════════════════════════╗\n"
        "║  🔥  TRENDING ON BAGS.FM  ║\n"
        "╚═══════════════════════════╝\n"
    ]
    for i, t in enumerate(tokens[:10], 1):
        addr  = t.get("address","")
        name  = t.get("name","?")[:16]
        sym   = t.get("symbol","?")
        price = sg(t,"priceUsd",default=0)
        ch24  = sg(t,"priceChange","h24",default=0)
        vol   = sg(t,"volume","h24",default=0)
        lines.append(
            f"{i:02d}.  *{name}* `${sym}`\n"
            f"      💵 `{fmt_price(price)}`  {fmt_change(ch24)}\n"
            f"      📊 Vol: `{fmt_usd(vol)}`\n"
            f"      [💸 Buy](https://bags.fm/t/{addr}?ref=LILFID12)\n"
        )
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n[🛒 Trade on Bags.fm]({REF_LINK})")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="trending")],
        [InlineKeyboardButton("💸 Trade on Bags.fm", url=REF_LINK)],
    ])
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN,
                        reply_markup=kb, disable_web_page_preview=True)

async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Fetching new tokens...")
    async with aiohttp.ClientSession() as session:
        tokens = await fetch_new_tokens(session)
    if not tokens:
        await msg.edit_text("❌ No new tokens found right now.")
        return
    lines = [
        "╔═══════════════════════════╗\n"
        "║  🆕  NEWEST ON BAGS.FM    ║\n"
        "╚═══════════════════════════╝\n"
    ]
    for t in tokens[:8]:
        addr    = t.get("address","")
        name    = t.get("name","?")[:16]
        sym     = t.get("symbol","?")
        price   = sg(t,"priceUsd",default=0)
        liq     = sg(t,"liquidity","usd",default=0)
        created = t.get("createdAt",t.get("created_at",""))
        lines.append(
            f"🆕  *{name}* `${sym}`\n"
            f"    💵 `{fmt_price(price)}`  💧 `{fmt_usd(liq)}`\n"
            f"    🕐 `{age_str(created)}`\n"
            f"    [💸 Buy](https://bags.fm/t/{addr}?ref=LILFID12)\n"
        )
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n[🛒 Bags.fm]({REF_LINK})")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN,
                        disable_web_page_preview=True)

async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "⚠️  *Usage:* `/scan <token_address>`\n"
            "_Example: /scan 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgHkv_",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    addr = ctx.args[0].strip()
    msg  = await update.message.reply_text(f"🔍 Scanning `{addr[:8]}...{addr[-4:]}`...", parse_mode=ParseMode.MARKDOWN)
    async with aiohttp.ClientSession() as session:
        token = await fetch_token_detail(session, addr)
    if not token:
        await msg.edit_text(
            "❌ Token not found.\n\n"
            "Make sure the address is correct and the token is on Solana."
        )
        return
    text, kb = build_new_token_msg(token)
    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True)

async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🏆 Building leaderboard...")
    async with aiohttp.ClientSession() as session:
        tokens = await fetch_gainers(session)
    text = build_leaderboard_msg(tokens)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="lb")],
        [InlineKeyboardButton("💸 Trade on Bags.fm", url=REF_LINK)],
    ])
    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True)

async def cmd_watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not watchlist:
        await update.message.reply_text(
            "⭐  *Your Watchlist is Empty*\n\n"
            "Add tokens by tapping the ⭐ *Watchlist* button\n"
            "on any token alert in the channel.\n\n"
            "You'll get notified when a target price is hit!",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    lines = ["╔═══════════════════════════╗\n║  ⭐  YOUR WATCHLIST       ║\n╚═══════════════════════════╝\n"]
    for addr, info in watchlist.items():
        name   = info.get("name","?"); sym = info.get("symbol","?")
        target = info.get("target_price", 0)
        lines.append(
            f"🏷  *{name}* `${sym}`\n"
            f"    🎯 Target: `{fmt_price(target)}`\n"
            f"    [View](https://bags.fm/t/{addr})\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    up = int(time.time() - stats["start"])
    h, rem = divmod(up, 3600); m, s = divmod(rem, 60)
    text = (
        "╔═══════════════════════════╗\n"
        "║  📈  BOT STATISTICS       ║\n"
        "╚═══════════════════════════╝\n\n"
        f"⏱  *Uptime:*        `{h}h {m}m {s}s`\n"
        f"🔍  *Scanned:*      `{stats['scanned']} tokens`\n"
        f"📢  *Alerts Sent:*  `{stats['alerts']}`\n"
        f"🚀  *Pump Alerts:*  `{stats['pumps']}`\n"
        f"📊  *Vol Alerts:*   `{stats['volumes']}`\n"
        f"🎯  *Tracked:*      `{len(tracked_tokens)} tokens`\n"
        f"⭐  *Watchlist:*    `{len(watchlist)} tokens`\n\n"
        f"━━━ ⚙️ FILTERS ━━━━━━━━━━━━━\n\n"
        f"💧  Min Liquidity:  `{fmt_usd(MIN_LIQUIDITY)}`\n"
        f"📊  Min Volume:     `{fmt_usd(MIN_VOLUME)}`\n"
        f"⏰  Scan Interval:  `{SCAN_INTERVAL}s`\n"
        f"▶️   Status: `{'⏸ PAUSED' if paused else '✅ RUNNING'}`\n\n"
        f"📱  *@underdeathsol* · 🐦 *@lilfid12*"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global paused; paused = True
    await update.message.reply_text("⏸  Scanner *PAUSED*\n\nUse /resume to restart scanning.", parse_mode=ParseMode.MARKDOWN)

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global paused; paused = False
    await update.message.reply_text("▶️  Scanner *RESUMED*\n\nScanning for new tokens now!", parse_mode=ParseMode.MARKDOWN)

async def cmd_setfilter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global MIN_LIQUIDITY, MIN_VOLUME
    usage = (
        "⚙️  *Usage:* `/setfilter <min_liq> <min_vol>`\n\n"
        "_Examples:_\n"
        "`/setfilter 5000 10000` — stricter filter\n"
        "`/setfilter 500 1000`  — catch more tokens"
    )
    if len(ctx.args) < 2:
        await update.message.reply_text(usage, parse_mode=ParseMode.MARKDOWN)
        return
    try:
        MIN_LIQUIDITY = float(ctx.args[0])
        MIN_VOLUME    = float(ctx.args[1])
        await update.message.reply_text(
            f"✅  *Filters Updated!*\n\n"
            f"💧  Min Liquidity: `{fmt_usd(MIN_LIQUIDITY)}`\n"
            f"📊  Min Volume:    `{fmt_usd(MIN_VOLUME)}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except:
        await update.message.reply_text(usage, parse_mode=ParseMode.MARKDOWN)

async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "╔═══════════════════════════╗\n"
        "║  💸  BUY ON BAGS.FM       ║\n"
        "╚═══════════════════════════╝\n\n"
        "🛒  Trade tokens directly on *Bags.fm*\n"
        "    via our referral link!\n\n"
        "✅  *Benefits:*\n"
        "  • Supports the @underdeathsol channel\n"
        "  • Access all new & trending tokens\n"
        "  • Fast Solana-native swaps\n\n"
        f"🔗  [Open Bags.fm Trading]({REF_LINK})"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Open Bags.fm", url=REF_LINK)],
    ])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True)

# ══════════════════════════════════════════════════════════════════════════════
#  CALLBACK HANDLER
# ══════════════════════════════════════════════════════════════════════════════
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    async def reply(text, kb=None):
        try:
            await q.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=kb, disable_web_page_preview=True)
        except:
            pass

    if data == "start":
        await q.message.reply_text(
            "Use /start for the main menu.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Main Menu", callback_data="start_menu")
            ]])
        )

    elif data in ("new", "trending", "lb", "lb_refresh"):
        async with aiohttp.ClientSession() as s:
            if data == "new":
                tokens = await fetch_new_tokens(s)
                lines  = ["🆕 *LATEST NEW TOKENS*\n"]
                for t in tokens[:5]:
                    addr = t.get("address",""); sym = t.get("symbol","?"); name = t.get("name","?")[:14]
                    price = sg(t,"priceUsd",default=0)
                    lines.append(f"• *{name}* `${sym}` — `{fmt_price(price)}`\n  [💸 Buy](https://bags.fm/t/{addr}?ref=LILFID12)")
                if not lines[1:]: lines.append("No new tokens at the moment.")
                await reply("\n".join(lines))

            elif data == "trending":
                tokens = await fetch_trending_tokens(s)
                lines  = ["🔥 *TRENDING NOW*\n"]
                for i,t in enumerate(tokens[:5],1):
                    addr = t.get("address",""); sym = t.get("symbol","?"); name = t.get("name","?")[:14]
                    ch24 = sg(t,"priceChange","h24",default=0)
                    lines.append(f"{i}. *{name}* `${sym}` — {fmt_change(ch24)}\n  [💸 Buy](https://bags.fm/t/{addr}?ref=LILFID12)")
                if not lines[1:]: lines.append("No trending data at the moment.")
                await reply("\n".join(lines))

            else:  # leaderboard
                tokens = await fetch_gainers(s)
                text   = build_leaderboard_msg(tokens)
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh", callback_data="lb_refresh")],
                    [InlineKeyboardButton("💸 Trade", url=REF_LINK)],
                ])
                await reply(text, kb)

    elif data == "stats":
        up = int(time.time() - stats["start"])
        h, rem = divmod(up, 3600); m, _ = divmod(rem, 60)
        await reply(
            f"📈 *STATS*\n\n"
            f"⏱ Uptime: `{h}h {m}m`\n"
            f"🔍 Scanned: `{stats['scanned']}`\n"
            f"📢 Alerts: `{stats['alerts']}`\n"
            f"🎯 Tracked: `{len(tracked_tokens)}`"
        )

    elif data == "help":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Trade on Bags.fm", url=REF_LINK)],
        ])
        await reply(HELP_TEXT, kb)

    elif data == "wl_view":
        if not watchlist:
            await reply("⭐ Watchlist is empty. Tap ⭐ on any alert to add tokens!")
        else:
            lines = ["⭐ *YOUR WATCHLIST*\n"]
            for addr, info in watchlist.items():
                lines.append(f"• *{info['name']}* `${info['symbol']}`\n  Target: `{fmt_price(info.get('target_price',0))}`")
            await reply("\n".join(lines))

    elif data.startswith("ca_"):
        addr = data[3:]
        await q.answer(f"📋 CA: {addr}", show_alert=True)

    elif data.startswith("wl_"):
        parts = data.split("_", 3)
        addr  = parts[1] if len(parts)>1 else ""
        sym   = parts[2] if len(parts)>2 else "???"
        name  = parts[3] if len(parts)>3 else "Unknown"
        if addr:
            watchlist[addr] = {"name": name, "symbol": sym, "target_price": 0}
            await q.answer(f"⭐ {sym} added to watchlist!", show_alert=True)
        else:
            await q.answer("❌ Failed to add.", show_alert=True)

# ══════════════════════════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════════════════════════
async def post_init(app: Application):
    bot = app.bot
    startup_text = (
        "╔═══════════════════════════╗\n"
        "║  🤖  BAGS.FM SCANNER BOT  ║\n"
        "║       v2.0  ONLINE ✅     ║\n"
        "╚═══════════════════════════╝\n\n"
        f"📡  *Scanning every* `{SCAN_INTERVAL}s`\n"
        f"💧  *Min Liquidity:* `{fmt_usd(MIN_LIQUIDITY)}`\n"
        f"📊  *Min Volume:*    `{fmt_usd(MIN_VOLUME)}`\n"
        f"🏆  *Leaderboard:*  every `{LEADERBOARD_INTERVAL//60}min`\n"
        f"🚀  *Pump Alert:*   +30% from entry\n"
        f"📈  *Volume Spike:* 3x baseline\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 [@underdeathsol](https://t.me/underdeathsol) · 🐦 [@lilfid12](https://x.com/lilfid12)\n"
        f"🛒 [Trade on Bags.fm]({REF_LINK})"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Trade on Bags.fm", url=REF_LINK)],
    ])
    try:
        await send_ch(bot, startup_text, kb)
        log.info("✅ Startup message sent")
    except Exception as e:
        log.error(f"Startup message failed: {e}")

    asyncio.create_task(scanner_loop(bot))
    asyncio.create_task(leaderboard_loop(bot))
    asyncio.create_task(watchlist_loop(bot))

# ══════════════════════════════════════════════════════════════════════════════
#  MINI WEB APP (static HTML, served if you host a simple HTTP server)
# ══════════════════════════════════════════════════════════════════════════════
WEBAPP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Bags.fm Scanner</title>
<style>
  :root{--bg:#0d1117;--card:#161b22;--border:#30363d;--green:#3fb950;--red:#f85149;--yellow:#d29922;--blue:#58a6ff;--text:#e6edf3;--muted:#8b949e}
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh;padding-bottom:80px}
  header{background:linear-gradient(135deg,#1a1f2e,#0d1117);border-bottom:1px solid var(--border);padding:16px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:10}
  .logo{width:40px;height:40px;background:linear-gradient(135deg,#6e40c9,#3fb950);border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:20px}
  .title h1{font-size:17px;font-weight:700;color:var(--text)}
  .title p{font-size:12px;color:var(--muted)}
  .live-dot{width:8px;height:8px;background:var(--green);border-radius:50%;animation:pulse 2s infinite;margin-left:auto}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
  nav{display:flex;gap:0;border-bottom:1px solid var(--border);background:var(--card);overflow-x:auto}
  nav button{flex:1;min-width:80px;padding:12px 8px;background:none;border:none;color:var(--muted);font-size:12px;font-weight:600;cursor:pointer;border-bottom:2px solid transparent;transition:all .2s;white-space:nowrap}
  nav button.active{color:var(--blue);border-bottom-color:var(--blue)}
  .tab{display:none;padding:12px}
  .tab.active{display:block}
  .card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px;margin-bottom:12px;position:relative}
  .card-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px}
  .token-name{font-weight:700;font-size:15px}
  .token-sym{font-size:12px;color:var(--muted);background:#21262d;padding:2px 8px;border-radius:20px}
  .badge{font-size:11px;padding:3px 8px;border-radius:20px;font-weight:600}
  .badge-new{background:#1d4d2a;color:var(--green)}
  .badge-pump{background:#3d1f1f;color:var(--red)}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px}
  .stat{background:#0d1117;border-radius:8px;padding:8px 10px}
  .stat label{font-size:10px;color:var(--muted);display:block;margin-bottom:2px}
  .stat value{font-size:13px;font-weight:700}
  .up{color:var(--green)}.down{color:var(--red)}.neutral{color:var(--muted)}
  .addr{font-size:11px;color:var(--muted);font-family:monospace;margin-bottom:10px;background:#0d1117;padding:6px 8px;border-radius:6px;display:flex;align-items:center;justify-content:space-between}
  .copy-btn{background:none;border:none;color:var(--blue);cursor:pointer;font-size:12px}
  .actions{display:flex;gap:8px;flex-wrap:wrap}
  .btn{flex:1;min-width:80px;padding:9px 12px;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;text-decoration:none;text-align:center;display:inline-block}
  .btn-primary{background:linear-gradient(135deg,#6e40c9,#3fb950);color:#fff}
  .btn-secondary{background:#21262d;color:var(--text)}
  .btn-danger{background:#3d1f1f;color:var(--red)}
  .risk-high{color:var(--red)}.risk-med{color:var(--yellow)}.risk-low{color:var(--green)}
  .change-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
  .change-bar{flex:1;height:4px;background:#21262d;border-radius:4px;margin:0 10px;overflow:hidden}
  .change-fill{height:100%;border-radius:4px}
  .empty{text-align:center;padding:40px 20px;color:var(--muted)}
  .empty .icon{font-size:48px;margin-bottom:12px}
  .filter-bar{display:flex;gap:8px;margin-bottom:12px;align-items:center}
  .filter-bar input{flex:1;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:8px 12px;color:var(--text);font-size:13px}
  .filter-bar input::placeholder{color:var(--muted)}
  footer{position:fixed;bottom:0;left:0;right:0;background:var(--card);border-top:1px solid var(--border);padding:12px 16px;display:flex;align-items:center;justify-content:space-between}
  footer span{font-size:11px;color:var(--muted)}
  .footer-btn{background:linear-gradient(135deg,#6e40c9,#3fb950);color:#fff;border:none;border-radius:20px;padding:8px 20px;font-weight:700;font-size:13px;cursor:pointer;text-decoration:none}
  .medal{font-size:18px;margin-right:4px}
  .lb-row{display:flex;align-items:center;padding:10px 0;border-bottom:1px solid var(--border)}
  .lb-info{flex:1;margin:0 10px}
  .lb-change{font-weight:700;font-size:14px}
  .spinner{display:flex;justify-content:center;padding:30px}<br>  .spin{width:24px;height:24px;border:3px solid var(--border);border-top-color:var(--blue);border-radius:50%;animation:spin 1s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .stats-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center}
  .stats-card .num{font-size:28px;font-weight:800;color:var(--blue)}
  .stats-card .label{font-size:11px;color:var(--muted);margin-top:4px}
</style>
</head>
<body>
<header>
  <div class="logo">🎯</div>
  <div class="title"><h1>Bags.fm Scanner</h1><p>Real-Time Solana Token Radar</p></div>
  <div class="live-dot" title="Live"></div>
</header>

<nav>
  <button class="active" onclick="showTab('new',this)">🆕 New</button>
  <button onclick="showTab('trending',this)">🔥 Trending</button>
  <button onclick="showTab('leaderboard',this)">🏆 Gainers</button>
  <button onclick="showTab('stats',this)">📊 Stats</button>
  <button onclick="showTab('help',this)">❓ Help</button>
</nav>

<div id="new" class="tab active">
  <div class="filter-bar">
    <input id="search" placeholder="🔍 Search token name or symbol..." oninput="filterTokens()">
  </div>
  <div id="new-list"><div class="spinner"><div class="spin"></div></div></div>
</div>

<div id="trending" class="tab">
  <div id="trending-list"><div class="spinner"><div class="spin"></div></div></div>
</div>

<div id="leaderboard" class="tab">
  <div id="lb-list"><div class="spinner"><div class="spin"></div></div></div>
</div>

<div id="stats" class="tab">
  <div id="stats-content"><div class="spinner"><div class="spin"></div></div></div>
</div>

<div id="help" class="tab">
  <div class="card">
    <div style="font-size:24px;margin-bottom:10px">📖 How to Use</div>
    <p style="color:var(--muted);font-size:13px;line-height:1.6">
      <strong style="color:var(--text)">🆕 New Tokens</strong><br>
      Real-time new token listings from Bags.fm. Filtered by minimum liquidity &amp; volume.<br><br>
      <strong style="color:var(--text)">🔥 Trending</strong><br>
      Tokens with the highest trading volume right now.<br><br>
      <strong style="color:var(--text)">🏆 Top Gainers</strong><br>
      Tokens with the biggest 24h price increase.<br><br>
      <strong style="color:var(--text)">💸 Buy Button</strong><br>
      Opens Bags.fm trading page with our referral link.<br><br>
      <strong style="color:var(--text)">📋 Copy CA</strong><br>
      Copies the contract address to clipboard.<br><br>
      <strong style="color:var(--text)">🛡 RugCheck</strong><br>
      Opens RugCheck safety analysis for the token.<br><br>
      <strong style="color:var(--text)">⚠️ Risk Levels</strong><br>
      🔴 HIGH — Liq &lt; $5K &nbsp;|&nbsp; 🟡 MED — Liq &lt; $20K &nbsp;|&nbsp; 🟢 LOW — Liq ≥ $20K
    </p>
  </div>
  <div class="card">
    <div style="font-size:15px;font-weight:700;margin-bottom:8px">📱 Connect With Us</div>
    <a href="https://t.me/underdeathsol" style="display:block;color:var(--blue);margin-bottom:6px;font-size:14px">📡 @underdeathsol (Telegram Channel)</a>
    <a href="https://x.com/lilfid12" style="display:block;color:var(--blue);margin-bottom:6px;font-size:14px">🐦 @lilfid12 (Twitter/X)</a>
    <a href="https://bags.fm/$LILFID12" style="display:block;color:var(--green);font-size:14px">🛒 Trade on Bags.fm</a>
  </div>
  <div style="text-align:center;padding:16px;color:var(--muted);font-size:11px">
    Powered by Bags.fm · Built for @underdeathsol<br>
    <a href="https://bags.fm" style="color:var(--blue)">bags.fm</a>
  </div>
</div>

<footer>
  <span>📱 @underdeathsol</span>
  <a href="https://bags.fm/$LILFID12" class="footer-btn">💸 Trade on Bags.fm</a>
</footer>

<script>
const REF="https://bags.fm/$LILFID12";
let allTokens=[], trendTokens=[], lbTokens=[];
let loaded={new:false,trending:false,leaderboard:false,stats:false};

function fmtPrice(p){p=parseFloat(p||0);if(!p)return"$0.00";if(p<0.000001)return"$"+p.toFixed(10);if(p<0.001)return"$"+p.toFixed(8);if(p<1)return"$"+p.toFixed(6);return"$"+p.toFixed(4)}
function fmtUsd(n){n=parseFloat(n||0);if(n>=1e6)return"$"+(n/1e6).toFixed(2)+"M";if(n>=1e3)return"$"+(n/1e3).toFixed(1)+"K";return"$"+n.toFixed(2)}
function fmtChange(p){p=parseFloat(p||0);let c=p>=0?"up":"down",s=p>=0?"🟢 +":"🔴 ";return`<span class="${c}">${s}${Math.abs(p).toFixed(2)}%</span>`}
function fmtAge(ts){if(!ts)return"just now";let d=new Date(typeof ts==="number"&&ts>1e12?ts:ts*1000);let mins=Math.floor((Date.now()-d)/60000);if(mins<1)return"just now";if(mins<60)return mins+"m ago";let h=Math.floor(mins/60);if(h<24)return h+"h ago";return Math.floor(h/24)+"d ago"}
function risk(liq,vol){liq=parseFloat(liq||0);if(liq<5000)return'<span class="risk-high">🔴 HIGH RISK</span>';if(liq<20000)return'<span class="risk-med">🟡 MED RISK</span>';return'<span class="risk-low">🟢 LOW RISK</span>'}
function shortAddr(a){return a?a.slice(0,6)+"..."+a.slice(-4):""}
function copyCA(addr){navigator.clipboard.writeText(addr).then(()=>{showToast("📋 CA Copied!")}).catch(()=>{prompt("Copy CA:",addr)})}
function showToast(msg){let t=document.createElement("div");t.innerText=msg;t.style.cssText="position:fixed;top:70px;left:50%;transform:translateX(-50%);background:#3fb950;color:#000;padding:8px 18px;border-radius:20px;font-weight:700;font-size:13px;z-index:999;animation:fadeIn .3s";document.body.appendChild(t);setTimeout(()=>t.remove(),2000)}

function tokenCard(t){
  let addr=t.address||t.mint||"";
  let name=(t.name||"Unknown").slice(0,20);
  let sym=t.symbol||"???";
  let price=t.priceUsd||0;
  let liq=(t.liquidity&&t.liquidity.usd)||t.liquidityUsd||0;
  let vol=(t.volume&&t.volume.h24)||t.volume24h||0;
  let mcap=t.marketCap||t.fdv||0;
  let ch1=(t.priceChange&&t.priceChange.h1)||0;
  let ch24=(t.priceChange&&t.priceChange.h24)||t.priceChange24h||0;
  let created=t.createdAt||t.created_at||t.pairCreatedAt||"";
  let holders=t.holders||t.holderCount||"—";
  let buyUrl=`https://bags.fm/t/${addr}?ref=LILFID12`;
  return`<div class="card">
    <div class="card-header">
      <div>
        <div class="token-name">${name} <span class="token-sym">$${sym}</span></div>
        <div style="font-size:11px;color:var(--muted);margin-top:3px">${risk(liq,vol)} · 🕐 ${fmtAge(created)}</div>
      </div>
      <span class="badge badge-new">NEW</span>
    </div>
    <div class="addr"><span>${shortAddr(addr)}</span><button class="copy-btn" onclick="copyCA('${addr}')">📋 Copy</button></div>
    <div class="grid">
      <div class="stat"><label>💵 Price</label><value>${fmtPrice(price)}</value></div>
      <div class="stat"><label>💎 MCap</label><value>${fmtUsd(mcap)}</value></div>
      <div class="stat"><label>💧 Liquidity</label><value>${fmtUsd(liq)}</value></div>
      <div class="stat"><label>📊 Vol 24H</label><value>${fmtUsd(vol)}</value></div>
      <div class="stat"><label>📈 1H Change</label><value>${fmtChange(ch1)}</value></div>
      <div class="stat"><label>📈 24H Change</label><value>${fmtChange(ch24)}</value></div>
    </div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:10px">👥 Holders: ${holders}</div>
    <div class="actions">
      <a href="${buyUrl}" class="btn btn-primary" target="_blank">💸 BUY NOW</a>
      <a href="https://bags.fm/t/${addr}" class="btn btn-secondary" target="_blank">📊 Chart</a>
      <a href="https://dexscreener.com/solana/${addr}" class="btn btn-secondary" target="_blank">🦅 Dex</a>
      <a href="https://rugcheck.xyz/tokens/${addr}" class="btn btn-danger" target="_blank">🛡 RugCheck</a>
    </div>
  </div>`}

async function loadNew(){
  if(loaded.new)return;loaded.new=true;
  try{
    let r=await fetch("https://api.dexscreener.com/token-profiles/latest/v1");
    let data=await r.json();
    allTokens=(Array.isArray(data)?data:[]).filter(t=>t.chainId==="solana").slice(0,30).map(t=>({
      address:t.tokenAddress||t.address||"",name:t.name||t.description||"?",symbol:t.symbol||"?",
      priceUsd:t.priceUsd||0,liquidity:{usd:t.liquidity||0},volume:{h24:t.volume24h||0},
      marketCap:t.marketCap||0,priceChange:{h1:0,h24:t.priceChange24h||0},
      createdAt:t.createdAt||"",holders:t.holders||"—"
    }));
    renderNew();
  }catch(e){document.getElementById("new-list").innerHTML='<div class="empty"><div class="icon">⚠️</div><p>Could not load tokens.<br>Please try again.</p></div>'}
}
function renderNew(){
  let q=(document.getElementById("search").value||"").toLowerCase();
  let filtered=allTokens.filter(t=>!q||(t.name||"").toLowerCase().includes(q)||(t.symbol||"").toLowerCase().includes(q));
  document.getElementById("new-list").innerHTML=filtered.length?filtered.map(tokenCard).join(""):'<div class="empty"><div class="icon">🔍</div><p>No tokens match your search.</p></div>'
}
function filterTokens(){loaded.new=true;renderNew()}

async function loadTrending(){
  if(loaded.trending)return;loaded.trending=true;
  try{
    let r=await fetch("https://api.dexscreener.com/token-boosts/top/v1");
    let data=await r.json();
    trendTokens=(Array.isArray(data)?data:[]).filter(t=>t.chainId==="solana").slice(0,15).map(t=>({
      address:t.tokenAddress||t.address||"",name:t.name||t.description||"?",symbol:t.symbol||"?",
      priceUsd:t.priceUsd||0,liquidity:{usd:t.liquidity||0},volume:{h24:t.volume24h||0},
      marketCap:t.marketCap||0,priceChange:{h1:0,h24:t.priceChange24h||0},
      createdAt:t.createdAt||"",holders:t.holders||"—"
    }));
    document.getElementById("trending-list").innerHTML=trendTokens.length?trendTokens.map(tokenCard).join(""):'<div class="empty"><div class="icon">🔥</div><p>No trending tokens at the moment.</p></div>'
  }catch(e){document.getElementById("trending-list").innerHTML='<div class="empty"><div class="icon">⚠️</div><p>Could not load trending tokens.</p></div>'}
}

async function loadLB(){
  if(loaded.leaderboard)return;loaded.leaderboard=true;
  try{
    let r=await fetch("https://api.dexscreener.com/latest/dex/search?q=solana&chainIds=solana");
    let data=await r.json();
    let pairs=(data.pairs||[]).sort((a,b)=>parseFloat(b.priceChange?.h24||0)-parseFloat(a.priceChange?.h24||0)).slice(0,10);
    let medals=["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"];
    let html=pairs.map((p,i)=>{
      let base=p.baseToken||{};let addr=base.address||"";
      let ch24=parseFloat(p.priceChange?.h24||0);
      let price=parseFloat(p.priceUsd||0);
      let vol=parseFloat(p.volume?.h24||0);
      return`<div class="lb-row">
        <span class="medal">${medals[i]}</span>
        <div class="lb-info">
          <div style="font-weight:700">${(base.name||"?").slice(0,14)} <span style="color:var(--muted);font-size:12px">$${base.symbol||"?"}</span></div>
          <div style="font-size:11px;color:var(--muted)">Vol: ${fmtUsd(vol)}</div>
        </div>
        <div style="text-align:right">
          <div class="lb-change ${ch24>=0?'up':'down'}">${ch24>=0?'+':''}${ch24.toFixed(2)}%</div>
          <div style="font-size:12px;color:var(--muted)">${fmtPrice(price)}</div>
          <a href="https://bags.fm/t/${addr}?ref=LILFID12" style="font-size:11px;color:var(--green);text-decoration:none" target="_blank">💸 Buy</a>
        </div>
      </div>`}).join("");
    document.getElementById("lb-list").innerHTML=html||'<div class="empty"><div class="icon">🏆</div><p>No data available.</p></div>'
  }catch(e){document.getElementById("lb-list").innerHTML='<div class="empty"><div class="icon">⚠️</div><p>Could not load leaderboard.</p></div>'}
}

function loadStats(){
  if(loaded.stats)return;loaded.stats=true;
  document.getElementById("stats-content").innerHTML=`
    <div class="stats-grid" style="margin-bottom:12px">
      <div class="stats-card"><div class="num">30s</div><div class="label">Scan Interval</div></div>
      <div class="stats-card"><div class="num">$1K</div><div class="label">Min Liquidity</div></div>
      <div class="stats-card"><div class="num">+30%</div><div class="label">Pump Alert Threshold</div></div>
      <div class="stats-card"><div class="num">3x</div><div class="label">Volume Spike Trigger</div></div>
    </div>
    <div class="card">
      <div style="font-size:15px;font-weight:700;margin-bottom:10px">📡 Data Sources</div>
      <div style="font-size:13px;color:var(--muted);line-height:1.8">
        ✅ Bags.fm API (primary)<br>
        ✅ DexScreener API (fallback)<br>
        ✅ Token Profiles Feed<br>
        ✅ Token Boosts Feed
      </div>
    </div>
    <div class="card">
      <div style="font-size:15px;font-weight:700;margin-bottom:10px">🔔 Alert Types</div>
      <div style="font-size:13px;color:var(--muted);line-height:1.9">
        🆕 New Token Detected<br>
        🚀 Pump Alert (+30%)<br>
        📊 Volume Spike (3x)<br>
        🏆 Hourly Leaderboard<br>
        🎯 Watchlist Target Hit
      </div>
    </div>
    <div style="text-align:center;padding:16px;color:var(--muted);font-size:11px">
      Bot by <a href="https://t.me/underdeathsol" style="color:var(--blue)">@underdeathsol</a> ·
      <a href="https://bags.fm" style="color:var(--blue)">bags.fm</a>
    </div>`
}

function showTab(id,btn){
  document.querySelectorAll(".tab").forEach(t=>t.classList.remove("active"));
  document.querySelectorAll("nav button").forEach(b=>b.classList.remove("active"));
  document.getElementById(id).classList.add("active");
  btn.classList.add("active");
  if(id==="new")loadNew();
  if(id==="trending")loadTrending();
  if(id==="leaderboard")loadLB();
  if(id==="stats")loadStats();
}

// Auto-refresh every 60s
setInterval(()=>{loaded.new=false;loaded.trending=false;loadNew();if(document.getElementById("trending").classList.contains("active"))loadTrending()},60000);

loadNew();
</script>
</body>
</html>"""

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    if not BOT_TOKEN:
        log.error("❌ BOT_TOKEN not set in .env!")
        return
    if not BAGS_API_KEY:
        log.warning("⚠️ BAGS_API_KEY not set — will use DexScreener as fallback")

    log.info("🚀 Starting Bags.fm Scanner Bot v2.0...")

    # Write web app HTML to disk (for local serving if needed)
    with open("webapp.html", "w", encoding="utf-8") as f:
        f.write(WEBAPP_HTML)
    log.info("📱 Web App HTML written to webapp.html")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("trending",    cmd_trending))
    app.add_handler(CommandHandler("new",         cmd_new))
    app.add_handler(CommandHandler("scan",        cmd_scan))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("watchlist",   cmd_watchlist))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("pause",       cmd_pause))
    app.add_handler(CommandHandler("resume",      cmd_resume))
    app.add_handler(CommandHandler("setfilter",   cmd_setfilter))
    app.add_handler(CommandHandler("buy",         cmd_buy))
    app.add_handler(CallbackQueryHandler(on_callback))

    log.info("✅ All handlers registered. Bot is live!")
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)

if __name__ == "__main__":
    main()
