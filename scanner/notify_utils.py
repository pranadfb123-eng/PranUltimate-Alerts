"""
notify_utils.py
================
Telegram sender for PranUltimate's alert system. Reads credentials from
environment variables (populated from GitHub Actions secrets when run in
CI, or from a local .env / exported vars when run on your own machine).

Required env vars:
  TELEGRAM_BOT_TOKEN   — from @BotFather
  TELEGRAM_CHAT_ID     — your numeric chat id (DM the bot once, then check
                          https://api.telegram.org/bot<TOKEN>/getUpdates)

send_telegram() is best-effort: failures are logged, never raised.
"""

import os
import logging

import requests

log = logging.getLogger(__name__)


def send_telegram(message: str) -> bool:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("Telegram: missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — skipped.")
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning(f"Telegram send failed: {resp.status_code} {resp.text}")
            return False
        return True
    except Exception as e:
        log.warning(f"Telegram send error: {e}")
        return False


def send_email(*args, **kwargs):
    """Disabled — Telegram-only notification setup."""
    return False


TIMEFRAME_LABELS = {
    "1H": "1 Hour", "2H": "2 Hour", "3H": "3 Hour", "4H": "4 Hour",
    "1D": "Daily", "1W": "Weekly",
}
SOURCE_LABELS = {
    "regular": "Regular Scan", "sp_stocks": "SP Stocks", "choppy_stocks": "Choppy Stocks",
}


def _category_label(timeframe, source):
    tf_label  = TIMEFRAME_LABELS.get(timeframe, timeframe)
    src_label = SOURCE_LABELS.get(source, source)
    return f"{tf_label} — {src_label}"


def notify_breakout(symbol, timeframe, close, resistance, source="regular"):
    category = _category_label(timeframe, source)
    msg = (
        f"\U0001F680 <b>BREAKOUT — {symbol}</b>\n"
        f"Category: {category}\n"
        f"Close: Rs{close}\n"
        f"Resistance crossed: Rs{resistance}"
    )
    send_telegram(msg)


def notify_error(context: str, detail: str):
    """Best-effort heads-up when the checker itself hits a problem (e.g. a
    stale Dhan token) — so a silent failure doesn't go unnoticed for days."""
    msg = f"\u26A0\uFE0F <b>PranUltimate Alert Monitor — error</b>\n{context}\n{detail}"
    send_telegram(msg)