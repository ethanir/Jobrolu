"""
seed_registry.py - grow the company registry in safe, controlled batches.

The registry is the compounding asset: every company in it gets pulled on each
scan, so more companies means more jobs. This tool pulls public company/board
identifiers from a maintained open dataset, cleans and de-dupes them against what
we already have, and adds up to a capped number of NEW companies per ATS.

Why caps matter: Workday companies are large employers that can each return
hundreds or thousands of jobs, so adding too many at once can balloon the live
pool (memory + cold-scoring time). Grow in steps, deploy, watch /coverage, then
grow again once the numbers look healthy.

Usage (run from the repo root, where registry.json lives):
    python3 seed_registry.py                       # conservative defaults
    python3 seed_registry.py --greenhouse 800 --lever 400 --ashby 300 --workday 100
    python3 seed_registry.py --dry-run             # show what would change, write nothing

Notes:
  - Dead or moved boards are harmless: the scan skips them quietly. This tool only
    adds well-FORMED tokens; the live scan is what proves which ones return jobs.
  - Source data is public board identifiers (facts), pulled fresh each run.
"""
import argparse
import json
import os
import random
import re
import sys
import urllib.request

REGISTRY_PATH = "registry.json"

# Maintained open dataset of company board identifiers across ATSes.
BASE = ("https://raw.githubusercontent.com/Feashliaa/"
        "job-board-aggregator/main/data")
SOURCES = {
    "greenhouse": f"{BASE}/greenhouse_companies.json",
    "lever": f"{BASE}/lever_companies.json",
    "ashby": f"{BASE}/ashby_companies.json",
    "workday": f"{BASE}/workday_companies.json",
}

# Conservative first-batch defaults. Raise these once /coverage confirms the pool
# and scan stay healthy. Workday is kept lowest because it yields the most jobs
# per company.
DEFAULTS = {"greenhouse": 500, "lever": 250, "ashby": 200, "workday": 50}


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "jobrolu-seed/1.0"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))


def _good_slug(t):
    """Keep only real-looking board slugs (drops numeric ids and hex blobs)."""
    t = (t or "").strip()
    if not t or len(t) < 2 or len(t) > 40:
        return False
    if t.isdigit():
        return False
    if re.fullmatch(r"[0-9a-f]{16,}", t):
        return False
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", t, re.I):
        return False
    if len(re.sub(r"[^a-z]", "", t.lower())) < 2:
        return False
    return True


def _workday_token(raw):
    """Aggregator 'tenant|server|site' -> our 'tenant/site/wdN'.

    The source mixes in malformed rows of the form 'wdN|wd1|site' where the
    tenant slot holds a datacenter code, not a real tenant; those produce dead
    URLs like https://wd102.wd1.myworkdayjobs.com/... and must be skipped. A real
    tenant is never just 'wdN', so reject that case."""
    parts = (raw or "").split("|")
    if len(parts) != 3:
        return None
    tenant, server, site = (p.strip() for p in parts)
    if not (tenant and server and site) or not re.fullmatch(r"wd\d+", server):
        return None
    if re.fullmatch(r"wd\d+", tenant):
        return None
    return f"{tenant}/{site}/{server}"


def _name_from(ats, token):
    base = token.split("/")[0] if ats == "workday" else token
    nm = base.replace("-", " ").replace("_", " ").strip().title()
    return nm or token


def main():
    ap = argparse.ArgumentParser(description="Grow the company registry in safe batches.")
    for ats, n in DEFAULTS.items():
        ap.add_argument(f"--{ats}", type=int, default=n,
                        help=f"max NEW {ats} companies to add (default {n})")
    ap.add_argument("--seed", type=int, default=7, help="random seed for sampling")
    ap.add_argument("--dry-run", action="store_true", help="report only; do not write")
    args = ap.parse_args()
    caps = {a: getattr(args, a) for a in SOURCES}
    random.seed(args.seed)

    if not os.path.exists(REGISTRY_PATH):
        print(f"! {REGISTRY_PATH} not found. Run this from the repo root.")
        sys.exit(1)
    reg = json.load(open(REGISTRY_PATH))
    before = len(reg)
    have = set(reg.keys())

    added_total = 0
    for ats, url in SOURCES.items():
        cap = caps.get(ats, 0)
        if cap <= 0:
            continue
        try:
            raw = _fetch(url)
        except Exception as e:
            print(f"  ! {ats}: could not fetch source ({e}); skipping")
            continue
        # clean + convert
        if ats == "workday":
            toks = [w for w in (_workday_token(t) for t in raw) if w]
        else:
            toks = [t.strip() for t in raw if _good_slug(t)]
        # new only
        new = [t for t in toks if f"{ats}:{t}" not in have]
        random.shuffle(new)
        pick = new[:cap]
        for t in pick:
            key = f"{ats}:{t}"
            reg[key] = {"name": _name_from(ats, t), "ats": ats, "token": t}
            have.add(key)
        added_total += len(pick)
        print(f"  {ats:14} available-new {len(new):>6}  added {len(pick):>5}")

    after = len(reg)
    print(f"\nregistry: {before} -> {after}  (+{added_total})")
    if args.dry_run:
        print("dry-run: no file written.")
        return
    # write sorted + stable, matching registry.save() format
    with open(REGISTRY_PATH, "w") as f:
        json.dump(reg, f, indent=2, sort_keys=True)
    print(f"wrote {REGISTRY_PATH}.")
    print("Next: commit, deploy, trigger a scan (owner Refresh), then watch /coverage.")


if __name__ == "__main__":
    main()
