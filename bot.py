"""
bot.py — Crypto Pump/Dump Telegram Bot
────────────────────────────────────────
Single file. Does everything:
  • Scans top 100 Binance Futures pairs every SCAN_INTERVAL minutes
  • Scores each pair across 6 signals (RSI, funding, OI, L/S, price chg)
  • Auto-posts top pumps & dumps to your Telegram chat
  • Responds to interactive commands:
        /pump        → top 5 pump candidates right now
        /dump        → top 5 dump candidates right now
        /check <SYM> → full breakdown for one pair (e.g. /check BTCUSDT)
        /scan        → force a fresh scan immediately
        /help        → shows all commands

Setup:
  1. pip install python-binance python-telegram-bot requests
  2. Create a bot via @BotFather on Telegram, grab the TOKEN
  3. Get your CHAT_ID (send a message to the bot, then use
     https://api.telegram.org/bot<TOKEN>/getUpdates to find it)
  4. Set env vars or edit the CONFIG block below
  5. python bot.py
"""

import os, json, time, logging, asyncio
from datetime import datetime, timezone
from typing import Any

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ─────────────────────────────────────────────────────────────
# CONFIG  — edit these or set as env vars
# ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN",  "YOUR_TOKEN_HERE")
TELEGRAM_CHAT   = os.environ.get("TELEGRAM_CHAT_ID","YOUR_CHAT_ID_HERE")
SCAN_INTERVAL   = int(os.environ.get("SCAN_INTERVAL_MIN", "60"))   # minutes
BINANCE_BASE    = "https://fapi.binance.com"
TOP_N           = 100   # how many pairs to scan (by 24h volume)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# SHARED STATE  — latest scan results, updated in place
# ─────────────────────────────────────────────────────────────
latest_scan: dict[str, Any] = {}


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — BINANCE DATA FETCHERS
# ═══════════════════════════════════════════════════════════════

def get_top_symbols() -> list[str]:
    """Top N USDT perpetual symbols sorted by 24-h volume."""
    r = requests.get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=15)
    r.raise_for_status()
    tickers = r.json()
    usdt = [t for t in tickers if t["symbol"].endswith("USDT")]
    usdt.sort(key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)
    return [t["symbol"] for t in usdt[:TOP_N]]


def fetch_klines(symbol: str, interval: str, limit: int = 60) -> list[float]:
    """Close prices for one symbol + interval."""
    r = requests.get(
        f"{BINANCE_BASE}/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10,
    )
    r.raise_for_status()
    return [float(k[3]) for k in r.json()]


def fetch_funding_rate(symbol: str) -> float | None:
    r = requests.get(
        f"{BINANCE_BASE}/fapi/v1/premiumIndex",
        params={"symbol": symbol},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        data = data[0]
    try:
        return float(data["lastFundingRate"])
    except (KeyError, TypeError):
        return None


def fetch_oi_hist(symbol: str) -> list[dict]:
    """Last 2 hourly OI snapshots."""
    r = requests.get(
        f"{BINANCE_BASE}/futures/data/openInterestHist",
        params={"symbol": symbol, "period": "1h", "limit": 2},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def fetch_long_short_ratio(symbol: str) -> float | None:
    r = requests.get(
        f"{BINANCE_BASE}/futures/data/topLongShortRatio",
        params={"symbol": symbol, "period": "1h", "limit": 1},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if data:
        try:
            return float(data[0]["longShortRatio"])
        except (KeyError, TypeError):
            pass
    return None


def fetch_ticker(symbol: str) -> dict | None:
    r = requests.get(
        f"{BINANCE_BASE}/fapi/v1/ticker/24hr",
        params={"symbol": symbol},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — RSI CALCULATOR
# ═══════════════════════════════════════════════════════════════

def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(c, 0.0) for c in changes]
    losses = [abs(min(c, 0.0)) for c in changes]

    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period

    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - (100 / (1 + rs)), 2)


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — SCORING FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def score_rsi(rsi: float | None, weight: float) -> float:
    if rsi is None:
        return 0.0
    if rsi <= 30:
        return weight
    if rsi >= 70:
        return -weight
    if rsi < 50:
        return weight * (50 - rsi) / 20.0
    return -weight * (rsi - 50) / 20.0


def score_funding(fr: float | None, weight: float = 25) -> float:
    if fr is None:
        return 0.0
    threshold = 0.0005          # 0.05 %
    if fr <= -threshold:
        return weight
    if fr >= threshold:
        return -weight
    return -weight * (fr / threshold)


def score_oi_change(symbol: str, price_chg_pct: float, weight: float = 15) -> float:
    hist = fetch_oi_hist(symbol)
    if len(hist) < 2:
        return 0.0
    try:
        prev = float(hist[0]["sumOpenInterest"])
        now  = float(hist[1]["sumOpenInterest"])
    except (KeyError, TypeError):
        return 0.0
    if prev == 0:
        return 0.0
    oi_chg = (now - prev) / prev

    if oi_chg > 0.03 and price_chg_pct > 1:
        return weight
    if oi_chg > 0.03 and price_chg_pct < -1:
        return -weight
    if oi_chg < -0.03:
        return -weight * 0.4 if price_chg_pct > 0 else weight * 0.4
    return 0.0


def score_long_short(ratio: float | None, weight: float = 10) -> float:
    if ratio is None:
        return 0.0
    if ratio <= 0.8:
        return weight
    if ratio >= 1.5:
        return -weight
    mid = 1.15
    if ratio < mid:
        return weight * (mid - ratio) / (mid - 0.8)
    return -weight * (ratio - mid) / (1.5 - mid)


def score_price_change(pct: float, weight: float = 10) -> float:
    if -5 <= pct <= 0:
        return weight * 0.5
    if 5 <= pct <= 10:
        return -weight * 0.5
    if pct < -5:
        return weight
    if pct > 10:
        return -weight
    return 0.0


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — SCAN ONE PAIR
# ═══════════════════════════════════════════════════════════════

def scan_pair(symbol: str) -> dict[str, Any]:
    ticker         = fetch_ticker(symbol)
    price_chg_pct  = float(ticker.get("priceChangePercent", 0)) if ticker else 0.0

    closes_1h      = fetch_klines(symbol, "1h", 60)
    closes_4h      = fetch_klines(symbol, "4h", 60)
    rsi_1h         = calc_rsi(closes_1h)
    rsi_4h         = calc_rsi(closes_4h)
    fr             = fetch_funding_rate(symbol)
    ls_ratio       = fetch_long_short_ratio(symbol)

    s_rsi1  = score_rsi(rsi_1h, 25)
    s_rsi4  = score_rsi(rsi_4h, 15)
    s_fr    = score_funding(fr, 25)
    s_oi    = score_oi_change(symbol, price_chg_pct, 15)
    s_ls    = score_long_short(ls_ratio, 10)
    s_pc    = score_price_change(price_chg_pct, 10)
    total   = round(s_rsi1 + s_rsi4 + s_fr + s_oi + s_ls + s_pc, 2)

    signal = "PUMP" if total >= 35 else ("DUMP" if total <= -35 else "NEUTRAL")

    return {
        "symbol":            symbol,
        "score":             total,
        "signal":            signal,
        "rsi_1h":            rsi_1h,
        "rsi_4h":            rsi_4h,
        "funding_rate_pct":  round(fr * 100, 4) if fr else None,
        "long_short_ratio":  ls_ratio,
        "price_change_24h":  price_chg_pct,
        "last_price":        ticker.get("lastPrice") if ticker else None,
        "breakdown": {
            "rsi_1h":     round(s_rsi1, 2),
            "rsi_4h":     round(s_rsi4, 2),
            "funding":    round(s_fr, 2),
            "oi_change":  round(s_oi, 2),
            "long_short": round(s_ls, 2),
            "price_chg":  round(s_pc, 2),
        },
    }


# ═══════════════════════════════════════════════════════════════
# SECTION 5 — FULL MARKET SCAN
# ═══════════════════════════════════════════════════════════════

def run_scan() -> dict[str, Any]:
    global latest_scan
    log.info("Starting scan …")
    symbols = get_top_symbols()
    log.info("Scanning %d pairs …", len(symbols))

    results = []
    for sym in symbols:
        try:
            results.append(scan_pair(sym))
        except Exception as e:
            log.warning("Skip %s — %s", sym, e)

    results.sort(key=lambda r: r["score"], reverse=True)

    pumps = [r for r in results if r["signal"] == "PUMP"]
    dumps = [r for r in results if r["signal"] == "DUMP"]

    latest_scan = {
        "scan_time":     datetime.now(timezone.utc).isoformat(),
        "pairs_scanned": len(results),
        "pumps":         pumps,
        "dumps":         dumps,
        "all":           results,
    }
    log.info("Done. Pumps: %d | Dumps: %d | Neutral: %d",
             len(pumps), len(dumps), len(results) - len(pumps) - len(dumps))
    return latest_scan


# ═══════════════════════════════════════════════════════════════
# SECTION 6 — MESSAGE FORMATTERS
# ═══════════════════════════════════════════════════════════════

def confidence(record: dict) -> str:
    bd = record["breakdown"]
    strong = sum(1 for v in bd.values() if abs(v) >= 8)
    if strong >= 3:
        return "🔥 High"
    if strong >= 2:
        return "⚡ Medium"
    return "📍 Low"


def fmt_pump_list(pairs: list[dict], limit: int = 5) -> str:
    if not pairs:
        return "⚪ No pump candidates right now."
    lines = []
    for p in pairs[:limit]:
        lines.append(
            f"🟢 *{p['symbol']}*  |  Score: {p['score']}  |  Confidence: {confidence(p)}\n"
            f"    RSI 1h: {p['rsi_1h']} · FR: {p['funding_rate_pct']}% · L/S: {p['long_short_ratio']} · 24h: {p['price_change_24h']}%"
        )
    return "\n".join(lines)


def fmt_dump_list(pairs: list[dict], limit: int = 5) -> str:
    if not pairs:
        return "⚪ No dump candidates right now."
    lines = []
    for p in pairs[:limit]:
        lines.append(
            f"🔴 *{p['symbol']}*  |  Score: {p['score']}  |  Confidence: {confidence(p)}\n"
            f"    RSI 1h: {p['rsi_1h']} · FR: {p['funding_rate_pct']}% · L/S: {p['long_short_ratio']} · 24h: {p['price_change_24h']}%"
        )
    return "\n".join(lines)


def fmt_detail(record: dict) -> str:
    bd = record["breakdown"]
    sig_emoji = "🟢 PUMP" if record["signal"] == "PUMP" else ("🔴 DUMP" if record["signal"] == "DUMP" else "⚪ NEUTRAL")
    return (
        f"📊 *{record['symbol']} — Full Breakdown*\n\n"
        f"Signal: {sig_emoji}\n"
        f"Total Score: {record['score']} / 100  |  Confidence: {confidence(record)}\n\n"
        f"━━━ Signals ━━━\n"
        f"RSI 1h:          {record['rsi_1h']}       →  {bd['rsi_1h']:+}\n"
        f"RSI 4h:          {record['rsi_4h']}       →  {bd['rsi_4h']:+}\n"
        f"Funding Rate:    {record['funding_rate_pct']}%   →  {bd['funding']:+}\n"
        f"OI Change:       —           →  {bd['oi_change']:+}\n"
        f"Long/Short:      {record['long_short_ratio']}       →  {bd['long_short']:+}\n"
        f"24h Price Chg:   {record['price_change_24h']}%  →  {bd['price_chg']:+}\n\n"
        f"Last Price: ${record['last_price']}\n"
        f"⚠️ Technical signals only. Use stop-losses."
    )


def fmt_alert(scan: dict) -> str:
    t      = scan["scan_time"][:16]
    pumps  = scan["pumps"][:3]
    dumps  = scan["dumps"][:3]

    msg = f"📊 *Crypto Scan — {t} UTC*\nPairs scanned: {scan['pairs_scanned']}\n\n"

    if pumps:
        msg += "🟢 *Top Pumps*\n"
        for p in pumps:
            msg += f"  • *{p['symbol']}*  Score: {p['score']}  |  RSI: {p['rsi_1h']}  |  FR: {p['funding_rate_pct']}%\n"

    if dumps:
        msg += "\n🔴 *Top Dumps*\n"
        for d in dumps:
            msg += f"  • *{d['symbol']}*  Score: {d['score']}  |  RSI: {d['rsi_1h']}  |  FR: {d['funding_rate_pct']}%\n"

    if not pumps and not dumps:
        msg += "⚪ Market is neutral — no strong pump or dump signals.\n"

    msg += "\n⚠️ Technical signals only. Use stop-losses."
    return msg


# ═══════════════════════════════════════════════════════════════
# SECTION 7 — TELEGRAM COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Reply with the user's chat ID so they can configure the bot."""
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"✅ *Bot Connected*\n\n"
        f"Your Chat ID is: `{chat_id}`\n\n"
        f"Copy this number and set it as the `TELEGRAM_CHAT_ID` environment variable "
        f"in your Railway/Render deployment.\n\n"
        f"Once configured, the bot will auto-post scan alerts to this chat every {SCAN_INTERVAL} minutes.\n\n"
        f"Commands:\n"
        f"/pump  /dump  /check <SYM>  /scan  /help",
        parse_mode="Markdown",
    )
    log.info("Chat ID requested: %s", chat_id)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Crypto Pump/Dump Bot*\n\n"
        "/start         → Get your Chat ID\n"
        "/pump          → Top 5 pump candidates\n"
        "/dump          → Top 5 dump candidates\n"
        "/check <SYM>   → Full breakdown (e.g. /check BTCUSDT)\n"
        "/scan          → Force a fresh scan now\n"
        "/help          → This menu",
        parse_mode="Markdown",
    )


async def cmd_pump(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not latest_scan:
        await update.message.reply_text("⏳ No scan data yet. Running first scan …")
        run_scan()
    await update.message.reply_text(fmt_pump_list(latest_scan.get("pumps", [])), parse_mode="Markdown")


async def cmd_dump(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not latest_scan:
        await update.message.reply_text("⏳ No scan data yet. Running first scan …")
        run_scan()
    await update.message.reply_text(fmt_dump_list(latest_scan.get("dumps", [])), parse_mode="Markdown")


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /check <SYMBOL>  e.g. /check BTCUSDT")
        return

    symbol = args[0].upper().strip()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    # Try cache first, fall back to live fetch
    record = None
    if latest_scan:
        record = next((r for r in latest_scan.get("all", []) if r["symbol"] == symbol), None)

    if not record:
        await update.message.reply_text(f"🔄 Fetching live data for *{symbol}* …", parse_mode="Markdown")
        try:
            record = scan_pair(symbol)
        except Exception as e:
            await update.message.reply_text(f"❌ Error fetching {symbol}: {e}")
            return

    await update.message.reply_text(fmt_detail(record), parse_mode="Markdown")


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Running fresh scan … (takes ~1-2 min)")
    scan = run_scan()
    await update.message.reply_text(fmt_alert(scan), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════
# SECTION 8 — AUTO-ALERT LOOP + BOT STARTUP
# ═══════════════════════════════════════════════════════════════

async def auto_scan_loop(app):
    """Background task: scans every SCAN_INTERVAL and posts alert."""
    scan_count = 0
    max_scans = int(os.environ.get("MAX_SCANS", "0"))  # 0 = infinite
    
    while True:
        scan = run_scan()
        try:
            await app.bot.send_message(
                chat_id=TELEGRAM_CHAT,
                text=fmt_alert(scan),
                parse_mode="Markdown",
            )
            log.info("Alert posted to Telegram.")
        except Exception as e:
            log.error("Failed to send Telegram alert: %s", e)

        scan_count += 1
        if max_scans > 0 and scan_count >= max_scans:
            log.info("Reached max scans (%d), shutting down.", max_scans)
            break

        log.info("Next scan in %d minutes …", SCAN_INTERVAL)
        await asyncio.sleep(SCAN_INTERVAL * 60)


def main():
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # Register commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("pump",  cmd_pump))
    app.add_handler(CommandHandler("dump",  cmd_dump))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("scan",  cmd_scan))

    # Check if running in "one-shot" mode (for GitHub Actions)
    if os.environ.get("RUN_MODE") == "once":
        log.info("Running in one-shot mode (GitHub Actions)")
        scan = run_scan()
        asyncio.get_event_loop().run_until_complete(
            app.bot.send_message(
                chat_id=TELEGRAM_CHAT,
                text=fmt_alert(scan),
                parse_mode="Markdown",
            )
        )
        log.info("One-shot complete. Exiting.")
    else:
        # Normal mode: continuous polling with background scans
        asyncio.get_event_loop().create_task(auto_scan_loop(app))
        log.info("Bot starting. First auto-scan will begin immediately.")
        app.run_polling()


if __name__ == "__main__":
    main()
