"""
Standalone diagnostic -- does NOT modify scan.py.
Traces exactly what detect_sp_signal's fallback path does for BHAGCHEM on
1D and 1W: EMA-disconnect check, find_fallback_low's chosen anchor, and the
resulting build_range_box outcome.

Usage:
    py -3.13 C:\\Users\\prana\\PranUltimate\\scanner\\trace_bhagchem_fallback.py
"""
import sys, json

SCANNER_DIR = r"C:\Users\prana\PranUltimate\scanner"
sys.path.insert(0, SCANNER_DIR)

from scan import (
    DhanData, CONFIG_PATH, add_indicators, find_first_200ema_touch,
    find_fallback_low, build_range_box, first_low_decisively_broken,
)


def trace(symbol, tf, df):
    print(f"\n=== {symbol} {tf} ===")
    if df is None:
        print("  no data")
        return
    print(f"  raw candles: {len(df)}")
    di = add_indicators(df)
    print(f"  after indicators: {len(di)}")
    if len(di) < 60:
        print("  too few candles after warmup")
        return

    touch_idx = find_first_200ema_touch(di)
    print(f"  strict touch_idx: {touch_idx} ({'found' if touch_idx is not None else 'NONE -- fallback should try'})")

    last = di.iloc[-1]
    close_now = float(last["close"])
    ema_now = float(last["ema200"])
    gap_pct = abs(close_now - ema_now) / ema_now * 100 if ema_now > 0 else None
    print(f"  close={close_now:.2f}  ema200={ema_now:.2f}  gap={gap_pct:.1f}%  "
          f"(EMA-disconnect cap is 50% -- {'PASSES, fallback allowed' if gap_pct and gap_pct <= 50 else 'FAILS, fallback REJECTED'})")

    if gap_pct is not None and gap_pct <= 50:
        fl_pos = find_fallback_low(di)
        if fl_pos is None:
            print("  find_fallback_low returned None")
            return
        first_low = float(di.iloc[fl_pos]["low"])
        print(f"  fallback anchor: fl_pos={fl_pos}  date={di.index[fl_pos]}  first_low={first_low:.2f}")

        box = build_range_box(di, fl_pos)
        if box is None:
            print("  build_range_box: None (too brief, even via fallback anchor)")
            return
        bo = box["breakout_pos"]
        bo_str = str(di.index[bo]) if bo is not None else "None (still consolidating)"
        print(f"  box: ceiling={box['ceiling']:.2f}  range_start={di.index[box['range_start']]}  breakout_pos={bo_str}")
        broken = first_low_decisively_broken(di, first_low, fl_pos, bo)
        print(f"  decisively broken: {broken}")


def main():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    dhan = DhanData(cfg["client_id"], cfg["access_token"])
    ok, reason = dhan.verify_connection()
    if not ok:
        print(f"ABORTING -- Dhan connection failed: {reason}")
        return

    df_1d, df_1w = dhan.get_daily_and_weekly("BHAGCHEM")
    trace("BHAGCHEM", "1D", df_1d)
    trace("BHAGCHEM", "1W", df_1w)


if __name__ == "__main__":
    main()