"""
Job sources — multi-ATS backbone + self-growing company registry.

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


def _clean(text, limit=12000):
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _norm(source, company, title, location, url, description, date_posted, ats, token):
    return {"source": source, "company": company, "title": title, "location": location,
            "url": url, "description": description, "date_posted": date_posted,
            "ats": ats, "token": token}


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
    return [_norm("ashby", company or token, j.get("title", ""), j.get("location", ""),
                  j.get("jobUrl", ""), _clean(j.get("descriptionPlain") or j.get("descriptionHtml", "")),
                  None, "ashby", token)
            for j in r.json().get("jobs", [])]


def from_smartrecruiters(token, company=None):
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT); r.raise_for_status()
    out = []
    for j in r.json().get("content", []):
        loc = j.get("location", {}) or {}
        out.append(_norm("smartrecruiters", company or token, j.get("name", ""),
                         f"{loc.get('city','')}, {loc.get('country','')}".strip(", "),
                         j.get("ref", "") or j.get("applyUrl", ""), "",
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

    queries = queries or ["software engineer", "new grad software engineer",
                          "backend engineer", "full stack engineer"]
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
                    ts, "adzuna", ""))
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

    return jobs
