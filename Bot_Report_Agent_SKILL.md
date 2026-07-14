# Bot_Report_Agent SKILL

## What This Agent Does

Bot_Report_Agent reads the PranUltimate intraday bot log for a given date and generates a structured daily report. The report covers every trade taken, every stock skipped or rejected, watchlist stats, session P&L summary, and architecture notes. All data is extracted purely from the log file — no other files needed.

---

## Log File Location

**Task Scheduler run (normal):**
```
C:\Users\prana\PranUltimate\logs\bot_YYYYMMDD.log
```

**Manual run:**
```
C:\Users\prana\PranUltimate\intraday\bot_YYYY-MM-DD.log
```

If the user does not specify a date, use today's date. If the log file does not exist, say so and stop.

---

## Step-by-Step Parsing Instructions

Read the entire log file, then extract each section using the patterns below.

### STARTUP INFO (from the first ~10 lines)

- Trading mode: look for `PAPER TRADING` or `LIVE TRADING` on the header line
- Capital/trade, max positions, leverage: line matching `Capital/trade:`
- Square off time: line matching `Square off:`
- Loaded symbols: line matching `Loaded watchlist state:` — extract total symbols, confirmed boxes count, and per-TF breakdown (5min=N, 15min=N, 30min=N, 45min=N)

Example line:
```
Loaded watchlist state: 1175 symbols (295 confirmed boxes) | 15min=308, 30min=174, 45min=39, 5min=654
```

---

### SECTION 1 — TRADE TABLE

Each completed trade has exactly two key log lines:

**Entry (BUY placed):**
```
[PAPER] BUY Nxx<SYMBOL> | type=MARKET ...
```
or for live:
```
BUY Nxx<SYMBOL> | type=MARKET ...
```

The entry line is always paired with a setup confirmation line just before/after it that shows the resistance level. Look for:
```
<SYMBOL> [<TF>]: box CONFIRMED -- floor Rs<floor> / ceil Rs<ceil>
```
or the breakout trigger line:
```
<SYMBOL> [<TF>]: watching floor Rs<floor> / ceil Rs<ceil>
```

**Exit (trade closed):**
```
✗ CLOSED  <SYMBOL> [<TF>] @ <HH:MM:SS> | exit=₹<price> | resistance=₹<resistance> | P&L=<+/-₹amount> (<pct>%) | reason=<reason>
```

For each closed trade, assemble a row with:

| Stock | Timeframe | Entry Price | Resistance | Entry Time | Exit Time | Exit Price | Reason | P&L |

- **Stock**: symbol name
- **Timeframe**: TF in brackets e.g. `5min`, `15min`, `30min`, `45min`, `1H`
- **Entry Price**: find the BUY order timestamp and look nearby in the log for the actual price. If paper trade shows ₹0.0, use the `ceil` (ceiling) value from the confirmed-box line as the approximate entry — note it as "~₹X (box ceil)"
- **Resistance**: from the CLOSED line `resistance=₹X`
- **Entry Time**: timestamp of the `[PAPER] BUY` line
- **Exit Time**: timestamp from the `✗ CLOSED` line
- **Exit Price**: from `exit=₹X`
- **Reason**: from `reason=<reason>` — common values: `2-CANDLE-EXIT`, `SL-HIT`, `TARGET-HIT`, `SQUAREOFF`, `8EMA-EXIT`
- **P&L**: the full P&L string e.g. `+₹1172.6 (+1.17%)` or `₹-415.2 (-0.42%)`

Present as a markdown table sorted by entry time.

---

### SECTION 2 — REJECTED / SKIPPED STOCKS

Extract all lines containing `skipped` or `SKIP`. Group by rejection reason:

**A. Series-BE skips (can't trade intraday)**
Pattern: `SKIP <SYMBOL> [<TF>] -- series=BE`
Also: `SKIP <SYMBOL> [<TF>] -- series=BE (not EQ, can't trade intraday)`

**B. Price too low (< ₹50)**
Pattern 1 (Chartink scan): `✗ <SYMBOL>: skipped (price ₹X.XX < ₹50)`
Pattern 2 (direct scan): `<SYMBOL> [<TF>]: skipped at scan -- price RsX.XX < Rs50`

**C. Gap-check floor breached → escalated to higher TF**
Pattern: `<SYMBOL>: gap-check — floor breached offline (low ₹X.XX < floor ₹X.XX) — escalating to [<TF>]`
These are not full skips — they were escalated, not dropped. List them separately as "Gap-check escalations".

**D. Box forming too briefly (not enough candles yet)**
Pattern: `<SYMBOL> [<TF>]: forming box -- too brief yet`
These are in-progress setups that were not ready to trade. List count by TF.

**E. Higher TF consolidation skips (200 EMA filter)**
Pattern: `<SYMBOL> [<TF>]: no 200 EMA touch — not in a correction phase`
These stocks appeared on Chartink but failed the 200 EMA pullback filter.

**F. Liquidity filter skips**
Pattern: `✗ <SYMBOL>: skipped (est. turnover ₹X.Xcr < ₹5cr)`
Also: `✗ <SYMBOL>: skipped (est. turnover ₹0.0cr < ₹5cr)` (no data case)

For each category, list: total count + a sample of up to 10 stock names.

---

### SECTION 3 — WATCHLIST OVERVIEW

Extract these from the log:

1. **Total symbols at open**: from `Loaded watchlist state: N symbols`
2. **Confirmed boxes at open**: from `Loaded watchlist state: ... (N confirmed boxes)`
3. **Per-TF symbol counts at open**: 5min=N, 15min=N, 30min=N, 45min=N from same line
4. **New boxes discovered intraday**: count lines matching `box CONFIRMED -- floor`
5. **Symbols added to watchlist intraday**: count lines matching `+ <SYMBOL>: added to [<TF>] watchlist`
6. **Gap-check escalations**: count lines matching `escalating to`
7. **Stale breakouts archived**: count lines matching `stale breakout` and `removed -- terminal outcome`
8. **End-of-session watchlist size**: from `Watchlist state saved: N symbols carried to next session`
9. **Candle-close scan cycles**: count lines matching `candle closed | Chartink:`; break down by TF prefix (5min, 15min, 30min, 45min, 1H)

---

### SECTION 4 — SESSION SUMMARY

These are logged at the very end of the file. Look for the block:

```
SESSION COMPLETE
Total trades : N
Winners      : N
Losers       : N
Total P&L    : ₹X
```

Additionally compute from the CLOSED lines:
- **Win rate**: Winners / Total trades × 100 (round to 1 decimal)
- **Gross wins**: sum of all positive P&L amounts
- **Gross losses**: sum of all negative P&L amounts
- **Biggest winner**: CLOSED line with highest positive P&L
- **Biggest loser**: CLOSED line with most negative P&L
- **Most common exit reason**: count all `reason=` values from CLOSED lines and pick the most frequent

---

### SECTION 5 — ARCHITECTURE NOTES

**Parallel scan:**
The bot uses `ThreadPoolExecutor` (from `concurrent.futures`) with a shared `_trade_lock` for thread-safe order placement. Confirm this is active — there will be no explicit log message for it, but if you see many symbols scanned in quick succession (< 1 second apart in timestamps) within a single candle-close cycle, it confirms parallel scan is running.

**Errors and warnings:**
Extract all lines matching `ERROR` or `WARNING`. Group by error message text. Show count of each unique error type. Example:
```
ERROR    Scan error MBEL/5min: '>' not supported between instances of 'float' and 'NoneType'
```

**No-data symbols:**
Pattern: `no data -- keeping in watchlist` or `no data for gap-check -- skipping`
Count and list affected symbols.

---

## Output Format Template

Produce the report in this exact structure:

```
=============================================================
PRANULTIMATE BOT DAILY REPORT — <DATE>
Mode: <PAPER TRADING / LIVE TRADING>
Capital/trade: ₹X | Leverage: Xx | Max positions: N | Square off: HH:MM IST
=============================================================

SECTION 1 — TRADES TAKEN
─────────────────────────────────────────────────────────────────────────────────────────────────
| Stock      | TF    | Entry Price      | Resistance | Entry Time | Exit Time | Exit Price | Reason         | P&L              |
|------------|-------|------------------|------------|------------|-----------|------------|----------------|------------------|
| SYMBOL     | 5min  | ~₹X (box ceil)   | ₹X         | HH:MM:SS   | HH:MM:SS  | ₹X         | 2-CANDLE-EXIT  | +₹X (+X.XX%)     |
...

Total: N trades

─────────────────────────────────────────────────────────────────────────────────────────────────
SECTION 2 — REJECTED / SKIPPED STOCKS
─────────────────────────────────────────────────────────────────────────────────────────────────

A. Series-BE (can't trade intraday): N stocks
   GLOTTIS, CPCAP, RSWM, UNIDT, ...

B. Price < ₹50: N stocks
   IMAGICAA (₹48.00), MOL (₹47.59), HILINFRA (₹46.13), OLAELEC (₹43.53), ...
   [+ N more]

C. Gap-check escalations (floor breached at open, moved to higher TF): N stocks
   AMRUTANJAN (5min → 15min), BETA (5min → 15min), GSFC (5min → 1H), ...
   [+ N more]

D. Box forming too briefly (not ready): ~N instances across session
   (These are active watchlist symbols still building their setup)

E. 200 EMA filter rejects (no correction phase): N instances
   GODREJAGRO, AARTIIND, TMB, SUMICHEM, IFGLEXPOR, ...
   [+ N more]

F. Liquidity filter skips (est. turnover < ₹5cr): N stocks
   PILANIINVS (₹1.0cr), STYLAMIND (₹4.1cr), MPSLTD (₹4.0cr), ...
   [+ N more]

─────────────────────────────────────────────────────────────────────────────────────────────────
SECTION 3 — WATCHLIST OVERVIEW
─────────────────────────────────────────────────────────────────────────────────────────────────

Symbols loaded at open         : N (N confirmed boxes)
  5min                         : N
  15min                        : N
  30min                        : N
  45min                        : N

New boxes confirmed intraday   : N
New symbols added intraday     : N
Gap-check escalations at open  : N
Stale breakouts removed        : N
End-of-session watchlist size  : N symbols

Candle-close scan cycles       :
  5min : N cycles
  15min: N cycles
  30min: N cycles
  45min: N cycles
  1H   : N cycles

─────────────────────────────────────────────────────────────────────────────────────────────────
SECTION 4 — SESSION SUMMARY
─────────────────────────────────────────────────────────────────────────────────────────────────

Total trades    : N
Winners         : N
Losers          : N
Win rate        : X.X%

Total P&L       : ₹X
Gross wins      : +₹X
Gross losses    : ₹-X

Biggest winner  : SYMBOL [TF] — +₹X (+X.XX%) — reason
Biggest loser   : SYMBOL [TF] — ₹-X (-X.XX%) — reason

Most common exit reason: <REASON> (N times)

─────────────────────────────────────────────────────────────────────────────────────────────────
SECTION 5 — ARCHITECTURE NOTES
─────────────────────────────────────────────────────────────────────────────────────────────────

Parallel scan    : ThreadPoolExecutor active (concurrent.futures) | _trade_lock for thread safety
Scan workers     : MAX_SCAN_WORKERS (see bot.py line ~1503)

Errors/Warnings  :
  ERROR - Scan error X/5min: '>' not supported between instances of 'float' and 'NoneType'
    — Recurring on: MBEL, LIQUIDBEES, GANDHITUBE, GILT5YBEES (N occurrences each)
    — Root cause: NoneType comparison in scan logic; stock skipped gracefully, no trade impact

  [List any other unique error messages with count]

No-data symbols  : N (kept in watchlist, no action taken)
  [List up to 10: SYMBOL1, SYMBOL2, ...]

=============================================================
END OF REPORT
=============================================================
```

---

## Usage Instructions for Claude

1. Ask the user for the date (YYYYMMDD format) or default to today.
2. Determine which log path to use (logs\bot_YYYYMMDD.log for Task Scheduler, intraday\bot_YYYY-MM-DD.log for manual).
3. Read the full log file.
4. Parse each section top-to-bottom using the patterns above.
5. Fill in the output template. Do not skip sections — if a section has zero entries, say "None" or "0".
6. For P&L numbers: preserve the exact rupee amounts and percentages from the log. Do not round or reformat.
7. For paper trades: entry price shows ₹0.0 in the order line (paper mode does not submit real orders). Use the box ceiling (ceil) from the nearest confirmed-box line for that symbol as the proxy entry price, and note it as approximate.
8. Present the completed report as a code block or clean markdown table.

---

## Quick Reference — All Log Patterns

| What | Pattern |
|------|---------|
| Bot startup | `PranUltimate Intraday Bot —` |
| Watchlist loaded | `Loaded watchlist state: N symbols` |
| Gap-check start | `Startup gap-check: verifying` |
| Gap-check OK | `gap-check OK (low ₹X >= floor ₹X)` |
| Gap-check fail/escalate | `gap-check — floor breached offline ... escalating to` |
| Box confirmed | `box CONFIRMED -- floor Rs / ceil Rs` |
| Box too brief | `forming box -- too brief yet` |
| No 200 EMA touch | `no 200 EMA touch — not in a correction phase` |
| Stale breakout | `stale breakout` |
| Symbol removed | `removed -- terminal outcome` |
| Series-BE skip | `SKIP <SYM> [TF] -- series=BE` |
| Price < ₹50 skip (Chartink) | `skipped (price ₹X < ₹50)` |
| Price < ₹50 skip (scan) | `skipped at scan -- price RsX < Rs50` |
| Liquidity skip | `skipped (est. turnover ₹Xcr < ₹5cr)` |
| Added to watchlist | `+ <SYM>: added to [TF] watchlist` |
| Paper BUY placed | `[PAPER] BUY N×<SYM>` |
| Paper SELL placed | `[PAPER] SELL N×<SYM>` |
| Trade closed | `✗ CLOSED  <SYM> [TF] @ HH:MM:SS \| exit=₹X \| resistance=₹X \| P&L=X \| reason=X` |
| 2-candle exit note | `⏱ 2-candle exit: <SYM> [TF] @ ₹X (held Nmin)` |
| EMA exit signal | `EXIT: <SYM> [TF] \| close ₹X < 8EMA ₹X` |
| Candle-close cycle | `── <TF> candle closed \| Chartink: N candidates, N new \| tracking N on this TF ──` |
| Watchlist saved | `Watchlist state saved: N symbols carried to next session` |
| Session end | `Market session ended.` |
| Session summary block | `SESSION COMPLETE` |
| Scan ERROR | `ERROR    Scan error <SYM>/<TF>:` |
| No data | `no data -- keeping in watchlist` |
