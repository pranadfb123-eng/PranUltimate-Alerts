r"""
email_report.py
===============
Reads today's PranUltimate bot log, generates the daily trading report,
and emails it to the configured recipient via Gmail SMTP.

Credentials are read from:
    C:\Users\prana\PranUltimate\report_secrets.env

Run manually:
    python email_report.py
    python email_report.py 2026-07-06     # specific date

Scheduled at 3:45 PM IST via Windows Task Scheduler.
"""

import os
import re
import sys
import smtplib
import logging
from collections import defaultdict
from datetime import date, datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS_FILE = os.path.join(BASE_DIR, "report_secrets.env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger(__name__)


def load_secrets():
    secrets = {}
    if not os.path.exists(SECRETS_FILE):
        log.error(f"report_secrets.env not found at {SECRETS_FILE}")
        return None
    with open(SECRETS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                secrets[k.strip()] = v.strip()
    return secrets


def find_log(target_date: date):
    """Return the bot log with actual trade data.

    intraday/bot_YYYY-MM-DD.log  — main trading log (written by bot.py file handler)
    logs/bot_YYYYMMDD.log        — startup-scan log (stops at ~09:40, no closed trades)

    Always prefer the intraday/ log; fall back to logs/ only if it's missing.
    """
    fmt1 = os.path.join(BASE_DIR, "intraday", f"bot_{target_date.strftime('%Y-%m-%d')}.log")
    fmt2 = os.path.join(BASE_DIR, "logs",     f"bot_{target_date.strftime('%Y%m%d')}.log")
    for p in (fmt1, fmt2):
        if os.path.exists(p):
            return p
    return None


def _extract_time(line: str) -> str:
    """Extract HH:MM:SS from a log line like '2026-07-06 09:50:46,032  INFO ...'"""
    m = re.search(r"\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})", line)
    return m.group(1) if m else ""


# ── Log parsers ───────────────────────────────────────────────────────────────

def parse_header(lines: list) -> dict:
    info = {
        "mode":         "UNKNOWN",
        "capital":      "?",
        "max_pos":      "?",
        "leverage":     "?",
        "sq_off":       "?",
        "symbols":      "?",
        "boxes":        "?",
        "tf_breakdown": "",
    }
    for line in lines[:15]:
        if "PAPER TRADING" in line:
            info["mode"] = "PAPER"
        elif "LIVE TRADING" in line:
            info["mode"] = "LIVE"
        m = re.search(r"Capital/trade:\s*[₹Rs]*([\d,]+)", line)
        if m:
            info["capital"] = m.group(1)
        m = re.search(r"Max positions:\s*(\d+)", line)
        if m:
            info["max_pos"] = m.group(1)
        m = re.search(r"Leverage:\s*([\dx]+)", line)
        if m:
            info["leverage"] = m.group(1)
        m = re.search(r"Square off:\s*(\d+:\d+)", line)
        if m:
            info["sq_off"] = m.group(1)
    for line in lines[:20]:
        m = re.search(r"Loaded watchlist state:\s*(\d+) symbols \((\d+) confirmed", line)
        if m:
            info["symbols"] = m.group(1)
            info["boxes"]   = m.group(2)
        m = re.search(r"\|\s*(5min=\d+.*?)$", line)
        if m:
            info["tf_breakdown"] = m.group(1).strip()
    return info


def parse_trades(lines: list) -> list:
    """
    Match closed trades and enrich with SIGNAL-line RSI/vol/qty.
    """
    trades = []

    signal_map: dict = {}

    # Two-stage signal detection:
    # Stage 1 — any SIGNAL line → set _last_signal_sym (needed to capture close= line)
    signal_sym_re = re.compile(r"SIGNAL:\s+([A-Z0-9&\-]+) \[([^\]]+)\]")
    # Stage 2 — "locked ceil" format has extra detail inline
    signal_ceil_re = re.compile(
        r"SIGNAL:\s+[A-Z0-9&\-]+ \[[^\]]+\].*?ceil Rs([\d.]+).*?RSI ([\d.]+).*?vol ([\d.]+)x"
    )
    # "breakout confirmed" format still has RSI/vol on the SIGNAL line
    signal_rsi_vol_re = re.compile(r"RSI ([\d.]+).*?vol ([\d.]+)x")

    buy_re = re.compile(
        r"(\d{2}:\d{2}:\d{2}).*?\[PAPER\] BUY\s+(\d+)[×xX]([A-Z0-9&\-]+)"
    )
    buy_live_re = re.compile(
        r"(\d{2}:\d{2}:\d{2}).*?(?<!\[PAPER\] )BUY\s+(\d+)[×xX]([A-Z0-9&\-]+)"
    )
    close_re = re.compile(
        r"(\d{2}:\d{2}:\d{2}).*?✗ CLOSED\s+([A-Z0-9&\-]+) \[([^\]]+)\] @ (\d{2}:\d{2}:\d{2})"
        r".*?exit=₹([\d.]+).*?resistance=₹([\d.]+).*?P&L=([^|]+).*?reason=([^\s|]+)"
    )

    first_low_re   = re.compile(r"first_low=Rs([\d.]+)")
    fill_price_re  = re.compile(r"close=Rs([\d.]+)")   # breakout candle close = actual fill

    _last_signal_sym: str = ""

    for line in lines:
        ts = _extract_time(line)

        # Stage 1: detect any SIGNAL line and set _last_signal_sym
        ssm = signal_sym_re.search(line)
        if ssm:
            sym = ssm.group(1)
            _last_signal_sym = sym
            signal_map.setdefault(sym, {}).update({"tf": ssm.group(2)})
            # Stage 2a: "locked ceil" format — ceil + RSI + vol all on signal line
            sdm = signal_ceil_re.search(line)
            if sdm:
                signal_map[sym].update({
                    "ceil": sdm.group(1),
                    "rsi":  sdm.group(2),
                    "vol":  sdm.group(3),
                })
            else:
                # Stage 2b: "breakout confirmed" format — RSI/vol still on signal line
                rvm = signal_rsi_vol_re.search(line)
                if rvm:
                    signal_map[sym].update({
                        "rsi": rvm.group(1),
                        "vol": rvm.group(2),
                    })

        # close=Rs... line immediately follows SIGNAL — this is the actual fill price.
        # Also grab resistance= from same line as ceil fallback for "confirmed box" signals.
        fp_m = fill_price_re.search(line)
        if fp_m and _last_signal_sym:
            signal_map.setdefault(_last_signal_sym, {})["fill_price"] = fp_m.group(1)
            if "ceil" not in signal_map[_last_signal_sym]:
                rm = re.search(r"resistance=Rs([\d.]+)", line)
                if rm:
                    signal_map[_last_signal_sym]["ceil"] = rm.group(1)

        fl_m = first_low_re.search(line)
        if fl_m and _last_signal_sym:
            signal_map.setdefault(_last_signal_sym, {})["floor"] = fl_m.group(1)
            _last_signal_sym = ""

        bm = buy_re.search(line) or buy_live_re.search(line)
        if bm:
            entry_ts = bm.group(1)
            qty      = bm.group(2)
            sym      = bm.group(3)
            signal_map.setdefault(sym, {}).update({
                "entry_time": entry_ts,
                "qty":        qty,
            })

        em = close_re.search(line)
        if em:
            close_ts, sym, tf, exit_time, exit_price, resistance, pnl_raw, reason = (
                em.group(1), em.group(2), em.group(3), em.group(4),
                em.group(5), em.group(6), em.group(7).strip(), em.group(8)
            )
            info = signal_map.pop(sym, {})
            pnl_num   = _parse_pnl(pnl_raw)
            qty_raw   = info.get("qty", "?")
            ceil_raw  = info.get("ceil", "?")
            fill_raw  = info.get("fill_price", ceil_raw)   # breakout candle close (actual fill)
            try:
                invested = f"₹{int(float(qty_raw) * float(fill_raw)):,}"
            except (ValueError, TypeError):
                invested = "?"
            trades.append({
                "symbol":      sym,
                "tf":          info.get("tf", tf),
                "floor":       info.get("floor", "?"),
                "ceil":        ceil_raw,                    # box ceiling (resistance level)
                "entry_price": fill_raw,                    # actual fill = breakout candle close
                "resistance":  resistance,
                "entry_time":  info.get("entry_time", "?"),
                "exit_time":   exit_time,
                "exit_price":  exit_price,
                "reason":      reason,
                "rsi":         info.get("rsi", "?"),
                "vol":         info.get("vol", "?"),
                "qty":         qty_raw,
                "invested":    invested,
                "pnl":         pnl_raw,
                "pnl_num":     pnl_num,
            })

    return sorted(trades, key=lambda t: t["entry_time"] if t["entry_time"] != "?" else "99:99:99")


def _parse_pnl(pnl_str: str) -> float:
    """Extract numeric P&L. Handles: '₹-415.2 (-0.42%)', '+₹158.0 (+0.16%)'"""
    m = re.search(r"([+\-]?)₹([+\-]?[\d.]+)", pnl_str)
    if m:
        sign = m.group(1)
        val  = float(m.group(2))
        if sign == "-":
            return -abs(val)
        return val
    return 0.0


def parse_skips(lines: list) -> list:
    """Return list of (symbol, reason) for every SKIP/skipped line, deduplicated."""
    skips = []
    seen: set = set()
    for line in lines:
        if "SKIP" not in line and "skipped" not in line:
            continue

        sym_m = re.search(r"(?:SKIP|✗)\s+([A-Z0-9&\-]+)", line)
        if not sym_m:
            continue
        sym = sym_m.group(1)

        reason = ""
        dash_m = re.search(r"--\s*(.+)$", line)
        if dash_m:
            reason = dash_m.group(1).strip()
        else:
            paren_m = re.search(r"\((.+?)\)", line)
            if paren_m:
                reason = paren_m.group(1).strip()

        dedup_key = f"{sym}|{reason[:40]}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        skips.append((sym, reason))

    return skips


def _normalize_skip_reason(reason: str) -> str:
    """Collapse variable-value reasons (turnover, price) into a single canonical label."""
    r = reason.lower()
    # "est. turnover ₹3.1cr < ₹5cr", "est. turnover ₹0.0cr < ₹5cr", etc.
    if "turnover" in r and "cr" in r:
        return "Turnover < ₹5Cr (too illiquid)"
    # "price Rs0.45 < Rs50", "price ₹32 < ₹50", etc.
    if re.search(r"price\s*(rs|₹)", r):
        return "Price < ₹50"
    return reason.strip() if reason else "unknown"


def _group_skips(skips: list) -> list:
    """Group (sym, reason) pairs by normalized reason. Returns list sorted by count desc."""
    groups = defaultdict(list)
    for sym, reason in skips:
        key = _normalize_skip_reason(reason)
        groups[key].append(sym)
    return sorted(
        [{"reason": r, "count": len(s), "examples": s[:3]} for r, s in groups.items()],
        key=lambda x: -x["count"],
    )


def parse_session_summary(lines: list) -> dict:
    """Pull end-of-session stats from the log footer."""
    summary = {
        "total_pnl":  None,
        "trades_won": None,
        "trades_lost": None,
        "signals":    None,
        "gap_fails":  0,
    }
    for line in lines:
        m = re.search(r"Total P&L\s*:\s*[₹Rs]*([-+]?[\d,]+\.?\d*)", line)
        if m:
            try:
                summary["total_pnl"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
        m = re.search(r"Trades won\s*:\s*(\d+)", line)
        if m:
            summary["trades_won"] = int(m.group(1))
        m = re.search(r"Trades lost\s*:\s*(\d+)", line)
        if m:
            summary["trades_lost"] = int(m.group(1))
        m = re.search(r"Signals\s*:\s*(\d+)", line)
        if m:
            summary["signals"] = int(m.group(1))
        if "gap-check" in line and ("floor breached" in line or "escalating" in line):
            summary["gap_fails"] += 1
    return summary


# ── Narrative / Remarks ───────────────────────────────────────────────────────

def build_session_narrative(lines: list, trades: list, header: dict, session: dict) -> str:
    """Build a plain-English timeline of what the bot did today."""
    # Start / end times
    start_time = next((_extract_time(l) for l in lines if _extract_time(l)), "?")
    end_time   = next((_extract_time(l) for l in reversed(lines) if _extract_time(l)), "?")

    # New symbols added during session
    added = sum(1 for l in lines if "added to [" in l and "watchlist" in l)

    n      = len(trades)
    wins   = [t for t in trades if t["pnl_num"] > 0]
    losses = [t for t in trades if t["pnl_num"] < 0]
    total_pnl = session["total_pnl"] if session["total_pnl"] is not None else sum(t["pnl_num"] for t in trades)

    # Exit reason breakdown
    reason_counts: dict = {}
    for t in trades:
        reason_counts[t["reason"]] = reason_counts.get(t["reason"], 0) + 1
    reason_summary = ", ".join(
        f"{r} ×{c}" for r, c in sorted(reason_counts.items(), key=lambda x: -x[1])
    )

    # Loss cluster: largest group of losses whose entry_times span ≤ 30 min
    def to_mins(s):
        try:
            h, m, _ = s.split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return None

    loss_times = sorted(
        [(to_mins(t["entry_time"]), t) for t in losses if to_mins(t["entry_time"]) is not None],
        key=lambda x: x[0],
    )
    cluster_note = ""
    if loss_times:
        best_cluster = []
        for i, (t0, _) in enumerate(loss_times):
            c = [(tm, tr) for (tm, tr) in loss_times if 0 <= tm - t0 <= 30]
            if len(c) > len(best_cluster):
                best_cluster = c
        if len(best_cluster) >= 3:
            c_pnl = sum(tr["pnl_num"] for _, tr in best_cluster)
            h0, m0 = divmod(best_cluster[0][0], 60)
            h1, m1 = divmod(best_cluster[-1][0], 60)
            cluster_note = (
                f" A tight loss cluster of <b>{len(best_cluster)} trades</b> fired between "
                f"<b>{h0:02d}:{m0:02d}–{h1:02d}:{m1:02d}</b>, "
                f"accounting for ₹{c_pnl:,.2f} of the total loss."
            )

    parts = []
    parts.append(f"Session started at <b>{start_time}</b> in <b>{header['mode']}</b> mode "
                 f"(₹{header['capital']} per trade, {header['leverage']} leverage, "
                 f"square-off {header['sq_off']}).")
    if header["symbols"] != "?":
        parts.append(f"<b>{header['symbols']}</b> symbols loaded from saved watchlist "
                     f"(<b>{header['boxes']}</b> confirmed boxes"
                     + (f"; {header['tf_breakdown']}" if header["tf_breakdown"] else "") + ").")
    if added:
        parts.append(f"<b>{added}</b> new symbols were added to the watchlist during the session.")
    if trades:
        parts.append(
            f"<b>{n}</b> trades were taken — first entry at "
            f"<b>{trades[0]['entry_time']}</b> ({trades[0]['symbol']}), "
            f"last at <b>{trades[-1]['entry_time']}</b> ({trades[-1]['symbol']})."
        )
        parts.append(
            f"Outcome: <b>{len(wins)}W / {len(losses)}L</b>, "
            f"total P&L <b>₹{total_pnl:+,.2f}</b>."
        )
        if reason_summary:
            parts.append(f"Exit breakdown — {reason_summary}.")
        if cluster_note:
            parts.append(cluster_note)
    else:
        parts.append("No trades were taken today.")
    if session["gap_fails"]:
        parts.append(f"{session['gap_fails']} gap-check escalations occurred at the open.")
    parts.append(f"Session ended at <b>{end_time}</b>.")

    return " ".join(parts)


def build_remarks(trades: list, skips: list, session: dict) -> str:
    """Analytical remarks: what went right, what went wrong, what could improve."""
    if not trades:
        return "<p style='color:#9ca3af'>No trades to analyse today.</p>"

    n         = len(trades)
    wins      = [t for t in trades if t["pnl_num"] > 0]
    losses    = [t for t in trades if t["pnl_num"] < 0]
    total_pnl = session["total_pnl"] if session["total_pnl"] is not None else sum(t["pnl_num"] for t in trades)
    win_rate  = len(wins) / n * 100

    remarks = []

    # ── Win rate
    if win_rate < 25:
        remarks.append(
            f"⚠️ <b>Win rate is critically low ({win_rate:.0f}%, {len(wins)}/{n}).</b> "
            f"The strategy needs at least 40–50% win rate at this W:L ratio to be profitable. "
            f"Review whether today's entries met all box-model criteria or if market conditions "
            f"were simply not suitable."
        )
    elif win_rate < 45:
        remarks.append(
            f"📉 <b>Win rate ({win_rate:.0f}%) is below target.</b> "
            f"At this rate, average wins need to meaningfully outsize average losses "
            f"to stay net-positive."
        )
    else:
        remarks.append(
            f"✅ <b>Win rate ({win_rate:.0f}%) is acceptable.</b> Solid execution on the model."
        )

    # ── Exit reason analysis
    reason_counts: dict = {}
    reason_pnl: dict   = {}
    for t in trades:
        r = t["reason"]
        reason_counts[r] = reason_counts.get(r, 0) + 1
        reason_pnl[r]    = reason_pnl.get(r, 0.0) + t["pnl_num"]

    dominant = max(reason_counts, key=reason_counts.get)
    dom_c    = reason_counts[dominant]
    dom_pnl  = reason_pnl[dominant]
    if dom_c >= 3:
        if "8_EMA" in dominant:
            remarks.append(
                f"🔴 <b>{dominant} dominated ({dom_c}/{n} trades, ₹{dom_pnl:+,.2f}).</b> "
                f"This exit fires when the 8 EMA crosses below entry price shortly after entry — "
                f"meaning price reversed almost immediately. Possible causes: entering too late in "
                f"the breakout candle (chasing), or the breakout was a fakeout. "
                f"Consider tightening the entry window or adding a 'retest' confirmation."
            )
        elif "2-CANDLE" in dominant:
            remarks.append(
                f"🟡 <b>{dominant} dominated ({dom_c}/{n} trades, ₹{dom_pnl:+,.2f}).</b> "
                f"Two consecutive red candles after entry triggered the exit. "
                f"{'This actually preserved capital — exits were disciplined.' if dom_pnl > -5000 else 'These exits still resulted in meaningful losses — the entries themselves need review.'}"
            )
        else:
            remarks.append(
                f"ℹ️ <b>{dominant} was the most common exit ({dom_c}/{n} trades, ₹{dom_pnl:+,.2f}).</b>"
            )

    # ── Average win vs loss
    if wins and losses:
        avg_win  = sum(t["pnl_num"] for t in wins)  / len(wins)
        avg_loss = sum(t["pnl_num"] for t in losses) / len(losses)
        ratio    = abs(avg_win / avg_loss) if avg_loss else 0
        if ratio >= 1.5:
            remarks.append(
                f"✅ <b>Risk:reward looks good</b> — average win ₹{avg_win:,.2f} vs "
                f"average loss ₹{avg_loss:,.2f} ({ratio:.2f}x). "
                f"If win rate improves the overall P&L will follow."
            )
        elif ratio >= 0.8:
            remarks.append(
                f"⚠️ <b>Average win (₹{avg_win:,.2f}) is close to average loss (₹{avg_loss:,.2f}) "
                f"({ratio:.2f}x).</b> Need either higher win rate or wider profit targets."
            )
        else:
            remarks.append(
                f"🔴 <b>Average loss (₹{avg_loss:,.2f}) outpaces average win (₹{avg_win:,.2f}) "
                f"({ratio:.2f}x).</b> Losses are running too far — consider tighter stops "
                f"or exiting faster on adverse moves."
            )

    # ── Best and worst
    best  = max(trades, key=lambda t: t["pnl_num"])
    worst = min(trades, key=lambda t: t["pnl_num"])
    remarks.append(
        f"🏆 <b>Best trade:</b> {best['symbol']} ₹{best['pnl_num']:+,.2f} "
        f"(entered {best['entry_time']}, exited via {best['reason']}). "
        f"🩸 <b>Worst trade:</b> {worst['symbol']} ₹{worst['pnl_num']:+,.2f} "
        f"(entered {worst['entry_time']}, exited via {worst['reason']})."
    )

    # ── Loss cluster warning
    def to_mins(s):
        try:
            h, m, _ = s.split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return None

    loss_times = sorted(
        [(to_mins(t["entry_time"]), t) for t in losses if to_mins(t["entry_time"]) is not None],
        key=lambda x: x[0],
    )
    if loss_times:
        best_cluster = []
        for i, (t0, _) in enumerate(loss_times):
            c = [(tm, tr) for (tm, tr) in loss_times if 0 <= tm - t0 <= 30]
            if len(c) > len(best_cluster):
                best_cluster = c
        if len(best_cluster) >= 3:
            c_pnl  = sum(tr["pnl_num"] for _, tr in best_cluster)
            c_syms = ", ".join(tr["symbol"] for _, tr in best_cluster)
            h0, m0 = divmod(best_cluster[0][0], 60)
            h1, m1 = divmod(best_cluster[-1][0], 60)
            remarks.append(
                f"⚠️ <b>Loss cluster {h0:02d}:{m0:02d}–{h1:02d}:{m1:02d}: "
                f"{len(best_cluster)} losses in 30 min (₹{c_pnl:,.2f})</b> — {c_syms}. "
                f"Consider a circuit-breaker rule: after 3 consecutive losses, "
                f"pause new entries for 30 minutes."
            )

    # ── 200 EMA gate
    ema_blocks = sum(1 for _, r in skips if "200 EMA" in r or "below 200 EMA" in r)
    if ema_blocks:
        remarks.append(
            f"🛡️ <b>200 EMA ceiling gate blocked {ema_blocks} entries</b> where the "
            f"consolidation box was below the 200 EMA — these would likely have been losses. "
            f"The gate is working correctly."
        )

    # ── Clean wins
    if wins:
        top_wins = sorted(wins, key=lambda t: -t["pnl_num"])[:3]
        win_strs = ", ".join(f"{t['symbol']} (+₹{t['pnl_num']:,.0f})" for t in top_wins)
        remarks.append(f"✅ <b>Clean wins:</b> {win_strs}.")

    html = "<ul style='margin:6px 0;padding-left:20px;line-height:2'>"
    for r in remarks:
        html += f"<li>{r}</li>"
    html += "</ul>"
    return html


# ── HTML builder ──────────────────────────────────────────────────────────────

def _pnl_color(val: float) -> str:
    if val > 0:  return "#16a34a"
    if val < 0:  return "#dc2626"
    return "#6b7280"


def _pnl_bg(val: float) -> str:
    if val > 0:  return "#f0fdf4"
    if val < 0:  return "#fef2f2"
    return "transparent"


def build_html(report_date: date, header: dict, trades: list,
               skips: list, session: dict,
               narrative: str = "", remarks: str = "") -> str:

    total_pnl  = session["total_pnl"]
    if total_pnl is None:
        total_pnl = sum(t["pnl_num"] for t in trades)

    wins   = session["trades_won"]  if session["trades_won"]  is not None else sum(1 for t in trades if t["pnl_num"] > 0)
    losses = session["trades_lost"] if session["trades_lost"] is not None else sum(1 for t in trades if t["pnl_num"] < 0)
    n      = len(trades)
    win_rate = f"{round(wins / n * 100)}%" if n else "—"
    pnl_color_sum = _pnl_color(total_pnl)

    rows = ""
    for t in trades:
        pnl_val   = t["pnl_num"]
        pnl_col   = _pnl_color(pnl_val)
        pnl_bg    = _pnl_bg(pnl_val)
        fill_px   = f"₹{t['entry_price']}" if t["entry_price"] != "?" else "—"
        ceil_px   = f"₹{t['ceil']}" if t.get("ceil", "?") != "?" else "—"
        rsi_str   = t["rsi"] if t["rsi"] != "?" else "—"
        vol_str   = f"{t['vol']}x" if t["vol"] != "?" else "—"
        qty_str   = t["qty"] if t["qty"] != "?" else "—"
        floor_str = f"₹{t['floor']}" if t["floor"] != "?" else "—"
        invested  = t.get("invested", "?")
        rows += f"""
        <tr style="background:{pnl_bg}">
          <td><b>{t['symbol']}</b></td>
          <td style="color:#475569">{t['tf']}</td>
          <td>{floor_str}</td>
          <td>{ceil_px}</td>
          <td style="font-weight:600">{fill_px}</td>
          <td>₹{t['resistance']}</td>
          <td style="color:#64748b">{t['entry_time']}</td>
          <td style="color:#64748b">{t['exit_time']}</td>
          <td>₹{t['exit_price']}</td>
          <td style="color:#7c3aed">{t['reason']}</td>
          <td style="color:#0369a1">{rsi_str}</td>
          <td style="color:#0369a1">{vol_str}</td>
          <td style="color:#64748b">{qty_str}</td>
          <td style="color:#64748b">{invested}</td>
          <td style="color:{pnl_col};font-weight:bold">{t['pnl']}</td>
        </tr>"""

    gap_note = f" ({session['gap_fails']} gap-check escalations at open)" if session["gap_fails"] else ""

    # ── Trade table block
    if not trades:
        trade_block = '<p style="color:#9ca3af">No trades today.</p>'
    else:
        trade_block = (
            "<table>"
            "<tr><th>Stock</th><th>TF</th><th>Floor</th><th>Ceiling</th><th>Entry Fill</th><th>Resistance</th>"
            "<th>Entry Time</th><th>Exit Time</th><th>Exit Price</th>"
            "<th>Reason</th><th>RSI</th><th>Vol</th><th>Qty</th><th>Invested</th><th>P&amp;L</th></tr>"
            + rows +
            "</table>"
        )

    # ── Grouped skips block
    if not skips:
        skip_block = '<p style="color:#9ca3af">Nothing skipped.</p>'
    else:
        grouped = _group_skips(skips)
        skip_rows = ""
        for g in grouped:
            examples = ", ".join(g["examples"])
            if g["count"] > 3:
                examples += f" + {g['count'] - 3} more"
            skip_rows += (
                f"<tr>"
                f"<td style='white-space:nowrap;font-weight:600'>{g['count']}</td>"
                f"<td style='color:#374151'>{g['reason']}</td>"
                f"<td style='color:#6b7280;font-size:12px'>{examples}</td>"
                f"</tr>"
            )
        skip_block = (
            "<table>"
            "<tr><th>#</th><th>Reason</th><th>Examples</th></tr>"
            + skip_rows +
            "</table>"
        )

    # ── Narrative / remarks blocks
    narrative_block = f'<p style="line-height:1.8;color:#1e293b">{narrative}</p>' if narrative else ""
    remarks_block   = remarks if remarks else ""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
  body  {{ font-family: -apple-system, Arial, sans-serif; font-size: 13px; color: #111; background:#fff; margin:0; padding:16px; }}
  h1    {{ font-size: 19px; margin:0 0 6px 0; }}
  h2    {{ font-size: 13px; font-weight:600; margin:22px 0 6px 0;
           border-bottom:1px solid #e2e8f0; padding-bottom:4px; color:#334155; }}
  table {{ border-collapse:collapse; width:100%; font-size:12px; }}
  th    {{ background:#1e293b; color:#fff; padding:6px 10px; text-align:left; white-space:nowrap; }}
  td    {{ padding:5px 10px; border-bottom:1px solid #f1f5f9; white-space:nowrap; }}
  .pill {{ display:inline-block; border-radius:4px; padding:2px 10px; font-size:12px; font-weight:600; margin-right:6px; }}
  .g    {{ background:#dcfce7; color:#166534; }}
  .r    {{ background:#fee2e2; color:#991b1b; }}
  .b    {{ background:#f1f5f9; color:#475569; }}
  .meta {{ color:#64748b; font-size:12px; line-height:1.8; }}
</style></head><body>
<h1>🤖 PranUltimate Bot Report — {report_date.strftime('%d %B %Y')}</h1>
<div class="meta">
  Mode: <b>{header['mode']}</b> &nbsp;|&nbsp;
  Capital/trade: <b>₹{header['capital']}</b> &nbsp;|&nbsp;
  Leverage: <b>{header['leverage']}</b> &nbsp;|&nbsp;
  Max positions: <b>{header['max_pos']}</b> &nbsp;|&nbsp;
  Square-off: <b>{header['sq_off']}</b><br>
  Symbols scanned: <b>{header['symbols']}</b> &nbsp;|&nbsp;
  Confirmed boxes: <b>{header['boxes']}</b>
  {(' &nbsp;|&nbsp; ' + header['tf_breakdown']) if header['tf_breakdown'] else ''}
  {(' &nbsp;|&nbsp; <span style="color:#b45309">' + str(session['gap_fails']) + ' gap-check escalations</span>') if session['gap_fails'] else ''}
</div>

<h2>📊 P&amp;L Summary</h2>
<div>
  <span class="pill {'g' if total_pnl >= 0 else 'r'}">Total P&L: ₹{total_pnl:+,.2f}</span>
  <span class="pill g">Wins: {wins}</span>
  <span class="pill r">Losses: {losses}</span>
  <span class="pill b">Win Rate: {win_rate}</span>
  <span class="pill b">Trades: {n}</span>
</div>

<h2>📝 Session Summary</h2>
{narrative_block}

<h2>📋 Trade Table</h2>
{trade_block}

<h2>🚫 Rejected / Skipped ({len(skips)} total{gap_note})</h2>
{skip_block}

<h2>💡 Remarks</h2>
{remarks_block}

<hr style="margin-top:28px;border:none;border-top:1px solid #e5e7eb">
<p style="font-size:11px;color:#9ca3af">
  Generated by PranUltimate email_report.py &nbsp;·&nbsp;
  {datetime.now().strftime('%d %b %Y %H:%M')} IST
</p>
</body></html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1:
        try:
            target = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
        except ValueError:
            log.error(f"Invalid date '{sys.argv[1]}' — use YYYY-MM-DD")
            sys.exit(1)
    else:
        target = date.today()

    log.info(f"Generating report for {target}")

    secrets = load_secrets()
    if secrets is None:
        sys.exit(1)

    sender    = secrets.get("REPORT_EMAIL_SENDER", "").strip()
    app_pass  = secrets.get("REPORT_EMAIL_APP_PASSWORD", "").strip()
    recipient = secrets.get("REPORT_EMAIL_RECIPIENT", "pranadfb123@gmail.com").strip()

    if not sender or not app_pass:
        log.error("REPORT_EMAIL_SENDER or REPORT_EMAIL_APP_PASSWORD missing in report_secrets.env")
        sys.exit(1)

    log_path = find_log(target)
    if log_path is None:
        log.error(f"No bot log found for {target}")
        sys.exit(1)

    log.info(f"Reading log: {log_path}")
    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    header  = parse_header(lines)
    trades  = parse_trades(lines)
    skips   = parse_skips(lines)
    session = parse_session_summary(lines)

    wins   = sum(1 for t in trades if t["pnl_num"] > 0)
    losses = sum(1 for t in trades if t["pnl_num"] < 0)
    total_pnl = session["total_pnl"] if session["total_pnl"] is not None else sum(t["pnl_num"] for t in trades)

    log.info(f"Parsed: {len(trades)} trades ({wins}W/{losses}L), {len(skips)} skips, "
             f"P&L ₹{total_pnl:+,.2f}")

    narrative = build_session_narrative(lines, trades, header, session)
    remarks   = build_remarks(trades, skips, session)
    html_body = build_html(target, header, trades, skips, session, narrative, remarks)

    subject = (
        f"PranUltimate {target.strftime('%d %b')} | "
        f"{len(trades)} trades | {wins}W/{losses}L | "
        f"P&L ₹{total_pnl:+,.0f}"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    log.info(f"Sending to {recipient} via Gmail SMTP...")
    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(sender, app_pass)
        smtp.sendmail(sender, recipient, msg.as_string())

    log.info(f"✓ Email sent: {subject}")


if __name__ == "__main__":
    main()
