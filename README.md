# LeadSearch — Beyond Border lead automation

LeadSearch takes Beyond Border's inbound visa leads (O‑1, EB‑1, EB‑2 …) from the
Meta, Website and WhatsApp forms, checks who each person really is, judges
their fit, gives each accepted lead to an Account Executive (AE), and writes it
to the Pipedrive sheet and Pipedrive itself. People only step in for the leads
it isn't sure about.

```
Source sheet ──pull every 10 min──▶ verify person (LLM + web/LinkedIn search)
   ──▶ fit screening (NURTURE / REVIEW / NO-GO) ──▶ accept · review · reject
   ──▶ assign AE ──▶ push to "Master Leads Sheet Cleaned" ──▶ Pipedrive deals (05:00 & 20:00 IST)
                                                         └──▶ GTM report (MQL Tracker)
```

Repo: `github.com/Beyond-Border-Global/lead-automation` · hosted on Render (company account).

## How a lead is handled

1. **Pull** — new rows are read from the source sheet (all tabs; each tab is
   column-mapped on its own) and de-duplicated against everything already seen.
2. **Verify** — an LLM cleans the lead and writes search queries; Tavily
   searches the web. Page content comes from Tavily when it's good enough,
   otherwise Firecrawl (PDFs, heavy-JS pages) or Apify (LinkedIn). A typed-in
   LinkedIn URL is opened and checked against what the lead filled in.
3. **Score** — the LLM scores the best profile match:
   `≥ ACCEPT_THRESHOLD` (0.75) accepted · `≥ REVIEW_THRESHOLD` (0.45) review ·
   not finding a profile is never a reason to reject.
4. **Screen** — the ForRAG screening service judges fit from the LinkedIn
   profile: NURTURE, REVIEW or NO-GO.
5. **Review agent** — an LLM suggests accept / reject for leads in review,
   following [`app/review_rules.md`](app/review_rules.md) (edit it to change
   the rules; it's re-read every cycle). Only spam, scams, test entries and
   NO-GO are rejected without a person; everything else waits on the
   **Review** page with the agent's suggestion.
6. **Assign an AE** — on acceptance only, see [AE distribution](#ae-distribution).
7. **Push** — accepted leads go to the Pipedrive sheet; clean values are
   written back to the exact source row and marked *In Progress*.
8. **Pipedrive** — at `AUTO_PIPE_TIMES` (05:00 and 20:00 IST) new deals
   `<Visa>_<Name>` are created in *Consulting and agencies → New Lead*, owned
   by the AE. A lead whose person already has a deal is linked to it, not
   duplicated.
9. **GTM report** — each pushed lead is counted in the weekly *MQL Tracker*
   (GTM_Funnels sheet).

## AE distribution

Rules, in priority order ([`app/pipeline/distribution.py`](app/pipeline/distribution.py)):

| Lead | Goes to | Setting |
|---|---|---|
| LATAM (phone code / country) | Agustina, Rocio, Juline | `AE_LATAM` |
| Pilot (by profession) | Rahul | `AE_PILOT` |
| Everyone else | Vansh, Rahul, Agustina, Rocio, Juline | `AE_ROUND_ROBIN` |
| Already has a Pipedrive deal | that deal's AE (uses no turn) | — |

All three groups share one rotation, so totals stay level. Untick an AE under
**Team on duty** (sidebar) while they're on leave. A lead's AE can be changed
from the Leads table. Each lead records when its AE was given it
(`assigned_at`).

## The Pipedrive sheet's tabs

| Tab | What's in it |
|---|---|
| **Master leads** | Accepted leads (Pipedrive reads this tab) |
| **Review** | Leads waiting for a person. Pick **Approve** or **Reject** in the *Decision* column; the app applies it within 10 min (even with automation paused), and the lead moves to Master leads / Rejected. The dashboard's Review buttons do the same. |
| **Rejected** | Every rejected lead, newest first, with the reason |

Review and Rejected are rewritten by the app — don't edit them by hand except
the Decision column. They show the channel in a "Channel" column and never fill
"Source", so they can never turn into Pipedrive deals.

## The dashboard

| Page | What it's for |
|---|---|
| **Dashboard** | Totals, charts per period (channel, outcomes, fit verdicts, AE workload, leads over time) and the automation controls |
| **Pipeline** | Upload a CSV, run the pipeline by hand |
| **Leads** | Every lead, filterable by status, channel and date |
| **Review** | Leads waiting on a person, with the review agent's suggestion |
| **Duplicates** | Possible duplicates (e.g. same name, different email) to merge or keep |
| **Pipedrive** | Preview and write sheet rows to Pipedrive ("Write on Pipe") |
| **LinkedIn check** | Re-checks the LinkedIn links in the Pipedrive sheet |
| **GTM report** | Weekly MQL counts, with a recount |
| **Services** | Credits and API health for every outside service; alerts go to Google Chat |
| **Written on Pipedrive** | History of what was written to Pipedrive |

- Dashboard tiles open the Leads list filtered to that status.
- **AE Workload bars are clickable:** a bar opens that AE's accepted leads
  assigned in the chart's period — the same leads the bar counts — with email,
  phone, LinkedIn and a link to the Pipedrive deal. The link
  (`#leads/accepted?ae=<AE>&from=<date>&to=<date>`) can be bookmarked or shared.

**Automation controls** (dashboard toolbar): ▶ **Pull**, ▶ **Push** and
▶ **Pipe** each play/pause on their own. Paused means nothing new comes in or
goes out — check the status line on the dashboard.

## Run it locally

Python 3.12 (as on Render; the pinned packages don't build on much newer Pythons).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in the keys
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000. Keep `SCHEDULER_ENABLED=false` and
`PIPEDRIVE_WRITE=false` locally so a local copy never writes to the real
sheets or Pipedrive. The local database lives in `data/` (override with `DATA_DIR`).

## Configuration

All settings are environment variables — `.env` locally, the Render dashboard
in production. [`.env.example`](.env.example) lists them with comments. The main ones:

| Area | Variables |
|---|---|
| LLM (OpenRouter) | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` (matching), `OPENROUTER_MODEL_FAST` (simple jobs) |
| Search & fetch | `TAVILY_API_KEY`, `FIRECRAWL_API_KEY` |
| LinkedIn (Apify) | `APIFY_API_TOKEN`, `APIFY_LINKEDIN_ACTOR`, `APIFY_LINKEDIN_SEARCH_ACTOR`, `APIFY_LINKEDIN_SEARCH_MODE` |
| Fit screening | `SCREENING_URL`, `SCREENING_API_KEY`, `SCREENING_ENABLED` |
| Google Sheets | `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_SOURCE_SHEET_ID`/`_TAB`, `GOOGLE_DEST_SHEET_ID`/`_TAB`, `GTM_SHEET_ID`, `GTM_TAB` |
| Pipedrive | `PIPEDRIVE_API_TOKEN`, `PIPEDRIVE_DOMAIN` (`bbg`), `PIPEDRIVE_PIPELINE`, `PIPEDRIVE_STAGE`, `PIPEDRIVE_WRITE` |
| Write hold | `HOLD_GTM_PIPEDRIVE` — **true by default**: nothing is written to Pipedrive or the GTM sheet until it is set to `false` |
| AEs | `AE_ROUND_ROBIN`, `AE_LATAM`, `AE_PILOT`, `AE_DIRECTORY` (name → Pipedrive user id) |
| Automation | `SCHEDULER_ENABLED`, `SCHEDULER_INTERVAL_SECONDS` (600), `SCHEDULER_MAX_PER_CYCLE` (50), `AUTO_PIPE_TIMES`, `AUTO_PIPE_TZ` |
| Tuning | `ACCEPT_THRESHOLD`, `REVIEW_THRESHOLD`, `MAX_RESULTS_PER_LEAD`, `MAX_CANDIDATES_MATCHED`, `MIN_CONTENT_CHARS` |
| Alerts & status | `GCHAT_WEBHOOK_URL` (Google Chat), `RENDER_API_KEY` (deploy status) |

**Google Sheets access** uses a service account: share the source sheet with
its email as *Viewer* and the destination and GTM sheets as *Editor*.

## Deploy (Render)

[`render.yaml`](render.yaml) defines the service: a *Starter* web service (always
on, needed for the automation) with a 1 GB persistent disk at `/var/data`
holding the SQLite database, so leads and history survive deploys.

- Deploy from `main` (Render dashboard → *Manual Deploy* if auto-deploy is
  off). Check **Services → Render server** for the running commit.
- Secrets are entered in the Render dashboard, never committed.
- Static files are cache-busted by the `?v=` numbers in
  [`app/static/index.html`](app/static/index.html) — bump them when you
  change `app.js` or `styles.css`.

> ⚠️ **There is no login.** Anyone with the app's URL can see leads' contact
> details and run Pull / Push / Pipe. Keep the URL inside the team until auth
> is added.

## Code map

| Path | What's in it |
|---|---|
| `app/main.py` | FastAPI app: API routes, scheduler, sheet pull/push, column mapping |
| `app/db.py` | SQLite schema (auto-migrates new columns on start), dashboard analytics |
| `app/pipeline/` | Per-lead steps: `orchestrator`, `llm`, `search`, `fetch`, `linkedin`, `screening`, `decision`, `distribution` |
| `app/pipedrive.py` | Sheet → Pipedrive sync and its history |
| `app/gtm.py` | GTM / MQL Tracker counts |
| `app/review_agent.py`, `app/review_rules.md` | Review agent and its rules |
| `app/dedupe.py` | Duplicate detection and merging |
| `app/health.py`, `app/server_status.py`, `app/notify.py` | Services page checks and Google Chat alerts |
| `app/static/` | Dashboard (plain HTML/JS + Chart.js) |
