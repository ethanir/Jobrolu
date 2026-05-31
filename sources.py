"""
Job sources - multi-ATS backbone + self-growing company registry.

Each connector returns NORMALIZED job dicts:
    {
      "source": str, "company": str, "title": str, "location": str,
      "url": str, "description": str, "date_posted": int|None,
      "ats": str, "token": str
    }

THE KEY IDEA: every apply URL contains the company's ATS token. So every job we
ingest teaches us new companies (see registry.discover_from_jobs). The registry
compounds over time -- that's the asset.

ToS-friendly sources only. No LinkedIn/Handshake mass scraping (bans users).
"""
import html
import os
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (job-finder)"}

# Multi-field switch (mirrors prefilter.MULTIFIELD). ON by default: the keyword-search
# sources fetch across professional fields, not tech only, and USAJOBS is pulled in
# pull_all. Set MULTIFIELD=off to revert to the tech-only behavior.
MULTIFIELD = os.environ.get("MULTIFIELD", "on").strip().lower() in ("1", "true", "on", "yes")

# Keyword sets for the search-based sources (Adzuna, USAJOBS). The tech set is the default
# and reproduces prior behavior; the broad set adds professional, resume-driven non-tech
# fields so one query pass can serve many kinds of candidate.
_TECH_QUERIES = ["software engineer", "backend engineer", "full stack engineer",
                 "frontend engineer", "mobile engineer", "data engineer",
                 "data scientist", "machine learning engineer", "devops engineer",
                 "security engineer", "data analyst"]
_BROAD_QUERIES = _TECH_QUERIES + [
    "registered nurse", "nurse practitioner", "physical therapist", "pharmacist",
    "accountant", "financial analyst", "auditor", "controller",
    "marketing manager", "digital marketing", "content strategist",
    "sales representative", "account executive", "business development",
    "human resources", "recruiter", "operations manager", "supply chain analyst",
    "project manager", "program manager", "business analyst", "management consultant",
    "attorney", "paralegal", "teacher", "professor",
    "graphic designer", "executive assistant", "customer success manager"]


def _clean(text, limit=12000):
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


# --- salary parsed from description text (pay-transparency disclosures) ----------
# A growing share of postings state a pay range in the body (CA, CO, NY, WA, and other
# pay-transparency laws). We already keep the full text, so we read a range out of it for
# free, with no model call. Deliberately conservative: ranges only, a salary/pay context
# word must sit nearby, and anything near bonus/equity/referral wording is rejected, so a
# sign-on bonus never reads as a salary. Off-switch: env PARSE_SALARY_FROM_TEXT=off.
PARSE_SALARY_FROM_TEXT = os.environ.get(
    "PARSE_SALARY_FROM_TEXT", "on").strip().lower() in ("1", "true", "on", "yes")

# Dollar range, e.g. "$120,000 - $160,000", "$120K-$160K", "$58.50 to $72/hour". The
# separator class includes the unicode dashes real postings use, written as escapes so
# this file stays ASCII.
_PAY_RANGE = re.compile(
    r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?([kK])?\s?(?:-|to|\u2013|\u2014|\u2212)\s?\$?\s?(\d[\d,]*(?:\.\d+)?)\s?([kK])?")
# A salary/pay word must appear near the match, or we skip it.
_PAY_CTX = re.compile(
    r"(salary|base pay|base salary|pay range|compensation|annual|annually|per\s+year|/\s?yr|/\s?year|per\s+hour|hourly|/\s?hr|/\s?hour|an?\s+hour)", re.I)
# If any of these sit near the match it is not base pay, so skip it.
_PAY_REJECT = re.compile(
    r"(bonus|sign[\s-]?on|referral|equity|stock|401|relocation|tuition|commission|per\s+diem|stipend|budget|revenue|grant|funding|portfolio|damages)", re.I)
_PAY_HOURLY = re.compile(r"(per\s+hour|hourly|/\s?hr|/\s?hour|an?\s+hour)", re.I)


def _salary_from_text(text):
    """Read a salary range out of a job description, or None when there is no clear one.
    Ranges only; a salary/pay context word must sit within ~80 chars; matches near bonus,
    equity, or referral wording are skipped so a sign-on amount never reads as pay."""
    if not text or len(text) < 10:
        return None
    try:
        for m in _PAY_RANGE.finditer(text):
            lead = text[max(0, m.start() - 25): m.start()]
            tail = text[m.end(): m.end() + 12]
            if _PAY_REJECT.search(lead) or _PAY_REJECT.search(tail):
                continue
            ctx = text[max(0, m.start() - 80): m.end() + 80]
            if not _PAY_CTX.search(ctx):
                continue
            lo = float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
            hi = float(m.group(3).replace(",", "")) * (1000 if m.group(4) else 1)
            if _PAY_HOURLY.search(ctx):
                if not (5 <= lo <= 500 and 5 <= hi <= 500):
                    continue
                period = "hour"
            else:
                if not (10000 <= lo <= 5000000 and 10000 <= hi <= 5000000):
                    continue
                period = "year"
            return _salary(lo, hi, "USD", period, estimated=False)
    except Exception:
        return None
    return None


# --- normalizers for salary/dates an AI returns from a posting -------------------
# Both the owner ranker (rank.py) and the bring-your-own-AI import (server.py) read
# salary and dates out of the description the model is already given. These bound and
# validate that output so a malformed or hallucinated value is dropped, not shown.
def salary_from_ai(v):
    """Normalize a salary an AI returned (a {min,max,period[,currency]} dict, or a
    text range) into the salary dict the UI reads, or None. Bounds-checked, so a
    nonsense or out-of-range figure is dropped. From the posting text, never estimated."""
    if v is None:
        return None
    if isinstance(v, str):
        return _salary_from_text(v)
    if not isinstance(v, dict):
        return None
    period = str(v.get("period") or "year").lower()
    period = "hour" if ("hour" in period or period == "hr") else "year"
    s = _salary(v.get("min"), v.get("max"), v.get("currency") or "USD", period, estimated=False)
    if not s:
        return None
    lo, hi = s["min"], s["max"]
    if s["period"] == "hour":
        if not (5 <= lo <= 500 and 5 <= hi <= 500):
            return None
    elif not (10000 <= lo <= 5000000 and 10000 <= hi <= 5000000):
        return None
    return s


def ai_posted_ts(s):
    """An ISO date (YYYY-MM-DD) an AI read from a posting to epoch seconds, or None.
    Used to fill a posting-age label only when the source itself gave no date."""
    if not isinstance(s, str):
        return None
    try:
        import datetime
        d = datetime.datetime.strptime(s.strip()[:10], "%Y-%m-%d")
        return int(d.replace(tzinfo=datetime.timezone.utc).timestamp())
    except Exception:
        return None


def ai_close_date(s):
    """Validate an ISO close/deadline date (YYYY-MM-DD) an AI read, returning it
    unchanged or None. Kept as a short string for compact display."""
    if not isinstance(s, str):
        return None
    try:
        import datetime
        v = s.strip()[:10]
        datetime.datetime.strptime(v, "%Y-%m-%d")
        return v
    except Exception:
        return None


def _norm(source, company, title, location, url, description, date_posted, ats, token, salary=None):
    d = {"source": source, "company": company, "title": title, "location": location,
         "url": url, "description": description, "date_posted": date_posted,
         "ats": ats, "token": token}
    if salary:
        d["salary"] = salary
    elif description and PARSE_SALARY_FROM_TEXT:
        s = _salary_from_text(description)
        if s:
            d["salary"] = s
    return d


def _salary(mn, mx, currency="USD", period="year", estimated=False):
    """Normalize a pay figure into the dict the UI reads, or None when there is
    nothing usable. min/max are numbers; period is "year" or "hour"; estimated
    marks a guess (e.g. an aggregator's predicted pay) the UI can hide or label.
    Returns None on anything unparseable, so callers can attach it without guarding."""
    def num(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None
    lo, hi = num(mn), num(mx)
    if lo is None and hi is None:
        return None
    if lo is None:
        lo = hi
    if hi is None:
        hi = lo
    if hi < lo:
        lo, hi = hi, lo
    return {"min": lo, "max": hi, "currency": (currency or "USD"),
            "period": (period or "year"), "estimated": bool(estimated)}


def _ashby_salary(comp):
    """Pull employer-stated pay from an Ashby posting's compensation object. Ashby
    returns a structured breakdown (compensationTiers -> components); boards that do
    not publish pay return empty fields, so this returns None then. Reads the Salary
    component only (ignores equity/bonus). Employer-stated, so never estimated."""
    if not isinstance(comp, dict):
        return None
    for tier in (comp.get("compensationTiers") or []):
        if not isinstance(tier, dict):
            continue
        for c in (tier.get("components") or []):
            if not isinstance(c, dict):
                continue
            if str(c.get("compensationType") or "").lower() == "salary":
                interval = str(c.get("interval") or "").upper()
                period = "hour" if "HOUR" in interval else "year"
                s = _salary(c.get("minValue"), c.get("maxValue"),
                            c.get("currencyCode") or "USD", period, estimated=False)
                if s:
                    return s
    return None


def from_greenhouse(token, company=None):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT); r.raise_for_status()
    return [_norm("greenhouse", company or token, j.get("title", ""),
                  (j.get("location") or {}).get("name", ""), j.get("absolute_url", ""),
                  _clean(j.get("content", "")), None, "greenhouse", token)
            for j in r.json().get("jobs", [])]


def from_lever(token, company=None):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT); r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        out.append(_norm("lever", company or token, j.get("text", ""),
                         cats.get("location", ""), j.get("hostedUrl", ""),
                         _clean(j.get("descriptionPlain") or j.get("description", "")),
                         int(j.get("createdAt", 0) / 1000) or None, "lever", token))
    return out


def from_ashby(token, company=None):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT); r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append(_norm("ashby", company or token, j.get("title", ""), j.get("location", ""),
                         j.get("jobUrl", ""),
                         _clean(j.get("descriptionPlain") or j.get("descriptionHtml", "")),
                         None, "ashby", token,
                         salary=_ashby_salary(j.get("compensation"))))
    return out


def from_smartrecruiters(token, company=None):
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT); r.raise_for_status()
    out = []
    for j in r.json().get("content", []):
        loc = j.get("location", {}) or {}
        pid = j.get("id", "") or ""
        # The posting 'ref' is the API self-link (returns JSON, not a page). Build the
        # public careers URL so "Open the posting" lands on the real listing.
        human = f"https://jobs.smartrecruiters.com/{token}/{pid}" if pid \
            else (j.get("applyUrl", "") or j.get("ref", ""))
        out.append(_norm("smartrecruiters", company or token, j.get("name", ""),
                         f"{loc.get('city','')}, {loc.get('country','')}".strip(", "),
                         human, "",
                         None, "smartrecruiters", token))
    return out


def from_recruitee(token, company=None):
    url = f"https://{token}.recruitee.com/api/offers/"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT); r.raise_for_status()
    return [_norm("recruitee", company or token, j.get("title", ""),
                  j.get("location", "") or j.get("city", ""), j.get("careers_url", ""),
                  _clean(j.get("description", "")), None, "recruitee", token)
            for j in r.json().get("offers", [])]


def from_workable(token, company=None):
    url = f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT); r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = j.get("location", {})
        out.append(_norm("workable", company or token, j.get("title", ""),
                         loc.get("city", "") if isinstance(loc, dict) else "",
                         j.get("application_url", "") or j.get("url", ""),
                         _clean(j.get("description", "")), None, "workable", token))
    return out


def from_workday(token, company=None):
    """Workday CXS connector. Public job boards, no auth, POST + offset pagination.

    Workday needs three parts (tenant, site, data-center server), so the registry
    token is stored as 'tenant/site/wdN' (e.g. 'nvidia/NVIDIAExternalCareerSite/wd5').
    If the server part is omitted, we try the common ones.
    """
    parts = (token or "").split("/")
    if len(parts) < 2:
        return []
    tenant, site = parts[0], parts[1]
    servers = [parts[2]] if len(parts) > 2 and parts[2] else ["wd1", "wd3", "wd5", "wd2", "wd103"]

    def _try(server):
        base = f"https://{tenant}.{server}.myworkdayjobs.com"
        api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
        hdr = {**HEADERS, "Accept": "application/json", "Content-Type": "application/json",
               "Referer": f"{base}/en-US/{site}"}
        out, offset = [], 0
        for _ in range(20):                       # cap pages so one company can't run away
            payload = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""}
            r = requests.post(api, json=payload, headers=hdr, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            postings = data.get("jobPostings", [])
            if not postings:
                break
            for j in postings:
                ext = j.get("externalPath", "") or ""
                out.append(_norm(
                    "workday", company or tenant, j.get("title", ""),
                    j.get("locationsText", "") or "",
                    f"{base}/en-US/{site}{ext}",
                    _clean(j.get("bulletFields", [""])[0] if j.get("bulletFields") else ""),
                    None, "workday", token))
            total = data.get("total", 0)
            offset += 20
            if offset >= total:
                break
        return out

    last_err = None
    for server in servers:
        try:
            jobs = _try(server)
            if jobs:
                return jobs
        except Exception as e:
            last_err = e
            continue
    if last_err:
        raise last_err
    return []


def from_adzuna(queries=None, country="us", pages=5, per_page=50):
    """Adzuna aggregator. Keyword-search across many job boards at once.

    Different shape from the ATS connectors: instead of one company per call, it
    queries by keyword and returns jobs from across Adzuna's index. Needs free
    credentials in env: ADZUNA_APP_ID and ADZUNA_APP_KEY (register at
    developer.adzuna.com). Returns [] quietly if keys are absent, so the pipeline
    runs fine without it.
    """
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        return []

    queries = queries or (_BROAD_QUERIES if MULTIFIELD else _TECH_QUERIES)
    out = []
    for what in queries:
        for page in range(1, pages + 1):
            url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
            params = {"app_id": app_id, "app_key": app_key,
                      "results_per_page": per_page, "what": what,
                      "content-type": "application/json"}
            try:
                r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
                r.raise_for_status()
                results = r.json().get("results", [])
            except Exception:
                break          # stop this query on error (rate limit etc), keep others
            if not results:
                break
            for j in results:
                created = j.get("created", "")
                ts = None
                if created:
                    try:
                        import datetime
                        ts = int(datetime.datetime.fromisoformat(
                            created.replace("Z", "+00:00")).timestamp())
                    except Exception:
                        ts = None
                out.append(_norm(
                    "adzuna", (j.get("company") or {}).get("display_name", "") or "Unknown",
                    j.get("title", ""), (j.get("location") or {}).get("display_name", ""),
                    j.get("redirect_url", ""), _clean(j.get("description", "")),
                    ts, "adzuna", "",
                    salary=_salary(j.get("salary_min"), j.get("salary_max"), "USD", "year",
                                   estimated=str(j.get("salary_is_predicted")) == "1")))
    return out


def from_usajobs(queries=None, pages=2, per_page=250):
    """USAJOBS aggregator: open US federal jobs across ALL fields (nursing, law, finance,
    trades, science, admin, IT, and more). Keyword-search, like Adzuna. Needs a free key
    plus the email you registered with, in env: USAJOBS_API_KEY and USAJOBS_EMAIL (register
    at developer.usajobs.gov). Returns [] quietly if either is missing, so the pipeline runs
    fine without it."""
    key = os.environ.get("USAJOBS_API_KEY")
    email = os.environ.get("USAJOBS_EMAIL")
    if not (key and email):
        return []
    queries = queries or _BROAD_QUERIES
    hdr = {**HEADERS, "Host": "data.usajobs.gov", "User-Agent": email,
           "Authorization-Key": key, "Accept": "application/json"}
    out = []
    for what in queries:
        for page in range(1, pages + 1):
            params = {"Keyword": what, "ResultsPerPage": per_page, "Page": page}
            try:
                r = requests.get("https://data.usajobs.gov/api/search",
                                 params=params, headers=hdr, timeout=TIMEOUT)
                r.raise_for_status()
                items = r.json().get("SearchResult", {}).get("SearchResultItems", []) or []
            except Exception:
                break          # stop this query on error (rate limit etc), keep the rest
            if not items:
                break
            for it in items:
                d = it.get("MatchedObjectDescriptor", {}) or {}
                loc = d.get("PositionLocationDisplay", "") or ""
                summary = d.get("QualificationSummary", "") or \
                    ((d.get("UserArea") or {}).get("Details") or {}).get("JobSummary", "")
                pub = d.get("PublicationStartDate", "") or ""
                ts = None
                if pub:
                    try:
                        import datetime
                        ts = int(datetime.datetime.fromisoformat(
                            pub.replace("Z", "+00:00")).timestamp())
                    except Exception:
                        ts = None
                rem = d.get("PositionRemuneration") or []
                rem0 = rem[0] if rem else {}
                ival = str(rem0.get("RateIntervalCode") or "").upper()
                usal = _salary(rem0.get("MinimumRange"), rem0.get("MaximumRange"),
                               "USD", "hour" if ival in ("PH", "SH") else "year",
                               estimated=False)
                out.append(_norm(
                    "usajobs", d.get("OrganizationName", "") or "U.S. Government",
                    d.get("PositionTitle", ""), loc, d.get("PositionURI", ""),
                    _clean(summary), ts, "", "", salary=usal))
            if len(items) < per_page:
                break
    return out


def from_simplify_repo():
    """Curated new-grad list. Also a great seed for the registry (URLs -> tokens)."""
    url = ("https://raw.githubusercontent.com/SimplifyJobs/"
           "New-Grad-Positions/dev/.github/scripts/listings.json")
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT); r.raise_for_status()
    out = []
    for j in r.json():
        if not (j.get("active") and j.get("is_visible")):
            continue
        out.append(_norm("simplify_repo", j.get("company_name", ""), j.get("title", ""),
                         "; ".join(j.get("locations", [])), j.get("url", ""), "",
                         j.get("date_posted"), "", ""))
    return out


def from_vansh_repo():
    """Second free, daily-updated new-grad list (vanshb03/New-Grad-2027). Same
    schema as the Simplify repo (company_name/title/locations/url/date_posted/
    active/is_visible), US + Canada + Remote, SWE/quant/PM. Pure additional
    coverage; pull_all dedupes it against everything else so overlaps are dropped."""
    url = ("https://raw.githubusercontent.com/vanshb03/"
           "New-Grad-2027/dev/.github/scripts/listings.json")
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT); r.raise_for_status()
    out = []
    for j in r.json():
        if not (j.get("active") and j.get("is_visible")):
            continue
        out.append(_norm("vansh_repo", j.get("company_name", ""), j.get("title", ""),
                         "; ".join(j.get("locations", [])), j.get("url", ""), "",
                         j.get("date_posted"), "", ""))
    return out


ATS = {"greenhouse": from_greenhouse, "lever": from_lever, "ashby": from_ashby,
       "smartrecruiters": from_smartrecruiters, "recruitee": from_recruitee,
       "workable": from_workable, "workday": from_workday}


def pull_all(companies, include_repo=True, max_workers=20):
    """Pull every company's jobs in parallel. ~20x faster than sequential."""
    jobs = []

    def _pull(c):
        fn = ATS.get(c["ats"])
        if not fn:
            return c, None, "unknown ats"
        try:
            return c, fn(c["token"], c["name"]), None
        except Exception as e:
            return c, None, str(e)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_pull, c) for c in companies]
        done = 0
        for fut in as_completed(futures):
            c, got, err = fut.result()
            done += 1
            if err:
                continue  # quietly skip dead boards; they're common and noisy
            jobs.extend(got)
            if done % 25 == 0:
                print(f"  pulled {done}/{len(companies)} companies, {len(jobs)} jobs so far")

    if include_repo:
        try:
            got = from_simplify_repo(); jobs.extend(got)
            print(f"  {'SimplifyRepo':<18} {len(got):>4} (new-grad list)")
        except Exception as e:
            print(f"  ! simplify repo failed: {e}")

        # Second free new-grad list (vanshb03). Dedup against what we have by
        # (company+title) and url, so roles already pulled above aren't doubled.
        try:
            vg = from_vansh_repo()
            if vg:
                seen_ct = {((j.get("company") or "").lower().strip(),
                            (j.get("title") or "").lower().strip()) for j in jobs}
                seen_url = {j.get("url") for j in jobs if j.get("url")}
                fresh = [j for j in vg
                         if ((j.get("company") or "").lower().strip(),
                             (j.get("title") or "").lower().strip()) not in seen_ct
                         and j.get("url") not in seen_url]
                jobs.extend(fresh)
                print(f"  {'VanshRepo':<18} {len(fresh):>4} (new-grad list, after dedup)")
        except Exception as e:
            print(f"  ! vansh repo failed: {e}")

    # Adzuna aggregator (only runs if ADZUNA_APP_ID / ADZUNA_APP_KEY are set)
    try:
        ad = from_adzuna()
        if ad:
            # dedup against what we already have, by (company+title) and by url
            seen_ct = {((j.get("company") or "").lower().strip(),
                        (j.get("title") or "").lower().strip()) for j in jobs}
            seen_url = {j.get("url") for j in jobs if j.get("url")}
            fresh = [j for j in ad
                     if ((j.get("company") or "").lower().strip(),
                         (j.get("title") or "").lower().strip()) not in seen_ct
                     and j.get("url") not in seen_url]
            jobs.extend(fresh)
            print(f"  {'Adzuna':<18} {len(fresh):>4} (aggregator, after dedup)")
    except Exception as e:
        print(f"  ! adzuna failed: {e}")

    # USAJOBS (US federal, every field) - only in multi-field mode, only when keyed.
    if MULTIFIELD:
        try:
            uj = from_usajobs()
            if uj:
                seen_ct = {((j.get("company") or "").lower().strip(),
                            (j.get("title") or "").lower().strip()) for j in jobs}
                seen_url = {j.get("url") for j in jobs if j.get("url")}
                fresh = [j for j in uj
                         if ((j.get("company") or "").lower().strip(),
                             (j.get("title") or "").lower().strip()) not in seen_ct
                         and j.get("url") not in seen_url]
                jobs.extend(fresh)
                print(f"  {'USAJOBS':<18} {len(fresh):>4} (federal, after dedup)")
        except Exception as e:
            print(f"  ! usajobs failed: {e}")

    return jobs
