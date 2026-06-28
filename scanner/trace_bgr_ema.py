"""
Standalone diagnostic -- does NOT modify scan.py.
Dumps BGRENERGY's last 40 daily candles with close + ema200, to check for
a sudden discontinuity (data issue / corporate action) vs. a genuine
gradual EMA trajectory.

Usage:
    py -3.13 C:\\Users\\prana\\PranUltimate\\scanner\\trace_bgr_ema.py
"""
import sys, json

SCANNER_DIR = r"C:\Users\prana\PranUltimate\scanner"
sys.path.insert(0, SCANNER_DIR)

from scan import DhanData, add_indicators, CONFIG_PATH


def trace():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    dhan = DhanData(cfg["client_id"], cfg["access_token"])
    ok, reason = dhan.verify_connection()
    if not ok:
        print(f"ABORTING -- Dhan connection failed: {reason}")
        return

    df_1d, df_1w = dhan.get_daily_and_weekly("BGRENERGY")
    di = add_indicators(df_1d)
    n = len(di)
    print(f"Total daily candles: {n}\n")
    print("Oldest 5 candles (checking for a split-adjustment scale mismatch):")
    for i in range(min(5, n)):
        row = di.iloc[i]
        print(f"  {str(di.index[i])[:10]}  close={float(row['close']):.2f}")
    print()
    print(f"{'date':>12} {'close':>10} {'ema200':>10} {'gap_pct':>10}")
    for i in range(max(0, n - 40), n):
        row = di.iloc[i]
        close = float(row["close"])
        ema = float(row["ema200"])
        gap = (close - ema) / ema * 100 if ema > 0 else 0
        print(f"{str(di.index[i])[:10]:>12} {close:>10.2f} {ema:>10.2f} {gap:>9.1f}%")


if __name__ == "__main__":
    trace()