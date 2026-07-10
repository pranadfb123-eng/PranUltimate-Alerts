"""
PranUltimate Intraday Bot
=========================
Real-time scanner + automated trader for 5min, 15min, 30min timeframes.
Runs during market hours: 9:00 AM - 3:20 PM IST (Mon-Fri).

Paper trading mode: logs all decisions, no real orders placed.
Live trading mode:  places real orders via Dhan API.

Usage:
    py -3.13 bot.py

Task Scheduler: Run at 9:00 AM on weekdays.
"""

import json
import os
import sys
import time
import logging
import requests
import pandas as pd
from datetime import datetime, timedelta, time as dtime
from concurrent.futures import ThreadPoolExecutor
import threading
from dhan_data import DhanData, fetch_hist_dhan
from chartink import fetch_chartink_candidates, save_watchlist, load_watchlist

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH   = os.path.join(BASE_DIR, "..", "intraday_config.json")
TRADES_PATH   = os.path.join(BASE_DIR, "trades.json")
ACTIVE_WATCHLIST_PATH = os.path.join(BASE_DIR, "active_watchlist_state.json")
WATCHLIST_STATE_PATH  = os.path.join(BASE_DIR, "watchlist_state.json")
LOG_PATH      = os.path.join(BASE_DIR, f"bot_{datetime.now().strftime('%Y-%m-%d')}.log")
# CHANGED 2026-06-25: was a single ever-growing "bot.log" that mixed every
# day's runs together in one file (caused real confusion/bugs when
# analyzing "today's" activity -- date-filtering after the fact is
# unreliable because multi-line log entries don't all start with a
# timestamp). Now one fresh file per calendar day -- e.g.
# bot_2026-06-25.log -- so "today's log" is just "today's file," no
# filtering needed.
WATCHLIST_PATH   = os.path.join(BASE_DIR, "..", "server", "watchlist.json")
ACTION_LOG_PATH  = os.path.join(BASE_DIR, "..", "logs", "action_log.jsonl")
RESULTS_PATH     = os.path.join(BASE_DIR, "..", "server", "results.json")

# Module-level dict populated at startup from results.json.
# Maps symbol → timeframe for every stock in the nightly 1H+ scanner universe.
# Chartink-sourced candidates that appear here are blocked from lower-TF entry.
_scanner_1h_universe: dict = {}


def _log_action(symbol: str, tf: str, action: str, detail: dict = None):
    """Append one structured line to logs/action_log.jsonl.

    Called at every rejection / exit / upgrade point introduced by the recent
    bot fixes so there's a queryable audit trail beyond the plain text log:
      SKIP_CEILING_BELOW_EMA200  — Fix #1: ceiling < EMA200, no valid higher TF
      TF_UPGRADE_CEILING         — Fix #1 revised: upgraded to higher TF
      SKIP_15MIN_EMA             — Fix #4: price below 15min 200 EMA
      SKIP_TF_EMA                — Fix #2: price below own-TF 200 EMA (30/45min)
      TF_UPGRADE_CHARTINK        — Fix #3: Chartink higher-TF hit → upgraded
      EXIT_8EMA_CLOSE            — Fix #6: candle closed below 8 EMA → market exit
      ENTRY_LOCKED_BOX           — locked-box breakout entry taken
      ENTRY_FRESH                — fresh-detection breakout entry taken
    """
    os.makedirs(os.path.dirname(ACTION_LOG_PATH), exist_ok=True)
    entry = {
        "ts":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "tf":     tf,
        "action": action,
        **(detail or {}),
    }
    try:
        with open(ACTION_LOG_PATH, "a", encoding="utf-8") as _f:
            _f.write(json.dumps(entry) + "\n")
    except Exception as _e:
        log.warning(f"_log_action: could not write to action_log.jsonl — {_e}")


def _send_bot_alert(message: str) -> None:
    """Send a Telegram message to the main bot channel.
    Reads credentials from alert_secrets.env at runtime (never hardcoded).
    Best-effort — failures are logged, never raised.
    """
    _env_path = os.path.join(BASE_DIR, "..", "alert_secrets.env")
    _token, _chat_id = None, None
    try:
        with open(_env_path) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line.startswith("#") or "=" not in _line:
                    continue
                _k, _v = _line.split("=", 1)
                if _k.strip() == "TELEGRAM_BOT_TOKEN":
                    _token = _v.strip()
                elif _k.strip() == "TELEGRAM_CHAT_ID":
                    _chat_id = _v.strip()
    except Exception as _e:
        log.warning(f"_send_bot_alert: could not read alert_secrets.env — {_e}")
        return
    if not _token or not _chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_token}/sendMessage",
            data={"chat_id": _chat_id, "text": message, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as _e:
        log.warning(f"_send_bot_alert: Telegram send failed — {_e}")


# ── Logging ────────────────────────────────────────────────────────────────────
# Windows consoles often default to the legacy cp1252 codepage, which can't
# represent ★, ⊘, or even the ₹ rupee sign used throughout this file's price
# logging — without this, every such line raises a UnicodeEncodeError. Today's
# session happened to run in a console where this wasn't an issue, but that's
# luck of whatever codepage was active, not something to rely on during live
# trading — confirmed broken on this same machine when running the scanner.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── Timeframe config ───────────────────────────────────────────────────────────
INTRADAY_TFS = ["5min", "15min", "30min"]

CANDLE_COUNTS = {
    "5min":  300,
    "15min": 250,
    "30min": 220,
}

# ── Stock Universe (tiered by timeframe) ───────────────────────────────────────
# 5min: Top ~150 most liquid NSE stocks (real intraday 5min setups happen here)
# 15min: Expands to ~250
# 30min: Expands to ~350
# Supplemented at runtime by nightly watchlist.json

UNIVERSE_5MIN = [
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","SBIN","BHARTIARTL",
    "ITC","KOTAKBANK","LT","HCLTECH","AXISBANK","ASIANPAINT","MARUTI",
    "SUNPHARMA","TITAN","BAJFINANCE","WIPRO","NESTLEIND","POWERGRID",
    "NTPC","TECHM","ONGC","JSWSTEEL","TATAMOTORS","COALINDIA","BRITANNIA",
    "DRREDDY","HINDALCO","CIPLA","BPCL","GRASIM","DIVISLAB","BAJAJFINSV",
    "EICHERMOT","TATACONSUM","INDUSINDBK","HEROMOTOCO","APOLLOHOSP",
    "SBILIFE","HDFCLIFE","M&M","ADANIPORTS","TATASTEEL","BAJAJ-AUTO",
    "PIDILITIND","SIEMENS","HAVELLS","DABUR","BERGEPAINT","MARICO",
    "COLPAL","TORNTPHARM","MUTHOOTFIN","GODREJCP","CHOLAFIN","BOSCHLTD",
    "MPHASIS","LTIM","PERSISTENT","COFORGE","OFSS","KPITTECH","TATAELXSI",
    "POLYCAB","DIXON","CANBK","PNB","BANKBARODA","FEDERALBNK","IDFCFIRSTB",
    "AUBANK","ZOMATO","IRFC","PFC","RECLTD","NHPC","TATAPOWER","SUZLON",
    "RVNL","IRCON","NBCC","APLAPOLLO","JSWINFRA","IPCALAB","LUPIN",
    "AUROPHARMA","GLENMARK","BIOCON","LAURUSLABS","DLF","GODREJPROP",
    "PRESTIGE","BRIGADE","OBEROIRLTY","PHOENIXLTD","ENDURANCE","CUMMINSIND",
    "THERMAX","BHEL","ABB","DEEPAKNTR","AARTIIND","SRF","TATACHEM",
    "COROMANDEL","PI","TRENT","ABFRL","METROPOLIS","DRLAL","MAXHEALTH",
    "FORTIS","CONCOR","BLUEDART","HAPPSTMNDS","AFFLE","TANLA","INDIAMART",
    "MGL","IGL","VARUNBEV","RADICO","EMAMILTD","KALYANKJIL","HSCL",
    "ADANIGREEN","ADANIPOWER","SJVN","CESC","TORNTPOWER","IREDA","HUDCO",
    "INOXWIND","NCC","KEC","LICI","THANGAMAYL","SENCO","RAJESHEXPO",
    "TRIDENT","NAZARA","SPARC","STRIDES","ASTER","GATI","VRL","DELHIVERY",
]

UNIVERSE_15MIN = list(dict.fromkeys(UNIVERSE_5MIN + [
    "HINDUNILVR","ADANIENT","UPL","SHREECEM","PAGEIND","MOTHERSON",
    "BAJAJHLDNG","MINDTREE","ZENSAR","CYIENT","GMRINFRA","IRB","KALPATPOWR",
    "SANOFI","ABBOTINDIA","PFIZER","GLAXO","PGHH","VMART","SHOPERSTOP",
    "ARVIND","NIIT","SAREGAMA","NETWORK18","TIPS","PVRINOX","INOXLEISUR",
    "AJANTPHARM","NATCOPHARM","GRANULES","ALKEM","SYNGENE","SUVEN",
    "SOBHA","MAHINDCIE","SCHAEFFLER","TIMKEN","SKFINDIA","ABB","HBLPOWER",
    "NAVINFLUOR","ALKYLAMINE","FINEORG","VINATIORG","ATUL","GNFC",
    "CHAMBLFERT","RBLBANK","BANDHANBNK","YESBANK","INDIANB","MAHABANK",
    "WOCKPHARMA","MEDANTA","SNOWMAN","MAPMYINDIA","CARTRADE","NYKAA",
    "PAYTM","POLICYBZR","RATEGAIN","ROUTE","GSPL","GUJGASLTD","ATGL",
    "PCBL","NOCIL","GALLANTT","SHYAMMETL","PRAJIND","KALYANKJIL",
]))

UNIVERSE_30MIN = list(dict.fromkeys(UNIVERSE_15MIN + [
    "ADANITRANS","AEGASIND","UNIONB","BANDHANBNK","KRSNAA","VIJAYADIAG",
    "WELSPUNIND","VARDHMAN","KPRMILL","RAYMOND","CAREER","APTECH",
    "HERITGFOOD","HATSUN","BAJAJCON","JYOTHYLAB","EMAMILTD","PGHH",
    "TDPOWERSYS","VOLTAMP","IGPL","NOCIL","SEQUENT","SOLARA","SPARC",
    "ASHOKA","SADBHAV","PNCINFRA","NCC","GMMPFAUDLR","TILINDLTD",
]))

UNIVERSE = {
    "5min":  UNIVERSE_5MIN,
    "15min": UNIVERSE_15MIN,
    "30min": UNIVERSE_30MIN,
}

# ── Dhan API Client ────────────────────────────────────────────────────────────
class DhanClient:
    BASE_URL = "https://api.dhan.co/v2"

    def __init__(self, client_id, access_token, paper_trading=True):
        self.client_id    = client_id
        self.access_token = access_token
        self.paper_trading = paper_trading
        self.headers = {
            "access-token":  access_token,
            "client-id":     client_id,
            "Content-Type":  "application/json",
        }
        self._sec_map = {}
        if not paper_trading:
            self._load_security_master()

    def _load_security_master(self):
        """Download Dhan scrip master → symbol → securityId map"""
        try:
            log.info("Loading Dhan security master...")
            url = "https://images.dhan.co/api-data/api-scrip-master.csv"
            df  = pd.read_csv(url)
            nse = df[(df["SEM_EXM_EXCH_ID"] == "NSE") & (df["SEM_SEGMENT"] == "E")]
            self._sec_map = dict(zip(
                nse["SEM_TRADING_SYMBOL"],
                nse["SEM_SMST_SECURITY_ID"].astype(str)
            ))
            log.info(f"Security master: {len(self._sec_map)} NSE symbols loaded")
        except Exception as e:
            log.error(f"Security master load failed: {e}")

    def _sec_id(self, symbol):
        return self._sec_map.get(symbol)

    def place_order(self, symbol, quantity, transaction_type,
                    order_type="MARKET", price=0.0, trigger_price=0.0):
        """Place order (paper or live)"""
        if quantity <= 0:
            return None

        if self.paper_trading:
            log.info(f"    [PAPER] {transaction_type} {quantity}×{symbol} "
                     f"| type={order_type} price=₹{price} trigger=₹{trigger_price}")
            return {
                "orderId": f"PAPER_{symbol}_{datetime.now().strftime('%H%M%S%f')[:13]}",
                "orderStatus": "PAPER_FILLED",
            }

        sec_id = self._sec_id(symbol)
        if not sec_id:
            log.error(f"No security ID found for {symbol} — skipping order")
            return None

        payload = {
            "dhanClientId":    self.client_id,
            "transactionType": transaction_type,   # BUY / SELL
            "exchangeSegment": "NSE_EQ",
            "productType":     "INTRADAY",
            "orderType":       order_type,          # MARKET / SL-M
            "validity":        "DAY",
            "securityId":      sec_id,
            "tradingSymbol":   symbol,
            "quantity":        quantity,
            "price":           price,
            "triggerPrice":    trigger_price,
        }
        try:
            r = requests.post(f"{self.BASE_URL}/orders",
                              headers=self.headers, json=payload, timeout=10)
            r.raise_for_status()
            resp = r.json()
            log.info(f"    ORDER: {transaction_type} {quantity}×{symbol} → {resp.get('orderId')}")
            return resp
        except Exception as e:
            log.error(f"Order error {symbol}: {e}")
            return None

    def cancel_order(self, order_id):
        if self.paper_trading:
            return
        try:
            requests.delete(f"{self.BASE_URL}/orders/{order_id}",
                            headers=self.headers, timeout=10)
        except Exception:
            pass

# ── ETF / Index-fund guard ────────────────────────────────────────────────────
def is_etf(sym: str) -> bool:
    """Return True if the symbol is an ETF or index fund (not tradeable intraday)."""
    s = sym.upper()
    return "ETF" in s or s.endswith("BEES") or "IETF" in s


# ── Indicator Calculations ─────────────────────────────────────────────────────
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def rsi(series, period=14):
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs       = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def fetch_hist(dhan_data, symbol, timeframe, retries=2, pause=1.0):
    """Robust Dhan data fetch with retries. Returns DataFrame or None."""
    return fetch_hist_dhan(dhan_data, symbol, timeframe, retries=retries, pause=pause)


def add_indicators(df):
    df = df.copy()
    df["ema8"]    = ema(df["close"], 8)
    df["ema20"]   = ema(df["close"], 20)
    df["ema50"]   = ema(df["close"], 50)
    df["ema100"]  = ema(df["close"], 100)
    df["ema200"]  = ema(df["close"], 200)
    df["rsi14"]   = rsi(df["close"])
    df["vol_avg"] = df["volume"].rolling(20).mean()
    return df.dropna()

# ── PranUltimate Signal Detection (STATEFUL MODEL) ─────────────────────────────
#
# Correct model:
#   1. Stock was in an uptrend, then came DOWN to touch the 200 EMA (wick touch)
#   2. First Low forms = bottom of the U/V dip after the 200 EMA touch
#   3. Sideways range forms afterward (price may drift up, away from 200 EMA)
#   4. Resistance = top of that range (or a stronger zone 1-2% above if present)
#   5. Breakout = close above resistance + 20 EMA splitting up + RSI 45-70 + volume
#   6. First Low must NEVER be broken between the touch and the breakout
#   7. "Cleanliness" only ranks results — never rejects
#
# The 200 EMA's only job is the initial touch. After that it's irrelevant;
# only the First Low and the breakout matter.


def find_200ema_touch(df, search_window=400, symbol="", tf=""):
    """
    Walk backwards to find the most recent candle whose WICK touched the 200 EMA
    while the stock was coming DOWN from an uptrend.

    Returns the index of the touch candle, or None.

    A touch = candle low <= 200 EMA <= candle high (any part of candle crosses it)
    Plus: before the touch, price should have been ABOVE the 200 EMA (uptrend),
    confirming this is a correction down to the 200 EMA, not a downtrend crossing.
    """
    n = len(df)
    if n < 60:
        return None

    end = n - 2   # don't use the last candle (potential breakout candle)
    earliest = max(30, end - search_window)

    for i in range(end, earliest, -1):
        row    = df.iloc[i]
        ema200 = row["ema200"]
        if ema200 == 0:
            continue

        # Did this candle's range cross the 200 EMA?
        touched = (row["low"] <= ema200 <= row["high"])
        if not touched:
            continue

        # Consolidation filter: if the EMA is already inside the range of 3+ of
        # the 5 candles immediately before this one, the EMA has been sitting
        # inside the range for a while — this is a consolidation candle, not the
        # original touch from above. Skip and keep walking backward to find the
        # candle where price first descended to the EMA from an uptrend.
        consol_count = 0
        for j in range(max(0, i - 5), i):
            prow = df.iloc[j]
            if prow["low"] <= prow["ema200"] <= prow["high"]:
                consol_count += 1
        if consol_count >= 3:
            continue  # EMA inside range for >=3 of the 5 preceding candles -> skip

        # Confirm prior uptrend: in the ~20 candles before the touch,
        # price should have been predominantly ABOVE the 200 EMA
        lookback_start = max(0, i - 20)
        prior = df.iloc[lookback_start:i]
        if len(prior) < 5:
            continue
        above_count = (prior["close"] > prior["ema200"]).sum()
        if above_count < len(prior) * 0.6:   # at least 60% of prior candles above
            continue

        if symbol:
            _log_action(symbol, tf, "EMA_TOUCH_FOUND", {
                "touch_idx": int(i),
                "touch_date": str(df.index[i]),
                "ema200": float(df.iloc[i]["ema200"]),
                "close": float(df.iloc[i]["close"])
            })
        return i

    return None


def find_first_low(df, touch_idx):
    """
    The First Low = the lowest point of the U/V dip around the 200 EMA touch.
    Look in a window around the touch (a few candles before and after) for the
    actual swing low — the bottom of the dip.

    Returns (first_low_price, first_low_idx).
    """
    n = len(df)
    # Window: from touch to a handful of candles after (the bounce confirms the low)
    win_start = max(0, touch_idx - 3)
    win_end   = min(n - 1, touch_idx + 8)
    window    = df.iloc[win_start:win_end + 1]

    fl_idx   = window["low"].idxmin()
    fl_price = float(df.loc[fl_idx, "low"])
    # Convert label index to positional index
    fl_pos   = df.index.get_loc(fl_idx)
    return fl_price, fl_pos


def build_range_box(df, fl_pos):
    """
    Build the consolidation BOX:
      Floor   = First Low (at fl_pos)
      Ceiling = highest high reached AFTER the First Low, BEFORE any breakout

    Then find the breakout point: the FIRST candle after the First Low that
    CLOSES above the running ceiling.

    Returns dict:
      {
        "ceiling":        resistance price (top of box),
        "breakout_pos":   index of first candle that closed above ceiling,
                          or None if no breakout yet (still consolidating),
        "range_start":    fl_pos + 1,
      }
    or None if the range is too short / invalid.

    The ceiling is built incrementally so it reflects the box BEFORE breakout —
    not contaminated by the post-breakout run-up (which fixes the SPARC bug).
    """
    n = len(df)
    range_start = fl_pos + 1
    if range_start >= n - 1:
        return None

    # Walk forward from just after the First Low, tracking the running ceiling.
    # The box ceiling = highest high seen so far in the range.
    # Breakout = first candle whose CLOSE exceeds the ceiling established by
    # the candles before it.
    ceiling = float(df.iloc[range_start]["high"])
    breakout_pos = None

    for i in range(range_start + 1, n):
        row = df.iloc[i]
        # Did this candle CLOSE above the ceiling built from prior candles?
        if row["close"] > ceiling:
            breakout_pos = i
            break
        # Otherwise it's still inside the box — extend the ceiling
        if row["high"] > ceiling:
            ceiling = float(row["high"])

    # Require a minimum range width (at least a few candles of consolidation)
    if breakout_pos is not None:
        if breakout_pos - range_start < 3:
            return None
    else:
        if (n - 1) - range_start < 3:
            return None

    return {
        "ceiling":      ceiling,
        "breakout_pos": breakout_pos,
        "range_start":  range_start,
    }


def compute_cleanliness(df, touch_idx, fl_pos, range_start):
    """
    Cleanliness score (0-100) for RANKING ONLY — never rejects a setup.
    """
    score = 50.0
    pre = df.iloc[max(0, touch_idx - 30):touch_idx]
    if len(pre) > 1:
        above = pre["close"] > pre["ema200"]
        crossings = (above != above.shift()).sum()
        score += max(0, 20 - crossings * 3)
    last = df.iloc[-2]
    close = last["close"]
    if close > 0:
        spread = abs(last["ema20"] - last["ema100"]) / close
        score += max(0, 20 - spread * 1000)
    return round(min(100, max(0, score)), 1)


def get_swing_low(df, n=15):
    """First visible swing low below the last close (the entry candle)."""
    last  = df.iloc[-1]["close"]
    lows  = df.tail(n)["low"]
    below = lows[lows < last]
    if below.empty:
        return float(df.tail(n)["low"].min())
    return float(below.min())


def detect_signal(df, symbol, timeframe):
    """
    Stateful PranUltimate detection using the BOX model.

    Returns (signal_dict, reason_str).
      signal_dict is None  → stock was rejected; reason_str explains why.
      signal_dict is a dict → breakout confirmed; reason_str summarises it.

    Sequence: uptrend → 200 EMA wick touch → First Low (box floor) →
    range forms → box ceiling = highest high in range →
    BREAKOUT = the CURRENT candle closes above the ceiling for the FIRST time.

    Critically: if the breakout already happened on an EARLIER candle (price has
    already run away), this is NOT a fresh signal. This fixes the SPARC bug.
    """
    if len(df) < 220:
        return None, "not enough candles"

    df   = add_indicators(df)
    if len(df) < 60:
        return None, "not enough candles after EMA warmup"
    last = df.iloc[-1]
    prev = df.iloc[-2]

    # ── STAGE 1: 200 EMA wick touch (coming down from uptrend) ───────────────
    touch_idx = find_200ema_touch(df, symbol=symbol, tf=timeframe)
    if touch_idx is None:
        return None, "no 200 EMA touch — not in a correction phase"

    # ── STAGE 2: First Low (box floor) ──────────────────────────────────────
    first_low, fl_pos = find_first_low(df, touch_idx)

    # ── STAGE 3: Build the box and locate the breakout point ────────────────
    box = build_range_box(df, fl_pos)
    if box is None:
        return None, "consolidation too brief (<3 candles)"
    resistance   = box["ceiling"]
    breakout_pos = box["breakout_pos"]
    range_start  = box["range_start"]

    # ── KEY: breakout must be on the CURRENT (last) candle ──────────────────
    if breakout_pos is None:
        range_candles = (len(df) - 1) - range_start
        return None, (f"still consolidating — {range_candles} candles in box "
                      f"(floor ₹{round(first_low, 2)} / ceil ₹{round(resistance, 2)})")
    if breakout_pos != len(df) - 1:
        candles_ago = (len(df) - 1) - breakout_pos
        return None, f"stale breakout — broke out {candles_ago} candle(s) ago, already ran"

    # ── INVALIDATION: First Low must never have been broken in the box ──────
    after_fl = df.iloc[fl_pos + 1 : len(df) - 1]
    if len(after_fl) > 0 and (after_fl["close"] < first_low).any():
        return None, f"First Low ₹{round(first_low, 2)} was broken — setup invalidated"

    # ── STAGE 4: Breakout-candle confirmations ──────────────────────────────
    rsi_val      = round(float(last["rsi14"]), 1)
    vol_ratio    = round(float(last["volume"] / last["vol_avg"]), 1)
    ema200_val   = round(float(last["ema200"]), 2)

    if last["rsi14"] < 45:
        return None, f"RSI {rsi_val} below 45"
    if last["volume"] <= last["vol_avg"]:
        return None, f"volume {vol_ratio}x avg — below average"

    # Box ceiling must be above the trading-TF 200 EMA.
    # If the ceiling is below the 200 EMA the entire consolidation happened in
    # downtrend territory — the "breakout" is just price tagging the EMA from below.
    if resistance < ema200_val:
        return None, (f"box ceiling ₹{round(resistance, 2)} below 200 EMA ₹{ema200_val} "
                      f"— consolidation in downtrend territory")

    range_candles = (len(df) - 1) - range_start
    cleanliness   = compute_cleanliness(df, touch_idx, fl_pos, range_start)

    return {
        "symbol":            symbol,
        "timeframe":         timeframe,
        "close":             round(float(last["close"]), 2),
        "resistance":        round(resistance, 2),
        "first_low":         round(first_low, 2),
        "stop_loss":         round(float(last["ema8"]), 2),  # SL = 8 EMA at entry candle
        "rsi":               round(float(last["rsi14"]), 1),
        "ema8":              round(float(last["ema8"]), 2),
        "ema200":            round(float(last["ema200"]), 2),
        "vol_ratio":         vol_ratio,
        "range_candles":     range_candles,
        "cleanliness":       cleanliness,
        "signal_time":       str(df.index[-1]),
    }, f"breakout confirmed — {range_candles}-candle box, RSI {rsi_val}, vol {vol_ratio}x avg"


ORIGIN_ABOVE_MARGIN_HTF = 0.03

def _find_first_200ema_touch_HTF(df):
    """
    HIGHER-TIMEFRAME-CHECK ONLY. Do NOT use for live 5/15/30min signal
    detection — that stays on find_200ema_touch() above, deliberately
    unchanged (correct for the bot's intraday "most recent touch" use).

    This is the scanner's corrected-origin logic (validated against
    BGRENERGY/KSL in the scanner session): collapses consecutive-day touch
    candidates into one representative per distinct event, then does a
    SINGLE comparison against the next-older event using
    first_low_decisively_broken — preventing a mid-base wobble (like a
    brief bounce candle) from being mistaken for a fresh origin.
    """
    n = len(df)
    if n < 60:
        return None
    candidates = []
    for i in range(n - 2, 30, -1):
        row    = df.iloc[i]
        ema200 = row["ema200"]
        if ema200 == 0:
            continue
        touched = (row["low"] <= ema200 <= row["high"])
        if not touched:
            continue
        lookback_start = max(0, i - 20)
        prior = df.iloc[lookback_start:i]
        if len(prior) < 5:
            continue
        above_count = (prior["close"] > prior["ema200"]).sum()
        if above_count < len(prior) * 0.6:
            continue
        prior_ema = prior["ema200"].replace(0, float("nan"))
        rel = ((prior["close"] - prior_ema) / prior_ema).median()
        if not (rel >= ORIGIN_ABOVE_MARGIN_HTF):
            continue
        candidates.append(i)
    if not candidates:
        return None

    clusters = []
    prev = None
    for c in candidates:
        if prev is not None and prev - c == 1:
            prev = c
            continue
        clusters.append(c)
        prev = c

    best = clusters[0]
    if len(clusters) > 1:
        cand = clusters[1]
        fl_price, fl_pos = find_first_low(df, cand)
        broken = _first_low_decisively_broken_HTF(df, fl_price, fl_pos, best)
        if not broken:
            best = cand
    return best


def _first_low_decisively_broken_HTF(df, first_low, fl_pos, check_end):
    """HIGHER-TIMEFRAME-CHECK ONLY companion to _find_first_200ema_touch_HTF
    / _build_range_box_HTF. 3+ consecutive closes below first_low between
    fl_pos+1 and check_end (exclusive) counts as decisively broken."""
    end = check_end if check_end is not None else len(df)
    segment = df.iloc[fl_pos + 1:end]
    if len(segment) == 0:
        return False
    below = (segment["close"] < first_low)
    run = 0
    for v in below:
        run = run + 1 if v else 0
        if run >= 3:
            return True
    return False


def _build_range_box_HTF(df, fl_pos):
    """
    HIGHER-TIMEFRAME-CHECK ONLY. Do NOT use for live 5/15/30min signal
    detection — that stays on build_range_box() above, deliberately
    unchanged (correct for the bot's intraday use).

    Scanner's noise-spike + pullback-chaining logic (validated against
    BGRENERGY/KSL): re-anchors past early V-bottom noise spikes (<3
    candles), and re-anchors past a REAL breakout too if price later pulls
    back into the old range (low <= old ceiling) — so an old, resolved
    breakout doesn't mask a genuine, CURRENT consolidation that formed
    since. Without this, is_consolidating() would say "not consolidating"
    for a stock like KAMAHOLD (real 1H box, but with an old resolved
    breakout earlier in its history) even while it's visibly sitting
    inside a live, current range on the chart.
    """
    n = len(df)
    range_start = fl_pos + 1
    if range_start >= n - 1:
        return None

    while range_start < n - 1:
        ceiling = float(df.iloc[range_start]["high"])
        breakout_pos = None
        for i in range(range_start + 1, n):
            row = df.iloc[i]
            if row["close"] > ceiling:
                breakout_pos = i
                break
            if row["high"] > ceiling:
                ceiling = float(row["high"])

        if breakout_pos is not None:
            if breakout_pos - range_start < 3:
                range_start = breakout_pos
                continue
            old_ceiling = ceiling
            pullback_pos = None
            sustained_above = 0  # candles that CLOSED above the ceiling post-breakout
            for j in range(breakout_pos + 1, n):
                if df.iloc[j]["close"] <= old_ceiling:
                    # FIX 1: require a CLOSE back inside (not just a wick touch) to
                    # count as a real pullback. A wick touching the old ceiling while
                    # the close is still above it is just normal post-breakout noise.
                    #
                    # FIX 2: if the breakout sustained for 10+ candles above the
                    # ceiling before coming back, treat it as a confirmed, standing
                    # breakout on THIS timeframe — don't re-anchor. The stock now
                    # belongs to a lower timeframe and the bot will catch it there.
                    if sustained_above < 10:
                        pullback_pos = j
                    break
                sustained_above += 1
            if pullback_pos is not None:
                range_start = pullback_pos
                continue
            return {"ceiling": ceiling, "breakout_pos": breakout_pos, "range_start": range_start}
        else:
            if (n - 1) - range_start < 3:
                return None
            return {"ceiling": ceiling, "breakout_pos": None, "range_start": range_start}

    return None

def _find_fallback_low_HTF(df):
    """
    HIGHER-TIMEFRAME-CHECK ONLY fallback. Used when _find_first_200ema_touch_HTF
    finds NO valid origin -- i.e. the stock never had a sharp prior uptrend
    clearly above the 200 EMA (margin >= 3%), it's just been choppy/flat the
    whole visible window (confirmed case: KAMAHOLD 1H, every candidate maxed
    out around 1.8% margin, never cleared 3%).

    Rather than concluding "no structure, no veto" -- which let a 5min trade
    fire today against a stock that was visibly rangebound on 1H -- treat the
    ENTIRE choppy window as one big box: anchor at the single LOWEST low in
    the analyzable range, and let _build_range_box_HTF find the ceiling
    (resistance of the chop) and whether it's broken out, exactly as it
    would for a "real" origin.

    Returns the index position of that lowest low, or None if there's no
    usable data.
    """
    n = len(df)
    if n < 60:
        return None
    window = df.iloc[30:n - 1]
    if len(window) == 0:
        return None
    lowest_pos_in_window = window["low"].idxmin()
    # idxmin returns the actual index label; convert back to positional index.
    fl_pos = df.index.get_loc(lowest_pos_in_window)
    return fl_pos


def is_consolidating(df):
    """
    Returns True ONLY if the stock is currently INSIDE its box — i.e. a valid
    PranUltimate setup is WAITING and has NOT broken out yet.

    Box model:
      Floor   = First Low
      Ceiling = highest high since First Low
      Consolidating = price still inside the box, NO candle has closed above
                      the ceiling yet, First Low never broken.

    If a candle has ALREADY closed above the ceiling (breakout happened) → the
    stock is no longer consolidating, it's trending → returns False.
    This fixes the SPARC bug (broke out days ago, was wrongly called consolidating).

    A clean breakout on a lower timeframe is valid on its own terms — a higher
    timeframe's EMAs drifting close together after an old breakout does not
    override that.

    Used to block lower-timeframe signals when a higher timeframe is consolidating.
    """
    if len(df) < 220:
        return False

    df = add_indicators(df)
    if len(df) < 60:
        return False

    touch_idx = _find_first_200ema_touch_HTF(df)
    used_fallback = touch_idx is None
    if touch_idx is None:
        # No sharp prior uptrend found -- stock has likely just been
        # choppy/flat the whole visible window (KAMAHOLD 1H case). Fall
        # back to treating the whole window as one box anchored at its
        # lowest low, rather than concluding "no structure, no veto."
        fl_pos = _find_fallback_low_HTF(df)
        if fl_pos is None:
            return False
        first_low = float(df.iloc[fl_pos]["low"])
    else:
        first_low, fl_pos = find_first_low(df, touch_idx)

    box = _build_range_box_HTF(df, fl_pos)
    if box is None:
        return False

    if used_fallback:
        # FALLBACK-ONLY sanity check (confirmed bug on METROPOLIS 2H,
        # 2026-06-24): the fallback's "first_low" is just an arbitrary
        # global lowest point, not a true structural reference. After
        # pullback-chaining moves range_start forward, comparing the
        # CURRENT ceiling against that stale, distant original low can
        # measure the width of an entire multi-week uptrend (METROPOLIS:
        # floor 463 vs ceiling 600 = 29.6%) rather than an actual tight
        # consolidation (KAMAHOLD's validated case: 9.8%). Use the lowest
        # low from the box's actual current range_start onward instead.
        end = box["breakout_pos"] if box["breakout_pos"] is not None else len(df)
        current_segment = df.iloc[box["range_start"]:end]
        current_floor = float(current_segment["low"].min()) if len(current_segment) > 0 else first_low
        if current_floor <= 0:
            return False
        width_pct = (box["ceiling"] - current_floor) / current_floor * 100
        if width_pct > 10:
            return False

    # First Low must still be intact
    after_fl = df.iloc[fl_pos + 1:]
    if len(after_fl) > 0 and (after_fl["close"] < first_low).any():
        return False

    # KEY: if a breakout is the function's FINAL state (i.e. price never
    # pulled back into the old range afterward — see _build_range_box_HTF),
    # it's a real, standing continuation, not a current consolidation.
    if box["breakout_pos"] is not None:
        return False

    # No standing breakout, First Low intact, price inside the (possibly
    # re-anchored, current) box → consolidating.
    return True


def has_higher_tf_consolidation(tv, symbol, current_tf):
    """
    Checks if the stock is consolidating on any HIGHER timeframe than current_tf.
    If yes → the intraday signal / box confirmation should be skipped (higher TF owns the move).

    The full intraday TF ladder is used to determine what counts as "higher":
      5min  → checks 15min, 30min, 45min, 1H, 2H, 3H, 4H
      15min → checks 30min, 45min, 1H, 2H, 3H, 4H
      30min → checks 45min, 1H, 2H, 3H, 4H
      45min → checks 1H, 2H, 3H, 4H
      1H    → checks 2H, 3H, 4H

    Capped at 4H — Daily/Weekly consolidations are too far removed to block an
    intraday trade reliably.
    """
    # Full ordered ladder from lowest to highest intraday TF
    _ALL_INTRADAY_TFS = ["5min", "15min", "30min", "45min", "1H", "2H", "3H", "4H"]

    if current_tf not in _ALL_INTRADAY_TFS:
        # Unknown TF (e.g. daily feed) — fall back to original behaviour
        higher_tfs = ["45min", "1H", "2H", "3H", "4H"]
    else:
        idx = _ALL_INTRADAY_TFS.index(current_tf)
        higher_tfs = _ALL_INTRADAY_TFS[idx + 1:]   # every TF strictly above current_tf

    for tf_label in higher_tfs:
        df = fetch_hist(tv, symbol, tf_label, retries=2, pause=1.0)
        if df is None or len(df) < 220:
            continue
        if is_consolidating(df):
            return tf_label   # return which timeframe is consolidating

        # Fix B: recent-breakout guard.
        # is_consolidating() returns False once the HTF has closed above its
        # ceiling (breakout registered).  But if the breakout is very fresh —
        # fewer than 5 candles above the ceiling — the higher TF still owns the
        # move.  Letting a lower-TF Chartink signal fire at this point is
        # exactly the MobiKwik failure mode: the 1H broke out on candle 1, the
        # veto cleared, and the bot took a 15min trade that was really a 1H play.
        try:
            df_ind = add_indicators(df)
            if len(df_ind) >= 60:
                _touch = _find_first_200ema_touch_HTF(df_ind)
                if _touch is None:
                    _fl_pos = _find_fallback_low_HTF(df_ind)
                else:
                    _, _fl_pos = find_first_low(df_ind, _touch)
                if _fl_pos is not None:
                    _box = _build_range_box_HTF(df_ind, _fl_pos)
                    if _box is not None and _box["breakout_pos"] is not None:
                        _ceiling = _box["ceiling"]
                        _candles_above = sum(
                            1 for i in range(_box["breakout_pos"], len(df_ind))
                            if float(df_ind.iloc[i]["close"]) > _ceiling
                        )
                        if _candles_above < 5:
                            return (f"recently broken out on {tf_label} "
                                    f"(only {_candles_above} candle"
                                    f"{'s' if _candles_above != 1 else ''} above ceiling)")
        except Exception:
            pass

    return None   # not consolidating on any higher timeframe


def higher_tf_same_ceiling(tv, symbol, current_tf, current_ceiling, tolerance=0.01):
    """
    Returns (htf_name, htf_ceiling) if any higher TF's box ceiling is within
    `tolerance` (default 1%) of `current_ceiling`, otherwise (None, None).

    The scenario this guards against: a 30min breakout fires at ₹189.80 while
    the 1H chart's own box ceiling is also ₹189-190.  Both timeframes are
    hitting the same wall simultaneously — there is no higher-TF confirmation
    of cleared resistance, just two TFs testing the same level at once.
    These entries almost always reverse immediately (8_EMA_EXIT) because the
    higher-TF resistance is still firmly overhead.

    Differs from has_higher_tf_consolidation(): that function only fires when
    the higher TF is BELOW its ceiling (still consolidating).  This function
    fires when the higher TF is AT its ceiling — i.e. both TFs are attempting
    to break the same resistance in the same candle.
    """
    _ALL_INTRADAY_TFS = ["5min", "15min", "30min", "45min", "1H", "2H", "3H", "4H"]
    if current_tf not in _ALL_INTRADAY_TFS:
        return None, None
    idx        = _ALL_INTRADAY_TFS.index(current_tf)
    higher_tfs = _ALL_INTRADAY_TFS[idx + 1:]

    for htf in higher_tfs:
        try:
            df_htf = fetch_hist(tv, symbol, htf, retries=2, pause=1.0)
            if df_htf is None or len(df_htf) < 60:
                continue
            df_htf = add_indicators(df_htf)

            touch_idx = _find_first_200ema_touch_HTF(df_htf)
            if touch_idx is None:
                fl_pos = _find_fallback_low_HTF(df_htf)
            else:
                _, fl_pos = find_first_low(df_htf, touch_idx)

            if fl_pos is None:
                continue

            box = _build_range_box_HTF(df_htf, fl_pos)
            if box is None:
                continue

            htf_ceiling = box["ceiling"]
            if abs(htf_ceiling - current_ceiling) / current_ceiling <= tolerance:
                return htf, round(htf_ceiling, 2)

        except Exception:
            continue

    return None, None


# ── Position Manager ───────────────────────────────────────────────────────────
class PositionManager:
    def __init__(self, capital_per_trade, max_positions, leverage=1):
        self.capital_per_trade = capital_per_trade
        self.max_positions     = max_positions
        self.leverage          = leverage
        self.positions         = {}   # symbol → position dict
        self.trades            = []   # completed trades

    @property
    def open_count(self):
        return len(self.positions)

    def can_open(self, symbol):
        return symbol not in self.positions and self.open_count < self.max_positions

    def open(self, signal, order_id):
        price    = signal["close"]
        # Leverage: ₹1000 capital × 5x = ₹5000 buying power
        buying_power = self.capital_per_trade * self.leverage
        quantity = int(buying_power / price)
        if quantity == 0:
            log.warning(f"    {signal['symbol']}: ₹{price} > buying power ₹{buying_power} "
                        f"(₹{self.capital_per_trade} × {self.leverage}x) — skipped")
            return False

        self.positions[signal["symbol"]] = {
            "symbol":           signal["symbol"],
            "timeframe":        signal["timeframe"],
            "entry_price":      price,
            "quantity":         quantity,
            "stop_loss":        signal["stop_loss"],
            "resistance":       signal.get("resistance"),
            "sl_order_id":      None,
            "buy_order_id":     order_id,
            "leverage":         self.leverage,
            "margin_used":      round(price * quantity / self.leverage, 2),
            "entry_time":       datetime.now().isoformat(),
            "candles_since_entry": 0,
        }
        position_value = round(price * quantity, 2)
        margin_used    = round(position_value / self.leverage, 2)
        _res = signal.get('resistance', '?')
        _ts  = datetime.now().strftime('%H:%M:%S')
        log.info(f"  ✓ OPENED  {signal['symbol']} [{signal['timeframe']}] @ {_ts} | "
                 f"qty={quantity} | entry=₹{price} | resistance=₹{_res} | SL=₹{signal['stop_loss']}")
        log.info(f"            position value=₹{position_value} | "
                 f"margin used=₹{margin_used} | leverage={self.leverage}x")
        return True

    def close(self, symbol, exit_price, reason):
        if symbol not in self.positions:
            return
        pos = self.positions.pop(symbol)
        pnl = round((exit_price - pos["entry_price"]) * pos["quantity"], 2)
        pnl_pct = round((exit_price - pos["entry_price"]) / pos["entry_price"] * 100, 2)
        trade = {**pos, "exit_price": exit_price,
                 "exit_time": datetime.now().isoformat(),
                 "exit_reason": reason, "pnl": pnl, "pnl_pct": pnl_pct}
        self.trades.append(trade)
        sign = "+" if pnl >= 0 else ""
        _exit_ts  = datetime.now().strftime("%H:%M:%S")
        _res_str  = f" | resistance=₹{pos['resistance']}" if pos.get("resistance") else ""
        log.info(f"  ✗ CLOSED  {symbol} [{pos['timeframe']}] @ {_exit_ts} | exit=₹{exit_price}{_res_str} | "
                 f"P&L={sign}₹{pnl} ({sign}{pnl_pct}%) | reason={reason}")
        self._save()

    def _save(self):
        try:
            with open(TRADES_PATH, "w") as f:
                json.dump({
                    "date":        datetime.now().strftime("%Y-%m-%d"),
                    "trades":      self.trades,
                    "total_pnl":   round(sum(t["pnl"] for t in self.trades), 2),
                    "open_count":  self.open_count,
                }, f, indent=2)
        except Exception:
            pass

# ── Candle Close Timing ────────────────────────────────────────────────────────
_last_scanned = {"5min": None, "15min": None, "30min": None, "45min": None, "1H": None}

def candle_just_closed(timeframe, now):
    """Returns True if a new candle just closed for this timeframe."""
    if now.hour < 9 or (now.hour == 9 and now.minute < 20):
        return False  # first 5min candle closes at 9:20

    m   = now.minute
    key = now.strftime("%H:%M")

    if timeframe == "5min":
        if m % 5 == 0 and _last_scanned["5min"] != key:
            _last_scanned["5min"] = key
            return True

    elif timeframe == "15min":
        if m % 15 == 0 and _last_scanned["15min"] != key:
            _last_scanned["15min"] = key
            return True

    elif timeframe == "30min":
        # NSE 30min candles close at :45 and :15 (from 9:45 onwards)
        is_close = (m == 45) or (m == 15 and now.hour >= 10)
        if is_close and _last_scanned["30min"] != key:
            _last_scanned["30min"] = key
            return True

    elif timeframe == "45min":
        # 45min candles from 9:15: close at 10:00, 10:45, 11:30, 12:15, 13:00, 13:45, 14:30, 15:15
        total_mins = now.hour * 60 + now.minute - (9 * 60 + 15)
        is_close   = total_mins > 0 and total_mins % 45 == 0
        if is_close and _last_scanned["45min"] != key:
            _last_scanned["45min"] = key
            return True

    elif timeframe == "1H":
        # 1H candles from 9:15: close at 10:15, 11:15, 12:15, 13:15, 14:15, 15:15
        total_mins = now.hour * 60 + now.minute - (9 * 60 + 15)
        is_close   = total_mins > 0 and total_mins % 60 == 0
        if is_close and _last_scanned["1H"] != key:
            _last_scanned["1H"] = key
            return True

    return False

def is_market_open(now):
    # NOTE: closing boundary intentionally set PAST square_off_time (15:20).
    # Bug fixed 2026-06-22: this used to be dtime(15, 20) -- identical to
    # square_off_time. Breaking out of the nested scan loops at exactly
    # 15:20:00 takes a moment to unwind back to the top of the main loop;
    # by the time `now` is re-checked here, the clock has already ticked
    # past 15:20:00, so is_market_open() returned False FIRST and routed
    # into the "not market open" sleep(10)/continue branch -- skipping the
    # actual square-off block entirely, forever (since is_market_open never
    # becomes True again that day). This silently skipped square-off,
    # skipped save_watchlist(), and left the process hung in an infinite
    # idle-sleep loop until manually killed. Using the real NSE close
    # (15:30) instead gives a 10-minute buffer so the square-off code is
    # always reached.
    return dtime(9, 15) <= now.time() <= dtime(15, 30)

# ── Load nightly watchlist additions ──────────────────────────────────────────
# ── Watchlist State (persistent, per-symbol box data) ────────────────────────────────────────────────
# Format: {symbol: {tf, floor, ceiling, box_confirmed, added_date}}
#
# Chartink is a DISCOVERY tool only — it tells us which stocks are touching the
# 200 EMA right now.  Once a stock is added here it STAYS until one of two exits:
#   1. Floor breach (low < floor)    → escalate to the next higher timeframe
#   2. Ceiling breach (close > ceil) → validate confirmations and trade
# Nothing else removes a symbol. The box (floor/ceiling) is locked the moment
# 3 candles of consolidation are confirmed and is NEVER redrawn.
#
# TF ladder: 5min → 15min → 30min → 45min → 1H
#   Stocks surfaced on 5min  Chartink start at 5min  and walk up.
#   Stocks surfaced on 15min Chartink start at 15min and walk up.
#   Stocks surfaced on 30min Chartink start at 30min and walk up.
#   45min and 1H stocks arrive via TF escalation only (no Chartink formula).

TF_LADDER  = ["5min", "15min", "30min", "45min", "1H"]
TF_CASCADE = {"5min": "15min", "15min": "30min", "30min": "45min", "45min": "1H"}
TF_MINUTES = {"5min": 5, "15min": 15, "30min": 30, "45min": 45, "1H": 60}
_trade_lock = threading.Lock()   # serialises order-placement across parallel scan threads


def load_watchlist_state():
    """Load persistent watchlist with floor/ceiling data from previous sessions."""
    try:
        if not os.path.exists(WATCHLIST_STATE_PATH):
            return {}
        with open(WATCHLIST_STATE_PATH) as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Could not load watchlist state ({e}) — starting fresh.")
        return {}


def save_watchlist_state(state):
    """Persist watchlist state so the next session picks up floor/ceiling exactly.
    Uses atomic write (temp file + os.replace) so a crash mid-write never
    corrupts the state file — the old version stays intact until the new one
    is fully flushed and renamed."""
    try:
        tmp = WATCHLIST_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, WATCHLIST_STATE_PATH)  # atomic on POSIX; near-atomic on Windows
        return True
    except Exception as e:
        log.warning(f"Could not save watchlist state: {e}")
        return False


def load_scanner_universe() -> dict:
    """
    Read the nightly scanner results.json and return a dict mapping
    symbol → timeframe for every stock that appears on a 1H or higher
    timeframe with a status of NEAR BREAKOUT, WATCHING, or CONSOLIDATING.

    These stocks are in an active 1H+ structure and must not be added to the
    intraday watchlist via a lower-TF Chartink signal — the higher timeframe
    owns the move and any intraday entry would be premature.

    Only the lowest qualifying TF is kept when a symbol appears in multiple
    TF buckets (e.g. both 1H and 2H → stored as 1H).
    """
    HIGH_TFS = {"1H", "2H", "3H", "4H"}
    universe: dict = {}
    if not os.path.exists(RESULTS_PATH):
        log.warning(f"Scanner results not found at {RESULTS_PATH} — 1H universe empty")
        return universe
    try:
        with open(RESULTS_PATH) as f:
            data = json.load(f)
        results = data.get("results", {})
        for tf in ("1H", "2H", "3H", "4H"):   # ordered low→high; first match wins
            for item in results.get(tf, []):
                sym    = item.get("symbol", "")
                status = item.get("status", "")
                if not sym:
                    continue
                if ("NEAR BREAKOUT" in status
                        or "WATCHING"     in status
                        or "CONSOLIDATING" in status):
                    if sym not in universe:   # keep the lowest (most relevant) TF
                        universe[sym] = tf
        sample = sorted(universe.keys())[:8]
        ellipsis = "..." if len(universe) > 8 else ""
        log.info(f"Scanner 1H+ universe loaded: {len(universe)} symbols "
                 f"({', '.join(sample)}{ellipsis})")
    except Exception as e:
        log.warning(f"Failed to load scanner universe: {e}")
    return universe


def startup_gap_check(tv, watchlist_state):
    """
    On session startup, check all confirmed boxes against the most recent candle.
    Catches floor breaches that happened while the bot was offline (weekend gaps,
    circuit breakers, overnight news) so the bot doesn't carry dead setups into
    the live session.
    Called once per session, right after load_watchlist_state().
    """
    confirmed = [(sym, dict(data)) for sym, data in list(watchlist_state.items())
                 if data.get("box_confirmed")]
    if not confirmed:
        return
    log.info(f"Startup gap-check: verifying {len(confirmed)} confirmed boxes...")
    for symbol, data in confirmed:
        if symbol not in watchlist_state:
            continue  # already removed by an earlier iteration
        tf    = data["tf"]
        floor = data["floor"]
        try:
            df = fetch_hist(tv, symbol, tf)
            if df is None or len(df) < 5:
                log.info(f"  {symbol}: no data for gap-check — skipping")
                continue
            last_low = float(df.iloc[-1]["low"])
            if last_low < floor:
                next_tf = TF_CASCADE.get(tf)
                if next_tf:
                    log.info(f"  {symbol}: gap-check — floor breached offline "
                             f"(low \u20b9{last_low:.2f} < floor \u20b9{floor:.2f}) "
                             f"\u2014 escalating to [{next_tf}]")
                    watchlist_state[symbol] = {
                        "tf":                 next_tf,
                        "floor":              None,
                        "ceiling":            None,
                        "box_confirmed":      False,
                        "added_date":         data["added_date"],
                        "box_confirmed_date": None,
                    }
                else:
                    log.info(f"  {symbol}: gap-check — floor breached, end of ladder, removed")
                    del watchlist_state[symbol]
            else:
                log.info(f"  {symbol} [{tf}]: gap-check OK "
                         f"(low \u20b9{last_low:.2f} >= floor \u20b9{floor:.2f})")
                # ── Startup EMA-touch upgrade (FLUOROCHEM/SANSERA/RELAXO fix) ──────
                # Confirmed boxes carry their locked TF across sessions.  If the box
                # was confirmed on a lower TF but the next-higher TF’s 200 EMA was
                # recently touched, the setup really belongs to the higher TF.
                # Re-run this check every startup so stale lower-TF tags are fixed
                # before the live session begins.  Floor/ceiling/box_confirmed are
                # left intact — only the TF label is updated.
                _STARTUP_UPGRADEABLE = {"5min", "15min", "30min"}
                if tf in _STARTUP_UPGRADEABLE:
                    _cur_tf_idx = TF_LADDER.index(tf)
                    _next_tf = (
                        TF_LADDER[_cur_tf_idx + 1]
                        if _cur_tf_idx + 1 < len(TF_LADDER)
                        else None
                    )
                    if _next_tf:
                        _htf_lb_map = {
                            "15min": 40,   # 5min  → 15min: ~40 candles (≈2 trading days)
                            "30min": 16,   # 15min → 30min: ~16 candles (≈2 trading days)
                            "45min": 12,   # 30min → 45min: ~12 candles (≈2 trading days)
                        }
                        _htf_lb = _htf_lb_map.get(_next_tf, 16)
                        try:
                            _df_htf_raw = fetch_hist(tv, symbol, _next_tf)
                            if _df_htf_raw is not None and len(_df_htf_raw) >= 50:
                                _df_htf_ind = add_indicators(_df_htf_raw)
                                if len(_df_htf_ind) >= _htf_lb:
                                    _touch_found = False
                                    _n_htf = len(_df_htf_ind)
                                    _window_start = _n_htf - _htf_lb
                                    for _ci in range(_window_start, _n_htf):
                                        _row = _df_htf_ind.iloc[_ci]
                                        _ema = _row["ema200"]
                                        if _ema == 0:
                                            continue
                                        if not (_row["low"] <= _ema <= _row["high"]):
                                            continue
                                        # Consolidation filter: skip if EMA was already
                                        # inside range for 3+ of the 5 preceding candles
                                        _consol_count = 0
                                        for _j in range(max(0, _ci - 5), _ci):
                                            _prow = _df_htf_ind.iloc[_j]
                                            if _prow["low"] <= _prow["ema200"] <= _prow["high"]:
                                                _consol_count += 1
                                        if _consol_count >= 3:
                                            continue
                                        _touch_found = True
                                        break
                                    if _touch_found:
                                        log.info(
                                            f"  {symbol} [{tf}]: startup EMA-touch upgrade — "
                                            f"{_next_tf} 200 EMA touch in last {_htf_lb} candles "
                                            f"→ upgrading to [{_next_tf}] "
                                            f"(floor/ceil/confirmed preserved)"
                                        )
                                        watchlist_state[symbol]["tf"] = _next_tf
                                        _log_action(symbol, tf, "TF_UPGRADE_STARTUP",
                                                    {"new_tf": _next_tf})
                                        _send_bot_alert(
                                            f"\U0001f53c <b>{symbol}</b>: startup TF upgrade "
                                            f"[{tf}] → [{_next_tf}]\n"
                                            f"Reason: {_next_tf} 200 EMA touched recently "
                                            f"(startup re-check)"
                                        )
                        except Exception as _ema_e:
                            log.warning(
                                f"  {symbol}: startup EMA-upgrade check failed — {_ema_e}"
                            )
        except Exception as e:
            log.error(f"  {symbol}: gap-check error — {e}")
        time.sleep(0.3)


# ── Main Bot Loop ──────────────────────────────────────────────────────────────────────────────
def run():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    mode = "PAPER TRADING" if cfg["paper_trading"] else "LIVE TRADING"
    log.info("=" * 60)
    log.info(f"PranUltimate Intraday Bot — {mode}")
    log.info(f"Capital/trade: ₹{cfg['capital_per_trade']} | "
             f"Max positions: {cfg['max_positions']} | "
             f"Leverage: {cfg.get('leverage', 1)}x")
    log.info(f"Square off: {cfg['square_off_time']} IST")
    log.info("=" * 60)

    dhan = DhanClient(cfg["client_id"], cfg["access_token"], cfg["paper_trading"])
    pm   = PositionManager(
        cfg["capital_per_trade"], cfg["max_positions"],
        leverage=cfg.get("leverage", 1)
    )
    tv = DhanData(cfg["client_id"], cfg["access_token"])
    log.info("Dhan data API: connected")

    # Load nightly scanner universe — stocks on 1H+ structure that should never
    # be added via a lower-TF Chartink signal.
    global _scanner_1h_universe
    _scanner_1h_universe = load_scanner_universe()

    # Load persistent watchlist state (carries floor/ceiling across sessions)
    watchlist_state = load_watchlist_state()
    total_tracked = len(watchlist_state)
    if total_tracked:
        by_tf = {}
        for sym, data in watchlist_state.items():
            tf_k = data.get("tf", "?")
            by_tf[tf_k] = by_tf.get(tf_k, 0) + 1
        confirmed = sum(1 for d in watchlist_state.values() if d.get("box_confirmed"))
        log.info(f"Loaded watchlist state: {total_tracked} symbols "
                 f"({confirmed} confirmed boxes) | "
                 + ", ".join(f"{tf}={n}" for tf, n in sorted(by_tf.items())))
    else:
        log.info("No saved watchlist state — starting fresh")

    # Startup gap-check: catch floor breaches that happened while bot was offline
    # (weekend gaps, circuit breakers, overnight news).
    if watchlist_state:
        startup_gap_check(tv, watchlist_state)
        save_watchlist_state(watchlist_state)  # persist any escalations from gap-check

    sq_h, sq_m      = map(int, cfg["square_off_time"].split(":"))
    square_off_time = dtime(sq_h, sq_m)
    entered_today   = set()

    while True:
        now = datetime.now()

        # ── Pre-market wait ────────────────────────────────────────────────────────────────────
        if not is_market_open(now):
            if now.time() < dtime(9, 0):
                log.info("Pre-market — sleeping 5 minutes")
                time.sleep(300)
            else:
                time.sleep(10)
            continue

        # ── Square off ──────────────────────────────────────────────────────────────────────────
        if now.time() >= square_off_time:
            if pm.open_count > 0:
                log.info("⏰ SQUARE OFF — closing all open positions")
                for sym in list(pm.positions.keys()):
                    pos = pm.positions[sym]
                    dhan.place_order(sym, pos["quantity"], "SELL")
                    df_sq = fetch_hist(tv, sym, pos["timeframe"])
                    sq_price = (float(df_sq.iloc[-1]["close"])
                                if df_sq is not None and len(df_sq) > 0
                                else pos["entry_price"])
                    pm.close(sym, sq_price, "SQUARE_OFF")

            if save_watchlist_state(watchlist_state):
                log.info(f"Watchlist state saved: {len(watchlist_state)} symbols "
                         f"carried to next session")
            else:
                log.warning("Watchlist state save failed")

            log.info("Market session ended.")
            break

        # ── Time-based 2-candle exit: runs every 30 s regardless of candle close ─────────────────────────
        # Replaces counter approach: 5min scan takes 14+ min and misses 15min/30min closes entirely.
        # Exit at first outer-loop tick where now >= entry_time + 2 × TF_minutes.
        for _pos_sym, _pos in list(pm.positions.items()):
            _pos_tf   = _pos["timeframe"]
            _entry_dt = datetime.fromisoformat(_pos["entry_time"])
            _tf_mins  = TF_MINUTES.get(_pos_tf, 5)
            if now >= _entry_dt + timedelta(minutes=2 * _tf_mins):
                _df_x = fetch_hist(tv, _pos_sym, _pos_tf)
                _xprice = (float(_df_x.iloc[-1]["close"])
                           if _df_x is not None and len(_df_x) > 0
                           else _pos["entry_price"])
                dhan.place_order(_pos_sym, _pos["quantity"], "SELL")
                pm.close(_pos_sym, _xprice, "2-CANDLE-EXIT")
                _held = int((now - _entry_dt).total_seconds() / 60)
                log.info(f"  ⏱ 2-candle exit: {_pos_sym} [{_pos_tf}] @ ₹{_xprice} (held {_held}min)")

        # ── Process each timeframe at its candle close ─────────────────────────────────────────────────────
        for tf in TF_LADDER:
            if datetime.now().time() >= square_off_time:
                break

            if not candle_just_closed(tf, now):
                continue

            # ── Fix #6: Close-based 8 EMA stop-loss for open positions ────────────────────────
            # Replaces exchange-side SL-M orders. At each candle CLOSE, check every
            # open position on this TF. If close < 8 EMA → exit at market immediately.
            # "If the candle does not close below 8 EMA, it should not exit."
            for _sl_sym in list(pm.positions.keys()):
                _sl_pos = pm.positions.get(_sl_sym)
                if _sl_pos is None or _sl_pos.get("timeframe") != tf:
                    continue
                _df_sl = fetch_hist(tv, _sl_sym, tf)
                if _df_sl is None or len(_df_sl) < 10:
                    continue
                _df_sl_ind = add_indicators(_df_sl)
                if len(_df_sl_ind) == 0:
                    continue
                _sl_last   = _df_sl_ind.iloc[-1]
                _sl_ema8_val = _sl_last.get("ema8")
                if _sl_ema8_val is None or pd.isna(_sl_ema8_val):
                    continue
                _sl_close  = float(_sl_last["close"])
                _sl_ema8   = float(_sl_ema8_val)
                if _sl_close < _sl_ema8:
                    log.info(f"  🔴 8EMA CLOSE SL: {_sl_sym} [{tf}] "
                             f"close ₹{_sl_close:.2f} < 8 EMA ₹{_sl_ema8:.2f} — exiting")
                    _log_action(_sl_sym, tf, "EXIT_8EMA_CLOSE",
                                {"close": _sl_close, "ema8": _sl_ema8,
                                 "entry": _sl_pos.get("entry_price"),
                                 "qty":   _sl_pos.get("quantity")})
                    _send_bot_alert(f"🔴 <b>{_sl_sym}</b> [{tf}]: 8 EMA exit\nClose ₹{_sl_close:.2f} < 8 EMA ₹{_sl_ema8:.2f}")
                    dhan.place_order(_sl_sym, _sl_pos["quantity"], "SELL")
                    pm.close(_sl_sym, _sl_close, "8EMA_CLOSE_EXIT")

            # ── Step 1: Chartink → add new stocks immediately ─────────────────────────────────
            chartink_results = []
            if tf in ("5min", "15min", "30min"):
                chartink_results = fetch_chartink_candidates(tf)

            new_count = 0
            for sym in chartink_results:
                # Never re-add a stock that was already traded today on ANY timeframe.
                _sym_day_key = f"{sym}_{now.strftime('%Y%m%d')}"
                if _sym_day_key in entered_today:
                    log.info(f"    ✗ {sym} [{tf}]: already traded today — skip Chartink re-add")
                    _log_action(sym, tf, "SKIP_REENTRY_TODAY", {"blocked_key": _sym_day_key})
                    continue

                # Fix #3 (Jamna Auto): if this symbol is already on a LOWER TF
                # watchlist (box not yet confirmed), upgrade it to the current
                # (higher) TF. Rationale: both TFs fire on the same first-candle
                # EMA touch, but the lower TF fires first (15min at 9:30 vs
                # 30min at 9:45). Without this, the stock is locked as 15min even
                # though the actual setup belongs to the 30min chart.
                _lower_tfs = TF_LADDER[:TF_LADDER.index(tf)] if tf in TF_LADDER else []
                if (sym in watchlist_state
                        and watchlist_state[sym].get("tf") in _lower_tfs
                        and not watchlist_state[sym].get("box_confirmed")):
                    _cur_tf = watchlist_state[sym]["tf"]
                    log.info(f"    ↑ {sym}: upgrading [{_cur_tf}] → [{tf}] "
                             f"(Chartink hit on higher TF)")
                    _log_action(sym, _cur_tf, "TF_UPGRADE_CHARTINK", {"new_tf": tf})
                    _send_bot_alert(f"🔼 <b>{sym}</b>: upgraded [{_cur_tf}] → [{tf}]\nReason: Chartink hit on higher TF")
                    watchlist_state[sym] = {
                        "tf":                 tf,
                        "floor":              None,
                        "ceiling":            None,
                        "box_confirmed":      False,
                        "added_date":         watchlist_state[sym].get("added_date",
                                                                       now.strftime("%Y-%m-%d")),
                        "box_confirmed_date": None,
                    }
                    new_count += 1
                    continue

                if sym not in watchlist_state:
                    # Nightly-scanner universe guard: if the stock is already
                    # tracked in the 1H+ scanner (NEAR BREAKOUT / WATCHING /
                    # CONSOLIDATING), the higher timeframe owns the setup.
                    # A lower-TF Chartink signal here is noise — skip it.
                    if sym in _scanner_1h_universe:
                        log.info(f"    ✗ {sym} [{tf}]: in nightly 1H+ scanner universe "
                                 f"({_scanner_1h_universe[sym]}) — skip lower-TF Chartink signal")
                        continue
                    # ETF / index-fund filter — skip immediately, no API call needed
                    if is_etf(sym):
                        log.info(f"    ✗ {sym}: skipped (ETF/index fund)")
                        continue
                    # Price filter: skip very low-price stocks (ETFs, penny stocks)
                    _df_chk = fetch_hist(tv, sym, tf)
                    if _df_chk is not None and len(_df_chk) > 0:
                        _last_close = float(_df_chk.iloc[-1]["close"])
                        if _last_close < 50:
                            log.info(f"    ✗ {sym}: skipped (price ₹{_last_close:.2f} < ₹50)")
                            continue
                        # Liquidity filter: estimate daily turnover from recent candles.
                        # Reuses _df_chk already fetched above -- zero extra API calls.
                        # Scale intraday candles to a full trading day (390 min).
                        _cpd      = 390 // TF_MINUTES.get(tf, 5)  # candles per day
                        _daily_to = (_df_chk["close"] * _df_chk["volume"]).tail(_cpd).sum()
                        if _daily_to < 5_00_00_000:  # ₹5 crore
                            log.info(f"    ✗ {sym}: skipped "
                                     f"(est. turnover ₹{_daily_to / 1e7:.1f}cr < ₹5cr)")
                            continue
                    # Higher-TF consolidation guard: if a higher TF is already
                    # consolidating, that TF owns the move — a lower-TF entry is
                    # premature and should be skipped entirely.
                    _htf = has_higher_tf_consolidation(tv, sym, tf)
                    if _htf:
                        log.info(f"    SKIP {sym} [{tf}] -- higher TF [{_htf}] consolidation active (Chartink add)")
                        continue
                    watchlist_state[sym] = {
                        "tf":                 tf,
                        "floor":              None,
                        "ceiling":            None,
                        "box_confirmed":      False,
                        "added_date":         now.strftime("%Y-%m-%d"),
                        "box_confirmed_date": None,
                    }
                    new_count += 1
                    log.info(f"    + {sym}: added to [{tf}] watchlist")

            tf_stocks = [s for s, d in watchlist_state.items() if d.get("tf") == tf]
            log.info(f"\n── {tf} candle closed | Chartink: {len(chartink_results)} candidates, "
                     f"{new_count} new | tracking {len(tf_stocks)} on this TF ──")

            # ── Step 1b: 2-candle exit handled in outer loop (time-based) ──

            # ── Step 2: Process each tracked symbol on this TF ─────────────────────────────────
            # -- Step 2: Process each tracked symbol on this TF (parallel) ----------------
            # Up to MAX_SCAN_WORKERS symbols are fetched and analysed simultaneously.
            # _trade_lock serialises the final order-placement step so two threads
            # can never open the same position or exceed max_positions in a race.
            MAX_SCAN_WORKERS = 5   # tune up for faster scans, down if TV API throttles

            def _scan_symbol(symbol):
                if datetime.now().time() >= square_off_time:
                    return
                sig_key = f"{symbol}_{now.strftime('%Y%m%d')}"
                if sig_key in entered_today:
                    return
                if not pm.can_open(symbol):
                    if pm.open_count >= pm.max_positions:
                        return
                    return

                wl_data = watchlist_state.get(symbol)
                if wl_data is None:
                    return

                # ETF guard for pre-loaded watchlist symbols
                if is_etf(symbol):
                    log.info(f"  ✗ {symbol}: removing (ETF/index fund)")
                    watchlist_state.pop(symbol, None)
                    return

                try:
                    df = fetch_hist(tv, symbol, tf)
                    if df is None or len(df) < 50:
                        log.info(f"  {symbol}: no data -- keeping in watchlist")
                        time.sleep(1.0)
                        return

                    # -- Price filter: skip sub-Rs50 stocks ----------------------
                    _scan_price = float(df.iloc[-1]["close"])
                    if _scan_price < 50:
                        log.info(f"  {symbol} [{tf}]: skipped at scan -- price Rs{_scan_price:.2f} < Rs50")
                        time.sleep(1.0)
                        return

                    # -- Staleness gate ------------------------------------------
                    # Even with parallel workers the scan takes time. If this candle
                    # closed more than 2x the TF ago, we're too late to trade it --
                    # skip now and catch the NEXT fresh close instead.
                    try:
                        _candle_dt = pd.Timestamp(df.index[-1]).to_pydatetime().replace(tzinfo=None)
                        _age_mins  = (now - _candle_dt).total_seconds() / 60
                        _tf_mins   = TF_MINUTES.get(tf, 5)
                        if _age_mins > _tf_mins * 2:
                            log.info(f"  {symbol} [{tf}]: stale candle ({_age_mins:.0f}min old) -- skipping, wait for fresh close")
                            return   # keep in watchlist; check again next close
                    except Exception:
                        pass

                    wl_data = watchlist_state.get(symbol)
                    if wl_data is None:
                        return

                    if wl_data["box_confirmed"]:
                        # --------------------------------------------------------
                        # BOX IS LOCKED
                        # --------------------------------------------------------
                        df_ind = add_indicators(df)
                        if len(df_ind) < 60:
                            return
                        last    = df_ind.iloc[-1]
                        prev    = df_ind.iloc[-2]
                        floor   = wl_data["floor"]
                        ceiling = wl_data["ceiling"]

                        # Guard: box state can arrive with None floor/ceiling if the
                        # state file was written mid-session before the box was fully
                        # formed, or from an older bot version that had a different
                        # save path.  Without this guard the very first comparison
                        # `prev["high"] > ceiling` (wick-update below) raises:
                        #   TypeError: '>' not supported between 'float' and 'NoneType'
                        if floor is None or ceiling is None:
                            log.warning(f"  {symbol} [{tf}]: box_confirmed=True but "
                                        f"floor={floor} / ceiling={ceiling} is None "
                                        f"— box state corrupted, resetting to unconfirmed")
                            watchlist_state[symbol].update({"box_confirmed": False})
                            return

                        # Guard: ema200 / ema8 / rsi14 / vol_avg must all be valid
                        # (non-None, non-NaN) before any arithmetic or comparison.
                        # These can be None or NaN when the symbol has sparse intraday
                        # data on this timeframe (circuit-filter stocks, recently listed
                        # SME stocks, or stocks with frequent trading halts) — Dhan
                        # returns fewer candles than needed for the rolling/EWM windows
                        # to converge, and add_indicators() / dropna() can leave NaN
                        # values in the last row in edge cases.
                        # Without this guard, `last["close"] > last["ema200"]` raises:
                        #   TypeError: '>' not supported between 'float' and 'NoneType'
                        for _ind_name in ("ema200", "ema8", "rsi14", "vol_avg"):
                            _ind_val = last.get(_ind_name)
                            if _ind_val is None or pd.isna(_ind_val):
                                log.warning(f"  {symbol} [{tf}]: {_ind_name} not available "
                                            f"(insufficient candles) — skip")
                                return

                        # -- TTL: archive boxes older than 45 calendar days ------
                        confirmed_date = wl_data.get("box_confirmed_date")
                        if confirmed_date:
                            days_old = (
                                datetime.now().date()
                                - datetime.strptime(confirmed_date, "%Y-%m-%d").date()
                            ).days
                            if days_old > 45:
                                log.info(f"  {symbol} [{tf}]: box confirmed {confirmed_date} "
                                         f"({days_old}d ago) -- TTL exceeded, archiving")
                                del watchlist_state[symbol]
                                return

                        # -- Wick update: raise ceiling for post-lock wicks ------
                        # Also back-check the PREVIOUS candle for a missed wick update
                        # (Fix #5: KALYANIFRG — prev candle had high=630.9 above ceiling
                        # but scan timing meant the wick update ran on the wrong candle).
                        if prev["high"] > ceiling and prev["close"] <= ceiling:
                            _old_ceil = ceiling
                            ceiling = float(prev["high"])
                            watchlist_state[symbol]["ceiling"] = ceiling
                            log.info(f"  {symbol} [{tf}]: ceiling raised from prev-candle wick "
                                     f"Rs{_old_ceil:.2f} → Rs{ceiling:.2f}")

                        if last["high"] > ceiling and last["close"] <= ceiling:
                            _old_ceil = ceiling
                            ceiling = float(last["high"])
                            watchlist_state[symbol]["ceiling"] = ceiling
                            log.info(f"  {symbol} [{tf}]: ceiling up Rs{_old_ceil:.2f} -> Rs{ceiling:.2f} (wick)")

                        if last["close"] > ceiling:
                            if prev["close"] > ceiling:
                                log.info(f"  {symbol} [{tf}]: stale breakout -- "
                                         f"already above ceiling Rs{ceiling:.2f} last candle, archiving")
                                del watchlist_state[symbol]
                                return

                            # Fix #1 revised (NEOGEN): ceiling below 200 EMA means
                            # the locked box formed in downtrend territory. But the
                            # stock may be valid on a HIGHER TF (as NEOGEN was on 45min
                            # while the bot had it tagged 15min). Walk up TF_LADDER
                            # and upgrade to the first TF where ceiling > EMA200.
                            # Only delete if no higher TF clears the check.
                            if ceiling < float(last["ema200"]):
                                log.info(f"  {symbol} [{tf}]: locked ceiling Rs{ceiling:.2f} "
                                         f"below {tf} 200 EMA Rs{float(last['ema200']):.2f} "
                                         f"— checking higher TFs for valid box")
                                _upgraded = False
                                _cur_idx = TF_LADDER.index(tf) if tf in TF_LADDER else -1
                                for _htf in (TF_LADDER[_cur_idx + 1:] if _cur_idx >= 0 else []):
                                    _df_h = fetch_hist(tv, symbol, _htf)
                                    if _df_h is None or len(_df_h) < 200:
                                        continue
                                    _df_h_ind  = add_indicators(_df_h)
                                    _ema200_h  = float(_df_h_ind.iloc[-1]["ema200"])
                                    if ceiling > _ema200_h:
                                        log.info(f"    ↑ {symbol}: ceiling Rs{ceiling:.2f} > "
                                                 f"{_htf} EMA200 Rs{_ema200_h:.2f} "
                                                 f"— upgrading [{tf}] → [{_htf}]")
                                        watchlist_state[symbol]["tf"] = _htf
                                        _log_action(symbol, tf, "TF_UPGRADE_CEILING",
                                                    {"ceiling": ceiling,
                                                     "ema200_old_tf": float(last["ema200"]),
                                                     "new_tf": _htf,
                                                     "ema200_new_tf": _ema200_h})
                                        _send_bot_alert(f"🔼 <b>{symbol}</b>: ceiling ₹{ceiling:.2f} valid on [{_htf}]\nUpgraded from [{tf}]")
                                        _upgraded = True
                                        break
                                if not _upgraded:
                                    log.info(f"  SKIP {symbol}: ceiling Rs{ceiling:.2f} below "
                                             f"EMA200 on all higher TFs — removing from watchlist")
                                    _log_action(symbol, tf, "SKIP_CEILING_BELOW_EMA200",
                                                {"ceiling": ceiling,
                                                 "ema200": float(last["ema200"])})
                                    del watchlist_state[symbol]
                                return

                            rsi_val      = float(last["rsi14"])
                            vol_ratio    = float(last["volume"] / last["vol_avg"])
                            above_200    = last["close"] > last["ema200"]
                            # NOTE: 20/50 EMA split removed — not used as entry gate.

                            if (rsi_val >= 45 and vol_ratio > 1.0 and above_200):

                                signal = {
                                    "symbol":        symbol,
                                    "timeframe":     tf,
                                    "close":         round(float(last["close"]), 2),
                                    "resistance":    round(ceiling, 2),
                                    "first_low":     round(floor, 2),
                                    "stop_loss":     round(float(last["ema8"]), 2),
                                    "rsi":           round(rsi_val, 1),
                                    "ema8":          round(float(last["ema8"]), 2),
                                    "ema200":        round(float(last["ema200"]), 2),
                                    "vol_ratio":     round(vol_ratio, 1),
                                    "range_candles": 0,
                                    "cleanliness":   0,
                                    "signal_time":   str(df_ind.index[-1]),
                                }
                                r_str = (f"breakout -- locked ceil Rs{ceiling:.2f}, "
                                         f"RSI {rsi_val:.1f}, vol {vol_ratio:.1f}x avg")
                                log.info(f"\n  SIGNAL: {symbol} [{tf}] -- {r_str}")
                                log.info(f"    close=Rs{signal['close']} | "
                                         f"resistance=Rs{signal['resistance']}")
                                log.info(f"    first_low=Rs{signal['first_low']} | "
                                         f"RSI={signal['rsi']} | "
                                         f"vol={signal['vol_ratio']}x avg")
                                log.info(f"    SL=Rs{signal['stop_loss']}")

                                series = tv.get_series(symbol)
                                if series != "EQ":
                                    log.info(f"  SKIP {symbol} [{tf}] -- "
                                             f"series={series or 'unresolved'} "
                                             f"(not EQ, can't trade intraday)")
                                    del watchlist_state[symbol]
                                    time.sleep(1.0)
                                    return

                                # -- 15-min 200 EMA filter (mandatory) ---------
                                # Fix #4 (CARRARO): previously skipped silently
                                # when 15min data was unavailable — now mandatory.
                                # A fetch failure or insufficient data = reject entry
                                # (safer than trading blind on macro trend).
                                _df_15 = fetch_hist(tv, symbol, "15min")
                                if _df_15 is None or len(_df_15) < 200:
                                    log.info(f"  SKIP {symbol} [{tf}] -- "
                                             f"15min data unavailable/insufficient "
                                             f"({len(_df_15) if _df_15 is not None else 0} candles) "
                                             f"— cannot verify macro trend")
                                    _log_action(symbol, tf, "SKIP_15MIN_EMA",
                                                {"reason": "data_unavailable",
                                                 "candles": len(_df_15) if _df_15 is not None else 0})
                                    del watchlist_state[symbol]
                                    time.sleep(1.0)
                                    return
                                _df_15i    = add_indicators(_df_15)
                                _ema200_15 = float(_df_15i.iloc[-1]["ema200"])
                                if signal["close"] < _ema200_15:
                                    log.info(f"  SKIP {symbol} [{tf}] -- "
                                             f"price Rs{signal['close']} below 15min 200 EMA "
                                             f"Rs{_ema200_15:.2f} -- macro trend bearish")
                                    _log_action(symbol, tf, "SKIP_15MIN_EMA",
                                                {"close": signal["close"],
                                                 "ema200_15min": _ema200_15})
                                    del watchlist_state[symbol]
                                    time.sleep(1.0)
                                    return

                                # -- 30min/45min 200 EMA filter -----------------
                                # Fix #2 (KPI Green): for higher intraday TFs,
                                # explicitly verify price is above the trading-TF
                                # 200 EMA. The inline above_200 check covers this
                                # logically, but df_ind may have fewer candles than
                                # needed for full EMA convergence. This uses
                                # df_ind (already computed) and requires ≥ 220
                                # candles for a reliable EMA200 signal.
                                if tf in ("30min", "45min"):
                                    if len(df_ind) >= 220:
                                        _ema200_tf = float(df_ind.iloc[-1]["ema200"])
                                        if signal["close"] < _ema200_tf:
                                            log.info(f"  SKIP {symbol} [{tf}] -- "
                                                     f"price Rs{signal['close']} below "
                                                     f"{tf} 200 EMA Rs{_ema200_tf:.2f}")
                                            _log_action(symbol, tf, "SKIP_TF_EMA",
                                                        {"close": signal["close"],
                                                         "ema200": _ema200_tf})
                                            del watchlist_state[symbol]
                                            time.sleep(1.0)
                                            return
                                    else:
                                        log.info(f"  SKIP {symbol} [{tf}] -- "
                                                 f"only {len(df_ind)} candles, need 220 "
                                                 f"for reliable {tf} 200 EMA check")
                                        _log_action(symbol, tf, "SKIP_TF_EMA",
                                                    {"reason": "insufficient_candles",
                                                     "candles": len(df_ind)})
                                        del watchlist_state[symbol]
                                        time.sleep(1.0)
                                        return

                                # -- Co-ceiling guard ----------------------------
                                # Block if a higher TF's box ceiling coincides with
                                # this TF's ceiling (within 1%).  Both TFs testing
                                # the same wall simultaneously = no HTF confirmation.
                                _co_tf, _co_ceil = higher_tf_same_ceiling(
                                    tv, symbol, tf, ceiling
                                )
                                if _co_tf:
                                    log.info(
                                        f"  SKIP {symbol} [{tf}] -- ceiling "
                                        f"Rs{ceiling:.2f} coincides with [{_co_tf}] "
                                        f"ceiling Rs{_co_ceil} (±1%) — no higher-TF "
                                        f"confirmation, both TFs hitting same resistance"
                                    )
                                    del watchlist_state[symbol]
                                    time.sleep(1.0)
                                    return

                                lev = cfg.get("leverage", 1)
                                qty = int((cfg["capital_per_trade"] * lev) / signal["close"])
                                if qty == 0:
                                    log.warning(f"    Price Rs{signal['close']} > "
                                                f"buying power -- skip")
                                    del watchlist_state[symbol]
                                    time.sleep(1.0)
                                    return

                                # -- Order placement under _trade_lock ----------
                                # Re-check inside the lock: another thread may have
                                # already entered this signal or filled the last slot.
                                with _trade_lock:
                                    _sig_key2 = f"{symbol}_{now.strftime('%Y%m%d')}"
                                    if _sig_key2 in entered_today:
                                        log.info(f"  {symbol} [{tf}]: already entered by another thread -- skip")
                                        return
                                    if not pm.can_open(symbol):
                                        return
                                    buy_order = dhan.place_order(symbol, qty, "BUY")
                                    if buy_order:
                                        opened = pm.open(signal, buy_order.get("orderId", ""))
                                        if opened:
                                            # Fix #6: SL is now close-based (8 EMA candle close).
                                            # No SL-M exchange order — the bot monitors each
                                            # candle close and exits when close < 8 EMA.
                                            # (was: place SL-M at signal["stop_loss"])
                                            _log_action(symbol, tf, "ENTRY_LOCKED_BOX",
                                                        {"close": signal["close"],
                                                         "resistance": signal["resistance"],
                                                         "stop_loss": signal["stop_loss"],
                                                         "qty": qty})
                                            _send_bot_alert(f"✅ <b>{symbol}</b> [{tf}]: ENTRY (locked box)\nClose ₹{signal['close']} | Resistance ₹{signal['resistance']} | SL ₹{signal['stop_loss']}")
                                            entered_today.add(_sig_key2)
                                del watchlist_state[symbol]

                            else:
                                reasons = []
                                if rsi_val < 45:
                                    reasons.append(f"RSI {rsi_val:.1f} below 45")
                                if vol_ratio <= 1.0:
                                    reasons.append(f"vol {vol_ratio:.1f}x below avg")
                                if not above_200:
                                    reasons.append("close below 200 EMA")
                                log.info(f"  {symbol}: ceiling breached, "
                                         f"confirmations failed: {', '.join(reasons)} "
                                         f"-- removing")
                                del watchlist_state[symbol]

                        elif last["low"] < floor:
                            next_tf = TF_CASCADE.get(tf)
                            if next_tf:
                                log.info(f"  {symbol}: floor Rs{floor:.2f} breached "
                                         f"(low Rs{last['low']:.2f}) "
                                         f"-- escalating to [{next_tf}]")
                                watchlist_state[symbol] = {
                                    "tf":                 next_tf,
                                    "floor":              None,
                                    "ceiling":            None,
                                    "box_confirmed":      False,
                                    "added_date":         wl_data["added_date"],
                                    "box_confirmed_date": None,
                                }
                            else:
                                log.info(f"  {symbol}: floor breached on [{tf}] "
                                         f"-- end of ladder, removed")
                                del watchlist_state[symbol]

                        else:
                            log.info(f"  {symbol} [{tf}]: watching "
                                     f"floor Rs{floor:.2f} / ceil Rs{ceiling:.2f} | "
                                     f"last Rs{last['close']:.2f}")

                    else:
                        # --------------------------------------------------------
                        # BOX NOT YET CONFIRMED
                        # --------------------------------------------------------
                        signal, reason = detect_signal(df, symbol, tf)

                        if signal:
                            log.info(f"\n  SIGNAL: {symbol} [{tf}] -- {reason}")
                            log.info(f"    close=Rs{signal['close']} | "
                                     f"resistance=Rs{signal['resistance']}")
                            log.info(f"    first_low=Rs{signal['first_low']} | "
                                     f"RSI={signal['rsi']} | "
                                     f"vol={signal['vol_ratio']}x avg")
                            log.info(f"    SL=Rs{signal['stop_loss']} | "
                                     f"range={signal['range_candles']} candles | "
                                     f"clean={signal['cleanliness']}")

                            series = tv.get_series(symbol)
                            if series != "EQ":
                                log.info(f"  SKIP {symbol} [{tf}] -- "
                                         f"series={series or 'unresolved'}")
                                del watchlist_state[symbol]
                                time.sleep(1.0)
                                return

                            lev = cfg.get("leverage", 1)
                            qty = int((cfg["capital_per_trade"] * lev) / signal["close"])
                            if qty == 0:
                                log.warning(f"    Price Rs{signal['close']} > buying power")
                                del watchlist_state[symbol]
                                time.sleep(1.0)
                                return

                            with _trade_lock:
                                _sig_key2 = f"{symbol}_{now.strftime('%Y%m%d')}"
                                if _sig_key2 in entered_today:
                                    log.info(f"  {symbol} [{tf}]: already entered by another thread -- skip")
                                    return
                                if not pm.can_open(symbol):
                                    return
                                buy_order = dhan.place_order(symbol, qty, "BUY")
                                if buy_order:
                                    opened = pm.open(signal, buy_order.get("orderId", ""))
                                    if opened:
                                        # Fix #6: SL is now close-based (8 EMA candle close).
                                        # No SL-M exchange order — bot monitors each candle
                                        # close and exits when close < 8 EMA.
                                        _log_action(symbol, tf, "ENTRY_FRESH",
                                                    {"close": signal["close"],
                                                     "resistance": signal["resistance"],
                                                     "stop_loss": signal["stop_loss"],
                                                     "qty": qty})
                                        _send_bot_alert(f"✅ <b>{symbol}</b> [{tf}]: ENTRY (fresh)\nClose ₹{signal['close']} | Resistance ₹{signal['resistance']} | SL ₹{signal['stop_loss']}")
                                        entered_today.add(_sig_key2)
                            del watchlist_state[symbol]

                        elif reason.startswith("still consolidating"):
                            df_ind    = add_indicators(df)
                            touch_idx = find_200ema_touch(df_ind, symbol=symbol, tf=tf)
                            if touch_idx is not None:
                                first_low, fl_pos = find_first_low(df_ind, touch_idx)
                                box = build_range_box(df_ind, fl_pos)
                                if box is not None and box["breakout_pos"] is None:
                                    # Higher-TF consolidation guard: block box confirmation
                                    # if a higher TF is already consolidating — that TF
                                    # owns the move and a lower-TF box entry is premature.
                                    _htf = has_higher_tf_consolidation(tv, symbol, tf)
                                    if _htf:
                                        log.info(f"  SKIP {symbol} [{tf}] -- higher TF [{_htf}] consolidation active (box confirm)")
                                        del watchlist_state[symbol]
                                        time.sleep(1.0)
                                        return

                                    # -- Higher-TF EMA touch upgrade (EMCURE fix) ---
                                    # Before locking the box on this TF, check whether
                                    # the stock has touched the NEXT higher TF's 200 EMA
                                    # recently (last 2 trading days worth of that TF's
                                    # candles). If yes, the real setup belongs to the
                                    # higher TF — upgrade and let the box form there.
                                    # This catches cases where the higher-TF Chartink
                                    # screener didn't fire (candle-containment criteria
                                    # not met) but the EMA touch is visible in price data.
                                    _cur_tf_idx = TF_LADDER.index(tf) if tf in TF_LADDER else -1
                                    _next_tf = (
                                        TF_LADDER[_cur_tf_idx + 1]
                                        if 0 <= _cur_tf_idx < len(TF_LADDER) - 1
                                        else None
                                    )
                                    if _next_tf:
                                        # Lookback in higher-TF candles ≈ 2 trading days
                                        _htf_lb_map = {
                                            "15min": 40,   # 5min → 15min: ~40 candles
                                            "30min": 16,   # 15min → 30min: ~16 candles
                                            "45min": 12,   # 30min → 45min: ~12 candles
                                            "1H":     8,   # 45min → 1H:   ~8 candles
                                        }
                                        _htf_lb = _htf_lb_map.get(_next_tf, 16)
                                        _df_htf_raw = fetch_hist(tv, symbol, _next_tf)
                                        if _df_htf_raw is not None and len(_df_htf_raw) >= 50:
                                            _df_htf_ind = add_indicators(_df_htf_raw)
                                            if len(_df_htf_ind) >= _htf_lb:
                                                _recent_htf = _df_htf_ind.iloc[-_htf_lb:]
                                                # Primary: candle range contains the 200 EMA
                                                _ema_wick = (
                                                    (_recent_htf["low"] <= _recent_htf["ema200"]) &
                                                    (_recent_htf["ema200"] <= _recent_htf["high"])
                                                ).any()
                                                # Secondary: price crossed through the 200 EMA
                                                # (close went from one side to the other)
                                                _above_ema  = _recent_htf["close"] > _recent_htf["ema200"]
                                                _ema_cross  = (_above_ema != _above_ema.shift(1)).any()
                                                if _ema_wick or _ema_cross:
                                                    _touch_type = "wick" if _ema_wick else "close-cross"
                                                    log.info(
                                                        f"  {symbol} [{tf}]: recent {_next_tf} 200 EMA "
                                                        f"touch detected ({_touch_type}, last {_htf_lb} "
                                                        f"candles) — upgrading to [{_next_tf}] "
                                                        f"before box confirm"
                                                    )
                                                    _log_action(symbol, tf, "TF_UPGRADE_EMA_TOUCH",
                                                                {"new_tf":           _next_tf,
                                                                 "touch_type":       _touch_type,
                                                                 "lookback_candles": _htf_lb})
                                                    _send_bot_alert(f"\U0001f53c <b>{symbol}</b>: upgraded [{tf}] → [{_next_tf}]\nReason: {_next_tf} 200 EMA touched recently")
                                                    watchlist_state[symbol].update({
                                                        "tf":                 _next_tf,
                                                        "floor":              None,
                                                        "ceiling":            None,
                                                        "box_confirmed":      False,
                                                        "box_confirmed_date": None,
                                                    })
                                                    time.sleep(1.0)
                                                    return

                                    watchlist_state[symbol].update({
                                        "floor":              first_low,
                                        "ceiling":            box["ceiling"],
                                        "box_confirmed":      True,
                                        "box_confirmed_date": now.strftime("%Y-%m-%d"),
                                    })
                                    log.info(f"  {symbol} [{tf}]: box CONFIRMED -- "
                                             f"floor Rs{first_low:.2f} / "
                                             f"ceil Rs{box['ceiling']:.2f}")
                                else:
                                    log.info(f"  {symbol} [{tf}]: {reason}")
                            else:
                                log.info(f"  {symbol} [{tf}]: {reason}")

                        elif reason == "consolidation too brief (<3 candles)":
                            log.info(f"  {symbol} [{tf}]: forming box -- too brief yet")

                        elif "First Low" in reason and "broken" in reason:
                            next_tf = TF_CASCADE.get(tf)
                            if next_tf:
                                log.info(f"  {symbol}: first low broken on [{tf}] "
                                         f"-- escalating to [{next_tf}]")
                                watchlist_state[symbol] = {
                                    "tf":                 next_tf,
                                    "floor":              None,
                                    "ceiling":            None,
                                    "box_confirmed":      False,
                                    "added_date":         wl_data["added_date"],
                                    "box_confirmed_date": None,
                                }
                            else:
                                log.info(f"  {symbol}: first low broken on [{tf}] "
                                         f"-- end of ladder, removed")
                                del watchlist_state[symbol]

                        else:
                            log.info(f"  {symbol} [{tf}]: {reason}")
                            _TERMINAL = ("stale breakout", "box too wide")
                            if any(reason.startswith(t) for t in _TERMINAL):
                                del watchlist_state[symbol]
                                log.info(f"  {symbol} [{tf}]: removed -- terminal outcome, will re-discover if new setup forms")

                except Exception as e:
                    log.error(f"  Scan error {symbol}/{tf}: {e}")
                time.sleep(1.0)   # per-worker rate limit

            with ThreadPoolExecutor(max_workers=MAX_SCAN_WORKERS) as executor:
                list(executor.map(_scan_symbol, tf_stocks))

            save_watchlist_state(watchlist_state)

        # ── Monitor open positions ──────────────────────────────────────────

        # ── Monitor exits for open positions ────────────────────────────────
        for symbol in list(pm.positions.keys()):
            pos = pm.positions[symbol]
            try:
                df = fetch_hist(tv, symbol, pos["timeframe"], retries=2, pause=1.0)
                if df is None or len(df) < 10:
                    continue

                df   = add_indicators(df)
                if len(df) == 0:
                    continue
                last = df.iloc[-1]
                _exit_ema8 = last.get("ema8")
                if _exit_ema8 is None or pd.isna(_exit_ema8):
                    continue

                # Exit: last closed candle below 8 EMA
                if last["close"] < last["ema8"]:
                    log.info(f"\n  \U0001f4c9 EXIT: {symbol} | "
                             f"close ₹{round(float(last['close']),2)} < "
                             f"8EMA ₹{round(float(last['ema8']),2)}")
                    # Cancel existing SL order before placing market sell
                    sl_oid = pos.get("sl_order_id")
                    if sl_oid and not cfg["paper_trading"]:
                        dhan.cancel_order(sl_oid)
                    dhan.place_order(symbol, pos["quantity"], "SELL")
                    pm.close(symbol, float(last["close"]), "8_EMA_EXIT")

            except Exception as e:
                log.error(f"  Exit monitor error {symbol}: {e}")

        time.sleep(30)   # main loop: check every 30 seconds

    # ── End of session summary ───────────────────────────────────────────────
    total_pnl   = round(sum(t["pnl"] for t in pm.trades), 2)
    win_trades  = [t for t in pm.trades if t["pnl"] > 0]
    lose_trades = [t for t in pm.trades if t["pnl"] <= 0]
    log.info("\n" + "=" * 60)
    log.info(f"SESSION COMPLETE")
    log.info(f"Total trades : {len(pm.trades)}")
    log.info(f"Winners      : {len(win_trades)}")
    log.info(f"Losers       : {len(lose_trades)}")
    log.info(f"Total P&L    : ₹{total_pnl}")
    log.info("=" * 60)
    pm._save()

if __name__ == "__main__":
    run()
