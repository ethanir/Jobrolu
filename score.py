"""
Free heuristic scorer - ranks every job for the user with ZERO API cost.

This is the matching core. It scores the whole pool against the user's profile so
the best-fitting roles rise to the top, and only the top slice ever needs a paid
LLM read. It is deliberately transparent: every point a job earns or loses maps to
a concrete, readable reason, so the user can see exactly why a role ranked where it
did.

Design choices that make the match actually good:
  * Profile-derived, not persona-hardcoded. The desired seniority and the role
    family come from the user's own resume/targets, so it works for a new grad, a
    senior engineer, or an adjacent role equally. It does NOT assume "new grad SWE".
  * Title fit is the dominant signal. What a role IS matters more than which stray
    keywords it happens to contain, so a real title/role match outweighs a pile of
    coincidental skill hits.
  * Honest about thin postings. Some job boards (e.g. SmartRecruiters, Workday) omit
    the description in their public feed, so a missing description is not proof the
    job is bad. We mildly down-weight a role we can't fully read and flag it ("open
    the posting to confirm") rather than burying it.
  * 'strong' is never awarded here. Keyword reading can't see real disqualifiers, so
    the best a free score shows is 'possible'; only an LLM that reads the full
    posting earns 'strong'. That keeps every green "Strong fit" meaningful.
"""
import re
import time

# --- title / level signals -------------------------------------------------
SENIOR_RX = re.compile(
    r"\bsenior\b|\bstaff\b|\bprincipal\b|\blead\b|\bmanager\b|\bdirector\b|"
    r"\bvp\b|\bhead of\b|\barchitect\b|\bsr\.?\b|\b(?:iii|iv|v)\b|\b(?:3|4)\b", re.I)
ENTRY_RX = re.compile(
    r"new.?grad|early.career|entry.?level|\bassociate\b|\bgraduate\b|\bjunior\b|"
    r"\bjr\.?\b|university grad|\bentry\b|\b(?:i|1)\b", re.I)
INTERN_RX = re.compile(r"\bintern\b|internship|\bco.?op\b", re.I)
SWE_RX = re.compile(
    r"software engineer|software developer|\bsde\b|\bswe\b|full.?stack|back.?end|"
    r"front.?end|web developer|application engineer|platform engineer|"
    r"\bprogrammer\b", re.I)

# --- hard-ish disqualifiers a keyword pass CAN see safely -------------------
CLEARANCE_RX = re.compile(
    r"security clearance|ts/sci|\bts\b/\bsci\b|top secret|polygraph|"
    r"active clearance|government clearance|dod clearance", re.I)
YEARS_RX = re.compile(r"(\d{1,2})\s*\+?\s*years", re.I)
PHD_RX = re.compile(
    r"(?:\bph\.?\s?d\.?\b|\bdoctorate\b)[^.]{0,30}\brequired\b|"
    r"\brequires?\b[^.]{0,30}(?:\bph\.?\s?d\.?\b|\bdoctorate\b)", re.I)

# Generic job words that, on their own, do NOT mean two roles are the same kind of
# role. Distinctive words (e.g. "data", "security", "ios") are what identify a role.
_GENERIC_TITLE = {
    "engineer", "engineering", "developer", "development", "analyst", "specialist",
    "associate", "senior", "junior", "staff", "lead", "principal", "manager",
    "new", "grad", "graduate", "entry", "level", "intern", "internship", "coop",
    "i", "ii", "iii", "iv", "v", "1", "2", "3", "jr", "sr", "of", "the", "a", "an",
    "and", "or", "for", "in", "to", "with", "remote", "hybrid", "onsite",
}


def _word_in(skill, blob):
    """True only if skill appears as a whole token (so 'c' won't match 'clearance')."""
    return re.search(r"(?<![a-z0-9+#])" + re.escape(skill) + r"(?![a-z0-9+#])",
                     blob) is not None


def _flat_skills(profile):
    """Flatten the profile's skills (a dict of lists) into a de-duped lowercase list.
    A malformed profile whose skills is a plain list will raise here; that is
    intentional: the server catches it and degrades to an empty personalized feed
    rather than showing a feed scored from a corrupt profile."""
    s = profile.get("skills", {}) or {}
    out = []
    for k in ("languages", "frameworks", "tools", "databases", "cloud", "other"):
        out += [str(x).lower() for x in (s.get(k) or []) if x]
    seen, uniq = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def _tokens(text):
    return [t for t in re.split(r"[^a-z0-9+#]+", (text or "").lower()) if t]


def _distinctive(tokens):
    """The tokens in a title that actually identify the KIND of role."""
    return [t for t in tokens if t not in _GENERIC_TITLE and len(t) > 1]


def _job_level(title):
    """A job's seniority from its title: 'senior' | 'entry' | 'intern' | 'unspecified'.
    An unmarked title is 'unspecified' (a plain "Software Engineer" is often
    new-grad-friendly), so it is never penalized as if it were senior."""
    if INTERN_RX.search(title):
        return "intern"
    if SENIOR_RX.search(title):
        return "senior"
    if ENTRY_RX.search(title):
        return "entry"
    return "unspecified"


def desired_level(profile):
    """The level the USER is targeting, inferred from their own target titles and
    years of experience. This is what lets the scorer fit anyone, not just a new
    grad. Returns 'intern' | 'entry' | 'mid' | 'senior'."""
    titles = " ".join(str(t).lower() for t in (profile.get("target_titles") or []))
    yrs = profile.get("years_experience")
    try:
        yrs = float(yrs) if yrs is not None and yrs != "" else None
    except (TypeError, ValueError):
        yrs = None
    if INTERN_RX.search(titles):
        return "intern"
    if SENIOR_RX.search(titles):      # the user's OWN words win over a year count
        return "senior"
    if ENTRY_RX.search(titles):
        return "entry"
    if yrs is not None:
        if yrs <= 1:
            return "entry"
        if yrs <= 4:
            return "mid"
        return "senior"
    return "mid"        # no signal at all: assume mid, don't hard-penalize extremes


# desired-level x job-level -> (points, concern phrase or None). Concern phrases are
# deliberately worded so the UI classifies them under "Worth knowing".
_ALIGN = {
    "intern": {
        "intern": (20, None), "entry": (6, None), "unspecified": (0, None),
        "senior": (-40, "Senior-level role, not an internship"),
    },
    "entry": {
        "entry": (18, None), "unspecified": (4, None),
        "intern": (-22, "Internship, not a full-time role"),
        "senior": (-45, "Senior-level title; may not fit an early-career target"),
    },
    "mid": {
        "unspecified": (10, None), "mid": (12, None), "entry": (-4, None),
        "intern": (-25, "Internship, not a full-time role"),
        "senior": (-12, "Senior-level title; may need more experience"),
    },
    "senior": {
        "senior": (18, None), "unspecified": (4, None),
        "entry": (-28, "Junior-level role; may not fit a senior target"),
        "intern": (-40, "Internship, not a senior role"),
    },
}


def _title_fit(job_title, target_titles):
    """How well a job's title matches what the user wants. The dominant signal:
    being the right KIND of role matters more than scattered keyword hits.
    Returns (points, matched_target or None)."""
    tl = job_title.lower()
    jd = set(_distinctive(_tokens(job_title)))
    best, hit = 0, None
    for t in target_titles:
        t = (t or "").strip().lower()
        if not t:
            continue
        if t in tl:                                   # full target phrase present
            pts = 34
        else:
            tdist = set(_distinctive(_tokens(t)))
            overlap = jd & tdist
            if tdist and overlap:                     # share the identifying words
                frac = len(overlap) / len(tdist)
                pts = int(round(8 + 22 * frac))       # 8..30 by coverage
            else:
                pts = 0
        if pts > best:
            best, hit = pts, (t if pts > 0 else None)
    return min(best, 34), hit


def _looks_swe(target_titles, skills):
    """Does the USER look like a software engineer? Used only to let SWE-family
    synonyms (SWE/SDE/backend/full-stack/developer), which share no title tokens,
    still cohere. Other families rely on plain distinctive-token overlap."""
    if any(SWE_RX.search(t or "") for t in target_titles):
        return True
    swe_skills = {"python", "java", "javascript", "typescript", "c++", "c#", "go",
                  "rust", "react", "node", "node.js", "django", "spring", "kubernetes"}
    return sum(1 for s in skills if s in swe_skills) >= 2


def heuristic_score(job, skills, titles, locs, desired="entry",
                    remote_ok=True, onsite_ok=True, user_years=0,
                    user_is_swe=True):
    """Return (score:int, matched:list, why:list[str], flags:list[str]).

    Pure function, no side effects. `why` are positive reasons (shown under "Why you
    fit"); `flags` are concerns (shown under "Worth knowing"). The reason strings are
    the transparency layer: the score is just their weights summed."""
    title = job.get("title", "") or ""
    desc = (job.get("description", "") or "")
    blob = (title + " " + desc)[:12000].lower()
    why, flags = [], []
    score = 0

    # --- title / role fit: the dominant signal -----------------------------
    tpts, thit = _title_fit(title, titles)
    if tpts == 0 and user_is_swe and SWE_RX.search(title):
        tpts, thit = 16, "software engineering"      # SWE-family synonym fallback
    score += tpts
    if tpts >= 30:
        why.append(f"Closely matches your target: {thit}")
    elif tpts >= 12:
        why.append(f"In your target area: {thit}")

    # --- seniority alignment (derived from the user's profile) -------------
    jlevel = _job_level(title)
    pts, concern = _ALIGN.get(desired, _ALIGN["entry"]).get(jlevel, (0, None))
    score += pts
    if pts >= 14:
        why.append({"intern": "Internship level matches your search",
                    "entry": "New-grad / early-career level",
                    "mid": "Mid-level role fits your experience",
                    "senior": "Senior-level role matches your target"}.get(desired,
                    "Seniority fits your target"))
    elif concern:
        flags.append(concern)

    # --- skill overlap (whole-word; saturates so it can't dominate) --------
    matched = [s for s in skills if s and _word_in(s, blob)]
    n = len(matched)
    score += min(n, 3) * 6 + max(0, min(n - 3, 5)) * 2     # up to 18 + 10 = 28
    if n:
        shown = ", ".join(matched[:5])
        why.append(f"{n} of your skills appear: {shown}")

    # --- location fit (remote / preferred city vs the user's preferences) --
    loc = (job.get("location", "") or "").lower()
    is_remote = "remote" in loc
    in_pref = bool(locs) and any(l in loc for l in locs)
    if is_remote and remote_ok:
        score += 12
        why.append("Remote-friendly")
    elif in_pref:
        score += 12
        why.append("In your preferred location")
    elif is_remote and not remote_ok:
        score += 2
    elif locs and loc and not is_remote and not in_pref:
        if remote_ok and not onsite_ok:
            score -= 10
            flags.append("Onsite, not in your preferred locations")
        else:
            score -= 3

    # --- recency -----------------------------------------------------------
    dp = job.get("date_posted")
    if dp:
        try:
            days = (time.time() - float(dp)) / 86400
            if days < 14:
                score += 8
                why.append("Posted in the last two weeks")
            elif days < 45:
                score += 4
        except (TypeError, ValueError):
            pass

    # --- description quality: a role we can't read is less certain ---------
    # NOTE: empty here often just means the job board omits descriptions in its
    # feed, so this is a MILD down-weight + a flag, never a knockout.
    dlen = len(desc.strip())
    if dlen >= 400:
        score += 5
    elif dlen >= 160:
        pass
    elif dlen > 0:
        score -= 5
        flags.append("Brief listing here; details unclear until you open the posting")
    else:
        score -= 8
        flags.append("No description in this feed; open the posting to confirm details")

    # --- hard-ish disqualifiers --------------------------------------------
    if CLEARANCE_RX.search(blob):
        score -= 55
        flags.append("Security clearance required, a likely gap")
    ym = YEARS_RX.search(blob)
    if ym:
        try:
            yrs_req = int(ym.group(1))
            gap = yrs_req - (user_years or 0)
            if gap >= 5:
                score -= 40
                flags.append(f"May need {yrs_req}+ years of experience")
            elif gap >= 3:
                score -= 22
                flags.append(f"May need {yrs_req}+ years of experience")
        except ValueError:
            pass
    if PHD_RX.search(blob):
        score -= 16
        flags.append("PhD required, a likely gap")

    return score, matched, why, flags


def rank_free(jobs, profile):
    """Attach a free '_score' + '_matched' (+ '_why'/'_flags' for transparent
    reasons) to each job and return them sorted, best-first. Mutates in place."""
    skills = _flat_skills(profile)
    titles = [t.lower() for t in (profile.get("target_titles") or [])] or \
             ["software engineer", "developer", "full stack", "backend"]
    pref = profile.get("preferences") or {}
    locs = [l.lower() for l in (pref.get("locations") or [])]
    remote_ok = pref.get("remote_ok", True)
    onsite_ok = pref.get("onsite_ok", True)
    desired = desired_level(profile)
    user_is_swe = _looks_swe(titles, skills)
    try:
        user_years = float(profile.get("years_experience") or 0)
    except (TypeError, ValueError):
        user_years = 0

    for j in jobs:
        s, m, why, flags = heuristic_score(
            j, skills, titles, locs, desired=desired, remote_ok=remote_ok,
            onsite_ok=onsite_ok, user_years=user_years, user_is_swe=user_is_swe)
        j["_score"] = s
        j["_matched"] = m
        j["_why"] = why
        j["_flags"] = flags
    jobs.sort(key=lambda j: j["_score"], reverse=True)
    return jobs


# The free score maps to a 0-100 display. A great match lands high (80s-90s), a
# solid one mid (55-75), a marginal one low (40-55), and a non-match below 40.
_POSSIBLE_MIN = 40


def heuristic_fit(job):
    """Turn the free score into a fit dict (so un-LLM'd jobs still display richly).

    IMPORTANT: the heuristic NEVER awards 'strong'. It only reads keywords, so it
    can't confirm a strong fit (it can't reliably see every disqualifier in the full
    posting). The best a keyword-only job shows is 'possible'; only the LLM, which
    reads the whole posting, marks 'strong'. That keeps every green "Strong fit"
    meaningful.

    The final reason MUST contain "not yet verified" so the UI flags the job as an
    estimate (and shows the '~' prefix) rather than an AI-verified fit."""
    s = job.get("_score", 0)
    matched = job.get("_matched", [])
    why = list(job.get("_why") or [])
    flags = list(job.get("_flags") or [])

    # Fallback if a cached job predates the richer signals: at least say something.
    if not why and matched:
        why.append(f"{len(matched)} of your skills appear: {', '.join(matched[:5])}")

    tier = "possible" if s >= _POSSIBLE_MIN else "skip"
    reasons = why + flags
    reasons.append("Not yet verified by the AI on the full description; "
                   "rank it to confirm the full fit.")
    return {
        "score": max(0, min(100, s)),
        "tier": tier,
        "reasons": reasons,
        "hard_disqualifiers": [],
        "matched_skills": matched,
        "missing_skills": [],
    }
