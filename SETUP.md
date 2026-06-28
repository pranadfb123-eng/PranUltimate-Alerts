# PranUltimate Scanner — Setup Guide

## Folder
Place this entire folder at: `C:\Users\prana\PranUltimate\`

---

## Step 1 — Install Python dependencies
Open terminal and run:
```
py -3.13 -m pip install tvdatafeed pandas numpy
```

---

## Step 2 — Get the full NSE symbol list
1. Go to: https://www.nseindia.com/market-data/securities-available-for-trading
2. Download the CSV of all NSE listed equities
3. Extract just the symbol column (one symbol per line)
4. Save as: `C:\Users\prana\PranUltimate\scanner\nse_symbols.txt`

Without this file the scanner falls back to a Nifty 500 representative list.

---

## Step 3 — Install Node.js dependencies
In terminal, navigate to the project folder and run:
```
cd C:\Users\prana\PranUltimate
npm install
```

---

## Step 4 — Start the web server
```
cd C:\Users\prana\PranUltimate
npm start
```
Server runs at: http://localhost:3001
Share with phone/dad on same WiFi: http://<your-local-IP>:3001

To find your local IP: open Command Prompt → type `ipconfig` → look for IPv4 Address

---

## Step 5 — Schedule the scanner (Task Scheduler)
1. Open Windows Task Scheduler
2. Create Basic Task → Name: "PranUltimate Scan"
3. Trigger: Daily at 4:00 PM
4. Action: Start a Program
   - Program: `py`
   - Arguments: `-3.13 C:\Users\prana\PranUltimate\scanner\scan.py`
5. Finish

The scanner will run every day at 4 PM, write results.json, and the webpage will show fresh results when you open it at night.

---

## Usage
- Open browser → http://localhost:3001
- Click a timeframe button (5min, 15min, 1H, 1D etc.)
- See all stocks with fresh breakouts or near-breakouts
- Green = Fresh Breakout, Yellow = 1 Candle Post, Orange = 2 Candles Post

---

## Sharing on same WiFi
- Find your Windows machine's local IP (ipconfig → IPv4)
- Share: http://192.168.x.x:3001 with dad / phone
- Keep the Node server running (just leave the terminal open)
