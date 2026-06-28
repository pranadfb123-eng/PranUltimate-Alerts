"""
Standalone trace tool — does NOT modify scan.py.
Dumps candle-by-candle ceiling tracking from the first low through the
breakout, so we can see exactly where build_range_box's running ceiling
diverges from the real chart (e.g. the 309 vs 349.5 question on BGR Daily).

Usage:
    py -3.13 C:\\Users\\prana\\PranUltimate\\scanner\\trace_box.py BGRENERGY

Run it with the full path; it inserts the scanner folder onto sys.path
itself so `import scan` / `import dhan_data` work regardless of cwd.
"""
import sys, json

SCANNER_DIR = r"C:\Users\prana\PranUltimate\scanner"
sys.path.insert(0, SCANNER_DIR)

from scan import (
    DhanData, add_indicators, find_first_200ema_touch, find_first_low,
    CONFIG_PATH,
)

def trace(symbol, timeframe="1D"):
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
        print(f"No data for {symbol} on {timeframe}")
        return

    di = add_indicators(df)
    touch_idx = find_first_200ema_touch(di)
    if touch_idx is None:
        print("No 200EMA touch found.")
        return
    first_low, fl_pos = find_first_low(di, touch_idx)
    print(f"touch_idx={touch_idx}  date={di.index[touch_idx]}")
    print(f"first_low={first_low}  fl_pos={fl_pos}  date={di.index[fl_pos]}")

    n = len(di)
    range_start = fl_pos + 1
    ceiling = float(di.iloc[range_start]["high"])
    print(f"\nSeed ceiling from candle at range_start={range_start} ({di.index[range_start]}): {ceiling}")
    print(f"\n{'idx':>5} {'date':>12} {'open':>9} {'high':>9} {'low':>9} {'close':>9} {'ceiling_before':>14} {'action':>30}")

    for i in range(range_start, n):
        row = di.iloc[i]
        date = di.index[i]
        ceiling_before = ceiling
        action = "-"
        if i > range_start:
            if row["close"] > ceiling:
                action = f"BREAKOUT (close {row['close']:.2f} > ceiling {ceiling:.2f})"
            elif row["high"] > ceiling:
                ceiling = float(row["high"])
                action = f"ceiling updated -> {ceiling:.2f}"
        print(f"{i:>5} {str(date)[:10]:>12} {row['open']:>9.2f} {row['high']:>9.2f} {row['low']:>9.2f} {row['close']:>9.2f} {ceiling_before:>14.2f} {action:>30}")
        if "BREAKOUT" in action:
            print("\n--- breakout fired here, continuing 5 more candles for context ---")
            for j in range(i + 1, min(n, i + 6)):
                r2 = di.iloc[j]
                print(f"{j:>5} {str(di.index[j])[:10]:>12} {r2['open']:>9.2f} {r2['high']:>9.2f} {r2['low']:>9.2f} {r2['close']:>9.2f}")
            break

if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BGRENERGY"
    tf = sys.argv[2] if len(sys.argv) > 2 else "1D"
    trace(symbol, tf)
