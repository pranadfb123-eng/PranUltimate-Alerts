"""
Standalone litmus test -- does NOT modify scan.py.

New rule for chaining past a REAL (non-noise) breakout:
  After finding a real breakout (>=3 candles from range_start), check
  whether price EVER comes back down and re-enters the old range
  (low <= old ceiling) at any point afterward.
    - If yes: that's a genuine pullback -> a distinct new base may have
      started there. Re-anchor range_start at the pullback candle and
      keep building forward.
    - If no: price broke out and kept going / stayed above -- that's a
      genuine continuation of the same move, not a separate structure.
      Return this breakout as final.

This should NOT affect KSL (it never breaks out in the first place, so
this new branch never triggers) but SHOULD let BGR Daily chain past its
April 8 breakout into the later May-June box.

Usage:
    py -3.13 C:\\Users\\prana\\PranUltimate\\scanner\\litmus_pullback.py
"""
import sys, json

SCANNER_DIR = r"C:\Users\prana\PranUltimate\scanner"
sys.path.insert(0, SCANNER_DIR)

from scan import (
    DhanData, add_indicators, find_first_low, first_low_decisively_broken,
    CONFIG_PATH, ORIGIN_ABOVE_MARGIN,
)


def find_first_200ema_touch_NEW(df):
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
        broken = first_low_decisively_broken(df, fl_price, fl_pos, best)
        if not broken:
            best = cand
    return best


def build_range_box_PULLBACK(df, fl_pos):
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
                # noise spike -- re-anchor as before
                range_start = breakout_pos
                continue

            # Real breakout. Check if price ever pulls back into the old
            # range afterward.
            old_ceiling = ceiling
            pullback_pos = None
            for j in range(breakout_pos + 1, n):
                if df.iloc[j]["low"] <= old_ceiling:
                    pullback_pos = j
                    break

            if pullback_pos is not None:
                range_start = pullback_pos
                continue
            else:
                return {"ceiling": ceiling, "breakout_pos": breakout_pos,
                        "range_start": range_start, "pullback": False}
        else:
            if (n - 1) - range_start < 3:
                return None
            return {"ceiling": ceiling, "breakout_pos": None,
                    "range_start": range_start, "pullback": False}

    return None


def check(symbol, timeframe, label):
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
        print(f"{symbol} {timeframe}: insufficient candles")
        return

    touch_idx = find_first_200ema_touch_NEW(di)
    if touch_idx is None:
        print(f"{symbol} {timeframe}: no valid touch -- {label}")
        return
    first_low, fl_pos = find_first_low(di, touch_idx)
    box = build_range_box_PULLBACK(di, fl_pos)

    print(f"\n=== {symbol} {timeframe} ===  ({label})")
    print(f"touch={di.index[touch_idx]}  first_low={round(first_low,2)}@{di.index[fl_pos]}")
    if box is None:
        print("build_range_box_PULLBACK: None (too brief)")
        return
    bo = box["breakout_pos"]
    bo_str = str(di.index[bo])[:16] if bo is not None else "None (still consolidating)"
    print(f"FINAL ceiling={round(box['ceiling'],2)}  range_start={di.index[box['range_start']]}  breakout_pos={bo_str}")


if __name__ == "__main__":
    check("BGRENERGY", "1D", "expect: should chain past April 8 into the later May-June box (~368 ceiling)")
    check("KSL", "1W", "expect: unaffected, still consolidating, same numbers as last validated run")
