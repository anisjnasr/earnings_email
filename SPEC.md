# SPEC — Weekly and Daily Earnings Emails with Todoist Quick-Add Links

## How to use this document in Cursor
Put this file in the repo root as `SPEC.md`, open the repo in Cursor, and prompt:
> "Implement the project exactly as described in SPEC.md. Create every file listed
> in the File Structure section. Follow the Hard Constraints and Known Gotchas
> precisely. Do not add libraries beyond the Python standard library."

Then work section by section. When something is ambiguous, prefer the explicit
instruction in this doc over any default Cursor would choose.

---

## 1. Purpose (plain English)
Send two automated digests for US companies with market cap **at least $1B**:
- A **weekly** email on Sunday morning covering the upcoming Monday–Friday.
- A **daily** email Monday–Friday covering the prior trading day through
  tomorrow (header/subject still show today–tomorrow; Monday’s prior day is
  Friday).

Group each list by day, and within each day split it into **Before-Open (BMO)**
and **After-Close (AMC)**. Each company has a tap-to-add button; tapping it on
my phone opens the **Todoist app** with the task name, all-day date, Priority 1,
and project pre-filled so I choose which handful to add. Nothing is added
automatically.

---

## 2. Hard constraints (do not deviate)
- **Language:** Python 3.12, **standard library only** (`urllib`, `json`, `time`,
  `smtplib`, `email`, `ssl`, `datetime`, `zoneinfo`, `os`, `sys`). No `requests`,
  no third-party packages in the script.
- **No application server / no always-on process.** The data/email job runs as
  scheduled **GitHub Actions**. A single static GitHub Pages redirect is allowed
  solely because Gmail Android strips `todoist://` links from email.
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
│       └── earnings.yml         # weekly + daily scheduler
├── docs/
│   └── add.html                 # static cross-platform Todoist redirect
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
ACCENT         = "#3BBFCF"       # button colour in the email
RATE_LIMIT_SLEEP = 1.1           # seconds between Finnhub per-symbol lookups
TODOIST_REDIRECT_URL = "https://anisjnasr.github.io/earnings_email/add.html"
```
`EXCHANGES` is used to filter Finnhub's profile `exchange` field (it returns full
names like `"NASDAQ NMS - GLOBAL MARKET"` and `"NEW YORK STOCK EXCHANGE, INC."`).
The date window uses `Asia/Dubai` via `zoneinfo`, matching the 08:00 UAE
schedule year-round. Finnhub's `hour` value supplies the US session bucket.

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

1. **Select the mode + window.** The CLI accepts `weekly` or `daily`:
   - `weekly`: compute the upcoming/current Monday–Friday. On Sunday, roll
     forward to Monday so the Sunday email previews the coming trading week.
   - `daily`: display today and tomorrow in `Asia/Dubai`, but **fetch** from the
     prior trading day through tomorrow (Monday's prior day is Friday).
2. **Fetch earnings** for the fetch window (6a). Log the raw count. Keep
   Finnhub estimate/actual fields: `epsEstimate`, `epsActual`,
   `revenueEstimate`, `revenueActual`.
3. **Collect in-window events + unique tickers.** For each earnings row: skip if
   no `symbol`/`date`; parse `date` and skip if outside the fetch window;
   remember symbol/date/hour plus estimate/actual fields and the unique symbols.
4. **Build the market-cap universe** (6b). For each unique ticker (paced by
   `RATE_LIMIT_SLEEP`), fetch its profile; keep it if `cap ≥ MIN_MARKET_CAP` and
   its exchange matches `EXCHANGES`. Log how many tickers were looked up and how
   many qualified.
5. **Filter + bucket.** For each in-window event:
   - Skip if `symbol` not in `universe` (below the cap or wrong exchange).
   - Map `hour` → bucket via the table below.
   - Append to `events_by_day[date][bucket]` with
     `{symbol, name, cap, dt, bucket, eps_estimate, eps_actual,
     revenue_estimate, revenue_actual}`.
   - Increment a running `count`.
6. **Sort** each bucket's list by `cap` descending (biggest company first).
7. **Build the HTML email** (section 8).
8. **Send** via Gmail SMTP (section 9).
9. **Log** the final qualifying count and "Email sent."

### Timing map (`hour` field → bucket)
| Raw `hour` value                         | Bucket |
|------------------------------------------|--------|
| `bmo`, `before market open`              | bmo    |
| `amc`, `after market close`              | amc    |
| `dmh`, `during market hours`             | dmh    |
| clock string, hour ≥ 16                  | amc    |
| clock string, hour < 16                  | bmo    |
| empty / anything else                    | tbd    |

---

## 8. The Todoist quick-add link (most important detail)

Each stock row contains a normal HTTPS link to the static GitHub Pages redirect.
Gmail Android permits HTTPS links but strips `todoist://` links. The redirect
detects the platform and opens Todoist's add-task panel **pre-filled but not
submitted** — the user taps Todoist's own add button to confirm.

**Email link format:**
```
https://anisjnasr.github.io/earnings_email/add.html
    ?title=<ENCODED TITLE>&date=<ENCODED DATE>&project=<ENCODED PROJECT>
```

**Task title (before encoding):**
```
{SYMBOL} Earnings[ - {SESSION}]
```
- Example title: `NVDA Earnings - AMC`
- Example date: `Jul 24 2026`
- Example project: `Earnings`
- `date` is always the company's **earnings date**, not the day the link is
  tapped. It has no time, producing an all-day due date.
- `SESSION` is the uppercased bucket (`BMO`/`AMC`/`DMH`) appended to the task
  **name**; omit the ` - {SESSION}` segment for the `tbd` bucket.
- Note the capital **E** in `Earnings`.
- URL-encode `title`, `date`, and `project` separately with
  `urllib.parse.quote(value, safe="")`.

**Redirect behavior (`docs/add.html`):**
- Android: open an Android `intent://` targeting package `com.todoist`, which
  resolves to `todoist://addtask?content=...&date=...&priority=1`.
- iOS: open `todoist://addtask?content=...&date=...&priority=1`.
- Desktop: open
  `todoist://openquickadd?content={TITLE} {DATE} p1 #{PROJECT_NAME}`.
- Mobile `priority=1` and desktop `p1` both produce client-facing **Priority 1**.
- If automatic external navigation is blocked by an in-app browser, show a
  visible "Open in Todoist" button as a user-initiated fallback.

No Todoist token, no API call, no network request from the script for this step.

---

## 9. Email delivery — Gmail SMTP
- Build a `MIMEMultipart("alternative")` with a short plain-text part and the
  HTML part.
- Weekly subject:
  `Earnings this week ({count}) — {start Mon} {D} - {end Mon} {D}`.
- Daily subject:
  `Upcoming Earnings ({count}) — {start Mon} {D} - {end Mon} {D}`.
- Send with `smtplib.SMTP_SSL("smtp.gmail.com", 465)` using an
  `ssl.create_default_context()`, then `login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)`
  and `sendmail(GMAIL_ADDRESS, [RECIPIENT], msg.as_string())`.

---

## 10. Email layout spec (HTML, mobile-first)
- Single-column, `max-width:640px`, centered, system font stack, light-grey page
  background (`#f5f6f7`), white content area.
- **Weekly header:** one bold line:
  `Earnings this week - {start Mon} {D} – {end Mon} {D}`.
- **Daily header:** one bold line:
  `Upcoming Earnings - {start Mon} {D} – {end Mon} {D}`.
- Do not include the qualifying count, cutoff, or Todoist hint below the header.
- **Per day** (only days that have events), in date order:
  - Each day is a collapsible `<details open>` block; the day heading is the
    `<summary>` (e.g. "Thursday, Jul 24  (12)" with the qualifying count) with an
    accent-colour bottom border. Native tap-to-collapse works in clients that
    support `<details>` (Apple Mail, Outlook for Mac, …); Gmail ignores it and
    shows the day expanded. Default `open` keeps every client readable.
  - Then, in this order, only for non-empty buckets:
    `Before open (BMO)`, `After close (AMC)`, `During hours`, `Time not confirmed`.
  - Each bucket is another `<details open>` block, indented 16px beneath its
    date. Its `<summary>` matches the date-header design at a smaller size, has
    an accent bottom border and event count, and collapses/expands its rows.
    The table is inside the same indented block so it aligns with its bucket.
- **Each row** = a table row with ticker on the left, metrics to its right on the
  same line (`white-space:nowrap`), then market cap, then the `+` button:
  1. Ticker (bold) with truncated company name beneath it.
  2. Metrics (right of ticker):
     - Reported: `Sales E/A $est / $act +x% - EPS E/A $est / $act +x%`
     - Not yet reported: `Sales E $est - EPS E $est`
     - Surprise % = `(Actual ÷ Est) − 1`; beat green (`#0F7B3A`), miss red
       (`#B42318`). Omit a metric if Finnhub has no value for it.
  3. Market cap, right-aligned (`$X.XB`, or `$X.XXT` at/above a trillion).
  4. A compact **`+` icon** button (no text): 40×40 accent square, white plus,
     centered with an email-safe presentation table, `href` = §8.
- Daily emails mark the prior trading day with a small grey **Prior day** badge.
- Use **inline CSS only** (email clients strip `<style>` blocks unreliably).
- Footer: tiny grey line crediting Finnhub and noting times are the US session.
- If there are zero qualifying companies, show a friendly
  "No qualifying earnings found." message instead of empty tables.

---

## 11. GitHub Actions workflow (`.github/workflows/earnings.yml`)
- Scheduled triggers (UAE is UTC+4 with no DST):
  - `0 4 * * 0`: weekly email, Sunday at 08:00 UAE.
  - `0 4 * * 1-5`: daily email, Monday–Friday at 08:00 UAE.
- `workflow_dispatch` offers `both`, `weekly`, and `daily`; `both` sends the two
  emails sequentially to stay within Finnhub's account-level rate limit.
- Job on `ubuntu-latest`:
  1. `actions/checkout@v4`
  2. `actions/setup-python@v5` with `python-version: "3.12"`
  3. `pip install tzdata` (ensures `zoneinfo` has the timezone database)
  4. Resolve the scheduled/manual digest mode and run
     `python earnings_email.py weekly`, `python earnings_email.py daily`, or both,
     with `FINNHUB_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, and
     `RECIPIENT` passed from `secrets` as `env`.

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
1. Manually triggering `both` completes green and logs two modes/windows, two
   sets of counts, and two "Email sent." lines.
2. Two emails arrive at `RECIPIENT`: one weekly and one daily.
3. The weekly email shows the upcoming/current Mon–Fri range; the daily email
   header shows today–tomorrow and includes a prior-trading-day section (Monday
   → Friday) with estimate/actual metrics. Both contain only companies ≥ $1B.
4. Each day is split into BMO/AMC; rows are sorted biggest-cap first; reported
   rows show Sales/EPS E/A with colored surprise %.
5. On a phone with the Todoist app installed, tapping **+** opens Todoist Quick
   Add with `{SYMBOL} Earnings - {SESSION}`, the correct all-day date, Priority 1,
   and the `Earnings` project pre-filled.
6. Changing `MIN_MARKET_CAP` and re-running visibly changes the list length.

---

## 14. Known gotchas (call these out to Cursor)
- **Do not** use the Todoist REST/Sync API or any `Bearer` token for adding
  tasks — the whole design relies on Todoist's `todoist://` URL scheme.
- Never put `todoist://` directly in the email: Gmail Android strips custom
  schemes. Link to the HTTPS GitHub Pages redirect instead.
- The redirect must use `todoist://addtask`/Android intent on mobile and
  `todoist://openquickadd` on desktop; these endpoints are platform-specific.
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
