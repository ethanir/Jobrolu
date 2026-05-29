"""
Cheap, fast, FREE prefilter. Runs before the LLM so we never pay to rank
thousands of obviously-wrong jobs. Rule-based only: title sanity + light
location/keyword checks against the profile.

The LLM (rank.py) does the nuanced scoring on whatever survives here.
"""
import re

# Broad "is this a software / CS / tech role?" allowlist, used to build the SHARED
# pool at full breadth. It is intentionally generous (software, data, ML, security,
# devops, QA, product, design, embedded, and adjacent CS roles) so one pool can
# serve every kind of candidate; per-user scoring (score.py) then narrows each
# person's feed by discipline, level, and location. Only the title is judged.
TECH_ROLE_RX = re.compile(
    r"software|\bswe\b|\bsde\b|\bsdet\b|back.?end|front.?end|full.?stack|"
    r"web (?:developer|engineer)|application (?:engineer|developer)|"
    r"platform engineer|infrastructure|systems? (?:engineer|programmer)|"
    r"devops|\bsre\b|site reliability|reliability engineer|cloud engineer|"
    r"\bprogrammer\b|\bdeveloper\b|mobile (?:engineer|developer|app)|\bios\b|android|"
    r"game(?:play)? (?:engineer|developer|programmer)|graphics (?:engineer|programmer)|"
    r"engine (?:programmer|engineer)|compiler|kernel|embedded|firmware|"
    r"distributed systems|operating system|"
    r"data engineer|data scientist|data science|machine learning|\bml\b|"
    r"deep learning|\bmlops\b|\bai\b (?:engineer|researcher|scientist)|applied scientist|"
    r"research (?:scientist|engineer)|\bnlp\b|computer vision|"
    r"data analyst|data analytics|business intelligence|\bbi\b (?:developer|engineer|analyst)|"
    r"analytics engineer|\betl\b|data platform|"
    r"security engineer|security software|application security|\bappsec\b|\binfosec\b|"
    r"cyber.?security|security analyst|security researcher|product security|penetration test|"
    r"\bqa\b|quality (?:assurance|engineer)|test engineer|test automation|engineer in test|"
    r"product manager|technical program manager|\btpm\b|product owner|"
    r"developer (?:advocate|relations|experience|tools)|\bdevrel\b|"
    r"solutions (?:engineer|architect)|forward deployed|implementation engineer|integration engineer|"
    r"\bux\b|\bui\b (?:engineer|designer)|product designer|design (?:engineer|technologist)|"
    r"hardware engineer|\bfpga\b|\basic\b|\brtl\b|robotics|"
    r"computer (?:engineer|scientist)|\bsdk\b|"
    r"blockchain|smart contract|web3|crypto engineer|"
    r"\barchitect\b|technical lead|tech lead", re.I)

# Explicit non-tech roles to drop even if a tech keyword happens to appear. Kept to
# role-defining nouns (not domain words like "marketing"), so a software role in a
# non-tech domain (e.g. "Software Engineer, Marketing Platform") is still kept.
NONTECH_RX = re.compile(
    r"\brecruit|talent acquisition|account executive|sales (?:representative|development|manager|associate)|"
    r"\bbdr\b|\bsdr\b|business development representative|customer success|customer support|"
    r"support specialist|help desk|\bnurse\b|physician|clinician|therapist|pharmacist|"
    r"\bdental\b|veterinar|phlebotom|attorney|legal counsel|paralegal|accountant|bookkeep|"
    r"barista|cashier|warehouse|forklift|\bdriver\b|janitor|custodian|\bchef\b|\bcook\b|"
    r"mechanical engineer|civil engineer|chemical engineer|biomedical engineer|"
    r"industrial engineer|structural engineer|petroleum|\bhvac\b|plumb|electrician|"
    r"social worker|real estate|loan officer|underwriter|teacher|tutor", re.I)


def prefilter_generic(jobs):
    """Build the SHARED pool: keep any software / CS / tech role at FULL breadth,
    with NO seniority or location filtering and NO coupling to any one profile.
    Per-user scoring narrows each person's feed afterward, so this single pool can
    serve a new grad, a senior, a data scientist, a security engineer, and more.
    A recognized tech title is kept unless it also matches the non-tech blocklist."""
    kept = []
    for j in jobs:
        title = j.get("title", "") or ""
        if not TECH_ROLE_RX.search(title):
            continue
        if NONTECH_RX.search(title):
            continue
        kept.append(j)
    # de-dupe by (company, title), same as prefilter()
    seen, deduped = set(), []
    for j in kept:
        key = ((j.get("company") or "").lower().strip(), (j.get("title") or "").lower().strip())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(j)
    return deduped


SWE_RX = re.compile(
    r"software|engineer|developer|backend|back.?end|front.?end|full.?stack|swe|"
    r"\bsde\b|web|platform|infrastructure|programmer",
    re.I,
)
SENIOR_RX = re.compile(
    r"\bsenior\b|\bstaff\b|\bprincipal\b|\blead\b|\bmanager\b|\bdirector\b|"
    r"\bvp\b|\bhead of\b|\barchitect\b|\bsr\.?\b|\b(ii|iii|iv|v)\b|\b[2-9]\+?\b\s*years",
    re.I,
)


def prefilter(jobs, profile, max_years_for_entry=2):
    """Return only jobs worth sending to the LLM."""
    years = profile.get("years_experience") or 0
    pref = profile.get("preferences") or {}
    pref_locs = [l.lower() for l in (pref.get("locations") or [])]
    remote_ok = pref.get("remote_ok", True)

    kept = []
    for j in jobs:
        title = j.get("title", "")
        if not SWE_RX.search(title):
            continue
        # if candidate is early-career, drop senior-coded titles
        if years <= max_years_for_entry and SENIOR_RX.search(title):
            continue
        # light location gate (only if the candidate specified locations and isn't remote-open)
        loc = (j.get("location") or "").lower()
        if pref_locs and not remote_ok:
            if not (any(p in loc for p in pref_locs) or "remote" in loc):
                continue
        kept.append(j)

    # de-dupe by (company, title)
    seen, deduped = set(), []
    for j in kept:
        key = (j["company"].lower().strip(), j["title"].lower().strip())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(j)
    return deduped
