#!/usr/bin/env python3
"""Always-on multi-agent job search.

Every round, four Haiku agents (scout, bigtech, market, coach) run in parallel as headless
`claude -p` sessions with only WebSearch/WebFetch. Each reads the shared board (main.md),
the candidate profile and search stats, then returns structured JSON. This orchestrator is the
only writer: it appends each agent's post to main.md and its rows to that agent's own sheet
in jobs.xlsx. Agents never write files, so they stay independent and can't clobber each other.

Usage:
  python3 orchestrator.py                 # run forever, one round every --interval minutes
  python3 orchestrator.py --once          # run a single round and exit
  python3 orchestrator.py --once --agents scout,coach
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import fcntl
import json
import logging
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

import feeds
from feeds import ALLOWED_CITIES

ROOT = Path(__file__).resolve().parent
AGENTS_DIR = ROOT / "agents"
BOARD = ROOT / "main.md"
PROFILE = ROOT / "profile.md"
XLSX = ROOT / "jobs.xlsx"
STATE = ROOT / "state.json"
ATS_CACHE = ROOT / "ats_cache.json"  # which public job board each YC company uses
RUNS = ROOT / "runs"
LOGS = ROOT / "logs"
ARCHIVE = ROOT / "archive"
DIGESTS = ROOT / "digests"
WORKDIR = ROOT / ".agent-cwd"  # empty cwd so agents don't pick up a project CLAUDE.md

MODEL = os.environ.get("JOBHUNT_MODEL", "claude-haiku-5-5")
BUDGET_USD = os.environ.get("JOBHUNT_BUDGET_USD", "0.75")  # hard cap per agent per round
AGENT_TIMEOUT_S = 15 * 60
BOARD_TAIL_CHARS = 16000
BOARD_ROTATE_BYTES = 400_000
MIN_LPA = 60

S, N, I = "string", "number", "integer"
MODES = ["remote", "hybrid", "onsite"]
ELIG = ["yes", "no", "unclear"]
OLD_SHEET_NAMES = {"Remote Jobs": "Jobs 60+ LPA"}

# Each agent owns exactly one sheet. `fields` drive both the JSON schema and the sheet columns.
AGENTS = {
    "scout": {
        "title": "Job Scout",
        "sheet": "Jobs 60+ LPA",
        "fields": [("company", S), ("role", S), ("work_mode", MODES), ("location_policy", S),
                   ("india_eligible", ELIG), ("timezone", S), ("comp_listed", S),
                   ("est_lpa", N), ("reach", I), ("why_fit", S), ("gaps", S),
                   ("link", S), ("source", S), ("posted", S)],
        "key": ("link", "company+role"),
        "status": True,
    },
    "bigtech": {
        "title": "Big Tech Strategist",
        "sheet": "Big Tech Targets",
        "fields": [("company", S), ("role", S), ("level", S), ("work_mode", MODES), ("location", S),
                   ("india_eligible", ELIG), ("est_lpa", N),
                   ("reach", I), ("why_now", S), ("prep_focus", S), ("link", S)],
        "key": ("company+role+level",),
        "status": True,
    },
    "market": {
        "title": "Market Intelligence",
        "sheet": "Companies & Sources",
        "fields": [("name", S), ("type", ["company", "job_board", "search_query", "community", "market_fact"]),
                   ("pays_60lpa_remote", ["yes", "likely", "unknown", "no"]),
                   ("evidence", S), ("how_to_use", S), ("link", S)],
        "key": ("name",),
        "status": False,
    },
    "coach": {
        "title": "Search Strategist",
        "sheet": "Strategy Log",
        "fields": [("area", ["search_quality", "positioning", "skill_gap", "outreach", "interview_prep", "team_process"]),
                   ("observation", S), ("recommendation", S),
                   ("priority", ["high", "medium", "low"]),
                   ("owner", ["user", "scout", "bigtech", "market", "gems", "coach"])],
        "key": ("recommendation",),
        "status": False,
    },
    "gems": {
        "title": "Hidden Gems Finder",
        "sheet": "Hidden Gems",
        "fields": [("company", S), ("what_they_do", S), ("why_good", S),
                   ("stage", ["seed", "series_a", "series_b", "series_c_plus", "bootstrapped_profitable",
                              "public_small", "unknown"]),
                   ("team_size", S), ("hires_where", S), ("pay_signal", S), ("est_lpa", N),
                   ("ats", ["greenhouse", "lever", "ashby", "workable", "smartrecruiters", "other", "unknown"]),
                   ("ats_token", S), ("link", S)],
        "key": ("company",),
        "status": True,
    },
}

# Not an agent: rows pulled straight from free job APIs (feeds.py), already filtered for eligibility.
FEED = {
    "title": "Free job APIs",
    "sheet": "API Leads",
    "fields": [("company", S), ("tier", ["bigtech", "company", "board"]), ("title", S), ("work_mode", MODES),
               ("location", S), ("comp", S),
               ("posted", S), ("source", S), ("link", S), ("src", S), ("last_seen", S)],
    "key": ("link", "company+title"),
    "status": True,
}
SHEETS = dict(AGENTS, feed=FEED)
FEED_DIGEST_MAX = 30

log = logging.getLogger("jobhunt")


# ---------- schema ----------

def field_schema(t):
    if isinstance(t, list):
        return {"type": "string", "enum": t}
    return {"type": t}


def output_schema(name):
    cfg = AGENTS[name]
    row_props = {f: field_schema(t) for f, t in cfg["fields"]}
    who = list(AGENTS) + ["user"]
    return {
        "type": "object",
        "properties": {
            "board_post": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "findings": {"type": "array", "items": {"type": "string"}},
                    "asks": {"type": "array", "items": {"type": "object", "properties": {
                        "to": {"type": "string", "enum": [a for a in AGENTS if a != name]},
                        "ask": {"type": "string"}}, "required": ["to", "ask"]}},
                    "replies": {"type": "array", "items": {"type": "object", "properties": {
                        "to": {"type": "string", "enum": who},
                        "reply": {"type": "string"}}, "required": ["to", "reply"]}},
                    "next_focus": {"type": "string"},
                },
                "required": ["summary", "findings", "asks", "replies", "next_focus"],
            },
            "rows": {"type": "array", "items": {
                "type": "object", "properties": row_props, "required": [f for f, _ in cfg["fields"]]}},
        },
        "required": ["board_post", "rows"],
    }


# ---------- excel ----------

def headers(name):
    cfg = SHEETS[name]
    cols = ["found_at", "round"] + [f for f, _ in cfg["fields"]]
    if cfg["status"]:
        cols.append("status")  # yours to edit: New / Applied / Interviewing / Rejected / Skip
    return cols


def open_workbook():
    if XLSX.exists():
        wb = load_workbook(XLSX)
    else:
        wb = Workbook()
        wb.remove(wb.active)
    for old, new in OLD_SHEET_NAMES.items():
        if old in wb.sheetnames and new not in wb.sheetnames:
            wb[old].title = new
    for name, cfg in SHEETS.items():
        if cfg["sheet"] in wb.sheetnames:
            # Older sheets: add any new columns at the end; rows are written by header name.
            ws = wb[cfg["sheet"]]
            have = [c.value for c in ws[1]]
            for h in headers(name):
                if h not in have:
                    ws.cell(1, len(have) + 1, h).font = Font(bold=True, color="FFFFFF")
                    ws.cell(1, len(have) + 1).fill = PatternFill("solid", fgColor="305496")
                    have.append(h)
        else:
            ws = wb.create_sheet(cfg["sheet"])
            ws.append(headers(name))
            for cell in ws[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="305496")
            ws.freeze_panes = "A2"
            for i, h in enumerate(headers(name)):
                wide = h in ("why_fit", "gaps", "why_now", "prep_focus", "evidence", "how_to_use",
                             "observation", "recommendation", "link")
                ws.column_dimensions[ws.cell(1, i + 1).column_letter].width = 45 if wide else 16
    return wb


def append_rows(ws, rows, now, round_no):
    hdr = [c.value for c in ws[1]]
    for row in rows:
        full = dict(row, found_at=now, round=round_no, status="New")
        ws.append([full.get(h) for h in hdr])
        if row.get("link"):
            ws.cell(ws.max_row, hdr.index("link") + 1).hyperlink = row["link"]


def eligible(row):
    """Hard location rule from profile.md: remote must hire from India; onsite/hybrid only in allowed cities."""
    if "work_mode" not in row:
        return True  # market/coach rows aren't jobs
    place = row.get("location_policy") or row.get("location") or ""
    if "india_eligible" not in row:  # API feed rows: judge from the location text alone
        return feeds.classify(place, row.get("work_mode") == "remote")[0]
    if row.get("india_eligible") == "no":
        return False
    if row.get("work_mode") == "remote":
        return row.get("india_eligible") == "yes"
    return bool(ALLOWED_CITIES.search(place))


def sheet_rows(ws):
    hdr = [c.value for c in ws[1]]
    return [dict(zip(hdr, r)) for r in ws.iter_rows(min_row=2, values_only=True) if any(r)]


def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def norm_link(url):
    return norm(url).split("#")[0].split("?")[0].rstrip("/")


def row_keys(name, row):
    keys = set()
    for spec in SHEETS[name]["key"]:
        parts = spec.split("+")
        vals = [norm_link(row.get(p)) if p == "link" else norm(row.get(p)) for p in parts]
        if all(vals):
            keys.add(spec + "=" + "|".join(vals))
    # Same posting under another URL/title (e.g. amazon.jobs/en/jobs/10495277 vs .../10495277/sde-ii)
    m = re.match(r"https?://(?:www\.)?([^/]+)(/.*)?", norm_link(row.get("link")))
    if m:
        ids = re.findall(r"\d{6,}", m.group(2) or "")
        if ids:
            keys.add("jobid=" + m.group(1) + "|" + max(ids, key=len))
    return keys


def clean_rows(name, rows, known):
    """Validate agent rows at the trust boundary (they come from web content) and drop dupes."""
    cfg = SHEETS[name]
    kept, dropped = [], 0
    for r in rows:
        r = {f: r.get(f) for f, _ in cfg["fields"]}
        for f, t in cfg["fields"]:
            if t in (N, I):
                try:
                    r[f] = int(float(r[f])) if t == I else round(float(r[f]), 1)
                except (TypeError, ValueError):
                    r[f] = None
            elif r[f] is not None:
                r[f] = str(r[f]).strip()[:500]
        if "reach" in r and r["reach"] is not None:
            r["reach"] = max(1, min(5, r["reach"]))
        link = r.get("link") or ""
        if "link" in r and not link.startswith("https://"):
            dropped += 1
            continue
        if name == "scout" and (r.get("est_lpa") is None or r["est_lpa"] <= MIN_LPA):
            dropped += 1
            continue
        if not eligible(r):
            dropped += 1
            continue
        keys = row_keys(name, r)
        if not keys or keys & known:
            dropped += 1
            continue
        known |= keys
        kept.append(r)
    return kept, dropped


def compute_stats(wb, round_no):
    lines = []
    for name, cfg in SHEETS.items():
        rows = sheet_rows(wb[cfg["sheet"]])
        recent = [r for r in rows if isinstance(r.get("round"), int) and r["round"] > round_no - 4]
        line = f"- {cfg['sheet']} ({name}): {len(rows)} total, {len(recent)} added in the last 3 rounds"
        lpas = [r["est_lpa"] for r in rows if isinstance(r.get("est_lpa"), (int, float))]
        if lpas:
            line += f", median est {statistics.median(lpas):.0f} LPA"
        reach = [r["reach"] for r in rows if isinstance(r.get("reach"), int)]
        if reach:
            line += ", reach 4-5: %d, reach 3: %d, reach 1-2: %d" % (
                sum(x >= 4 for x in reach), sum(x == 3 for x in reach), sum(x <= 2 for x in reach))
        if cfg["status"]:
            st = {}
            for r in rows:
                s = r.get("status") or "New"
                st[s] = st.get(s, 0) + 1
            line += ", status: " + ", ".join(f"{k} {v}" for k, v in sorted(st.items()))
        lines.append(line)
    return "\n".join(lines)


def known_list(name, wb, limit=80):
    rows = sheet_rows(wb[SHEETS[name]["sheet"]])[-limit:]
    first = [f for f, _ in SHEETS[name]["fields"]][:3]
    return "\n".join("- " + " | ".join(str(r.get(f) or "") for f in first) for r in rows) or "(empty)"


# ---------- board ----------

BOARD_HEADER = """# Job Hunt Board (main.md)

Shared channel for the job-search agents. The orchestrator appends one entry per agent per round.
Agents: **scout** (Jobs 60+ LPA), **bigtech** (Big Tech Targets), **market** (Companies & Sources),
**gems** (Hidden Gems), **coach** (Strategy Log). **[feed]** posts are live openings pulled from free job APIs.
Rules: see `agents/_protocol.md`.

**To talk to the agents yourself**, append a block like this at the end of the file between rounds:

    ### [user]
    @scout skip crypto companies. @coach I applied to X and Y this week.

`[user]` entries override everything else.

---
"""


def ensure_board():
    if not BOARD.exists():
        BOARD.write_text(BOARD_HEADER)


def rotate_board():
    if BOARD.stat().st_size < BOARD_ROTATE_BYTES:
        return
    ARCHIVE.mkdir(exist_ok=True)
    dest = ARCHIVE / f"main-{dt.datetime.now():%Y%m%d-%H%M}.md"
    text = BOARD.read_text()
    shutil.copy2(BOARD, dest)
    tail = text[-BOARD_TAIL_CHARS:]
    cut = tail.find("\n## Round")  # carry the latest whole rounds over so the conversation continues
    carry = tail[cut:] if cut != -1 else ""
    BOARD.write_text(BOARD_HEADER + f"\n_Older rounds archived in `archive/{dest.name}`._\n" + carry)
    log.info("board rotated to %s", dest)


def board_tail():
    text = BOARD.read_text()
    if len(text) <= BOARD_TAIL_CHARS:
        return text
    tail = text[-BOARD_TAIL_CHARS:]
    cut = tail.find("\n### [")
    return "(...earlier rounds omitted...)\n" + (tail[cut:] if cut != -1 else tail)


def render_post(name, round_no, out, added, dropped, cost, error=None):
    when = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    head = f"### [{name}] {SHEETS[name]['title']} · round {round_no} · {when}\n"
    if error:
        return head + f"_No post this round: {error}_\n\n"
    p = out["board_post"]
    parts = [head, p.get("summary", "").strip() + "\n"]
    if p.get("findings"):
        parts.append("".join(f"- {f.strip()}\n" for f in p["findings"]))
    parts.append(f"\n**Excel:** +{added} rows to \"{AGENTS[name]['sheet']}\""
                 + (f" ({dropped} dropped: duplicate/invalid)" if dropped else "")
                 + (f" · cost ${cost:.3f}" if cost else "") + "\n")
    if p.get("asks"):
        asks = [(a["to"], re.sub(r"^@%s[:,]?\s*" % a["to"], "", a["ask"].strip())) for a in p["asks"]]
        parts.append("**Asks:**\n" + "".join(f"- @{to}: {ask}\n" for to, ask in asks))
    if p.get("replies"):
        parts.append("**Replies:**\n" + "".join(f"- to @{r['to']}: {r['reply'].strip()}\n" for r in p["replies"]))
    if p.get("next_focus"):
        parts.append(f"**Next focus:** {p['next_focus'].strip()}\n")
    return "".join(parts) + "\n"


# ---------- agents ----------

def claude_bin():
    found = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    if not Path(found).exists():
        sys.exit("claude CLI not found; install Claude Code and log in first")
    return found


def run_agent(name, round_no, context):
    system = (AGENTS_DIR / "_protocol.md").read_text() + "\n\n" + (AGENTS_DIR / f"{name}.md").read_text()
    extra = ""
    if name in context["leads"]:
        extra += ("\n## Live openings from free job APIs (new this round, already location-filtered)\n"
                  "Real, currently listed postings: your main source. Use web search only to check pay, years,\n"
                  "open status or company facts for the ones worth adding.\n" + context["leads"][name] + "\n")
    if name == "gems":
        extra += "\n## Hiring YC companies in India or remote (ideas, unverified)\n" + context["yc"] + "\n"
    prompt = f"""Today is {dt.date.today():%A, %d %B %Y}. This is round {round_no}. You are `{name}`.

## Candidate profile
{context['profile']}

## Search stats (all sheets)
{context['stats']}

## Already in your sheet (skip these)
{context['known'][name]}

## Board: latest part of main.md
{context['board']}
{extra}
Do your round now: research, then return the JSON (board_post + rows for your sheet only)."""
    cmd = [claude_bin(), "-p", "--model", MODEL,
           "--system-prompt", system,
           "--tools", "WebSearch,WebFetch",
           "--allowedTools", "WebSearch,WebFetch",
           "--output-format", "json",
           "--json-schema", json.dumps(output_schema(name)),
           "--max-budget-usd", BUDGET_USD,
           "--no-session-persistence",
           "--settings", json.dumps({"disableAllHooks": True})]
    t0 = time.time()
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          cwd=WORKDIR, timeout=AGENT_TIMEOUT_S)
    raw = proc.stdout
    (RUNS / f"round-{round_no:04d}").mkdir(parents=True, exist_ok=True)
    (RUNS / f"round-{round_no:04d}" / f"{name}.json").write_text(raw or proc.stderr)
    try:
        res = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"bad CLI output (exit {proc.returncode}): {(proc.stderr or raw)[:300]}")
    out = res.get("structured_output")
    if res.get("is_error") or not isinstance(out, dict):
        raise RuntimeError(f"agent error: {res.get('subtype')} {str(res.get('result'))[:300]}")
    log.info("%s finished in %.0fs, $%.3f", name, time.time() - t0, res.get("total_cost_usd") or 0)
    return out, res.get("total_cost_usd") or 0.0


# ---------- round ----------

def fetch_leads(wb):
    """Eligible openings from the free APIs that aren't in "API Leads" yet, plus the YC company list.
    Watchlist = big tech + seeds + Hidden Gems ATS + boards auto-discovered for hiring YC companies."""
    watch = feeds.BIGTECH_WATCHLIST + feeds.SEED_WATCHLIST
    for r in sheet_rows(wb[AGENTS["gems"]["sheet"]]):
        if r.get("ats") in feeds.ATS and r.get("ats_token"):
            watch.append((r["ats"], str(r["ats_token"]).strip(), "company"))
    yc = []
    try:
        yc = feeds.yc_hiring()
        cache = json.loads(ATS_CACHE.read_text()) if ATS_CACHE.exists() else {}
        new = feeds.discover_boards(yc, cache)
        ATS_CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True))
        log.info("board discovery: %d new boards, %d known", len(new), len(feeds.cached_boards(cache)))
        watch += feeds.cached_boards(cache)
    except Exception as e:
        log.warning("yc discovery failed: %s", e)
    try:
        jobs, report = feeds.fetch_all(watch)
    except Exception as e:
        log.warning("feeds failed: %s", e)
        return [], [], {"all": "failed"}, yc
    known = set()
    for x in sheet_rows(wb[FEED["sheet"]]):
        known |= row_keys("feed", x)
    fresh, _ = clean_rows("feed", jobs, known)
    log.info("feeds: %d eligible, %d new from %d sources", len(jobs), len(fresh), len(report))
    fresh.sort(key=lambda j: (not j["comp"], j["source"] == "Himalayas"))  # listed pay first
    return fresh, jobs, report, yc


LEAD_MAX_AGE_DAYS = 21  # window-only sources: a lead not seen for this long is treated as closed


def prune_leads(ws, jobs, report, today):
    """Remove API Leads rows whose posting has closed. Returns (removed rows, rows flagged).
    Closed = its full-list board was fetched fine this round and no longer has it, or (window-only sources
    and older rows without a source) not seen for LEAD_MAX_AGE_DAYS. Rows you've touched (status not New)
    are kept and marked instead of deleted."""
    hdr = [c.value for c in ws[1]]
    col = {h: hdr.index(h) + 1 for h in hdr}
    live = {j["link"]: j for j in jobs}
    ok = {k for k, v in report.items() if v != "failed"}
    removed, flagged = [], []
    for r in range(ws.max_row, 1, -1):
        get = lambda h: ws.cell(r, col[h]).value
        link = get("link")
        if not link:
            continue
        if link in live:
            ws.cell(r, col["last_seen"], today.isoformat())
            ws.cell(r, col["src"], live[link]["src"])
            ws.cell(r, col["tier"], live[link]["tier"])
            continue
        src = get("src") or ""
        if src.split(":")[0] in feeds.FULL_LIST_ATS:
            closed = src in ok
        else:
            seen = str(get("last_seen") or get("found_at") or "")[:10]
            try:
                closed = (today - dt.date.fromisoformat(seen)).days > LEAD_MAX_AGE_DAYS
            except ValueError:
                closed = False
        if not closed:
            continue
        status = str(get("status") or "New")
        row = {"company": get("company"), "title": get("title"), "status": status}
        if status == "New":
            ws.delete_rows(r)
            removed.append(row)
        elif "posting closed" not in status:
            ws.cell(r, col["status"], status + " (posting closed)")
            flagged.append(row)
    return removed, flagged


def lead_lines(rows, limit=60):
    return "\n".join(f"- {j['company']} | {j['title']} | {j['work_mode']}: {j['location']} | "
                     f"{j['comp'] or 'pay not listed'} | {j['link']}" for j in rows[:limit]) or "(none new)"


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {"round": 0}


def run_round(names):
    state = load_state()
    round_no = state["round"] + 1
    ensure_board()
    rotate_board()
    wb = open_workbook()
    leads, all_jobs, feed_report, yc_all = fetch_leads(wb)
    yc = feeds.yc_hiring_companies(round_no, companies=yc_all) if yc_all else []
    big = [j for j in leads if j["tier"] == "bigtech"]
    rest = [j for j in leads if j["tier"] != "bigtech"]
    context = {
        "leads": {"bigtech": lead_lines(big), "scout": lead_lines(rest),
                  "gems": lead_lines([j for j in rest if j["tier"] == "company"])},
        "yc": "\n".join(f"- {c['name']} ({c['batch']}, {c['team_size']} people, {', '.join(c['regions'] or [])}): {c['one_liner']} {c['website']}"
                         for c in yc) or "(unavailable)",
        "profile": PROFILE.read_text(),
        "stats": compute_stats(wb, round_no),
        "board": board_tail(),
        "known": {n: known_list(n, wb) for n in names},
    }
    log.info("round %d starting: %s", round_no, ", ".join(names))

    results = {}
    with cf.ThreadPoolExecutor(max_workers=len(names)) as pool:
        futs = {pool.submit(run_agent, n, round_no, context): n for n in names}
        for fut in cf.as_completed(futs):
            n = futs[fut]
            try:
                results[n] = fut.result()
            except Exception as e:  # one agent failing never blocks the others
                log.warning("%s failed: %s", n, e)
                results[n] = e

    # Single writer: re-open the workbook so edits you made during the round are kept.
    wb = open_workbook()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    posts = [f"\n## Round {round_no} · {now}\n\n"]
    total_cost = 0.0
    new_rows = {}
    ws = wb[FEED["sheet"]]
    known = set()
    for x in sheet_rows(ws):
        known |= row_keys("feed", x)
    feed_rows, _ = clean_rows("feed", leads, known)
    for j in feed_rows:
        j["last_seen"] = dt.date.today().isoformat()
    append_rows(ws, feed_rows, now, round_no)
    closed, flagged = prune_leads(ws, all_jobs, feed_report, dt.date.today()) if all_jobs else ([], [])
    new_rows["feed"] = feed_rows
    by_src = {}
    for j in feed_rows:
        by_src[j["source"]] = by_src.get(j["source"], 0) + 1
    failed = [k for k, v in feed_report.items() if v == "failed"]
    posts.append(f"### [feed] Free job APIs · round {round_no} · {now}\n"
                 f"+{len(feed_rows)} new eligible openings to \"API Leads\" from {len(feed_report)} sources "
                 f"({sum(j['tier'] == 'bigtech' for j in feed_rows)} big tech). New by source: "
                 + (", ".join(f"{k} {v}" for k, v in sorted(by_src.items())) or "none")
                 + (f". Failed: {', '.join(failed)}" if failed else "")
                 + f". Closed postings removed: {len(closed)}"
                 + (f", {len(flagged)} you'd acted on marked '(posting closed)'" if flagged else "") + "\n\n")
    for n in names:
        r = results[n]
        if isinstance(r, Exception):
            posts.append(render_post(n, round_no, None, 0, 0, 0, error=str(r)[:300]))
            continue
        out, cost = r
        total_cost += cost
        ws = wb[AGENTS[n]["sheet"]]
        known = set()
        for x in sheet_rows(ws):
            known |= row_keys(n, x)
        kept, dropped = clean_rows(n, out.get("rows") or [], known)
        new_rows[n] = kept
        append_rows(ws, kept, now, round_no)
        posts.append(render_post(n, round_no, out, len(kept), dropped, cost))

    save_workbook(wb)
    with BOARD.open("a") as f:  # append-only, so [user] notes added mid-round survive
        f.write("".join(posts))
    write_digest(round_no, now, new_rows, "".join(posts), closed + flagged)
    state["round"] = round_no
    state["last_round_at"] = now
    state["total_cost_usd"] = round(state.get("total_cost_usd", 0) + total_cost, 4)
    STATE.write_text(json.dumps(state, indent=2))
    log.info("round %d done, cost $%.3f (lifetime $%.2f)", round_no, total_cost, state["total_cost_usd"])


DIGEST_COLS = {
    "scout": ["company", "role", "work_mode", "location_policy", "est_lpa", "reach", "link"],
    "bigtech": ["company", "role", "level", "work_mode", "location", "est_lpa", "reach", "link"],
    "market": ["name", "type", "pays_60lpa_remote", "link"],
    "coach": ["priority", "owner", "recommendation"],
    "gems": ["company", "what_they_do", "stage", "hires_where", "est_lpa", "link"],
    "feed": ["company", "tier", "title", "work_mode", "location", "comp", "source", "link"],
}


def write_digest(round_no, when, new_rows, posts, closed=()):
    """One markdown file per round: the new Excel rows as tables, then the agents' board posts."""
    out = [f"# Job hunt digest · round {round_no} · {when}\n"]
    for n, rows in new_rows.items():
        cols = DIGEST_COLS[n]
        out.append(f"\n## {SHEETS[n]['sheet']} (+{len(rows)})\n")
        if not rows:
            out.append("_Nothing new._\n")
            continue
        out.append("| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n")
        for r in rows[:FEED_DIGEST_MAX]:
            out.append("| " + " | ".join(str(r.get(c) or "").replace("|", "/") for c in cols) + " |\n")
        if len(rows) > FEED_DIGEST_MAX:
            out.append(f"\n_+{len(rows) - FEED_DIGEST_MAX} more in jobs.xlsx._\n")
    if closed:
        out.append(f"\n## Closed postings ({len(closed)})\n" + "".join(
            f"- {c['company']} | {c['title']}" + (f" (your status: {c['status']})" if c["status"] != "New" else " (removed)") + "\n"
            for c in closed))
    out.append("\n## Board posts\n" + posts)
    DIGESTS.mkdir(exist_ok=True)
    path = DIGESTS / f"round-{round_no:04d}.md"
    path.write_text("".join(out))
    log.info("digest written: %s", path)


def save_workbook(wb):
    for attempt in range(5):
        try:
            tmp = XLSX.with_suffix(".tmp.xlsx")
            wb.save(tmp)
            os.replace(tmp, XLSX)  # atomic, so a crash never leaves a half-written jobs.xlsx
            return
        except OSError as e:
            log.warning("saving jobs.xlsx failed (%s), retrying", e)
            time.sleep(10)
    raise RuntimeError("could not save jobs.xlsx")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one round and exit")
    ap.add_argument("--interval", type=float, default=float(os.environ.get("JOBHUNT_INTERVAL_MIN", 60)),
                    help="minutes between round starts (default 60)")
    ap.add_argument("--agents", default=",".join(AGENTS), help="comma-separated subset")
    args = ap.parse_args()
    names = [n.strip() for n in args.agents.split(",") if n.strip()]
    bad = [n for n in names if n not in AGENTS]
    if bad:
        sys.exit(f"unknown agents: {bad}")

    for d in (RUNS, LOGS, WORKDIR):
        d.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOGS / "orchestrator.log"), logging.StreamHandler()])

    lock = open(ROOT / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another orchestrator is already running (see .lock)")

    while True:
        started = time.time()
        try:
            run_round(names)
        except Exception:
            log.exception("round failed")
        if args.once:
            return
        wait = max(60, args.interval * 60 - (time.time() - started))
        log.info("sleeping %.0f min", wait / 60)
        time.sleep(wait)


if __name__ == "__main__":
    main()
