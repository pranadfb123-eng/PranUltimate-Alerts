import json, os
from tvDatafeed import TvDatafeed, Interval

cfg = json.load(open(os.path.join(os.path.dirname(__file__), "..", "intraday_config.json")))
print("Attempting login as:", cfg.get("tv_username"))

tv = TvDatafeed(username=cfg["tv_username"], password=cfg["tv_password"])
df = tv.get_hist(symbol="RELIANCE", exchange="NSE", interval=Interval.in_5_minute, n_bars=10)

if df is not None and len(df) > 0:
    print("\n✓ SUCCESS — data fetched:")
    print(df.tail(3))
else:
    print("\n✗ FAILED — no data returned")