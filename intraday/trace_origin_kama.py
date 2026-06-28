"""
Standalone diagnostic -- does NOT modify bot.py.
Same candidate dump used to debug BGR's origin, applied to KAMAHOLD 1H.

Usage:
    py -3.13 C:\\Users\\prana\\PranUltimate\\intraday\\trace_origin_kama.py KAMAHOLD 1H
"""
import sys, json

INTRADAY_DIR = r"C:\Users\prana\PranUltimate\intraday"
sys.path.insert(0, INTRADAY_DIR)

from bot import DhanData, fetch_hist, add_indicators, CONFIG_PATH, ORIGIN_ABOVE_MARGIN_HTF


def trace(symbol, timeframe):
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    dhan = DhanData(cfg["client_id"], cfg["access_token"])

    df = fetch_hist(dhan, symbol, timeframe)
    if df is None:
        print("No data")
        return
    di = add_indicators(df)
    n = len(di)
    print(f"ORIGIN_ABOVE_MARGIN_HTF = {ORIGIN_ABOVE_MARGIN_HTF}\n")
    print(f"{'idx':>5} {'date':>20} {'low':>9} {'high':>9} {'ema200':>9} {'touched':>8} {'above_cnt/20':>13} {'margin_rel':>11} {'PASS?':>6}")

    shown = 0
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
        passed = (above_frac >= 0.6) and (rel >= ORIGIN_ABOVE_MARGIN_HTF)

        date = str(di.index[i])[:16]
        print(f"{i:>5} {date:>20} {row['low']:>9.2f} {row['high']:>9.2f} {ema200:>9.2f} "
              f"yes {above_count}/{len(prior):>9} {rel:>11.4f} {'PASS' if passed else 'fail':>6}")
        shown += 1
        if shown >= 25:
            break


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "KAMAHOLD"
    tf = sys.argv[2] if len(sys.argv) > 2 else "1H"
    trace(symbol, tf)
