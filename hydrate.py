"""
hydrate.py — fetch the FULL job description before the AI ranks a job.

Why this exists:
  Some connectors (SmartRecruiters, Workday, and the Adzuna aggregator) only return
  a short snippet or no description in their list endpoint. Greenhouse/Lever/Ashby
  return full text, but even those can be long. The AI fit-ranking is only as good
  as the text it sees, so before the paid LLM step we make sure each job that's
  about to be ranked has its complete posting -- including the parts that decide
  fit, like "requires security clearance" or "5+ years experience."

Key design choice for speed + cost:
  We hydrate ONLY the jobs that are about to go to the LLM (the top-N), not all
  ~50k. That's at most a few hundred small HTTP calls, run in parallel, so it adds
  seconds, not minutes, and $0 (plain HTTP, no LLM).

Each connector type has its own detail endpoint. If a fetch fails, we keep whatever
description we already had, so hydration can never make a job worse.
"""
import html
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (job-finder)"}
# Only bother fetching detail if the current description is shorter than this.
THIN = 600


def _clean(text, limit=20000):
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _detail_greenhouse(job):
    # job url like https://boards.greenhouse.io/<token>/jobs/<id>
    m = re.search(r"/jobs/(\d+)", job.get("url", ""))
    token = job.get("token")
    if not (m and token):
        return None
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{m.group(1)}"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT); r.raise_for_status()
    return _clean(r.json().get("content", ""))


def _detail_lever(job):
    m = re.search(r"/([0-9a-f-]{36})", job.get("url", ""))
    if not m:
        return None
    url = f"https://api.lever.co/v0/postings/{job.get('token')}/{m.group(1)}?mode=json"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT); r.raise_for_status()
    d = r.json()
    return _clean(d.get("descriptionPlain") or d.get("description", ""))


def _detail_smartrecruiters(job):
    m = re.search(r"/(\d{10,})", job.get("url", "")) or re.search(r"posting[s]?/([\w-]+)", job.get("url", ""))
    token = job.get("token")
    if not (m and token):
        return None
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{m.group(1)}"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT); r.raise_for_status()
    d = r.json()
    sections = (((d.get("jobAd") or {}).get("sections")) or {})
    parts = []
    for k in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        t = (sections.get(k) or {}).get("text", "")
        if t:
            parts.append(t)
    return _clean(" ".join(parts))


def _detail_workday(job):
    # token = tenant/site/wdN ; url = https://{tenant}.{wdN}.myworkdayjobs.com/en-US/{site}{externalPath}
    token = job.get("token", "")
    parts = token.split("/")
    if len(parts) < 2:
        return None
    tenant, site = parts[0], parts[1]
    m = re.search(r"myworkdayjobs\.com/en-US/[^/]+(/.+)$", job.get("url", ""))
    server = parts[2] if len(parts) > 2 else "wd1"
    if not m:
        return None
    ext = m.group(1)
    api = f"https://{tenant}.{server}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job{ext}"
    r = requests.post(api, json={}, headers={**HEADERS, "Accept": "application/json",
                                             "Content-Type": "application/json"}, timeout=TIMEOUT)
    r.raise_for_status()
    info = (r.json().get("jobPostingInfo") or {})
    return _clean(info.get("jobDescription", ""))


_DETAIL = {
    "greenhouse": _detail_greenhouse,
    "lever": _detail_lever,
    "smartrecruiters": _detail_smartrecruiters,
    "workday": _detail_workday,
}


def hydrate(jobs, max_workers=20, progress=None):
    """Fetch full descriptions in-place for jobs with thin text. Returns count filled.

    Safe: any failure leaves the existing description untouched. Only fetches when
    the current description is short, so already-full jobs cost nothing.
    """
    todo = [j for j in jobs
            if len((j.get("description") or "")) < THIN and j.get("ats") in _DETAIL]
    if not todo:
        return 0

    filled = 0

    def _one(j):
        fn = _DETAIL.get(j.get("ats"))
        try:
            full = fn(j)
            if full and len(full) > len(j.get("description") or ""):
                j["description"] = full
                return True
        except Exception:
            pass
        return False

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_one, j): j for j in todo}
        done = 0
        for fut in as_completed(futs):
            done += 1
            if fut.result():
                filled += 1
            if progress and done % 25 == 0:
                progress(done, len(todo))
    return filled
