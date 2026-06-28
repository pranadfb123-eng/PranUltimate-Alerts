"""
Standalone, all-timeframes BGR check -- does NOT modify scan.py or
dhan_data.py. Uses DhanData.get_hist(symbol, timeframe), which already
handles every resolution (5min/15min/30min/45min/1H/2H/3H/4H/1D/1W)
internally, so no guessing at fetch internals.

Usage:
    py -3.13 C:\\Users\\prana\\PranUltimate\\scanner\\litmus_all_tf.py BGRENERGY
"""
import sys, json

SCANNER_DIR = r"C:\Users\prana\PranUltimate\scanner"
sys.path.insert(0, SCANNER_DIR)

from scan import (
    DhanData, add_indicators, find_first_low, first_low_decisively_broken,
    build_range_box, CONFIG_PATH, ORIGIN_ABOVE_MARGIN,
)

ALL_TIMEFRAMES = ["5min", "15min", "30min", "45min", "1H", "2H", "3H", "4H", "1D", "1W"]


def find_first_200ema_touch_NEW(df):
    """Same corrected-origin logic validated on Daily/Weekly earlier --
    single-step comparison between distinct touch clusters, using
    first_low_decisively_broken to decide whether to extend back one step."""
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


def check_all_timeframes(symbol):
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    dhan = DhanData(cfg["client_id"], cfg["access_token"])
    ok, reason = dhan.verify_connection()
    if not ok:
        print(f"ABORTING -- Dhan connection failed: {reason}")
        return

    print(f"\n=== {symbol} -- ALL timeframes (5min through 1W), NEW origin logic ===")
    for tf in ALL_TIMEFRAMES:
        df = dhan.get_hist(symbol, tf)
        if df is None:
            print(f"  {tf:6}: no data")
            continue
        if len(df) < 220:
            print(f"  {tf:6}: only {len(df)} candles (need 220+) -- skipped")
            continue
        di = add_indicators(df)
        if len(di) < 60:
            print(f"  {tf:6}: only {len(di)} candles after warmup -- skipped")
            continue

        touch_idx = find_first_200ema_touch_NEW(di)
        if touch_idx is None:
            print(f"  {tf:6}: no valid touch found")
            continue
        touch_date = str(di.index[touch_idx])[:16]
        first_low, fl_pos = find_first_low(di, touch_idx)
        fl_date = str(di.index[fl_pos])[:16]
        box = build_range_box(di, fl_pos)
        last_close = float(di.iloc[-1]["close"])
        if box is None:
            print(f"  {tf:6}: touch={touch_date} first_low={round(first_low,2)}@{fl_date} -- box too brief")
            continue
        bo = box["breakout_pos"]
        decisive = first_low_decisively_broken(di, first_low, fl_pos, bo)
        if decisive:
            status = "First Low DECISIVELY BROKEN -> no setup"
        elif bo is None:
            status = f"CONSOLIDATING (close={round(last_close,2)} vs ceiling={round(box['ceiling'],2)})"
        else:
            candles_ago = (len(di) - 1) - bo
            bo_date = str(di.index[bo])[:16]
            status = f"breakout {bo_date} = {candles_ago} candles ago"
        print(f"  {tf:6}: touch={touch_date} first_low={round(first_low,2)}@{fl_date} "
              f"ceiling={round(box['ceiling'],2)} | {status}")


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BGRENERGY"
    check_all_timeframes(symbol)
