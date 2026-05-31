"""
All LLM prompts for the job finder live here, in one place, so they're
easy to read, tune, and improve. These are the heart of the product.
"""

# The single schema everything keys off of. Profiles are normalized to this.
PROFILE_SCHEMA = """{
  "name": null, "email": null, "phone": null, "location": null,
  "work_authorization": null, "requires_sponsorship": null,
  "field": null,
  "target_titles": [], "years_experience": null,
  "education": [{"degree": null, "school": null, "dates": null}],
  "experience": [{"title": null, "company": null, "dates": null, "summary": null}],
  "skills": {"languages": [], "frameworks": [], "tools": [], "databases": []},
  "skills_general": [],
  "certifications": [],
  "projects": [{"name": null, "stack": [], "summary": null}],
  "preferences": {"remote_ok": null, "onsite_ok": null, "locations": [], "min_salary": null},
  "links": {"github": null, "linkedin": null, "portfolio": null}
}"""

# 1. Resume text -> structured profile JSON. Run on an uploaded resume.
RESUME_TO_PROFILE = """Extract a structured candidate profile from the resume below.
Output ONLY valid JSON matching this exact schema, and nothing else.

Rules:
- Never fabricate facts. Employers, dates, numbers, schools, skills, licenses, and
  certifications must come straight from the resume. Use null or an empty list when a
  fact is not present.
- "field": name the candidate's primary professional field in a few words, for example
  "software engineering", "data science", "nursing", "accounting", "marketing", "sales",
  "mechanical engineering", "teaching", or "legal". Infer it from their titles, education,
  and experience.
- DO fill "target_titles": infer 2 to 4 realistic job titles this person should apply to,
  based on their most recent role, their education, and the focus of their experience. A CS
  new grad with backend projects gives "Software Engineer", "Backend Engineer", "New Grad
  Software Engineer"; a staff nurse gives "Registered Nurse", "ICU Nurse", "Charge Nurse".
- "skills" is for SOFTWARE skills ONLY: programming languages, frameworks, developer tools,
  and databases. Leave its lists empty for non-technical candidates.
- "skills_general": a flat list of the candidate's most important skills, competencies,
  tools, and methods in THEIR field, in their own words. For a nurse: "patient assessment",
  "IV therapy", "EHR", "ACLS". For a marketer: "SEO", "Google Analytics", "content strategy".
  For an accountant: "GAAP", "QuickBooks", "financial reporting", "Excel". Fill this for
  everyone, technical or not, and for technical people you may include their key tools here too.
- "certifications": professional licenses and certifications, for example "RN license", "CPA",
  "PMP", "CFA", "Bar admission", or "AWS Solutions Architect". Empty list if none.
- "projects": ONLY when the resume actually lists named projects. Many fields have none; use
  an empty list then. Do not invent projects.
- DO fill locations: take the candidate's own city and state from the header, education, or
  experience, and put it in BOTH "location" and "preferences.locations". Set "remote_ok" and
  "onsite_ok" to true unless the resume clearly states otherwise.

SCHEMA:
{schema}

RESUME:
{resume_text}"""

# 2. "Bring your own AI" onboarding. The user pastes this into their own ChatGPT/Claude.
BRING_YOUR_OWN_AI = """Based on everything you know about me from our past conversations,
plus anything I add below, output a single JSON object matching the schema for a
job-application profile, in whatever field I work in. Only include facts you're confident
about; use null for anything you don't know. Do not invent employers, dates, or numbers.
Fill "field" with my profession, "skills_general" with my key skills/tools/methods in my own
words, and "certifications" with any licenses or certifications I hold. Output ONLY the JSON.

SCHEMA:
{schema}

EXTRA INFO FROM ME:
<add anything here, or leave blank>"""

# 3. The fit engine. Scores one job against one profile. This is the core value.
FIT_RANKING = """You are an expert recruiter scoring fit between a candidate and a job
posting. Be honest and strict: it is more useful to correctly reject a bad fit than to
inflate a score. Judge fit WITHIN the candidate's own field, whatever it is.

Score 0-100 using this rubric:
- Title / role match: is this the kind of role the candidate is targeting?
- Skills / qualifications overlap: how many of the role's required skills, tools, methods,
  or qualifications the candidate actually has. For a technical role this is the tech stack;
  for any other field it is that field's own skills, software, and methods.
- Licenses / certifications: if the role requires a specific license or certification (for
  example an RN license, CPA, bar admission, a security clearance, or a named certification)
  and the candidate does not hold it, treat that as a serious gap or a hard disqualifier.
- Seniority fit: judge against THIS candidate's level. A senior/staff/lead/manager/director
  title is a mismatch for an early-career candidate; a junior/entry/internship role is a poor
  fit for a senior candidate (over-qualified).
- Experience match: does the candidate meet the required years? (a new grad applying to a
  role that needs 5+ years is a hard disqualifier)
- Location / remote: does it fit the candidate's stated preferences?
- Sponsorship: if the role does not sponsor and the candidate needs it, disqualify
- If the posting has little or no description, score from the title and note that the
  detail was limited rather than guessing.
- For salary, posted_date, and close_date: report them ONLY if the description states
  them outright; never estimate or guess, and use null when the posting does not say.
  Salary is yearly unless the posting says hourly (period "hour"); ignore sign-on
  bonuses, equity, and relocation. Dates are YYYY-MM-DD.

Return ONLY JSON in this shape:
{{
  "score": 0-100,
  "tier": "strong | possible | skip",
  "reasons": ["short bullet", "short bullet"],
  "hard_disqualifiers": ["..."],
  "matched_skills": ["..."],
  "missing_skills": ["..."],
  "salary": {{"min": null, "max": null, "period": "year"}},
  "posted_date": null,
  "close_date": null
}}

CANDIDATE PROFILE:
{profile_json}

JOB POSTING:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}"""
