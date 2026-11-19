"""Free, keyless job APIs: the main source of openings. Web search is only for checking details.

Pulled once per round, filtered to eligible engineering roles, written to the "API Leads" sheet and handed
to the agents. Sources (all public, documented or official feeds, no key; a daily run is far inside limits):
- Company job boards (ATS): Greenhouse, Lever, Ashby, Workable, SmartRecruiters
    watchlist = BIGTECH_WATCHLIST + SEED_WATCHLIST + Hidden Gems sheet + boards auto-discovered for
    hiring YC companies (cached in ats_cache.json, so each company is probed once a month at most)
- Aggregators: Himalayas search (country=India), We Work Remotely RSS, RemoteOK, Remotive
  (credit Remote OK / Remotive / WWR as the source; Remotive is 24h delayed)
- Hacker News "Who is hiring" via the Algolia API; yc-oss list of hiring YC companies
Not used (no free public API, or terms forbid automated access): HiringCafe, Wellfound, Welcome to the
Jungle, LinkedIn, Indeed, Glassdoor, ZipRecruiter. Agents may still look at them with web search.
"""
import concurrent.futures as cf
import datetime as dt
import html
import json
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET  # shortcut: stdlib parser on a known RSS feed; use defusedxml if feeds grow

log = logging.getLogger("jobhunt")
UA = {"User-Agent": "jobhunt-agents/0.1"}

# Where the candidate can work onsite/hybrid. Remote roles must hire from India instead.
ALLOWED_CITIES = re.compile(r"\b(noida|new delhi|delhi|gurugram|gurgaon|bengaluru|bangalore)\b", re.I)
REMOTE_OK_REGION = re.compile(r"\b(india|apac|asia|worldwide|anywhere|global)\b", re.I)
ROLE_IN = re.compile(r"\b(backend|back-end|platform|infra\w*|site reliability|sre|devops|golang|go engineer|"
                     r"software engineer|software developer|cloud|distributed|ai engineer|ml infra\w*|"
                     r"developer productivity|systems engineer|reliability)\b", re.I)
ROLE_OUT = re.compile(r"\b(staff|principal|director|head|manager|vp|frontend|front-end|ios|android|mobile|"
                      r"sales|support|intern|qa|test|recruit\w*|designer|marketing|account|product|people|salesforce|"
                      r"netsuite|sap|servicenow|architect|analyst|data scientist|consultant)\b", re.I)
TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")  # board tokens come from agent output / guesses: never trust raw
PAY = re.compile(r"(?:₹|INR|\$|USD)\s?[\d][\d,.]*\s?[kKlL]?\s?(?:-|–|—|to)\s?(?:₹|INR|\$|USD)?\s?[\d][\d,.]*\s?[kKlL]?")

# Public ATS boards, checked 2026-11-18. tier: "bigtech" leads go to bigtech, the rest to scout/gems.
BIGTECH_WATCHLIST = [("greenhouse", t, "bigtech") for t in (
    "stripe", "databricks", "cloudflare", "datadog", "mongodb", "coinbase", "rubrik", "okta", "twilio",
    "elastic", "airbnb", "dropbox", "pinterest", "reddit", "figma", "anthropic", "samsara")] + [
    ("ashby", t, "bigtech") for t in ("openai", "snowflake", "notion")]
SEED_WATCHLIST = [("greenhouse", t, "company") for t in (
    "gitlab", "grafanalabs", "sezzle", "razorpaysoftwareprivatelimited", "canonical", "vercel",
    "tailscale", "cockroachlabs", "planetscale")] + [("ashby", t, "company") for t in (
    "signoz", "posthog", "supabase", "temporal", "clickhouse", "sentry", "render", "snyk", "atlan")] + [
    ("lever", t, "company") for t in ("zeta", "cred")]
HIMALAYAS_QUERIES = ["backend engineer", "platform engineer", "site reliability engineer", "golang",
                     "infrastructure engineer", "devops engineer"]
WWR_CATEGORIES = ["remote-back-end-programming-jobs", "remote-devops-sysadmin-jobs", "remote-full-stack-programming-jobs"]
YC_REGIONS = {"India", "Remote", "Fully Remote", "Asia"}
DISCOVER_PER_ROUND = 30
RECHECK_DAYS = 30


def get_json(url, timeout=40, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(UA, **({"Content-Type": "application/json"} if body is not None else {}))
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def classify(location, remote_flag=False):
    """Return (eligible, work_mode) for a free-text location, per the candidate's location rules."""
    loc = location or ""
    remote = remote_flag or "remote" in loc.lower()
    if ALLOWED_CITIES.search(loc):
        return True, "remote" if remote else "onsite"
    if remote and REMOTE_OK_REGION.search(loc):
        return True, "remote"
    return False, "remote" if remote else "onsite"


def job(company, title, location, link, source, comp="", posted="", remote=False):
    ok, mode = classify(location, remote)
    if not ok or not ROLE_IN.search(title or "") or ROLE_OUT.search(title or ""):
        return None
    if not str(link).startswith("https://"):
        return None
    return {"company": company, "title": title.strip(), "work_mode": mode, "location": (location or "")[:200],
            "comp": (comp or "")[:120], "posted": str(posted or "")[:10], "source": source, "link": link}


def find_pay(text):
    """First salary-looking range in text. Ignores tiny numbers ("$0 to $300") that aren't salaries."""
    for m in PAY.finditer(text or ""):
        nums = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", m.group(0))]
        if re.search(r"[kKlL]\b|Cr", m.group(0)) or max(nums) >= 1000:
            return m.group(0).strip()
    return ""


def strip_html(s):
    return re.sub(r"<[^>]+>", " ", html.unescape(s or ""))


# ---------- company job boards ----------

def greenhouse(token):
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    for j in d.get("jobs", []):
        yield job(j.get("company_name") or token, j.get("title"), (j.get("location") or {}).get("name"),
                  j.get("absolute_url"), "Greenhouse", find_pay(strip_html(j.get("content"))), j.get("updated_at"))


def lever(token):
    for j in get_json(f"https://api.lever.co/v0/postings/{token}?mode=json"):
        cat, sr = j.get("categories") or {}, j.get("salaryRange") or {}
        comp = f"{sr.get('currency', '')} {sr.get('min')}-{sr.get('max')} {sr.get('interval', '')}" if sr else ""
        locs = " / ".join(cat.get("allLocations") or [cat.get("location") or ""])
        yield job(token, j.get("text"), locs, j.get("hostedUrl"), "Lever", comp, "",
                  remote=j.get("workplaceType") == "remote")


def ashby(token):
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true")
    for j in d.get("jobs", []):
        locs = " / ".join([j.get("location") or ""] + [x.get("location", "") for x in j.get("secondaryLocations") or []])
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        name = (j.get("jobUrl") or "").split("/")[3:4] or [token]  # URL keeps the company's own casing
        yield job(name[0], j.get("title"), locs, j.get("jobUrl"), "Ashby", comp, j.get("publishedAt"),
                  remote=bool(j.get("isRemote")))


def workable(token, pages=5):
    body, url = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}, \
        f"https://apply.workable.com/api/v3/accounts/{token}/jobs"
    for _ in range(pages):
        d = get_json(url, body=body)
        for j in d.get("results", []):
            locs = " / ".join(", ".join(filter(None, [l.get("city"), l.get("country")]))
                              for l in (j.get("locations") or [j.get("location") or {}]))
            yield job(token, j.get("title"), locs, f"https://apply.workable.com/{token}/j/{j.get('shortcode')}/",
                      "Workable", "", j.get("published"), remote=bool(j.get("remote")))
        if not d.get("nextPage"):
            return
        body = dict(body, token=d["nextPage"])


def smartrecruiters(token, pages=3):
    for page in range(pages):
        d = get_json(f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset={page * 100}")
        for j in d.get("content", []):
            loc = j.get("location") or {}
            place = ", ".join(filter(None, [loc.get("city"), loc.get("country")]))
            yield job((j.get("company") or {}).get("name") or token, j.get("name"), place,
                      f"https://jobs.smartrecruiters.com/{token}/{j.get('id')}", "SmartRecruiters", "",
                      j.get("releasedDate"), remote=bool(loc.get("remote")))
        if (page + 1) * 100 >= d.get("totalFound", 0):
            return


ATS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable,
       "smartrecruiters": smartrecruiters}


# ---------- aggregators ----------

def himalayas():
    for q in HIMALAYAS_QUERIES:
        d = get_json("https://himalayas.app/jobs/api/search?" + urllib.parse.urlencode(
            {"q": q, "country": "India", "sort": "recent"}))
        for j in d.get("jobs", []):
            regions = j.get("locationRestrictions") or []
            loc = "Remote - " + (", ".join(regions) if regions else "Worldwide")
            comp = f"{j.get('currency') or ''} {j.get('minSalary')}-{j.get('maxSalary')}" if j.get("minSalary") else ""
            yield job(j.get("companyName"), j.get("title"), loc, j.get("applicationLink"), "Himalayas", comp,
                      j.get("pubDate"), remote=True)


def weworkremotely():
    for cat in WWR_CATEGORIES:
        req = urllib.request.Request(f"https://weworkremotely.com/categories/{cat}.rss", headers=UA)
        with urllib.request.urlopen(req, timeout=40) as r:
            root = ET.fromstring(r.read())
        for it in root.iter("item"):
            company, _, title = (it.findtext("title") or "").partition(": ")
            yield job(company, title or company, "Remote - " + (it.findtext("region") or ""), it.findtext("link"),
                      "We Work Remotely", "", it.findtext("pubDate"), remote=True)


def remoteok():
    for j in get_json("https://remoteok.com/api")[1:]:  # item 0 is the legal notice
        comp = f"USD {j['salary_min']}-{j['salary_max']}" if j.get("salary_min") else ""
        yield job(j.get("company"), j.get("position"), "Remote - " + (j.get("location") or ""), j.get("url"),
                  "Remote OK", comp, j.get("date"), remote=True)


def remotive():
    d = get_json("https://remotive.com/api/remote-jobs?category=software-dev")
    for j in d.get("jobs", []):
        yield job(j.get("company_name"), j.get("title"), "Remote - " + (j.get("candidate_required_location") or ""),
                  j.get("url"), "Remotive", j.get("salary"), j.get("publication_date"), remote=True)


def hn_who_is_hiring():
    stories = get_json("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=5")
    story = next((s for s in stories["hits"] if s["title"].startswith("Ask HN: Who is hiring")), None)
    if not story:
        return
    sid = story["objectID"]
    d = get_json(f"https://hn.algolia.com/api/v1/search?tags=comment,story_{sid}&hitsPerPage=1000&query=remote")
    for c in d.get("hits", []):
        if str(c.get("parent_id")) != sid:
            continue  # replies, not job posts
        text = strip_html(c.get("comment_text"))
        parts = [p.strip() for p in text.strip().split("\n")[0][:200].split("|")]
        if len(parts) < 2:
            continue
        loc = next((p for p in parts if re.search(r"remote|onsite|hybrid", p, re.I)), parts[-1])
        yield job(parts[0], parts[1], loc, f"https://news.ycombinator.com/item?id={c['objectID']}",
                  "HN Who is hiring", find_pay(text), c.get("created_at"))


# Sources that return a company's complete list of open jobs: if a fetch succeeds and a posting is missing,
# it has closed. Aggregators, Workable and SmartRecruiters (paged, capped) only show a window, so their rows
# are closed by age instead.
FULL_LIST_ATS = ("greenhouse", "lever", "ashby")

AGGREGATORS = {"himalayas": himalayas, "weworkremotely": weworkremotely, "remoteok": remoteok,
               "remotive": remotive, "hn": hn_who_is_hiring}


def fetch_all(watchlist, workers=8):
    """All eligible engineering jobs from every source, deduped by link. A failing source is logged and skipped.
    watchlist: (ats, token, tier) tuples. Each job gets a `tier`: bigtech | company | board."""
    sources = []
    for item in dict.fromkeys(tuple(w) for w in watchlist):
        ats, tok, tier = (list(item) + ["company"])[:3]
        if ats in ATS and TOKEN.match(tok or ""):
            sources.append((f"{ats}:{tok}", tier, lambda a=ats, t=tok: ATS[a](t)))
    sources += [(name, "board", fn) for name, fn in AGGREGATORS.items()]

    def run(src):
        name, tier, fn = src
        try:
            return name, tier, [j for j in fn() if j]
        except Exception as e:  # network/schema change: keep the other sources going
            log.warning("feed %s failed: %s", name, e)
            return name, tier, None

    jobs, seen, report = [], set(), {}
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for name, tier, got in pool.map(run, sources):
            report[name] = "failed" if got is None else len(got)
            for j in got or []:
                if j["link"] not in seen:
                    seen.add(j["link"])
                    jobs.append(dict(j, tier=tier, src=name))
    return jobs, report


# ---------- company discovery ----------

def yc_hiring():
    return get_json("https://yc-oss.github.io/api/companies/hiring.json")


def yc_fit(companies):
    return [c for c in companies if YC_REGIONS & set(c.get("regions") or []) and 10 <= (c.get("team_size") or 0) <= 1000]


def yc_hiring_companies(offset, n=40, companies=None):
    """A rotating slice of hiring YC companies that hire in India or remotely: raw material for `gems`."""
    fit = yc_fit(companies if companies is not None else yc_hiring())
    if not fit:
        return []
    start = (offset * n) % len(fit)
    return [{"name": c["name"], "one_liner": c.get("one_liner", ""), "team_size": c.get("team_size"),
             "regions": c.get("regions"), "batch": c.get("batch"), "website": c.get("website")}
            for c in (fit + fit)[start:start + n]]


def board_has_jobs(ats, token):
    """Cheap existence check for a public board (no job content downloaded)."""
    urls = {"ashby": f"https://api.ashbyhq.com/posting-api/job-board/{token}",
            "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
            "lever": f"https://api.lever.co/v0/postings/{token}?mode=json&limit=1"}
    try:
        d = get_json(urls[ats], timeout=20)
    except Exception:
        return False
    return bool(d.get("jobs") if isinstance(d, dict) else d)


def discover_boards(companies, cache, today=None, limit=DISCOVER_PER_ROUND):
    """Guess each company's public job board from its YC slug and remember the answer in `cache`.
    cache: {slug: {"ats": str|None, "token": str|None, "name": str, "checked": "YYYY-MM-DD"}}. Returns new finds."""
    today = today or dt.date.today()
    due = []
    for c in yc_fit(companies):
        hit = cache.get(c["slug"])
        if hit and (hit.get("ats") or (today - dt.date.fromisoformat(hit["checked"])).days < RECHECK_DAYS):
            continue
        due.append(c)
    found = []

    def probe(c):
        s = c["slug"]
        for ats, tok in (("ashby", s), ("greenhouse", s.replace("-", "")), ("greenhouse", s), ("lever", s)):
            if TOKEN.match(tok) and board_has_jobs(ats, tok):
                return c, ats, tok
        return c, None, None

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        for c, ats, tok in pool.map(probe, due[:limit]):
            cache[c["slug"]] = {"ats": ats, "token": tok, "name": c["name"], "checked": today.isoformat()}
            if ats:
                found.append((ats, tok, "company"))
    return found


def cached_boards(cache):
    return [(v["ats"], v["token"], "company") for v in cache.values() if v.get("ats")]
