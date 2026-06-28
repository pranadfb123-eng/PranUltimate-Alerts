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
from datetime import datetime, time as dtime
from dhan_data import DhanData, fetch_hist_dhan
from chartink import fetch_chartink_candidates, save_watchlist, load_watchlist

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH   = os.path.join(BASE_DIR, "..", "intraday_config.json")
TRADES_PATH   = os.path.join(BASE_DIR, "trades.json")
ACTIVE_WATCHLIST_PATH = os.path.join(BASE_DIR, "active_watchlist_state.json")
LOG_PATH      = os.path.join(BASE_DIR, f"bot_{datetime.now().strftime('%Y-%m-%d')}.log")
# CHANGED 2026-06-25: was a single ever-growing "bot.log" that mixed every
# day's runs together in one file (caused real confusion/bugs when
# analyzing "today's" activity -- date-filtering after the fact is
# unreliable because multi-line log entries don't all start with a
# timestamp). Now one fresh file per calendar day -- e.g.
# bot_2026-06-25.log -- so "today's log" is just "today's file," no
# filtering needed.
WATCHLIST_PATH = os.path.join(BASE_DIR, "..", "server", "watchlist.json")

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


def find_200ema_touch(df, search_window=400):
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

        # Confirm prior uptrend: in the ~20 candles before the touch,
        # price should have been predominantly ABOVE the 200 EMA
        lookback_start = max(0, i - 20)
        prior = df.iloc[lookback_start:i]
        if len(prior) < 5:
            continue
        above_count = (prior["close"] > prior["ema200"]).sum()
        if above_count < len(prior) * 0.6:   # at least 60% of prior candles above
            continue

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
    touch_idx = find_200ema_touch(df)
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
    ema20_rising = last["ema20"] > prev["ema20"]
    ema20_above  = last["ema20"] > last["ema50"]
    splitting    = ema20_rising and ema20_above
    above_200    = last["close"] > last["ema200"]

    if not (45 <= last["rsi14"] <= 70):
        return None, f"RSI {rsi_val} out of range (need 45–70)"
    if last["volume"] <= last["vol_avg"]:
        return None, f"volume {vol_ratio}x avg — below average"
    if not splitting:
        detail = "not rising" if not ema20_rising else "below 50 EMA"
        return None, f"20 EMA {detail} — not splitting upward"
    if not above_200:
        return None, (f"close ₹{round(float(last['close']), 2)} "
                      f"below 200 EMA ₹{round(float(last['ema200']), 2)}")

    range_candles = (len(df) - 1) - range_start
    cleanliness   = compute_cleanliness(df, touch_idx, fl_pos, range_start)

    return {
        "symbol":            symbol,
        "timeframe":         timeframe,
        "close":             round(float(last["close"]), 2),
        "resistance":        round(resistance, 2),
        "first_low":         round(first_low, 2),
        "stop_loss":         round(get_swing_low(df), 2),
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
            for j in range(breakout_pos + 1, n):
                if df.iloc[j]["low"] <= old_ceiling:
                    pullback_pos = j
                    break
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
    If yes → the intraday signal should be rejected (higher TF owns the move).

    Higher timeframes checked: 45min, 1H, 2H, 3H, 4H, 1D, 1W
    """
    # Map of higher timeframes to check (above the intraday 5/15/30)
    # Higher timeframes to check for consolidation. Capped at 4H —
    # Daily/Weekly consolidations are too far removed to block an intraday trade.
    HIGHER_TFS = ["45min", "1H", "2H", "3H", "4H"]

    for tf_label in HIGHER_TFS:
        df = fetch_hist(tv, symbol, tf_label, retries=2, pause=1.0)
        if df is None or len(df) < 220:
            continue
        if is_consolidating(df):
            return tf_label   # return which timeframe is consolidating

    return None   # not consolidating on any higher timeframe

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
            "symbol":      signal["symbol"],
            "timeframe":   signal["timeframe"],
            "entry_price": price,
            "quantity":    quantity,
            "stop_loss":   signal["stop_loss"],
            "sl_order_id": None,
            "buy_order_id": order_id,
            "leverage":    self.leverage,
            "margin_used": round(price * quantity / self.leverage, 2),
            "entry_time":  datetime.now().isoformat(),
        }
        position_value = round(price * quantity, 2)
        margin_used    = round(position_value / self.leverage, 2)
        log.info(f"  ✓ OPENED  {signal['symbol']} | "
                 f"qty={quantity} | entry=₹{price} | SL=₹{signal['stop_loss']}")
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
        log.info(f"  ✗ CLOSED  {symbol} | exit=₹{exit_price} | "
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
_last_scanned = {"5min": None, "15min": None, "30min": None}

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
def load_watchlist_extras(timeframe):
    try:
        if os.path.exists(WATCHLIST_PATH):
            with open(WATCHLIST_PATH) as f:
                wl = json.load(f)
            return wl.get(timeframe, [])
    except Exception:
        pass
    return []


def load_active_watchlist():
    """
    Loads the active watchlist saved from the previous session(s), so a
    real, still-valid box found yesterday (or several days ago) isn't
    forgotten just because the process restarted. This is DIFFERENT from
    chartink_watchlist.json -- that one is just a list of symbol names for
    tomorrow's 9:15-9:45 opening-gap merge. This one is the actual ongoing
    "I am tracking this symbol's box every cycle" state.

    Deliberately only restores the SYMBOL SET, not any stale floor/ceiling
    numbers -- the box itself gets recomputed fresh from live data on the
    very next cycle either way (is_consolidating / detect_signal always
    re-evaluate from current price data, never trust a saved number), so
    there's no risk of acting on an outdated level. If a symbol's setup
    quietly resolved overnight (broke out, broke down, or simply isn't
    valid anymore), the first real check today will correctly remove it.
    """
    try:
        if not os.path.exists(ACTIVE_WATCHLIST_PATH):
            return {tf: set() for tf in ["5min", "15min", "30min"]}
        with open(ACTIVE_WATCHLIST_PATH) as f:
            saved = json.load(f)
        return {tf: set(saved.get(tf, [])) for tf in ["5min", "15min", "30min"]}
    except Exception as e:
        log.warning(f"Could not load active watchlist state ({e}) — starting fresh.")
        return {tf: set() for tf in ["5min", "15min", "30min"]}


def save_active_watchlist(active_watchlist):
    """Persists the active watchlist so tomorrow's session picks up exactly
    where today left off, instead of forgetting every still-valid box."""
    try:
        serializable = {tf: sorted(syms) for tf, syms in active_watchlist.items()}
        with open(ACTIVE_WATCHLIST_PATH, "w") as f:
            json.dump(serializable, f, indent=2)
        return True
    except Exception as e:
        log.warning(f"Could not save active watchlist state: {e}")
        return False

# ── Main Bot Loop ──────────────────────────────────────────────────────────────
def run():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    mode = "PAPER TRADING" if cfg["paper_trading"] else "⚡ LIVE TRADING"
    log.info("=" * 60)
    log.info(f"PranUltimate Intraday Bot — {mode}")
    log.info(f"Capital/trade: ₹{cfg['capital_per_trade']} | Max positions: {cfg['max_positions']} | Leverage: {cfg.get('leverage', 1)}x")
    log.info(f"Square off: {cfg['square_off_time']} IST")
    log.info("=" * 60)

    dhan = DhanClient(cfg["client_id"], cfg["access_token"], cfg["paper_trading"])
    pm   = PositionManager(cfg["capital_per_trade"], cfg["max_positions"], leverage=cfg.get("leverage", 1))

    # Dhan official data API — stable, authenticated
    tv = DhanData(cfg["client_id"], cfg["access_token"])
    log.info("Dhan data API: connected")

    # Load yesterday's saved Chartink watchlist for the pre-9:45 morning merge.
    # Before 9:45 Chartink is stale; this ensures early morning setups on
    # yesterday's candidates are still caught.
    saved_watchlist = load_watchlist()   # {} if no file yet (first run)
    if saved_watchlist:
        total = sum(len(v) for v in saved_watchlist.values())
        log.info(f"Loaded saved Chartink watchlist: {total} candidates "
                 f"({', '.join(f'{tf}={len(v)}' for tf, v in saved_watchlist.items())})")
    else:
        log.info("No saved Chartink watchlist found — will use live Chartink only")

    sq_h, sq_m       = map(int, cfg["square_off_time"].split(":"))
    square_off_time  = dtime(sq_h, sq_m)
    entered_today    = set()   # signal_key → avoid re-entry same setup same day

    # ── Active watchlist (in-memory, this session only) ─────────────────────
    # Fixes the "stale breakout" discovery-lag bug confirmed 2026-06-24
    # (SHARDAMOTR case): a stock only gets checked when Chartink's live
    # screener happens to return it THAT cycle. If Chartink surfaces a stock
    # a few cycles late, its breakout candle has already passed by the time
    # the bot first sees it -- detect_signal correctly calls that "stale"
    # (breakout_pos != len(df)-1), but the real problem is upstream: the
    # bot never had the chance to check it on the actual breakout candle.
    #
    # Fix: once a symbol shows "still consolidating" (a real, valid box,
    # just not broken out yet), add it here and keep checking it directly
    # via Dhan EVERY cycle going forward, regardless of whether Chartink
    # mentions it again. This is discovery (Chartink) vs. tracking (direct
    # Dhan polling) -- two separate jobs that were previously conflated.
    # Scoped to 5min/15min/30min only per requirements (not 45min yet).
    active_watchlist = load_active_watchlist()
    for tf in ["5min", "15min", "30min"]:
        if active_watchlist[tf]:
            log.info(f"Restored {len(active_watchlist[tf])} symbols to active tracking on [{tf}] from previous session(s)")
    # If a box decisively breaks DOWN on one tracked timeframe, that doesn't
    # mean the stock is done -- the same setup may still be forming validly
    # on a higher timeframe. Cascade it up rather than dropping it entirely.
    TF_CASCADE = {"5min": "15min", "15min": "30min"}  # 30min has no cascade target in current scope

    while True:
        now = datetime.now()

        # ── Pre-market wait ─────────────────────────────────────────────────
        if not is_market_open(now):
            if now.time() < dtime(9, 0):
                log.info("Pre-market — sleeping 5 minutes")
                time.sleep(300)
            else:
                time.sleep(10)
            continue

        # ── Square off ──────────────────────────────────────────────────────
        if now.time() >= square_off_time:
            if pm.open_count > 0:
                log.info("⏰ SQUARE OFF — closing all open positions")
                for sym in list(pm.positions.keys()):
                    pos = pm.positions[sym]
                    dhan.place_order(sym, pos["quantity"], "SELL")
                    # Fetch current price so P&L is accurate, not always ₹0
                    df_sq = fetch_hist(tv, sym, pos["timeframe"])
                    sq_price = (float(df_sq.iloc[-1]["close"])
                                if df_sq is not None and len(df_sq) > 0
                                else pos["entry_price"])
                    pm.close(sym, sq_price, "SQUARE_OFF")

            # Persist today's Chartink candidates so tomorrow's pre-9:45
            # window can merge them with live (stale) data.
            log.info("Saving Chartink watchlist for tomorrow morning...")
            saved = save_watchlist()
            if saved:
                total = sum(len(v) for v in saved.values())
                log.info(f"Watchlist saved: {total} candidates → chartink_watchlist.json")
            else:
                log.warning("Watchlist save failed — Chartink may be unavailable")

            active_total = sum(len(v) for v in active_watchlist.values())
            if save_active_watchlist(active_watchlist):
                log.info(f"Active watchlist saved: {active_total} symbols → active_watchlist_state.json "
                         f"(carried forward to tomorrow's session)")
            else:
                log.warning("Active watchlist save failed")

            log.info("Market session ended.")
            break

        # ── Real-time scan at each candle close ─────────────────────────────
        for tf in ["5min", "15min", "30min"]:
            # Bail out of the timeframe loop immediately if square-off time has
            # arrived mid-scan — don't start a fresh universe scan this close.
            if datetime.now().time() >= square_off_time:
                break

            if not candle_just_closed(tf, now):
                continue

            # Build universe:
            #   5min / 15min → Chartink screener candidates (full NSE, 200 EMA touch)
            #   30min        → hardcoded universe MERGED with the new live
            #                  30min Chartink screener (added 2026-06-25 --
            #                  previously hardcoded-only since no 30min
            #                  screener existed yet). Chartink failure
            #                  degrades gracefully to hardcoded-only, same
            #                  as 5min/15min's fallback behavior.
            if tf in ("5min", "15min"):
                candidates = fetch_chartink_candidates(tf)

                # Before 9:45 Chartink data is stale from the previous day.
                # Merge in yesterday's saved watchlist so early morning setups
                # on known candidates are still detected.
                if now.hour == 9 and now.minute < 45:
                    saved = saved_watchlist.get(tf, [])
                    if saved:
                        before = len(candidates)
                        candidates = list(dict.fromkeys(candidates + saved))
                        added = len(candidates) - before
                        if added:
                            log.info(f"    Pre-9:45 morning merge: +{added} from saved watchlist")

                if candidates:
                    universe = list(dict.fromkeys(candidates))
                    log.info(f"\n── {tf} candle closed | Chartink returned {len(universe)} candidates ──")
                else:
                    # Chartink failed — fall back to hardcoded universe so we don't go blind
                    universe = list(dict.fromkeys(UNIVERSE[tf]))
                    log.warning(f"\n── {tf} candle closed | Chartink empty/failed, "
                                f"fallback to {len(universe)} hardcoded stocks ──")
            elif tf == "30min":
                hardcoded = UNIVERSE[tf] + load_watchlist_extras(tf)
                chartink_candidates = fetch_chartink_candidates(tf)
                universe = list(dict.fromkeys(hardcoded + chartink_candidates))
                if chartink_candidates:
                    log.info(f"\n── {tf} candle closed | scanning {len(universe)} stocks "
                              f"({len(hardcoded)} hardcoded + {len(chartink_candidates)} from Chartink) ──")
                else:
                    log.info(f"\n── {tf} candle closed | scanning {len(universe)} stocks "
                              f"(hardcoded only — Chartink empty/failed) ──")
            else:
                universe = list(dict.fromkeys(UNIVERSE[tf] + load_watchlist_extras(tf)))
                log.info(f"\n── {tf} candle closed | scanning {len(universe)} stocks ──")

            # Merge in stocks already under active tracking (real, valid
            # boxes found on a PREVIOUS cycle) regardless of whether
            # Chartink mentions them again this cycle -- this is what
            # actually catches a breakout fresh instead of discovering it
            # several candles late.
            tracked = active_watchlist[tf] - set(universe)
            if tracked:
                universe = universe + list(tracked)
                log.info(f"    +{len(tracked)} from active tracking (already known consolidating boxes)")

            for symbol in universe:
                # A single timeframe's scan (e.g. 242 stocks × ~1.5s) can take several
                # minutes. Check the clock every iteration so we never run a new
                # entry or delay square-off by minutes once 15:20 has passed.
                if datetime.now().time() >= square_off_time:
                    log.info(f"  ⏰ Square-off time reached mid-scan ({symbol}) — stopping scan early")
                    break

                sig_key = f"{symbol}_{tf}_{now.strftime('%Y%m%d')}"
                if sig_key in entered_today:
                    active_watchlist[tf].discard(symbol)
                    continue
                if not pm.can_open(symbol):
                    active_watchlist[tf].discard(symbol)
                    if pm.open_count >= pm.max_positions:
                        log.info("  Max positions reached — pausing scan")
                        break
                    continue

                try:
                    df = fetch_hist(tv, symbol, tf)
                    if df is None or len(df) < 50:
                        log.info(f"  {symbol}: no data from Dhan")
                        time.sleep(1.0)
                        continue

                    signal, reason = detect_signal(df, symbol, tf)

                    # ── Active watchlist membership update ──────────────────
                    # "still consolidating" = a real, valid box -- keep
                    # tracking it directly every cycle so the eventual
                    # breakout is caught fresh, not several candles late.
                    # Any other terminal outcome (stale, invalidated, no
                    # touch, too brief) means this box is no longer a live
                    # candidate -- stop tracking it. Transient failures
                    # ("no data from Dhan") leave membership unchanged so a
                    # single bad fetch doesn't drop a real, valid setup.
                    if signal:
                        # Fresh breakout -- this box is resolved one way or
                        # another (traded, vetoed, or BE-blocked downstream).
                        was_tracked = symbol in active_watchlist[tf]
                        active_watchlist[tf].discard(symbol)
                        if was_tracked:
                            log.info(f"    {symbol}: breakout resolved on [{tf}] — removed from watchlist")
                    elif reason.startswith("still consolidating"):
                        active_watchlist[tf].add(symbol)
                    elif reason.startswith("First Low") and "broken" in reason:
                        # Real breakdown (box failed DOWNWARD), not just "no
                        # signal yet". Don't forget the stock entirely --
                        # the same setup may still be forming validly on a
                        # higher timeframe (e.g. 5min breaks down, but the
                        # underlying move is really a 15min/30min one).
                        # Cascade it up so the same active-tracking process
                        # picks it up there.
                        active_watchlist[tf].discard(symbol)
                        next_tf = TF_CASCADE.get(tf)
                        if next_tf:
                            active_watchlist[next_tf].add(symbol)
                            log.info(f"    {symbol}: broke down on [{tf}] — now tracking on [{next_tf}]")
                        else:
                            # End of the cascade chain (30min has no further
                            # target in current scope) -- nowhere left to
                            # promote this to. Explicitly NO SETUP, not a
                            # silent drop.
                            log.info(f"    {symbol}: broke down on [{tf}] — end of cascade, classified NO SETUP")
                    else:
                        # Other terminal outcomes (stale breakout, no 200
                        # EMA touch, too brief) -- not a breakdown, just not
                        # a live candidate right now. No cascade.
                        was_tracked = symbol in active_watchlist[tf]
                        active_watchlist[tf].discard(symbol)
                        if was_tracked:
                            log.info(f"    {symbol}: no longer valid on [{tf}] ({reason}) — classified NO SETUP")

                    if not signal:
                        log.info(f"  {symbol}: {reason}")
                        time.sleep(1.0)
                        continue

                    # ── HIGHER TIMEFRAME CHECK ──────────────────────────────
                    # Reject if stock is consolidating on any higher timeframe —
                    # the higher timeframe owns the move, not this intraday one.
                    higher_tf = has_higher_tf_consolidation(tv, symbol, tf)
                    if higher_tf:
                        log.info(f"\n  ⊘ SKIP {symbol} [{tf}] — consolidating on higher TF [{higher_tf}]")
                        time.sleep(1.0)
                        continue

                    # ── SERIES CHECK: BE (trade-to-trade) can't trade intraday ─
                    # BE-series stocks typically require full delivery — they
                    # can't be sold same-day, which is incompatible with this
                    # bot's leveraged intraday model. The signal above is still
                    # valid on its own merits (useful for chart validation),
                    # it just can't be acted on here.
                    series = tv.get_series(symbol)
                    if series != "EQ":
                        log.info(f"\n  ⊘ SKIP {symbol} [{tf}] — series={series or 'unresolved'} "
                                 f"(not EQ, can't trade intraday) — signal was otherwise valid")
                        time.sleep(1.0)
                        continue

                    log.info(f"\n  ★ SIGNAL: {symbol} [{tf}] — {reason}")
                    log.info(f"    close=₹{signal['close']} | resistance=₹{signal['resistance']}")
                    log.info(f"    first_low=₹{signal['first_low']} | RSI={signal['rsi']} | vol={signal['vol_ratio']}x avg")
                    log.info(f"    SL=₹{signal['stop_loss']} | range={signal['range_candles']} candles | clean={signal['cleanliness']}")

                    lev = cfg.get("leverage", 1)
                    qty = int((cfg["capital_per_trade"] * lev) / signal["close"])
                    if qty == 0:
                        log.warning(f"    Price ₹{signal['close']} > buying power "
                                    f"₹{cfg['capital_per_trade'] * lev} — skip")
                        time.sleep(1.0)
                        continue

                    # Place BUY order
                    buy_order = dhan.place_order(symbol, qty, "BUY")
                    if not buy_order:
                        time.sleep(1.0)
                        continue

                    opened = pm.open(signal, buy_order.get("orderId", ""))
                    if opened:
                        # Place stop loss order
                        sl_order = dhan.place_order(
                            symbol, qty, "SELL",
                            order_type="SL-M",
                            trigger_price=signal["stop_loss"]
                        )
                        if sl_order:
                            pm.positions[symbol]["sl_order_id"] = sl_order.get("orderId")
                        entered_today.add(sig_key)

                except Exception as e:
                    log.error(f"  Scan error {symbol}/{tf}: {e}")

                time.sleep(1.0)   # rate limit

            # Periodic save (crash resilience) -- not just at clean
            # square-off. If the process dies mid-session for any reason,
            # today's tracking progress up to this point isn't lost.
            save_active_watchlist(active_watchlist)

        # ── Monitor exits for open positions ────────────────────────────────
        for symbol in list(pm.positions.keys()):
            pos = pm.positions[symbol]
            try:
                df = fetch_hist(tv, symbol, pos["timeframe"], retries=2, pause=1.0)
                if df is None or len(df) < 10:
                    continue

                df   = add_indicators(df)
                last = df.iloc[-1]

                # Exit: last closed candle below 8 EMA
                if last["close"] < last["ema8"]:
                    log.info(f"\n  📉 EXIT: {symbol} | "
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