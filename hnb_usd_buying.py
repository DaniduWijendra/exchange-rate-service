#!/usr/bin/env python3
"""
Track how HNB's (Hatton National Bank, Sri Lanka) USD *buying* rate moves over time.

https://www.hnb.lk/exchange-rates is a client-rendered React app: the rates are not
in the raw HTML, so we render the page with Playwright, pull the USD row out of the
rate table, append it to a CSV, and plot the series.

The page only ever shows *today's* rates, so the history is built by running this on
a schedule (cron / Task Scheduler). Each run appends one row per date.

Setup
-----
    pip install playwright pandas matplotlib
    playwright install chromium

Usage
-----
    python hnb_usd_buying.py fetch              # scrape today's rate, append to CSV
    python hnb_usd_buying.py fetch --show       # same, but print every number found
    python hnb_usd_buying.py fetch --headful    # watch the browser (debugging)
    python hnb_usd_buying.py plot               # chart the accumulated history
    python hnb_usd_buying.py show               # dump the CSV to the terminal

Cron example (weekdays, 10:30 local time):
    30 10 * * 1-5 cd /path/to/dir && /usr/bin/python3 hnb_usd_buying.py fetch >> log.txt 2>&1
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sys
from pathlib import Path

URL = "https://www.hnb.lk/exchange-rates"
CSV_PATH = Path("hnb_usd_buying.csv")

# Verified against the live page on 2026-08-25. HNB publishes exactly two columns
# per currency, left to right:
#   0: Telegraphic Transfer Buying Rate (LKR)
#   1: Telegraphic Transfer Selling Rate (LKR)
# There are no currency-notes columns on this page. Run `fetch --show` if the
# layout ever changes.
COLUMN_LABELS = ["tt_buying", "tt_selling"]
DEFAULT_BUYING_COLUMN = "tt_buying"

NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})*\.\d{1,4}|\d+\.\d{1,4}")
# Every other code HNB lists. If one of these appears on a line, that line is not
# the USD row -- this is what stops the parser reading AUD's figures as USD.
OTHER_CURRENCY_CODES = [
    "AUD", "GBP", "CAD", "CNY", "DKK", "EUR", "HKD", "INR", "JPY",
    "NZD", "NOK", "SGD", "SEK", "CHF", "THB", "AED",
]
# Plausible LKR-per-USD band. Rates outside this are almost certainly not FX rates
# (page furniture, phone numbers, version strings, etc.).
SANE_RANGE = (100.0, 1000.0)


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #
def fetch_page_text(headful: bool = False, timeout_ms: int = 45_000) -> str:
    """Render the exchange-rates page and return its visible text."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "Playwright is required.\n"
            "  pip install playwright pandas matplotlib\n"
            "  playwright install chromium"
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        page.goto(URL, wait_until="networkidle", timeout=timeout_ms)
        # Wait until the currency actually appears rather than a fixed sleep.
        try:
            page.wait_for_function(
                "() => /USD|US\\s*DOLLAR/i.test(document.body.innerText)",
                timeout=timeout_ms,
            )
        except Exception:
            pass  # fall through; the parser will complain if it's genuinely absent
        text = page.inner_text("body")
        browser.close()
    return text


def parse_usd_row(page_text: str, verbose: bool = False) -> dict[str, float]:
    """
    Find the USD rate row and map its numbers to columns.

    Deliberately strict, because a wrong number here is worse than no number:
    an earlier version matched the page's disclaimer ("...up to USD 1000...")
    and then read the *next* table row, silently reporting AUD's rates as USD.

    Two rules prevent that class of error:
      1. A candidate line must not mention any other currency code.
      2. Numbers are only taken from the USD line itself, or from immediately
         following lines that contain nothing but a number. A non-numeric line
         (the next currency's name) stops collection.
    """
    lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
    n_cols = len(COLUMN_LABELS)

    def nums(text: str) -> list[float]:
        return [
            float(m.replace(",", ""))
            for m in NUMBER_RE.findall(text)
            if SANE_RANGE[0] <= float(m.replace(",", "")) <= SANE_RANGE[1]
        ]

    def is_number_only(text: str) -> bool:
        return bool(re.fullmatch(r"[\d,]+\.\d{1,4}", text.strip()))

    rejected: list[str] = []
    candidate = None
    for i, line in enumerate(lines):
        if not re.search(r"\bUSD\b|\bUS\s*DOLLAR", line, re.I):
            continue

        # Guard 1: a real USD row names no other currency.
        others = [c for c in OTHER_CURRENCY_CODES if re.search(rf"\b{c}\b", line)]
        if others:
            rejected.append(f"{line[:60]!r} (mentions {', '.join(others)})")
            continue

        # Guard 2: prose, not a data row. The disclaimer is long and wordy.
        if len(line.split()) > 10:
            rejected.append(f"{line[:60]!r} (looks like prose, not a table row)")
            continue

        found = nums(line)
        if len(found) < n_cols:
            # Row may wrap onto its own numeric lines; collect only those.
            for nxt in lines[i + 1 : i + 1 + n_cols]:
                if not is_number_only(nxt):
                    break
                found.extend(nums(nxt))
        if len(found) >= n_cols:
            candidate = found[:n_cols]
            break
        rejected.append(f"{line[:60]!r} (only {len(found)} usable number(s))")

    if not candidate:
        detail = "\n  ".join(rejected) if rejected else "no line mentioned USD at all"
        raise RuntimeError(
            "Could not find a USD rate row. Candidates considered:\n  "
            f"{detail}\nRe-run `fetch --headful --show` to inspect the page."
        )

    if verbose:
        print(f"Numbers taken from the USD row: {candidate}")
        if rejected:
            print("Lines skipped: " + "; ".join(rejected))

    return dict(zip(COLUMN_LABELS, candidate))


def scrape_effective_date(page_text: str) -> str:
    """Pull the 'Last updated' date off the page, falling back to today."""
    # The live page prints "Last updated : 2026-08-25" (ISO). Try that first.
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", page_text)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return dt.date(y, mo, d).isoformat()
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](20\d{2})", page_text)
    if m:
        d, mo, y = (int(g) for g in m.groups())
        try:
            return dt.date(y, mo, d).isoformat()
        except ValueError:
            pass
    return dt.date.today().isoformat()


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
FIELDNAMES = ["date", "scraped_at", *COLUMN_LABELS]


def append_snapshot(row: dict, path: Path = CSV_PATH) -> bool:
    """Append a row, skipping dates already recorded. Returns True if written."""
    existing_dates = set()
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            existing_dates = {r["date"] for r in csv.DictReader(f)}

    if row["date"] in existing_dates:
        print(f"{row['date']} already recorded — skipping.")
        return False

    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    return True


def load_history(path: Path = CSV_PATH):
    import pandas as pd

    if not path.exists():
        sys.exit(f"No data yet at {path}. Run `fetch` a few times first.")
    df = pd.read_csv(path, parse_dates=["date"]).sort_values("date")
    return df


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_fetch(args) -> None:
    text = fetch_page_text(headful=args.headful)
    rates = parse_usd_row(text, verbose=args.show)
    row = {
        "date": scrape_effective_date(text),
        "scraped_at": dt.datetime.now().isoformat(timespec="seconds"),
        **{col: rates.get(col, "") for col in COLUMN_LABELS},
    }
    buying = rates.get(DEFAULT_BUYING_COLUMN)
    if append_snapshot(row, Path(args.csv)):
        print(f"{row['date']}  USD buying ({DEFAULT_BUYING_COLUMN}): {buying:,.2f} LKR")
        for col in COLUMN_LABELS:
            if rates.get(col) is not None:
                print(f"    {col:<14} {rates[col]:>10,.2f}")


def cmd_plot(args) -> None:
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    df = load_history(Path(args.csv))
    col = args.column
    if col not in df.columns or df[col].dropna().empty:
        sys.exit(f"Column '{col}' has no data. Available: {list(df.columns)}")

    series = df.dropna(subset=[col])
    if len(series) < 2:
        print(
            f"Only {len(series)} data point(s) so far — the chart will be sparse. "
            "Keep running `fetch` daily to build a trend."
        )

    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.plot(series["date"], series[col], marker="o", markersize=4,
            linewidth=1.8, color="#1f6f8b", label=f"USD {col}")

    if len(series) >= 7:
        ax.plot(series["date"], series[col].rolling(7, min_periods=3).mean(),
                linestyle="--", linewidth=1.2, color="#c1502e",
                label="7-point moving average")

    first, last = series[col].iloc[0], series[col].iloc[-1]
    change = last - first
    pct = (change / first * 100) if first else 0.0
    ax.set_title(
        f"HNB USD buying rate  ·  {series['date'].iloc[0]:%d %b %Y} → "
        f"{series['date'].iloc[-1]:%d %b %Y}\n"
        f"{first:,.2f} → {last:,.2f} LKR  ({change:+,.2f}, {pct:+.2f}%)",
        fontsize=12, loc="left",
    )
    ax.set_ylabel("LKR per USD")
    ax.grid(alpha=0.25, linewidth=0.6)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    fig.autofmt_xdate()
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()

    out = Path(args.out)
    fig.savefig(out, dpi=150)
    print(f"Chart written to {out}")
    if args.display:
        plt.show()


def cmd_show(args) -> None:
    df = load_history(Path(args.csv))
    print(df.to_string(index=False))
    col = DEFAULT_BUYING_COLUMN
    if col in df and not df[col].dropna().empty:
        s = df[col].dropna()
        print(f"\nmin {s.min():,.2f}   max {s.max():,.2f}   latest {s.iloc[-1]:,.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", default=str(CSV_PATH), help="data file path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="scrape today's rate and append it")
    f.add_argument("--headful", action="store_true", help="show the browser window")
    f.add_argument("--show", action="store_true", help="print all numbers parsed")
    f.set_defaults(func=cmd_fetch)

    p = sub.add_parser("plot", help="chart the history")
    p.add_argument("--column", default=DEFAULT_BUYING_COLUMN, choices=COLUMN_LABELS)
    p.add_argument("--out", default="hnb_usd_buying.png")
    p.add_argument("--display", action="store_true", help="open an interactive window")
    p.set_defaults(func=cmd_plot)

    s = sub.add_parser("show", help="print the stored data")
    s.set_defaults(func=cmd_show)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
