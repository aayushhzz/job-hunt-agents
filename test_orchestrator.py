"""Offline checks for the parts that touch your data: row validation, dedupe, Excel and board writes.
Run: python3 test_orchestrator.py  (no Claude calls)"""
import tempfile
from pathlib import Path

import orchestrator as o

tmp = Path(tempfile.mkdtemp())
o.XLSX, o.BOARD = tmp / "jobs.xlsx", tmp / "main.md"

# schema is valid JSON-able and enumerates the other agents only
s = o.output_schema("scout")
assert "scout" not in s["properties"]["board_post"]["properties"]["asks"]["items"]["properties"]["to"]["enum"]

job = dict(company="Acme", role="Backend Engineer", work_mode="remote", location_policy="Remote - India",
           india_eligible="yes", timezone="IST", comp_listed="$90k",
           est_lpa="79", reach=9, why_fit="Go+AWS", gaps="none", link="https://jobs.lever.co/acme/1?src=x",
           source="lever", posted="")
low = dict(job, company="Cheap", est_lpa=40, link="https://x.com/2")
nolink = dict(job, company="NoLink", link="javascript:alert(1)")
known = set()
kept, dropped = o.clean_rows("scout", [job, low, nolink, dict(job, link="https://jobs.lever.co/acme/1/")], known)
assert [k["company"] for k in kept] == ["Acme"], kept
assert dropped == 3, dropped                      # low pay, bad link, duplicate link
assert kept[0]["est_lpa"] == 79.0 and kept[0]["reach"] == 5

# second round: same company+role under a new URL is still a duplicate
kept2, _ = o.clean_rows("scout", [dict(job, link="https://acme.com/careers/1")], known)
assert kept2 == []

# workbook round-trip keeps user edits
wb = o.open_workbook()
ws = wb["Jobs 60+ LPA"]
o.append_rows(ws, kept, "now", 1)
ws.cell(2, [c.value for c in ws[1]].index("status") + 1, "Applied")
o.save_workbook(wb)
rows = o.sheet_rows(o.open_workbook()["Jobs 60+ LPA"])
assert rows[0]["status"] == "Applied" and set(o.open_workbook().sheetnames) == {a["sheet"] for a in o.SHEETS.values()}
assert "1 total" in o.compute_stats(o.open_workbook(), 2)

# board rendering
post = {"board_post": {"summary": "Found 1.", "findings": ["a"], "asks": [{"to": "market", "ask": "boards?"}],
                       "replies": [], "next_focus": "APAC"}, "rows": []}
txt = o.render_post("scout", 3, post, 1, 2, 0.12)
assert txt.startswith("### [scout]") and "@market: boards?" in txt and "+1 rows" in txt
assert "_No post this round: boom_" in o.render_post("coach", 3, None, 0, 0, 0, error="boom")

# rotation keeps the latest rounds
o.ensure_board()
o.BOARD.write_text(o.BOARD_HEADER + "".join(f"\n## Round {i}\n\n### [scout] x\n" + "y" * 5000 for i in range(100)))
o.ARCHIVE = tmp / "archive"
o.rotate_board()
new = o.BOARD.read_text()
assert "## Round 99" in new and len(list(o.ARCHIVE.iterdir())) == 1 and len(new) < 20000
# eligibility: remote needs India hiring; onsite/hybrid only Noida, Delhi, Gurugram, Bengaluru
E = o.eligible
assert E(dict(work_mode="remote", india_eligible="yes")) and not E(dict(work_mode="remote", india_eligible="unclear"))
assert E(dict(work_mode="onsite", india_eligible="yes", location="Bengaluru / Hyderabad"))
assert E(dict(work_mode="hybrid", india_eligible="unclear", location_policy="Noida, UP"))
assert not E(dict(work_mode="onsite", india_eligible="yes", location="Hyderabad"))
assert E(dict(work_mode="hybrid", india_eligible="yes", location="Gurgaon, Haryana")) and E(dict(work_mode="onsite", india_eligible="yes", location="Gurugram"))
assert not E(dict(work_mode="onsite", india_eligible="no", location="Bangalore"))
assert E(dict(name="x"))  # non-job rows pass
kept3, d3 = o.clean_rows("scout", [dict(job, company="Hyd", work_mode="onsite", location_policy="Hyderabad")], set())
assert kept3 == [] and d3 == 1

# migration: old "Remote Jobs" sheet without the new columns is renamed and extended; rows land by header
from openpyxl import Workbook
old = Workbook(); ws0 = old.active; ws0.title = "Remote Jobs"
ws0.append(["found_at", "round", "company", "role", "status"]); ws0.append(["t", 1, "Old", "R", "Applied"])
o.XLSX = tmp / "old.xlsx"; old.save(o.XLSX)
wb = o.open_workbook(); ws = wb["Jobs 60+ LPA"]
o.append_rows(ws, kept, "now", 3)
rows = o.sheet_rows(ws)
assert rows[0]["status"] == "Applied" and rows[1]["company"] == "Acme" and rows[1]["india_eligible"] == "yes"
assert rows[1]["status"] == "New" and rows[1]["round"] == 3

# digest: tables for new rows, "nothing new" for empty agents, pipes escaped
o.DIGESTS = tmp / "digests"
o.write_digest(7, "now", {"scout": [dict(kept[0], role="A|B")], "coach": []}, "### [scout] x\n")
d = (o.DIGESTS / "round-0007.md").read_text()
assert "| Acme | A/B | remote | Remote - India | 79.0 | 5 |" in d and "_Nothing new._" in d and "### [scout] x" in d, d
print("all checks passed")

# ---- free-API feed (offline: sources are stubbed) ----
import feeds
assert feeds.classify("Bangalore, India") == (True, "onsite")
assert feeds.classify("Remote, Global", True) == (True, "remote")
assert feeds.classify("India, Remote") == (True, "remote")
assert feeds.classify("Remote, United States", True)[0] is False
assert feeds.classify("Hyderabad, India")[0] is False
assert feeds.classify("Remote", True)[0] is False                   # region unknown -> not eligible
assert feeds.job("X", "Backend Engineer", "Remote - India", "https://x/1", "t")["work_mode"] == "remote"
assert feeds.job("X", "Staff Backend Engineer", "Remote - India", "https://x/1", "t") is None
assert feeds.job("X", "Product Designer", "Bengaluru", "https://x/1", "t") is None
assert feeds.job("X", "Backend Engineer", "Bengaluru", "http://x/1", "t") is None
assert feeds.find_pay("Pay: ₹40L – ₹1Cr").startswith("₹40L")
assert feeds.find_pay("fee $0 to $300 then $120,000 - $150,000 base") == "$120,000 - $150,000"
assert feeds.find_pay("$5,000 - $9,500 per month") == "$5,000 - $9,500"
assert feeds.job("X", "Senior Software Engineer Salesforce", "Bengaluru", "https://x/1", "t") is None
assert feeds.job("X", "Software Architect, Java Backend", "Bengaluru", "https://x/1", "t") is None

def boom(): raise IOError("down")
saved = (feeds.AGGREGATORS, feeds.ATS)
feeds.AGGREGATORS = {
    "himalayas": lambda: iter([feeds.job("A", "SRE", "Remote - India", "https://a/1", "Himalayas"),
                               feeds.job("A", "SRE", "Remote - India", "https://a/1", "Himalayas")]),
    "remoteok": boom, "remotive": lambda: iter([]), "hn": lambda: iter([None])}
calls = []
feeds.ATS = {"ashby": lambda t: calls.append(t) or iter([feeds.job("B", "Backend Engineer", "Bengaluru", "https://b/1", "Ashby")])}
jobs, rep = feeds.fetch_all([("ashby", "good-co", "bigtech"), ("ashby", "good-co", "bigtech"),
                             ("ashby", "bad/../token"), ("lever", "x")])
assert len(jobs) == 2 and rep["remoteok"] == "failed" and calls == ["good-co"], (jobs, rep, calls)
assert {j["tier"] for j in jobs} == {"bigtech", "board"}
feeds.AGGREGATORS, feeds.ATS = saved

# board discovery: probes only fitting, unchecked companies; caches hits and misses; rechecks misses after 30 days
import datetime as dt
saved_probe, probed = feeds.board_has_jobs, []
feeds.board_has_jobs = lambda ats, tok: probed.append((ats, tok)) or (ats, tok) == ("greenhouse", "acmeai")
yc = [dict(slug="acme-ai", name="Acme AI", regions=["India"], team_size=50),
      dict(slug="big-us", name="Big US", regions=["America / Canada"], team_size=50),
      dict(slug="tiny", name="Tiny", regions=["Remote"], team_size=3),
      dict(slug="nojobs", name="NoJobs", regions=["Remote"], team_size=40)]
cache = {}
found = feeds.discover_boards(yc, cache, today=dt.date(2026, 11, 18))
assert found == [("greenhouse", "acmeai", "company")] and set(cache) == {"acme-ai", "nojobs"}, (found, cache)
probed.clear()
assert feeds.discover_boards(yc, cache, today=dt.date(2026, 11, 30)) == [] and probed == []  # cached
feeds.discover_boards(yc, cache, today=dt.date(2026, 12, 25))
assert probed and all(tok == "nojobs" for _, tok in probed)                                  # miss rechecked
assert feeds.cached_boards(cache) == [("greenhouse", "acmeai", "company")]
feeds.board_has_jobs = saved_probe

# feed rows pass the orchestrator's eligibility check and dedupe
lead = dict(company="A", tier="board", title="SRE", work_mode="remote", location="Remote - India", comp="", posted="",
            source="Himalayas", link="https://a/1")
k, d = o.clean_rows("feed", [lead, dict(lead, location="Remote - United States", link="https://a/2", title="SRE 2")], set())
assert len(k) == 1 and d == 1

# closed postings: full-list board fetched OK and posting gone -> removed; touched rows are marked, not deleted;
# failed boards and window-only sources (until 21 days unseen) are left alone
import datetime as dt
from openpyxl import Workbook
o.XLSX = tmp / "prune.xlsx"; Workbook().save(o.XLSX)
wb = o.open_workbook(); ws = wb["API Leads"]
base = dict(company="C", tier="company", work_mode="remote", location="Remote - India", comp="", posted="", source="x")
rows = [dict(base, title="open", link="https://g/1", src="greenhouse:c"),
        dict(base, title="gone", link="https://g/2", src="greenhouse:c"),
        dict(base, title="gone but applied", link="https://g/3", src="greenhouse:c"),
        dict(base, title="board down", link="https://g/4", src="ashby:down"),
        dict(base, title="himalayas recent", link="https://h/1", src="himalayas", last_seen="2026-11-10"),
        dict(base, title="himalayas old", link="https://h/2", src="himalayas", last_seen="2026-10-01"),
        dict(base, title="legacy old", link="https://l/1", src=None)]
o.append_rows(ws, rows, "2026-10-01 10:00", 1)
hdr = [c.value for c in ws[1]]
ws.cell(4, hdr.index("status") + 1, "Applied")
live = [dict(rows[0], src="greenhouse:c")]
removed, flagged = o.prune_leads(ws, live, {"greenhouse:c": 3, "ashby:down": "failed", "himalayas": 20}, dt.date(2026, 11, 18))
left = {r["title"]: r for r in o.sheet_rows(ws)}
assert sorted(x["title"] for x in removed) == ["gone", "himalayas old", "legacy old"], removed
assert [x["title"] for x in flagged] == ["gone but applied"]
assert set(left) == {"open", "gone but applied", "board down", "himalayas recent"}, left
assert left["gone but applied"]["status"] == "Applied (posting closed)" and left["open"]["last_seen"] == "2026-11-18"
o.prune_leads(ws, live, {"greenhouse:c": 3}, dt.date(2026, 11, 18))
assert o.sheet_rows(ws)[1]["status"] == "Applied (posting closed)"           # not marked twice
o.DIGESTS = tmp / "digests"
o.write_digest(9, "now", {}, "", removed + flagged)
d = (o.DIGESTS / "round-0009.md").read_text()
assert "## Closed postings (4)" in d and "C | gone (removed)" in d and "(your status: Applied)" in d
print("feed checks passed")
