"""
All LLM prompts for the job finder live here, in one place, so they're
easy to read, tune, and improve. These are the heart of the product.
"""

# The single schema everything keys off of. Profiles are normalized to this.
PROFILE_SCHEMA = """{
  "name": null, "email": null, "phone": null, "location": null,
  "work_authorization": null, "requires_sponsorship": null,
  "target_titles": [], "years_experience": null,
  "education": [{"degree": null, "school": null, "dates": null}],
  "skills": {"languages": [], "frameworks": [], "tools": [], "databases": []},
  "projects": [{"name": null, "stack": [], "summary": null}],
  "preferences": {"remote_ok": null, "onsite_ok": null, "locations": [], "min_salary": null},
  "links": {"github": null, "linkedin": null, "portfolio": null}
}"""

# 1. Resume text -> structured profile JSON. Run on an uploaded resume.
RESUME_TO_PROFILE = """Extract a structured candidate profile from the resume below.
Output ONLY valid JSON matching this exact schema, and nothing else.

Rules:
- Never fabricate facts. Employers, dates, numbers, schools, and skills must come
  straight from the resume. Use null or an empty list when a fact is not present.
- DO fill "target_titles": infer 2 to 4 realistic job titles this person should
  apply to, based on their most recent role, their level of education, and the
  focus of their experience and projects. For example, a CS new grad with backend
  and full-stack projects gives "Software Engineer", "Backend Engineer", "New Grad
  Software Engineer".
- DO fill locations: take the candidate's own city and state from the header,
  education, or experience, and put it in BOTH "location" and
  "preferences.locations". Set "remote_ok" and "onsite_ok" to true unless the
  resume clearly states otherwise.

SCHEMA:
{schema}

RESUME:
{resume_text}"""

# 2. "Bring your own AI" onboarding. The user pastes this into their own ChatGPT/Claude.
BRING_YOUR_OWN_AI = """Based on everything you know about me from our past conversations,
plus anything I add below, output a single JSON object matching the schema for a
job-application profile. Only include facts you're confident about; use null for
anything you don't know. Do not invent employers, dates, or numbers. Output ONLY the JSON.

SCHEMA:
{schema}

EXTRA INFO FROM ME:
<add anything here, or leave blank>"""

# 3. The fit engine. Scores one job against one profile. This is the core value.
FIT_RANKING = """You are an expert technical recruiter scoring fit between a
candidate and a job posting. Be honest and strict: it is more useful to correctly
reject a bad fit than to inflate a score.

Score 0-100 using this rubric:
- Title / role match: is this the kind of role the candidate is targeting?
- Stack overlap: how many of the required technologies the candidate actually has
- Seniority fit: judge against THIS candidate's level. A senior/staff/lead/manager
  title is a mismatch for an early-career candidate; a junior/new-grad/internship
  role is a poor fit for a senior candidate (over-qualified).
- Experience match: does the candidate meet the required years? (a new grad applying
  to a role that needs 5+ years is a hard disqualifier)
- Location / remote: does it fit the candidate's stated preferences?
- Sponsorship: if the role does not sponsor and the candidate needs it, disqualify
- If the posting has little or no description, score from the title and note that the
  detail was limited rather than guessing.

Return ONLY JSON in this shape:
{{
  "score": 0-100,
  "tier": "strong | possible | skip",
  "reasons": ["short bullet", "short bullet"],
  "hard_disqualifiers": ["..."],
  "matched_skills": ["..."],
  "missing_skills": ["..."]
}}

CANDIDATE PROFILE:
{profile_json}

JOB POSTING:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}"""
