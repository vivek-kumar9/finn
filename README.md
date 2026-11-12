# FINN

FINN is a phone-first investment agent for an Indian equity portfolio. It shows what changed across your holdings, ranked by how much it matters to your portfolio, and it can build a research page on any NSE company using only that company's own filings and earnings calls.

I built it for the ENAM AI Engineer challenge (IIT Delhi, 2027).

- App (open it on a phone): https://finn-pi-six.vercel.app
- API health check: https://finn-api-enea.onrender.com/health
- Telegram bot: https://t.me/finn_enam_portfolio_bot

## What it does

### Push: the Brief

The Brief works like an inbox. You go through what changed and clear it.

- Filings for the portfolio are grouped into stories. One results event comes as 5-7 filings (outcome, results PDF, press release, deck, newspaper ad, call notice, transcript), but it shows up as one card.
- Stories are ranked by event type and position size, so results on a 10% holding come above a dividend on a 3% holding. A story also moves up if it explains one of the price or volume moves. The scoring is in `pipeline/stories.py` and `web/lib/brief.ts`.
- Each type of event gets its own card. Results show Revenue, EBITDA, PBT and PAT with QoQ, YoY and the margin change, read from the filing. Leadership changes show who joins, who leaves, and a short background of the new person (from the filing if it has a profile, otherwise from a web search). Deals, fund raises, pledges, credit ratings and dividends show the one or two numbers that matter. Routine filings (board meeting notices, compliance paperwork, investor meeting schedules) are folded into one line each.
- Moves: end-of-day price and volume alerts. You pick how a price move counts: a fixed %, the move against Nifty, or the move compared with the stock's own usual daily move over 20 days (a 3% day is noise for some stocks and big news for others). Each alert names the filing that most likely explains it, which means a material filing made between the previous close and that day's close, or says there wasn't one.
- Time windows: since your last visit, 24 hours, 3 days, 7 days, or any single day. They count back from the newest data, so the Brief isn't empty on a weekend or a market holiday.
- Telegram: the same rules, sent as messages after every hourly run. The Alert rules sheet in the app links to the bot with your settings already filled in.

### Pull: research on any NSE company

- Promised vs delivered for the last four earnings calls. A model lists what management promised with a number attached, along with the exact quote and page. Every quote is then looked up in the transcript's own text, and any that can't be found are dropped. Python, not the model, checks each promise against the reported numbers: met or missed if the period is over, on track or off track if it's still going, and "not checkable" for promises about one segment or without a financial number. Guidance that was raised or cut between calls is shown too.
- The business, from the relevant pages of the latest annual report.
- Trajectory, balance sheet and cash quality, from screener.in's multi-year tables (which are put together from filings).
- Bull and bear: three points each, written only from facts the code has already checked, and each point shows which facts it's based on.
- A cost line at the end of every run: model calls, tokens, cache hits and rupees.

Research only uses primary sources: transcripts, annual reports and standard financial tables. No news, no broker notes and nothing from the model's own memory.

## How it's put together

- **Ingest** (Python, runs on Render). Every hour on market days, GitHub Actions calls the API. The API pulls end-of-day prices from Yahoo and new filings from NSE, sorts the filings into categories and stories, builds the cards and sends the Telegram alerts. Everything goes into Supabase (Postgres).
- **App** (Next.js, installable as a PWA, on Vercel). The Brief reads Supabase straight from the browser with a read-only key (row-level security), so the first screen never waits for the free server to wake up.
- **API** (FastAPI on Render). Only the slow work: research, streamed section by section, the Telegram webhook and the hourly ingest trigger. It starts up before importing the pipeline, so cold starts stay short, and it skips an ingest if the previous one is still running.

All fetching happens on Render because the source probe (`pipeline/probe.py`) showed it can reach NSE and screener. BSE blocked both the cloud server and my laptop, so it isn't used.

## Which model does what

The rule I followed: models read, code does the maths. Every number on screen is calculated in Python from a primary source.

| Job | Model | Why |
| --- | --- | --- |
| Filing category | Regex rules first (92% of filings), then Groq `gpt-oss-20b` | NSE subjects are formulaic, and a label is an easy job |
| Finding the results table | No model. pypdf scores the company's PDFs, then their pages, on statement line items | The story's lead is often the press release or the deck, and big filings have the table on page 12 or later. Only the table pages are sent |
| Reading the results table | Gemini 3.5 Flash, then the other full Flash models, then Lite, then Groq on the page text | Every reading has to match screener's quarterly table, so a weaker model's reading is only kept if it does |
| Leadership, other filings, bios | Gemini Flash-Lite | Short, factual extraction |
| Guidance from transcripts | Full Flash, Lite if needed | Long text, and every quote is checked afterwards |
| Promise verdicts, QoQ/YoY, margins, ratios | Python | Maths doesn't belong in a prompt |
| Bull and bear | Full Flash, Groq `gpt-oss-120b` if needed | The one judgment call, and it only gets checked facts |

To keep the cost at ₹0, every model output is cached by document URL and prompt version, so opening the same thing again costs nothing. Free-tier quotas are per model per day, so calls move down a chain of models when one runs out. A card that can't be read yet waits for the next hourly run instead of showing half a table.

## Numbers

From the live system on 11 November 2026 (`python scripts/stats.py`):

- 259 NSE filings across the 18 holdings over 30 days. 238 (92%) were classified by rules and 21 needed a model.
- 135 stories. 65 got a card, and 69 routine ones were folded away without any model call.
- 15 of 16 results cards match screener's quarterly table on revenue and PAT. Eternal's links to the filing instead, because none of its PDFs around the results date had a statement table the models could read.
- 31 earnings-call transcripts read. 67 guidance quotes kept and 3 (4%) dropped because the words weren't in the transcript.
- 288 model calls and 1.23M tokens for all of this: 56% on results tables (mostly re-reading the hard ones while I was tightening the checks) and 32% on transcripts. Cost: ₹0 on free tiers.
- Opening research that has already been run: 0 model calls, about 5 seconds.

## What went wrong, and what I changed

- **The screener check caught real mistakes.** A model read CDSL's total income (₹417 cr) as revenue (₹394 cr), another read Persistent's table in ₹ million as crore, and Titan's standalone table came back labelled as consolidated. Each of these is now fixed from the filing's own lines (other income taken out, the unit corrected, the label switched), and the fixed reading still has to match screener on both revenue and PAT. The card says what was fixed.
- **Four results cards kept failing with every model, full Flash included.** A debug run showed the models weren't the problem: the story was handing them the press release or the investor deck instead of the results statement. Now the code scores every PDF the company filed around the results date on statement line items and reads the best one. That took results from 11 to 15 of 16 with no extra model calls.
- **Segment guidance was being judged against company totals.** My first version marked Kaynes' smart-metering guidance as "met" against the whole company's revenue. Promises now have a scope, segment ones are never checked against company totals, and a test covers that case.
- **"Stock up X% that day" was quoting the wrong day.** The line on a card used to take the stock's biggest move anywhere in the window, so a results card could quote a move from before the results came out. Now it only uses the move that the story explains.
- **The free tier shaped a lot of this.** Gemini gives roughly 20 requests per model per day, and on the build days every full Flash model was used up or overloaded by the morning. So 18 of the 31 transcripts were read by Flash-Lite through the fallback chain.

## Decisions

- **EBITDA is worked out** (profit before exceptional items and tax, plus finance costs and depreciation, minus other income), because most results filings don't print it. It's marked "d" on the card. Banks get net interest income and operating profit instead.
- **A half-read table isn't shown.** A results card needs revenue, PAT, PBT and at least one comparison column, or it links to the filing. If one line can't be read, the card can use screener's number for it, but only after revenue and PAT matched, and that line is marked "s".
- **Small penalties and rating reaffirmations count as routine.** A ₹1 lakh fine on an ₹8 lakh crore telecom company isn't news, and it isn't offered as the reason a stock moved.
- **Board meeting notices are routine.** "Meeting on 6 Nov to consider results" is a date, not results, so a results story starts when the numbers come out.
- **The Brief counts back from the newest data** and shows a "data as of" time, so it stays honest when the ingest is paused or the market is shut.

## What I left out

- Logins and per-user portfolios. There's one demo portfolio with made-up weights.
- Intraday data.
- Native apps. It's an installable web app instead.
- Insider and pledge data from the separate SAST and PIT feeds. Only what comes in as an announcement is covered.
- Tone analysis of calls, and digging into related-party transactions and contingent liabilities.
- Chat as the main screen.

## Running it

```bash
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env                      # Supabase, Gemini, Groq, Telegram and Tavily keys
# run db/schema.sql once in the Supabase SQL editor
python -m pipeline.ingest --days 30       # backfill
python -m pipeline.cards --limit 60       # build cards
python -m pipeline.research KAYNES        # one research run, printed section by section
python scripts/stats.py                   # counts, plus whether the hourly ingest is healthy
pip install -r requirements-dev.txt       # pytest, reportlab
python -m pytest -q                       # tests, including two real screener pages
cd web && npm install && npm run dev      # app on localhost:3000
```
