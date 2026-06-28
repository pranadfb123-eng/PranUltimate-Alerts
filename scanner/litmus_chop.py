"""
Standalone litmus test -- does NOT modify scan.py.
Confirms detect_chop_signal correctly catches KAMAHOLD (no strict origin,
but genuinely rangebound) before relying on it in a full ~2hr scan.

Usage:
    py -3.13 C:\\Users\\prana\\PranUltimate\\scanner\\litmus_chop.py KAMAHOLD
"""
import sys, json

SCANNER_DIR = r"C:\Users\prana\PranUltimate\scanner"
sys.path.insert(0, SCANNER_DIR)

from scan import (
    DhanData, detect_signal, detect_chop_signal, CONFIG_PATH, TIMEFRAMES,
)


def check(symbol):
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

    print(f"\n=== {symbol} -- regular detect_signal() vs detect_chop_signal() ===")
    for tf in TIMEFRAMES:
        df = frames.get(tf)
        if df is None or len(df) < 50:
            print(f"  {tf:4}: no/insufficient data")
            continue
        category, payload = detect_signal(df, symbol, tf)
        if category is not None:
            print(f"  {tf:4}: REGULAR signal found -- {category} ({payload.get('status')}) "
                  f"-- would NOT appear in Choppy tab")
            continue
        chop = detect_chop_signal(df, symbol, tf)
        if chop is None:
            print(f"  {tf:4}: no regular signal, no chop signal either")
        else:
            print(f"  {tf:4}: CHOP -- {chop['status']} | floor={chop['first_low']} "
                  f"ceiling={chop['resistance']} close={chop['close']}")


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "KAMAHOLD"
    check(symbol)
