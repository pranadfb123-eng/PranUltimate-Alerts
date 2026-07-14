"""
Standalone diagnostic -- does NOT modify bot.py.
Finds ABB's 30min box and prints the actual breakout candle's close price,
the ceiling it broke above, and why it was rejected (volume etc).

Usage:
    py -3.13 C:\\Users\\prana\\PranUltimate\\intraday\\trace_breakout_price.py ABB 30min
"""
import sys, json

INTRADAY_DIR = r"C:\Users\prana\PranUltimate\intraday"
sys.path.insert(0, INTRADAY_DIR)

from bot import (
    DhanData, fetch_hist, add_indicators, find_200ema_touch, find_first_low,
    build_range_box, CONFIG_PATH,
)


def trace(symbol, timeframe):
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    dhan = DhanData(cfg["client_id"], cfg["access_token"])

    df = fetch_hist(dhan, symbol, timeframe)
    if df is None:
        print("No data")
        return
    di = add_indicators(df)
    if len(di) < 60:
        print("Not enough candles")
        return

    touch_idx = find_200ema_touch(di)
    if touch_idx is None:
        print("No 200 EMA touch found")
        return
    first_low, fl_pos = find_first_low(di, touch_idx)
    box = build_range_box(di, fl_pos)
    if box is None:
        print("Box too brief")
        return

    bo = box["breakout_pos"]
    print(f"First low: ₹{round(first_low,2)} @ {di.index[fl_pos]}")
    print(f"Ceiling (resistance): ₹{round(box['ceiling'],2)}")

    if bo is None:
        print("Still consolidating -- no breakout candle yet")
        return

    row = di.iloc[bo]
    candles_ago = (len(di) - 1) - bo
    print(f"\nBreakout candle: {di.index[bo]}  ({candles_ago} candles ago from latest)")
    print(f"  Open:   ₹{round(float(row['open']),2)}")
    print(f"  High:   ₹{round(float(row['high']),2)}")
    print(f"  Low:    ₹{round(float(row['low']),2)}")
    print(f"  Close:  ₹{round(float(row['close']),2)}  (broke above ceiling ₹{round(box['ceiling'],2)})")
    print(f"  RSI14:  {round(float(row['rsi14']),1)}")
    print(f"  Volume: {int(row['volume'])}  vs avg {int(row['vol_avg'])}  ({round(float(row['volume']/row['vol_avg']),2)}x)")

    last = di.iloc[-1]
    print(f"\nLatest candle ({di.index[-1]}):")
    print(f"  Close:  ₹{round(float(last['close']),2)}")
    print(f"  Volume: {int(last['volume'])} vs avg {int(last['vol_avg'])} ({round(float(last['volume']/last['vol_avg']),2)}x)")


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "ABB"
    tf = sys.argv[2] if len(sys.argv) > 2 else "30min"
    trace(symbol, tf)