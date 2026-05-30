# Jobrolu Coverage Plan

Goal: cover the maximum realistic share of open roles for our users, honestly,
and show that growth live on the `/coverage` page.

This is a working plan. It captures where we stand, what to build next in order of
value-per-effort, and the honesty rules for the public coverage page.

---

## Where we stand today (verified, live on /coverage)

Live pool: ~90,250 open roles across ~5,266 tracked companies and 9 job systems.

Sources live now: Greenhouse (~51.7k roles), Ashby (~14k), Lever (~14k),
SmartRecruiters (~4.4k), USAJOBS (~3k federal), Workday (~1.4k), the Simplify
new-grad repo (~1.4k), Workable (~0.3k), and Recruitee. Adzuna (keyword snippets)
is keyed and available. USAJOBS is live, adding federal coverage across fields.

By field, a large cross-field "other" bucket leads on raw count (~51.6k) and
software is the deepest single tech field (~17.2k). Then sales ~6.1k, product
~2.9k, data / ML ~2.7k, marketing ~2.7k, finance ~1.7k, healthcare ~1.4k, HR
~1.3k, operations ~1.2k, legal ~843, design ~770, hardware ~649, education ~583,
data analytics ~554. The non-tech professional fields are live and growing but
remain the headroom, since the modern ATSes we read skew tech.

The honest gaps that remain:

- Enterprise portals are still mostly unread. We now read some Workday, but iCIMS,
  SAP SuccessFactors, and Oracle/Taleo (which power much large-employer finance,
  healthcare, retail, and manufacturing hiring) are not integrated yet.
- Within the tech ATSes we already support there is still headroom: roughly 10k
  more known Greenhouse / Lever / Ashby boards remain unharvested. The remaining
  non-tech headroom is larger still and needs the diverse ATSes and Workday.

Rough, honest estimates (never published as facts; the true denominator is
unknowable): tech / new-grad coverage is meaningful and climbing; non-tech is
improving fast as USAJOBS and more diverse companies come in, but it still has the
most headroom.

---

## The plan, in priority order

### Phase 1 - Expand the registry on the systems we already have (highest value, lowest effort)
The ATS sources iterate over our company registry, so more companies in the
registry directly multiplies the roles we pull, with zero new integration work.

- Bulk-seed known company tokens for Greenhouse, Lever, Ashby, SmartRecruiters,
  Recruitee, Workable. Public aggregator lists (the kind that power new-grad and
  intern job-board repos) already map companies to their ATS tokens; harvest and
  add them to `registry.json` as `{ats, token, name}` entries.
- Deliberately seed **non-tech** employers that use these systems (for example
  Greenhouse and Workable are used well beyond tech), so finance, healthcare,
  marketing, and ops fields start filling in.
- Tune `registry.py` auto-discovery so new company tokens seen in results are
  retained and re-scanned.
- Watch `BASE_KEEP` (currently 2000) as the pool grows; raise it if good roles
  are being truncated out of the scored base.

Target: go from ~515 to several thousand well-chosen companies. Done so far: the
registry is at ~5,266 companies and the pool roughly tripled (49k to ~90k). Tech
ATS headroom remains (~10k more known boards); the next batches finish tech, then
turn to non-tech.

### Phase 2 - Add Workday (biggest coverage gap, unlocks non-tech)
Workday is the single most impactful new source, especially for the "every field"
promise on the landing page.

- Each company runs its own Workday tenant at a URL like
  `https://<tenant>.wdN.myworkdayjobs.com/<site>`, with a JSON jobs API at
  `.../wday/cxs/<tenant>/<site>/jobs` (POST, paginated).
- Build a `from_workday(tenant, site)` source that posts to the cxs endpoint,
  paginates, and captures the full description (fetch the per-posting detail for
  the body, like the other full-description sources).
- Maintain a Workday tenant list the same way as the ATS registry. Public lists
  of Workday tenants exist; seed banks, hospitals, universities, and large
  enterprises first, since that is exactly the non-tech supply we lack.
- Effort is higher than an ATS (per-tenant URLs, the POST API, pagination), but
  the payoff is the largest of any single source.

### Phase 3 - Turn on the keyword sources and seed government (quick wins)
- Set `USAJOBS_API_KEY` + `USAJOBS_EMAIL` (free at developer.usajobs.gov) to make
  the existing USAJOBS source live. Instant federal-government coverage across
  many fields.
- Confirm Adzuna keys are set and broaden its keyword sets across non-tech fields
  so the aggregator adds breadth (note: Adzuna descriptions are snippets, so these
  roles read thinner than ATS roles).

### Phase 4 - Other enterprise systems and aggregators (later, evaluate first)
- iCIMS, Oracle/Taleo, SAP SuccessFactors: large reach but messier/less open APIs;
  evaluate feasibility and terms before investing.
- Always respect each source's terms of service. We do not scrape LinkedIn or
  Indeed.

---

## The public coverage page (`/coverage`)

Live now. It fetches `/api/coverage` and shows only real, self-measured numbers:

- Headline: live open-role count (animated), which climbs as we add jobs.
- Stat pills: companies tracked, companies hiring now, job systems read.
- Per-field bars: live roles by field, classified by our own ranking engine,
  shown as relative volume (a role can count toward more than one field).
- The systems we read, with counts.
- A straight-talk section: where we are deep, where we are growing, and what we
  do not do.

It updates on its own every time someone loads it, because it reads the live pool
and registry. As Phases 1-3 land, the headline number and the non-tech bars grow
without any change to the page.

### Honesty rules (do not break these)
- Never publish a "% of all jobs in field X" number. The total market size is
  unknowable, and a wrong public number destroys trust.
- Only show numbers we can measure from our own data (roles, companies, systems).
- Keep the "what we do not cover" note honest. Transparency is the trust feature.

---

## Shipped recently
- USAJOBS is live (federal coverage across every field).
- `from_workday(...)` is built and live (a first batch of Workday tenants is in).
- Performance: feed scoring is serialized per profile (no pile-ups or hangs), and
  the cold scoring path now reads full descriptions only for roles with a
  relevance signal, so first loads are much faster on the larger pool with the
  exact same ranking.
- Live coverage counter on the landing hero, in the sticky app header, and on the
  coverage page, all reading one cheap `/api/stats` and staying in sync.
- A `/coverage` link is now in both the landing nav and the app nav (reachable
  everywhere, not just by direct URL).
- Registry growth, in verified batches: +950 tech boards (batch 1), then +2,800
  tech boards (batch 2). Pool grew 49k to ~90k across these. Each batch validated
  for zero removed / altered / malformed entries before deploy, and the site
  confirmed healthy at the new size before the next batch.
- A short-lived "recently added roles" ticker was tried on landing and coverage,
  then removed: with reduce-motion enabled it fell back to a messy stacked layout.
  The live number counter stays; it is honest (real pool size, climbs only on real
  change) and looks clean.

## Maximization strategy (current)
The goal is the ultimate tool for EVERY field, so the priority is **diverse**
coverage, not raw volume. The feed only surfaces each user's top matches, so more
tech roles do little for a nurse or a teacher and only grow server memory. So:
- Grow toward the thin non-tech fields first (healthcare, education, legal,
  finance), via Workday enterprises (hospitals, universities, retailers) and the
  more diverse ATSes (SmartRecruiters, Workable, Recruitee).
- Grow in measured batches, verifying the site stays healthy after each, rather
  than one large dump. We are deliberately not adding a per-company cap for now,
  so the pool is unbounded; that makes measured, verified growth the safety net.
- Watch `BASE_KEEP` (2000) and server memory as the pool climbs.

## Next actions
1. Assemble a diverse, non-tech-leaning batch of company tokens (Workday tenants
   for hospitals / universities / retailers; SmartRecruiters / Workable in
   healthcare, education, finance) and import into `registry.json`. Verify health.
2. Add a Workday auto-discovery pattern to `registry.py` so new tenants seen in
   results are retained, growing non-tech coverage over time.
3. Evaluate iCIMS / SAP SuccessFactors / Oracle Taleo (the big non-tech reach),
   scoping feasibility and terms before investing.
4. Re-check `/coverage` after each batch; every counter reflects the growth on its
   own.
