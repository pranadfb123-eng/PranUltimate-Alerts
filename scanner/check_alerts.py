"""
check_alerts.py
================
Run ONLY at 1H/2H candle-close boundaries (the GitHub Actions workflow
schedules this — see .github/workflows/alert_check.yml). Checks every
`active` alert in alerts_state.json against the latest CLOSED candle:

  - close > resistance  -> fire alert (Telegram + email), mark `triggered`
  - close < first_low   -> First Low broken, mark `disabled` (silent —
                            no notification, per design: a disabled alert
                            just stops being checked)

Requires dhan_data.py (your existing DhanData class) to be present in the
same scanner/ folder in this repo — copy it over from ../intraday/dhan_data.py
so the GitHub Actions runner (which only checks out THIS repo, not your
whole local folder structure) can import it.

Env vars required (set as GitHub Actions secrets):
  DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN   — same as intraday_config.json
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  GMAIL_ADDRESS, GMAIL_APP_PASSWORD, ALERT_EMAIL_TO
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "..", "server", "alerts_state.json")

sys.path.insert(0, BASE_DIR)  # so `dhan_data.py` (vendored into scanner/) resolves
from dhan_data import DhanData          # noqa: E402
from notify_utils import notify_breakout, notify_error  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)


def _load_state():
    if not os.path.exists(STATE_PATH):
        log.warning("alerts_state.json not found — nothing to check yet.")
        return {}
    with open(STATE_PATH) as f:
        return json.load(f)


def _save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)  # atomic on Windows — no partial-write corruption


def _latest_closed_candle(dhan, symbol, timeframe):
    """Return (close, prev_close) for the two most recent CLOSED candles on
    the given timeframe. Returns (None, None) if data isn't available or there
    are fewer than 2 candles. Reuses the same frames the scanner already
    fetches — get_remaining_timeframes covers 1H/2H."""
    frames = dhan.get_remaining_timeframes(symbol)
    df = frames.get(timeframe)
    if df is None or len(df) < 2:
        return None, None   # returns (close, prev_close)
    return float(df.iloc[-1]["close"]), float(df.iloc[-2]["close"])


_IST = timezone(timedelta(hours=5, minutes=30))
# Market hours in IST. Check runs are only meaningful between open and close.
# Any Task Scheduler trigger that fires outside this window is a misconfiguration
# — but even if it does, we exit silently rather than spamming error alerts.
_MARKET_OPEN_H,  _MARKET_OPEN_M  = 9,  0
_MARKET_CLOSE_H, _MARKET_CLOSE_M = 16, 0


def _in_market_hours() -> bool:
    now = datetime.now(_IST)
    if now.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    open_t  = now.replace(hour=_MARKET_OPEN_H,  minute=_MARKET_OPEN_M,  second=0, microsecond=0)
    close_t = now.replace(hour=_MARKET_CLOSE_H, minute=_MARKET_CLOSE_M, second=0, microsecond=0)
    return open_t <= now <= close_t


def check_alerts():
    # ── Market hours guard ──────────────────────────────────────────────────────
    # Task Scheduler sometimes has stray triggers outside market hours (e.g. a
    # run at 5:30 PM or 9:46 PM IST). Dhan's API can return transient 401s
    # after hours even with a valid token, which previously caused false
    # "token expired" Telegram alerts. Exit silently — nothing to check anyway.
    now_ist = datetime.now(_IST)
    if not _in_market_hours():
        log.info(f"Outside market hours ({now_ist.strftime('%H:%M IST, %A')}) — skipping run.")
        return

    state = _load_state()
    active = {k: v for k, v in state.items() if v["status"] == "active"}
    if not active:
        log.info("No active alerts to check.")
        return

    client_id    = os.environ.get("DHAN_CLIENT_ID")
    access_token = os.environ.get("DHAN_ACCESS_TOKEN")
    if not client_id or not access_token:
        log.error("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set — aborting.")
        notify_error("check_alerts.py aborted", "Missing Dhan credentials in environment.")
        return

    dhan = DhanData(client_id, access_token)
    ok, reason = dhan.verify_connection()   # retries 3× internally before failing
    if not ok:
        log.error(f"Dhan connection failed after retries: {reason}")
        notify_error("Dhan connection failed", f"check_alerts.py could not connect after retries: {reason}")
        return

    triggered_count = 0
    disabled_count  = 0
    error_count     = 0

    for key, alert in active.items():
        symbol     = alert["symbol"]
        tf         = alert["timeframe"]
        resistance = alert["resistance"]
        first_low  = alert["first_low"]

        try:
            close, prev_close = _latest_closed_candle(dhan, symbol, tf)
        except Exception as e:
            log.warning(f"{symbol} [{tf}]: fetch error — {e}")
            error_count += 1
            continue

        if close is None:
            log.warning(f"{symbol} [{tf}]: no candle data — skipped this run.")
            error_count += 1
            continue

        if close > resistance:
            # Only alert if this is the FIRST candle above resistance.
            # If prev_close was also above resistance, this is a stale breakout
            # — the stock already ran, no point alerting now.
            if prev_close is not None and prev_close > resistance:
                log.info(f"  \u23ed STALE BREAKOUT: {symbol} [{tf}] close=Rs{close} "
                         f"but prev_close=Rs{prev_close} also > resistance=Rs{resistance} "
                         f"— already ran, marking triggered silently")
                state[key]["status"] = "triggered"
                state[key]["triggered_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                state[key]["trigger_close"] = close
                state[key]["stale"] = True
                triggered_count += 1
            else:
                # Fresh breakout — first candle above resistance, alert now
                log.info(f"  \u2605 TRIGGER: {symbol} [{tf}] close=Rs{close} > resistance=Rs{resistance} (FRESH)")
                notify_breakout(symbol, tf, close, resistance, source=alert.get("source", "regular"))
                state[key]["status"]       = "triggered"
                state[key]["triggered_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                state[key]["trigger_close"] = close
                triggered_count += 1
        elif close < first_low:
            log.info(f"  \u2298 DISABLED: {symbol} [{tf}] close=Rs{close} < first_low=Rs{first_low}")
            state[key]["status"] = "disabled"
            state[key]["disabled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            disabled_count += 1
        else:
            log.info(f"  -- holding: {symbol} [{tf}] close=Rs{close} "
                      f"(first_low=Rs{first_low} < close < resistance=Rs{resistance})")

    _save_state(state)
    log.info(f"Check complete. {triggered_count} triggered, {disabled_count} disabled, "
              f"{error_count} errors, {le