#!/usr/bin/env python3
"""
Email weekly and daily earnings digests, split into Before-Open (BMO) and
After-Close (AMC), for every US company above a market-cap cutoff.

Each stock has a tap-to-add link. Tapping it on your phone opens the Todoist
app with the task name, all-day date, Priority 1, and project pre-filled so you
just press the add button. Nothing is added automatically -- YOU choose.

Plain-English flow:
  1. Ask Finnhub who reports in the selected weekly or daily date window, and
     whether it's BMO (before open) or AMC (after close).
  2. For each unique ticker, ask Finnhub for its market cap + company name.
  3. Keep only companies at/above MIN_MARKET_CAP on the target US exchanges,
     group them by day and by BMO/AMC.
  4. Build an HTML email with a Todoist "add" link on each row and send it to
     yourself via Gmail.

Data source note: Finnhub's free tier exposes the earnings calendar and basic
company profiles (market cap). There is no bulk market-cap screener on the free
tier, so we look up each reporting ticker individually and pace the calls to
respect the 60-requests/minute limit. The data-fetch functions are isolated so
the provider can be swapped without touching the email/link logic.

Secrets (stored safely in GitHub, never in this file):
  FINNHUB_API_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD
"""

import os
import sys
import ssl
import json
import time
import smtplib
import urllib.parse
import urllib.error
import urllib.request
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ------------------------------- CONFIG -------------------------------------
MIN_MARKET_CAP = 1_000_000_000     # $1B cutoff. Raise this if the list is too long.
PROJECT_NAME   = "Earnings"        # Todoist project the "add" links target.
                                   #   -> create a project called this in Todoist,
                                   #      or set to "" to drop tasks in your Inbox.
EXCHANGES      = "NASDAQ,NYSE"     # US exchanges to include.

ACCENT    = "#3BBFCF"   # button colour in the email
# ----------------------------------------------------------------------------

FINNHUB_API_KEY    = os.environ.get("FINNHUB_API_KEY", "").strip()
GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
# Note: the workflow always defines RECIPIENT (as an empty string when the
# secret is unset), so a plain get(default=...) won't fall back. Coalesce an
# empty value to GMAIL_ADDRESS explicitly.
RECIPIENT          = os.environ.get("RECIPIENT", "").strip() or GMAIL_ADDRESS

UAE = ZoneInfo("Asia/Dubai")
FINNHUB = "https://finnhub.io/api/v1"

# Finnhub free tier allows 60 requests/minute; pace per-symbol lookups just
# under that so a busy week never trips a 429.
RATE_LIMIT_SLEEP = 1.1                # seconds between profile lookups

# US exchange name fragments accepted from Finnhub's profile 'exchange' field
# (it returns full names like "NASDAQ NMS - GLOBAL MARKET").
_EXCHANGE_TOKENS = [x.strip().upper() for x in EXCHANGES.split(",") if x.strip()]


# -------------------------------- helpers -----------------------------------
def http_get(url, retries=4):
    # Retry on HTTP 429 (Finnhub's rate-limit signal) with exponential backoff,
    # so bursts or a concurrent run don't hard-fail the job.
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": "earnings-bot"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode())
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(2 * (2 ** attempt))    # 2, 4, 8, 16s
                continue
            raise
    # Finnhub reports problems as a JSON object with an "error" key; surface it
    # clearly instead of failing later with a confusing TypeError.
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Finnhub API error: {data['error']}")
    return data


def compute_window(mode):
    """Return the (start, end) date window for the given email mode.

    weekly -> Mon..Fri of the current week (rolls to next week on weekends, so a
              Sunday send previews the upcoming trading week).
    daily  -> today and tomorrow in UAE, matching the 08:00 UAE schedule."""
    today = datetime.now(UAE).date()
    if mode == "daily":
        return today, today + timedelta(days=1)
    if today.weekday() >= 5:                  # Sat(5)/Sun(6) -> next Monday
        today = today + timedelta(days=7 - today.weekday())
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=4)


def get_earnings(from_date, to_date):
    """One Finnhub call: earnings events (symbol, date, hour) in the window.

    Returns the raw list of {'symbol', 'date', 'hour', ...} dicts. Finnhub's
    'hour' field carries the bmo/amc/dmh timing that the bucketing relies on."""
    url = (f"{FINNHUB}/calendar/earnings?"
           f"from={from_date}&to={to_date}&token={FINNHUB_API_KEY}")
    data = http_get(url)
    return data.get("earningsCalendar", []) if isinstance(data, dict) else []


def is_target_exchange(exchange):
    """True if Finnhub's profile exchange name matches one of EXCHANGES."""
    e = (exchange or "").upper()
    if not _EXCHANGE_TOKENS:
        return True
    # Finnhub spells NYSE out as "NEW YORK STOCK EXCHANGE".
    if "NYSE" in _EXCHANGE_TOKENS and "NEW YORK STOCK EXCHANGE" in e:
        return True
    return any(tok in e for tok in _EXCHANGE_TOKENS)


def get_profile(symbol):
    """One Finnhub call: {'cap': float_usd, 'name': str, 'exchange': str} for a
    ticker, or None if the profile is empty. Finnhub returns marketCapitalization
    in millions of USD, so scale it up to raw dollars."""
    url = f"{FINNHUB}/stock/profile2?symbol={urllib.parse.quote(symbol)}&token={FINNHUB_API_KEY}"
    data = http_get(url)
    if not isinstance(data, dict) or not data:
        return None
    cap_millions = data.get("marketCapitalization")
    if not cap_millions:
        return None
    return {
        "cap": float(cap_millions) * 1_000_000,
        "name": data.get("name") or symbol,
        "exchange": data.get("exchange") or "",
    }


def timing_bucket(raw):
    """Map Finnhub's 'hour' field to an email/task-name bucket."""
    t = (raw or "").strip().lower()
    if t in ("bmo", "before market open"):
        return "bmo"
    if t in ("amc", "after market close"):
        return "amc"
    if t in ("dmh", "during market hours"):
        return "dmh"
    if ":" in t:                              # a real clock time like "08:30"
        try:
            hh = int(t.split(":")[0])
            return "amc" if hh >= 16 else "bmo"
        except ValueError:
            pass
    return "tbd"


# Session suffix appended to the task name (e.g. "GOOGL Earnings - AMC").
# tbd -> no suffix, since the session is unknown.
BUCKET_SUFFIX = {"bmo": " - BMO", "amc": " - AMC", "dmh": " - DMH", "tbd": ""}


def quick_add_link(symbol, dt, bucket):
    """Build a todoist:// link that opens the app's quick-add, pre-filled.

    Uses Todoist natural-language syntax: the date sets the due date (no time),
    'p1' sets Priority 1, and '#Project' routes it. The session (BMO/AMC/DMH) is
    part of the task *name*, not the due time."""
    date_txt = f"{dt.strftime('%b')} {dt.day} {dt.year}"      # e.g. "Jul 24 2026"
    suffix = BUCKET_SUFFIX.get(bucket, "")
    proj = f" #{PROJECT_NAME}" if PROJECT_NAME else ""
    content = f"{symbol} Earnings{suffix} {date_txt} p1{proj}"
    return "todoist://openquickadd?content=" + urllib.parse.quote(content, safe="")


# ----------------------------- build the email ------------------------------
def money(cap):
    return f"${cap/1e9:.1f}B" if cap < 1e12 else f"${cap/1e12:.2f}T"


def row_html(ev):
    link = quick_add_link(ev["symbol"], ev["dt"], ev["bucket"])
    return f"""
    <tr>
      <td style="padding:10px 8px;border-bottom:1px solid #eee;">
        <div style="font-weight:700;font-size:15px;color:#171717;">{ev['symbol']}</div>
        <div style="font-size:12px;color:#888;">{ev['name']}</div>
      </td>
      <td style="padding:10px 8px;border-bottom:1px solid #eee;text-align:right;
                 white-space:nowrap;font-size:14px;color:#444;">{money(ev['cap'])}</td>
      <td style="padding:10px 8px;border-bottom:1px solid #eee;text-align:right;
                 width:52px;">
        <table role="presentation" align="right" width="40" height="40"
               cellpadding="0" cellspacing="0" style="border-collapse:separate;">
          <tr>
            <td width="40" height="40" align="center" valign="middle"
                bgcolor="{ACCENT}" style="width:40px;height:40px;background:{ACCENT};
                border-radius:10px;text-align:center;vertical-align:middle;">
              <a href="{link}" title="Add to Todoist"
                 aria-label="Add {ev['symbol']} to Todoist"
                 style="display:block;width:40px;color:#fff;text-decoration:none;
                        font-family:Arial,sans-serif;font-size:23px;font-weight:700;
                        line-height:24px;text-align:center;">&#43;</a>
            </td>
          </tr>
        </table>
      </td>
    </tr>"""


def section_html(label, events):
    if not events:
        return ""
    events.sort(key=lambda e: e["cap"], reverse=True)
    rows = "".join(row_html(e) for e in events)
    return f"""
      <div style="margin:18px 0 8px;">
        <span style="display:inline-block;background:{ACCENT};color:#fff;
                     font-size:12px;font-weight:800;letter-spacing:.8px;
                     text-transform:uppercase;padding:6px 12px;border-radius:6px;
                     box-shadow:0 1px 2px rgba(0,0,0,.12);">{label}</span>
      </div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;">{rows}</table>"""


def build_email(events_by_day, start, end, count, heading):
    days_html = ""
    for day in sorted(events_by_day):
        buckets = events_by_day[day]
        n = sum(len(buckets[b]) for b in ("bmo", "amc", "dmh", "tbd"))
        header = day.strftime("%A, %b ") + str(day.day)
        # <details>/<summary> gives native tap-to-collapse per day in clients that
        # support it (Apple Mail, Outlook for Mac, etc.); Gmail ignores it and
        # simply shows each day expanded. Default 'open' keeps it readable either way.
        days_html += f"""
          <details open style="margin-top:26px;">
            <summary style="font-size:17px;font-weight:700;color:#171717;
                            border-bottom:2px solid {ACCENT};padding-bottom:6px;
                            cursor:pointer;">{header}
              <span style="font-size:13px;font-weight:600;color:#999;">&nbsp;({n})</span>
            </summary>
            {section_html("Before open (BMO)", buckets["bmo"])}
            {section_html("After close (AMC)", buckets["amc"])}
            {section_html("During hours", buckets["dmh"])}
            {section_html("Time not confirmed", buckets["tbd"])}
          </details>"""

    range_txt = f"{start.strftime('%b')} {start.day} \u2013 {end.strftime('%b')} {end.day}"
    return f"""\
<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:#f5f6f7;">
  <div style="max-width:640px;margin:0 auto;padding:20px 16px;
              font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
    <div style="font-size:22px;font-weight:800;color:#171717;">{heading} - {range_txt}</div>
    {days_html if days_html else
     '<div style="margin-top:24px;color:#666;">No qualifying earnings found.</div>'}
    <div style="margin-top:30px;font-size:12px;color:#aaa;">
      Data: Finnhub. Times shown are the US session (BMO/AMC).</div>
  </div>
</body></html>"""


def send_email(html, subject):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT
    msg.attach(MIMEText("Open in an HTML-capable mail client.", "plain"))
    msg.attach(MIMEText(html, "html"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        s.sendmail(GMAIL_ADDRESS, [RECIPIENT], msg.as_string())


# Email modes -> (title heading, subject prefix)
MODES = {
    "weekly": "Earnings this week",
    "daily":  "Upcoming Earnings",
}


# ---------------------------------- main ------------------------------------
def main():
    if not (FINNHUB_API_KEY and GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        sys.exit("Missing one of: FINNHUB_API_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD.")

    mode = sys.argv[1].strip().lower() if len(sys.argv) > 1 else "weekly"
    if mode not in MODES:
        sys.exit(f"Unknown mode '{mode}'. Use one of: {', '.join(MODES)}.")
    heading = MODES[mode]

    start, end = compute_window(mode)
    print(f"Mode: {mode} | window {start} -> {end}")

    earnings = get_earnings(start.isoformat(), end.isoformat())
    print(f"Raw earnings events in window: {len(earnings)}")

    # Keep in-window events and collect the unique tickers we need caps for.
    events, symbols = [], []
    for e in earnings:
        sym, day = e.get("symbol"), e.get("date")
        if not sym or not day:
            continue
        dt = datetime.strptime(day, "%Y-%m-%d").date()
        if not (start <= dt <= end):
            continue
        events.append((sym, dt, e.get("hour")))
        if sym not in symbols:
            symbols.append(sym)

    # Build the market-cap "universe" one profile at a time (no bulk screener on
    # the free tier), pacing calls to stay under Finnhub's 60/min limit.
    print(f"Looking up market caps for {len(symbols)} unique tickers...")
    universe = {}
    for i, sym in enumerate(symbols):
        if i:
            time.sleep(RATE_LIMIT_SLEEP)
        prof = get_profile(sym)
        if not prof:
            continue
        if prof["cap"] >= MIN_MARKET_CAP and is_target_exchange(prof["exchange"]):
            universe[sym] = prof
    print(f"Universe at/above {money(MIN_MARKET_CAP)}: {len(universe)} companies")

    events_by_day, count = {}, 0
    for sym, dt, hour in events:
        if sym not in universe:
            continue
        bucket = timing_bucket(hour)
        events_by_day.setdefault(dt, {"bmo": [], "amc": [], "dmh": [], "tbd": []})
        events_by_day[dt][bucket].append({
            "symbol": sym,
            "name": universe[sym]["name"],
            "cap": universe[sym]["cap"],
            "dt": dt,
            "bucket": bucket,
        })
        count += 1

    print(f"Qualifying companies ({money(MIN_MARKET_CAP)}+): {count}")
    html = build_email(events_by_day, start, end, count, heading)
    subject = f"{heading} ({count}) \u2014 {start.strftime('%b')} {start.day}"
    send_email(html, subject)
    print("Email sent.")


if __name__ == "__main__":
    main()
