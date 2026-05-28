"""
seed_workday.py — add Workday-hosted employers to the registry.

Workday is where most large enterprises (and many new-grad rotation programs) post,
and they're invisible to the Greenhouse/Lever/Ashby connectors. Workday needs three
parts, so each token is stored as 'tenant/site/wdN'.

Unlike seed.py / bulk_seed.py, these are added directly (not auto-validated), because
the 3-part Workday token can't be probed the same simple way. The connector itself
validates at pull time: a dead board just returns nothing and is skipped quietly.

HOW TO ADD YOUR OWN:
  Open any company's Workday careers page. The URL looks like:
     https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
                ^tenant ^server                ^site
  So the token is:  nvidia/NVIDIAExternalCareerSite/wd5
  Add a line below in that format and re-run.

USAGE:
  python3 seed_workday.py            # add the built-in set
  python3 seed_workday.py --list     # just print what's built in
"""
import sys

import registry

# (tenant, site, server, "Display Name") — verified URL formats.
WORKDAY = [
    ("nvidia", "NVIDIAExternalCareerSite", "wd5", "NVIDIA"),
    ("salesforce", "External_Career_Site", "wd12", "Salesforce"),
    ("workday", "Workday", "wd5", "Workday"),
    ("target", "targetcareers", "wd5", "Target"),
    ("walmart", "WalmartExternal", "wd5", "Walmart"),
    ("cisco", "External_Network", "wd1", "Cisco"),
    ("dell", "External", "wd1", "Dell"),
    ("hpe", "Hewlett_Packard_Enterprise_Career_Site", "wd5", "HPE"),
    ("paypal", "jobs", "wd1", "PayPal"),
    ("adobe", "external_experienced", "wd5", "Adobe"),
    ("autodesk", "Ext", "wd1", "Autodesk"),
    ("intuit", "External", "wd1", "Intuit"),
    ("ibm", "IBM", "wd5", "IBM"),
    ("capitalone", "Capital_One", "wd1", "Capital One"),
    ("mastercard", "CorporateCareers", "wd1", "Mastercard"),
    ("fanniemae", "FannieMae", "wd1", "Fannie Mae"),
    ("nasdaq", "Global_External_Site", "wd1", "Nasdaq"),
    ("blackrock", "BlackRock_Professional", "wd1", "BlackRock"),
    ("statefarm", "ExternalSelfApply", "wd1", "State Farm"),
    ("verizon", "verizon", "wd12", "Verizon"),
    ("att", "ATTexternal", "wd1", "AT&T"),
    ("emerson", "Emerson", "wd1", "Emerson"),
    ("3m", "Search", "wd1", "3M"),
    ("kohls", "ExternalNonStore", "wd5", "Kohl's"),
    ("gm", "Careers_GM", "wd5", "General Motors"),
    ("ford", "FordCareers", "wd1", "Ford"),
    ("johnson", "jnjcareers", "wd5", "Johnson & Johnson"),
    ("cargill", "Cargill_External", "wd1", "Cargill"),
    ("thomsonreuters", "External", "wd3", "Thomson Reuters"),
    ("spglobal", "SPGlobal", "wd5", "S&P Global"),
]


def main():
    if "--list" in sys.argv:
        for t, s, srv, name in WORKDAY:
            print(f"  {name:<22} {t}/{s}/{srv}")
        return

    reg = registry.load()
    before = len(reg)
    added = 0
    for tenant, site, server, name in WORKDAY:
        token = f"{tenant}/{site}/{server}"
        key = f"workday:{token}"
        if key in reg:
            continue
        reg[key] = {"name": name, "ats": "workday", "token": token}
        added += 1
        print(f"  + {name:<22} {token}")

    registry.save(reg)
    print(f"\nAdded {added} Workday companies. Registry now {before + added}.")
    print("Dead or changed boards are skipped automatically at pull time.")
    print("Next run of main.py pulls from them.")


if __name__ == "__main__":
    main()
