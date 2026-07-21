#!/usr/bin/env python3
"""
Email a "this week" earnings digest, split into Before-Open (BMO) and
After-Close (AMC), for every US company above a market-cap cutoff.

Each stock has a tap-to-add link. Tapping it on your phone opens the Todoist
app with the task pre-filled (ticker + timing + date) so you just press the
add button. Nothing is added automatically -- YOU pick which ones to keep.

Plain-English flow each morning:
  1. Ask Financial Modeling Prep (FMP) for every US company above MIN_MARKET_CAP
     -> this gives us the ticker, company name, and market cap.
  2. Ask FMP who reports earnings Monday-Friday of the current week, and whether
     it's BMO (before open) or AMC (after close).
  3. Keep only companies in both lists, group them by day and by BMO/AMC.
  4. Build an HTML email with a Todoist "add" link on each row and send it to
     yourself via Gmail.

Secrets (stored safely in GitHub, never in this file):
  FMP_API_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD
"""

import os
import sys
import ssl
import json
import smtplib
import urllib.parse
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

# Task time-of-day (US Eastern) that the "add" link pre-fills.
# Todoist shows these in your local Dubai time automatically.
BMO_CLOCK = "9:00am"    # before market open
AMC_CLOCK = "4:30pm"    # after market close
DMH_CLOCK = "12:00pm"   # during market hours (rare)

ACCENT    = "#3BBFCF"   # button colour in the email
# ----------------------------------------------------------------------------

FMP_API_KEY        = os.environ.get("FMP_API_KEY", "").strip()
GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
RECIPIENT          = os.environ.get("RECIPIENT", GMAIL_ADDRESS).strip()

EASTERN = ZoneInfo("America/New_York")
FMP     = "https://financialmodelingprep.com"


# -------------------------------- helpers -----------------------------------
def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "earnings-bot"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode())
    # FMP signals gating/quota problems as a JSON object, not the usual array.
    # Surface it clearly instead of failing later with a confusing TypeError.
    if isinstance(data, dict) and "Error Message" in data:
        raise RuntimeError(f"FMP API error: {data['Error Message']}")
    return data


def current_week_mon_fri():
    """Return (monday, friday) dates for the current week; roll to next week
    on weekends so you always see a full trading week."""
    today = datetime.now(EASTERN).date()
    if today.weekday() >= 5:                 # Sat(5)/Sun(6) -> next Monday
        today = today + timedelta(days=7 - today.weekday())
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    return monday, friday


def get_universe():
    """One FMP call: {symbol: {'cap': float, 'name': str}} above the cutoff."""
    url = (f"{FMP}/stable/company-screener?"
           f"marketCapMoreThan={MIN_MARKET_CAP}"
           f"&exchange={EXCHANGES}"
           f"&isActivelyTrading=true&limit=3000&apikey={FMP_API_KEY}")
    uni = {}
    for row in http_get(url):
        sym, cap = row.get("symbol"), row.get("marketCap")
        if sym and cap:
            uni[sym] = {"cap": cap, "name": row.get("companyName") or sym}
    return uni


def get_earnings(from_date, to_date):
    """One FMP call: earnings events (symbol, date, time) in the window.

    includeReportTimes=true keeps the bmo/amc/tas 'time' field that the
    bucketing logic relies on (the stable endpoint omits it by default)."""
    url = (f"{FMP}/stable/earnings-calendar?"
           f"from={from_date}&to={to_date}"
           f"&includeReportTimes=true&apikey={FMP_API_KEY}")
    return http_get(url)


def timing_bucket(raw):
    """Map FMP's 'time' field to a bucket + the clock time for the task."""
    t = (raw or "").strip().lower()
    if t in ("bmo", "before market open"):
        return "bmo", BMO_CLOCK
    if t in ("amc", "after market close"):
        return "amc", AMC_CLOCK
    if t in ("dmh", "during market hours"):
        return "dmh", DMH_CLOCK
    if ":" in t:                              # a real clock time like "08:30"
        try:
            hh = int(t.split(":")[0])
            return ("amc", AMC_CLOCK) if hh >= 16 else ("bmo", BMO_CLOCK)
        except ValueError:
            pass
    return "tbd", ""                          # unknown -> all-day task


def quick_add_link(symbol, dt, clock):
    """Build a todoist:// link that opens the app's quick-add, pre-filled."""
    date_txt = f"{dt.strftime('%b')} {dt.day} {dt.year}"      # e.g. "Jul 24 2026"
    when = f"{date_txt} {clock}".strip()
    proj = f" #{PROJECT_NAME}" if PROJECT_NAME else ""
    content = f"{symbol} earnings {when}{proj}"
    return "todoist://openquickadd?content=" + urllib.parse.quote(content, safe="")


# ----------------------------- build the email ------------------------------
def money(cap):
    return f"${cap/1e9:.1f}B" if cap < 1e12 else f"${cap/1e12:.2f}T"


def row_html(ev):
    link = quick_add_link(ev["symbol"], ev["dt"], ev["clock"])
    return f"""
    <tr>
      <td style="padding:10px 8px;border-bottom:1px solid #eee;">
        <div style="font-weight:700;font-size:15px;color:#171717;">{ev['symbol']}</div>
        <div style="font-size:12px;color:#888;">{ev['name']}</div>
      </td>
      <td style="padding:10px 8px;border-bottom:1px solid #eee;text-align:right;
                 white-space:nowrap;font-size:14px;color:#444;">{money(ev['cap'])}</td>
      <td style="padding:10px 8px;border-bottom:1px solid #eee;text-align:right;">
        <a href="{link}"
           style="display:inline-block;background:{ACCENT};color:#fff;
                  text-decoration:none;font-size:14px;font-weight:600;
                  padding:10px 14px;border-radius:8px;">+ Add</a>
      </td>
    </tr>"""


def section_html(label, events):
    if not events:
        return ""
    events.sort(key=lambda e: e["cap"], reverse=True)
    rows = "".join(row_html(e) for e in events)
    return f"""
      <div style="font-size:12px;font-weight:700;letter-spacing:.5px;
                  text-transform:uppercase;color:#999;margin:14px 0 4px;">{label}</div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;">{rows}</table>"""


def build_email(events_by_day, monday, friday, count):
    days_html = ""
    for day in sorted(events_by_day):
        buckets = events_by_day[day]
        header = day.strftime("%A, %b ") + str(day.day)
        days_html += f"""
          <div style="margin-top:26px;">
            <div style="font-size:17px;font-weight:700;color:#171717;
                        border-bottom:2px solid {ACCENT};padding-bottom:6px;">{header}</div>
            {section_html("Before open (BMO)", buckets["bmo"])}
            {section_html("After close (AMC)", buckets["amc"])}
            {section_html("During hours", buckets["dmh"])}
            {section_html("Time not confirmed", buckets["tbd"])}
          </div>"""

    week_txt = f"{monday.strftime('%b')} {monday.day} \u2013 {friday.strftime('%b')} {friday.day}"
    return f"""\
<!DOCTYPE html><html><body style="margin:0;background:#f5f6f7;">
  <div style="max-width:640px;margin:0 auto;padding:20px 16px;
              font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
    <div style="font-size:22px;font-weight:800;color:#171717;">Earnings this week</div>
    <div style="font-size:14px;color:#666;margin-top:2px;">
      {week_txt} &middot; {count} companies &ge; {money(MIN_MARKET_CAP)} &middot;
      tap <b>+ Add</b> to send one to Todoist</div>
    {days_html if days_html else
     '<div style="margin-top:24px;color:#666;">No qualifying earnings found this week.</div>'}
    <div style="margin-top:30px;font-size:12px;color:#aaa;">
      Data: Financial Modeling Prep. Times shown are the US session (BMO/AMC).</div>
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


# ---------------------------------- main ------------------------------------
def main():
    if not (FMP_API_KEY and GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        sys.exit("Missing one of: FMP_API_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD.")

    monday, friday = current_week_mon_fri()
    universe = get_universe()
    print(f"Universe above {money(MIN_MARKET_CAP)}: {len(universe)} companies")

    earnings = get_earnings(monday.isoformat(), friday.isoformat())
    print(f"Raw earnings events this week: {len(earnings)}")

    events_by_day, count = {}, 0
    for e in earnings:
        sym, day = e.get("symbol"), e.get("date")
        if not sym or not day or sym not in universe:
            continue
        dt = datetime.strptime(day, "%Y-%m-%d").date()
        if not (monday <= dt <= friday):
            continue
        bucket, clock = timing_bucket(e.get("time"))
        events_by_day.setdefault(dt, {"bmo": [], "amc": [], "dmh": [], "tbd": []})
        events_by_day[dt][bucket].append({
            "symbol": sym,
            "name": universe[sym]["name"],
            "cap": universe[sym]["cap"],
            "dt": dt,
            "clock": clock,
        })
        count += 1

    print(f"Qualifying companies ({money(MIN_MARKET_CAP)}+): {count}")
    html = build_email(events_by_day, monday, friday, count)
    subject = f"Earnings this week ({count}) \u2014 {monday.strftime('%b')} {monday.day}"
    send_email(html, subject)
    print("Email sent.")


if __name__ == "__main__":
    main()
