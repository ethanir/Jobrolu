"""
Free heuristic scorer — ranks every job with ZERO API cost.

This is the cost fix. Instead of paying an LLM to rank thousands of jobs (most of
which are obvious non-matches), we score them all for free here, then send only
the top N to the LLM for the nuanced write-up. A full run drops from ~$70 to ~$1.

The score combines: skill overlap, target-title match, new-grad signal,
seniority penalty, location fit, and recency. It's deliberately simple and
transparent — you can read exactly why a job ranked where it did.
"""
import re
import time

SENIOR_RX = re.compile(
    r"\bsenior\b|\bstaff\b|\bprincipal\b|\blead\b|\bmanager\b|\bdirector\b|"
    r"\bvp\b|\bhead of\b|\bsr\.?\b|\bprincipal\b|\b(iii|iv|v)\b", re.I)
NEWGRAD_RX = re.compile(
    r"new.?grad|early career|entry.?level|\bassociate\b|\bgraduate\b|"
    r"\bjunior\b|university grad|\b(i|1)\b", re.I)
INTERN_RX = re.compile(r"\bintern\b|internship|\bco.?op\b", re.I)
SWE_RX = re.compile(
    r"software engineer|software developer|\bsde\b|\bswe\b|full.?stack|"
    r"back.?end|front.?end|web developer|application engineer", re.I)
# Hard-ish disqualifiers for a new grad: clearance, heavy experience requirements.
CLEARANCE_RX = re.compile(
    r"security clearance|ts/sci|\bts\b/\bsci\b|top secret|polygraph|"
    r"active clearance|government clearance|dod clearance", re.I)
SENIOR_YEARS_RX = re.compile(r"(\d{1,2})\+?\s*years", re.I)


def _word_in(skill, blob):
    """True only if skill appears as a whole token, not a substring.

    Fixes the bug where 'c' matched 'clearance' and 'go' matched inside other
    words, inflating scores with skills the job doesn't actually want.
    """
    return re.search(r"(?<![a-z0-9+#])" + re.escape(skill) + r"(?![a-z0-9+#])",
                     blob) is not None


def _flat_skills(profile):
    s = profile.get("skills", {}) or {}
    out = []
    for k in ("languages", "frameworks", "tools", "databases"):
        out += [str(x).lower() for x in (s.get(k) or []) if x]
    return out


def heuristic_score(job, skills, titles, locs, wants_intern=False):
    """Return (score:int, matched_skills:list). Pure function, no side effects.

    Sharper than a flat keyword count: it rewards the things that actually make a
    new-grad role a real fit (exact title match, new-grad signal, SWE role family,
    location) and penalizes the things that make it a non-fit (seniority, wrong
    role type), so the top-N forwarded to the LLM is genuinely high quality rather
    than a pile of look-alikes that all share a few common skills.
    """
    title = job.get("title", "") or ""
    tl = title.lower()
    blob = (title + " " + (job.get("description", "") or ""))[:12000].lower()

    score = 0

    # --- skill overlap: whole-word match only (no more 'c' matching 'clearance') ---
    matched = [s for s in skills if s and _word_in(s, blob)]
    n = len(matched)
    # first few matched skills are worth more; saturates so it can't dominate
    score += min(n, 3) * 6 + max(0, min(n - 3, 6)) * 2     # up to +30

    # --- title family: is this even a software-engineering role? ---
    if SWE_RX.search(title):
        score += 16
    if any(t in tl for t in titles):                       # matches a target title
        score += 22
    # exact-ish target title at the start of the title = very strong signal
    if any(tl.startswith(t) for t in titles):
        score += 10

    # --- new-grad / seniority: the biggest fit signals for this user ---
    if NEWGRAD_RX.search(title):
        score += 24
    if SENIOR_RX.search(title):
        score -= 55                                        # hard down-weight

    # --- intern handling: only reward intern roles if the user wants them ---
    if INTERN_RX.search(title):
        score += 16 if wants_intern else -20

    # --- hard-ish disqualifiers a new grad can't satisfy ---
    if CLEARANCE_RX.search(blob):
        score -= 60      # security clearance / TS-SCI / poly: effectively a non-fit
    ym = SENIOR_YEARS_RX.search(blob)
    if ym:
        try:
            yrs = int(ym.group(1))
            if yrs >= 8:
                score -= 40
            elif yrs >= 5:
                score -= 25
        except ValueError:
            pass

    # --- location fit ---
    loc = (job.get("location", "") or "").lower()
    if "remote" in loc or any(l in loc for l in locs):
        score += 12

    # --- recency ---
    dp = job.get("date_posted")
    if dp:
        days = (time.time() - dp) / 86400
        if days < 14:
            score += 10
        elif days < 45:
            score += 5

    return score, matched


def rank_free(jobs, profile):
    """Attach a free '_score' + '_matched' to each job and return sorted, best-first."""
    skills = _flat_skills(profile)
    titles = [t.lower() for t in (profile.get("target_titles") or [])] or \
             ["software engineer", "developer", "full stack", "backend"]
    pref = profile.get("preferences") or {}
    locs = [l.lower() for l in (pref.get("locations") or [])]
    # does the user actually want internships? (infer from target titles)
    wants_intern = any("intern" in t for t in titles)

    for j in jobs:
        s, m = heuristic_score(j, skills, titles, locs, wants_intern)
        j["_score"] = s
        j["_matched"] = m
    jobs.sort(key=lambda j: j["_score"], reverse=True)
    return jobs


def heuristic_fit(job):
    """Turn the free score into a fit dict (so un-LLM'd jobs still display).

    IMPORTANT: the heuristic NEVER awards 'strong'. It only reads keywords, so it
    can't be trusted to confirm a strong fit (it can't see disqualifiers like a
    required security clearance). The best a keyword-only job can show is
    'possible' -- only the LLM, which reads the full posting, can mark 'strong'.
    That keeps every green 'Strong fit' meaningful.
    """
    s = job.get("_score", 0)
    # capped at 'possible' on purpose; 'strong' is reserved for LLM-verified jobs
    tier = "possible" if s >= 40 else "skip"
    matched = job.get("_matched", [])
    return {
        "score": max(0, min(100, s)),
        "tier": tier,
        "reasons": [f"Keyword pre-match: {len(matched)} of your skills appear in the text"
                    + (f" ({', '.join(matched[:5])})" if matched else "")
                    + ". Not yet verified by the AI on the full description."],
        "hard_disqualifiers": [],
        "matched_skills": matched,
        "missing_skills": [],
    }
