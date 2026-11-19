# Job Hunt Agents

Four Claude Haiku 5.5 agents that run forever, talk to each other through `main.md`, and fill `jobs.xlsx`.

```
                ┌──────────── orchestrator.py (only writer, runs a round every 60 min) ────────────┐
 profile.md ──▶ │  reads main.md tail + stats + each sheet's existing rows                          │
                │                                                                                   │
                │   scout ─┐   bigtech ─┐   market ─┐   coach ─┐     (parallel `claude -p`,         │
                │          │            │           │          │      Haiku, WebSearch/WebFetch     │
                │          ▼            ▼           ▼          ▼      only, JSON-schema output)     │
                │   board_post + rows  (validated, deduped)                                         │
                └─────┬───────────────────────────────────────────────┬─────────────────────────────┘
                      ▼                                               ▼
        main.md  (append "### [agent]" posts)            jobs.xlsx (one sheet per agent)
```

## The agents

| Agent | Owns sheet | Job |
|---|---|---|
| `scout` | Jobs 60+ LPA | Open roles paying > ₹60 LPA you're eligible for (remote from India, or Noida/Delhi/Gurugram/Bengaluru), scored for reach (1-5) |
| `bigtech` | Big Tech Targets | Big-tech roles + ladder level to target now for ~1.5 YOE, with open postings and prep focus |
| `market` | Companies & Sources | Which companies pay ₹60 LPA+ remote to India, boards, search queries, communities, pay facts |
| `gems` | Hidden Gems | Lesser-known but good companies (funded, well-paying, hiring where you can work) plus their ATS board ID |
| `coach` | Strategy Log | How the search is going (from stats + board) and what to change: team process, resume, gaps, outreach |

**Independent by design:** each agent writes only its own sheet, never waits on another, and runs fully
even with an empty board. They cooperate only by reading the board: `@asks`, `replies`, `findings`,
`next_focus`. Agents can't write files at all (tools are limited to WebSearch/WebFetch); the orchestrator
is the single writer, so there are no write races. Definitions live in `agents/*.md`, shared rules in
`agents/_protocol.md`.

## Free job APIs (`feeds.py`, no keys)

Every round, before the agents start, `feeds.py` pulls live openings and keeps only eligible engineering roles.
They go to the **API Leads** sheet and are handed to scout, bigtech and gems as leads.

| Source | What it gives |
|---|---|
| Company job boards: Greenhouse, Lever, Ashby, Workable, SmartRecruiters | Every open job at each watched company, many with pay. Watchlist = 20 big-tech boards (Stripe, Databricks, Cloudflare, Datadog, MongoDB, Coinbase, OpenAI, Snowflake...) + 20 seed companies + every Hidden Gems row with an ATS token + boards auto-discovered for hiring YC companies (`ats_cache.json`, 30 new companies probed per round, misses rechecked after 30 days) |
| Himalayas search (country=India) | Remote jobs open to India, some with INR pay |
| We Work Remotely RSS, RemoteOK, Remotive | Remote jobs with region (credit them as the source; Remotive is 24h delayed) |
| HN "Who is hiring" via Algolia | This month's thread, remote/India posts |
| yc-oss hiring list | YC companies hiring in India/remote: seeds board discovery and ideas for gems |

Not pulled (no free public API, or terms forbid automated access): HiringCafe, Wellfound, Welcome to the
Jungle, LinkedIn, Indeed, Glassdoor, ZipRecruiter. `market` gives you exact search settings for the useful
ones (HiringCafe, Wellfound, YC jobs) instead. Agents use web search only to verify pay/years/open status.

Each row in **API Leads** has a `tier`: `bigtech` leads go to bigtech, the rest to scout and gems.

A once-a-day run makes roughly 200 requests spread over ~70 sources (about 1-3 each), far inside every limit. A source that fails is skipped.

## Run it

```bash
python3 -m pip install --user openpyxl
```

One round to try it (takes a few minutes):

```bash
python3 orchestrator.py --once
```

**Current setup: once a day.** A scheduled prompt in the Claude Code chat runs `--once` each morning and
posts the new `digests/round-NNNN.md` summary into that chat. It lives only while that chat session is
open and expires after 7 days, so ask Claude to renew it.

Alternative, always on without the chat (launchd, restarts on crash and at login):

```bash
./service.sh install
```

Other commands: `./service.sh status`, `./service.sh logs`, `./service.sh uninstall`.
Change the pace with `JOBHUNT_INTERVAL_MIN=30 ./service.sh install`.

Auth uses your existing `claude` login; no API key needed. Hooks are disabled for agent runs.

## Talk to the agents

Append to the end of `main.md` between rounds:

```
### [user]
@scout skip crypto. @bigtech focus on Bengaluru infra teams. @coach I applied to Acme and Foo.
```

`[user]` entries override everything. Start with `cp profile.example.md profile.md`, then fill it in and update it when your situation changes.
In `jobs.xlsx`, edit the `status` column (New / Applied / Interviewing / Rejected / Skip); the coach sees those counts.

## Files

- `main.md` - the board. Rotated into `archive/` past 400 KB, keeping the latest rounds.
- `jobs.xlsx` - results. Close it in Excel when a round is saving, or Excel will overwrite the new rows when you save.
- `runs/round-NNNN/<agent>.json` - raw output of every agent run (audit trail).
- `state.json` - round counter and lifetime cost. `logs/` - orchestrator logs.

## Cost

Each agent run is capped at $0.75 (`JOBHUNT_BUDGET_USD`). Typical round cost is logged per post and summed
in `state.json`. At the default 60-minute interval that's 96 agent runs a day; raise the interval if you
want it cheaper.
