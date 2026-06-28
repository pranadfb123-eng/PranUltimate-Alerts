"""
Standalone litmus test — does NOT modify scan.py.
Implements the proposed NEW find_first_200ema_touch (candidate-chain version)
locally, and checks:
  1. BGRENERGY Daily -> should anchor to April 7 (idx ~1116), NOT May 8.
  2. KSL Weekly -> should still correctly show up as consolidating on Weekly
     (the case that validated the ORIGINAL find_first_200ema_touch earlier
     this session) -- i.e. confirm this change doesn't regress it.

Usage:
    py -3.13 C:\\Users\\prana\\PranUltimate\\scanner\\litmus_origin.py
"""
import sys, json

SCANNER_DIR = r"C:\Users\prana\PranUltimate\scanner"
sys.path.insert(0, SCANNER_DIR)

from scan import (
    DhanData, add_indicators, find_first_low, first_low_decisively_broken,
    build_range_box, CONFIG_PATH, ORIGIN_ABOVE_MARGIN,
)


def find_first_200ema_touch_NEW(df):
    """Proposed replacement -- candidate chain, stops at first decisive break."""
    n = len(df)
    if n < 60:
        return None

    candidates = []
    for i in range(n - 2, 30, -1):
        row = df.iloc[i]
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
        if not (rel >= ORIGIN_ABOVE_MARGIN):
            continue
        candidates.append(i)

    if not candidates:
        return None

    # Candidates are in descending order (most recent first). Many adjacent
    # days within the same flat-EMA touch event all pass individually --
    # collapse each run of CONSECUTIVE indices into one representative (the
    # most recent day of that cluster) so comparisons happen between
    # distinct touch EVENTS, not adjacent days of the same event.
    clusters = []
    prev = None
    for c in candidates:
        if prev is not None and prev - c == 1:
            prev = c
            continue  # still inside the same consecutive run, skip
        clusters.append(c)
        prev = c

    best = clusters[0]
    chain = [best]
    if len(clusters) > 1:
        cand = clusters[1]
        fl_price, fl_pos = find_first_low(df, cand)
        broken = first_low_decisively_broken(df, fl_price, fl_pos, best)
        if not broken:
            best = cand
            chain.append(best)
        # Only ONE comparison -- do not keep walking further back regardless
        # of outcome.

    return best, chain


def check(symbol, timeframe, label_expect):
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    dhan = DhanData(cfg["client_id"], cfg["access_token"])
    ok, reason = dhan.verify_connection()
    if not ok:
        print(f"ABORTING -- Dhan connection failed: {reason}")
        return

    df_1d, df_1w = dhan.get_daily_and_weekly(symbol)
    df = df_1d if timeframe == "1D" else df_1w
    if df is None:
        print(f"{symbol} {timeframe}: no data")
        return
    di = add_indicators(df)
    if len(di) < 60:
        print(f"{symbol} {timeframe}: insufficient candles after warmup")
        return

    result = find_first_200ema_touch_NEW(di)
    if result is None:
        print(f"{symbol} {timeframe}: NEW function found no valid touch -- {label_expect}")
        return
    touch_idx, chain = result
    touch_date = str(di.index[touch_idx])[:10]
    print(f"\n=== {symbol} {timeframe} ===")
    print(f"Expected: {label_expect}")
    print(f"NEW touch_idx={touch_idx}  date={touch_date}")
    print(f"Candidate chain walked (most-recent -> final): "
          f"{[str(di.index[c])[:10] for c in chain]}")

    first_low, fl_pos = find_first_low(di, touch_idx)
    print(f"first_low={round(first_low,2)} @ {di.index[fl_pos]}")
    box = build_range_box(di, fl_pos)
    if box is None:
        print("build_range_box: None (too brief)")
    else:
        bo = box["breakout_pos"]
        bo_str = str(di.index[bo])[:10] if bo is not None else "None (still consolidating)"
        print(f"ceiling={round(box['ceiling'],2)}  breakout_pos={bo_str}")


def check_all_timeframes(symbol):
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    dhan = DhanData(cfg["client_id"], cfg["access_token"])
    ok, reason = dhan.verify_connection()
    if not ok:
        print(f"ABORTING -- Dhan connection failed: {reason}")
        return

    df_1d, df_1w = dhan.get_daily_and_weekly(symbol)
    frames = {}
    if df_1d is not None:
        frames["1D"] = df_1d
    if df_1w is not None:
        frames["1W"] = df_1w
    frames.update(dhan.get_remaining_timeframes(symbol))

    print(f"\n=== {symbol} -- all timeframes, NEW origin logic ===")
    for tf in ["1H", "2H", "3H", "4H", "1D", "1W"]:
        df = frames.get(tf)
        if df is None:
            print(f"  {tf:4}: no data")
            continue
        if len(df) < 220:
            print(f"  {tf:4}: only {len(df)} candles (need 220+) -- skipped")
            continue
        di = add_indicators(df)
        if len(di) < 60:
            print(f"  {tf:4}: only {len(di)} candles after warmup -- skipped")
            continue

        result = find_first_200ema_touch_NEW(di)
        if result is None:
            print(f"  {tf:4}: no valid touch found")
            continue
        touch_idx, chain = result
        touch_date = str(di.index[touch_idx])[:10]
        first_low, fl_pos = find_first_low(di, touch_idx)
        fl_date = str(di.index[fl_pos])[:10]
        box = build_range_box(di, fl_pos)
        last_close = float(di.iloc[-1]["close"])
        if box is None:
            print(f"  {tf:4}: touch={touch_date} first_low={round(first_low,2)}@{fl_date} -- box too brief")
            continue
        bo = box["breakout_pos"]
        decisive = first_low_decisively_broken(di, first_low, fl_pos, bo)
        if decisive:
            status = "First Low DECISIVELY BROKEN -> no setup"
        elif bo is None:
            status = f"CONSOLIDATING (close={round(last_close,2)} vs ceiling={round(box['ceiling'],2)})"
        else:
            candles_ago = (len(di) - 1) - bo
            bo_date = str(di.index[bo])[:10]
            status = f"breakout {bo_date} = {candles_ago} candles ago"
        print(f"  {tf:4}: touch={touch_date} first_low={round(first_low,2)}@{fl_date} "
              f"ceiling={round(box['ceiling'],2)} | {status}")


if __name__ == "__main__":
    check("BGRENERGY", "1D", "should anchor April 7, NOT May 8")
    check("KSL", "1W", "should still show Weekly consolidation (regression check)")
    check_all_timeframes("BGRENERGY")
