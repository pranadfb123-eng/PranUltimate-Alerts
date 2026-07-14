import json, os
from dhan_data import DhanData

cfg = json.load(open(os.path.join(os.path.dirname(__file__), "..", "intraday_config.json")))
d = DhanData(cfg["client_id"], cfg["access_token"])

print("\nTesting RELIANCE 5min...")
df = d.get_hist("RELIANCE", "5min")
if df is not None:
    print(f"SUCCESS - {len(df)} candles")
    print(df.tail(3))
else:
    print("FAILED - no data")