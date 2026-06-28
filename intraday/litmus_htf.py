"""
Standalone litmus test -- does NOT modify bot.py.
Confirms the fixed is_consolidating() (HTF-only origin + pullback-chaining
logic) correctly returns True for KAMAHOLD on 1H -- the exact case that
slipped through today and let a 5min trade fire despite an obvious, real
1H consolidation.

Also re-checks 45min/2H/3H/4H for completeness, and runs has_higher_tf_consolidation
end-to-end exactly as the bot would call it before an entry.

Usage:
    py -3.13 C:\\Users\\prana\\PranUltimate\\intraday\\litmus_htf.py KAMAHOLD
"""
import sys, json

INTRADAY_DIR = r"C:\Users\prana\PranUltimate\intraday"
sys.path.insert(0, INTRADAY_DIR)

from bot import (
    DhanData, fetch_hist, is_consolidating, has_higher_tf_consolidation,
    CONFIG_PATH,
)

HIGHER_TFS = ["45min", "1H", "2H", "3H", "4H"]


def check(symbol):
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    dhan = DhanData(cfg["client_id"], cfg["access_token"])

    print(f"\n=== {symbol} -- per-timeframe is_consolidating() ===")
    for tf in HIGHER_TFS:
        df = fetch_hist(dhan, symbol, tf)
        if df is None:
            print(f"  {tf:6}: no data")
            continue
        if len(df) < 220:
            print(f"  {tf:6}: only {len(df)} candles (need 220+) -- skipped")
            continue
        result = is_consolidating(df)
        print(f"  {tf:6}: is_consolidating = {result}")

    print(f"\n=== {symbol} -- has_higher_tf_consolidation() (as called before entry) ===")
    verdict = has_higher_tf_consolidation(dhan, symbol, "5min")
    if verdict:
        print(f"  VETO -- would SKIP trade (consolidating on [{verdict}])")
    else:
        print(f"  No veto -- trade would proceed (no higher TF found consolidating)")


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "KAMAHOLD"
    check(symbol)
