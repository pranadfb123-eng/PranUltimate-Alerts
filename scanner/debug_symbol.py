"""
Standalone per-timeframe debug tool for a single symbol.
=======================================================
Does NOT modify scan.py — it imports the box-model functions from it and prints
exactly what the scanner sees on every timeframe (1H-1W), so you can see why a
stock landed on a given timeframe.

Run from anywhere:
    py -3.13 C:\\Users\\prana\\PranUltimate\\scanner\\debug_symbol.py VOLTAMP
"""

import os
import sys
import json

# Make sure this folder (with scan.py) is importable, regardless of where
# the script is run from.
#
# BUG FIXED 2026-06-29: this used to ALSO add the intraday folder to
# sys.path, inserted AFTER this folder -- since sys.path.insert(0, ...)
# always inserts at the very front, whichever insert() runs LAST wins, so
# the intraday folder's dhan_data.py was silently taking priority over the
# scanner's own copy. If the two dhan_data.py files have drifted apart (they
# have -- separate fixes landed on each independently this session), this
# debug tool was running against the WRONG file the whole time. Confirmed on
# BGRENERGY: this caused debug_symbol.py to report 1186 daily candles /
# EMA200=138.60 / origin=2025-03-03, while a script pointed ONLY at the
# scanner folder correctly reported 1167 candles / EMA200=292.44 /
# origin in the April-May 2026 window -- consistent with everything
# validated earlier this session. The scanner's dhan_data.py is the correct
# one for this tool; the intraday path is no longer added at all.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Reuse everything from the existing scanner — no duplication, no edits to it.
import scan
from dhan_data import DhanData


def _build_range_box_diagnostic(df, fl_pos):
    """
    DIAGNOSTIC-ONLY replica of scan.build_range_box's exact logic -- does
    NOT modify scan.py. The real function just returns None when a box is
    too brief, with no way to see WHY (range_start may have been pushed far
    forward by noise-spike/pullback chaining, completely independent of how
    old the original first_low is -- confirmed misleading on JBMA/BHAGCHEM,
    2026-06-29: "first_low 14 candles ago" but still "<3 candles" by the
    real (chained) range_start, which the old message never showed).

    Returns (box_or_None, final_range_start_pos, final_range_start_date)
    so the caller can show the REAL reason instead of a hardcoded guess.
    """
    n = len(df)
    range_start = fl_pos + 1
    if range_start >= n - 1:
        return None, range_start, str(df.index[min(range_start, n - 1)])[:10]

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
            return ({"ceiling": ceiling, "breakout_pos": breakout_pos, "range_start": range_start},
                    range_start, str(df.index[range_start])[:10])
        else:
            if (n - 1) - range_start < 3:
                return None, range_start, str(df.index[range_start])[:10]
            return ({"ceiling": ceiling, "breakout_pos": None, "range_start": range_start},
                    range_start, str(df.index[range_start])[:10])

    return None, range_start, str(df.index[min(range_start, n - 1)])[:10]


def debug_one_timeframe(df, tf):
    """Human-readable explanation of what the box model sees on this timeframe."""
    if df is None:
        return f"  {tf:4}: no data"
    if len(df) < 220:
        return f"  {tf:4}: only {len(df)} candles (need 220+) -- skipped"
    di = scan.add_indicators(df)
    if len(di) < 60:
        return f"  {tf:4}: only {len(di)} candles after EMA warmup -- skipped"

    # TARGETED DIAGNOSTIC (2026-06-29): print exactly what this code path
    # sees for the LAST row's close/ema200, to isolate whether a reported
    # discrepancy is in the underlying data or in this computation itself.
    last_row = di.iloc[-1]
    print(f"    [diag] {tf} last row ({str(di.index[-1])[:10]}): "
          f"close={float(last_row['close']):.2f}  ema200={float(last_row['ema200']):.2f}")

    touch_idx = scan.find_first_200ema_touch(di)
    if touch_idx is None:
        return f"  {tf:4}: no 200 EMA touch found (not in a correction structure)"

    touch_date = str(di.index[touch_idx])[:10]
    candles_since_touch = (len(di) - 1) - touch_idx

    first_low, fl_pos = scan.find_first_low(di, touch_idx)
    fl_date = str(di.index[fl_pos])[:10]

    # Use the diagnostic replica (not scan.build_range_box directly) so we
    # can show the REAL reason on failure -- the original first_low's age
    # is NOT what determines "too brief"; the box's actual range_start
    # (which chaining can push far forward from fl_pos) is.
    box, final_range_start, final_range_start_date = _build_range_box_diagnostic(di, fl_pos)

    if box is None:
        candles_since_fl    = (len(di) - 1) - fl_pos
        candles_since_range = (len(di) - 1) - final_range_start
        chained = final_range_start != fl_pos + 1
        chain_note = (f" (chained forward from first_low -- real anchor is {final_range_start_date}, "
                      f"only {candles_since_range} candles old)" if chained else "")
        return (f"  {tf:4}: touch={touch_date} ({candles_since_touch} candles ago), "
                f"first_low=Rs{round(first_low,2)} on {fl_date} ({candles_since_fl} candles ago) "
                f"-- box too brief: real anchor only {candles_since_range} candle(s) old, "
                f"need 3+{chain_note}")

    ceiling      = box["ceiling"]
    breakout_pos = box["breakout_pos"]
    last_close   = float(di.iloc[-1]["close"])

    decisive = scan.first_low_decisively_broken(di, first_low, fl_pos, breakout_pos)

    range_start = box["range_start"]
    chained = range_start != fl_pos + 1
    chain_note = f" [re-anchored to {str(di.index[range_start])[:10]}]" if chained else ""

    base = (f"  {tf:4}: touch={touch_date} ({candles_since_touch} candles ago) | "
            f"first_low=Rs{round(first_low,2)} ({fl_date}) | "
            f"ceiling=Rs{round(ceiling,2)}{chain_note} | close=Rs{round(last_close,2)}")

    if decisive:
        return base + " | First Low DECISIVELY BROKEN -> no setup"

    if breakout_pos is None:
        dist = (ceiling - last_close) / last_close * 100 if last_close > 0 else 0
        return base + f" | CONSOLIDATING, {round(dist,1)}% below ceiling -> consolidation"

    candles_ago = (len(di) - 1) - breakout_pos
    bo_date = str(di.index[breakout_pos])[:10]
    if candles_ago > scan.MAX_BREAKOUT_AGE:
        return (base + f" | breakout {bo_date} = {candles_ago} candles ago "
                f"(> {scan.MAX_BREAKOUT_AGE}) -> EXPIRED, timeframe excluded")
    return (base + f" | FRESH BREAKOUT {bo_date} = {candles_ago} candles ago "
            f"-> fresh_breakout")


def main(symbol):
    print("=" * 72)
    print(f"DEBUG -- {symbol}")
    print("=" * 72)

    with open(scan.CONFIG_PATH) as f:
        cfg = json.load(f)
    dhan = DhanData(cfg["client_id"], cfg["access_token"])

    ok, reason = dhan.verify_connection()
    if not ok:
        print(f"ABORTING -- Dhan connection failed: {reason}")
        return

    sec_id = dhan.get_security_id(symbol)
    if sec_id is None:
        print(f"{symbol} did not resolve in Dhan master -- check ALIAS_MAP.")
        return
    print(f"Resolved {symbol} -> security_id={sec_id}, series={dhan.get_series(symbol)}")

    # Fetch every timeframe (same calls scan_sp_stock makes).
    df_1d, df_1w = dhan.get_daily_and_weekly(symbol)
    frames = {}
    if df_1d is not None:
        frames["1D"] = df_1d
    if df_1w is not None:
        frames["1W"] = df_1w
    frames.update(dhan.get_remaining_timeframes(symbol))

    print("\nData coverage per timeframe (first candle -> last candle):")
    for tf in scan.TF_PRIORITY:
        df = frames.get(tf)
        if df is None or len(df) == 0:
            print(f"  {tf:4}: no data")
        else:
            print(f"  {tf:4}: {len(df)} candles, {str(df.index[0])[:10]} -> {str(df.index[-1])[:10]}")

    if frames.get("1D") is not None:
        df1d = frames["1D"]
        print("\nOldest 5 daily candles (checking for a split-adjustment scale mismatch):")
        for i in range(min(5, len(df1d))):
            print(f"  {str(df1d.index[i])[:10]}  close={float(df1d.iloc[i]['close']):.2f}")

    print("\nPer-timeframe box analysis (lowest -> highest):")
    for tf in scan.TF_PRIORITY:
        print(debug_one_timeframe(frames.get(tf), tf))
        df_tf = frames.get(tf)
        if df_tf is not None and len(df_tf) >= 220:
            sp_result = scan.detect_sp_signal(df_tf, symbol, tf)
            if sp_result is None:
                print(f"        [SP Stocks view] no live setup (strict origin expired/none, "
                      f"and the fallback anchor found nothing live either)")
            else:
                print(f"        [SP Stocks view] {sp_result['setup_kind']} | {sp_result['status']} | "
                      f"resistance=Rs{sp_result.get('resistance')} | first_low=Rs{sp_result.get('first_low')}")

    # Show the actual selection the scanner would make.
    print("\nFinal selection by scan_sp_stock:")
    result = scan.scan_sp_stock(dhan, symbol)
    print(f"  -> timeframe={result.get('timeframe')} | status={result.get('status')} | "
          f"setup_kind={result.get('setup_kind', '-')}")
    print("=" * 72)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: py -3.13 scanner\\debug_symbol.py SYMBOL")
        print("Example: py -3.13 scanner\\debug_symbol.py VOLTAMP")
        sys.exit(1)
    main(sys.argv[1].upper())