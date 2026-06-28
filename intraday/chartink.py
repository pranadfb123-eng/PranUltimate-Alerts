"""
Chartink Screener Fetcher for PranUltimate Bot
==============================================
Pulls candidate stocks from two Chartink screeners that detect the 200 EMA
touch (PranUltimate stages 1-2) across the full NSE universe.

These candidates are then fed to the bot, which fetches fresh real-time Dhan
data and runs the precise box-model breakout detection on them.

Free Chartink data is delayed ~30-45 min, but that's acceptable here because
the screeners catch the 200 EMA *touch* (a slow-forming setup). The actual
*breakout* is detected on fresh Dhan data by the bot.
"""

import json
import os
import requests
from bs4 import BeautifulSoup

CHARTINK_BASE = "https://chartink.com"
PROCESS_URL   = "https://chartink.com/screener/process"

# CHANGED 2026-06-25: the old 5min/15min clauses checked historical offsets
# up to [-8] candles back (40 min on 5min) before all conditions aligned --
# that lag was the root cause of most "stale breakout" cases this session
# (the bot never saw the symbol until well after its actual breakout).
# Replaced with a single, current-candle ([0]) touch condition across all
# four timeframes -- fires the instant a candle touches the 200 EMA, zero
# structural lag. Everything downstream (does a real box exist, where's the
# ceiling, is it broken) is the bot's own job via detect_signal/
# active_watchlist -- this screener's only job now is fast, lag-free
# discovery.
def _touch_clause(minutes):
    return (
        f"( {{cash}} (  [0] {minutes} minute low <=  [0] {minutes} minute ema(  [0] {minutes} minute close , 200 ) "
        f"and  [0] {minutes} minute high >=  [0] {minutes} minute ema(  [0] {minutes} minute close , 200 ) ) )"
    )

SCAN_CLAUSE_5MIN  = _touch_clause(5)
SCAN_CLAUSE_15MIN = _touch_clause(15)
SCAN_CLAUSE_30MIN = _touch_clause(30)
SCAN_CLAUSE_45MIN = _touch_clause(45)

SCREENERS = {
    "5min":  {"url": f"{CHARTINK_BASE}/screener/5minutebot",  "clause": SCAN_CLAUSE_5MIN},
    "15min": {"url": f"{CHARTINK_BASE}/screener/15minutebot", "clause": SCAN_CLAUSE_15MIN},
    "30min": {"url": f"{CHARTINK_BASE}/screener/30minutebot", "clause": SCAN_CLAUSE_30MIN},
    "45min": {"url": f"{CHARTINK_BASE}/screener/45minutebot", "clause": SCAN_CLAUSE_45MIN},
}

# Persisted watchlist path — same directory as this file
CHARTINK_WATCHLIST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "chartink_watchlist.json"
)


def fetch_chartink_candidates(timeframe, retries=2):
    """
    Fetch the list of NSE symbols matching the Chartink screener for the
    given timeframe ("5min", "15min", "30min", or "45min").

    Returns a list of NSE symbols (strings), or [] on failure.
    """
    screener = SCREENERS.get(timeframe)
    if not screener:
        return []

    for attempt in range(retries):
        try:
            with requests.Session() as s:
                # Step 1: get the page to obtain the CSRF token
                r = s.get(screener["url"], timeout=15)
                soup = BeautifulSoup(r.text, "html.parser")
                csrf_tag = soup.select_one("[name='csrf-token']")
                if not csrf_tag:
                    continue
                csrf = csrf_tag["content"]

                # Step 2: POST the scan clause
                s.headers.update({
                    "x-csrf-token": csrf,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer":      screener["url"],
                })
                resp = s.post(PROCESS_URL,
                              data={"scan_clause": screener["clause"]},
                              timeout=20)
                resp.raise_for_status()
                data = resp.json()

                # Extract NSE symbols from the result
                rows = data.get("data", [])
                symbols = [row["nsecode"] for row in rows if "nsecode" in row]
                return symbols

        except Exception:
            pass

    return []


def save_watchlist():
    """
    Fetch current Chartink candidates for all screener timeframes and persist
    to chartink_watchlist.json (same directory as this file).

    Call this at end of session (3:20 square-off) so next morning's pre-9:45
    window can still catch yesterday's setups without relying on stale Chartink data.

    Returns the watchlist dict on success, {} on failure.
    """
    watchlist = {}
    for tf in ["5min", "15min", "30min", "45min"]:
        candidates = fetch_chartink_candidates(tf)
        watchlist[tf] = candidates
    try:
        with open(CHARTINK_WATCHLIST_PATH, "w") as f:
            json.dump(watchlist, f, indent=2)
        return watchlist
    except Exception:
        return {}


def load_watchlist(timeframe=None):
    """
    Load the persisted Chartink watchlist from disk.

    If timeframe is given (e.g. "5min"), returns a list of symbols for that timeframe.
    If timeframe is None, returns the full {timeframe: [symbols]} dict.
    Returns [] / {} if the file doesn't exist or is corrupt.
    """
    try:
        with open(CHARTINK_WATCHLIST_PATH) as f:
            data = json.load(f)
        if timeframe is not None:
            return data.get(timeframe, [])
        return data
    except Exception:
        return [] if timeframe is not None else {}


if __name__ == "__main__":
    # Quick test
    for tf in ["5min", "15min", "30min", "45min"]:
        syms = fetch_chartink_candidates(tf)
        print(f"\n{tf} screener: {len(syms)} candidates")
        print(syms[:20])