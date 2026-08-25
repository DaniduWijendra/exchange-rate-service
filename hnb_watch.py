#!/usr/bin/env python3
"""
Daily watcher for HNB's USD Telegraphic Transfer BUYING rate.

You are selling USD and receiving LKR, so a *higher* buying rate is better for you.
This module adds the two things the scraper lacked: pushing each day's reading into
your Google Sheet, and alerting you when today's rate is a high worth acting on.

It imports the scraper from hnb_usd_buying.py -- keep both files in the same folder.

    python hnb_watch.py run          # the daily entrypoint: scrape -> sheet -> alert
    python hnb_watch.py test-alert   # send a test notification, no scraping
    python hnb_watch.py backtest     # replay the alert rule over your CSV history

Configuration lives in a .env file beside this script, so no secret is written
into the code or into your shell history. Create it, then `chmod 600 .env`.
Set only what you use.

    # --- Google Sheet (optional; omit to write CSV only) ---
    SHEET_ID=1f1CZB6vt688B48VoNU1fK64huntXTwQW-zztvQ9WA4s
    GOOGLE_SA_JSON=/secure/path/service-account.json

    # --- alert rule ---
    ALERT_MODE=n_day_high     # n_day_high | threshold | both
    ALERT_WINDOW=30           # for n_day_high: "highest in this many days"
    ALERT_THRESHOLD=330.0     # for threshold: fire at or above this rate
    ALERT_COOLDOWN_DAYS=3     # stay quiet this long after firing

    # --- delivery: Telegram (arrives as a phone push; free) ---
    TELEGRAM_BOT_TOKEN=123456:ABC...
    TELEGRAM_CHAT_ID=987654321

    # --- delivery: email (optional, and not recommended) ---
    # A Gmail App Password grants send-as-you access to your entire mailbox.
    # Telegram already delivers to your phone; prefer it and leave these unset.
    # SMTP_HOST=smtp.gmail.com
    # SMTP_PORT=587
    # SMTP_USER=you@gmail.com
    # SMTP_PASS=your_app_password
    # ALERT_EMAIL_TO=you@gmail.com

Dependencies:
    pip install playwright pandas matplotlib gspread google-auth requests
    playwright install chromium
"""

from __future__ import annotations

import datetime as dt
import json
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

import hnb_usd_buying as scraper

STATE_PATH = Path("hnb_watch_state.json")
ENV_PATH = Path(".env")


def load_dotenv(path: Path = ENV_PATH) -> None:
    """
    Read KEY=value lines from .env into the environment.

    Keeps tokens out of ~/.zsh_history, which is what happens if you type
    `export TELEGRAM_BOT_TOKEN=...` at the prompt. Real environment variables
    always win, so CI (which injects them properly) is unaffected.
    """
    if not path.exists():
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        print(f"Warning: {path} is readable by other users (mode {mode:o}). "
              f"Run: chmod 600 {path}", file=sys.stderr)
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = val


# --------------------------------------------------------------------------- #
# Alert rule
# --------------------------------------------------------------------------- #
def evaluate_alert(
    history: list[tuple[str, float]],
    today_date: str,
    today_rate: float,
    mode: str = "n_day_high",
    window: int = 30,
    threshold: float | None = None,
    cooldown_days: int = 3,
    last_alert_date: str | None = None,
) -> tuple[bool, str]:
    """
    Decide whether today's buying rate deserves a notification.

    history: prior readings as (iso_date, rate), today excluded.
    Returns (should_alert, human_readable_reason).
    """
    if last_alert_date and cooldown_days > 0:
        try:
            gap = (dt.date.fromisoformat(today_date)
                   - dt.date.fromisoformat(last_alert_date)).days
            if gap < cooldown_days:
                return False, f"within {cooldown_days}-day cooldown (last alert {last_alert_date})"
        except ValueError:
            pass

    reasons: list[str] = []

    if mode in ("n_day_high", "both"):
        cutoff = dt.date.fromisoformat(today_date) - dt.timedelta(days=window)
        recent = [r for d, r in history if dt.date.fromisoformat(d) >= cutoff]
        if not recent:
            reasons.append("no prior history to compare against yet")
        elif today_rate > max(recent):
            reasons.append(
                f"highest in {window} days: {today_rate:,.4f} beats the previous "
                f"best of {max(recent):,.4f} (from {len(recent)} readings)"
            )

    if mode in ("threshold", "both") and threshold is not None:
        if today_rate >= threshold:
            reasons.append(f"at or above your target of {threshold:,.4f}")

    if mode == "both":
        # Require the high, and treat the threshold as a bonus note.
        high_hit = any("highest in" in r for r in reasons)
        thr_hit = any("target" in r for r in reasons)
        if not (high_hit or thr_hit):
            return False, "neither the high nor the threshold was met"
        return True, "; ".join(reasons)

    if reasons and not any("no prior history" in r for r in reasons):
        return True, "; ".join(reasons)
    if reasons:
        return False, reasons[0]
    if mode == "threshold":
        return False, f"{today_rate:,.4f} is below your target of {threshold:,.4f}"
    return False, f"{today_rate:,.4f} is not a new {window}-day high"


def build_message(date: str, rate: float, selling: float, reason: str,
                  history: list[tuple[str, float]]) -> tuple[str, str]:
    rates = [r for _, r in history]
    stats = ""
    if rates:
        stats = (
            f"\nRecent context: min {min(rates):,.4f} / max {max(rates):,.4f} "
            f"over {len(rates)} prior readings."
        )
    subject = f"HNB USD buying rate {rate:,.4f} LKR — {reason.split(':')[0]}"
    body = (
        f"HNB USD Telegraphic Transfer rates for {date}\n\n"
        f"  Buying  (what you get per USD): {rate:,.4f} LKR\n"
        f"  Selling (what the bank charges): {selling:,.4f} LKR\n\n"
        f"Why you're seeing this: {reason}.{stats}\n\n"
        "Before acting, two caveats:\n"
        "  - These are the bank's published indicative TT rates, quoted for amounts\n"
        "    up to about USD 1,000. For a larger conversion, call the branch or\n"
        "    treasury desk -- negotiated rates are often better than the published one.\n"
        "  - Rates change intraday without notice. Confirm before you transact.\n\n"
        f"Source: {scraper.URL}\n"
    )
    return subject, body


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
def send_email(subject: str, body: str) -> bool:
    host, user = os.getenv("SMTP_HOST"), os.getenv("SMTP_USER")
    password, to = os.getenv("SMTP_PASS"), os.getenv("ALERT_EMAIL_TO")
    if not all([host, user, password, to]):
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=30) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)
    print(f"Email sent to {to}")
    return True


def send_telegram(text: str) -> bool:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    import requests

    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text},
        timeout=30,
    )
    r.raise_for_status()
    print("Telegram notification sent")
    return True


def notify(subject: str, body: str) -> None:
    delivered = False
    for fn in (lambda: send_email(subject, body),
               lambda: send_telegram(f"{subject}\n\n{body}")):
        try:
            delivered |= bool(fn())
        except Exception as exc:  # never let a delivery failure lose the data
            print(f"Delivery failed: {exc}", file=sys.stderr)
    if not delivered:
        print("No delivery channel configured — alert printed only:\n")
        print(subject, "\n", body, sep="")


# --------------------------------------------------------------------------- #
# Google Sheet
# --------------------------------------------------------------------------- #
def push_to_sheet(row: list) -> bool:
    """Append one row to the sheet. Returns False if Sheets isn't configured."""
    sheet_id, sa_path = os.getenv("SHEET_ID"), os.getenv("GOOGLE_SA_JSON")
    if not (sheet_id and sa_path):
        return False
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        sa_path, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    ws = gspread.authorize(creds).open_by_key(sheet_id).sheet1

    existing = {r[0] for r in ws.get_all_values()[1:] if r and r[0]}
    if str(row[0]) in existing:
        print(f"{row[0]} already in the sheet — not duplicating.")
        return True

    ws.append_row(row, value_input_option="USER_ENTERED")
    print(f"Appended to sheet: {row}")
    return True


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def read_history(exclude_date: str | None = None) -> list[tuple[str, float]]:
    import csv as _csv

    path = scraper.CSV_PATH
    if not path.exists():
        return []
    out = []
    with path.open(newline="", encoding="utf-8") as f:
        for r in _csv.DictReader(f):
            val = r.get(scraper.DEFAULT_BUYING_COLUMN)
            if val and r["date"] != exclude_date:
                try:
                    out.append((r["date"], float(val)))
                except ValueError:
                    continue
    return sorted(out)


def cmd_run() -> None:
    text = scraper.fetch_page_text()
    rates = scraper.parse_usd_row(text)
    date = scraper.scrape_effective_date(text)
    buying = rates[scraper.DEFAULT_BUYING_COLUMN]
    selling = rates.get("tt_selling", float("nan"))
    print(f"{date}  buying {buying:,.4f}  selling {selling:,.4f}")

    scraper.append_snapshot(
        {
            "date": date,
            "scraped_at": dt.datetime.now().isoformat(timespec="seconds"),
            **{c: rates.get(c, "") for c in scraper.COLUMN_LABELS},
        }
    )
    push_to_sheet([date, buying, selling, "hnb.lk/exchange-rates"])

    history = read_history(exclude_date=date)
    state = load_state()
    fire, reason = evaluate_alert(
        history,
        date,
        buying,
        mode=os.getenv("ALERT_MODE", "n_day_high"),
        window=int(os.getenv("ALERT_WINDOW", "30")),
        threshold=float(os.getenv("ALERT_THRESHOLD")) if os.getenv("ALERT_THRESHOLD") else None,
        cooldown_days=int(os.getenv("ALERT_COOLDOWN_DAYS", "3")),
        last_alert_date=state.get("last_alert_date"),
    )
    print(("ALERT: " if fire else "No alert: ") + reason)

    if fire:
        subject, body = build_message(date, buying, selling, reason, history)
        notify(subject, body)
        state["last_alert_date"] = date
        state["last_alert_rate"] = buying
        save_state(state)


def cmd_test_alert() -> None:
    subject, body = build_message(
        dt.date.today().isoformat(), 325.0, 332.25,
        "test notification — delivery check only", [("2026-08-01", 318.0)],
    )
    notify(subject, body)


def cmd_backtest() -> None:
    history = read_history()
    if not history:
        sys.exit("No CSV history yet.")
    mode = os.getenv("ALERT_MODE", "n_day_high")
    window = int(os.getenv("ALERT_WINDOW", "30"))
    threshold = float(os.getenv("ALERT_THRESHOLD")) if os.getenv("ALERT_THRESHOLD") else None
    cooldown = int(os.getenv("ALERT_COOLDOWN_DAYS", "3"))
    last = None
    fired = 0
    for i, (date, rate) in enumerate(history):
        ok, reason = evaluate_alert(history[:i], date, rate, mode, window,
                                   threshold, cooldown, last)
        if ok:
            fired += 1
            last = date
            print(f"  {date}  {rate:>10,.4f}  <- {reason}")
    print(f"\n{fired} alert(s) across {len(history)} readings "
          f"({mode}, window={window}, cooldown={cooldown}d)")


def main() -> None:
    load_dotenv()
    cmds = {"run": cmd_run, "test-alert": cmd_test_alert, "backtest": cmd_backtest}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        sys.exit(f"Usage: python hnb_watch.py [{' | '.join(cmds)}]")
    cmds[sys.argv[1]]()


if __name__ == "__main__":
    main()
