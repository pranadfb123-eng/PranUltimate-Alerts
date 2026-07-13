"""
Dhan Data Layer for PranUltimate Intraday Bot
=============================================
Replaces tvDatafeed with Dhan's official, stable Historical Data API.

- 5min, 15min: fetched natively from Dhan
- 30min: fetched as 5min and resampled to 30min (Dhan has no native 30min)

Endpoint: POST https://api.dhan.co/v2/charts/intraday
"""

import requests
import pandas as pd
import time
import difflib
from datetime import datetime, timedelta

BASE_URL = "https://api.dhan.co/v2"
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

# Manually verified symbol renames/mergers — only add an entry here once you've
# confirmed it's correct (e.g. checked the company's actual current NSE ticker).
# These are trusted instantly, no fuzzy guessing involved.
ALIAS_MAP = {
    # "OLD_SYMBOL": "CURRENT_DHAN_SYMBOL",
    "WOCKPHARM": "WOCKPHARMA",   # Wockhardt — your SP list ticker → Dhan's symbol
}

# Dhan native interval strings (minutes)
DHAN_INTERVAL = {
    "5min":  "5",
    "15min": "15",
    "30min": "5",     # fetch 5min then resample to 30min
    "45min": "15",    # fetch 15min then resample to 45min
    "1H":    "60",    # native 60min
    "2H":    "60",    # fetch 60min then resample to 2H
    "3H":    "60",    # fetch 60min then resample to 3H
    "4H":    "60",    # fetch 60min then resample to 4H
}
RESAMPLE_RULE = {
    "30min": "30min",
    "45min": "45min",
    "2H":    "2h",
    "3H":    "3h",
    "4H":    "4h",
}
# How many days of history to pull per timeframe (need 200+ candles for EMA200).
# 2H/3H/4H require 180 days: 90 days only yields ~186 3H and ~139 4H candles
# (both below the 220-candle minimum), causing 3H/4H signals to never fire.
# 180 days gives ~372 3H and ~279 4H candles — safely above the gate.
DAYS_BACK = {
    "5min": 12, "15min": 30, "30min": 30,
    "45min": 45, "1H": 60, "2H": 180, "3H": 180, "4H": 180,
}


class DhanData:
    def __init__(self, client_id, access_token):
        self.client_id    = client_id
        self.access_token = access_token
        self.headers = {
            "access-token": access_token,
            "client-id":    client_id,
            "Accept":       "application/json",
            "Content-Type": "application/json",
        }
        self._sec_map = {}
        self._series_map = {}
        self._all_symbols = []
        self._fuzzy_cache = {}   # symbol -> resolved Dhan symbol (or None), cached per run
        self._last_error = None  # most recent fetch failure reason, set by _get_intraday/_get_daily
        self._load_security_master()

    def _load_security_master(self):
        """
        Download Dhan scrip master → {symbol: security_id} for NSE cash-segment
        equity, restricted to SEM_SERIES "EQ" (standard mainboard shares) and
        "BE" (trade-to-trade equity).

        Excludes everything else tagged INSTRUMENT_NAME=="EQUITY" in Dhan's
        master that isn't actually common stock — confirmed via a live check:
        SG (~4,246 rows, Sovereign Gold Bond tranches), N0/N1.../NA... (~912,
        NCDs/corporate bonds, one series per issue), SM (~388, SME platform —
        separate market, not this strategy's target), plus ~130 other tiny
        exotic series. Without this filter the "universe" balloons to ~9,500
        rows that are mostly not stocks at all.
        """
        try:
            df = pd.read_csv(SCRIP_MASTER_URL, low_memory=False)
            nse = df[(df["SEM_EXM_EXCH_ID"] == "NSE") &
                     (df["SEM_SEGMENT"] == "E") &
                     (df["SEM_INSTRUMENT_NAME"] == "EQUITY") &
                     (df["SEM_SERIES"].isin(["EQ", "BE"]))]
            self._sec_map = dict(zip(
                nse["SEM_TRADING_SYMBOL"].astype(str),
                nse["SEM_SMST_SECURITY_ID"].astype(str)
            ))
            self._series_map = dict(zip(
                nse["SEM_TRADING_SYMBOL"].astype(str),
                nse["SEM_SERIES"].astype(str)
            ))
            self._all_symbols = list(self._sec_map.keys())
            eq_count = sum(1 for s in self._series_map.values() if s == "EQ")
            be_count = sum(1 for s in self._series_map.values() if s == "BE")
            print(f"Dhan security master loaded: {len(self._sec_map)} NSE cash-segment symbols "
                  f"(EQ={eq_count}, BE={be_count})")
        except Exception as e:
            print(f"Failed to load Dhan security master: {e}")
            self._sec_map = {}
            self._series_map = {}

    def get_series(self, symbol):
        """
        Returns the SEM_SERIES for a resolved symbol ("EQ" or "BE"), or None
        if unresolved. BE = trade-to-trade — typically can't be sold same-day,
        so intraday/leveraged strategies should check this before trading,
        even though it's fine to fetch data and detect signals on it.

        Follows the same alias/fuzzy resolution path as get_security_id, so
        it reflects whatever symbol the data was actually fetched under.
        """
        if symbol in self._series_map:
            return self._series_map[symbol]
        alias = ALIAS_MAP.get(symbol)
        if alias:
            return self._series_map.get(alias)
        cached = self._fuzzy_cache.get(symbol)
        if cached:
            return self._series_map.get(cached)
        return None

    def get_security_id(self, symbol):
        # 1. Exact match — trusted, instant.
        sec_id = self._sec_map.get(symbol)
        if sec_id:
            return sec_id

        # 2. Manually verified alias (renamed/merged ticker you've confirmed yourself).
        alias = ALIAS_MAP.get(symbol)
        if alias:
            sec_id = self._sec_map.get(alias)
            if sec_id:
                return sec_id

        # 3. Not found. Suggest the closest real Dhan symbol so the failure is
        # actionable instead of a silent skip — but do NOT auto-trade on a guess.
        # A string-similarity match could land on an economically different
        # instrument (e.g. a DVR share class) — verifying manually before
        # adding it to ALIAS_MAP avoids putting capital into the wrong security.
        if symbol not in self._fuzzy_cache:
            matches = difflib.get_close_matches(symbol, self._all_symbols, n=1, cutoff=0.8)
            self._fuzzy_cache[symbol] = matches[0] if matches else None
            suggestion = self._fuzzy_cache[symbol]
            if suggestion:
                print(f"  [unresolved] {symbol} not in Dhan master — closest match: "
                      f"'{suggestion}'. If correct, add \"{symbol}\": \"{suggestion}\" "
                      f"to ALIAS_MAP in dhan_data.py to enable trading on it.")
            else:
                print(f"  [unresolved] {symbol} not in Dhan master — no close match found "
                      f"(possibly delisted/renamed beyond recognition).")
        return None

    def get_all_symbols(self):
        """Full list of NSE equity symbols loaded from the Dhan security master."""
        return list(self._all_symbols)

    def verify_connection(self, test_symbol="RELIANCE", retries=3, pause=3.0):
        """
        One quick real fetch to confirm the access token and connection are
        actually working BEFORE looping over the full universe. A bad token
        makes every single call fail identically — without this check, that
        only becomes visible after burning hours scanning thousands of
        symbols that all silently return "no data".

        retries=3, pause=3.0: transient 401s from Dhan (brief API hiccups,
        not actual token expiry) caused false "token expired" Telegram alerts.
        We retry up to 3× before declaring a real failure, so a one-off
        network or API blip doesn't wake the user unnecessarily.

        Returns (True, None) on success, or (False, reason_str) on failure.
        """
        sec_id = self.get_security_id(test_symbol)
        if not sec_id:
            return False, f"Could not resolve test symbol '{test_symbol}' in security master"

        last_error = None
        for attempt in range(retries):
            self._last_error = None
            df = self._get_daily(sec_id, days_back=10)
            if df is not None and len(df) > 0:
                return True, None
            last_error = self._last_error or "Unknown failure (no data returned)"
            if attempt < retries - 1:
                import logging as _log_mod
                _log_mod.getLogger(__name__).info(
                    f"verify_connection: attempt {attempt + 1}/{retries} failed "
                    f"({last_error}) — retrying in {pause:.0f}s"
                )
                time.sleep(pause)

        return False, last_error

    def get_daily_and_weekly(self, symbol, days_back=1750, retries=2, pause=0.25):
        """
        Fetch daily candles once and derive weekly via resample — a single API
        call covers BOTH "1D" and "1W". days_back defaults to ~1750 days
        (~250 weeks) so the resampled weekly series clears the 220-candle
        minimum needed for a meaningful 200 EMA (1500 days only gives ~214
        weeks — too close to the line).

        Always sleeps `pause` after a successful call too (not just on retry)
        so a long full-universe scan stays comfortably under Dhan's 5 req/sec cap.

        Returns (df_1d, df_1w), each possibly None on failure.
        """
        sec_id = self.get_security_id(symbol)
        if not sec_id:
            return None, None

        df_1d = None
        for attempt in range(retries):
            df_1d = self._get_daily(sec_id, days_back=days_back)
            if df_1d is not None and len(df_1d) > 0:
                break
            if attempt < retries - 1:
                time.sleep(pause)
        time.sleep(pause)

        if df_1d is None:
            return None, None

        df_1w = self._resample(df_1d, "1W")
        return df_1d, df_1w

    def get_remaining_timeframes(self, symbol, retries=2, pause=0.25):
        """
        Fetch the 3 remaining unique underlying intervals (5min, 15min, 60min)
        and derive ALL of: 5min, 30min, 15min, 45min, 1H, 2H, 3H, 4H from them.
        That's 3 raw API calls instead of 8 separate get_hist() calls — call
        this AFTER get_daily_and_weekly() has already gated out excluded stocks,
        so you only spend these 3 calls on stocks worth fully evaluating.

        days_back per raw fetch is the MAX needed across everything derived
        from it, so every derived timeframe gets enough history for EMA200.
        Sleeps `pause` after each successful call for steady rate-limiting.

        Returns a dict {timeframe: DataFrame}; missing fetches are simply
        absent from the dict (no None entries).
        """
        sec_id = self.get_security_id(symbol)
        if not sec_id:
            return {}

        frames = {}

        def _fetch(interval, days_back):
            for attempt in range(retries):
                df = self._get_intraday(sec_id, interval, days_back)
                if df is not None and len(df) > 0:
                    time.sleep(pause)
                    return df
                if attempt < retries - 1:
                    time.sleep(pause)
            time.sleep(pause)
            return None

        # Native 5-min (30 days satisfies both 5min and 30min's DAYS_BACK needs)
        df5 = _fetch("5", 30)
        if df5 is not None:
            frames["5min"]  = df5
            frames["30min"] = self._resample(df5, "30min")

        # Native 15-min (45 days satisfies both 15min and 45min)
        df15 = _fetch("15", 45)
        if df15 is not None:
            frames["15min"] = df15
            frames["45min"] = self._resample(df15, "45min")

        # Native 60-min — 180 days required so 3H/4H resample clears the
        # 220-candle minimum. 90 days only yielded ~186 3H and ~139 4H
        # candles, both below 220, so 3H/4H signals never fired.
        # 180 days gives ~372 3H and ~279 4H candles — safely above the gate.
        df60 = _fetch("60", 180)
        if df60 is not None:
            frames["1H"] = df60
            frames["2H"] = self._resample(df60, "2h")
            frames["3H"] = self._resample(df60, "3h")
            frames["4H"] = self._resample(df60, "4h")

        return frames

    def get_hist(self, symbol, timeframe):
        """
        Fetch candles for a symbol on a given timeframe.
        Returns DataFrame [open, high, low, close, volume] indexed by datetime, or None.

        Handles:
          5min, 15min          → native intraday
          30min, 45min, 2H,3H,4H → resampled from native intraday
          1H                   → native 60min
          1D                   → native daily
          1W                   → resampled from daily
        """
        sec_id = self.get_security_id(symbol)
        if not sec_id:
            return None

        # Daily and weekly use the daily endpoint
        if timeframe in ("1D", "1W"):
            df = self._get_daily(sec_id)
            if df is None:
                return None
            if timeframe == "1W":
                df = self._resample(df, "1W")
            return df

        interval = DHAN_INTERVAL.get(timeframe)
        if interval is None:
            return None

        days_back = DAYS_BACK.get(timeframe, 30)
        df = self._get_intraday(sec_id, interval, days_back)
        if df is None:
            return None

        rule = RESAMPLE_RULE.get(timeframe)
        if rule:
            df = self._resample(df, rule)

        return df

    def _get_intraday(self, sec_id, interval, days_back):
        to_date   = datetime.now()
        from_date = to_date - timedelta(days=days_back)
        payload = {
            "securityId":      sec_id,
            "exchangeSegment": "NSE_EQ",
            "instrument":      "EQUITY",
            "interval":        interval,
            "oi":              False,
            "fromDate":        from_date.strftime("%Y-%m-%d %H:%M:%S"),
            "toDate":          to_date.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            r = requests.post(f"{BASE_URL}/charts/intraday",
                              headers=self.headers, json=payload, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            self._last_error = self._describe_error(e)
            return None
        return self._parse(data)

    def _get_daily(self, sec_id, days_back=500):
        to_date   = datetime.now()
        from_date = to_date - timedelta(days=days_back)
        payload = {
            "securityId":      sec_id,
            "exchangeSegment": "NSE_EQ",
            "instrument":      "EQUITY",
            "oi":              False,
            "fromDate":        from_date.strftime("%Y-%m-%d"),
            "toDate":          to_date.strftime("%Y-%m-%d"),
        }
        try:
            r = requests.post(f"{BASE_URL}/charts/historical",
                              headers=self.headers, json=payload, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            self._last_error = self._describe_error(e)
            return None
        return self._parse(data)

    @staticmethod
    def _describe_error(e):
        """Turn a requests exception into a short, actionable description."""
        resp = getattr(e, "response", None)
        if resp is not None:
            if resp.status_code == 401:
                return "401 Unauthorized — access_token is invalid or expired"
            if resp.status_code == 429:
                return "429 Rate limited — too many requests"
            return f"HTTP {resp.status_code} — {resp.text[:200]}"
        return f"{type(e).__name__}: {e}"

    @staticmethod
    def _parse(data):
        if not data or "close" not in data or not data["close"]:
            return None
        try:
            df = pd.DataFrame({
                "open":   data["open"],
                "high":   data["high"],
                "low":    data["low"],
                "close":  data["close"],
                "volume": data["volume"],
            })
            df.index = pd.to_datetime(data["timestamp"], unit="s") + pd.Timedelta(hours=5, minutes=30)
            df.index.name = "datetime"
            return df
        except Exception:
            return None

    @staticmethod
    def _resample(df, rule):
        """
        Resample 5min/15min/60min candles to a higher timeframe (e.g. 30min,
        2H) -- or daily candles to weekly ("1W").

        BUG FIXED 2026-06-25: df.resample(rule, ...) with no `origin` anchors
        bins to midnight (pandas' default) -- NOT to market open. For 30min,
        this produced candles closing at :00/:30 (e.g. 10:30:00) instead of
        the correct NSE-aligned :15/:45 (9:45, 10:15, 10:45...), since the
        market opens at 9:15, not on a clean half-hour boundary. Every
        intraday higher-timeframe candle built this way was offset by up to
        15 minutes from its real boundary (confirmed on ABB's 30min box this
        session -- a "10:30:00" candle should not exist).

        Fix: for INTRADAY rules only, anchor the resample to the actual
        first timestamp in this symbol's data (Dhan correctly returns
        intraday data starting at 9:15) via origin="start", instead of
        letting pandas default to midnight.

        Deliberately NOT applied to the "1W" rule: weekly resampling should
        stay on pandas' calendar-week default (Mon-Sun buckets), which is
        already correct and consistent across symbols regardless of when
        each stock's own price history happens to start. Anchoring weekly
        bins to "start" instead would make different stocks' weekly
        candles land on inconsistent day-of-week boundaries depending on
        their individual data history length.
        """
        agg = {
            "open":   "first",
            "high":   "max",
            "low":    "min",
            "close":  "last",
            "volume": "sum",
        }
        is_intraday_rule = rule not in ("1W", "1D")
        if is_intraday_rule:
            out = df.resample(rule, label="left", closed="left", origin="start").agg(agg).dropna()
        else:
            out = df.resample(rule, label="left", closed="left").agg(agg).dropna()
        return out


def fetch_hist_dhan(dhan_data, symbol, timeframe, retries=2, pause=1.0):
    """
    Robust wrapper with retries around DhanData.get_hist.
    Returns DataFrame or None.
    """
    for attempt in range(retries):
        df = dhan_data.get_hist(symbol, timeframe)
        if df is not None and len(df) > 0:
            return df
        if attempt < retries - 1:
            time.sleep(pause)
    return None