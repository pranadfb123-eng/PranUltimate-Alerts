"""
Standalone diagnostic -- does NOT modify scan.py.
Runs scan_sp_stock_with_memory for BGRENERGY, BHAGCHEM, JBMA against the
REAL scanner_state.json on disk (creates it fresh if it doesn't exist yet),
so you can see exactly what the memory feature assigns each one to.

Usage:
    py -3.13 C:\\Users\\prana\\PranUltimate\\scanner\\check_memory.py
"""
import sys, json

SCANNER_DIR = r"C:\Users\prana\PranUltimate\scanner"
sys.path.insert(0, SCANNER_DIR)

from scan import (
    DhanData, CONFIG_PATH, load_scanner_state, save_scanner_state,
    scan_sp_stock_with_memory,
)


def check(symbols):
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    dhan = DhanData(cfg["client_id"], cfg["access_token"])
    ok, reason = dhan.verify_connection()
    if not ok:
        print(f"ABORTING -- Dhan connection failed: {reason}")
        return

    state = load_scanner_state()
    print(f"Loaded scanner_state.json -- {len(state['sp'])} SP entries already persisted\n")

    for symbol in symbols:
        had_entry = symbol in state["sp"]
        result = scan_sp_stock_with_memory(dhan, symbol, state)
        print(f"=== {symbol} ===")
        print(f"  Had persisted entry before this run: {had_entry}")
        print(f"  -> timeframe={result.get('timeframe')} | status={result.get('status')} | "
              f"origin_kind={result.get('origin_kind','-')}")
        print()

    save_scanner_state(state)
    print("scanner_state.json updated and saved.")


if __name__ == "__main__":
    check(["BGRENERGY", "BHAGCHEM", "JBMA"])