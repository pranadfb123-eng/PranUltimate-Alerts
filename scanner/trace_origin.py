"""
Standalone trace tool — does NOT modify scan.py.
Investigates why find_first_200ema_touch picked a LATER touch (e.g. May 8)
over an earlier, real origin (e.g. April 1) on a given symbol/timeframe.

For every candidate touch found while walking backward, prints:
  - date, price, ema200
  - above_count over the prior-20 lookback window (needs >= 60%)
  - the margin test value (median (close-ema200)/ema200 over that window,
    needs >= ORIGIN_ABOVE_MARGIN) and whether it passed
  - whether this candidate was ACCEPTED (first one walking backward that
    passes both tests) or just inspected-and-rejected

This shows exactly which test let a false origin (mid-base wobble) through.

Usage:
    py -3.13 C:\\Users\\prana\\PranUltimate\\scanner\\trace_origin.py BGRENERGY
"""
import sys, json

SCANNER_DIR = r"C:\Users\prana\PranUltimate\scanner"
sys.path.insert(0, SCANNER_DIR)

from scan import DhanData, add_indicators, CONFIG_PATH, ORIGIN_ABOVE_MARGIN

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
    n = len(di)
    print(f"ORIGIN_ABOVE_MARGIN = {ORIGIN_ABOVE_MARGIN}\n")
    print(f"{'idx':>5} {'date':>12} {'low':>9} {'high':>9} {'ema200':>9} {'touched':>8} {'above_cnt/20':>13} {'margin_rel':>11} {'PASS?':>6}")

    accepted = None
    for i in range(n - 2, 30, -1):
        row = di.iloc[i]
        ema200 = row["ema200"]
        if ema200 == 0:
            continue
        touched = (row["low"] <= ema200 <= row["high"])
        if not touched:
            continue

        lookback_start = max(0, i - 20)
        prior = di.iloc[lookback_start:i]
        if len(prior) < 5:
            continue
        above_count = (prior["close"] > prior["ema200"]).sum()
        above_frac = above_count / len(prior)
        prior_ema = prior["ema200"].replace(0, float("nan"))
        rel = ((prior["close"] - prior_ema) / prior_ema).median()

        passed_count = above_frac >= 0.6
        passed_margin = rel >= ORIGIN_ABOVE_MARGIN
        passed = passed_count and passed_margin

        date = str(di.index[i])[:10]
        marker = ""
        if passed and accepted is None:
            accepted = i
            marker = "<== ACCEPTED (first valid touch walking backward)"

        print(f"{i:>5} {date:>12} {row['low']:>9.2f} {row['high']:>9.2f} {ema200:>9.2f} "
              f"{'yes' if touched else 'no':>8} {above_count}/{len(prior):>9} "
              f"{rel:>11.4f} {'PASS' if passed else 'fail':>6}  {marker}")

        # Keep going well past the accepted candidate so we can see earlier
        # candidates too (e.g. an April 1 origin), instead of cutting off early.
        if accepted is not None and i < accepted - 60:
            break

if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BGRENERGY"
    tf = sys.argv[2] if len(sys.argv) > 2 else "1D"
    trace(symbol, tf)
