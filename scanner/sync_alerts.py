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
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)  # atomic on Windows — no partial-write corruption


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
            status = str(row.get("status", ""))
            # "NEAR BREAKOUT" = within 5% of ceiling; "WATCHING" = valid
            # strict-origin setup still in box but farther from ceiling. Both
            # are tracked for alerts so they fire when the ceiling is crossed.
            if (status.startswith("NEAR BREAKOUT") or status.startswith("WATCHING")) and "resistance" in row and "first_low" in row:
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

    # ── Highest-TF-wins dedup across incoming candidates ─────────────────
    # scan.py already deduplicates results.json so a symbol appears on only
    # one timeframe per scan run.  But alerts accumulate across *multiple*
    # scan days: a stock may have been added as SYMBOL_2H on Monday and then
    # re-scanned as SYMBOL_1H on Tuesday (its 2H box resolved and a fresh 1H
    # box appeared), producing two active alerts that both fire.
    #
    # Guard 1 — within this batch: keep only the highest-priority TF per
    # symbol so a single scan run can never add both 1H and 2H for the same
    # stock (e.g. when it appears in both the regular tab AND sp_stocks on
    # different timeframes).
    TF_PRIORITY_ORDER = ["1H", "2H", "3H", "4H", "1D", "1W"]
    best_candidate: dict[str, tuple] = {}  # symbol -> (tf, resistance, first_low, source)
    for symbol, tf, resistance, first_low, source in candidates:
        current = best_candidate.get(symbol)
        if current is None:
            best_candidate[symbol] = (tf, resistance, first_low, source)
        else:
            current_rank = TF_PRIORITY_ORDER.index(current[0]) if current[0] in TF_PRIORITY_ORDER else -1
            new_rank     = TF_PRIORITY_ORDER.index(tf)          if tf          in TF_PRIORITY_ORDER else -1
            if new_rank > current_rank:
                best_candidate[symbol] = (tf, resistance, first_low, source)

    # Guard 2 — against the already-persisted state: if an active alert
    # already exists for this symbol on a *higher* TF, skip adding the new
    # lower-TF entry (the stock is already being tracked at the right level).
    # Likewise, if we're about to add a higher-TF entry and the state has a
    # stale lower-TF active alert, downgrade it to `disabled` so it no longer
    # fires — the higher TF is the authoritative signal.
    # (triggered/disabled entries are never touched — historical record only.)
    symbols_in_state: dict[str, list[tuple[str, str]]] = {}  # symbol -> [(tf, key), ...]
    for key, entry in state.items():
        sym = entry.get("symbol", "")
        tf  = entry.get("timeframe", "")
        symbols_in_state.setdefault(sym, []).append((tf, key))

    added = 0
    reactivated = 0
    for symbol, (tf, resistance, first_low, source) in best_candidate.items():
        key = _candidate_key(symbol, tf)
        if key in state:
            existing = state[key]
            if existing["status"] == "active":
                continue  # already live — leave it untouched

            # triggered or disabled: re-activate if scan found a NEW box at a
            # strictly higher resistance (stock stairstepped up and formed a
            # fresh consolidation above the old breakout level).
            old_resistance = existing.get("resistance", 0)
            if old_resistance > 0 and resistance > old_resistance:
                log.info(
                    f"  ↻ reactivating {symbol} [{tf}]: new box Rs{resistance:.2f} "
                    f"(prev {existing['status']} at Rs{old_resistance:.2f})"
                )
                # Preserve historical fields; update only the box-specific ones.
                state[key].update({
                    "resistance":   resistance,
                    "first_low":    first_low,
                    "status":       "active",
                    "source":       source,
                    "added_at":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "triggered_at": None,
                    "disabled_at":  None,
                })
                state[key].pop("disabled_reason", None)
                reactivated += 1
            # If new resistance <= old (stock retreated), leave the entry as-is.
            continue

        new_rank = TF_PRIORITY_ORDER.index(tf) if tf in TF_PRIORITY_ORDER else -1

        # Check whether an active alert on a different TF already exists
        existing_tfs = symbols_in_state.get(symbol, [])
        skip = False
        for existing_tf, existing_key in existing_tfs:
            existing_entry = state[existing_key]
            if existing_entry["status"] != "active":
                continue  # triggered/disabled entries don't block new ones
            existing_rank = TF_PRIORITY_ORDER.index(existing_tf) if existing_tf in TF_PRIORITY_ORDER else -1
            if existing_rank > new_rank:
                # A higher-TF active alert already covers this symbol — skip.
                log.info(f"  ↷ skipped {symbol} [{tf}]: already tracked on higher TF [{existing_tf}]")
                skip = True
                break
            elif existing_rank < new_rank:
                # New candidate is higher TF — retire the stale lower-TF active alert.
                log.info(f"  ↓ retiring lower-TF active alert {symbol} [{existing_tf}] "
                         f"(superseded by new higher TF [{tf}])")
                state[existing_key]["status"] = "disabled"
                state[existing_key]["disabled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                state[existing_key]["disabled_reason"] = f"superseded by {tf} alert"

        if skip:
            continue

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
    log.info(f"Sync complete. {added} new alerts added, {reactivated} reactivated. {active_count} total active alerts.")


def fix_existing_duplicates():
    """One-shot cleanup: retire lower-TF active alerts that are already
    superseded by a higher-TF active alert for the same symbol.
    Run once manually to clean up alerts_state.json; sync_alerts() will
    prevent new duplicates from forming going forward.
    """
    TF_PRIORITY_ORDER = ["1H", "2H", "3H", "4H", "1D", "1W"]
    state = _load_state()

    # Group active entries by symbol
    from collections import defaultdict
    active_by_symbol: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for key, entry in state.items():
        if entry.get("status") == "active":
            active_by_symbol[entry["symbol"]].append((entry["timeframe"], key))

    retired = 0
    for symbol, pairs in active_by_symbol.items():
        if len(pairs) <= 1:
            continue
        # Keep only the highest-TF active entry; retire the rest
        pairs_ranked = sorted(
            pairs,
            key=lambda x: TF_PRIORITY_ORDER.index(x[0]) if x[0] in TF_PRIORITY_ORDER else -1,
            reverse=True,
        )
        winner_tf, winner_key = pairs_ranked[0]
        for loser_tf, loser_key in pairs_ranked[1:]:
            log.info(f"  cleanup: {symbol} [{loser_tf}] disabled — superseded by active [{winner_tf}]")
            state[loser_key]["status"] = "disabled"
            state[loser_key]["disabled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state[loser_key]["disabled_reason"] = f"duplicate: superseded by {winner_tf} alert"
            retired += 1

    _save_state(state)
    log.info(f"fix_existing_duplicates: retired {retired} lower-TF duplicate active alert(s).")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--fix-duplicates":
        fix_existing_duplicates()
    else:
        sync_alerts()