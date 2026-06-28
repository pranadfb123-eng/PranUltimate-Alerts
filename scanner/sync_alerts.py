"""
sync_alerts.py
===============
Run this right after scan.py's daily 16:00 scan (either call it from inside
scan.py's run_scan()/run_sp_only(), right after publish_to_github(), or
schedule it a minute later in Task Scheduler).

Reads server/results.json and pulls every CURRENTLY-CONSOLIDATING 1H/2H
candidate (i.e. waiting to break out, not already broken out) from:
  - results["1H"] / results["2H"]      (setup_kind == "consolidation")
  - sp_stocks                          (timeframe in ("1H","2H"),
                                         status starts with "NEAR BREAKOUT")
  - choppy_stocks                      (timeframe in ("1H","2H"),
                                         status == "CONSOLIDATING")

For each NEW symbol+timeframe combo not already tracked, adds an `active`
alert entry to alerts_state.json with its resistance + first_low. Existing
entries (active, triggered, or disabled) are left untouched — this script
only ever ADDS, never resets or removes.

alerts_state.json lives in server/ alongside results.json and watchlist.json,
and gets committed to the same GitHub repo so the GitHub Actions checker can
read it.
"""

import json
import os
import logging
from datetime import datetime

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
RESULTS_PATH = os.path.join(BASE_DIR, "..", "server", "results.json")
STATE_PATH   = os.path.join(BASE_DIR, "..", "server", "alerts_state.json")

ALERT_TIMEFRAMES = ("1H", "2H")

# Temporary scope limiter while testing the alert pipeline end-to-end.
# Set to True once you're ready to bring Choppy Stocks back into scope.
INCLUDE_CHOPPY_STOCKS = False

# Explicit list of ETF/index-fund symbols seen in scan output (from today's
# choppy_stocks run) that track an index rather than trading on PranUltimate's
# box-model structure. Exact-match only — deliberately NOT substring matching,
# since broad patterns like "HDFC" or "SBI" would also wrongly catch real
# stocks (HDFCBANK, SBIN, SBILIFE). Add new ones here as they show up.
KNOWN_ETF_SYMBOLS = {
    "AXISGOLD", "QGOLDHALF", "GOLDIETF", "GOLDBETA", "GOLDCASE", "GOLDBEES",
    "EGOLD", "TATAGOLD", "NIFTYBEES", "NIFTYIETF", "NIFTY1", "NIFTYCASE",
    "LICNETFN50", "LICNMID100", "SETFNIF50", "NV20BEES", "NV20IETF",
    "MIDQ50ADD", "MIDCAPADD", "MIDSMALL", "MID150CASE", "MOM50", "MOMOMENTUM",
    "MOQUALITY", "MOENERGY", "MOALPHA50", "MOIPO", "MOSERVICE", "MOPSE",
    "MOSMALL250", "MOBANK10", "MONIFTY100", "MULTICAP", "ENIFTY", "ESENSEX",
    "EBBETF0433", "MSCIINDIA", "CONSUMBEES", "MANUFGBEES", "PVTBANKADD",
    "EQUAL50ADD", "HDFCGROWTH", "HDFCVALUE", "HDFCSENSEX", "HDFCMID150",
    "HDFCQUAL", "HDFCNIFTY", "HDFCLOWVOL", "HDFCBSE500", "SBIETFCON",
    "SBIETFQLTY", "SBILIQETF", "SBIMIDMOM", "ABSL10BANK", "AXISCETF",
    "QUAL30IETF", "VAL30IETF", "NEXT50ETF", "SENSEXIETF", "SENSEXBETA",
    "SMALLCAP", "SMALL250", "SML100CASE", "DIVIDEND", "INFRA", "DEFENCE",
    "ENERGY", "VALUE", "METALIETF", "AONENIFTY", "AONETMMQ50", "OILIETF",
    "NIFTY100EW", "GROWWSC250", "GROWWMC150", "GROWWPSE", "GROWWCHEM",
    "GROWWDEFNC", "MIDCAPADD", "LICNETFN50", "LTGILTCASE", "CPSEETF",
    "BSLNIFTY", "ELM250",
}


def _is_etf(symbol: str) -> bool:
    return symbol.upper() in KNOWN_ETF_SYMBOLS

log = logging.getLogger(__name__)
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")


def _load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            log.warning("alerts_state.json unreadable — starting fresh.")
    return {}


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def _candidate_key(symbol, timeframe):
    return f"{symbol}_{timeframe}"


def _collect_candidates(results):
    """Return a list of (symbol, timeframe, resistance, first_low, source)
    for every stock currently consolidating (not yet broken out) on 1H/2H,
    across the regular tabs, SP Stocks, and Choppy Stocks."""
    candidates = []

    # ── Regular per-timeframe tabs ──────────────────────────────────────
    for tf in ALERT_TIMEFRAMES:
        for row in results.get("results", {}).get(tf, []):
            if row.get("setup_kind") == "consolidation" and "resistance" in row and "first_low" in row:
                candidates.append((row["symbol"], tf, row["resistance"], row["first_low"], "regular"))

    # ── SP Stocks ────────────────────────────────────────────────────────
    for row in results.get("sp_stocks", []):
        tf = row.get("timeframe")
        if tf in ALERT_TIMEFRAMES and str(row.get("status", "")).startswith("NEAR BREAKOUT"):
            if "resistance" in row and "first_low" in row:
                candidates.append((row["symbol"], tf, row["resistance"], row["first_low"], "sp_stocks"))

    # ── Choppy Stocks ────────────────────────────────────────────────────
    # Gated off for now (INCLUDE_CHOPPY_STOCKS=False) while testing the
    # pipeline on the more curated SP Stocks list. ETF/index-fund symbols
    # are excluded even when re-enabled.
    if INCLUDE_CHOPPY_STOCKS:
        for row in results.get("choppy_stocks", []):
            tf = row.get("timeframe")
            if tf in ALERT_TIMEFRAMES and row.get("status") == "CONSOLIDATING":
                if _is_etf(row["symbol"]):
                    continue
                if "resistance" in row and "first_low" in row:
                    candidates.append((row["symbol"], tf, row["resistance"], row["first_low"], "choppy_stocks"))

    return candidates


def sync_alerts():
    if not os.path.exists(RESULTS_PATH):
        log.error(f"results.json not found at {RESULTS_PATH} — run scan.py first.")
        return

    with open(RESULTS_PATH) as f:
        results = json.load(f)

    state = _load_state()
    candidates = _collect_candidates(results)

    added = 0
    for symbol, tf, resistance, first_low, source in candidates:
        key = _candidate_key(symbol, tf)
        if key in state:
            continue  # already tracked (active/triggered/disabled) — leave it
        state[key] = {
            "symbol":     symbol,
            "timeframe":  tf,
            "resistance": resistance,
            "first_low":  first_low,
            "status":     "active",
            "source":     source,
            "added_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "triggered_at": None,
        }
        added += 1
        log.info(f"  + new alert: {symbol} [{tf}] resistance=Rs{resistance} (source={source})")

    _save_state(state)
    active_count = sum(1 for v in state.values() if v["status"] == "active")
    log.info(f"Sync complete. {added} new alerts added. {active_count} total active alerts.")


if __name__ == "__main__":
    sync_alerts()