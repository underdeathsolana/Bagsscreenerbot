#!/usr/bin/env python3
"""
╔══════════════════════════════════════════╗
║   🎯 BAGS.FM SCANNER BOT v3.1           ║
║   @underdeathsol | Twitter: @lilfid12   ║
╚══════════════════════════════════════════╝
  Fix: correct URL routing (bags.fm native vs external DEX)
"""

import os, asyncio, aiohttp, logging, time, traceback, random, math
from datetime import datetime, timezone
from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, RetryAfter

# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN            = os.getenv("BOT_TOKEN", "")
CHANNEL_ID           = os.getenv("CHANNEL_ID", "")
THREAD_ID            = int(os.getenv("THREAD_ID", "0")) or None
REF_LINK             = os.getenv("REF_LINK", "https://bags.fm/$LILFID12")
BAGS_API_KEY         = os.getenv("BAGS_API_KEY", "")
BAGS_API_BASE        = os.getenv("BAGS_API_BASE", "https://public-api-v2.bags.fm/api/v1")
MIN_LIQUIDITY        = float(os.getenv("MIN_LIQUIDITY", "1000"))
MIN_VOLUME           = float(os.getenv("MIN_VOLUME", "2000"))
SCAN_INTERVAL        = int(os.getenv("SCAN_INTERVAL", "30"))
LEADERBOARD_INTERVAL = int(os.getenv("LEADERBOARD_INTERVAL", "3600"))

BIRDEYE_KEY = os.getenv("BIRDEYE_KEY", "")   # optional, public endpoints work without

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
stats = {"alerts": 0, "scanned": 0, "pumps": 0, "vol_alerts": 0, "start": time.time()}

# ─────────────────────────────────────────────────────────────────────────────
# SKIP LIST — known stablecoins / base tokens that pollute results
SKIP_SYMBOLS = {"SOL","WSOL","USDC","USDT","WBTC","ETH","WETH","BNB","RAY","ORCA"}
SKIP_NAMES   = {"solana","wrapped solana","usd coin","tether","wrapped bitcoin"}

# ══════════════════════════════════════════════════════════════════════════════
#  HTTP HELPERS
# ══════════════════════════════════════════════════════════════════════════════
BAGS_HEADERS = {
    "Authorization": f"Bearer {BAGS_API_KEY}",
    "x-api-key":     BAGS_API_KEY,
    "Accept":        "application/json",
    "User-Agent":    "BagsScanner/3.0",
}
DEX_HEADERS = {
    "Accept":     "application/json",
    "User-Agent": "BagsScanner/3.0",
}
BIRDEYE_HEADERS = {
    "X-API-KEY": BIRDEYE_KEY or "public",
    "x-chain":   "solana",
    "Accept":    "application/json",
}

async def _get(session: aiohttp.ClientSession, url: str,
               params=None, headers=None, timeout=15):
    try:
        async with session.get(
            url,
            headers=headers or DEX_HEADERS,
            params=params,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            if r.status == 200:
                ct = r.headers.get("Content-Type", "")
                if "json" in ct:
                    return await r.json(content_type=None)
            elif r.status not in (404, 403):
                log.debug(f"HTTP {r.status} ← {url}")
    except asyncio.TimeoutError:
        log.debug(f"Timeout: {url}")
    except Exception as e:
        log.debug(f"GET error {url}: {e}")
    return None


def _is_junk(name: str, symbol: str, address: str) -> bool:
    """Return True if this token should be skipped"""
    if not address:
        return True
    sym  = (symbol or "").upper().strip()
    nm   = (name   or "").lower().strip()
    if sym in SKIP_SYMBOLS:
        return True
    if nm in SKIP_NAMES:
        return True
    # very short/generic names are often base tokens
    if len(sym) <= 2 and sym not in {"AI", "UP"}:
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  TOKEN NORMALIZER
# ══════════════════════════════════════════════════════════════════════════════
def _norm_dex(pair: dict) -> dict | None:
    base = pair.get("baseToken", {})
    addr = base.get("address", "")
    name = base.get("name", "")
    sym  = base.get("symbol", "")
    if _is_junk(name, sym, addr):
        return None
    return {
        "address":     addr,
        "name":        name or "Unknown",
        "symbol":      sym  or "???",
        "priceUsd":    float(pair.get("priceUsd") or 0),
        "liquidity":   {"usd": float((pair.get("liquidity") or {}).get("usd") or 0)},
        "volume":      {"h24": float((pair.get("volume")    or {}).get("h24") or 0)},
        "priceChange": {
            "h1":  float((pair.get("priceChange") or {}).get("h1")  or 0),
            "h6":  float((pair.get("priceChange") or {}).get("h6")  or 0),
            "h24": float((pair.get("priceChange") or {}).get("h24") or 0),
        },
        "marketCap":   float(pair.get("fdv") or 0),
        "pairAddress": pair.get("pairAddress", ""),
        "createdAt":   pair.get("pairCreatedAt", ""),
        "txns":        pair.get("txns", {}),
        "holders":     "—",
        "_source":     "dexscreener",
        "_raw":        pair,          # keep raw for chart data
    }

def _norm_profile(item: dict) -> dict | None:
    addr = item.get("tokenAddress") or item.get("address", "")
    name = item.get("name") or item.get("description") or ""
    sym  = item.get("symbol", "")
    if _is_junk(name, sym, addr):
        return None
    return {
        "address":     addr,
        "name":        name or "Unknown",
        "symbol":      sym  or "???",
        "priceUsd":    float(item.get("priceUsd") or 0),
        "liquidity":   {"usd": float(item.get("liquidity") or 0)},
        "volume":      {"h24": float(item.get("volume24h") or 0)},
        "priceChange": {"h1": 0, "h6": 0, "h24": float(item.get("priceChange24h") or 0)},
        "marketCap":   float(item.get("marketCap") or 0),
        "createdAt":   item.get("createdAt", ""),
        "holders":     "—",
        "_source":     "dexscreener_profile",
    }

def _norm_birdeye(item: dict) -> dict | None:
    addr = item.get("address", "")
    name = item.get("name", "")
    sym  = item.get("symbol", "")
    if _is_junk(name, sym, addr):
        return None
    return {
        "address":     addr,
        "name":        name or "Unknown",
        "symbol":      sym  or "???",
        "priceUsd":    float(item.get("price") or item.get("priceUsd") or 0),
        "liquidity":   {"usd": float(item.get("liquidity") or item.get("liquidityUsd") or 0)},
        "volume":      {"h24": float(item.get("v24hUSD") or item.get("volume24h") or 0)},
        "priceChange": {
            "h1":  float(item.get("priceChange1hPercent") or 0),
            "h6":  float(item.get("priceChange6hPercent") or 0),
            "h24": float(item.get("priceChange24hPercent") or 0),
        },
        "marketCap":   float(item.get("mc") or item.get("marketCap") or 0),
        "createdAt":   item.get("listTime") or item.get("createdAt", ""),
        "holders":     item.get("holder") or item.get("holders") or "—",
        "_source":     "birdeye",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  API FETCH FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════
def _dedup(tokens: list) -> list:
    """Remove duplicate addresses, skip junk"""
    seen = set(); out = []
    for t in tokens:
        a = t.get("address", "")
        if a and a not in seen:
            seen.add(a); out.append(t)
    return out


async def fetch_new_tokens(session: aiohttp.ClientSession) -> list:
    results = []

    # ── 1. Bags.fm API ────────────────────────────────────────────────────────
    for url in [
        f"{BAGS_API_BASE}/token-launch/tokens",
        f"{BAGS_API_BASE}/tokens/new",
        f"{BAGS_API_BASE}/coins/new",
    ]:
        data = await _get(session, url, params={"limit":50}, headers=BAGS_HEADERS)
        raw  = _extract(data, ["tokens","data","coins","results","items"])
        if raw:
            log.info(f"✅ Bags.fm new tokens from {url}: {len(raw)}")
            return raw     # trust Bags.fm directly if it returns

    # ── 2. Birdeye new listings ───────────────────────────────────────────────
    data = await _get(
        session,
        "https://public-api.birdeye.so/defi/tokenlist",
        params={"sort_by":"created","sort_type":"desc","offset":0,"limit":50},
        headers=BIRDEYE_HEADERS,
    )
    if data:
        items = _extract(data, ["data","tokens","items"])
        tokens = [t for t in (_norm_birdeye(i) for i in items) if t]
        if tokens:
            log.info(f"✅ Birdeye new listings: {len(tokens)}")
            results.extend(tokens)

    # ── 3. DexScreener token profiles feed (latest) ───────────────────────────
    data = await _get(session, "https://api.dexscreener.com/token-profiles/latest/v1")
    if data:
        raw = data if isinstance(data, list) else data.get("data", [])
        tokens = [t for t in (_norm_profile(i) for i in raw
                               if i.get("chainId") == "solana") if t]
        if tokens:
            log.info(f"✅ DexScreener profiles: {len(tokens)}")
            results.extend(tokens)

    # ── 4. DexScreener new pairs on Solana ────────────────────────────────────
    # Search several known Solana DEX labels to get diverse fresh tokens
    for query in ["pump", "moon", "pepe", "dog", "cat", "ai", "baby", "meme"]:
        data = await _get(
            session,
            "https://api.dexscreener.com/latest/dex/search",
            params={"q": query},
        )
        if data:
            pairs = [p for p in data.get("pairs", [])
                     if p.get("chainId") == "solana"]
            # sort by pair creation time desc
            pairs.sort(key=lambda p: p.get("pairCreatedAt") or 0, reverse=True)
            tokens = [t for t in (_norm_dex(p) for p in pairs[:15]) if t]
            results.extend(tokens)
        await asyncio.sleep(0.2)

    results = _dedup(results)
    log.info(f"✅ Total unique new tokens after dedup: {len(results)}")
    return results


async def fetch_trending_tokens(session: aiohttp.ClientSession) -> list:
    results = []

    # Bags.fm
    for url in [f"{BAGS_API_BASE}/market/trending", f"{BAGS_API_BASE}/tokens/trending"]:
        data = await _get(session, url, params={"limit":20}, headers=BAGS_HEADERS)
        raw  = _extract(data, ["tokens","data","coins"])
        if raw: return raw

    # Birdeye trending
    data = await _get(
        session,
        "https://public-api.birdeye.so/defi/tokenlist",
        params={"sort_by":"v24hUSD","sort_type":"desc","offset":0,"limit":20},
        headers=BIRDEYE_HEADERS,
    )
    if data:
        items  = _extract(data, ["data","tokens","items"])
        tokens = [t for t in (_norm_birdeye(i) for i in items) if t]
        if tokens:
            log.info(f"✅ Birdeye trending: {len(tokens)}")
            results.extend(tokens)

    # DexScreener boosts (boosted = trending)
    data = await _get(session, "https://api.dexscreener.com/token-boosts/top/v1")
    if data:
        raw    = data if isinstance(data, list) else data.get("data", [])
        tokens = [t for t in (_norm_profile(i) for i in raw
                               if i.get("chainId") == "solana") if t]
        if tokens:
            results.extend(tokens)

    return _dedup(results)[:20]


async def fetch_gainers(session: aiohttp.ClientSession) -> list:
    # Bags.fm
    for url in [f"{BAGS_API_BASE}/market/gainers", f"{BAGS_API_BASE}/tokens/gainers"]:
        data = await _get(session, url, params={"limit":10,"period":"24h"}, headers=BAGS_HEADERS)
        raw  = _extract(data, ["tokens","data","gainers"])
        if raw: return raw

    # Birdeye — sort by 24h change
    data = await _get(
        session,
        "https://public-api.birdeye.so/defi/tokenlist",
        params={"sort_by":"priceChange24hPercent","sort_type":"desc","offset":0,"limit":30},
        headers=BIRDEYE_HEADERS,
    )
    if data:
        items  = _extract(data, ["data","tokens","items"])
        tokens = [t for t in (_norm_birdeye(i) for i in items) if t]
        if tokens:
            return sorted(tokens,
                          key=lambda x: x["priceChange"]["h24"],
                          reverse=True)[:10]

    # DexScreener: fetch several popular meme queries, sort by 24h gain
    all_pairs = []
    for q in ["meme","pump","pepe","dog","moon"]:
        data = await _get(session, "https://api.dexscreener.com/latest/dex/search", params={"q": q})
        if data:
            all_pairs += [p for p in data.get("pairs",[]) if p.get("chainId")=="solana"]
        await asyncio.sleep(0.15)
    all_pairs.sort(key=lambda p: float((p.get("priceChange") or {}).get("h24") or 0), reverse=True)
    tokens = [t for t in (_norm_dex(p) for p in all_pairs[:30]) if t]
    return _dedup(tokens)[:10]


async def fetch_token_detail(session: aiohttp.ClientSession, address: str) -> dict | None:
    # Bags.fm
    for url in [f"{BAGS_API_BASE}/token-launch/tokens/{address}", f"{BAGS_API_BASE}/tokens/{address}"]:
        data = await _get(session, url, headers=BAGS_HEADERS)
        if data:
            t = data.get("token", data)
            if isinstance(t, dict) and t.get("address"):
                return t

    # Birdeye token overview
    data = await _get(
        session,
        f"https://public-api.birdeye.so/defi/token_overview",
        params={"address": address},
        headers=BIRDEYE_HEADERS,
    )
    if data:
        item = data.get("data", data)
        if isinstance(item, dict) and item.get("address"):
            t = _norm_birdeye(item)
            if t: return t

    # DexScreener
    data = await _get(session, f"https://api.dexscreener.com/latest/dex/tokens/{address}")
    if data and data.get("pairs"):
        pairs = [p for p in data["pairs"] if p.get("chainId")=="solana"]
        if pairs:
            t = _norm_dex(pairs[0])
            if t: return t

    return None


async def fetch_ohlcv(session: aiohttp.ClientSession, address: str) -> list:
    """Fetch OHLCV candle data for mini chart — returns list of close prices"""

    # Birdeye OHLCV
    data = await _get(
        session,
        "https://public-api.birdeye.so/defi/ohlcv",
        params={
            "address":   address,
            "type":      "15m",
            "time_from": int(time.time()) - 3600 * 6,   # last 6h
            "time_to":   int(time.time()),
        },
        headers=BIRDEYE_HEADERS,
    )
    if data:
        items = _extract(data, ["data","items","ohlcv"])
        if items and isinstance(items, list):
            closes = [float(c.get("c", c.get("close", 0)) or 0) for c in items if c]
            closes = [c for c in closes if c > 0]
            if len(closes) >= 4:
                log.debug(f"📊 Got {len(closes)} candles from Birdeye for {address[:8]}")
                return closes[-20:]

    # DexScreener: derive from priceChange data as fallback sparkline
    data = await _get(session, f"https://api.dexscreener.com/latest/dex/tokens/{address}")
    if data and data.get("pairs"):
        p     = data["pairs"][0]
        price = float(p.get("priceUsd") or 0)
        if price > 0:
            ch1h  = float((p.get("priceChange") or {}).get("h1")  or 0) / 100
            ch6h  = float((p.get("priceChange") or {}).get("h6")  or 0) / 100
            ch24h = float((p.get("priceChange") or {}).get("h24") or 0) / 100
            # Reconstruct approximate price series (6 points)
            p_now  = price
            p_1h   = price / (1 + ch1h)  if ch1h  != -1 else price
            p_6h   = price / (1 + ch6h)  if ch6h  != -1 else price
            p_24h  = price / (1 + ch24h) if ch24h != -1 else price
            closes = [p_24h, (p_24h+p_6h)/2, p_6h, (p_6h+p_1h)/2, p_1h, p_now]
            return closes

    return []


def _extract(data, keys):
    if isinstance(data, list) and data:
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list) and v:
                return v
    return []


# ══════════════════════════════════════════════════════════════════════════════
#  MINI CHART (ASCII sparkline)
# ══════════════════════════════════════════════════════════════════════════════
SPARK_CHARS = ["▁","▂","▃","▄","▅","▆","▇","█"]

def mini_chart(prices: list, width: int = 16) -> str:
    """Return an ASCII sparkline string from a price list"""
    if len(prices) < 2:
        return "〰️ No chart data"

    # Resample to `width` points
    if len(prices) > width:
        step   = len(prices) / width
        prices = [prices[int(i * step)] for i in range(width)]

    lo, hi = min(prices), max(prices)
    if hi == lo:
        bar = "".join(SPARK_CHARS[3] for _ in prices)
    else:
        bar = "".join(
            SPARK_CHARS[round((p - lo) / (hi - lo) * 7)]
            for p in prices
        )

    # Trend arrow
    trend = "📈" if prices[-1] >= prices[0] else "📉"
    pct   = ((prices[-1] - prices[0]) / prices[0] * 100) if prices[0] else 0
    sign  = "+" if pct >= 0 else ""
    return f"{trend} `{bar}` ({sign}{pct:.1f}%)"


# ══════════════════════════════════════════════════════════════════════════════
#  FORMATTERS
# ══════════════════════════════════════════════════════════════════════════════
def fmt_usd(n):
    n = float(n or 0)
    if n >= 1_000_000_000: return f"${n/1e9:.2f}B"
    if n >= 1_000_000:     return f"${n/1e6:.2f}M"
    if n >= 1_000:         return f"${n/1e3:.1f}K"
    return f"${n:.2f}"

def fmt_price(p):
    p = float(p or 0)
    if p == 0:       return "$0.00"
    if p < 1e-9:     return f"${p:.14f}"
    if p < 0.000001: return f"${p:.10f}"
    if p < 0.001:    return f"${p:.8f}"
    if p < 1:        return f"${p:.6f}"
    return f"${p:.4f}"

def fmt_chg(pct):
    pct = float(pct or 0)
    return f"🟢 +{pct:.2f}%" if pct >= 0 else f"🔴 {pct:.2f}%"

def age_str(ts):
    try:
        if not ts: return "just now"
        if isinstance(ts, (int,float)):
            t = ts/1000 if ts > 1e10 else ts
            dt = datetime.fromtimestamp(t, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(ts).replace("Z","+00:00"))
        delta = datetime.now(timezone.utc) - dt
        m = int(delta.total_seconds()/60)
        if m < 1:   return "just now"
        if m < 60:  return f"{m}m ago"
        h = m//60
        if h < 24:  return f"{h}h ago"
        return f"{h//24}d ago"
    except: return "just now"

def risk_tag(liq):
    liq = float(liq or 0)
    if liq < 5_000:  return "🔴 HIGH"
    if liq < 20_000: return "🟡 MED"
    return "🟢 LOW"

def sg(d, *keys, default=0):
    for k in keys:
        if isinstance(d, dict): d = d.get(k, default)
        else: return default
    return d if d is not None else default


# ══════════════════════════════════════════════════════════════════════════════
#  MESSAGE BUILDERS
# ══════════════════════════════════════════════════════════════════════════════
def _buy_url(addr: str, source: str = "") -> str:
    """
    Smart BUY URL routing based on token source:
    - bags.fm native token  → bags.fm/<mint>?ref=LILFID12
      (tokens launched ON Bags.fm have a trade page at bags.fm/<mint>)
    - external tokens (DexScreener/Birdeye) → Jupiter swap
      (these tokens are NOT on Bags.fm and will 404 if sent there)
    - REF_LINK used on Jupiter via partnerReferral param
    """
    if source == "bags.fm":
        # Token was launched on Bags.fm — direct token page exists
        return f"https://bags.fm/{addr}?ref=LILFID12"
    # External token — use Jupiter (universal Solana DEX aggregator)
    return f"https://jup.ag/swap/SOL-{addr}"

def _jupiter_url(addr: str) -> str:
    """Jupiter swap URL — direct fallback for any Solana token"""
    return f"https://jup.ag/swap/SOL-{addr}"

def _chart_url(addr: str, source: str = "", pair_addr: str = "") -> str:
    """
    Best chart URL:
    - bags.fm tokens  → bags.fm/<mint>  (has native chart)
    - other tokens    → DexScreener pair page (most reliable)
    """
    if source == "bags.fm":
        return f"https://bags.fm/{addr}"
    if pair_addr:
        return f"https://dexscreener.com/solana/{pair_addr}"
    return f"https://dexscreener.com/solana/{addr}"

def _bags_url(addr: str = "") -> str:
    """Bags.fm URL — only for bags.fm native tokens"""
    if addr:
        return f"https://bags.fm/{addr}?ref=LILFID12"
    return REF_LINK

def build_new_token_msg(token: dict, chart: str = "") -> tuple:
    addr      = token.get("address", "")
    name      = (token.get("name") or "Unknown")[:22]
    sym       = (token.get("symbol") or "???").upper()
    price     = sg(token, "priceUsd", default=0)
    liq       = sg(token, "liquidity", "usd", default=0)
    vol24     = sg(token, "volume", "h24", default=0)
    mcap      = sg(token, "marketCap", default=0)
    ch1h      = sg(token, "priceChange", "h1",  default=0)
    ch6h      = sg(token, "priceChange", "h6",  default=0)
    ch24h     = sg(token, "priceChange", "h24", default=0)
    created   = token.get("createdAt") or token.get("created_at") or ""
    holders   = token.get("holders") or token.get("holderCount") or "—"
    txns_buy  = sg(token, "txns", "h24", "buys",  default=0)
    txns_sell = sg(token, "txns", "h24", "sells", default=0)
    source    = token.get("_source", "dexscreener")
    pair_addr = token.get("pairAddress", "")
    short     = f"{addr[:6]}...{addr[-4:]}" if len(addr) > 10 else addr

    # ── Smart URLs ────────────────────────────────────────────────────────────
    buy_url   = _buy_url(addr, source)
    chart_url = _chart_url(addr, source, pair_addr)
    dex_url   = f"https://dexscreener.com/solana/{pair_addr or addr}"
    bird_url  = f"https://birdeye.so/token/{addr}?chain=solana"
    rug_url   = f"https://rugcheck.xyz/tokens/{addr}"

    # Smart buy label: Bags.fm for native, Jupiter for external
    if source == "bags.fm":
        buy_label = "💸  BUY on Bags.fm  🛒"
    else:
        buy_label = "💸  BUY on Jupiter  🚀"

    chart_line = f"\n📊  *Mini Chart (6h):*\n{chart}\n" if chart else ""

    # Source badge
    src_badge = {
        "bags.fm":             "🎒 Bags.fm",
        "dexscreener":         "🦅 DexScreener",
        "dexscreener_profile": "🦅 DexScreener",
        "birdeye":             "🐦 Birdeye",
    }.get(source, f"📡 {source}")

    text = (
        "╔═══════════════════════════╗\n"
        "║  🚨  NEW TOKEN DETECTED   ║\n"
        "╚═══════════════════════════╝\n\n"
        f"🏷  *{name}*  ·  `${sym}`\n"
        f"📋  `{short}`\n"
        f"⚠️  Risk: {risk_tag(liq)}  ·  🕐 {age_str(created)}\n"
        f"📡  Via: {src_badge}\n\n"
        "──────── 💰 MARKET DATA ────────\n"
        f"💵  *Price:*      `{fmt_price(price)}`\n"
        f"💎  *Mkt Cap:*    `{fmt_usd(mcap)}`\n"
        f"💧  *Liquidity:*  `{fmt_usd(liq)}`\n"
        f"📊  *Vol 24h:*    `{fmt_usd(vol24)}`\n"
        f"👥  *Holders:*    `{holders}`\n"
        f"🔄  *Txns 24h:*   `{txns_buy}B / {txns_sell}S`\n\n"
        "──────── 📈 PRICE CHANGE ───────\n"
        f"  1H   {fmt_chg(ch1h)}\n"
        f"  6H   {fmt_chg(ch6h)}\n"
        f" 24H   {fmt_chg(ch24h)}\n"
        f"{chart_line}\n"
        "────────────────────────────────\n"
        f"📱  [@underdeathsol](https://t.me/underdeathsol)  ·  "
        f"🐦  [@lilfid12](https://x.com/lilfid12)"
    )

    # For external tokens, show both Jupiter buy + Bags.fm referral as secondary
    if source == "bags.fm":
        buy_rows = [
            [InlineKeyboardButton(buy_label, url=buy_url)],
        ]
    else:
        buy_rows = [
            [InlineKeyboardButton(buy_label, url=buy_url)],
            [InlineKeyboardButton("🎒 Also on Bags.fm", url=REF_LINK)],
        ]

    kb = InlineKeyboardMarkup(
        buy_rows + [
            [
                InlineKeyboardButton("📊 Chart",       url=chart_url),
                InlineKeyboardButton("🦅 DexScreener", url=dex_url),
            ],
            [
                InlineKeyboardButton("🔍 Birdeye",  url=bird_url),
                InlineKeyboardButton("🛡 RugCheck", url=rug_url),
            ],
            [
                InlineKeyboardButton("📋 Copy CA",  callback_data=f"ca_{addr}"),
                InlineKeyboardButton("⭐ Watchlist", callback_data=f"wl_{addr[:20]}_{sym[:8]}_{name[:12]}"),
            ],
        ]
    )
    return text, kb


def build_pump_msg(token: dict, pct: float, chart: str = "") -> tuple:
    addr      = token.get("address", "")
    name      = (token.get("name") or "Unknown")[:22]
    sym       = (token.get("symbol") or "???").upper()
    price     = sg(token, "priceUsd", default=0)
    liq       = sg(token, "liquidity", "usd", default=0)
    vol       = sg(token, "volume", "h24", default=0)
    source    = token.get("_source", "dexscreener")
    pair_addr = token.get("pairAddress", "")
    emoji     = "🚀🚀🚀" if pct >= 100 else ("🚀🚀" if pct >= 50 else "⚡🚀")
    chart_line = f"\n📊  *Mini Chart:*\n{chart}\n" if chart else ""

    buy_url   = _buy_url(addr, source)
    chart_url = _chart_url(addr, source, pair_addr)
    dex_url   = f"https://dexscreener.com/solana/{pair_addr or addr}"
    if source == "bags.fm":
        buy_label = "💸 BUY on Bags.fm 🚀"
    else:
        buy_label = "💸 BUY on Jupiter 🚀"

    text = (
        f"╔═══════════════════════════╗\n"
        f"║  {emoji}  PUMP ALERT!        ║\n"
        f"╚═══════════════════════════╝\n\n"
        f"🏷  *{name}*  ·  `${sym}`\n"
        f"📋  `{addr[:6]}...{addr[-4:]}`\n\n"
        f"💵  *Price:*  `{fmt_price(price)}`\n"
        f"📈  *Gain:*   `+{pct:.1f}%`  ← {fmt_chg(pct)}\n"
        f"💧  *Liq:*    `{fmt_usd(liq)}`\n"
        f"📊  *Vol:*    `{fmt_usd(vol)}`\n"
        f"{chart_line}\n"
        f"⚡  *Act fast — momentum detected!*\n\n"
        f"📱  *@underdeathsol*  ·  🐦  *@lilfid12*"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(buy_label, url=buy_url)],
        [InlineKeyboardButton("🔀 Buy on Jupiter", url=_jupiter_url(addr))],
        [
            InlineKeyboardButton("📊 Chart",       url=chart_url),
            InlineKeyboardButton("🦅 DexScreener", url=dex_url),
        ],
    ])
    return text, kb


def build_volume_msg(token: dict, ratio: float, chart: str = "") -> tuple:
    addr      = token.get("address", "")
    name      = (token.get("name") or "Unknown")[:22]
    sym       = (token.get("symbol") or "???").upper()
    price     = sg(token, "priceUsd", default=0)
    vol       = sg(token, "volume", "h24", default=0)
    source    = token.get("_source", "dexscreener")
    pair_addr = token.get("pairAddress", "")
    chart_line = f"\n📊  *Mini Chart:*\n{chart}\n" if chart else ""

    buy_url   = _buy_url(addr, source)
    chart_url = _chart_url(addr, source, pair_addr)
    if source == "bags.fm":
        buy_label = "💸 BUY on Bags.fm 🛒"
    else:
        buy_label = "💸 BUY on Jupiter 🛒"

    text = (
        "╔═══════════════════════════╗\n"
        "║  📊  VOLUME SPIKE!  🔥    ║\n"
        "╚═══════════════════════════╝\n\n"
        f"🏷  *{name}*  ·  `${sym}`\n"
        f"📋  `{addr[:6]}...{addr[-4:]}`\n\n"
        f"💵  *Price:*   `{fmt_price(price)}`\n"
        f"📊  *Vol 24h:* `{fmt_usd(vol)}`\n"
        f"🔥  *Spike:*   `{ratio:.1f}x` above avg\n"
        f"{chart_line}\n"
        f"👁  *Unusual buying activity!*\n\n"
        f"📱  *@underdeathsol*"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(buy_label,  url=buy_url)],
        [InlineKeyboardButton("📊 Chart", url=chart_url)],
    ])
    return text, kb


def build_leaderboard_msg(tokens: list) -> str:
    if not tokens:
        return (
            "╔═══════════════════════════╗\n"
            "║  🏆  TOP GAINERS (24H)    ║\n"
            "╚═══════════════════════════╝\n\n"
            "📭  No gainers data right now.\n"
            "Bot is still warming up — check back soon!\n\n"
            f"🔗  [Trade on Bags.fm]({REF_LINK})"
        )
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    now    = datetime.now(timezone.utc).strftime("%d %b · %H:%M UTC")
    lines  = [
        "╔═══════════════════════════╗\n"
        "║  🏆  TOP GAINERS (24H)    ║\n"
        f"╚══════  📅 {now}  ══════╝\n"
    ]
    for i, t in enumerate(tokens[:10]):
        addr  = t.get("address", "")
        name  = (t.get("name") or "?")[:16]
        sym   = (t.get("symbol") or "?").upper()
        ch24  = sg(t, "priceChange", "h24", default=0)
        price = sg(t, "priceUsd", default=0)
        vol   = sg(t, "volume", "h24", default=0)
        liq   = sg(t, "liquidity", "usd", default=0)
        src   = t.get("_source","dexscreener")
        buy_u = _buy_url(addr, src)
        buy   = f"[💸]({buy_u})"
        lines.append(
            f"{medals[i]} *{name}* `${sym}`\n"
            f"   💵 `{fmt_price(price)}`  {fmt_chg(ch24)}\n"
            f"   📊 Vol: `{fmt_usd(vol)}`  💧 `{fmt_usd(liq)}`\n"
            f"   {buy} Buy now\n"
        )
    lines.append(
        "────────────────────────────────\n"
        f"🔗  [Trade on Bags.fm]({REF_LINK})\n"
        f"📱  *@underdeathsol*  ·  🐦  *@lilfid12*"
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
            log.error(f"TG error (try {attempt+1}): {e}")
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
                        addr = token.get("address", "")
                        if not addr or addr in seen_tokens:
                            continue
                        liq = float(sg(token,"liquidity","usd",default=0))
                        vol = float(sg(token,"volume","h24",default=0))
                        if liq < MIN_LIQUIDITY or vol < MIN_VOLUME:
                            seen_tokens.add(addr)
                            continue

                        seen_tokens.add(addr)
                        tracked_tokens[addr] = {
                            "name":   token.get("name","?"),
                            "symbol": (token.get("symbol") or "?").upper(),
                            "price":  float(sg(token,"priceUsd",default=0)),
                            "vol":    vol,
                            "pumped": False,
                            "vol_alerted": False,
                            "data":   token,
                        }

                        # Fetch mini chart
                        chart = ""
                        try:
                            prices = await fetch_ohlcv(session, addr)
                            if prices:
                                chart = mini_chart(prices)
                        except:
                            pass

                        text, kb = build_new_token_msg(token, chart)
                        await send_ch(bot, text, kb)
                        log.info(f"🆕 {token.get('symbol','?')} | {addr[:8]} | liq={fmt_usd(liq)} vol={fmt_usd(vol)}")
                        await asyncio.sleep(0.5)

                    await _check_tracked(session, bot)

            except Exception as e:
                log.error(f"Scanner error: {e}\n{traceback.format_exc()}")
            await asyncio.sleep(SCAN_INTERVAL)


async def _check_tracked(session, bot):
    for addr, info in list(tracked_tokens.items()):
        try:
            detail = await fetch_token_detail(session, addr)
            if not detail: continue

            cur_p = float(sg(detail,"priceUsd",default=0))
            old_p = info.get("price", cur_p)
            cur_v = float(sg(detail,"volume","h24",default=0))
            old_v = info.get("vol", cur_v) or cur_v

            if old_p > 0 and not info["pumped"]:
                pct = ((cur_p - old_p) / old_p) * 100
                if pct >= 30:
                    prices = await fetch_ohlcv(session, addr)
                    chart  = mini_chart(prices) if prices else ""
                    t, kb  = build_pump_msg(detail, pct, chart)
                    await send_ch(bot, t, kb)
                    tracked_tokens[addr]["pumped"] = True
                    stats["pumps"] += 1
                    log.info(f"🚀 Pump: {info['symbol']} +{pct:.1f}%")

            if old_v > 0 and not info["vol_alerted"]:
                ratio = cur_v / old_v
                if ratio >= 3:
                    prices = await fetch_ohlcv(session, addr)
                    chart  = mini_chart(prices) if prices else ""
                    t, kb  = build_volume_msg(detail, ratio, chart)
                    await send_ch(bot, t, kb)
                    tracked_tokens[addr]["vol_alerted"] = True
                    stats["vol_alerts"] += 1

            tracked_tokens[addr].update({"price": cur_p, "vol": cur_v})
            await asyncio.sleep(0.3)
        except Exception as e:
            log.debug(f"Track error {addr}: {e}")


async def leaderboard_loop(bot: Bot):
    log.info("🏆 Leaderboard loop started")
    await asyncio.sleep(90)
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
                    if not detail: continue
                    price  = float(sg(detail,"priceUsd",default=0))
                    target = float(info.get("target_price") or 0)
                    if target and price >= target:
                        name = info.get("name","?"); sym = info.get("symbol","?")
                        text = (
                            "╔═══════════════════════════╗\n"
                            "║  🎯  WATCHLIST TARGET HIT ║\n"
                            "╚═══════════════════════════╝\n\n"
                            f"🏷  *{name}* `${sym}`\n"
                            f"💵  Price:  `{fmt_price(price)}`\n"
                            f"🎯  Target: `{fmt_price(target)}`\n\n"
                            f"✅  Target reached!\n\n"
                            f"[💸 Buy Now]({_buy_url(addr, info.get('_source','dexscreener'))})"
                        )
                        await send_ch(bot, text)
                        watchlist.pop(addr, None)
                    await asyncio.sleep(0.3)
                except: pass
            await asyncio.sleep(60)


# ══════════════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════════════════
HELP_TEXT = (
    "╔═══════════════════════════╗\n"
    "║  📖  HOW TO USE  —  GUIDE ║\n"
    "╚═══════════════════════════╝\n\n"
    "━━━ 📋 ALL COMMANDS ━━━━━━━━━━━━\n\n"
    "🔵  */start*  —  Main menu\n\n"
    "🔵  */new*  —  Latest new tokens\n\n"
    "🔵  */trending*  —  Top by volume\n\n"
    "🔵  */leaderboard*  —  Top 24h gainers\n\n"
    "🔵  */scan* `<address>`  —  Scan any token\n"
    "    _ex: /scan 7xKXtg2CW..._\n\n"
    "🔵  */watchlist*  —  Your saved tokens\n\n"
    "🔵  */setfilter* `<liq> <vol>`\n"
    "    _ex: /setfilter 5000 10000_\n\n"
    "🔵  */stats*  —  Bot performance\n\n"
    "🔵  */buy*  —  Open Bags.fm trade page\n\n"
    "🔵  */pause*  /  */resume*  —  Toggle scan\n\n"
    "━━━ 🔔 AUTO ALERTS ━━━━━━━━━━━━━\n\n"
    "🆕  *New Token*  — meets liq & vol filter\n"
    "🚀  *Pump Alert* — token gains +30%\n"
    "📊  *Vol Spike*  — volume 3x above avg\n"
    "🏆  *Leaderboard* — every hour, top 10\n"
    "🎯  *Watchlist*  — price target hit\n\n"
    "━━━ 📌 ALERT BUTTONS ━━━━━━━━━━━\n\n"
    "💸  *BUY on Bags.fm* — with referral link\n"
    "📊  *Chart*      — live chart on Bags.fm\n"
    "🦅  *DexScreener* — pair analytics\n"
    "🔍  *Birdeye*    — token overview\n"
    "🛡  *RugCheck*   — safety analysis\n"
    "📋  *Copy CA*    — contract address popup\n"
    "⭐  *Watchlist*  — add target price alert\n\n"
    "━━━ 📊 MINI CHART ━━━━━━━━━━━━━\n\n"
    "Each alert shows a 6h sparkline chart:\n"
    "📈 `▁▂▃▅▆▇` (+X.X%) = uptrend\n"
    "📉 `▇▆▅▃▂▁` (-X.X%) = downtrend\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    f"📱 [@underdeathsol](https://t.me/underdeathsol)\n"
    f"🐦 [@lilfid12](https://x.com/lilfid12)\n"
    f"🛒 [Trade on Bags.fm]({REF_LINK})"
)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "╔═══════════════════════════╗\n"
        "║  🤖  BAGS.FM SCANNER BOT  ║\n"
        "║  v3.0  Real-Time Radar ✅ ║\n"
        "╚═══════════════════════════╝\n\n"
        "👋  *Welcome!* I scan *Bags.fm* 24/7 and alert\n"
        "you instantly when new Solana tokens appear.\n\n"
        "📡  *Features:*\n"
        "  🆕  Real-time new token detection\n"
        "  📊  Mini sparkline chart on every alert\n"
        "  🚀  Pump alerts (+30%)\n"
        "  🔥  Volume spike alerts (3x)\n"
        "  🏆  Hourly top-gainers leaderboard\n"
        "  ⭐  Personal watchlist with targets\n"
        "  🛡  RugCheck safety links\n\n"
        "💡  *Data sources:* Bags.fm · Birdeye · DexScreener\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 [@underdeathsol](https://t.me/underdeathsol)  ·  🐦 [@lilfid12](https://x.com/lilfid12)"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆕 New Tokens",  callback_data="new"),
            InlineKeyboardButton("🔥 Trending",    callback_data="trending"),
        ],
        [
            InlineKeyboardButton("🏆 Leaderboard", callback_data="lb"),
            InlineKeyboardButton("📈 Bot Stats",   callback_data="stats"),
        ],
        [
            InlineKeyboardButton("📖 How to Use",  callback_data="help"),
            InlineKeyboardButton("⭐ Watchlist",   callback_data="wl_view"),
        ],
        [InlineKeyboardButton("💸  Trade on Bags.fm  🛒", url=REF_LINK)],
    ])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=kb, disable_web_page_preview=True)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Trade on Bags.fm", url=REF_LINK)],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu")],
    ])
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=kb, disable_web_page_preview=True)

async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Fetching new tokens from all sources...")
    async with aiohttp.ClientSession() as session:
        tokens = await fetch_new_tokens(session)
    tokens = [t for t in tokens if t.get("address")][:8]
    if not tokens:
        await msg.edit_text("❌ No new tokens found right now. Try again in a moment.")
        return
    lines = ["╔═══════════════════════════╗\n║  🆕  NEWEST ON BAGS.FM    ║\n╚═══════════════════════════╝\n"]
    for t in tokens:
        addr  = t.get("address","")
        name  = (t.get("name") or "?")[:18]
        sym   = (t.get("symbol") or "?").upper()
        price = sg(t,"priceUsd",default=0)
        liq   = sg(t,"liquidity","usd",default=0)
        ch24  = sg(t,"priceChange","h24",default=0)
        age   = age_str(t.get("createdAt",""))
        src   = t.get("_source","dexscreener")
        lines.append(
            f"🆕  *{name}*  `${sym}`\n"
            f"    💵 `{fmt_price(price)}`  {fmt_chg(ch24)}\n"
            f"    💧 `{fmt_usd(liq)}`  🕐 `{age}`\n"
            f"    [💸 Buy]({_buy_url(addr, src)})\n"
        )
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n[🛒 Trade on Bags.fm]({REF_LINK})")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def cmd_trending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Fetching trending tokens...")
    async with aiohttp.ClientSession() as session:
        tokens = await fetch_trending_tokens(session)
    if not tokens:
        await msg.edit_text("❌ No trending data available right now.")
        return
    lines = ["╔═══════════════════════════╗\n║  🔥  TRENDING ON BAGS.FM  ║\n╚═══════════════════════════╝\n"]
    for i, t in enumerate(tokens[:10], 1):
        addr  = t.get("address","")
        name  = (t.get("name") or "?")[:16]
        sym   = (t.get("symbol") or "?").upper()
        price = sg(t,"priceUsd",default=0)
        ch24  = sg(t,"priceChange","h24",default=0)
        vol   = sg(t,"volume","h24",default=0)
        src   = t.get("_source","dexscreener")
        lines.append(
            f"{i:02d}.  *{name}*  `${sym}`\n"
            f"      💵 `{fmt_price(price)}`  {fmt_chg(ch24)}\n"
            f"      📊 Vol: `{fmt_usd(vol)}`\n"
            f"      [💸 Buy]({_buy_url(addr, src)})\n"
        )
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n[🛒 Bags.fm]({REF_LINK})")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="trending")]])
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN,
                        reply_markup=kb, disable_web_page_preview=True)

async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "⚠️  *Usage:* `/scan <token_address>`\n"
            "_Example: /scan 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgHkv_",
            parse_mode=ParseMode.MARKDOWN)
        return
    addr = ctx.args[0].strip()
    msg  = await update.message.reply_text(f"🔍 Scanning `{addr[:8]}...{addr[-4:]}`", parse_mode=ParseMode.MARKDOWN)
    async with aiohttp.ClientSession() as session:
        token  = await fetch_token_detail(session, addr)
        prices = await fetch_ohlcv(session, addr) if token else []
    if not token:
        await msg.edit_text("❌ Token not found. Check the address and try again.")
        return
    chart = mini_chart(prices) if prices else ""
    text, kb = build_new_token_msg(token, chart)
    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True)

async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🏆 Building leaderboard...")
    async with aiohttp.ClientSession() as session:
        tokens = await fetch_gainers(session)
    text = build_leaderboard_msg(tokens)
    kb   = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="lb_refresh")],
        [InlineKeyboardButton("💸 Trade on Bags.fm", url=REF_LINK)],
    ])
    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True)

async def cmd_watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not watchlist:
        await update.message.reply_text(
            "⭐  *Watchlist Empty*\n\n"
            "Tap the ⭐ *Watchlist* button on any token alert to add it.\n"
            "You'll be notified when the target price is hit!",
            parse_mode=ParseMode.MARKDOWN)
        return
    lines = ["╔═══════════════════════════╗\n║  ⭐  YOUR WATCHLIST       ║\n╚═══════════════════════════╝\n"]
    for addr, info in watchlist.items():
        lines.append(
            f"🏷  *{info['name']}*  `${info['symbol']}`\n"
            f"    🎯 Target: `{fmt_price(info.get('target_price',0))}`\n"
            f"    [View]({_chart_url(addr)})\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    up = int(time.time() - stats["start"])
    h, rem = divmod(up, 3600); m, s = divmod(rem, 60)
    await update.message.reply_text(
        "╔═══════════════════════════╗\n"
        "║  📈  BOT STATISTICS       ║\n"
        "╚═══════════════════════════╝\n\n"
        f"⏱  *Uptime:*         `{h}h {m}m {s}s`\n"
        f"🔍  *Tokens Scanned:* `{stats['scanned']}`\n"
        f"📢  *Alerts Sent:*    `{stats['alerts']}`\n"
        f"🚀  *Pump Alerts:*    `{stats['pumps']}`\n"
        f"📊  *Vol Alerts:*     `{stats['vol_alerts']}`\n"
        f"🎯  *Tracked:*        `{len(tracked_tokens)}`\n"
        f"⭐  *Watchlist:*      `{len(watchlist)}`\n\n"
        f"━━━ ⚙️ FILTERS ━━━━━━━━━━━━━\n\n"
        f"💧  Min Liquidity:  `{fmt_usd(MIN_LIQUIDITY)}`\n"
        f"📊  Min Volume:     `{fmt_usd(MIN_VOLUME)}`\n"
        f"⏰  Scan Interval:  `{SCAN_INTERVAL}s`\n"
        f"▶️   Status: `{'⏸ PAUSED' if paused else '✅ RUNNING'}`\n\n"
        f"💡  *Sources:* Bags.fm · Birdeye · DexScreener",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Open Bags.fm", url=REF_LINK)]])
    await update.message.reply_text(
        "╔═══════════════════════════╗\n"
        "║  💸  TRADE ON BAGS.FM     ║\n"
        "╚═══════════════════════════╝\n\n"
        "🛒  Trade Solana tokens via our referral!\n\n"
        "✅  Fast Solana-native swaps\n"
        "✅  Low fees\n"
        "✅  Supports @underdeathsol channel\n\n"
        f"🔗  [Open Bags.fm Now]({REF_LINK})",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True
    )

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global paused; paused = True
    await update.message.reply_text("⏸  Scanner *PAUSED*. Use /resume to restart.", parse_mode=ParseMode.MARKDOWN)

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global paused; paused = False
    await update.message.reply_text("▶️  Scanner *RESUMED*! 🔍 Scanning now...", parse_mode=ParseMode.MARKDOWN)

async def cmd_setfilter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global MIN_LIQUIDITY, MIN_VOLUME
    usage = "⚙️  *Usage:* `/setfilter <min_liq> <min_vol>`\n_ex: /setfilter 5000 10000_"
    if len(ctx.args) < 2:
        await update.message.reply_text(usage, parse_mode=ParseMode.MARKDOWN); return
    try:
        MIN_LIQUIDITY = float(ctx.args[0])
        MIN_VOLUME    = float(ctx.args[1])
        await update.message.reply_text(
            f"✅  *Filters Updated!*\n\n"
            f"💧  Min Liquidity: `{fmt_usd(MIN_LIQUIDITY)}`\n"
            f"📊  Min Volume:    `{fmt_usd(MIN_VOLUME)}`",
            parse_mode=ParseMode.MARKDOWN)
    except:
        await update.message.reply_text(usage, parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════════
#  CALLBACK HANDLER
# ══════════════════════════════════════════════════════════════════════════════
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    async def reply(text, kb=None):
        try:
            await q.message.reply_text(
                text, parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb, disable_web_page_preview=True)
        except: pass

    if data in ("menu", "start"):
        await cmd_start.__wrapped__(update, ctx) if hasattr(cmd_start,"__wrapped__") else await reply("Use /start")

    elif data == "help":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💸 Trade on Bags.fm", url=REF_LINK)]])
        await reply(HELP_TEXT, kb)

    elif data == "new":
        async with aiohttp.ClientSession() as s:
            tokens = await fetch_new_tokens(s)
        tokens = [t for t in tokens if t.get("address")][:5]
        if not tokens:
            await reply("❌ No new tokens right now. Try again soon.")
            return
        lines = ["🆕 *LATEST NEW TOKENS*\n"]
        for t in tokens:
            addr  = t.get("address","")
            name  = (t.get("name") or "?")[:16]
            sym   = (t.get("symbol") or "?").upper()
            price = sg(t,"priceUsd",default=0)
            ch24  = sg(t,"priceChange","h24",default=0)
            src   = t.get("_source","dexscreener")
            lines.append(f"• *{name}* `${sym}` — `{fmt_price(price)}` {fmt_chg(ch24)}\n  [💸 Buy]({_buy_url(addr, src)})")
        await reply("\n".join(lines))

    elif data == "trending":
        async with aiohttp.ClientSession() as s:
            tokens = await fetch_trending_tokens(s)
        if not tokens:
            await reply("❌ No trending data right now.")
            return
        lines = ["🔥 *TRENDING NOW*\n"]
        for i,t in enumerate(tokens[:5],1):
            addr  = t.get("address","")
            name  = (t.get("name") or "?")[:16]
            sym   = (t.get("symbol") or "?").upper()
            ch24  = sg(t,"priceChange","h24",default=0)
            vol   = sg(t,"volume","h24",default=0)
            src   = t.get("_source","dexscreener")
            lines.append(f"{i}. *{name}* `${sym}` {fmt_chg(ch24)}\n  📊 `{fmt_usd(vol)}` [💸 Buy]({_buy_url(addr, src)})")
        await reply("\n".join(lines))

    elif data in ("lb","lb_refresh"):
        async with aiohttp.ClientSession() as s:
            tokens = await fetch_gainers(s)
        text = build_leaderboard_msg(tokens)
        kb   = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="lb_refresh")],
            [InlineKeyboardButton("💸 Trade", url=REF_LINK)],
        ])
        await reply(text, kb)

    elif data == "stats":
        up = int(time.time()-stats["start"]); h,rem=divmod(up,3600); m,_=divmod(rem,60)
        await reply(
            f"📈 *STATS*\n\n⏱ `{h}h {m}m`\n"
            f"🔍 Scanned: `{stats['scanned']}`\n"
            f"📢 Alerts: `{stats['alerts']}`\n"
            f"🚀 Pumps: `{stats['pumps']}`\n"
            f"🎯 Tracked: `{len(tracked_tokens)}`"
        )

    elif data == "wl_view":
        if not watchlist:
            await q.answer("⭐ Watchlist empty. Tap ⭐ on alerts!", show_alert=True)
        else:
            lines = ["⭐ *WATCHLIST*\n"]
            for addr, info in watchlist.items():
                lines.append(f"• *{info['name']}* `${info['symbol']}` — target `{fmt_price(info.get('target_price',0))}`")
            await reply("\n".join(lines))

    elif data.startswith("ca_"):
        addr = data[3:]
        await q.answer(f"📋 CA: {addr}", show_alert=True)

    elif data.startswith("wl_"):
        parts = data.split("_",3)
        addr  = parts[1] if len(parts)>1 else ""
        sym   = parts[2] if len(parts)>2 else "???"
        name  = parts[3] if len(parts)>3 else "Unknown"
        if addr:
            watchlist[addr] = {"name": name, "symbol": sym, "target_price": 0}
            await q.answer(f"⭐ {sym} added to watchlist!", show_alert=True)
        else:
            await q.answer("❌ Failed.", show_alert=True)


# ══════════════════════════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════════════════════════
async def post_init(app: Application):
    bot = app.bot
    text = (
        "╔═══════════════════════════╗\n"
        "║  🤖  BAGS.FM SCANNER v3.0 ║\n"
        "║       ONLINE  ✅          ║\n"
        "╚═══════════════════════════╝\n\n"
        f"📡  Scanning every `{SCAN_INTERVAL}s`\n"
        f"💧  Min Liquidity: `{fmt_usd(MIN_LIQUIDITY)}`\n"
        f"📊  Min Volume:    `{fmt_usd(MIN_VOLUME)}`\n"
        f"🏆  Leaderboard:   every `{LEADERBOARD_INTERVAL//60}min`\n"
        f"🚀  Pump Alert:    +30% from entry\n"
        f"📊  Vol Spike:     3x baseline\n"
        f"📈  Mini Chart:    6h sparkline\n\n"
        f"💡  *Sources:* Bags.fm · Birdeye · DexScreener\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 [@underdeathsol](https://t.me/underdeathsol)  ·  "
        f"🐦 [@lilfid12](https://x.com/lilfid12)\n"
        f"🛒 [Trade on Bags.fm]({REF_LINK})"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💸 Trade on Bags.fm", url=REF_LINK)]])
    try:
        await send_ch(bot, text, kb)
        log.info("✅ Startup message sent")
    except Exception as e:
        log.error(f"Startup msg failed: {e}")

    asyncio.create_task(scanner_loop(bot))
    asyncio.create_task(leaderboard_loop(bot))
    asyncio.create_task(watchlist_loop(bot))


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    if not BOT_TOKEN:
        log.error("❌ BOT_TOKEN not set in .env!"); return
    if not BAGS_API_KEY:
        log.warning("⚠️ BAGS_API_KEY missing — using DexScreener + Birdeye as primary")

    log.info("🚀 Starting Bags.fm Scanner Bot v3.0...")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    for cmd, fn in [
        ("start",       cmd_start),
        ("help",        cmd_help),
        ("new",         cmd_new),
        ("trending",    cmd_trending),
        ("scan",        cmd_scan),
        ("leaderboard", cmd_leaderboard),
        ("watchlist",   cmd_watchlist),
        ("stats",       cmd_stats),
        ("buy",         cmd_buy),
        ("pause",       cmd_pause),
        ("resume",      cmd_resume),
        ("setfilter",   cmd_setfilter),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_handler(CallbackQueryHandler(on_callback))
    log.info("✅ All handlers registered. Bot is live!")
    app.run_polling(allowed_updates=["message","callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()

