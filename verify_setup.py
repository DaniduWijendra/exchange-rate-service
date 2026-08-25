#!/usr/bin/env python3
"""
Self-check for the HNB rate watcher. Run this after replacing the scripts:

    python3 verify_setup.py

It confirms the AUD-bleed bug is actually fixed, that the environment is wired
up, and that nothing is misconfigured -- without ever printing a secret value.
Exits non-zero if anything important is wrong, so it is safe to use in CI too.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

PASS, FAIL, WARN = "  ok  ", " FAIL ", " warn "
failures = 0


def report(status: str, msg: str) -> None:
    global failures
    if status is FAIL:
        failures += 1
    print(f"[{status}] {msg}")


# --------------------------------------------------------------------------- #
# 1. Files present
# --------------------------------------------------------------------------- #
print("\n--- files ---")
here = Path(__file__).resolve().parent
for name, required in [
    ("hnb_usd_buying.py", True),
    ("hnb_watch.py", True),
    ("hnb_usd_buying.csv", False),
    (".env", False),
    (".gitignore", False),
]:
    p = here / name
    if p.exists():
        report(PASS, f"{name} present")
    elif required:
        report(FAIL, f"{name} MISSING -- the watcher cannot run without it")
    else:
        report(WARN, f"{name} not found (optional)")

if not (here / "hnb_usd_buying.py").exists():
    sys.exit("\nCannot continue without hnb_usd_buying.py.")

spec = importlib.util.spec_from_file_location("scraper", here / "hnb_usd_buying.py")
scraper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scraper)


# --------------------------------------------------------------------------- #
# 2. The parser fix -- the important part
# --------------------------------------------------------------------------- #
print("\n--- parser (regression tests for the AUD-bleed bug) ---")

if not hasattr(scraper, "OTHER_CURRENCY_CODES"):
    report(FAIL, "OTHER_CURRENCY_CODES missing -- you are still on the OLD, "
                 "buggy hnb_usd_buying.py. Replace it before running the watcher.")
else:
    report(PASS, "guard list present (new version installed)")

LIVE_PAGE = """Exchange Rates
Last updated : 2026-08-25
Rates are given on indicative basis for sales and purchases up to USD 1000 or its equivalent only
Currency Currency Code Telegraphic Transfer Buying Rate (LKR) Telegraphic Transfer Selling Rate (LKR)
Australian Dollars AUD 230.9432 239.1549
British Pounds GBP 441.9455 453.7849
UAE Dirham AED 87.8340 91.1669
US Dollars USD 325.0000 332.2500
Daily exchange rates are published as reference rates of the Bank."""

try:
    got = scraper.parse_usd_row(LIVE_PAGE)
    if got.get("tt_buying") == 230.9432:
        report(FAIL, "STILL BROKEN: parser returned the AUD rate (230.9432) as USD. "
                     "You are running the old file.")
    elif got.get("tt_buying") == 325.0 and got.get("tt_selling") == 332.25:
        report(PASS, f"reads the USD row correctly: {got}")
    else:
        report(FAIL, f"unexpected result: {got}")
except Exception as exc:
    report(FAIL, f"parser raised on the live page layout: {exc}")

# Wrapped layout (numbers on their own lines)
try:
    got = scraper.parse_usd_row(
        "UAE Dirham AED 87.8340 91.1669\nUS Dollars\nUSD\n325.0000\n332.2500\nend"
    )
    report(PASS if got.get("tt_buying") == 325.0 else FAIL,
           f"wrapped layout -> {got}")
except Exception as exc:
    report(FAIL, f"wrapped layout raised: {exc}")

# Must refuse to guess rather than return another currency
try:
    got = scraper.parse_usd_row(
        "Rates up to USD 1000 only\nAustralian Dollars AUD 230.9432 239.1549"
    )
    report(FAIL, f"guessed instead of raising: {got}")
except RuntimeError:
    report(PASS, "raises rather than guessing when the USD row is absent")
except Exception as exc:
    report(FAIL, f"crashed unexpectedly ({type(exc).__name__}: {exc})")

# Date parsing
d = scraper.scrape_effective_date("Last updated : 2026-08-25")
report(PASS if d == "2026-08-25" else FAIL, f"date parsed as {d}")


# --------------------------------------------------------------------------- #
# 3. Dependencies
# --------------------------------------------------------------------------- #
print("\n--- dependencies ---")
print(f"[{PASS}] python {sys.version.split()[0]}")
for mod, why in [
    ("playwright", "renders the JS page -- required"),
    ("requests", "Telegram delivery -- required"),
    ("pandas", "plotting/backtest"),
    ("gspread", "Google Sheets push"),
    ("google.oauth2", "Google Sheets auth"),
    ("matplotlib", "charting"),
]:
    try:
        importlib.import_module(mod)
        report(PASS, f"{mod} importable ({why})")
    except ImportError:
        level = FAIL if "required" in why else WARN
        report(level, f"{mod} NOT installed ({why})")

cache = Path.home() / "Library/Caches/ms-playwright"
if cache.exists() and any(cache.glob("chromium*")):
    report(PASS, "Playwright Chromium is downloaded")
else:
    report(WARN, "Chromium not found in the Playwright cache -- "
                 "run: python3 -m playwright install chromium")


# --------------------------------------------------------------------------- #
# 4. Configuration (names only, never values)
# --------------------------------------------------------------------------- #
print("\n--- configuration ---")
env_file = here / ".env"
if env_file.exists():
    mode = env_file.stat().st_mode & 0o777
    if mode & 0o077:
        report(FAIL, f".env is readable by other users (mode {mode:o}) -- run: chmod 600 .env")
    else:
        report(PASS, f".env permissions are {mode:o}")
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

def has(k: str) -> bool:
    return bool(os.getenv(k))

if has("TELEGRAM_BOT_TOKEN") and has("TELEGRAM_CHAT_ID"):
    report(PASS, "Telegram configured (token value not shown)")
else:
    report(WARN, "Telegram not configured -- alerts will only print to stdout")

if has("SHEET_ID") and has("GOOGLE_SA_JSON"):
    sa = Path(os.environ["GOOGLE_SA_JSON"]).expanduser()
    if not sa.exists():
        report(FAIL, f"GOOGLE_SA_JSON points to a missing file: {sa}")
    else:
        m = sa.stat().st_mode & 0o777
        report(FAIL if m & 0o077 else PASS,
               f"service account key found, mode {m:o}"
               + (" -- run chmod 600 on it" if m & 0o077 else ""))
        try:
            import json
            info = json.loads(sa.read_text())
            report(PASS, f"share the sheet with: {info.get('client_email', '??')}")
        except Exception as exc:
            report(FAIL, f"key file is not valid JSON: {exc}")
else:
    report(WARN, "Sheets not configured yet -- CSV only (this is step 3)")

if has("SMTP_PASS"):
    report(WARN, "SMTP_PASS is set. A Gmail App Password grants send-as-you access "
                 "to your whole mailbox; Telegram alone is safer.")

mode_ = os.getenv("ALERT_MODE", "n_day_high")
report(PASS, f"alert mode: {mode_}"
             + (f", threshold {os.getenv('ALERT_THRESHOLD')}" if os.getenv("ALERT_THRESHOLD") else "")
             + f", window {os.getenv('ALERT_WINDOW', '30')}d")
if mode_ == "n_day_high":
    csv = here / "hnb_usd_buying.csv"
    rows = max(0, len(csv.read_text().strip().splitlines()) - 1) if csv.exists() else 0
    if rows < 5:
        report(WARN, f"only {rows} reading(s) of history -- n_day_high will stay quiet "
                     "for a while. Set ALERT_THRESHOLD and ALERT_MODE=both meanwhile.")


# --------------------------------------------------------------------------- #
print("\n" + ("-" * 60))
if failures:
    print(f"{failures} problem(s) need fixing. See FAIL lines above.")
    sys.exit(1)
print("All checks passed. Next: python3 hnb_watch.py run")
