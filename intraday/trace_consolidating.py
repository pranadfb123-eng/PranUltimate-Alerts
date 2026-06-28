"""
Standalone diagnostic -- does NOT modify bot.py.
Traces is_consolidating()'s internals step by step for one symbol/timeframe,
showing exactly which check returns False.

Usage:
    py -3.13 C:\\Users\\prana\\PranUltimate\\intraday\\trace_consolidating.py KAMAHOLD 1H
"""
import sys, json

INTRADAY_DIR = r"C:\Users\prana\PranUltimate\intraday"
sys.path.insert(0, INTRADAY_DIR)

from bot import (
    DhanData, fetch_hist, add_indicators, find_first_low,
    _find_first_200ema_touch_HTF, _build_range_box_HTF, _find_fallback_low_HTF,
    CONFIG_PATH,
)


def trace(symbol, timeframe):
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    dhan = DhanData(cfg["client_id"], cfg["access_token"])

    df = fetch_hist(dhan, symbol, timeframe)
    if df is None:
        print("No data")
        return
    print(f"Raw candles: {len(df)}")
    if len(df) < 220:
        print("FAILS len < 220 check")
        return

    di = add_indicators(df)
    print(f"After add_indicators: {len(di)}")
    if len(di) < 60:
        print("FAILS len < 60 check (post-warmup)")
        return

    touch_idx = _find_first_200ema_touch_HTF(di)
    if touch_idx is None:
        print("No strict origin found -- trying FALLBACK (lowest low in window)")
        fl_pos = _find_fallback_low_HTF(di)
        if fl_pos is None:
            print("FAILS: fallback also found nothing -- this is why is_consolidating returned False")
            return
        first_low = float(di.iloc[fl_pos]["low"])
        print(f"FALLBACK first_low={round(first_low,2)} @ fl_pos={fl_pos} ({di.index[fl_pos]})")
    else:
        print(f"touch_idx={touch_idx}  date={di.index[touch_idx]}")
        first_low, fl_pos = find_first_low(di, touch_idx)
        print(f"first_low={round(first_low,2)} @ fl_pos={fl_pos} ({di.index[fl_pos]})")

    box = _build_range_box_HTF(di, fl_pos)
    if box is None:
        print("FAILS: build_range_box returned None (too brief) -- this is why is_consolidating returned False")
        return
    bo = box["breakout_pos"]
    bo_str = str(di.index[bo]) if bo is not None else "None"
    print(f"box: ceiling={round(box['ceiling'],2)}  range_start={di.index[box['range_start']]}  breakout_pos={bo_str}")

    after_fl = di.iloc[fl_pos + 1:]
    broke_fl = (after_fl["close"] < first_low).any() if len(after_fl) > 0 else False
    print(f"First Low ({first_low}) broken anywhere after fl_pos: {broke_fl}")
    if broke_fl:
        print("FAILS: First Low was broken at some point -- this is why is_consolidating returned False")
        # show exactly where
        broken_rows = after_fl[after_fl["close"] < first_low]
        print(f"  First break at: {broken_rows.index[0]}  close={broken_rows.iloc[0]['close']}")
        return

    if bo is not None:
        print(f"FAILS: box['breakout_pos'] is not None ({bo_str}) -- standing breakout, "
              f"no pullback found after it -- this is why is_consolidating returned False")
        return

    print("\n=> is_consolidating should be TRUE -- contradicts what we saw, something else is wrong")


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "KAMAHOLD"
    tf = sys.argv[2] if len(sys.argv) > 2 else "1H"
    trace(symbol, tf)
