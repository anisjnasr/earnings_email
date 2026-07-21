# SPEC — Weekly Earnings Email with Todoist Quick-Add Links

## How to use this document in Cursor
Put this file in the repo root as `SPEC.md`, open the repo in Cursor, and prompt:
> "Implement the project exactly as described in SPEC.md. Create every file listed
> in the File Structure section. Follow the Hard Constraints and Known Gotchas
> precisely. Do not add libraries beyond the Python standard library."

Then work section by section. When something is ambiguous, prefer the explicit
instruction in this doc over any default Cursor would choose.

---

## 1. Purpose (plain English)
Once every weekday morning, automatically email me a digest of every US company
reporting earnings **this week** whose market cap is **at least $1B**. Group the
list by day, and within each day split it into **Before-Open (BMO)** and
**After-Close (AMC)**. Each company has a tap-to-add button; tapping it on my
phone opens the **Todoist app** with the task pre-filled (ticker + date + time)
so I choose which handful to actually add. Nothing is added automatically.

---

## 2. Hard constraints (do not deviate)
- **Language:** Python 3.12, **standard library only** (`urllib`, `json`, `time`,
  `smtplib`, `email`, `ssl`, `datetime`, `zoneinfo`, `os`, `sys`). No `requests`,
  no third-party packages in the script.
- **No server / no always-on process.** The whole thing runs as a scheduled
  **GitHub Actions** job (cron). This is why the "add" action must be a link the
  email client opens, not an API call the email makes.
- **Todoist integration is via the `todoist://` quick-add URL scheme only.**
  Do **NOT** call the Todoist REST/Sync API and do **NOT** require a Todoist API
  token. (The old Todoist REST v2 API is deprecated; avoid it entirely.)
- **Email is sent through Gmail SMTP** using an App Password (not OAuth, not the
  account password).
- All secrets come from environment variables. **Never hard-code keys.**

> **Data source note (v1.1):** The project originally targeted Financial Modeling
> Prep (FMP), but FMP retired its legacy `/api/v3/` endpoints for keys created
> after 2025-08-31 (they return `403`), and its stable screener + earnings
> calendar are paid-only (`402`). v1 therefore uses **Finnhub's free tier**
> instead. The data-fetch functions remain isolated so the provider can be
> swapped again if needed.

---

## 3. File structure
```
.
├── earnings_email.py            # the whole program
├── .github/
│   └── workflows/
│       └── earnings.yml         # the daily scheduler
└── SPEC.md                      # this document
```

---

## 4. Environment variables (GitHub repo secrets)
| Name                 | Required | Purpose                                             |
|----------------------|----------|-----------------------------------------------------|
| `FINNHUB_API_KEY`    | yes      | Finnhub API key (the data source; free tier is fine)|
| `GMAIL_ADDRESS`      | yes      | Gmail address that sends (and by default receives)  |
| `GMAIL_APP_PASSWORD` | yes      | 16-char Gmail App Password (2-Step Verification on) |
| `RECIPIENT`          | no       | Override the recipient; defaults to `GMAIL_ADDRESS` |

The script must exit with a clear error message if any of the three required
variables is missing.

> Note: the workflow always defines `RECIPIENT` as an env var (empty string when
> the secret is unset), so the script coalesces an empty `RECIPIENT` to
> `GMAIL_ADDRESS` rather than relying on a plain `.get(default=...)`.

---

## 5. Configuration constants (top of `earnings_email.py`)
Expose these as clearly-labelled constants so they're easy to tweak:
```
MIN_MARKET_CAP = 1_000_000_000   # $1B cutoff
PROJECT_NAME   = "Earnings"      # Todoist project for added tasks; "" = Inbox
EXCHANGES      = "NASDAQ,NYSE"   # US exchanges included
BMO_CLOCK      = "9:00am"        # task time for before-open reporters
AMC_CLOCK      = "4:30pm"        # task time for after-close reporters
DMH_CLOCK      = "12:00pm"       # task time for during-hours reporters
ACCENT         = "#3BBFCF"       # button colour in the email
RATE_LIMIT_SLEEP = 1.1           # seconds between Finnhub per-symbol lookups
```
`EXCHANGES` is used to filter Finnhub's profile `exchange` field (it returns full
names like `"NASDAQ NMS - GLOBAL MARKET"` and `"NEW YORK STOCK EXCHANGE, INC."`).
All market-time reasoning uses the `America/New_York` timezone via `zoneinfo`
(handles US daylight saving automatically).

---

## 6. Data source — Finnhub (free tier)

Finnhub's free tier has **no bulk market-cap screener**, so the flow is inverted
relative to a screener-first design: fetch the week's earnings calendar in one
call, then look up each unique ticker's market cap individually. The free tier
allows **60 requests/minute (US coverage, 1-month calendar range)**, so per-symbol
lookups are paced by `RATE_LIMIT_SLEEP` to avoid a `429`.

### 6a. Earnings calendar — one call
```
GET https://finnhub.io/api/v1/calendar/earnings
    ?from={monday}&to={friday}&token={FINNHUB_API_KEY}
```
Dates are `YYYY-MM-DD`. Response: JSON object `{"earningsCalendar": [ ... ]}`.
Use per row: `symbol` (str), `date` (str `YYYY-MM-DD`), `hour` (str).
The `hour` field is one of `"bmo"`, `"amc"`, `"dmh"`, `""`, or occasionally a
clock string like `"08:30"`.

### 6b. Market cap + name (per unique ticker) — one call each
```
GET https://finnhub.io/api/v1/stock/profile2
    ?symbol={symbol}&token={FINNHUB_API_KEY}
```
Response: JSON object. Use `marketCapitalization` (number, **in millions of
USD** — multiply by 1e6 for raw dollars), `name` (str), and `exchange` (str).
An empty `{}` or missing/zero market cap means "skip this symbol".
Build a dict of qualifiers: `universe[symbol] = {"cap": dollars, "name": name,
"exchange": exchange}` for symbols whose cap ≥ `MIN_MARKET_CAP` and whose
`exchange` matches `EXCHANGES`.

> Note: Finnhub reports API problems as a JSON object with an `error` key;
> surface it clearly in the log. Keep the data-fetch functions isolated so a
> future provider swap is easy.

---

## 7. Core logic (deterministic, step by step)

1. **Compute the week window.** Get "today" in `America/New_York`. If it's
   Saturday or Sunday, roll forward to next Monday. `monday = today - weekday`,
   `friday = monday + 4 days`. This is the Mon–Fri window shown in the email.
2. **Fetch earnings** for `monday`→`friday` (6a). Log the raw count.
3. **Collect in-window events + unique tickers.** For each earnings row: skip if
   no `symbol`/`date`; parse `date` and skip if outside `[monday, friday]`;
   remember `(symbol, date, hour)` and the set of unique symbols.
4. **Build the market-cap universe** (6b). For each unique ticker (paced by
   `RATE_LIMIT_SLEEP`), fetch its profile; keep it if `cap ≥ MIN_MARKET_CAP` and
   its exchange matches `EXCHANGES`. Log how many tickers were looked up and how
   many qualified.
5. **Filter + bucket.** For each in-window event:
   - Skip if `symbol` not in `universe` (below the cap or wrong exchange).
   - Map `hour` → bucket + clock via the table below.
   - Append to `events_by_day[date][bucket]` with `{symbol, name, cap, dt, clock}`.
   - Increment a running `count`.
6. **Sort** each bucket's list by `cap` descending (biggest company first).
7. **Build the HTML email** (section 8).
8. **Send** via Gmail SMTP (section 9).
9. **Log** the final qualifying count and "Email sent."

### Timing map (`hour` field → bucket, clock)
| Raw `hour` value                         | Bucket | Clock used   |
|------------------------------------------|--------|--------------|
| `bmo`, `before market open`              | bmo    | `BMO_CLOCK`  |
| `amc`, `after market close`              | amc    | `AMC_CLOCK`  |
| `dmh`, `during market hours`             | dmh    | `DMH_CLOCK`  |
| clock string, hour ≥ 16                  | amc    | `AMC_CLOCK`  |
| clock string, hour < 16                  | bmo    | `BMO_CLOCK`  |
| empty / anything else                    | tbd    | `""` (none)  |

---

## 8. The Todoist quick-add link (most important detail)

Each stock row contains an anchor whose `href` is a **Todoist quick-add URL**.
Tapping it opens the Todoist app's Quick Add panel **pre-filled but not
submitted** — the user taps Todoist's own add button to confirm.

**Format:**
```
todoist://openquickadd?content=<URL-ENCODED CONTENT>
```

**Content string (before encoding):**
```
{SYMBOL} Earnings[ - {SESSION}] {Mon} {D} {YYYY} p1 #{PROJECT_NAME}
```
- Example: `NVDA Earnings - AMC Jul 24 2026 p1 #Earnings`
- The date is written in **Todoist natural-language date syntax** (no time of
  day) so Todoist sets an all-day due date. Include the year to avoid ambiguity.
- `SESSION` is the uppercased bucket (`BMO`/`AMC`/`DMH`) appended to the task
  **name**; omit the ` - {SESSION}` segment for the `tbd` bucket.
- `p1` sets **Priority 1** (highest) on every task.
- Note the capital **E** in `Earnings`.
- If `PROJECT_NAME` is `""`, omit the `#...` segment → task goes to Inbox.
- URL-encode the **entire** content with `urllib.parse.quote(content, safe="")`
  so spaces become `%20` and `#` becomes `%23` (critical — an unencoded `#`
  would be treated as a URL fragment and break the project assignment).

No Todoist token, no API call, no network request from the script for this step.

---

## 9. Email delivery — Gmail SMTP
- Build a `MIMEMultipart("alternative")` with a short plain-text part and the
  HTML part.
- Subject: `Earnings this week ({count}) — {Mon} {D}`.
- Send with `smtplib.SMTP_SSL("smtp.gmail.com", 465)` using an
  `ssl.create_default_context()`, then `login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)`
  and `sendmail(GMAIL_ADDRESS, [RECIPIENT], msg.as_string())`.

---

## 10. Email layout spec (HTML, mobile-first)
- Single-column, `max-width:640px`, centered, system font stack, light-grey page
  background (`#f5f6f7`), white content area.
- **Header:** bold title "Earnings this week"; subline with the week range, the
  qualifying count, the cutoff, and a hint to tap **+ Add**.
- **Per day** (only days that have events), in date order:
  - Each day is a collapsible `<details open>` block; the day heading is the
    `<summary>` (e.g. "Thursday, Jul 24  (12)" with the qualifying count) with an
    accent-colour bottom border. Native tap-to-collapse works in clients that
    support `<details>` (Apple Mail, Outlook for Mac, …); Gmail ignores it and
    shows the day expanded. Default `open` keeps every client readable.
  - Then, in this order, only for non-empty buckets:
    `Before open (BMO)`, `After close (AMC)`, `During hours`, `Time not confirmed`.
  - Each bucket label is an emphasized **accent-filled pill** (white uppercase
    bold text on the accent colour) so BMO/AMC stand out, followed by rows.
- **Each row** = a 3-column table row:
  1. Ticker (bold) with company name beneath it in small grey text.
  2. Market cap, right-aligned (`$X.XB`, or `$X.XXT` at/above a trillion).
  3. A compact **`+` icon** button (no text): ~40×40 accent square, white plus,
     rounded (tap target ≥ ~40px), `href` = the quick-add URL from §8.
- Use **inline CSS only** (email clients strip `<style>` blocks unreliably).
- Footer: tiny grey line crediting Finnhub and noting times are the US session.
- If there are zero qualifying companies, show a friendly "No qualifying
  earnings found this week." message instead of empty tables.

---

## 11. GitHub Actions workflow (`.github/workflows/earnings.yml`)
- Triggers: `schedule` cron `0 4 * * 1-5` (04:00 UTC weekdays = 08:00 UAE time,
  UTC+4, no DST) **and** `workflow_dispatch` (manual "Run workflow" button).
- Job on `ubuntu-latest`:
  1. `actions/checkout@v4`
  2. `actions/setup-python@v5` with `python-version: "3.12"`
  3. `pip install tzdata` (ensures `zoneinfo` has the timezone database)
  4. Run `python earnings_email.py` with `FINNHUB_API_KEY`, `GMAIL_ADDRESS`,
     `GMAIL_APP_PASSWORD`, and `RECIPIENT` passed from `secrets` as `env`.

> Runtime note: because each reporting ticker needs its own profile lookup, a
> busy week (~500 tickers) takes several minutes per run at 60 req/min. This is
> well within the Actions job timeout.

---

## 12. Error handling & edge cases
- Missing required env var → `sys.exit` with a clear message.
- Finnhub HTTP error, non-JSON body, or a JSON `{"error": ...}` object → surface
  it / let it raise; the Actions log shows it.
- Empty earnings list or empty universe → still send a well-formed email
  (use the "no qualifying earnings" state if nothing qualifies).
- A symbol in the earnings feed whose cap is below the cutoff, whose exchange is
  off-target, or whose profile is empty → silently skipped.
- Symbols with unusual `hour` values → fall into the `tbd` bucket, all-day task.
- Duplicate-safety is **not** required: the email is a fresh view each day and
  the user manually picks what to add.

---

## 13. Acceptance criteria (how to verify)
1. Manually triggering the workflow ("Run workflow") completes green and the log
   prints the raw earnings count, the ticker-lookup count, the universe count,
   the qualifying count, and "Email sent."
2. An email arrives at `RECIPIENT` within ~1 minute.
3. The email shows the current Mon–Fri range and only companies ≥ $1B.
4. Each day is split into BMO/AMC; rows are sorted biggest-cap first.
5. On a phone with the Todoist app installed, tapping **+ Add** opens Todoist's
   Quick Add pre-filled with the ticker, the correct date, and the correct time,
   and the task lands in the `Earnings` project when confirmed.
6. Changing `MIN_MARKET_CAP` and re-running visibly changes the list length.

---

## 14. Known gotchas (call these out to Cursor)
- **Do not** use the Todoist REST/Sync API or any `Bearer` token for adding
  tasks — the whole design relies on the `todoist://openquickadd` scheme.
- **Do not** import `requests` or any pip package inside the script; stdlib only.
- The `#project` in the quick-add content **must be URL-encoded** (`%23`).
- Gmail SMTP needs an **App Password**, which requires **2-Step Verification**;
  the normal account password will be rejected.
- Use `smtp.gmail.com:465` with SSL (or `587` with STARTTLS) — plain/unencrypted
  connections are refused.
- `zoneinfo` needs the tz database on the runner; the workflow installs `tzdata`.
- Keep the Finnhub fetches in their own functions so the data provider can be
  swapped later without touching email/link logic.
- Finnhub's `marketCapitalization` is in **millions of USD** — scale by 1e6.
- Respect Finnhub's **60 req/min** free limit; pace per-symbol lookups.
- Finnhub's `exchange` field uses full names (e.g. `"NASDAQ NMS - GLOBAL MARKET"`,
  `"NEW YORK STOCK EXCHANGE, INC."`), so match on substrings, not `==`.
- **FMP is not usable here:** legacy `/api/v3/` returns `403` for keys made after
  2025-08-31, and the stable screener/earnings calendar are paid (`402`).

---

## 15. Out of scope for v1 (do not build now)
- One-click "silently add via API" links (would need a hosted endpoint).
- Deduplication / editing of already-added tasks.
- Non-US exchanges, ETFs, or after-the-fact actual-vs-estimate results.
- Any database or persistent state.
