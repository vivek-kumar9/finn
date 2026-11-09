# FINN

A phone-first investment agent for an Indian equity portfolio. It tells you what changed across your holdings, ranked by how much it matters to *your* book, and turns any company into a research page built only from that company's own filings and earnings calls.

**Live app:** https://finn-pi-six.vercel.app

**API health:** https://finn-api-enea.onrender.com/health

## What it does

**Push: the Brief.** An inbox you clear, not a dashboard you read.

- Filings for the portfolio are grouped into *stories*. One results event is five to seven filings (board-meeting notice, outcome, results PDF, deck, newspaper ad, call notice, transcript), but it is one card.
- Stories are ranked by event type × position size. A results story on a 10% holding outranks a dividend on a 3% one, and a story gets a boost if the stock also moved that day. The formula is in `pipeline/stories.py` and `web/lib/brief.ts`.
- Each category gets its own treatment rather than a generic summary. Results get a Revenue / EBITDA / PBT / PAT grid with QoQ, YoY and margin change, read from the filing. Leadership changes get who joins, who leaves, and a short background of the incoming person (from the filing's own profile first, web search only if it has none). Deals, fund raises, pledges, credit ratings and dividends get the one or two facts that matter (deal size, dilution, pledge before → after, yield). Routine paperwork collapses into one line.
- **Moves**: end-of-day price and volume alerts in three modes the user picks: a fixed %, the move relative to Nifty, or the move relative to the stock's own 20-day volatility (a 3% day is noise for some stocks and news for others). Every alert shows the filing that likely explains it, or says that none was found.
- Windows: since your last visit, 24 hours, 3 days, 7 days, or any past day. Windows count back from the newest data, so the Brief never opens empty when markets are shut.
- **Telegram**: the same rules, delivered as messages after each hourly ingest. The app's *Alert rules* sheet links to the bot with your thresholds already set.

**Pull: research on any NSE company.**

- **Promised vs delivered** over the last four earnings calls. A model lists management's quantified, forward-looking statements with a verbatim quote and page. Code then checks every quote against the transcript's own text and drops the ones it can't find. Code, not the model, judges each promise against reported numbers: *met* or *missed* when the period is over, *on track* or *off track* year-to-date, *not checkable* for segment-level or non-financial promises. Guidance raised or cut between calls is tracked.
- The business, from targeted pages of the latest annual report.
- Trajectory, balance sheet and cash quality, computed from screener.in's multi-year tables (which are compiled from filings).
- Bull and bear: three points each, written only from facts the code has already verified, each showing what it rests on.
- A cost line on every run: model calls, tokens, cache hits, rupees.

Sources for research are primary only: transcripts, annual reports, and standardised financials. No news, no broker notes, no model background knowledge.

## Architecture

```
NSE announcements ─┐                         ┌─ FINN PWA (Next.js, Vercel)
yfinance EOD ──────┼─ ingest, hourly ───► Supabase Postgres ◄──┤   reads the Brief directly
                   │  (GitHub Actions          (stories,        │
                   │   calls Render)            prices, cache)  └─ research ─► FastAPI (Render)
                   └─ Telegram alerts                                          screener.in, transcripts,
                                                                               annual reports
```

- The Brief reads Supabase straight from the browser with a read-only key (row-level security), so the first screen never waits on a sleeping free-tier server.
- The API only does slow work: research, streamed section by section over SSE, and the Telegram webhook. It binds its port before importing the pipeline, so cold starts stay short.
- GitHub Actions runs the hourly schedule and simply calls the API; all fetching happens on Render, which the source probe (`pipeline/probe.py`) showed can reach NSE and screener. BSE blocks both cloud and home IPs, so it is not used.

## Models: who does what

Models read; code decides. Every number shown is computed in Python from a primary source.

| Job | Model | Why |
| --- | --- | --- |
| Filing category | Rules first (92% of filings), Groq `gpt-oss-20b` for the rest | Most NSE subjects are formulaic; a label is an easy task |
| Results table → 4 metrics × 3 periods | Gemini 3.5 Flash, then other full Flash models | Table reading needs the stronger model; Lite misread tables in testing, so it is never used here |
| Page finding for results | None: pypdf scores pages on line items | Large filings put the table on page 12+; only those pages are sent |
| Leadership, other filings, bios | Gemini Flash-Lite | Short, factual extraction |
| Guidance from transcripts | Full Flash, Lite as fallback | Prose, and every quote is verified afterwards |
| Promise verdicts, QoQ/YoY, margins, ratios | **Python** | Arithmetic doesn't belong in a prompt |
| Bull and bear | Full Flash, Groq `gpt-oss-120b` as fallback | The one judgment call, fed only verified facts |

Cost controls: every model output is cached by document URL and prompt version, so a repeat view costs nothing; free-tier daily quotas are per model, so calls move down a chain of models when one runs out, and stories wait for the next hourly run rather than showing a half-read table.

<!-- numbers from the live system: fill in from the evening run -->

## Judgment calls

- **EBITDA is derived** (profit before exceptional items and tax + finance costs + depreciation − other income) because most results filings don't print it, and it is marked *d* on the card. Banks get net interest income and operating profit instead.
- **A half-read table is rejected**, not shown: a results card needs PAT and at least one comparison column.
- **Segment guidance is never judged against company totals.** An early version marked "smart metering revenue ₹700–800 cr" as met against total revenue; promises now carry a scope, and a test pins that case.
- **Small penalties and rating reaffirmations are routine.** A ₹1 lakh telecom fine on an ₹8 lakh crore company is not news.
- **The Brief anchors to the newest data**, with a "data as of" stamp, so it stays honest when ingest is paused or markets are closed.

## Left out, on purpose

User accounts and per-user portfolios (one demo book with made-up weights); intraday data; native app builds (it is an installable PWA); insider and pledge data from the separate SAST/PIT feeds (only what arrives as announcements); tone analysis of calls and related-party/contingent-liability digging; chat as the main interface.

## Running it

```bash
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env                      # Supabase, Gemini, Groq, Telegram keys
# run db/schema.sql once in the Supabase SQL editor
python -m pipeline.ingest --days 30       # backfill
python -m pipeline.cards --limit 60       # build cards
python -m pipeline.research KAYNES        # one research report, printed section by section
python -m pytest -q                       # tests, including two real screener pages
cd web && npm install && npm run dev      # app on localhost:3000
```
