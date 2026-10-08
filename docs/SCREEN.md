# The halal screen

> [!CAUTION]
> **This is a filter, not a fatwa.** It encodes one reading of which businesses are haram,
> written down as data in `config.json`. It is not a certification, it was not issued by a
> scholar, and it may be wrong about a company or out of date. Check it with a scholar you
> trust and edit the lists to match their judgement and yours. Purification of income is not
> handled at all. Nothing here is financial or religious advice.

This page explains the standard the momentum book's stock list follows, how `universe.py`
applies it, how it fails closed, the reviewed close calls, and how to make it stricter or
looser by editing `config.json`.

## What the screen covers

Three things decide what the bot may buy:

| What | How it is screened |
| --- | --- |
| The core fund | It tracks an MSCI Islamic index, whose methodology does the screening. It is listed by name in `compliance.allowlist`. |
| Anything else bought by a fixed rule | Only what is in `compliance.allowlist`, which you edit by hand. `compliance.allowlist_only` is `true`: nothing else can be bought. |
| The momentum picks | The screen on this page, applied to the S&P 500's members. A pick is buyable only in the month it was picked, and only by the momentum book. |

Selling is never restricted by any of these, so a holding that fails the screen can always be
sold.

## The standard: not haram

A company is out only if its **core business** is haram. There are no financial-ratio screens,
so a stock that no screening service has labelled halal is fine if it is not haram.

That differs from the S&P 500 Shariah index (held by the SPUS fund), which combines a business
screen with ratio screens on debt, cash and receivables. Many standards, AAOIFI's among them,
also apply such ratios. `compliance.ratio_limits` records AAOIFI-style thresholds so an
allowlist entry can be audited by hand, but the code does not compute them: Trading 212's API
returns no company financials. If you follow a standard that requires ratio screens, see
[Stricter](#stricter) below.

The excluded activities are listed in `compliance.excluded_activities`:

- alcohol
- tobacco
- pork and other non-halal food
- gambling and casinos
- conventional banking, lending and insurance, being interest based
- adult entertainment
- weapons and defence
- cannabis

The screen does not measure revenue shares. A permissible business with a small haram line
passes by design, and the close calls below are judgements about which side of "core" a
company falls.

## How it works

`universe.py` builds the list on each monthly build. All of its decisions are data under
`compliance.screen` in `config.json`, each with its reason.

1. **The index members.** It reads the S&P 500's constituents from Wikipedia's list: symbol,
   name, GICS sector and sub-industry, and the date each member joined
   (`strategy.momentum.indexes`, default `["sp500"]`). Columns are found by their header text,
   so a reordered table still parses.
2. **Each company is judged**, in this order of precedence:
   1. In `excluded_symbols`: **dropped**. A named exclusion beats everything.
   2. In `allowed_symbols`: **kept**. A named allowance beats any sector or sub-industry
      exclusion, and its sub-industry needs no review.
   3. Sector in `excluded_sectors`: dropped. (Optional; the shipped config has none.)
   4. Sub-industry in `excluded_sub_industries`: dropped, with its activity as the reason.
   5. No sub-industry given: dropped, "not screened".
   6. Sub-industry not in `permitted_sub_industries`: dropped as "unreviewed".
   7. Otherwise: kept.

   Sub-industry labels are compared loosely: footnote marks like `[3]` are removed, "and" is
   read as "&", and case and spacing are ignored, so an edit to Wikipedia's formatting does
   not change what is excluded.
3. **The list is checked as a whole** (`universe.sound`). It is refused if any canary is kept,
   if none of the canaries is in the list at all (so it is not the list expected), or if
   `excluded_sub_industries`, `permitted_sub_industries` or `canaries` is missing or empty.
4. **The fallback.** If the list is refused, cannot be read, or keeps fewer than
   `strategy.momentum.min_universe` (100) names, the holdings file of SPUS
   (`strategy.momentum.universe_url`) is used instead, with `excluded_symbols` still applied
   to it. If that too has fewer than 100 names, no picks are built and nothing is bought.
5. **Picks are screened again on every run** against the current `compliance.screen`. An
   exclusion added mid-month stops any further buying of that pick from the next run; inside
   the monthly trading window it is then sold as no longer a pick, otherwise in the next
   month's window. The run prints `momentum revoked <ticker>: <reason>`.

### Why it fails closed

A blocklist alone fails open: a company it has never heard of gets bought. Here a company is
kept only if its sub-industry is on a reviewed list of permitted ones, so a new or renamed
GICS label, a footnote or a blank cell drops the company and reports it; it is never let
through. The canaries are companies that are haram by any reading of this standard: if one is
kept, the rules are broken or the list is not what was expected, and the whole list is
refused rather than trusted.

One cost of this design: a sub-industry nobody has reviewed yet keeps its companies out until
someone adds it. That is the intended failure.

## What is excluded

### Sub-industries (`excluded_sub_industries`)

| Activity | GICS sub-industries |
| --- | --- |
| Interest | Diversified Banks, Regional Banks, Consumer Finance, Commercial & Residential Mortgage Finance, Thrifts & Mortgage Finance, Diversified Financial Services, Specialized Finance, Other Specialized Finance, Asset Management & Custody Banks, Investment Banking & Brokerage, Diversified Capital Markets, Financial Exchanges & Data, Mortgage REITs |
| Insurance | Multi-Sector Holdings, Insurance Brokers, Life & Health Insurance, Multi-line Insurance, Property & Casualty Insurance, Reinsurance, Managed Health Care |
| Alcohol | Brewers, Distillers & Vintners |
| Tobacco | Tobacco |
| Gambling | Casinos & Gaming |
| Weapons and defence | Aerospace & Defense |

Health insurers (Managed Health Care) are excluded as insurance. Excluding Financial
Exchanges & Data as a whole keeps out the derivatives exchanges (CME, Cboe, ICE) and Coinbase;
the data and index firms filed there are allowed back by name (below).

Adult entertainment and cannabis have no GICS sub-industry of their own. A company whose core
is either must be named in `excluded_symbols`.

### Named companies (`excluded_symbols`)

For companies whose sub-industry hides the activity:

| Symbol | Company | Why |
| --- | --- | --- |
| HRL | Hormel | Pork is its main raw material and its biggest brands are pork; filed under Packaged Foods & Meats. |
| VICI | VICI Properties | A landlord whose rent comes mostly from casino resorts. |
| LDOS | Leidos | About 45 to 50% of revenue is US defence and intelligence. Closest call. |
| GLPI | Gaming and Leisure Properties | Rent from casino operators. S&P 400. |
| BAH | Booz Allen Hamilton | About two thirds defence and intelligence. S&P 400. |
| CACI | CACI | About three quarters defence. S&P 400. |
| SAIC | SAIC | About three quarters defence and intelligence. S&P 400. |
| KBR | KBR | Mostly mission work for militaries; low confidence. S&P 400. |
| CDP | COPT Defense Properties | Offices beside military and intelligence sites; medium confidence. S&P 400. |

The S&P 400 names matter only if you add `"sp400"` to `strategy.momentum.indexes`. The
shipped list is the S&P 500 alone: a test with the S&P 400 added was inconclusive, as its
members carry no join dates.

### Canaries (`canaries`)

JPM, BAC, WFC (banks), AIG, MET (insurers), MO, PM (tobacco), LVS, MGM (casinos), STZ, TAP
(alcohol), LMT, NOC (defence), UNH (managed health care), HRL (pork), VICI (casino landlord)
and CME (derivatives exchange). Every one must be excluded by some rule, and at least one
must be in the list, or the list is refused.

## Named allowances (`allowed_symbols`)

These are kept even where their sub-industry is excluded, each with its reason in
`config.json`:

| Group | Symbols | Reasoning |
| --- | --- | --- |
| Financial data and indexes | FDS, MSCI, SPGI, MCO, MORN | Sell data, indexes and ratings; do not lend or insure. MORN is S&P 400. |
| Stock exchange | NDAQ | An equity exchange, data and market software, not a derivatives clearing house. |
| Fee-paid fund managers | BLK, TROW, IVZ, BEN | Paid fees to manage mostly equity funds; not lending or insurance. |
| Advisory | EVR, HLI | Merger advisory fees are the core. S&P 400. |
| Commercial aerospace | BA, GE, HWM, HONA, TXT, TDG, HXL, ATI, SARO | Mostly commercial aircraft, engines and parts. HXL, ATI and SARO are S&P 400. |
| Police equipment | AXON | Body cameras and evidence software are the majority. |
| Food | TSN, KHC | Pork is about a tenth of sales, not the core. |
| Cruise lines | CCL, RCL, NCLH | Tickets are most of revenue. |
| Palantir | PLTR | Defence is a minority of revenue. On a watch list. |

Some of these would be kept anyway, because their sub-industry is permitted: the cruise
lines, for one, are filed under Hotels, Resorts & Cruise Lines. They are listed so the
decision and its reason are written down.

## The closest calls

Reviewed on 2026-10-07 against the companies' filings as then known. Revenue shares move, so
revisit them yearly.

- **Excluded:** Leidos (defence about 45 to 50% of revenue).
- **Allowed:**
  - Boeing, TransDigm, Textron and Honeywell Aerospace: defence a third to 40%.
  - Axon: TASER devices about a third.
  - Palantir: defence perhaps 35 to 45%.
  - Moody's and S&P Global: ratings serve the bond market.
  - The cruise lines (Carnival, Royal Caribbean, Norwegian): casinos and bars are inside the
    third of revenue that is not tickets.
  - Tyson: pork about a tenth.

A stricter reading moves any of these into `excluded_symbols`.

## Making it stricter or looser

Everything is in `config.json` under `compliance.screen`. Keys named `about` or `note` are
documentation only. Symbols are upper case; `BRK.B` and `BRK-B` are read the same.

### Stricter

**Exclude a company by name.** Add it to `excluded_symbols` with its activity and reason:

```json
"excluded_symbols": [
  {"symbol": "BA", "activity": "weapons and defence",
   "reason": "Boeing: defence about a third; excluded on a stricter reading"},
  ...
]
```

and delete its entry from `allowed_symbols`, so the file says one thing. Deleting it from
`allowed_symbols` alone is not enough for a company in a permitted sub-industry, such as a
cruise line, and a named exclusion is the only rule that also applies when the bot falls back
to SPUS's list.

**Exclude a whole sub-industry.** Add it to `excluded_sub_industries`:

```json
{"name": "Hotels, Resorts & Cruise Lines", "activity": "gambling"}
```

and remove it from `permitted_sub_industries`. (Removing it from the permitted list alone also
keeps its companies out, reported as "unreviewed".) A named allowance still beats a
sub-industry exclusion: to keep the cruise lines out this way, also delete CCL, RCL and NCLH
from `allowed_symbols`.

**Exclude a whole sector.** `excluded_sectors` takes GICS sector names, for example
`"excluded_sectors": ["Financials"]`. A named allowance still beats it.

**Prove a company stays out.** Add it to `canaries`. It must then be excluded by some rule,
or the whole list is refused.

**Apply ratio screens.** Set `strategy.momentum.universe` to `"spus"`: the momentum book then
picks from SPUS's holdings, the S&P 500 Shariah index with its business and ratio screens,
with `excluded_symbols` still applied.

### Looser

**Allow a company by name.** Add it to `allowed_symbols` with its reason:

```json
"allowed_symbols": {
  "XYZ": "XYZ Corp: why its core business is not haram",
  ...
}
```

A named allowance beats a sub-industry exclusion. If the company is a canary, remove it from
`canaries` too, or the whole list is refused.

**Permit a sub-industry.** Add its exact GICS name to `permitted_sub_industries`, after
reviewing the companies filed under it. The monthly build's output names the companies left
out as unreviewed (the first 20), which is the place to start.

**Remove a named exclusion.** Delete its entry from `excluded_symbols`.

### After editing

```
python3 run.py doctor
```

checks that `config.json` still parses (`python3 universe.py selftest` tests the screening
code, not your lists). The next monthly build prints what the screen did, for example:

```
momentum         built 2026-11 targets from ... names (not_haram), signals to 2026-10
  universe       sp500: ... members
  universe       not screened, so left out: ...
  universe       not haram: ... of ... index members kept, ... excluded
```

or, if the list was refused, `the not-haram list is not trusted (...), so SPUS is used`.

To preview the screen on today's S&P 500 list without a run (Linux or macOS, with internet
access, from the project folder):

```
python3 - <<'EOF'
import json, pricefeed, universe
rules = universe.rules_from(json.load(open("config.json", encoding="utf-8")))
rows, notes = universe.load(["sp500"], pricefeed.http_get)
kept, dropped = universe.screen(rows, rules)
print("; ".join(notes))
print("kept %d of %d; %s" % (len(kept), len(rows),
      universe.sound(rows, kept, rules) or "the screen is sound"))
for row, why in dropped:
    print("%-6s %s" % (row["symbol"], why))
EOF
```

`research.py` reads the same screen when its tests run, so a changed screen can be
backtested without downloading prices again ([STRATEGY.md](STRATEGY.md#reproducing-it)).

## What it does not do

- **Purify income.** Dividends and any other income are not tracked or purified. If your
  scholar says a share of income from these companies should be given away, work it out
  yourself and ask them how.
- **Measure revenue or ratios.** It judges each company's core business from its GICS label
  and the named lists, not from its accounts.
- **Watch the news.** A company that changes its business stays where the lists put it until
  someone edits them. The GICS labels come from Wikipedia and can lag.
- **Certify anything.** See the top of this page.
