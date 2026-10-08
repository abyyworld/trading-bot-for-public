#!/usr/bin/env python3
"""
Which US stocks the momentum book may choose from, under the "not haram" standard.

SPUS's holdings, the S&P 500 Shariah index, are a LABEL: S&P's business screen plus
financial-ratio screens (debt, cash and receivables under a third of market value). The
standard here is the not-haram one: a company is out only if its core business is haram,
ratios do not decide it, and a stock no screener has labelled halal is fine if it is not
haram. So the list is an index's own members, the S&P 500 by default, minus the activities
in compliance.excluded_activities: by GICS sub-industry, plus named companies whose
sub-industry hides the activity (a pork producer filed under packaged foods). SPUS's list is
the fallback when this one cannot be trusted. Every decision is data in config.json under
compliance.screen, with its reason, so a stricter or looser reading is a config change.

What this does not do: measure revenue shares, so a permissible business with a small haram
line passes, by design; and purify income, which is not handled anywhere.

Sources (this is network code, called once a month):
  Wikipedia's constituents tables for the S&P 500 and S&P 400: symbol, name, GICS sector and
  sub-industry, and for the S&P 500 the date each member was added. Columns are found by
  their header text, so a reordered table still parses; a table that is missing them gives
  no rows, and the caller falls back to SPUS rather than buying from a broken list.

    python3 universe.py selftest
"""

import argparse
import html as entities
import re
import sys
from datetime import date

INDEX_URLS = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
}

COLUMNS = {   # our name: header fragments that identify it, lower case
    "symbol": ("symbol", "ticker"),
    "name": ("security", "company"),
    "sector": ("gics sector",),
    "sub_industry": ("gics sub-industry", "gics sub industry", "sub-industry"),
    "added": ("date added", "date first added", "added"),
}


def yahoo_symbol(symbol):
    """BRK.B -> BRK-B, the spelling Yahoo and Trading 212's shortName mapping both use here."""
    return str(symbol or "").strip().upper().replace(".", "-").replace("/", "-")


def _cells(row, tag):
    return [entities.unescape(re.sub(r"<[^>]+>", " ", c)).replace("\xa0", " ").strip()
            for c in re.findall(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), row, re.S)]


def parse_table(page):
    """[{symbol, name, sector, sub_industry, added}] from the first wikitable whose headers
    name a symbol and a GICS sub-industry. Empty if there is none."""
    for table in re.findall(r"<table[^>]*>(.*?)</table>", page, re.S):
        rows = table.split("<tr")[1:]
        if not rows:
            continue
        headers = [re.sub(r"\s+", " ", h).lower() for h in _cells(rows[0], "th")]
        where = {}
        for key, fragments in COLUMNS.items():
            for i, header in enumerate(headers):
                if any(fragment in header for fragment in fragments) and i not in where.values():
                    where[key] = i
                    break
        if "symbol" not in where or "sub_industry" not in where:
            continue
        out = []
        for row in rows[1:]:
            # Some tables put the symbol in a <th>; take every cell in order.
            cells = [entities.unescape(re.sub(r"<[^>]+>", " ", c)).replace("\xa0", " ").strip()
                     for _, c in re.findall(r"<(td|th)[^>]*>(.*?)</(?:td|th)>", row, re.S)]
            if len(cells) <= max(where.values()):
                continue
            symbol = yahoo_symbol(re.sub(r"\s+", "", cells[where["symbol"]]))
            if not symbol or not symbol[0].isalpha():
                continue
            added = None
            if "added" in where:
                found = re.search(r"\d{4}-\d{2}-\d{2}", cells[where["added"]])
                if found:
                    try:
                        added = date.fromisoformat(found.group(0))
                    except ValueError:
                        added = None
            out.append({"symbol": symbol,
                        "name": re.sub(r"\s+", " ", cells[where.get("name", where["symbol"])]),
                        "sector": re.sub(r"\s+", " ", cells[where["sector"]])
                        if "sector" in where else "",
                        "sub_industry": re.sub(r"\s+", " ", cells[where["sub_industry"]]),
                        "added": added})
        if out:
            return out
    return []


def rules_from(config):
    return ((config or {}).get("compliance") or {}).get("screen") or {}


def norm(text):
    """A GICS label as screen() compares it: footnotes like [5] removed, 'and' read as '&',
    case and spacing ignored. Wikipedia edits are not allowed to change what is excluded."""
    text = re.sub(r"\[[^\]]*\]", " ", str(text or ""))
    text = re.sub(r"\band\b", "&", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _named(entries):
    """{SYMBOL: reason} from a list of {symbol, reason, ...} or a {symbol: reason} map."""
    if isinstance(entries, dict):
        return {yahoo_symbol(k): str(v) for k, v in entries.items()}
    out = {}
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("symbol"):
            out[yahoo_symbol(entry["symbol"])] = "%s: %s" % (entry.get("activity", "excluded"),
                                                            entry.get("reason", ""))
    return out


def screen(rows, rules):
    """(kept, dropped) where dropped is [(row, reason)]. A named exclusion beats everything;
    a named allowance beats a sub-industry or sector exclusion.

    Fails closed. With permitted_sub_industries configured, a row is kept only if its
    sub-industry is one reviewed as permitted: a blank cell, a renamed GICS label or
    anything unrecognised is dropped and reported, never let through. A blocklist alone
    fails open, which is exactly what the allowlist in config exists to prevent.
    """
    sectors = {norm(s) for s in rules.get("excluded_sectors") or []}
    subs = {}
    for entry in rules.get("excluded_sub_industries") or []:
        if isinstance(entry, dict):
            subs[norm(entry.get("name", ""))] = entry.get("activity", "excluded")
        else:
            subs[norm(entry)] = "excluded"
    permitted = {norm(s) for s in rules.get("permitted_sub_industries") or []}
    banned = _named(rules.get("excluded_symbols"))
    allowed = _named(rules.get("allowed_symbols"))
    kept, dropped = [], []
    for row in rows:
        symbol = row["symbol"]
        sub = norm(row.get("sub_industry", ""))
        if symbol in banned:
            dropped.append((row, "named: %s" % banned[symbol]))
        elif symbol in allowed:
            kept.append(row)
        elif norm(row.get("sector", "")) in sectors:
            dropped.append((row, "sector %s" % row["sector"]))
        elif sub in subs:
            dropped.append((row, "%s (%s)" % (row["sub_industry"], subs[sub])))
        elif permitted and not sub:
            dropped.append((row, "no sub-industry given, so not screened"))
        elif permitted and sub not in permitted:
            dropped.append((row, "unreviewed sub-industry %r" % row.get("sub_industry", "")))
        else:
            kept.append(row)
    return kept, dropped


def sound(rows, kept, rules):
    """None if the screen did its job on these rows, else why it cannot be trusted: no
    exclusions configured, no reviewed list, or a canary (a company that is haram by any
    reading, named in config) present but kept. Callers fall back rather than buy from it."""
    if not rules.get("excluded_sub_industries"):
        return "compliance.screen has no excluded_sub_industries"
    if not rules.get("permitted_sub_industries"):
        return "compliance.screen has no permitted_sub_industries"
    present = {r["symbol"] for r in rows}
    kept_symbols = {r["symbol"] for r in kept}
    canaries = [yahoo_symbol(c) for c in rules.get("canaries") or []]
    if not canaries:
        return "compliance.screen has no canaries"
    leaked = [c for c in canaries if c in kept_symbols]
    if leaked:
        return "the screen kept %s, which must always be excluded" % " ".join(leaked)
    if not any(c in present for c in canaries):
        return "none of the canaries is in the list, so it is not the list expected"
    return None


def load(indexes, getter):
    """(rows merged across the indexes, notes). The first index to list a symbol wins."""
    rows, notes, seen = [], [], set()
    for name in indexes:
        url = INDEX_URLS.get(name)
        if not url:
            notes.append("unknown index %r" % name)
            continue
        try:
            parsed = parse_table(getter(url, {"Accept": "text/html,*/*"}))
        except Exception as error:
            notes.append("%s list failed: %s" % (name, error))
            continue
        fresh = [r for r in parsed if r["symbol"] not in seen]
        seen.update(r["symbol"] for r in fresh)
        rows.extend(dict(r, index=name) for r in fresh)
        notes.append("%s: %d members" % (name, len(parsed)))
    return rows, notes


def member_on(row, when):
    """True if the row's index already held it on `when`. No recorded date counts as always:
    the old members and the S&P 400, whose table carries none."""
    return row.get("added") is None or row["added"] <= when


# ---------------------------------------------------------------------- selftest

def cmd_selftest(args):
    checks = []

    def expect(name, condition):
        checks.append((name, bool(condition)))

    page = ('<table class="wikitable"><tr><th>Year</th><th>Note</th></tr><tr><td>1</td><td>x</td>'
            '</tr></table><table class="wikitable sortable" id="constituents"><tr>'
            '<th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th>'
            '<th>Headquarters Location</th><th>Date added</th></tr>'
            '<tr><td><a href="x">NVDA</a></td><td>Nvidia</td><td>Information Technology</td>'
            '<td>Semiconductors</td><td>Santa Clara</td><td>2001-11-30</td></tr>'
            '<tr><td>BRK.B</td><td>Berkshire Hathaway</td><td>Financials</td>'
            '<td>Multi-Sector Holdings</td><td>Omaha</td><td>2010-02-16</td></tr>'
            '<tr><td>JPM</td><td>JPMorgan Chase</td><td>Financials</td>'
            '<td>Diversified Banks</td><td>New York</td><td>1975-06-30</td></tr>'
            '<tr><td>V</td><td>Visa</td><td>Financials</td>'
            '<td>Transaction &amp; Payment Processing Services</td><td>SF</td><td>2009-12-21</td></tr>'
            '<tr><td>HRL</td><td>Hormel Foods</td><td>Consumer Staples</td>'
            '<td>Packaged Foods &amp; Meats</td><td>Austin</td><td>2009-03-04</td></tr>'
            '<tr><td>LMT</td><td>Lockheed Martin</td><td>Industrials</td>'
            '<td>Aerospace &amp; Defense</td><td>Bethesda</td><td></td></tr>'
            '<tr><td>GIS</td><td>General Mills</td><td>Consumer Staples</td>'
            '<td>Packaged Foods &amp; Meats</td><td>Minneapolis</td><td>1969-03-31</td></tr>'
            '</table>')
    rows = parse_table(page)
    expect("the constituents table parses, skipping a table without the columns",
           [r["symbol"] for r in rows] == ["NVDA", "BRK-B", "JPM", "V", "HRL", "LMT", "GIS"])
    expect("dots become Yahoo's dashes", rows[1]["symbol"] == "BRK-B")
    expect("entities decode in sub-industries",
           rows[3]["sub_industry"] == "Transaction & Payment Processing Services")
    expect("the date added parses", rows[0]["added"] == date(2001, 11, 30))
    expect("a blank date is no date", rows[5]["added"] is None)
    reordered = page.replace("<th>Symbol</th><th>Security</th>", "<th>Security</th><th>Symbol</th>")
    reordered = re.sub(r"<tr><td>(<a href=\"x\">NVDA</a>)</td><td>Nvidia</td>",
                       r"<tr><td>Nvidia</td><td>\1</td>", reordered)
    expect("columns are found by header, not position",
           parse_table(reordered)[0]["symbol"] == "NVDA")
    expect("a page with no such table gives nothing", parse_table("<table><tr><th>a</th></tr></table>")
           == [])

    rules = {"excluded_sub_industries": [{"name": "Diversified Banks", "activity": "interest"},
                                         {"name": "Multi-Sector Holdings", "activity": "insurance"},
                                         {"name": "Aerospace & Defense", "activity": "weapons"}],
             "excluded_symbols": [{"symbol": "HRL", "activity": "pork", "reason": "Spam, bacon"}],
             "allowed_symbols": {}}
    kept, dropped = screen(rows, rules)
    expect("banks, insurers, weapons and a named pork producer are out",
           {r["symbol"] for r, _ in dropped} == {"JPM", "BRK-B", "LMT", "HRL"})
    expect("payment processing and ordinary food stay in",
           {r["symbol"] for r in kept} == {"NVDA", "V", "GIS"})
    expect("every exclusion says why", all(reason for _, reason in dropped))
    allowed = dict(rules, allowed_symbols={"LMT": "reviewed"})
    expect("a named allowance beats a sub-industry",
           "LMT" in {r["symbol"] for r in screen(rows, allowed)[0]})
    both = dict(rules, allowed_symbols={"HRL": "reviewed"})
    expect("but never a named exclusion", "HRL" not in {r["symbol"] for r in screen(rows, both)[0]})
    expect("sector-wide exclusion works too",
           {r["symbol"] for r in screen(rows, {"excluded_sectors": ["Financials"]})[0]}
           == {"NVDA", "HRL", "LMT", "GIS"})
    expect("matching ignores case and spacing at the ends",
           screen(rows, {"excluded_sub_industries": ["  diversified banks "]})[1][0][0]["symbol"]
           == "JPM")

    expect("footnotes, 'and' and case do not change what is excluded",
           norm("Diversified Banks [1]") == norm("diversified  banks")
           and norm("Investment Banking and Brokerage") == norm("Investment Banking & Brokerage"))
    reviewed = dict(rules, permitted_sub_industries=["Semiconductors",
                                                     "Transaction & Payment Processing Services",
                                                     "Packaged Foods & Meats"])
    odd = rows + [{"symbol": "NEW", "name": "N", "sector": "Financials",
                   "sub_industry": "Banks of Tomorrow", "added": None},
                  {"symbol": "BLANK", "name": "B", "sector": "", "sub_industry": "",
                   "added": None},
                  {"symbol": "FOOT", "name": "F", "sector": "Financials",
                   "sub_industry": "Diversified Banks [3]", "added": None}]
    kept2, dropped2 = screen(odd, reviewed)
    why = {r["symbol"]: reason for r, reason in dropped2}
    expect("fails closed: an unrecognised sub-industry is dropped, with the reason",
           "NEW" in why and "unreviewed" in why["NEW"])
    expect("a blank sub-industry is dropped, not let through", "BLANK" in why)
    expect("a footnoted excluded sub-industry is still excluded",
           "FOOT" in why and "Diversified Banks" in why["FOOT"])
    expect("reviewed permitted sub-industries are kept",
           {r["symbol"] for r in kept2} == {"NVDA", "V", "GIS"})
    canary = dict(reviewed, canaries=["JPM", "LMT"])
    expect("a sound screen passes its canaries", sound(odd, kept2, canary) is None)
    expect("a screen that keeps a canary is not trusted",
           "JPM" in (sound(odd, odd, canary) or ""))
    expect("no exclusions configured is not trusted", sound(odd, odd, {}) is not None)
    expect("a list without any canary is not the list expected",
           "none of the canaries" in (sound(odd[:1], odd[:1], canary) or ""))

    expect("a member added later is not a member before", not member_on(rows[0], date(2000, 1, 1)))
    expect("but is after", member_on(rows[0], date(2002, 1, 1)))
    expect("no date counts as always a member", member_on(rows[5], date(1990, 1, 1)))

    pages = {INDEX_URLS["sp500"]: page, INDEX_URLS["sp400"]: page.replace("NVDA", "AAA")}
    merged, notes = load(["sp500", "sp400", "nasdaq"], lambda url, h: pages[url])
    expect("indexes merge without repeating a symbol",
           len(merged) == 8 and [r["symbol"] for r in merged].count("BRK-B") == 1)
    expect("each row remembers its index", merged[-1]["index"] == "sp400")
    expect("an unknown index is noted, not fatal", any("unknown index" in n for n in notes))

    def broken(url, headers):
        raise OSError("blocked")
    expect("a failed fetch is a note and no rows", load(["sp500"], broken)[0] == [])

    width = max(len(n) for n, _ in checks)
    for name, passed in checks:
        print("%s  %s" % ("pass" if passed else "FAIL", name.ljust(width)))
    failed = [n for n, p in checks if not p]
    print("\n%d checks, %d failed" % (len(checks), len(failed)))
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("selftest").set_defaults(func=cmd_selftest)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
