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
import os
import re
import time
from functools import lru_cache

# Mirrors prefilter.MULTIFIELD. ON by default. Enables the field-agnostic same-discipline
# title bonus below, so a role in the user's OWN non-tech field scores well even when the
# exact title words differ. Set MULTIFIELD=off to revert to the prior tech-only scoring.
MULTIFIELD = os.environ.get("MULTIFIELD", "on").strip().lower() in ("1", "true", "on", "yes")

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


@lru_cache(maxsize=4096)
def _skill_pat(skill):
    """Compile the whole-token matcher for a skill ONCE per distinct skill. The
    scoring loop runs this for every skill against every job in the pool, so
    rebuilding and re-escaping the pattern each call was the dominant cost;
    caching the compiled pattern keeps the matches identical and makes a cold
    rank of a large pool dramatically faster."""
    return re.compile(r"(?<![a-z0-9+#])" + re.escape(skill) + r"(?![a-z0-9+#])")


def _word_in(skill, blob):
    """True only if skill appears as a whole token (so 'c' won't match 'clearance')."""
    return _skill_pat(skill).search(blob) is not None


def _flat_skills(profile):
    """Flatten the profile's skills into a de-duped lowercase list used for overlap
    scoring. `skills` (a dict of lists) holds SOFTWARE skills; a malformed profile
    whose skills is a plain list will raise here, which is intentional (the server
    catches it and degrades rather than scoring from a corrupt profile). On top of
    that we fold in `skills_general` and `certifications`, flat lists that carry a
    candidate's field skills for ANY profession, so the same whole-word overlap logic
    works for a nurse or an accountant just as it does for a software engineer."""
    s = profile.get("skills", {}) or {}
    out = []
    for k in ("languages", "frameworks", "tools", "databases", "cloud", "other"):
        out += [str(x).lower() for x in (s.get(k) or []) if x]
    extra = profile.get("skills_general")
    if isinstance(extra, list):
        out += [str(x).lower() for x in extra if x]
    certs = profile.get("certifications")
    if isinstance(certs, list):
        out += [str(x).lower() for x in certs if x]
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


# --- role discipline (so ONE broad pool can stay on-target for every user) ---
# Each title maps to zero or more disciplines. A job is down-weighted for a user
# only when BOTH the job and the user classify confidently into DISJOINT
# disciplines AND their titles share no identifying words. That keeps adjacent
# engineering roles (security, devops, data, QA, mobile) available to a software
# engineer, while genuinely different fields (data science, analytics, product,
# design, hardware) drop out, and an unrecognized title is never penalized.
_DISC_ENG_RX = re.compile(
    r"software engineer|software developer|\bswe\b|\bsde\b|\bsdet\b|"
    r"back.?end|front.?end|full.?stack|web developer|web engineer|"
    r"application (?:engineer|developer)|platform engineer|infrastructure engineer|"
    r"cloud engineer|systems engineer|devops|\bsre\b|site reliability|reliability engineer|"
    r"quality assurance|test engineer|test automation|engineer in test|automation engineer|"
    r"data engineer|analytics engineer|\betl\b|"
    r"security engineer|application security|\bappsec\b|\binfosec\b|product security|"
    r"embedded|firmware|kernel|compiler|distributed systems|"
    r"mobile (?:engineer|developer)|\bios\b|android|\bprogrammer\b|\bdeveloper\b", re.I)
_DISC_MLDS_RX = re.compile(
    r"machine learning|\bml\b|deep learning|\bmlops\b|\bai\b engineer|"
    r"data scientist|data science|applied scientist|research scientist|"
    r"\bnlp\b|computer vision|quantitative (?:research|analyst|developer)", re.I)
_DISC_ANALYTICS_RX = re.compile(
    r"data analyst|data analytics|business intelligence|\bbi\b analyst|reporting analyst", re.I)
_DISC_PRODUCT_RX = re.compile(
    r"product manager|product management|technical program manager|\btpm\b|"
    r"program manager|product owner", re.I)
_DISC_DESIGN_RX = re.compile(
    r"\bux\b|\bui\b designer|user experience|product designer|interaction designer|visual designer", re.I)
_DISC_HARDWARE_RX = re.compile(
    r"hardware engineer|electrical engineer|\bfpga\b|\basic\b|\brtl\b|\bpcb\b|analog engineer|\brf\b engineer", re.I)

# --- professional non-tech disciplines (active once MULTIFIELD admits these roles) ---
# Title-level patterns so cross-field down-weighting can keep, say, a nurse's feed on
# nursing and an accountant's on finance. Kept reasonably specific to avoid mislabeling
# a software role; an unrecognized title still classifies as nothing and is never penalized.
_DISC_FINANCE_RX = re.compile(
    r"\baccountant\b|accounting|\bauditor\b|financial analyst|finance manager|\bcontroller\b|"
    r"\bcpa\b|\bcfa\b|investment (?:banking|analyst|associate|banker)|equity research|"
    r"financial advisor|wealth (?:manager|advisor)|\bfp&a\b|treasury analyst|tax (?:associate|manager|accountant)|"
    r"bookkeep|\bactuary\b|underwriter|loan officer|credit analyst|portfolio manager|financial planning", re.I)
_DISC_MARKETING_RX = re.compile(
    r"\bmarketing\b|brand (?:manager|strategist)|content (?:marketing|strategist)|\bseo\b|\bsem\b|"
    r"growth marketing|social media (?:manager|specialist|strategist)|communications (?:manager|specialist)|"
    r"public relations|demand generation|\bcopywriter\b|digital marketing|product marketing", re.I)
_DISC_SALES_RX = re.compile(
    r"account executive|sales (?:representative|manager|associate|director|development|consultant)|"
    r"business development|\bbdr\b|\bsdr\b|account manager|inside sales|enterprise sales|partnerships manager", re.I)
_DISC_HR_RX = re.compile(
    r"\brecruiter\b|recruiting|talent acquisition|human resources|\bhr\b (?:manager|generalist|business partner|coordinator)|"
    r"people operations|people partner|compensation (?:analyst|manager)|benefits (?:analyst|manager)|learning and development", re.I)
_DISC_OPERATIONS_RX = re.compile(
    r"operations manager|business operations|revenue operations|\brevops\b|supply chain|"
    r"logistics (?:manager|coordinator|analyst)|procurement|category manager", re.I)
_DISC_LEGAL_RX = re.compile(
    r"\battorney\b|\blawyer\b|legal counsel|\bparalegal\b|legal (?:assistant|analyst)|"
    r"compliance (?:officer|analyst|manager)|contracts manager|regulatory affairs|general counsel", re.I)
_DISC_HEALTHCARE_RX = re.compile(
    r"\bnurse\b|registered nurse|\brn\b|nurse practitioner|\bphysician\b|clinician|\bpharmacist\b|"
    r"physical therapist|occupational therapist|respiratory therapist|physician assistant|"
    r"medical assistant|\bdietitian\b|dental hygienist|radiolog|sonograph|clinical (?:specialist|coordinator|nurse)", re.I)
_DISC_EDUCATION_RX = re.compile(
    r"\bteacher\b|\bprofessor\b|\binstructor\b|\blecturer\b|\beducator\b|teaching|"
    r"curriculum (?:developer|specialist)|\btutor\b|academic advisor|school counselor", re.I)

# --- additional common fields, added to shrink the catch-all "other" bucket and
# sharpen cross-field down-weighting. Kept deliberately specific so they never
# grab a software, data, or other existing-discipline role by accident. A title
# that still matches nothing stays unclassified and is never penalized. ---
_DISC_SUPPORT_RX = re.compile(
    r"customer (?:service|support|success|experience)|client (?:service|services|success)|"
    r"\bhelp desk\b|call center|contact center|support (?:specialist|representative|associate|agent)|"
    r"member services|patient access", re.I)
_DISC_ADMIN_RX = re.compile(
    r"administrative (?:assistant|coordinator|specialist|aide)|executive assistant|"
    r"office (?:manager|coordinator|administrator|assistant)|\breceptionist\b|front desk|"
    r"data entry|\bsecretary\b|\bclerical\b|file clerk", re.I)
_DISC_TRADES_RX = re.compile(
    r"\belectrician\b|\bhvac\b|\bplumber\b|\bwelder\b|\bmachinist\b|\bmillwright\b|\bcarpenter\b|"
    r"maintenance (?:technician|mechanic|worker|engineer)|(?:field|service|automotive) technician|"
    r"facilities (?:technician|coordinator|manager)", re.I)
_DISC_ENGOTHER_RX = re.compile(
    r"mechanical engineer|civil engineer|chemical engineer|industrial engineer|"
    r"manufacturing engineer|process engineer|quality engineer|aerospace engineer|"
    r"structural engineer|environmental engineer|biomedical engineer|materials engineer|"
    r"petroleum engineer", re.I)

_DISC_NAMES = {
    "analytics": "data analytics", "design": "design", "eng": "software engineering",
    "hardware": "hardware", "ml_ds": "data science / ML", "product": "product management",
    "finance": "finance / accounting", "marketing": "marketing",
    "sales": "sales / business development", "hr": "HR / recruiting",
    "operations": "operations", "legal": "legal", "healthcare": "healthcare",
    "education": "education",
    "support": "customer support", "admin": "administrative",
    "trades": "skilled trades", "eng_other": "engineering (other)",
}


@lru_cache(maxsize=8192)
def role_disciplines(text):
    """Classify a title (or a user's target titles) into zero or more disciplines.
    Empty when nothing matches, so an unrecognized title stays neutral and never
    triggers a penalty. An ML/AI *engineer* counts as BOTH ml_ds and eng, so it
    fits software and data candidates alike.

    Cached: this runs ~14 regexes, and a pool has many duplicate titles (and the
    same title is checked more than once per job), so memoizing by text turns a
    cold rank of a large pool from seconds into a fraction of that. Returns a
    frozenset so the shared cached value can never be mutated by a caller."""
    t = (text or "").lower()
    d = set()
    if _DISC_ENG_RX.search(t):
        d.add("eng")
    if _DISC_MLDS_RX.search(t):
        d.add("ml_ds")
        if "engineer" in t:                 # an ML/AI *engineer* is also software
            d.add("eng")
    if _DISC_ANALYTICS_RX.search(t):
        d.add("analytics")
    if _DISC_PRODUCT_RX.search(t):
        d.add("product")
    if _DISC_DESIGN_RX.search(t):
        d.add("design")
    if _DISC_HARDWARE_RX.search(t):
        d.add("hardware")
    if _DISC_FINANCE_RX.search(t):
        d.add("finance")
    if _DISC_MARKETING_RX.search(t):
        d.add("marketing")
    if _DISC_SALES_RX.search(t):
        d.add("sales")
    if _DISC_HR_RX.search(t):
        d.add("hr")
    if _DISC_OPERATIONS_RX.search(t):
        d.add("operations")
    if _DISC_LEGAL_RX.search(t):
        d.add("legal")
    if _DISC_HEALTHCARE_RX.search(t):
        d.add("healthcare")
    if _DISC_EDUCATION_RX.search(t):
        d.add("education")
    if _DISC_SUPPORT_RX.search(t):
        d.add("support")
    if _DISC_ADMIN_RX.search(t):
        d.add("admin")
    if _DISC_TRADES_RX.search(t):
        d.add("trades")
    if _DISC_ENGOTHER_RX.search(t):
        d.add("eng_other")
    return frozenset(d)


def _profile_disciplines(target_titles, user_is_swe):
    """The disciplines the USER targets, unioned across their target titles. Falls
    back to software engineering only when we otherwise can't tell but the skills
    say SWE; stays empty when we truly can't classify, so we never penalize blindly."""
    d = set()
    for t in (target_titles or []):
        d |= role_disciplines(t)
    if not d and user_is_swe:
        d.add("eng")
    return d


def heuristic_score(job, skills, titles, locs, desired="entry",
                    remote_ok=True, onsite_ok=True, user_years=0,
                    user_is_swe=True, user_disc=None, desc_scan=True):
    """Return (score:int, matched:list, why:list[str], flags:list[str]).

    Pure function, no side effects. `why` are positive reasons (shown under "Why you
    fit"); `flags` are concerns (shown under "Worth knowing"). The reason strings are
    the transparency layer: the score is just their weights summed.

    desc_scan=False skips the parts that read the (long) job description: the skill
    overlap and the clearance/years/PhD checks. rank_free uses it for roles with no
    relevance signal (no matching title, field, or skill). For those the skill bonus
    is already zero and the only other description effects are penalties, so a role
    skipped this way can only stay where it is or rank lower, never higher; the
    ranked top is identical while we avoid the costly scan on most of the pool."""
    title = job.get("title", "") or ""
    desc = (job.get("description", "") or "")
    blob = (title + " " + desc)[:12000].lower() if desc_scan else ""
    why, flags = [], []
    score = 0

    # --- title / role fit: the dominant signal -----------------------------
    tpts, thit = _title_fit(title, titles)
    if tpts == 0 and user_is_swe and SWE_RX.search(title):
        tpts, thit = 16, "software engineering"      # SWE-family synonym fallback
    elif MULTIFIELD and tpts < 14 and user_disc:
        # Field-agnostic same-discipline fallback: a role in the user's own field
        # (finance, nursing, marketing, ...) should read as on-target even when the
        # exact title words differ. This mirrors the SWE fallback for every field and
        # is gated so tech-only behavior is unchanged when MULTIFIELD is off.
        common = role_disciplines(title) & user_disc
        if common:
            tpts = max(tpts, 14)
            thit = thit or _DISC_NAMES.get(sorted(common)[0], "your field")
    score += tpts
    if tpts >= 30:
        why.append(f"Closely matches your target: {thit}")
    elif tpts >= 12:
        why.append(f"In your target area: {thit}")

    # --- discipline fit: keep a broad pool on-target for THIS user ---------
    # Only fires when both sides classify confidently into disjoint disciplines
    # AND the titles share no identifying words (tpts < 12), so adjacent
    # engineering roles and unrecognized titles are never cut.
    if user_disc and tpts < 12:
        jdisc = role_disciplines(title)
        if jdisc and jdisc.isdisjoint(user_disc):
            score -= 38
            nm = _DISC_NAMES.get(sorted(jdisc)[0], "a different")
            flags.append(f"This looks like a {nm} role, outside your target area")

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
    matched = []
    if desc_scan:
        # Substring pre-check before the (slower) whole-word regex. A skill can
        # only match as a whole token if it appears as a substring at all, so
        # `s in blob` is a necessary condition and gates identically; it just
        # skips the regex for the many skills absent from a given posting, which
        # is where the time went on a large pool. Results are byte-identical.
        matched = [s for s in skills if s and s in blob and _word_in(s, blob)]
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

    # --- hard-ish disqualifiers (description scan) -------------------------
    if desc_scan:
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
    user_disc = _profile_disciplines(titles, user_is_swe)
    try:
        user_years = float(profile.get("years_experience") or 0)
    except (TypeError, ValueError):
        user_years = 0

    # Speed: the full scan reads every job's (long) description once per skill,
    # which dominates the cost over a large pool, yet most jobs are irrelevant to a
    # given user. So we do the expensive description scan only for jobs with a
    # relevance signal: a matching title, the user's own field, or any of their
    # skills appearing in the posting. The skill test below is exactly equivalent to
    # "would heuristic_score match any skill" (same whole-word boundaries), so a job
    # we skip has a zero skill bonus regardless and can only lose points from the
    # description checks. The ranked top is therefore identical, on far less work.
    skill_rx = None
    if skills:
        alts = "|".join(re.escape(s) for s in skills if s)
        if alts:
            skill_rx = re.compile(r"(?<![a-z0-9+#])(" + alts + r")(?![a-z0-9+#])")

    for j in jobs:
        title = j.get("title", "") or ""
        tpts, _thit = _title_fit(title, titles)
        relevant = (tpts > 0
                    or (user_is_swe and bool(SWE_RX.search(title)))
                    or bool(user_disc and (role_disciplines(title) & user_disc)))
        if not relevant and skill_rx is not None:
            blob = (title + " " + (j.get("description", "") or ""))[:12000].lower()
            relevant = skill_rx.search(blob) is not None
        s, m, why, flags = heuristic_score(
            j, skills, titles, locs, desired=desired, remote_ok=remote_ok,
            onsite_ok=onsite_ok, user_years=user_years, user_is_swe=user_is_swe,
            user_disc=user_disc, desc_scan=relevant)
        j["_score"] = s
        j["_matched"] = m
        j["_why"] = why
        j["_flags"] = flags
    jobs.sort(key=lambda j: j["_score"], reverse=True)
    return jobs


def field_relevant_subset(jobs, profile):
    """The jobs on-target for THIS profile by title or field alone, with NO
    description read. This is exactly the cheap front half of rank_free's
    relevance gate (matching title, the user's own field, or the SWE family),
    so a job in here is one rank_free would also treat as relevant; scoring just
    these gives the user's real matches fast. Off-field roles that match only via
    a skill buried in the description are intentionally excluded here and picked
    up by the full scan afterward, where they only ever become low/skip matches."""
    titles = [t.lower() for t in (profile.get("target_titles") or [])] or \
             ["software engineer", "developer", "full stack", "backend"]
    skills = _flat_skills(profile)
    user_is_swe = _looks_swe(titles, skills)
    user_disc = _profile_disciplines(titles, user_is_swe)
    out = []
    for j in jobs:
        title = j.get("title", "") or ""
        tpts, _ = _title_fit(title, titles)
        if (tpts > 0
                or (user_is_swe and bool(SWE_RX.search(title)))
                or (user_disc and bool(role_disciplines(title) & user_disc))):
            out.append(j)
    return out


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
