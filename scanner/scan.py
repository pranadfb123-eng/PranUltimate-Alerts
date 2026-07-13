"""
PranUltimate Scanner
=====================
Scans the FULL NSE equity universe across all 10 PranUltimate timeframes
using the SAME validated box-model detection as the live intraday bot
(bot.py) — 200 EMA touch -> First Low -> box ceiling -> breakout.

Two categories of result per timeframe:
  - BREAKOUT-type (BREAKOUT / 1 CANDLE POST / 2 CANDLES POST) — confirmed
    breakout within the last 3 candles. Same higher-timeframe rejection rule
    as the intraday bot (45min-4H, Daily/Weekly excluded).
  - NEAR BREAKOUT — price still inside the box but within NEAR_BREAKOUT_PCT
    of the ceiling. Informational watchlist only, no higher-TF filter.

Data source: Dhan (reuses dhan_data.py from ../intraday/) — NOT tvDatafeed.
tvDatafeed was abandoned for the intraday bot due to unfixable websocket
timeouts on Windows; this scanner now uses the same stable Dhan layer.

Per-symbol fetch is optimized: only 4 raw API calls cover all 10 timeframes
(5min, 15min, 60min, daily — the rest are derived via resampling), and stocks
that fail the weekly-200-EMA filter are skipped after just 1 call.

Run at 4 PM daily via Task Scheduler.
"""

import json
import os
import sys
import time
import logging
from datetime import datetime

import pandas as pd
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, "..", "intraday"))
from dhan_data import DhanData  # noqa: E402  (path inserted above)
from sync_alerts import sync_alerts  # noqa: E402

# ── Logging ────────────────────────────────────────────────────────────────────
# Windows consoles often default to the legacy cp1252 codepage, which can't
# represent ★, ⊘, or even the ₹ rupee sign — without this, every log line
# containing one of those raises a UnicodeEncodeError (non-fatal, but noisy,
# and not something to rely on working by luck of whatever codepage happens
# to be active in a given terminal session).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "scan.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
CONFIG_PATH    = os.path.join(BASE_DIR, "..", "intraday_config.json")
OUTPUT_PATH    = os.path.join(BASE_DIR, "..", "server", "results.json")
PUBLIC_DIR     = os.path.join(BASE_DIR, "..", "public")
STATE_PATH     = os.path.join(BASE_DIR, "scanner_state.json")


def load_scanner_state():
    """
    Persisted memory of which timeframe/anchor each symbol was last assigned
    to, across BOTH pipelines ("sp" and "regular"). Added 2026-06-29 per
    explicit request: re-running full discovery from scratch every scan was
    causing a symbol's timeframe assignment to flip-flop confusingly between
    scans purely because new candles shifted which origin/fallback won --
    even when nothing about the underlying structure actually changed.

    Structure: {"sp": {symbol: entry}, "regular": {symbol: entry}}
    entry: {"tf": str, "anchor_date": str, "origin_kind": "strict"|"fallback",
            "status": "consolidating"|"breakout", "breakout_date": str|None}

    Returns a fresh empty structure on any failure (corrupt file, first run).
    """
    try:
        with open(STATE_PATH) as f:
            data = json.load(f)
        data.setdefault("sp", {})
        data.setdefault("regular", {})
        # Migrate legacy graduated_daily → graduated[symbol][tf] structure
        if "graduated_daily" in data and "graduated" not in data:
            data["graduated"] = {
                sym: {"1D": info}
                for sym, info in data.pop("graduated_daily").items()
            }
        else:
            data.pop("graduated_daily", None)
        data.setdefault("graduated", {})
        return data
    except Exception:
        return {"sp": {}, "regular": {}, "graduated": {}}


def save_scanner_state(state):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.warning(f"Could not save scanner_state.json: {e}")


def _anchor_date_to_fl_pos(df, anchor_date):
    """Map a persisted anchor date back to its CURRENT positional index in
    freshly-fetched data (positions shift as new candles arrive). Returns
    None if the anchor date isn't found at all (e.g. data provider hiccup,
    or the date has rolled out of the available history) -- caller should
    treat that as "persisted entry invalid, fall through to fresh discovery"."""
    try:
        ts = pd.Timestamp(anchor_date)
        if ts in df.index:
            return df.index.get_loc(ts)
        matches = df.index[df.index <= ts]
        if len(matches) == 0:
            return None
        return df.index.get_loc(matches[-1])
    except Exception:
        return None


def recheck_persisted_box(df, anchor_date):
    """
    Core re-check mechanic: instead of rediscovering a fresh origin, re-run
    build_range_box from the SAME persisted anchor position against fresh
    data. This naturally lets the ceiling continue to creep up (if new highs
    formed without closing above it) and correctly reports whether a
    breakout/breakdown has happened since -- using the exact same box logic
    as fresh discovery, just skipping the "which origin/timeframe wins"
    decision entirely.

    Returns (box, fl_pos, first_low) or (None, None, None) if the anchor
    can't be resolved against this data at all.
    """
    fl_pos = _anchor_date_to_fl_pos(df, anchor_date)
    if fl_pos is None:
        return None, None, None
    first_low = float(df.iloc[fl_pos]["low"])
    box = build_range_box(df, fl_pos)
    return box, fl_pos, first_low

def publish_to_github():
    """
    Copies the freshly-written results.json into the public/ folder (the
    GitHub Pages repo) and pushes it, so the live dashboard
    (https://pranadfb123-eng.github.io/PranUltimate/) reflects this scan
    without needing the laptop on or any manual git commands.

    Best-effort: logs a warning and returns False on any failure (network
    down, git not configured, nothing changed, etc.) rather than crashing
    the scan -- publishing the dashboard should never take down the actual
    scan/trading pipeline.
    """
    try:
        import shutil
        import subprocess

        dest = os.path.join(PUBLIC_DIR, "results.json")
        shutil.copy2(OUTPUT_PATH, dest)

        def run(cmd):
            return subprocess.run(
                cmd, cwd=PUBLIC_DIR, capture_output=True, text=True, timeout=60
            )

        run(["git", "add", "results.json"])
        commit = run(["git", "commit", "-m",
                      f"Update results — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"])
        if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr):
            log.warning(f"GitHub publish: commit failed — {commit.stderr.strip()}")
            return False

        push = run(["git", "push", "origin", "main"])
        if push.returncode != 0:
            log.warning(f"GitHub publish: push failed — {push.stderr.strip()}")
            return False

        log.info("Published to GitHub Pages — dashboard updated.")
        return True

    except Exception as e:
        log.warning(f"GitHub publish: skipped — {e}")
        return False


def publish_alerts_repo():
    """
    Commits and pushes server/alerts_state.json + server/results.json to the
    PranUltimate-Alerts repo (the SEPARATE repo at the PranUltimate root —
    not public/, which is its own independent git repo for GitHub Pages).

    Run right after sync_alerts() so every new alert candidate added by a
    scan is immediately pushed and available to the GitHub Actions checker,
    with zero manual `git add/commit/push` needed.

    Best-effort, same as publish_to_github(): logs a warning and returns
    False on any failure rather than crashing the scan.
    """
    try:
        import subprocess

        repo_root = os.path.join(BASE_DIR, "..")  # PranUltimate/ root, NOT public/

        def run(cmd):
            return subprocess.run(
                cmd, cwd=repo_root, capture_output=True, text=True, timeout=60
            )

        # Pull first so GitHub Actions' "Update alert state" commits don't
        # cause a non-fast-forward rejection when we push.
        pull = run(["git", "pull", "--rebase", "origin", "main"])
        if pull.returncode != 0:
            log.warning(f"Alerts repo publish: pull --rebase failed — {pull.stderr.strip()}")
            # Don't abort — attempt the push anyway; worst case it fails too.

        run(["git", "add", "server/alerts_state.json", "server/results.json"])
        commit = run(["git", "commit", "-m",
                      f"Update alerts/results — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"])
        if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr):
            log.warning(f"Alerts repo publish: commit failed — {commit.stderr.strip()}")
            return False

        push = run(["git", "push", "origin", "main"])
        if push.returncode != 0:
            log.warning(f"Alerts repo publish: push failed — {push.stderr.strip()}")
            return False

        log.info("Pushed alerts_state.json + results.json to PranUltimate-Alerts repo.")
        return True

    except Exception as e:
        log.warning(f"Alerts repo publish: skipped — {e}")
        return False
WATCHLIST_PATH = os.path.join(BASE_DIR, "..", "server", "watchlist.json")
SYMBOL_FILE    = os.path.join(BASE_DIR, "nse_symbols.txt")

TIMEFRAMES = ["1H", "2H", "3H", "4H", "1D", "1W"]
TF_PRIORITY = TIMEFRAMES  # same order = higher index treated as "higher timeframe"

# Near-breakout window: flag stocks below the ceiling but within this % of it.
NEAR_BREAKOUT_PCT = 5.0

# A 200-EMA touch only counts as a structure ORIGIN if the prior uptrend traded
# at least this far above the EMA (median), filtering out mid-consolidation
# grazes where flat price and the catching-up EMA hug each other. 0.03 = 3%.
ORIGIN_ABOVE_MARGIN = 0.03

# How many candles back a breakout can be and still be reported (0=current,
# 1=one candle ago, 2=two candles ago). Lets a once-daily batch scan still
# surface breakouts that fired between scans, not just at this exact instant.
# Also doubles as the SP "expiry" threshold: a breakout older than this on a
# timeframe means that timeframe is done for the stock (look to lower TFs).
MAX_BREAKOUT_AGE = 2

# ── SP Stocks ──────────────────────────────────────────────────────────────────
# A hand-picked watchlist scanned across ALL scanner timeframes (1H-1W). Each
# stock gets ONE row showing its highest-timeframe setup (breakout or near-
# breakout), or an explicit NO SETUP if it has no valid box on any timeframe.
# Symbols that don't resolve against Dhan's master will log an [unresolved]
# suggestion (see dhan_data.py) — add confirmed renames to that file's ALIAS_MAP.
SP_STOCKS = [
    "ACE", "AIAENG", "AJAXENGG", "AMBUJACEM", "ANANDRATHI", "ANGELONE", "APOLLO",
    "AVANTEL", "AXISCADES", "BANDHANBNK", "BEL", "BGRENERGY", "BSE", "CAMS",
    "CARBORUNIV", "CDSL", "CERA", "CMSINFO", "COCHINSHIP", "CRAFTSMAN", "CRISIL",
    "CRIZAC", "DIXON", "ELECON", "FINPIPE", "FORCEMOT", "FOSECOIND", "GMRAIRPORT",
    "GMRP&UI", "GRSE", "HAL", "HINDCOPPER", "IDEA", "IDEAFORGE", "IKS", "INDHOTEL",
    "INDIGO", "INFY", "INGERRAND", "INOXINDIA", "INOXWIND", "IVALUE", "JINDALSAW",
    "JKIL", "KIRLOSENG", "KNRCON", "KRYSTAL", "LT", "M&M", "MAZDOCK", "MBEL",
    "MCLEODRUSS", "MEIL", "MOTILALOFS", "OMPOWER", "OSWALPUMPS", "PNGSREVA",
    "POLYCAB", "PREMIERENE", "RAIN", "RATEGAIN", "RKFORGE", "RUBICON", "SALZERELEC",
    "SANDHAR", "SANGHVIMOV", "SHAILY", "SHAKTIPUMP", "SHILCTECH", "SHRIRAMFIN",
    "SMLMAH", "SOLARINDS", "SPORTKING", "SUVEN", "SUZLON", "SWANDEF", "SWARAJENG",
    "TAJGVK", "TALBROAUTO", "TARIL", "TATAINVEST", "TDPOWERSYS", "THANGAMAYL",
    "TMCV", "TMPV", "TRANSRAILL", "TRENT", "UTLSOLAR", "VENTIVE", "VOLTAMP",
    "VOLTAS", "WEBELSOLAR", "WENDT", "WOCKPHARM", "YESBANK", "ZEEL", "ZENTEC",
]


# ── EMA / RSI (identical to bot.py — validated against today's live signals) ──
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


# ── PranUltimate Box Model (ported verbatim from bot.py — same logic that ─────
#    produced today's validated DVL / TORNTPOWER signals) ─────────────────────
def find_200ema_touch(df, search_window=400):
    n = len(df)
    if n < 60:
        return None
    end = n - 2
    earliest = max(30, end - search_window)
    for i in range(end, earliest, -1):
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
        return i
    return None


def find_first_low(df, touch_idx, max_idx=None):
    n = len(df)
    win_start = max(0, touch_idx - 3)
    win_end   = min(n - 1, touch_idx + 8)
    if max_idx is not None:
        # Cap the lookahead so it can never reach into territory that
        # belongs to a NEWER, already-accepted touch candidate -- prevents
        # a later crash from being silently absorbed into an earlier
        # touch's "first low" window (the exact failure mode that made a
        # naive full chain-walk run all the way back to 2025 on BGR).
        win_end = min(win_end, max_idx)
    window    = df.iloc[win_start:win_end + 1]
    fl_idx   = window["low"].idxmin()
    fl_price = float(df.loc[fl_idx, "low"])
    fl_pos   = df.index.get_loc(fl_idx)
    return fl_price, fl_pos


def find_first_200ema_touch(df):
    """
    SCANNER-ONLY. Anchors the box to the correct structural origin using a
    candidate-cluster comparison (validated against BGRENERGY Daily, KSL
    Weekly, and JBMA/BHAGCHEM/TCPLPACK Weekly this session):

      1. Walk backward collecting every candidate touch that comes "from
         above" (prior window sustained above EMA200 by margin, per the
         original logic).
      2. Collapse consecutive-candle runs into ONE representative per
         distinct touch EVENT (a flat EMA causes many adjacent candles to
         each pass individually -- these are the same event, not separate
         origins).
      3. Walk the FULL chain from most-recent backward: at each step,
         check whether the OLDER candidate's structure was decisively
         broken before the current "best" (newer) candidate occurred. If
         not broken, the newer touch is just a wobble inside the same
         still-ongoing base -- extend back to the older one and keep
         walking. Stop the moment a real break is found, or candidates run
         out.

         CRITICAL SAFETY BOUND: each step's find_first_low call is capped
         with max_idx=<the newer candidate's index> so its lookahead
         window can never reach into territory "owned" by a candidate
         that's already been superseded. Without this bound, a naive full
         chain-walk silently absorbs a LATER crash into an EARLIER touch's
         "first low" window, making everything look falsely unbroken all
         the way back through unrelated, much older structures (confirmed
         failure mode on BGR when first tried without this bound -- walked
         all the way back to 2025). With the bound in place, the chain can
         now safely walk multiple steps instead of just one -- fixing a
         separate confirmed bug where JBMA/BHAGCHEM/TCPLPACK Weekly were
         stopping at a recent intermediate wobble instead of the real,
         much older origin of a long-running consolidation (visually
         obvious on the real chart, missed by the old single-step-only
         version of this function).

    Confirmed on BGRENERGY Daily: correctly anchors to 2026-04-07 (origin of
    the real structure) instead of 2026-05-08 (a wobble), WITHOUT
    over-extending to the unrelated 2025 structure. Confirmed on KSL Weekly
    (unaffected, same conclusion as before). Confirmed on JBMA/BHAGCHEM/
    TCPLPACK Weekly: now correctly extends back through multiple
    intermediate wobbles to the real, long-running origin instead of
    stopping one step too early.
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
        prior_ema = prior["ema200"].replace(0, float("nan"))
        rel = ((prior["close"] - prior_ema) / prior_ema).median()

        if above_count >= len(prior) * 0.6 and rel >= ORIGIN_ABOVE_MARGIN:
            candidates.append(i)
            continue

        # Immediate 20-candle window failed. This can happen legitimately when
        # a stock made a SHARP correction toward the EMA — the descent itself
        # contaminates the lookback with closes near/below the EMA, even though
        # the prior uptrend is unmistakably real. Check the extended window
        # (20–60 bars before the touch, i.e. the pre-correction zone). If that
        # window shows a clear uptrend above EMA, the touch is still valid.
        # Confirmed failure case: SILVERTUC 1H (uptrend to 212, sharp descent
        # to 177/EMA, last 20 candles before touch all descending toward EMA).
        ext_start   = max(0, i - 60)
        ext_end     = max(0, i - 20)
        extended    = df.iloc[ext_start:ext_end]
        if len(extended) < 5:
            continue
        ext_above   = (extended["close"] > extended["ema200"]).sum()
        ext_ema_col = extended["ema200"].replace(0, float("nan"))
        ext_rel     = ((extended["close"] - ext_ema_col) / ext_ema_col).median()
        if ext_above >= len(extended) * 0.6 and ext_rel >= ORIGIN_ABOVE_MARGIN:
            candidates.append(i)

    if not candidates:
        return None

    # Collapse consecutive-candle runs into one representative per distinct
    # touch event (candidates is in descending/most-recent-first order).
    clusters = []
    prev = None
    for c in candidates:
        if prev is not None and prev - c == 1:
            prev = c
            continue
        clusters.append(c)
        prev = c

    best = clusters[0]
    for cand in clusters[1:]:
        # Bound find_first_low's lookahead at `best` (the newer candidate
        # we're currently comparing against) so it can't reach past it.
        fl_price, fl_pos = find_first_low(df, cand, max_idx=best)
        broken = first_low_decisively_broken(df, fl_price, fl_pos, best)
        if broken:
            break  # `best` stands as the real origin -- stop here
        best = cand  # not broken -- extend back, keep walking

    return best


def first_low_decisively_broken(df, first_low, fl_pos, breakout_pos):
    """
    SCANNER-ONLY relaxed invalidation. The box is only invalidated if price
    BROKE the first low AND STAYED below it — a single dip that recovers does
    not kill a long higher-timeframe consolidation.

    "Decisively broken" = 3+ consecutive candle CLOSES below the first low.
    A lone wick or single close below (that recovers next candle) is tolerated.
    """
    check_end = breakout_pos if breakout_pos is not None else len(df)
    segment = df.iloc[fl_pos + 1: check_end]
    if len(segment) == 0:
        return False
    below = segment["close"] < first_low
    run = 0
    for val in below:
        run = run + 1 if val else 0
        if run >= 3:
            return True
    return False


def build_range_box(df, fl_pos):
    """
    Seeds a ceiling from the candle right after the first low and walks
    forward looking for either a breakout (close > ceiling) or the end of
    data. Two distinct re-anchoring rules, both validated against
    BGRENERGY Daily and KSL Weekly this session:

      1. NOISE-SPIKE rule: if a "breakout" fires within 3 candles of
         range_start, that's mean-reversion noise off a sharp V-bottom (the
         first bounce candle(s) spike, closing above the single-candle seed
         ceiling almost immediately) -- NOT a real breakout. Re-anchor at
         the spike candle and keep walking forward.

      2. PULLBACK rule: if a REAL breakout fires (>=3 candles from
         range_start), check whether price EVER comes back down and
         re-enters the old range (low <= old ceiling) at any point
         afterward.
           - If yes: genuine pullback -- a distinct new base may have
             started there. Re-anchor at the pullback candle and keep
             building forward, so a LATER, CURRENT consolidation (formed
             after an old, already-resolved breakout) is what gets
             returned -- not the old, stale breakout itself.
           - If no: price broke out and kept going / stayed above without
             ever coming back -- that's a genuine continuation of the same
             move, not a separate structure. Return this breakout as final
             (this is what keeps a stock that broke out and is just
             drifting sideways NEAR (not back inside) its breakout level
             from being mistaken for "still in the old box").

    Both rules strictly increase range_start each iteration (the noise
    spike's breakout_pos, or the pullback_pos, is always > the current
    range_start), so this terminates within n iterations.

    Confirmed on BGRENERGY Daily: chains past the April 8 real breakout
    (price repeatedly pulled back below it afterward) into the current,
    still-consolidating ~350 ceiling box -- matching the real chart.
    Confirmed unaffected on KSL Weekly: same final ceiling/conclusion as
    before this fix (KSL never had a real breakout to chain past).
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
                # Early-spike noise — re-anchor from the spike candle and
                # keep looking forward for the next real box.
                range_start = breakout_pos
                continue

            # Real breakout. Did price ever come back down into the old
            # range afterward?
            old_ceiling = ceiling
            pullback_pos = None
            for j in range(breakout_pos + 1, n):
                if df.iloc[j]["low"] <= old_ceiling:
                    pullback_pos = j
                    break

            if pullback_pos is not None:
                # Genuine pullback -- look for the current/newer box that
                # may have formed since.
                range_start = pullback_pos
                continue

            # No pullback -- this breakout is a real, standing continuation.
            return {"ceiling": ceiling, "breakout_pos": breakout_pos, "range_start": range_start}
        else:
            if (n - 1) - range_start < 3:
                return None
            return {"ceiling": ceiling, "breakout_pos": None, "range_start": range_start}

    return None


def compute_cleanliness(df, touch_idx, fl_pos, range_start):
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
    last  = df.iloc[-1]["close"]
    lows  = df.tail(n)["low"]
    below = lows[lows < last]
    if below.empty:
        return float(df.tail(n)["low"].min())
    return float(below.min())


# Maximum age (in candles) a 200 EMA touch is allowed to be for `is_consolidating`
# to treat it as anchoring a CURRENT structure. Prevents ancient touches
# (e.g. a 2021 weekly wick on IPCALAB) from claiming ownership of a modern
# daily/intraday breakout via has_higher_tf_consolidation.
#   1W : 104 candles = ~2 years of weekly bars
#   1D : 500 candles = ~2 years of trading days
#   4H/3H/2H/1H : history windows are smaller anyway, 250 candles is safe
_MAX_TOUCH_AGE_CANDLES = {
    "1W": 104,
    "1D": 500,
    "4H": 250,
    "3H": 250,
    "2H": 250,
    "1H": 250,
}


def is_consolidating(df, timeframe=None):
    """
    SCANNER version. Uses the FIRST (oldest) 200-EMA touch of the current
    structure to anchor the box (so a long higher-TF consolidation isn't
    mistaken for a recent-dip box), and the relaxed "decisively broken"
    invalidation. Once a real breakout above the structure's ceiling has
    happened, it's no longer consolidating.

    `timeframe` is used to enforce a recency cap on the 200 EMA touch:
    a touch from more than _MAX_TOUCH_AGE_CANDLES[tf] bars ago is too
    old to represent the current structure and is rejected.
    """
    if len(df) < 220:
        return False
    df = add_indicators(df)
    if len(df) < 60:
        return False
    touch_idx = find_first_200ema_touch(df)
    if touch_idx is None:
        return False
    # Recency gate: reject stale anchors (e.g. a 2021 weekly wick on a
    # stock whose current consolidation has nothing to do with that touch)
    if timeframe is not None:
        max_age = _MAX_TOUCH_AGE_CANDLES.get(timeframe, len(df))
        touch_age = (len(df) - 1) - touch_idx
        if touch_age > max_age:
            return False
    first_low, fl_pos = find_first_low(df, touch_idx)
    box = build_range_box(df, fl_pos)
    if box is None:
        return False
    if first_low_decisively_broken(df, first_low, fl_pos, box["breakout_pos"]):
        return False
    if box["breakout_pos"] is not None:
        return False
    return True


def higher_tfs_for(current_tf):
    """
    Which timeframes count as "higher" for the rejection check, relative to
    whichever timeframe a breakout fired on.

    SCANNER BEHAVIOR (differs from the intraday bot on purpose): every
    timeframe checks ALL timeframes strictly above it in the full
    5min→…→1W chain — including Daily checking Weekly, and 1H checking
    2H/3H/4H/Daily/Weekly. This is what catches cases like a Daily breakout
    on HAL/KSL that's really owned by an in-progress Weekly setup, or a 1H
    breakout on Subros owned by a higher intraday timeframe.

    The live intraday bot deliberately does NOT do this — it caps at 4H and
    excludes Daily/Weekly, because for a leveraged same-day entry those higher
    frames are too far removed. That logic lives in bot.py and is untouched;
    this function only governs the scanner/website.
    """
    if current_tf not in TF_PRIORITY:
        return []
    idx = TF_PRIORITY.index(current_tf)
    return TF_PRIORITY[idx + 1:]


def has_higher_tf_consolidation(frames, current_tf):
    """
    Same purpose as the intraday bot's check, but reuses frames already
    fetched for this symbol — zero extra API calls, since every timeframe
    was pulled in one pass per symbol.
    """
    for tf_label in higher_tfs_for(current_tf):
        df = frames.get(tf_label)
        if df is None or len(df) < 220:
            continue
        if is_consolidating(df, timeframe=tf_label):
            return tf_label
    return None


# ── Signal Detection: confirmed breakout + near-breakout ───────────────────────
def detect_signal(df, symbol, timeframe, daily_df=None):
    """
    Runs the box model on one timeframe's data.

    Returns (category, payload):
      ("breakout", signal_dict)       — confirmed breakout within last 3 candles
      ("near_breakout", signal_dict)  — inside the box, within NEAR_BREAKOUT_PCT of ceiling
      (None, reason_str)              — rejected, with reason

    For 1H/2H specifically: also requires the stock's DAILY close to be
    above its DAILY 200 EMA (added 2026-06-29 per explicit request — too
    many 1H/2H "setups" were really just noise on stocks with no real daily
    uptrend context behind them). Not applied to 3H/4H/1D/1W.
    """
    if len(df) < 220:
        return None, "not enough candles"

    if timeframe in ("1H", "2H") and daily_df is not None:
        d = add_indicators(daily_df)
        if len(d) > 0:
            last_d = d.iloc[-1]
            if last_d["close"] <= last_d["ema200"]:
                return None, "daily close not above daily 200 EMA — 1H/2H setup ignored"

    df = add_indicators(df)
    if len(df) < 60:
        return None, "not enough candles after EMA warmup"

    touch_idx = find_first_200ema_touch(df)
    if touch_idx is None:
        return None, "no 200 EMA touch"

    # Recency gate: same cap used by is_consolidating — an ancient touch
    # (e.g. IPCALAB's 2021 weekly wick) should not anchor a current signal.
    _max_age = _MAX_TOUCH_AGE_CANDLES.get(timeframe, len(df))
    if (len(df) - 1) - touch_idx > _max_age:
        return None, f"200 EMA touch too old ({(len(df)-1)-touch_idx} candles ago, max {_max_age})"

    first_low, fl_pos = find_first_low(df, touch_idx)
    box = build_range_box(df, fl_pos)
    if box is None:
        return None, "consolidation too brief"

    resistance   = box["ceiling"]
    breakout_pos = box["breakout_pos"]
    range_start  = box["range_start"]

    # First Low must not be DECISIVELY broken (a single recovering dip is OK).
    if first_low_decisively_broken(df, first_low, fl_pos, breakout_pos):
        return None, "First Low decisively broken"

    # ── Range width cap: ceiling must not be more than 50% above first_low ──
    # A wider range is a trend leg, not a consolidation box.
    if first_low > 0 and (resistance - first_low) / first_low > 0.50:
        pct_wide = round((resistance - first_low) / first_low * 100, 1)
        return None, f"box too wide ({pct_wide}% range — max 50%)"

    range_candles = (len(df) - 1) - range_start
    cleanliness   = compute_cleanliness(df, touch_idx, fl_pos, range_start)

    # ── Case 1: still inside the box — NEAR BREAKOUT or WATCHING ──────────
    if breakout_pos is None:
        last  = df.iloc[-1]
        close = float(last["close"])
        if close <= 0 or resistance <= close:
            return None, "still consolidating"
        distance_pct = (resistance - close) / close * 100

        # "WATCHING" = valid strict-origin setup, still in the box, but more
        # than NEAR_BREAKOUT_PCT from the ceiling. Track it for alerts so we
        # fire when it eventually breaks out (even if not yet "close"). This
        # was the failure case for SILVERTUC 1H: valid uptrend → EMA touch →
        # consolidation, but too far from ceiling to qualify as NEAR BREAKOUT,
        # so it was silently dropped and no alert ever fired.
        status_label = (
            f"NEAR BREAKOUT ({round(distance_pct, 1)}%)"
            if distance_pct <= NEAR_BREAKOUT_PCT
            else f"WATCHING ({round(distance_pct, 1)}%)"
        )

        return "near_breakout", {
            "symbol":        symbol,
            "timeframe":     timeframe,
            "status":        status_label,
            "distance_pct":  round(distance_pct, 1),
            "close":         round(close, 2),
            "resistance":    round(resistance, 2),
            "first_low":     round(first_low, 2),
            "anchor_date":   str(df.index[fl_pos]),
            "rsi":           round(float(last["rsi14"]), 1),
            "volume":        int(last["volume"]),
            "vol_avg":       int(last["vol_avg"]),
            "ema200":        round(float(last["ema200"]), 2),
            "range_candles": range_candles,
            "cleanliness":   cleanliness,
            "timestamp":     str(df.index[-1]),
        }

    # ── Case 2: breakout already happened — only report if recent ──────────
    candles_ago = (len(df) - 1) - breakout_pos
    if candles_ago > MAX_BREAKOUT_AGE:
        return None, "stale breakout"

    candle = df.iloc[breakout_pos]
    prev_candle = df.iloc[breakout_pos - 1]

    rsi_ok       = 45 <= candle["rsi14"] <= 70
    volume_ok    = candle["volume"] > candle["vol_avg"]
    ema20_rising = candle["ema20"] > prev_candle["ema20"]
    ema20_above  = candle["ema20"] > candle["ema50"]
    above_200    = candle["close"] > candle["ema200"]

    if not (rsi_ok and volume_ok and ema20_rising and ema20_above and above_200):
        return None, "breakout failed confirmation filters (RSI/volume/EMA split)"

    status = ["BREAKOUT", "1 CANDLE POST BREAKOUT", "2 CANDLES POST BREAKOUT"][candles_ago]

    return "breakout", {
        "symbol":        symbol,
        "timeframe":     timeframe,
        "status":        status,
        "close":         round(float(candle["close"]), 2),
        "resistance":    round(resistance, 2),
        "first_low":     round(first_low, 2),
        "stop_loss":     round(get_swing_low(df.iloc[:breakout_pos + 1]), 2),
        "rsi":           round(float(candle["rsi14"]), 1),
        "volume":        int(candle["volume"]),
        "vol_avg":       int(candle["vol_avg"]),
        "ema200":        round(float(candle["ema200"]), 2),
        "range_candles": range_candles,
        "cleanliness":   cleanliness,
        "candles_ago":   candles_ago,
        "timestamp":     str(df.index[breakout_pos]),
    }


def find_fallback_low(df, min_start=30):
    """
    CHOPPY STOCKS TAB + SP STOCKS FALLBACK. Used when find_first_200ema_touch
    finds NO valid origin -- i.e. the stock never had a sharp prior uptrend
    clearly above the 200 EMA (margin >= 3%), it's just been choppy/flat the
    whole visible window (confirmed case this session: KAMAHOLD 1H/45min,
    every candidate maxed out around 1.8% margin, never cleared 3%).

    Rather than concluding "no structure" -- which is correct for the
    regular tabs (they require a real prior uptrend, by design) -- this
    treats the visible window as one big box: anchor at the single LOWEST
    low in the analyzable range, and let build_range_box find the ceiling
    (resistance of the chop) and whether it's broken out.

    RECENCY CUTOFF added 2026-06-29 (BGRENERGY Daily case): the original
    version used the ENTIRE available history (minus a 30-candle warmup
    trim) as the search window. That's fine for short intraday datasets
    (KAMAHOLD 1H, ~259 candles spanning a few weeks -- already entirely
    "recent"), but for a multi-YEAR Daily history on a stock that's only
    ever climbed since listing, it just re-finds the same ancient, already-
    resolved low every time (BGR: kept anchoring on a 2025-03 low even
    though the stock's CURRENT range is a year+ later and totally
    unrelated). A calendar-based cutoff (not a candle-count one) fixes both
    cases correctly: short datasets are already all within the cutoff, so
    nothing changes for them; long datasets get correctly restricted to a
    genuinely recent window.

    min_start lets a caller (see find_fallback_low_staircase below) push
    the search window forward past a previously-found, already-resolved
    breakout, instead of always starting at the same fixed 30-candle trim.

    Returns the positional index of that lowest low, or None.
    """
    n = len(df)
    if n < 60:
        return None
    window = df.iloc[max(30, min_start):n - 1]
    if len(window) == 0:
        return None

    # Calendar-based recency cutoff: restrict to roughly the last 180 days
    # of actual trading dates, not the last K candles. For short intraday
    # datasets this is a no-op (everything's already within 180 days); for
    # multi-year Daily/Weekly histories it correctly excludes ancient,
    # already-resolved structure.
    # Calendar-based recency cutoff -- but scaled to the timeframe's OWN
    # candle spacing, not a flat day count. A flat 180-day cutoff is ~120+
    # candles on Daily (fine) but only ~25 candles on Weekly (confirmed too
    # short on BHAGCHEM, 2026-06-29: cut off a real 6+ month consolidation
    # that the chart clearly showed). Guarantee AT LEAST 60 candles of
    # lookback, but never less than 180 calendar days either way (keeps
    # BGR's fix intact on Daily/intraday).
    if len(window) >= 2:
        median_gap_days = pd.Series(window.index).diff().median().total_seconds() / 86400
    else:
        median_gap_days = 1
    cutoff_days = max(180, median_gap_days * 60)
    cutoff_date = window.index[-1] - pd.Timedelta(days=cutoff_days)
    recent_window = window[window.index >= cutoff_date]
    if len(recent_window) > 0:
        window = recent_window

    lowest_label = window["low"].idxmin()
    return df.index.get_loc(lowest_label)


def find_fallback_low_staircase(df, max_breakout_age):
    """
    Wraps find_fallback_low to handle the "staircase" pattern: price breaks
    out for real, then keeps climbing WITHOUT ever pulling back into the
    old range, then does it again on a higher shelf, etc. (confirmed on
    both BGRENERGY Daily -- multi-year staircase -- and BHAGCHEM Daily --
    a smaller, one-step version: broke ₹232 in April, never looked back,
    so the CURRENT ₹244-298 range never got considered).

    build_range_box's own pullback-chaining can ONLY re-anchor past a
    breakout if price comes back down into that SPECIFIC breakout's old
    ceiling. When it climbs in disconnected shelves instead, there's
    nothing to chain through -- the fallback just reports the oldest shelf
    as an "expired breakout" and stops. This wrapper retries the SEARCH
    itself (not the box-building) starting just after each expired
    breakout, until it finds a shelf that's either still live
    (consolidating or a fresh breakout) or runs out of data.

    Returns the SAME (fl_pos) format as find_fallback_low — just possibly
    on a later shelf.
    """
    min_start = 30
    n = len(df)
    seen = set()
    for _ in range(6):  # hard cap -- a handful of shelves is plenty in practice
        fl_pos = find_fallback_low(df, min_start=min_start)
        if fl_pos is None or fl_pos in seen:
            return fl_pos
        seen.add(fl_pos)

        box = build_range_box(df, fl_pos)
        if box is None:
            return fl_pos  # too brief -- let the caller report that as-is

        bo = box["breakout_pos"]
        if bo is None:
            return fl_pos  # live consolidation -- this shelf is current
        candles_ago = (n - 1) - bo
        if candles_ago <= max_breakout_age:
            return fl_pos  # fresh breakout -- also current, use it

        # Expired breakout, nothing live on this shelf -- look for the NEXT
        # one, starting right after where this one broke out.
        min_start = bo + 1
        if min_start >= n - 1:
            return fl_pos

    return fl_pos


def detect_chop_signal(df, symbol, timeframe):
    """
    CHOPPY STOCKS TAB. Catches stocks with NO valid PranUltimate origin (no
    sharp prior uptrend above the 200 EMA) that are nonetheless genuinely
    rangebound right now -- e.g. KAMAHOLD on 1H/45min this session, which
    showed "NO SETUP" everywhere despite being visibly calm and box-like on
    the real chart.

    Deliberately narrower than detect_sp_signal:
      - Returns None if a strict origin EXISTS (find_first_200ema_touch
        succeeds) -- that stock is already covered by the regular timeframe
        tabs / SP Stocks, and showing it here too would be a duplicate.
      - Returns None on ANY standing breakout (box["breakout_pos"] is not
        None) -- a stock that genuinely broke out and is just drifting
        afterward is NOT "choppy/rangebound" by definition, even if price
        action looks quiet post-breakout. Only genuinely never-broken-out
        ranges qualify. (build_range_box's pullback-chaining already
        re-anchors past a broken-out-then-pulled-back-in box on its own --
        if it still returns a standing breakout here, that's a real,
        unresolved move, not a chop range.)
      - Only reports timeframes that are STILL inside the box right now (no
        "fresh breakout" status here at all -- this tab is rangebound
        setups only, per design).
    """
    if len(df) < 220:
        return None
    di = add_indicators(df)
    if len(di) < 60:
        return None

    if find_first_200ema_touch(di) is not None:
        return None  # has a real origin -- belongs in the regular tabs, not here

    fl_pos = find_fallback_low_staircase(di, MAX_BREAKOUT_AGE)
    if fl_pos is None:
        return None
    first_low = float(di.iloc[fl_pos]["low"])

    box = build_range_box(di, fl_pos)
    if box is None:
        return None

    if first_low_decisively_broken(di, first_low, fl_pos, box["breakout_pos"]):
        return None

    if box["breakout_pos"] is not None:
        return None  # standing breakout -- a real move, not a chop range

    resistance  = box["ceiling"]
    range_start = box["range_start"]
    last = di.iloc[-1]
    close = float(last["close"])
    if close <= 0 or resistance <= close:
        return None

    distance_pct  = (resistance - close) / close * 100
    range_candles = (len(di) - 1) - range_start

    return {
        "symbol":        symbol,
        "timeframe":     timeframe,
        "status":        "CONSOLIDATING",
        "distance_pct":  round(distance_pct, 1),
        "close":         round(close, 2),
        "resistance":    round(resistance, 2),
        "first_low":     round(first_low, 2),
        "rsi":           round(float(last["rsi14"]), 1),
        "volume":        int(last["volume"]),
        "vol_avg":       int(last["vol_avg"]),
        "ema200":        round(float(last["ema200"]), 2),
        "range_candles": range_candles,
        "timestamp":     str(di.index[-1]),
    }


def detect_sp_signal(df, symbol, timeframe):
    """
    Per-timeframe setup detection for the SP Stocks watchlist.

    Returns a dict for a LIVE setup, or None when this timeframe has no live
    setup for the stock:
      - Inside the box (consolidating) → setup_kind "consolidation", status
        NEAR BREAKOUT with distance_pct at ANY distance (no 5% cap — SP stocks
        always show how far they are from the ceiling).
      - Fresh breakout (≤MAX_BREAKOUT_AGE candles) → setup_kind "fresh_breakout",
        status BREAKOUT / 1 CANDLE POST / 2 CANDLES POST.
      - Breakout OLDER than that → returns None. The breakout has played out, so
        this timeframe is EXPIRED for the stock; the caller then looks to lower
        timeframes (where it may now be consolidating).
      - No valid box at all → returns None.

    FALLBACK added 2026-06-29 (BGRENERGY Daily case): if the strict origin
    is itself ancient with an old, EXPIRED breakout (or no origin at all),
    that doesn't mean nothing is happening NOW — it just means the strict
    "sharp uptrend → pullback" pattern hasn't recurred recently. Mirrors
    detect_chop_signal's fallback for the regular tabs: re-anchor at the
    lowest low in the window and check if a genuinely CURRENT box exists,
    rather than reporting a year-old, already-resolved structure as the
    final word on what this timeframe is doing right now.

    No higher-timeframe rejection is applied here — that selection happens in
    scan_sp_stock across all timeframes.
    """
    if len(df) < 220:
        return None
    df = add_indicators(df)
    if len(df) < 60:
        return None

    result = _detect_sp_signal_from_origin(df, symbol, timeframe, find_first_200ema_touch(df))
    if result is not None:
        result["origin_kind"] = "strict"
        return result

    # Strict origin gave nothing live -- try the fallback anchor instead.
    # Tagged "fallback" so scan_sp_stock can apply a DIFFERENT priority rule
    # to it (lowest/most-immediate TF wins, not highest -- a fallback box
    # isn't a confirmed real structure the way a strict-origin one is, so it
    # shouldn't automatically dominate lower timeframes the way a genuine
    # higher-TF consolidation does. Confirmed wrong behavior on BGRENERGY:
    # without this distinction, 1W's OWN fallback guess beat 1D's, purely
    # because 1W ranks higher in TF_PRIORITY -- not because it was actually
    # the more relevant timeframe).
    #
    # SANITY CHECK added 2026-06-29 (BGRENERGY Weekly case): even with the
    # lowest-wins tie-break above, a timeframe should never be considered
    # AT ALL via fallback if its own 200 EMA is wildly disconnected from
    # current price -- on BGR Weekly, EMA200 was Rs87.81 vs a close of
    # Rs315 (260%+ gap): the weekly EMA simply hasn't caught up with
    # reality, so ANY box built there is structurally meaningless, not
    # just "less relevant than Daily's". Reject outright rather than rely
    # on tie-breaking to sort it out.
    last_row = df.iloc[-1]
    ema200_now = float(last_row["ema200"])
    close_now  = float(last_row["close"])
    if ema200_now <= 0:
        return None
    ema_gap_pct = abs(close_now - ema200_now) / ema200_now * 100
    if ema_gap_pct > 50:
        return None

    fl_pos = find_fallback_low_staircase(df, MAX_BREAKOUT_AGE)
    if fl_pos is None:
        return None
    result = _detect_sp_signal_from_fl_pos(df, symbol, timeframe, fl_pos, touch_idx_for_cleanliness=fl_pos)
    if result is not None:
        result["origin_kind"] = "fallback"
    return result


def _detect_sp_signal_from_origin(df, symbol, timeframe, touch_idx):
    if touch_idx is None:
        return None
    first_low, fl_pos = find_first_low(df, touch_idx)
    return _detect_sp_signal_from_fl_pos(df, symbol, timeframe, fl_pos, touch_idx_for_cleanliness=touch_idx)


def _detect_sp_signal_from_fl_pos(df, symbol, timeframe, fl_pos, touch_idx_for_cleanliness):
    first_low = float(df.iloc[fl_pos]["low"])
    box = build_range_box(df, fl_pos)
    if box is None:
        return None

    resistance   = box["ceiling"]
    breakout_pos = box["breakout_pos"]
    range_start  = box["range_start"]

    if first_low_decisively_broken(df, first_low, fl_pos, breakout_pos):
        return None  # First Low decisively broken — treat as no setup

    range_candles = (len(df) - 1) - range_start
    cleanliness   = compute_cleanliness(df, touch_idx_for_cleanliness, fl_pos, range_start)
    last  = df.iloc[-1]

    # ── Inside the box: report distance at ANY % (no near-breakout cap) ─────
    if breakout_pos is None:
        close = float(last["close"])
        if close <= 0 or resistance <= close:
            return None
        distance_pct = (resistance - close) / close * 100
        return {
            "symbol":        symbol,
            "timeframe":     timeframe,
            "setup_kind":    "consolidation",   # inside the box, no breakout yet
            "status":        f"NEAR BREAKOUT ({round(distance_pct, 1)}%)",
            "distance_pct":  round(distance_pct, 1),
            "close":         round(close, 2),
            "resistance":    round(resistance, 2),
            "first_low":     round(first_low, 2),
            "anchor_date":   str(df.index[fl_pos]),
            "rsi":           round(float(last["rsi14"]), 1),
            "volume":        int(last["volume"]),
            "vol_avg":       int(last["vol_avg"]),
            "ema200":        round(float(last["ema200"]), 2),
            "range_candles": range_candles,
            "cleanliness":   cleanliness,
            "timestamp":     str(df.index[-1]),
        }

    # ── Breakout already happened ──────────────────────────────────────────
    candles_ago = (len(df) - 1) - breakout_pos

    # A breakout older than MAX_BREAKOUT_AGE candles EXPIRES this timeframe for
    # this stock: the move already played out here, so we stop looking at the
    # stock on this timeframe entirely and let a lower timeframe (where it may
    # now be consolidating) represent it. Returning None excludes this TF.
    if candles_ago > MAX_BREAKOUT_AGE:
        return None

    candle = df.iloc[breakout_pos]
    breakout_ts = df.index[breakout_pos]
    status = ["BREAKOUT", "1 CANDLE POST BREAKOUT", "2 CANDLES POST BREAKOUT"][candles_ago]

    return {
        "symbol":        symbol,
        "timeframe":     timeframe,
        "setup_kind":    "fresh_breakout",
        "status":        status,
        "candles_ago":   candles_ago,
        "breakout_date": str(breakout_ts),
        "close":         round(float(last["close"]), 2),
        "resistance":    round(resistance, 2),
        "first_low":     round(first_low, 2),
        "anchor_date":   str(df.index[fl_pos]),
        "stop_loss":     round(get_swing_low(df.iloc[:breakout_pos + 1]), 2),
        "rsi":           round(float(last["rsi14"]), 1),
        "volume":        int(last["volume"]),
        "vol_avg":       int(last["vol_avg"]),
        "ema200":        round(float(last["ema200"]), 2),
        "range_candles": range_candles,
        "cleanliness":   cleanliness,
        "timestamp":     str(df.index[breakout_pos]),
    }


def detect_touch_only(df, symbol, timeframe):
    """
    FALLBACK for SP Stocks ONLY, used when NO timeframe produces a real
    setup (the NO SETUP case). Reports that the stock has at least recently
    TOUCHED its 200 EMA on this timeframe, even though no mature box or
    fresh breakout exists yet — so the SP Stocks tab shows "this is where
    it's currently sitting" instead of a blank NO SETUP with zero context.
    Added 2026-06-29 per explicit request (SOLARINDS 1H case: just touched,
    no box matured yet — user wants to see the timeframe with breakout/
    ceiling left blank, since there's no level to report yet).

    Excludes "decisively broken" cases — those are genuinely dead setups,
    not "currently sitting at this level" in any useful sense.
    """
    if len(df) < 220:
        return None
    df = add_indicators(df)
    if len(df) < 60:
        return None

    touch_idx = find_first_200ema_touch(df)
    if touch_idx is None:
        return None

    first_low, fl_pos = find_first_low(df, touch_idx)
    box = build_range_box(df, fl_pos)
    breakout_pos = box["breakout_pos"] if box is not None else None

    if first_low_decisively_broken(df, first_low, fl_pos, breakout_pos):
        return None

    last = df.iloc[-1]
    touch_date = str(df.index[touch_idx])[:10]
    return {
        "symbol":     symbol,
        "timeframe":  timeframe,
        "setup_kind": "touch_only",
        "status":     f"TOUCHED 200 EMA ({touch_date}), no box yet",
        "close":      round(float(last["close"]), 2),
        "resistance": None,
        "first_low":  round(first_low, 2),
        "rsi":        round(float(last["rsi14"]), 1),
        "volume":     int(last["volume"]),
        "vol_avg":    int(last["vol_avg"]),
        "ema200":     round(float(last["ema200"]), 2),
        "timestamp":  str(df.index[-1]),
    }


def scan_sp_stock(dhan, symbol):
    """
    Scan one SP-watchlist stock across all scanner timeframes (1H-1W) and
    return a SINGLE row representing the stock's CURRENT ACTIVE STAGE.

    Selection priority (a breakout EXPIRES its timeframe after >3 candles —
    once the move has played out there, we stop looking at the stock on that
    timeframe and let a lower one represent it):

      1. STRICT consolidation wins first → the HIGHEST timeframe still
         inside a REAL (strict-origin) box. It hasn't made its move, so it
         dominates a fresh breakout on any lower timeframe. (KSL/Subros:
         Weekly beats a Daily breakout. JBMA/BHAGCHEM: a real, long-running
         Weekly consolidation correctly wins.)

      2. Else STRICT fresh breakouts (≤3 candles old) → the HIGHEST such
         timeframe. (Voltamp: Weekly breakout expired, 4H fresh breakout
         wins.)

      3. Else FALLBACK consolidation → the LOWEST such timeframe. A
         fallback box (used when NO strict origin exists, or the strict
         one is too old/expired to be live -- BGRENERGY Daily case) isn't
         a confirmed, structurally-real consolidation the way a strict one
         is -- it's "best guess at what's happening right now." It
         shouldn't automatically lose to a HIGHER timeframe's own
         independent guess just because that timeframe ranks higher; the
         most immediate/relevant one is the more useful answer here,
         mirroring detect_touch_only's lowest-wins rule. Confirmed wrong
         behavior without this distinction: BGRENERGY's 1W fallback beat
         its 1D fallback purely by TF rank, even though 1D was the more
         relevant/correct answer for where BGR is actually sitting.

      4. Else FALLBACK fresh breakout → the LOWEST such timeframe (same
         reasoning as #3).

      5. Else "currently touching" fallback (detect_touch_only) → lowest TF.

      6. Else NO SETUP — nothing live anywhere, by any method.

    Returns a dict with at least {symbol, timeframe, status}. timeframe is None
    with status UNRESOLVED / NO SETUP when applicable.
    """
    if dhan.get_security_id(symbol) is None:
        return {"symbol": symbol, "timeframe": None, "status": "UNRESOLVED",
                "note": "not found in Dhan master — check ALIAS_MAP"}

    frames = _fetch_sp_frames(dhan, symbol)
    return _discover_sp_setup(symbol, frames)


def _fetch_sp_frames(dhan, symbol):
    """One efficient pass: daily+weekly (1 call) + the intraday band (3 calls)."""
    df_1d, df_1w = dhan.get_daily_and_weekly(symbol)
    frames = {}
    if df_1d is not None:
        frames["1D"] = df_1d
    if df_1w is not None:
        frames["1W"] = df_1w
    frames.update(dhan.get_remaining_timeframes(symbol))
    return frames


def _discover_sp_setup(symbol, frames):
    """
    Full fresh discovery across every timeframe -- the original
    scan_sp_stock logic, extracted so scan_sp_stock_with_memory can fall
    through to it using already-fetched frames (no duplicate API calls)
    when there's no persisted entry, or a persisted one just resolved.
    """
    # Gather every timeframe's LIVE setup (consolidating, or breakout ≤3
    # candles old). Timeframes whose breakout has expired (>3 candles) return
    # None from detect_sp_signal and are simply absent here.
    setups = {}  # tf -> result dict
    for tf in TF_PRIORITY:
        df = frames.get(tf)
        if df is None or len(df) < 50:
            continue
        result = detect_sp_signal(df, symbol, tf)
        if result is not None:
            setups[tf] = result

    def tf_rank(tf):
        return TF_PRIORITY.index(tf)

    # ── Priority 0: Daily/Weekly CONSOLIDATION always beats intraday ───────
    # Added 2026-06-29 per explicit request: intraday (1H-4H) is "only for
    # the bot" -- for SP Stocks, Daily/Weekly should always take precedence
    # whenever they're ACTUALLY consolidating (strict or fallback, doesn't
    # matter), regardless of what intraday timeframes independently show.
    # Confirmed wrong without this: BGRENERGY's 1H (a real, valid STRICT
    # consolidation) was winning over 1D's fallback consolidation purely
    # because strict unconditionally beat fallback at the time -- but 1D is
    # what actually matters for an SP stock; 1H is bot territory.
    dw_consolidating = [(tf, r) for tf, r in setups.items()
                        if tf in ("1D", "1W") and r.get("setup_kind") == "consolidation"]
    if dw_consolidating:
        strict_dw = [(tf, r) for tf, r in dw_consolidating if r.get("origin_kind") == "strict"]
        if strict_dw:
            # Real structure on Daily/Weekly: highest (Weekly) wins, same as
            # the original KSL precedent -- just now scoped ahead of intraday.
            tf, r = max(strict_dw, key=lambda x: tf_rank(x[0]))
            return r
        fallback_dw = [(tf, r) for tf, r in dw_consolidating if r.get("origin_kind") == "fallback"]
        # Both fallback guesses: lowest (Daily, more immediate) wins, same
        # reasoning as the fallback tie-break rule elsewhere.
        tf, r = min(fallback_dw, key=lambda x: tf_rank(x[0]))
        return r

    strict_setups   = {tf: r for tf, r in setups.items() if r.get("origin_kind") == "strict"}
    fallback_setups = {tf: r for tf, r in setups.items() if r.get("origin_kind") == "fallback"}

    # ── Priority 1: STRICT consolidation → highest consolidating timeframe ──
    # (Daily/Weekly consolidation already handled above -- this tier now
    # effectively only matters for intraday, since neither 1D nor 1W had
    # anything live consolidating.)
    consolidating = [(tf, r) for tf, r in strict_setups.items()
                     if r.get("setup_kind") == "consolidation"]
    if consolidating:
        tf, r = max(consolidating, key=lambda x: tf_rank(x[0]))
        return r

    # ── Priority 2: STRICT fresh breakouts → highest such timeframe ────────
    fresh = [(tf, r) for tf, r in strict_setups.items()
             if r.get("setup_kind") == "fresh_breakout"]
    if fresh:
        tf, r = max(fresh, key=lambda x: tf_rank(x[0]))
        return r

    # ── Priority 3: FALLBACK consolidation → LOWEST such timeframe ─────────
    fallback_consolidating = [(tf, r) for tf, r in fallback_setups.items()
                               if r.get("setup_kind") == "consolidation"]
    if fallback_consolidating:
        tf, r = min(fallback_consolidating, key=lambda x: tf_rank(x[0]))
        return r

    # ── Priority 4: FALLBACK fresh breakout → LOWEST such timeframe ────────
    fallback_fresh = [(tf, r) for tf, r in fallback_setups.items()
                       if r.get("setup_kind") == "fresh_breakout"]
    if fallback_fresh:
        tf, r = min(fallback_fresh, key=lambda x: tf_rank(x[0]))
        return r

    # ── No live setup on any timeframe — fall back to "currently touching" ──
    # info instead of a blank NO SETUP (added 2026-06-29, SOLARINDS case).
    # Pick the LOWEST timeframe with a touch -- that's the most immediate/
    # current one, the opposite of the highest-wins rule used for strict
    # setups above (a real setup means "this level matters", whereas a bare
    # touch just means "this is where it's sitting right now").
    touch_only = {}
    for tf in TF_PRIORITY:
        df = frames.get(tf)
        if df is None or len(df) < 50:
            continue
        result = detect_touch_only(df, symbol, tf)
        if result is not None:
            touch_only[tf] = result
    if touch_only:
        tf = min(touch_only.keys(), key=tf_rank)
        return touch_only[tf]

    return {"symbol": symbol, "timeframe": None, "status": "NO SETUP"}


def scan_sp_stock_with_memory(dhan, symbol, state):
    """
    Memory-aware version of scan_sp_stock, added 2026-06-29 per explicit
    request: instead of re-running full discovery from scratch every scan
    (which flip-flops a symbol's timeframe confusingly as new candles
    shift which origin/fallback wins, even when nothing structurally
    changed), remember the symbol's last assignment and just re-check that
    SAME box against fresh data.

    Behavior:
      - No persisted entry → full fresh discovery (_discover_sp_setup),
        then persist whatever it found.
      - Persisted entry, anchor still resolvable, box still consolidating
        (no breakout/breakdown) → KEEP the same timeframe, just refresh the
        ceiling/distance numbers against fresh data.
      - Persisted entry, box has BROKEN OUT (fresh or aged) → keep
        reporting it as a breakout with candles-ago (uncapped -- SP Stocks
        doesn't expire a tracked breakout the way fresh discovery's
        MAX_BREAKOUT_AGE does), WHILE ALSO checking the next LOWER
        timeframe (already-fetched, no extra API cost) for a fresh
        consolidation. If found, SWITCH the persisted entry to that lower
        timeframe instead.
      - Persisted entry, first low DECISIVELY BROKEN (breakdown) → check
        the next LOWER timeframe for a fresh touch/consolidation there
        first (mirrors "touched the lower timeframe and started
        consolidating"); if found, switch to it. If not, fall through to
        full fresh discovery across all timeframes.
      - Persisted anchor no longer resolvable at all (data gap) → treat as
        invalid, fall through to full fresh discovery.
    """
    if dhan.get_security_id(symbol) is None:
        return {"symbol": symbol, "timeframe": None, "status": "UNRESOLVED",
                "note": "not found in Dhan master — check ALIAS_MAP"}

    frames = _fetch_sp_frames(dhan, symbol)
    persisted = state["sp"].get(symbol)

    if persisted is not None:
        tf = persisted.get("tf")
        df = frames.get(tf)
        if df is not None and len(df) >= 220:
            di = add_indicators(df)
            box, fl_pos, first_low = recheck_persisted_box(di, persisted.get("anchor_date"))

            if box is not None:
                breakout_pos = box["breakout_pos"]
                broken = first_low_decisively_broken(di, first_low, fl_pos, breakout_pos)

                if not broken and breakout_pos is None:
                    # Still consolidating on the SAME timeframe -- refresh
                    # numbers, keep the persisted assignment unchanged.
                    result = _detect_sp_signal_from_fl_pos(
                        di, symbol, tf, fl_pos, touch_idx_for_cleanliness=fl_pos)
                    if result is not None:
                        result["origin_kind"] = persisted.get("origin_kind", "strict")
                        state["sp"][symbol] = {
                            "tf": tf, "anchor_date": persisted.get("anchor_date"),
                            "origin_kind": result["origin_kind"],
                            "status": "consolidating", "breakout_date": None,
                        }
                        return result

                elif not broken and breakout_pos is not None:
                    # BROKEN OUT (fresh or aged) -- report it with candles-
                    # ago, uncapped, while checking the next lower TF for a
                    # fresh consolidation to switch to.
                    candles_ago = (len(di) - 1) - breakout_pos
                    next_tf = _next_lower_tf(tf)
                    if next_tf is not None:
                        lower_df = frames.get(next_tf)
                        if lower_df is not None and len(lower_df) >= 220:
                            lower_result = detect_sp_signal(lower_df, symbol, next_tf)
                            if lower_result is not None and lower_result.get("setup_kind") == "consolidation":
                                state["sp"][symbol] = {
                                    "tf": next_tf,
                                    "anchor_date": lower_result.get("anchor_date"),
                                    "origin_kind": lower_result.get("origin_kind"),
                                    "status": "consolidating", "breakout_date": None,
                                }
                                return lower_result
                    breakout_ts = di.index[breakout_pos]
                    last = di.iloc[-1]
                    result = {
                        "symbol": symbol, "timeframe": tf,
                        "setup_kind": "fresh_breakout",
                        "status": f"BROKE OUT ({candles_ago} candle(s) ago)",
                        "candles_ago": candles_ago,
                        "breakout_date": str(breakout_ts),
                        "close": round(float(last["close"]), 2),
                        "resistance": round(box["ceiling"], 2),
                        "first_low": round(first_low, 2),
                        "origin_kind": persisted.get("origin_kind", "strict"),
                        "timestamp": str(di.index[-1]),
                    }
                    state["sp"][symbol] = {
                        "tf": tf, "anchor_date": persisted.get("anchor_date"),
                        "origin_kind": result["origin_kind"],
                        "status": "breakout", "breakout_date": str(breakout_ts),
                    }
                    return result

                # else: broken == True -- breakdown, fall through below to
                # check the next lower TF, then full fresh discovery.

            # Breakdown, or anchor unresolvable -- try the next lower TF
            # first ("touched the lower timeframe and started
            # consolidating"), before giving up to full fresh discovery.
            next_tf = _next_lower_tf(tf)
            if next_tf is not None:
                lower_df = frames.get(next_tf)
                if lower_df is not None and len(lower_df) >= 220:
                    lower_result = detect_sp_signal(lower_df, symbol, next_tf)
                    if lower_result is not None and lower_result.get("setup_kind") == "consolidation":
                        state["sp"][symbol] = {
                            "tf": next_tf,
                            "anchor_date": lower_result.get("anchor_date"),
                            "origin_kind": lower_result.get("origin_kind"),
                            "status": "consolidating", "breakout_date": None,
                        }
                        return lower_result

        # Persisted entry invalid/resolved with no lower-TF replacement --
        # clear it and fall through to full fresh discovery below.
        state["sp"].pop(symbol, None)

    result = _discover_sp_setup(symbol, frames)
    if result.get("timeframe"):
        state["sp"][symbol] = {
            "tf": result["timeframe"],
            "anchor_date": result.get("anchor_date"),
            "origin_kind": result.get("origin_kind", "strict"),
            "status": "breakout" if result.get("setup_kind") == "fresh_breakout" else "consolidating",
            "breakout_date": result.get("breakout_date"),
        }
    return result


def _next_lower_tf(tf):
    """The timeframe immediately below `tf` in TF_PRIORITY, or None if `tf`
    is already the lowest (1H)."""
    idx = TF_PRIORITY.index(tf)
    if idx == 0:
        return None
    return TF_PRIORITY[idx - 1]



# ── Universe ────────────────────────────────────────────────────────────────────
def get_universe(dhan):
    """
    Full NSE equity universe straight from Dhan's security master (~9,500
    symbols) — no manual CSV download needed. If nse_symbols.txt exists in
    this folder, it's treated as a deliberate override (e.g. for a faster
    curated test run) and used instead.
    """
    if os.path.exists(SYMBOL_FILE):
        with open(SYMBOL_FILE) as f:
            symbols = [line.strip() for line in f if line.strip()]
        log.info(f"Using manual override list: {len(symbols)} symbols from nse_symbols.txt")
        return symbols

    symbols = dhan.get_all_symbols()
    log.info(f"Using FULL NSE universe from Dhan security master: {len(symbols)} symbols")
    return symbols


# ── Main Scanner ───────────────────────────────────────────────────────────────
def run_scan():
    log.info("=" * 60)
    log.info("PranUltimate Scanner started")
    log.info(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    dhan = DhanData(cfg["client_id"], cfg["access_token"])

    # ── Fail fast: confirm the token/connection actually works before ───────
    # looping over ~9,500 symbols. A bad token makes EVERY call fail the same
    # way — without this check, that's only visible after hours of silent
    # "no data" results across the entire universe.
    log.info("Verifying Dhan connection (one test fetch)...")
    ok, reason = dhan.verify_connection()
    if not ok:
        log.error("=" * 60)
        log.error(f"ABORTING — Dhan connection check failed: {reason}")
        log.error("Check intraday_config.json — the access_token likely needs regenerating.")
        log.error("=" * 60)
        return
    log.info("Dhan connection OK — proceeding with full scan.")

    symbols = get_universe(dhan)
    total = len(symbols)
    est_minutes = round(total * 4 * 0.25 / 60)  # worst case: 4 calls/symbol incl. pacing
    log.info(f"Scanning {total} symbols — worst-case estimate ~{est_minutes} min "
             f"(stocks excluded by the weekly filter finish much faster)")

    breakout_results     = {tf: [] for tf in TIMEFRAMES}
    near_breakout_results = {tf: [] for tf in TIMEFRAMES}
    chop_results          = {tf: [] for tf in TIMEFRAMES}
    errors        = []
    weekly_skipped    = 0
    liquidity_skipped = 0
    no_data           = 0
    done = 0
    no_data_reason_logged = 0
    scanner_state = load_scanner_state()

    for symbol in symbols:
        done += 1
        try:
            # ── Stage 1: daily+weekly only (1 API call) — cheap gate ────────
            df_1d, df_1w = dhan.get_daily_and_weekly(symbol)
            if df_1d is None:
                no_data += 1
                # Surface WHY for the first few — if every symbol fails the
                # same way (e.g. expired token), this makes it obvious in
                # seconds instead of after the whole universe has run.
                if no_data_reason_logged < 3:
                    log.warning(f"  {symbol}: no data — {dhan._last_error or 'unknown reason'}")
                    no_data_reason_logged += 1
                continue

            frames = {"1D": df_1d, "1W": df_1w}

            # Weekly 200 EMA filter — skip the expensive 3 remaining calls
            # entirely if the stock has broken down on the weekly chart.
            if len(df_1w) >= 220:
                df_1w_ind = add_indicators(df_1w)
                if len(df_1w_ind) > 0:
                    last_w = df_1w_ind.iloc[-1]
                    if last_w["close"] < last_w["ema200"] and last_w["ema20"] < last_w["ema200"]:
                        weekly_skipped += 1
                        continue

            # ── Liquidity filter: skip stocks with < ₹5cr avg daily turnover ─
            # df_1d is already in memory (Stage 1), so this costs zero extra
            # API calls. Uses last 20 trading days of close × volume.
            # Filters out thin small/mid-caps where breakout signals are noise
            # and slippage would eat any theoretical edge.
            _avg_turnover = (df_1d["close"] * df_1d["volume"]).tail(20).mean()
            if _avg_turnover < 5_00_00_000:  # ₹5 crore
                liquidity_skipped += 1
                continue

            # ── Stage 2: the other 8 timeframes (3 more API calls) ──────────
            frames.update(dhan.get_remaining_timeframes(symbol))

            # ── Evaluate every timeframe we have data for ───────────────────
            for tf in TIMEFRAMES:
                df = frames.get(tf)
                if df is None or len(df) < 50:
                    continue

                # ── Graduation guard: if this symbol sustained a prior
                # breakout on this exact TF, skip it here. Only re-allow
                # once the TF's candle low touches or crosses 200 EMA.
                _grad = scanner_state["graduated"].get(symbol, {})
                if tf in _grad:
                    df_gi = add_indicators(df)
                    last_gi = df_gi.iloc[-1]
                    if float(last_gi["low"]) <= float(last_gi["ema200"]):
                        log.info(f"  \u21ba {symbol} [{tf}]: 200 EMA touched "
                                 f"\u2014 graduation reset, re-entering {tf}")
                        del scanner_state["graduated"][symbol][tf]
                        if not scanner_state["graduated"][symbol]:
                            del scanner_state["graduated"][symbol]
                        # Fall through to fresh detection below
                    else:
                        continue  # still graduated on this TF

                category, payload = detect_signal(df, symbol, tf, daily_df=frames.get("1D"))

                # ── Graduation: stale breakout on any TF → mark as sustained ──
                if category is None and payload == "stale breakout":
                    persisted_bo = scanner_state["regular"].get(symbol)
                    if (persisted_bo
                            and persisted_bo.get("status") == "breakout"
                            and persisted_bo.get("tf") == tf):
                        from datetime import datetime as _dt
                        scanner_state["graduated"].setdefault(symbol, {})[tf] = {
                            "graduated_date": _dt.now().strftime("%Y-%m-%d"),
                            "resistance":     persisted_bo.get("resistance"),
                        }
                        scanner_state["regular"].pop(symbol, None)
                        log.info(f"  \U0001f393 {symbol} [{tf}]: graduated "
                                 f"\u2014 sustained breakout, lower TFs only")
                        continue

                if category is None:
                    # PERSISTED-ANCHOR RECOVERY (added 2026-06-29): fresh
                    # discovery found nothing here, but if this symbol
                    # previously had a persisted "consolidating" assignment
                    # on this EXACT timeframe, check whether price simply
                    # fell back below resistance (re-entered the box) after
                    # a brief breakout attempt -- in which case resume
                    # tracking the SAME box/anchor rather than treating it
                    # as a brand new, unrelated structure. This is what
                    # makes "broke out, then fell back" behave as one
                    # continuous story instead of flip-flopping.
                    persisted = scanner_state["regular"].get(symbol)
                    if persisted is not None and persisted.get("tf") == tf:
                        di_check = add_indicators(df)
                        box_chk, fl_pos_chk, first_low_chk = recheck_persisted_box(
                            di_check, persisted.get("anchor_date"))
                        if box_chk is not None:
                            broken_chk = first_low_decisively_broken(
                                di_check, first_low_chk, fl_pos_chk, box_chk["breakout_pos"])
                            if not broken_chk and box_chk["breakout_pos"] is None:
                                last_chk = di_check.iloc[-1]
                                close_chk = float(last_chk["close"])
                                resistance_chk = box_chk["ceiling"]
                                if close_chk > 0 and resistance_chk > close_chk:
                                    distance_chk = (resistance_chk - close_chk) / close_chk * 100
                                    if distance_chk <= NEAR_BREAKOUT_PCT:
                                        near_breakout_results[tf].append({
                                            "symbol": symbol, "timeframe": tf,
                                            "status": f"NEAR BREAKOUT ({round(distance_chk, 1)}%) [resumed]",
                                            "distance_pct": round(distance_chk, 1),
                                            "close": round(close_chk, 2),
                                            "resistance": round(resistance_chk, 2),
                                            "first_low": round(first_low_chk, 2),
                                            "anchor_date": persisted.get("anchor_date"),
                                            "timestamp": str(di_check.index[-1]),
                                        })
                                        continue
                    # No persisted recovery applied -- this symbol no longer
                    # has a regular-tabs assignment on this tf.
                    scanner_state["regular"].pop(symbol, None)

                if category is None:
                    # No strict-origin signal here -- check if this is a
                    # genuinely rangebound chop (Choppy Stocks tab). Zero
                    # extra API calls: reuses the df already fetched above.
                    chop = detect_chop_signal(df, symbol, tf)
                    if chop is not None:
                        chop_results[tf].append(chop)
                    continue

                if category == "breakout":
                    higher_tf = has_higher_tf_consolidation(frames, tf)
                    if higher_tf:
                        # The higher timeframe is still consolidating — it OWNS
                        # this move (e.g. a Daily breakout on HAL/KSL that's
                        # really an in-progress Weekly setup). Rather than drop
                        # the signal, surface it under the higher timeframe so
                        # it shows up where the move actually belongs.
                        payload["status"] = f"OWNED BY {higher_tf}"
                        payload["owned_by"] = higher_tf
                        payload["fired_on"] = tf
                        payload["timeframe"] = higher_tf
                        breakout_results[higher_tf].append(payload)
                        log.info(f"  ↑ {symbol} [{tf}] breakout reassigned to higher TF [{higher_tf}]")
                    else:
                        breakout_results[tf].append(payload)
                        log.info(f"  ★ BREAKOUT: {symbol} [{tf}] — {payload['status']}")
                        # Track breakout so it can be graduated once stale
                        # (applies to all TFs: 1H, 2H, 3H, 4H, 1D)
                        scanner_state["regular"][symbol] = {
                            "tf":            tf,
                            "anchor_date":   payload.get("anchor_date", ""),
                            "status":        "breakout",
                            "breakout_date": str(payload.get("timestamp", "")),
                            "resistance":    payload.get("resistance"),
                        }
                elif category == "near_breakout":
                    near_breakout_results[tf].append(payload)
                    scanner_state["regular"][symbol] = {
                        "tf": tf, "anchor_date": payload.get("anchor_date"),
                    }

        except Exception as e:
            errors.append(f"{symbol}: {e}")

        finally:
            # In a `finally` block (not just after try/except) so this ALWAYS
            # runs — including every time a `continue` fires above. If most
            # or all symbols hit "no data" (exactly what happened tonight),
            # a check placed after the try/except would never run at all,
            # since `continue` jumps straight to the next loop iteration.
            if done % 200 == 0:
                pct = round(done / total * 100, 1)
                log.info(f"Progress: {done}/{total} ({pct}%) | "
                         f"weekly-skipped={weekly_skipped} | "
                         f"liquidity-skipped={liquidity_skipped} | "
                         f"no-data={no_data} | errors={len(errors)}")

                # Circuit breaker: if the token expires PARTWAY through a long
                # run (Dhan tokens are time-limited to ~24h, and a full scan
                # can take hours), the preflight check at the start won't
                # catch that. A no-data rate this high only happens when
                # something systemic broke — a few genuinely stale/delisted
                # symbols never looks like this.
                if done >= 200 and (no_data / done) > 0.8:
                    log.error("=" * 60)
                    log.error(f"ABORTING — {no_data}/{done} symbols "
                              f"({round(no_data/done*100,1)}%) returned no data. This isn't "
                              f"normal stock-by-stock failure — something systemic broke "
                              f"(token expired mid-run? connection dropped?).")
                    log.error(f"Last failure reason seen: {dhan._last_error or 'unknown'}")
                    log.error("Check intraday_config.json's access_token and rerun.")
                    log.error("=" * 60)
                    return

    # ── Dedupe confirmed breakouts: keep only the highest-TF signal per symbol ─
    # Near-breakout entries are NOT deduped — seeing a stock close on multiple
    # timeframes is useful information, not noise, for a watchlist.
    best_breakout_per_symbol = {}
    for tf in TF_PRIORITY:
        for signal in breakout_results[tf]:
            sym = signal["symbol"]
            current_best = best_breakout_per_symbol.get(sym)
            if current_best is None:
                best_breakout_per_symbol[sym] = signal
                continue
            tf_idx     = TF_PRIORITY.index(tf)
            best_idx   = TF_PRIORITY.index(current_best["timeframe"])
            if tf_idx > best_idx:
                best_breakout_per_symbol[sym] = signal
            elif tf_idx == best_idx:
                # Same timeframe: prefer a genuine breakout over a reassigned
                # "owned by" signal — the genuine one is the more informative
                # (it actually broke out on this timeframe).
                if current_best.get("owned_by") and not signal.get("owned_by"):
                    best_breakout_per_symbol[sym] = signal

    deduped_breakouts = {tf: [] for tf in TF_PRIORITY}
    for signal in best_breakout_per_symbol.values():
        deduped_breakouts[signal["timeframe"]].append(signal)

    # Within each timeframe: genuine breakouts (fresh → 2-candle) first, then
    # reassigned "owned by" signals that bubbled up from a lower timeframe.
    status_order = {"BREAKOUT": 0, "1 CANDLE POST BREAKOUT": 1, "2 CANDLES POST BREAKOUT": 2}
    def breakout_sort_key(s):
        if s.get("owned_by"):
            return (1, 0)  # reassigned signals after all genuine ones
        return (0, status_order.get(s["status"], 9))
    for tf in deduped_breakouts:
        deduped_breakouts[tf].sort(key=breakout_sort_key)
    for tf in near_breakout_results:
        near_breakout_results[tf].sort(key=lambda s: s["distance_pct"])

    # ── Global highest-TF-wins dedup, ACROSS breakout + near-breakout ───────
    # Per explicit requirement (2026-06-29, KSL/BHAGCHEM case): a stock
    # should appear under its single HIGHEST qualifying timeframe ONLY,
    # full stop — never simultaneously in multiple tabs. Previously only
    # breakouts were deduped to highest-TF; near-breakout/consolidating
    # entries showed on EVERY timeframe they independently qualified on
    # (e.g. KSL showing as consolidating on 1H, 2H, 1D, AND 1W at once).
    # Category doesn't matter for this comparison — purely whichever entry
    # has the highest TF_PRIORITY index wins, breakout or not.
    all_candidates_by_symbol = {}
    for tf in TF_PRIORITY:
        for signal in deduped_breakouts[tf]:
            all_candidates_by_symbol.setdefault(signal["symbol"], []).append(("breakout", signal))
        for signal in near_breakout_results[tf]:
            all_candidates_by_symbol.setdefault(signal["symbol"], []).append(("near", signal))

    deduped_breakouts_final = {tf: [] for tf in TF_PRIORITY}
    near_breakout_final     = {tf: [] for tf in TF_PRIORITY}
    for sym, candidates in all_candidates_by_symbol.items():
        category, signal = max(candidates, key=lambda c: TF_PRIORITY.index(c[1]["timeframe"]))
        tf = signal["timeframe"]
        if category == "breakout":
            deduped_breakouts_final[tf].append(signal)
        else:
            near_breakout_final[tf].append(signal)

    for tf in deduped_breakouts_final:
        deduped_breakouts_final[tf].sort(key=breakout_sort_key)
    for tf in near_breakout_final:
        near_breakout_final[tf].sort(key=lambda s: s["distance_pct"])

    deduped_breakouts     = deduped_breakouts_final
    near_breakout_results = near_breakout_final

    # ── Choppy Stocks: dedupe per symbol (highest TF wins, same pattern as ──
    # breakouts), then exclude any symbol already represented in a regular
    # breakout or near-breakout tab -- a stock with a real signal elsewhere
    # should never also appear here. Uses the FINAL deduped sets (a symbol's
    # winning entry may have moved from breakout to near-breakout category
    # during the global highest-TF dedup above, or vice versa).
    symbols_already_covered = set(all_candidates_by_symbol.keys())

    best_chop_per_symbol = {}
    for tf in TF_PRIORITY:
        for r in chop_results[tf]:
            sym = r["symbol"]
            if sym in symbols_already_covered:
                continue
            current_best = best_chop_per_symbol.get(sym)
            if current_best is None or TF_PRIORITY.index(tf) > TF_PRIORITY.index(current_best["timeframe"]):
                best_chop_per_symbol[sym] = r

    # Flat array (one row per stock), matching sp_stocks' shape -- not a
    # dict-by-timeframe like the regular tabs. The dashboard's Choppy Stocks
    # tab is built the same way as SP Stocks: a single list, sorted by
    # closeness to the ceiling.
    choppy_stocks_flat = sorted(best_chop_per_symbol.values(), key=lambda s: s["distance_pct"])

    log.info(f"Choppy Stocks (no strict origin, genuinely rangebound): {len(choppy_stocks_flat)} symbols")

    # ── Merge into final per-timeframe results: breakouts first, then near ────
    final_results = {tf: deduped_breakouts[tf] + near_breakout_results[tf] for tf in TIMEFRAMES}

    total_breakouts = sum(len(v) for v in deduped_breakouts.values())
    total_near      = sum(len(v) for v in near_breakout_results.values())
    log.info(f"\nScan complete. {total_breakouts} confirmed breakouts, "
             f"{total_near} near-breakout candidates.")
    log.info(f"weekly-filter excluded: {weekly_skipped} | "
             f"liquidity excluded (<₹5cr): {liquidity_skipped} | "
             f"no data: {no_data} | errors: {len(errors)}")

    # ── SP Stocks: scan the manual watchlist across all timeframes ──────────
    sp_results = scan_sp_watchlist(dhan, errors)

    output = {
        "generated_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_signals":  total_breakouts,
        "total_near_breakout": total_near,
        "errors":         len(errors),
        "results":        final_results,
        "sp_stocks":      sp_results,
        "choppy_stocks":  choppy_stocks_flat,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)
    log.info(f"Results saved to: {OUTPUT_PATH}")
    save_scanner_state(scanner_state)
    publish_to_github()
    sync_alerts()
    # publish_alerts_repo() disabled — git pull --rebase was corrupting
    # alerts_state.json by partially merging the remote (older) version into
    # the freshly-written local file when the rebase conflicted.
    # Alerts checking now runs via Windows Task Scheduler on the local file
    # directly, so pushing to GitHub is not needed for alerts to work.
    # publish_alerts_repo()

    wl_path = write_watchlist(deduped_breakouts, near_breakout_results, OUTPUT_PATH)
    log.info(f"Watchlist saved to: {wl_path}")

    if errors:
        log.warning(f"{len(errors)} errors encountered (see scan.log for details)")


def scan_sp_watchlist(dhan, errors=None):
    """
    Scan the SP_STOCKS watchlist across all timeframes and return the sorted
    list of SP rows. Shared by the full scan and the SP-only mode.

    Uses the persisted scanner_state (added 2026-06-29) so a stock's
    timeframe assignment doesn't flip-flop between scans purely because new
    candles shifted which origin/fallback would win from scratch -- it only
    changes when the persisted box actually resolves (breakout/breakdown).
    """
    if errors is None:
        errors = []
    log.info(f"\nScanning {len(SP_STOCKS)} SP watchlist stocks...")
    state = load_scanner_state()
    sp_results = []
    sp_unresolved = []
    for sym in SP_STOCKS:
        try:
            row = scan_sp_stock_with_memory(dhan, sym, state)
        except Exception as e:
            errors.append(f"SP/{sym}: {e}")
            row = {"symbol": sym, "timeframe": None, "status": "ERROR"}
        sp_results.append(row)
        if row.get("status") == "UNRESOLVED":
            sp_unresolved.append(sym)
    save_scanner_state(state)

    # Sort: breakouts first (highest TF, freshest), then near-breakouts by
    # distance, then no-setup/unresolved last.
    def sp_sort_key(r):
        status = r.get("status", "")
        tf = r.get("timeframe")
        tf_rank = TF_PRIORITY.index(tf) if tf in TF_PRIORITY else -1
        if status.startswith("BREAKOUT") or status.startswith("BROKE OUT") or \
           status in ("1 CANDLE POST BREAKOUT", "2 CANDLES POST BREAKOUT"):
            return (0, -tf_rank, r.get("candles_ago", 0))
        if status.startswith("NEAR BREAKOUT"):
            return (1, -tf_rank, r.get("distance_pct", 999))
        return (2, 0, 0)  # NO SETUP / UNRESOLVED / ERROR
    sp_results.sort(key=sp_sort_key)

    sp_with_setup = sum(1 for r in sp_results if r.get("timeframe"))
    log.info(f"SP Stocks: {sp_with_setup}/{len(SP_STOCKS)} have a setup on some timeframe.")
    if sp_unresolved:
        log.warning(f"SP Stocks unresolved ({len(sp_unresolved)}): {', '.join(sp_unresolved)} "
                    f"— see [unresolved] lines above for suggested ALIAS_MAP fixes.")
    return sp_results


def _debug_one_timeframe(df, symbol, tf, daily_df=None):
    """Return a human-readable line explaining what the box model sees on this TF."""
    if df is None:
        return f"  {tf:4}: no data"
    if len(df) < 220:
        return f"  {tf:4}: only {len(df)} candles (need 220+) — skipped"

    if tf in ("1H", "2H") and daily_df is not None:
        d = add_indicators(daily_df)
        if len(d) > 0:
            last_d = d.iloc[-1]
            if last_d["close"] <= last_d["ema200"]:
                return (f"  {tf:4}: daily close Rs{round(float(last_d['close']),2)} <= "
                        f"daily 200 EMA Rs{round(float(last_d['ema200']),2)} "
                        f"— EXCLUDED (no daily uptrend context)")

    di = add_indicators(df)
    if len(di) < 60:
        return f"  {tf:4}: only {len(di)} candles after EMA warmup — skipped"

    touch_idx = find_first_200ema_touch(di)
    if touch_idx is None:
        return f"  {tf:4}: no 200 EMA touch found (not in a correction structure)"

    first_low, fl_pos = find_first_low(di, touch_idx)
    box = build_range_box(di, fl_pos)
    if box is None:
        return (f"  {tf:4}: touch@{touch_idx}, first_low={round(first_low,2)} @ {fl_pos} "
                f"— box too brief (<3 candles), no valid box")

    ceiling      = box["ceiling"]
    breakout_pos = box["breakout_pos"]
    range_start  = box["range_start"]
    last_close   = float(di.iloc[-1]["close"])

    decisive = first_low_decisively_broken(di, first_low, fl_pos, breakout_pos)
    touch_date = str(di.index[touch_idx])[:10]
    fl_date    = str(di.index[fl_pos])[:10]

    base = (f"  {tf:4}: touch={touch_date} | first_low=Rs{round(first_low,2)} ({fl_date}) | "
            f"ceiling=Rs{round(ceiling,2)} | close=Rs{round(last_close,2)}")

    if decisive:
        return base + " | First Low DECISIVELY BROKEN -> no setup"

    if breakout_pos is None:
        dist = (ceiling - last_close) / last_close * 100 if last_close > 0 else 0
        return base + f" | CONSOLIDATING, {round(dist,1)}% below ceiling -> setup_kind=consolidation"

    candles_ago = (len(di) - 1) - breakout_pos
    bo_date = str(di.index[breakout_pos])[:10]
    if candles_ago > MAX_BREAKOUT_AGE:
        return (base + f" | breakout {bo_date} = {candles_ago} candles ago "
                f"(> {MAX_BREAKOUT_AGE}) -> EXPIRED, timeframe excluded")
    return (base + f" | FRESH BREAKOUT {bo_date} = {candles_ago} candles ago "
            f"-> setup_kind=fresh_breakout")


def debug_symbol(symbol):
    """
    DEBUG MODE: print exactly what the box model sees on EVERY timeframe for one
    symbol, then show which timeframe scan_sp_stock selects and why. Use this to
    investigate why a stock landed on a given timeframe.

    Run with:  py -3.13 scanner\\scan.py debug VOLTAMP
    """
    log.info("=" * 70)
    log.info(f"DEBUG -- {symbol}")
    log.info("=" * 70)

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    dhan = DhanData(cfg["client_id"], cfg["access_token"])

    ok, reason = dhan.verify_connection()
    if not ok:
        log.error(f"ABORTING -- Dhan connection failed: {reason}")
        return

    sec_id = dhan.get_security_id(symbol)
    if sec_id is None:
        log.error(f"{symbol} did not resolve in Dhan master -- check ALIAS_MAP.")
        return
    log.info(f"Resolved {symbol} -> security_id={sec_id}, series={dhan.get_series(symbol)}")

    df_1d, df_1w = dhan.get_daily_and_weekly(symbol)
    frames = {}
    if df_1d is not None:
        frames["1D"] = df_1d
    if df_1w is not None:
        frames["1W"] = df_1w
    frames.update(dhan.get_remaining_timeframes(symbol))

    log.info("\nPer-timeframe box analysis (lowest -> highest):")
    for tf in TF_PRIORITY:
        log.info(_debug_one_timeframe(frames.get(tf), symbol, tf, daily_df=frames.get("1D")))

    log.info("\nFinal selection by scan_sp_stock:")
    result = scan_sp_stock(dhan, symbol)
    log.info(f"  -> timeframe={result.get('timeframe')} | status={result.get('status')} | "
             f"setup_kind={result.get('setup_kind', '-')}")
    log.info("=" * 70)


def run_sp_only():
    """
    SP-ONLY MODE: scan just the SP watchlist (~97 stocks, a few minutes) and
    update ONLY the sp_stocks section of results.json, leaving the existing
    timeframe results untouched. Lets you validate the SP logic without the
    full ~2-hour universe scan.

    Run with:  py -3.13 scanner\\scan.py sp
    """
    log.info("=" * 60)
    log.info("PranUltimate Scanner — SP-ONLY MODE")
    log.info(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    dhan = DhanData(cfg["client_id"], cfg["access_token"])

    log.info("Verifying Dhan connection (one test fetch)...")
    ok, reason = dhan.verify_connection()
    if not ok:
        log.error("=" * 60)
        log.error(f"ABORTING — Dhan connection check failed: {reason}")
        log.error("Check intraday_config.json — the access_token likely needs regenerating.")
        log.error("=" * 60)
        return
    log.info("Dhan connection OK.")

    errors = []
    sp_results = scan_sp_watchlist(dhan, errors)

    # Preserve existing main-scan results if results.json already exists, so the
    # timeframe tabs aren't wiped — we only replace the sp_stocks section.
    existing = {}
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH) as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    existing["sp_stocks"]   = sp_results
    existing["sp_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # If there was no prior full scan, fill in the required keys so the page loads.
    existing.setdefault("generated_at", existing["sp_updated_at"])
    existing.setdefault("total_signals", 0)
    existing.setdefault("total_near_breakout", 0)
    existing.setdefault("errors", len(errors))
    existing.setdefault("results", {tf: [] for tf in TIMEFRAMES})
    existing.setdefault("choppy_stocks", [])

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(existing, f, indent=2)
    log.info(f"\nSP-only results saved to: {OUTPUT_PATH}")
    log.info("(Timeframe tabs preserved from the last full scan, if any.)")
    publish_to_github()
    sync_alerts()
    publish_alerts_repo()
    if errors:
        log.warning(f"{len(errors)} errors encountered (see scan.log for details)")


def write_watchlist(deduped_breakouts, near_breakout_results, output_path):
    """
    Write watchlist.json for the intraday bot's load_watchlist_extras() —
    supplements its hardcoded 30min universe (and acts as a backup for
    5min/15min if Chartink is unavailable that day).

    Includes BOTH confirmed breakouts and near-breakout candidates — stocks
    sitting close to their ceiling today are exactly the ones worth having
    pre-seeded for tomorrow's session.
    """
    watchlist = {"5min": [], "15min": [], "30min": [], "45min": []}
    for tf in watchlist:
        syms = [s["symbol"] for s in deduped_breakouts.get(tf, [])]
        syms += [s["symbol"] for s in near_breakout_results.get(tf, [])]
        watchlist[tf] = list(dict.fromkeys(syms))  # dedupe, preserve order

    watchlist_path = os.path.join(os.path.dirname(output_path), "watchlist.json")
    with open(watchlist_path, "w") as f:
        json.dump(watchlist, f, indent=2)
    return watchlist_path


if __name__ == "__main__":
    # `py -3.13 scanner\scan.py sp`            → SP-only mode (fast, ~few minutes)
    # `py -3.13 scanner\scan.py debug VOLTAMP` → per-timeframe diagnostic for one symbol
    # `py -3.13 scanner\scan.py`               → full universe scan (~2 hours)
    if len(sys.argv) > 2 and sys.argv[1].lower() == "debug":
        debug_symbol(sys.argv[2].upper())
    elif len(sys.argv) > 1 and sys.argv[1].lower() == "sp":
        run_sp_only()
    else:
        run_scan()
