#!/usr/bin/env python3
"""
Research harness: one download of daily prices, then every pre-registered test against it.

It runs on any machine with internet access, or on a GitHub runner from
.github/workflows/research.yml, which has no trading credentials and its own queue, so research
can never touch a trading account or hold up a trading run. Some development containers cannot
reach Yahoo or Wikipedia; the runner can.

The rules are fixed BEFORE the data is seen, in TESTS below, each with its success criterion.
That is the protection against the classic way backtests lie: trying fifty variants and
reporting the best. Every result is printed for the full sample, each half, and the last five
years; a rule that wins only in one half is noise until shown otherwise.

What it fixes from backtest.py:
  point in time   a stock counts only from the date it joined the S&P 500 (Wikipedia's
                  "Date added"), so a small company that ran up 1,000% and was THEN added is
                  no longer bought before anyone could have known; that bias flatters momentum
  daily prices    month ends, overnight gaps, volume, and short-horizon rules all come from
                  the same aligned daily series
What it does not fix: delisted and acquired companies are still missing, because Yahoo has no
history for them. Absolute returns stay too high; compare rows, not levels.

    python3 research.py fetch [INDEXES...]   download daily OHLCV, cached in research/cache
    python3 research.py run [TESTS...]       run tests (default: all) and print the tables
    python3 research.py selftest
"""

import argparse
import json
import math
import sys
import time
from array import array
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "research" / "cache"
COST = 0.002          # per side: 0.15% FX fee plus a spread allowance, as backtest.py
SPAN = "20y"
NAN = float("nan")


# ---------------------------------------------------------------------- data

def parse_ohlcv(payload):
    """{t: [ordinal], o, h, l, c, a, v} from a Yahoo daily chart response; bad rows dropped.
    c is the split-adjusted close, a the dividend-adjusted one; o, h, l are scaled by a/c
    so that every price field is on the adjusted basis and gaps are not dividend artefacts."""
    chart = (payload or {}).get("chart") or {}
    result = (chart.get("result") or [None])[0] or {}
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    adj = (((result.get("indicators") or {}).get("adjclose") or [{}])[0] or {}).get("adjclose")
    out = {k: [] for k in "tohlcav"}
    seen = set()
    for n, stamp in enumerate(stamps):
        try:
            o, h, l, c = (quote[k][n] for k in ("open", "high", "low", "close"))
            v = quote.get("volume", [0] * len(stamps))[n] or 0
            a = adj[n] if adj else c
        except (KeyError, IndexError, TypeError):
            continue
        if not all(isinstance(x, (int, float)) and x > 0 for x in (o, h, l, c, a)):
            continue
        day = datetime.fromtimestamp(int(stamp), tz=timezone.utc).date().toordinal()
        if day in seen:
            continue
        seen.add(day)
        scale = a / c
        for key, value in zip("tohlcav", (day, o * scale, h * scale, l * scale, c, a, v)):
            out[key].append(value)
    return out


def fetch_symbol(symbol, getter=None, sleep=time.sleep):
    import pricefeed
    getter = getter or pricefeed.http_get
    url = ("https://query2.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d"
           "&includeAdjustedClose=true" % (symbol, SPAN))
    try:
        return parse_ohlcv(json.loads(pricefeed._retrying(getter, url, None, sleep)))
    except Exception as error:
        print("  %-8s skipped: %s" % (symbol, error), file=sys.stderr)
        return None


def is_daily(series):
    days = series.get("t") or []
    if len(days) < 300:
        return False
    gaps = sorted(b - a for a, b in zip(days, days[1:]))
    return gaps[len(gaps) // 2] < 5


def cached(symbol, getter=None, sleep=time.sleep, directory=CACHE):
    """The symbol's series from today's cache file, else fetched and cached. None if bad."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("%s.json" % symbol)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    series = fetch_symbol(symbol, getter, sleep)
    if series and is_daily(series):
        path.write_text(json.dumps(series), encoding="utf-8")
        return series
    return None


# ---------------------------------------------------------------------- the panel

class Panel:
    """Every symbol on one calendar: arrays of equal length, NaN where a symbol has no bar."""

    def __init__(self, raw, meta=None):
        counts = {}
        for series in raw.values():
            for d in series["t"]:
                counts[d] = counts.get(d, 0) + 1
        ordered = sorted(counts)
        # A day only a few names have is a data error (a stray bar on a holiday), not a
        # session: kept, it would replace the market's move with one stock's.
        self.dropped_days = []
        keep = []
        for n, d in enumerate(ordered):
            near = sorted(counts[x] for x in ordered[max(0, n - 10): n + 11])
            if counts[d] >= 0.5 * near[len(near) // 2]:
                keep.append(d)
            else:
                self.dropped_days.append(d)
        self.days = keep
        where = {d: i for i, d in enumerate(self.days)}
        size = len(self.days)
        self.meta = meta or {}
        self.f = {}
        for symbol, series in raw.items():
            cols = {k: array("d", [NAN]) * size for k in "ohlcav"}
            for n, d in enumerate(series["t"]):
                i = where.get(d)
                if i is None:
                    continue
                for k in "ohlcav":
                    cols[k][i] = series[k][n]
            self.f[symbol] = cols
        self.symbols = sorted(self.f)
        self.month_ends = [i for i in range(size)
                           if i + 1 == size
                           or date.fromordinal(self.days[i + 1]).month
                           != date.fromordinal(self.days[i]).month]
        # The last month end is only a month end if the month is over.
        if self.month_ends and self.month_ends[-1] == size - 1:
            last = date.fromordinal(self.days[-1])
            if last.day < 25:
                self.month_ends.pop()

    def day(self, i):
        return date.fromordinal(self.days[i])

    def member(self, symbol, i):
        added = (self.meta.get(symbol) or {}).get("added")
        return added is None or added <= self.days[i]

    def price(self, symbol, i, field="a"):
        value = self.f[symbol][field][i]
        return None if value != value else value

    def back(self, symbol, i, field="a"):
        """The latest non-missing value at or before i, or None."""
        col = self.f[symbol][field]
        while i >= 0:
            if col[i] == col[i]:
                return col[i]
            i -= 1
        return None


def ew_index_returns(panel, eligible):
    """Daily equal-weight return of the names eligible that day with bars on both days."""
    out = [0.0] * len(panel.days)
    for i in range(1, len(panel.days)):
        total, count = 0.0, 0
        for s in panel.symbols:
            a, b = panel.f[s]["a"][i - 1], panel.f[s]["a"][i]
            if a == a and b == b and eligible(s, i - 1):
                total += b / a - 1.0
                count += 1
        out[i] = total / count if count else 0.0
    return out


# ---------------------------------------------------------------------- metrics

def stats(monthly):
    """CAGR, vol, Sharpe (no risk-free rate), max drawdown, worst 12 months, months."""
    r = [x for _, x in monthly]
    if len(r) < 12:
        return None
    growth, peak, worst, curve = 1.0, 1.0, 0.0, []
    for x in r:
        growth *= 1.0 + x
        curve.append(growth)
        peak = max(peak, growth)
        worst = min(worst, growth / peak - 1.0)
    years = len(r) / 12.0
    mean = sum(r) / len(r)
    vol = math.sqrt(sum((x - mean) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(12)
    rolling = [curve[i] / (curve[i - 12] if i >= 12 else 1.0) - 1.0
               for i in range(11, len(curve))]
    return {"cagr": growth ** (1.0 / years) - 1.0, "vol": vol,
            "sharpe": mean * 12 / vol if vol else 0.0, "max_dd": worst,
            "worst_12m": min(rolling), "months": len(r)}


def periods(monthly):
    """The windows every result is judged on: full, first half, second half, last 5 years."""
    if not monthly:
        return []
    months = [m for m, _ in monthly]
    middle = months[len(months) // 2]
    five = months[-60] if len(months) >= 60 else months[0]
    return [("full", None, None), ("first half", None, middle),
            ("second half", middle, None), ("last 5y", five, None)]


def window(monthly, start, end):
    return [(m, r) for m, r in monthly if (start is None or m >= start)
            and (end is None or m < end)]


def table(rows, start=None, end=None):
    lines = ["%-26s %7s %7s %7s %8s %9s %6s" % ("test", "CAGR", "vol", "sharpe", "max_dd",
                                                  "worst12m", "months")]
    for name, monthly in rows:
        m = stats(window(monthly, start, end))
        if not m:
            lines.append("%-26s too little data" % name)
            continue
        lines.append("%-26s %6.1f%% %6.1f%% %7.2f %7.1f%% %8.1f%% %6d"
                     % (name[:26], m["cagr"] * 100, m["vol"] * 100, m["sharpe"],
                        m["max_dd"] * 100, m["worst_12m"] * 100, m["months"]))
    return "\n".join(lines)


def blend(parts):
    maps = [(w, dict(series)) for w, series in parts]
    shared = sorted(set.intersection(*(set(m) for _, m in maps))) if maps else []
    return [(month, sum(w * m[month] for w, m in maps)) for month in shared]


def monthly_from_daily(panel, daily):
    """[(YYYY-MM, return)] compounding a daily return list over each calendar month."""
    out, growth, month = [], 1.0, None
    for i, r in enumerate(daily):
        label = panel.day(i).strftime("%Y-%m")
        if month is not None and label != month:
            out.append((month, growth - 1.0))
            growth = 1.0
        month = label
        growth *= 1.0 + r
    if month is not None and panel.month_ends and panel.month_ends[-1] == len(daily) - 1:
        out.append((month, growth - 1.0))
    return out[1:]   # the first month is partial


# ---------------------------------------------------------------------- engine A: monthly

def monthly_book(panel, eligible, score, n=10, cost=COST, buffer=None, sector_cap=None,
                 pick=None, exposure=None, every=1, exit_rule=None, idle=None, report=None,
                 lag=0, offset=None):
    """[(YYYY-MM, net return)] holding the top n by score from each month end to the next.

    eligible(symbol, i)  may it be held from month end i (member, enough data)
    score(symbol, i)     higher is better, None to skip; uses data at or before i only
    buffer               keep a holding while it ranks inside `buffer`
    sector_cap           at most this many from one GICS sector
    pick(ranked, i)      replaces the top-n choice entirely, for rules like frog-in-the-pan
    exposure(i, held)    fraction of the sleeve invested (0..1), the rest earns nothing
    every                re-pick only every `every` month ends; in between, hold and let drift
    exit_rule(s, d)      checked on each held name every day d of the month; True sells it at
                         the close of d+1 and parks the money in `idle` (the core fund's daily
                         returns) until the next re-pick
    report               a dict that receives turnover (one side, a year, re-weighting
                         included) and switches a year (names bought or sold outright)
    lag                  trading days from the month-end signal to the fill. 0 fills at the
                         signal close; the live bot fills a day or more later, and later
                         still when GitHub drops scheduled runs
    offset               with every > 1, re-pick at month ends whose month index (year*12 +
                         month - 1) mod `every` is `offset`, rather than every `every`-th
                         scored month: one tranche of a staggered book
    report["log"]        also gets {YYYY-MM: (one-side turnover that month, names held)}
    """
    out, weights, held = [], {}, []
    turnover, trades, months = 0.0, 0, 0
    ends = panel.month_ends
    last = len(panel.days) - 1
    for k in range(len(ends) - 1):
        i, j = ends[k], ends[k + 1]
        fill, until = min(i + lag, last), min(j + lag, last)
        scored = []
        for s in panel.symbols:
            if not eligible(s, i):
                continue
            value = score(s, i)
            if value is not None and value == value:
                scored.append((value, s))
        if len(scored) < max(2 * n, 20):
            continue
        months += 1
        due = (months % every == 1 if offset is None
               else month_index(panel, i) % every == offset)
        if held and every > 1 and not due:
            target = dict(weights)      # no trading this month: hold what drifted
            chosen = held
        else:
            ranked = [s for _, s in sorted(scored, key=lambda x: (-x[0], x[1]))]
            if pick:
                chosen = pick(ranked, i)
            else:
                rank = {s: r for r, s in enumerate(ranked)}
                keep = [s for s in held if buffer and rank.get(s, buffer) < buffer][:n]
                chosen = list(keep)
                sectors = {}
                for s in chosen:
                    sector = (panel.meta.get(s) or {}).get("sector", "")
                    sectors[sector] = sectors.get(sector, 0) + 1
                for s in ranked:
                    if len(chosen) >= n:
                        break
                    if s in chosen:
                        continue
                    sector = (panel.meta.get(s) or {}).get("sector", "")
                    if sector_cap and sectors.get(sector, 0) >= sector_cap:
                        continue
                    chosen.append(s)
                    sectors[sector] = sectors.get(sector, 0) + 1
            invest = exposure(i, chosen) if exposure else 1.0
            target = {s: invest / len(chosen) for s in chosen} if chosen else {}
        held = [s for s in chosen if s in target]
        traded = sum(abs(target.get(s, 0.0) - weights.get(s, 0.0))
                     for s in set(target) | set(weights))
        # Switches only: a name bought into or sold out of entirely, not a re-weighting.
        trades += sum(1 for s in set(target) | set(weights) if s != "_core"
                      and (target.get(s, 0.0) > 0) != (weights.get(s, 0.0) > 0))
        month_turn = traded / 2.0
        gross, drifted = 0.0, {}
        for s, w in target.items():
            if s == "_core":
                growth = 1.0
                for d in range(fill + 1, until + 1):
                    growth *= 1.0 + (idle[d] if idle else 0.0)
                gross += w * (growth - 1.0)
                drifted["_core"] = drifted.get("_core", 0.0) + w * growth
                continue
            a, b = panel.back(s, fill), panel.back(s, until)
            left = None
            if exit_rule:
                for d in range(fill + 1, until):
                    if panel.price(s, d) and exit_rule(s, d):
                        left = d + 1
                        break
            if left is not None and a and panel.back(s, left):
                r_out = panel.back(s, left) / a - 1.0
                growth = 1.0
                for d in range(left + 1, until + 1):
                    growth *= 1.0 + (idle[d] if idle else 0.0)
                value = w * (1.0 + r_out) * (1.0 - cost) * growth
                gross += value - w
                drifted["_core"] = drifted.get("_core", 0.0) + value
                month_turn += w / 2.0
                trades += 1
                continue
            r = b / a - 1.0 if a and b else 0.0
            gross += w * r
            drifted[s] = w * (1.0 + r)
        cash = 1.0 - sum(target.values())
        total = sum(drifted.values()) + cash or 1.0
        weights = {s: v / total for s, v in drifted.items()}
        label = panel.day(j).strftime("%Y-%m")
        out.append((label, gross - traded * cost))
        turnover += month_turn
        if report is not None:
            report.setdefault("log", {})[label] = (
                month_turn, tuple(sorted(s for s in target if s != "_core")))
    if report is not None and months:
        report["turnover"] = turnover / months * 12
        report["trades"] = trades / months * 12
    return out


def daily_momentum_book(panel, eligible, n=10, exit_rank=30, cost=COST, report=None):
    """Rank every day, switch rarely. Starts with the top n by 12-1 momentum (in trading
    days: 252 back to 21 back). Each day a holding that has fallen below rank `exit_rank` is
    sold at the next close and its money buys the best-ranked name not held, the same
    close; nothing else trades. Returns [(YYYY-MM, return)]."""
    size = len(panel.days)
    held, cash = {}, 1.0
    daily, prev = [], 1.0
    swaps = 0

    def score(s, i):
        if i < 252:
            return None
        a, b = panel.back(s, i - 252), panel.back(s, i - 21)
        return b / a - 1.0 if a and b and panel.price(s, i) else None

    pending = []
    for i in range(size):
        for s, pos in held.items():
            price = panel.price(s, i)
            if price:
                pos["value"] *= price / pos["last"]
                pos["last"] = price
        if pending:
            for out_s in pending:
                pos = held.pop(out_s, None)
                if not pos:
                    continue
                cash += pos["value"] * (1.0 - cost)
            pending = []
        if i >= 252:
            scored = sorted(((score(s, i), s) for s in panel.symbols if eligible(s, i)),
                            key=lambda x: (-(x[0] if x[0] is not None else -9e9), x[1]))
            ranked = [s for v, s in scored if v is not None]
            if len(ranked) >= 2 * n:
                rank = {s: r for r, s in enumerate(ranked)}
                equity = cash + sum(p["value"] for p in held.values())
                # Fill empty slots with the best names not held, at today's close.
                for s in ranked:
                    if len(held) >= n or cash <= 1e-12:
                        break
                    if s in held:
                        continue
                    spend = min(cash, equity / n) if len(held) < n - 1 else cash
                    price = panel.price(s, i)
                    if not price or spend <= 1e-12:
                        continue
                    cash -= spend
                    held[s] = {"value": spend * (1.0 - cost), "last": price}
                    swaps += 1
                # Decide today, act at tomorrow's close: holdings that fell too far.
                pending = [s for s in held if rank.get(s, exit_rank) >= exit_rank]
        equity = cash + sum(p["value"] for p in held.values())
        daily.append(equity / prev - 1.0 if prev else 0.0)
        prev = equity
    if report is not None:
        years = max(size / 252.0, 1e-9)
        report["trades"] = swaps / years
        report["turnover"] = swaps / n / years
    return monthly_from_daily(panel, daily)


def month_index(panel, i):
    """Months since year 0 of day i: year*12 + month - 1, for staggering re-picks."""
    d = panel.day(i)
    return d.year * 12 + d.month - 1


def month_end_before(panel, i, months_back):
    """The index of the month end `months_back` month ends before month end i."""
    ends = panel.month_ends
    k = ends.index(i) if i in ends else None
    if k is None or k - months_back < 0:
        return None
    return ends[k - months_back]


def momentum_score(panel, s, i, start=12, skip=1):
    """Price at month end i-skip over price at i-start, minus one: 12-1 by default."""
    a_i, b_i = month_end_before(panel, i, start), month_end_before(panel, i, skip)
    if a_i is None or b_i is None:
        return None
    a, b = panel.price(s, a_i), panel.price(s, b_i)
    return b / a - 1.0 if a and b else None


def skip_days_score(panel, s, i, start=12, skip_days=5):
    """T7's 12-1w: the close `skip_days` trading days before month end i over the close at the
    month end `start` months back, minus one. Like momentum_score, a missing bar is no score."""
    a_i = month_end_before(panel, i, start)
    if a_i is None or i - skip_days < 0:
        return None
    a, b = panel.price(s, a_i), panel.price(s, i - skip_days)
    return b / a - 1.0 if a and b else None


def tranche_book(panel, eligible, score, n=10, K=3, cost=COST, lag=0, report=None):
    """T11: K tranches, each 1/K of the sleeve at the start, tranche j re-picked to the top n
    only at month ends whose month index mod K is j and held in between. They run
    independently and are never rebalanced against each other, so each grows with its own
    picks; each pays `cost` on its own trades (live orders would net across tranches, so this
    overstates cost). report gets turnover and switches a year of the combined book, its
    orders a year (every tranche's own switches), the distinct names held on average, and a
    per-month log like monthly_book's."""
    runs = []
    for j in range(K):
        rep = {}
        series = monthly_book(panel, eligible, score, n=n, cost=cost, every=K, offset=j,
                              lag=lag, report=rep)
        runs.append((dict(series), rep.get("log", {})))
    shared = sorted(set.intersection(*(set(r) for r, _ in runs))) if runs else []
    values = [1.0 / K] * K
    out, log = [], {}
    switches = orders = 0
    names_total = 0
    before, before_each = set(), [set() for _ in range(K)]
    for m in shared:
        total = sum(values)
        turn = sum(values[j] / total * runs[j][1].get(m, (0.0, ()))[0] for j in range(K))
        each = [set(runs[j][1].get(m, (0.0, ()))[1]) for j in range(K)]
        names = set().union(*each)
        out.append((m, sum(values[j] * (1.0 + runs[j][0][m]) for j in range(K)) / total - 1.0))
        values = [values[j] * (1.0 + runs[j][0][m]) for j in range(K)]
        log[m] = (turn, tuple(sorted(names)))
        switches += len(names ^ before)
        orders += sum(len(each[j] ^ before_each[j]) for j in range(K))
        names_total += len(names)
        before, before_each = names, each
    if report is not None and shared:
        report["log"] = log
        report["turnover"] = sum(t for t, _ in log.values()) / len(shared) * 12
        report["trades"] = switches / len(shared) * 12
        report["orders"] = orders / len(shared) * 12
        report["names"] = names_total / len(shared)
    return out


# ---------------------------------------------------------------------- engine B: daily

def event_book(panel, eligible, signal, hold, slots=5, cost=COST, idle=None, stop=None,
               switch=0.0):
    """Daily replay of a short-horizon rule. Returns (daily returns, trades).

    signal(symbol, i)   strength (higher first) if the close of day i is a signal, else None
    hold                trading days from the entry close to the exit close
    stop(symbol, i, entered_i)  True to exit at the NEXT close early, optional
    idle                daily returns earned by money not in a position (None for cash)
    switch              extra cost per side for moving money out of and back into `idle`,
                        the core fund's own spread when idle money is parked there
    Entry is at the close of day i+1, exit at a close: never the signal's own close.
    """
    size = len(panel.days)
    cash, held, pending, trades = 1.0, {}, [], []
    curve, prev = [], 1.0
    for i in range(size):
        if idle:
            cash *= 1.0 + idle[i]
        for s, pos in held.items():
            price = panel.price(s, i)
            if price:
                pos["value"] *= price / pos["last"]
                pos["last"] = price
                pos["days"] += 1
        for s in [s for s, pos in held.items()
                  if (pos["days"] >= hold or pos["leave"]) and panel.price(s, i)]:
            pos = held.pop(s)
            back = pos["value"] * (1.0 - cost) * (1.0 - switch)
            cash += back
            trades.append((s, pos["in"], i, back / pos["paid"] - 1.0))
        equity = cash + sum(p["value"] for p in held.values())
        for s in pending:
            if len(held) >= slots or s in held:
                continue
            price = panel.price(s, i)
            if not price:
                continue
            spend = min(cash, equity / slots)
            if spend <= 1e-9:
                break
            cash -= spend
            held[s] = {"value": spend * (1.0 - cost) * (1.0 - switch), "paid": spend,
                       "last": price, "in": i, "days": 0, "leave": False}
        if stop:
            for s, pos in held.items():
                if panel.price(s, i) and stop(s, i, pos["in"]):
                    pos["leave"] = True
        found = []
        for s in panel.symbols:
            if s in held or not eligible(s, i):
                continue
            strength = signal(s, i)
            if strength is not None:
                found.append((-strength, s))
        pending = [s for _, s in sorted(found)]
        equity = cash + sum(p["value"] for p in held.values())
        curve.append(equity / prev - 1.0 if prev else 0.0)
        prev = equity
    return curve, trades


def trade_summary(trades, days_per_year=252):
    if not trades:
        return "no trades"
    won = sum(1 for t in trades if t[3] > 0)
    mean = sum(t[3] for t in trades) / len(trades)
    return "%5d trades, %4.1f%% won, %+.2f%% a trade after costs" % (
        len(trades), 100.0 * won / len(trades), 100.0 * mean)


# ---------------------------------------------------------------------- universes

TESTS = {}    # name: function(panel, context) -> {"series": [(label, monthly)], "notes": [...]}


def load_universe(indexes, getter=None):
    """({symbol: meta} for EVERY index member plus the SPUS list, notes). Unscreened on
    purpose: the screen is applied when tests run, so it can change without a new download."""
    import pricefeed
    import universe
    getter = getter or pricefeed.http_get
    rows, notes = universe.load(indexes, getter)
    meta = {}
    for row in rows:
        meta[row["symbol"]] = {"name": row["name"], "sector": row["sector"],
                               "sub_industry": row["sub_industry"], "index": row["index"],
                               "added": row["added"].toordinal() if row["added"] else None}
    try:
        import backtest
        spus = {r[0] for r in backtest.parse_spus(getter(backtest.SPUS_HOLDINGS,
                                                         {"Accept": "text/csv,*/*"}))}
        notes.append("SPUS: %d names" % len(spus))
    except Exception as error:
        spus = set()
        notes.append("SPUS list failed: %s" % error)
    for s in spus:
        meta.setdefault(s, {"name": "", "sector": "", "sub_industry": "", "index": "spus",
                            "added": None})
        meta[s]["spus"] = True
    return meta, notes


def screened(panel, config):
    """The symbols the configured screen keeps, and [(symbol, reason)] for those it drops."""
    import universe
    rows = [dict(m, symbol=s, added=None) for s, m in panel.meta.items() if m.get("index")
            != "spus"]
    kept, dropped = universe.screen(rows, universe.rules_from(config))
    return {r["symbol"] for r in kept}, [(r["symbol"], why) for r, why in dropped]


def universe_test(which, point_in_time=True):
    """The current live rule, 12-1 momentum top 10, on one universe."""
    def run(panel, context):
        ok = context["universes"][which]
        eligible = ((lambda s, i: s in ok and panel.member(s, i)) if point_in_time
                    else (lambda s, i: s in ok))
        monthly = monthly_book(panel, eligible, lambda s, i: momentum_score(panel, s, i))
        label = "mom10 %s%s" % (which, "" if point_in_time else " (no pit)")
        return {"series": [(label, monthly)],
                "notes": ["%d names in %s" % (len(ok), which)]}
    return run


def equal_weight_test(which):
    def run(panel, context):
        ok = context["universes"][which]
        daily = ew_index_returns(panel, lambda s, i: s in ok and panel.member(s, i))
        context.setdefault("ew", {})[which] = daily
        return {"series": [("equal weight %s" % which, monthly_from_daily(panel, daily))]}
    return run


# ---------------------------------------------------------------------- features

def rolling_mean_prior(values, window):
    """Mean of the `window` values BEFORE each index (NaN-skipping), NaN until enough."""
    out = array("d", [NAN]) * len(values)
    total, count, queue = 0.0, 0, []
    for i, v in enumerate(values):
        if count >= window * 0.8 and queue:
            out[i] = total / count
        queue.append(v)
        if v == v:
            total += v
            count += 1
        if len(queue) > window:
            old = queue.pop(0)
            if old == old:
                total -= old
                count -= 1
    return out


def rolling_std_prior(values, window):
    out = array("d", [NAN]) * len(values)
    s1 = s2 = 0.0
    count, queue = 0, []
    for i, v in enumerate(values):
        if count >= window * 0.8 and count > 1:
            mean = s1 / count
            out[i] = math.sqrt(max(s2 / count - mean * mean, 0.0) * count / (count - 1))
        queue.append(v)
        if v == v:
            s1 += v
            s2 += v * v
            count += 1
        if len(queue) > window:
            old = queue.pop(0)
            if old == old:
                s1 -= old
                s2 -= old * old
                count -= 1
    return out


def rolling_max_prior(values, window):
    """Highest of the `window` values before each index, by a monotonic queue: O(n)."""
    from collections import deque
    out = array("d", [NAN]) * len(values)
    best = deque()   # indices, values decreasing
    for i, v in enumerate(values):
        while best and best[0] < i - window:
            best.popleft()
        if best and i >= window:
            out[i] = values[best[0]]
        if v == v:
            while best and values[best[-1]] <= v:
                best.pop()
            best.append(i)
    return out


def features(panel, context):
    """Per-symbol daily arrays the event rules read, computed once per run."""
    if "features" in context:
        return context["features"]
    ew = context.get("ew", {}).get("sp500") or ew_index_returns(
        panel, lambda s, i: s in context["universes"]["sp500"] and panel.member(s, i))
    context.setdefault("ew", {})["sp500"] = ew
    out = {}
    for s in panel.symbols:
        a, o, v = panel.f[s]["a"], panel.f[s]["o"], panel.f[s]["v"]
        size = len(a)
        ret = array("d", [NAN]) * size
        gap = array("d", [NAN]) * size
        ar = array("d", [NAN]) * size
        last = NAN
        for i in range(size):
            if a[i] == a[i] and last == last:
                ret[i] = a[i] / last - 1.0
                ar[i] = ret[i] - ew[i]
                gap[i] = o[i] / last - 1.0
            if a[i] == a[i]:
                last = a[i]
        out[s] = {"ret": ret, "ar": ar, "gap": gap,
                  "vol50": rolling_mean_prior(v, 50), "arsd60": rolling_std_prior(ar, 60),
                  "hi252": rolling_max_prior(a, 252), "sma200": rolling_mean_prior(a, 200),
                  "sd252": rolling_std_prior(ret, 252)}
    context["features"] = out
    return out


def daily_vol(panel, context, s, i):
    sd = features(panel, context)[s]["sd252"][i]
    return sd * math.sqrt(252) if sd == sd and sd > 0 else None


def median(values):
    ordered = sorted(values)
    k = len(ordered)
    if not k:
        return None
    return ordered[k // 2] if k % 2 else (ordered[k // 2 - 1] + ordered[k // 2]) / 2.0


def ea_surprise(panel, context, which="sp500", min_names=20):
    """T8: each day's volume surprise per name, v / vol50 divided by that day's median
    v / vol50 over the universe's members, so index rebalance and quad-witching days, when
    everything trades heavily, do not pass for company news. A day with fewer than
    `min_names` usable names has no median. -1.0 wherever unknown, never NaN, so max() is
    safe. Computed once a run: per call it would take hours."""
    key = ("ea_rel", which)
    if key in context:
        return context[key]
    feats = features(panel, context)
    names = sorted(set(context["universes"][which]) & set(panel.symbols))
    size = len(panel.days)
    ratio = {}
    for s in names:
        v, avg = panel.f[s]["v"], feats[s]["vol50"]
        row = array("d", [-1.0]) * size
        for d in range(size):
            if v[d] == v[d] and v[d] > 0 and avg[d] == avg[d] and avg[d] > 0:
                row[d] = v[d] / avg[d]
        ratio[s] = row
    medians = array("d", [-1.0]) * size
    for d in range(size):
        day = [ratio[s][d] for s in names if ratio[s][d] > 0 and panel.member(s, d)]
        if len(day) >= min_names:
            medians[d] = median(day)
    rel = {}
    for s in names:
        row = array("d", [-1.0]) * size
        for d in range(size):
            if ratio[s][d] > 0 and medians[d] > 0:
                row[d] = ratio[s][d] / medians[d]
        rel[s] = row
    context[key] = rel
    return rel


def ea_blocks(panel, context, s, i, which="sp500", blocks=4, length=63, threshold=2.0):
    """T8: the earnings-like day of each 63-day block of the 252 trading days ending at month
    end i, oldest first, as (day, EAR). The day is the block's largest volume surprise, the
    earliest if tied, kept only if it is at least `threshold`; EAR is the stock's return over
    the equal-weight universe from two days before it to two after, cut at i, so nothing
    after the signal is read."""
    cache = context.setdefault(("ea_blocks", which), {})
    if (s, i) in cache:
        return cache[(s, i)]
    rel = ea_surprise(panel, context, which).get(s)
    found = []
    first = i - blocks * length + 1
    if rel is not None and first >= 0:
        ar = features(panel, context)[s]["ar"]
        for b in range(blocks):
            lo = first + b * length
            block = rel[lo:lo + length]
            top = max(block)
            if top < threshold:
                continue
            d = lo + block.index(top)
            ear = sum(x for x in ar[max(d - 2, 0):min(d + 2, i) + 1] if x == x)
            found.append((d, ear))
    cache[(s, i)] = found
    return found


def eamom_score(panel, context, s, i, which="sp500", need=3):
    """T8 default: the sum of the year's EARs, if at least `need` earnings-like days were found."""
    found = ea_blocks(panel, context, s, i, which)
    return sum(e for _, e in found) if len(found) >= need else None


def intraday_parts(panel, context):
    """T9: per symbol, prefix sums over the panel's days of ln(a/o), the open-to-close move,
    and ln(o/a_prev), the overnight move from the previous bar's close, with running counts of
    bars and of back-filled opens (a raw open equal to the previous raw close within 1e-6,
    Yahoo's habit when it has no real open; raw open = o*c/a). o is on the adjusted basis
    (parse_ohlcv), so the two parts add up to the close-to-close log return."""
    if "intraday" in context:
        return context["intraday"]
    out = {}
    for s in panel.symbols:
        f = panel.f[s]
        a, o, c = f["a"], f["o"], f["c"]
        size = len(a)
        parts = {k: array("d", [0.0]) * (size + 1)
                 for k in ("intraday", "overnight", "bars", "filled")}
        prev_a = prev_c = NAN
        for d in range(size):
            x = y = bar = fill = 0.0
            if a[d] == a[d] and o[d] == o[d] and a[d] > 0 and o[d] > 0:
                bar = 1.0
                x = math.log(a[d] / o[d])
                if prev_a == prev_a:
                    y = math.log(o[d] / prev_a)
                    if abs(o[d] * c[d] / a[d] - prev_c) <= 1e-6 * prev_c:
                        fill = 1.0
                prev_a, prev_c = a[d], c[d]
            for key, value in (("intraday", x), ("overnight", y), ("bars", bar),
                               ("filled", fill)):
                parts[key][d + 1] = parts[key][d] + value
        out[s] = parts
    context["intraday"] = out
    return out


def intraday_score(panel, context, s, i, part="intraday", min_bars=0.95, max_filled=0.20):
    """T9: the sum of `part` (intraday, or the overnight placebo) over the 12-1 window, the
    trading days in (ME(i-12), ME(i-1)]. None if either end has no price, or the window fails
    the pre-registered data check: bars on under 95% of its days, or 20% or more of its opens
    back-filled."""
    A, B = month_end_before(panel, i, 12), month_end_before(panel, i, 1)
    if A is None or B is None or not panel.price(s, A) or not panel.price(s, B):
        return None
    p = intraday_parts(panel, context)[s]
    days = B - A
    bars = p["bars"][B + 1] - p["bars"][A + 1]
    filled = p["filled"][B + 1] - p["filled"][A + 1]
    if days <= 0 or bars < min_bars * days or filled >= max_filled * days:
        return None
    return p[part][B + 1] - p[part][A + 1]


def intraday_check_notes(panel, context, which="sp500"):
    """How many stock-months with a 12-1 score the open-data check rejected, before 2010 and
    after: Yahoo's early opens are the weak point of T9."""
    ok = context["universes"][which]
    counts = {"before 2010": [0, 0], "from 2010": [0, 0]}
    for i in panel.month_ends:
        era = "before 2010" if panel.day(i).year < 2010 else "from 2010"
        for s in ok:
            if s not in panel.f or not panel.member(s, i) or momentum_score(panel, s, i) is None:
                continue
            counts[era][0] += 1
            if intraday_score(panel, context, s, i) is None:
                counts[era][1] += 1
    return ["the open-data check rejected " + ", ".join(
        "%d of %d stock-months %s (%.0f%%)" % (bad, total, era, 100.0 * bad / total if total else 0)
        for era, (total, bad) in counts.items())]


def ew_monthly(panel, context):
    """{month end index: equal-weight universe return over the month ending there}."""
    if "ew_monthly" not in context:
        daily = features(panel, context) and context["ew"]["sp500"]
        out, ends = {}, panel.month_ends
        for a, b in zip(ends, ends[1:]):
            growth = 1.0
            for i in range(a + 1, b + 1):
                growth *= 1.0 + daily[i]
            out[b] = growth - 1.0
        context["ew_monthly"] = out
    return context["ew_monthly"]


def residual_score(panel, context, s, i, fit=36, form=11, skip=1):
    """Blitz, Huij and Martens (2011): regress the stock's monthly returns on the market's
    over `fit` months; score the residuals of months t-11..t-1 by their mean over their
    standard deviation. The equal-weight universe stands in for the market factor."""
    ends = panel.month_ends
    if i not in ends:
        return None
    k = ends.index(i)
    if k < fit + 1:
        return None
    market = ew_monthly(panel, context)
    xs, ys = [], []
    for j in range(k - fit + 1, k + 1):
        a, b = panel.price(s, ends[j - 1]), panel.price(s, ends[j])
        if a and b and ends[j] in market:
            xs.append(market[ends[j]])
            ys.append(b / a - 1.0)
        else:
            xs.append(None)
            ys.append(None)
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None]
    if len(pairs) < fit * 0.75:
        return None
    mx = sum(x for x, _ in pairs) / len(pairs)
    my = sum(y for _, y in pairs) / len(pairs)
    var = sum((x - mx) ** 2 for x, _ in pairs)
    beta = sum((x - mx) * (y - my) for x, y in pairs) / var if var else 0.0
    alpha = my - beta * mx
    window = [(x, y) for x, y in zip(xs[-(form + skip):-skip], ys[-(form + skip):-skip])
              if x is not None]
    if len(window) < form * 0.75:
        return None
    resid = [y - alpha - beta * x for x, y in window]
    mean = sum(resid) / len(resid)
    sd = math.sqrt(sum((e - mean) ** 2 for e in resid) / (len(resid) - 1)) if len(resid) > 1 else 0
    return mean / sd if sd > 0 else None


def info_discreteness(panel, context, s, i, start=252, end=21):
    """Da, Gurun and Warachka (2014): sign(formation return) x (%negative - %positive days)
    over the formation window. Low means the move came in many small steps."""
    ret = features(panel, context)[s]["ret"]
    days = [ret[j] for j in range(max(1, i - start), i - end + 1) if ret[j] == ret[j]]
    if len(days) < (start - end) * 0.8:
        return None
    a, b = panel.back(s, i - start), panel.back(s, i - end)
    if not a or not b:
        return None
    pos = sum(1 for r in days if r > 0) / len(days)
    neg = sum(1 for r in days if r < 0) / len(days)
    return (1.0 if b >= a else -1.0) * (neg - pos)


def trend_exposure(panel, context, months=10):
    """1 when the equal-weight universe index is above its average of the last `months` month
    ends, else 0 (Faber 2007), decided at the month end with data to that day only."""
    daily = features(panel, context) and context["ew"]["sp500"]
    level, levels = 1.0, []
    for r in daily:
        level *= 1.0 + r
        levels.append(level)

    def exposure(i, held):
        ends = [e for e in panel.month_ends if e <= i][-months:]
        if len(ends) < months:
            return 1.0
        return 1.0 if levels[i] > sum(levels[e] for e in ends) / months else 0.0
    return exposure


def vol_managed_exposure(panel, context, target=0.25, lookback=126):
    """Barroso and Santa-Clara (2015), long-only: invest target / realised volatility of the
    chosen names' equal-weight daily returns over the last `lookback` days, capped at 1, as
    the bot is long-only and never borrows. The rest earns nothing."""
    feats = features(panel, context)

    def exposure(i, held):
        if not held:
            return 1.0
        rets = []
        for j in range(max(1, i - lookback + 1), i + 1):
            day = [feats[s]["ret"][j] for s in held if feats[s]["ret"][j] == feats[s]["ret"][j]]
            if day:
                rets.append(sum(day) / len(day))
        if len(rets) < lookback * 0.8:
            return 1.0
        mean = sum(rets) / len(rets)
        vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252)
        return min(1.0, target / vol) if vol > 0 else 1.0
    return exposure


def sleeve_test(label, score_fn=None, pick_fn=None, which="sp500", n=10, notes_fn=None,
                **book):
    """A momentum-sleeve variant on a universe, point in time. Its report (turnover, and the
    per-month log the judges read) is kept in context["reports"][label]; notes_fn(panel,
    context) can add lines to the notes."""
    def run(panel, context):
        ok = context["universes"][which]
        eligible = lambda s, i: s in ok and panel.member(s, i) and panel.price(s, i)
        score = score_fn(panel, context) if score_fn else (
            lambda s, i: momentum_score(panel, s, i))
        pick = pick_fn(panel, context, n) if pick_fn else None
        extra = {k: (v(panel, context) if callable(v) else v) for k, v in book.items()}
        report = {}
        monthly = monthly_book(panel, eligible, score, n=n, pick=pick, report=report, **extra)
        context.setdefault("series", {})[label] = monthly
        context.setdefault("reports", {})[label] = report
        notes = ["turnover %.0f%% of the sleeve a year, %.0f stock switches a year"
                 % (report.get("turnover", 0) * 100, report.get("trades", 0))]
        return {"series": [(label, monthly)],
                "notes": notes + (notes_fn(panel, context) if notes_fn else [])}
    run.labels = [label]
    return run


def tranche_test(label, K=3, lag=0, which="sp500", n=10):
    """T11's staggered sleeve: tranche_book on 12-1 momentum, point in time."""
    def run(panel, context):
        ok = context["universes"][which]
        eligible = lambda s, i: s in ok and panel.member(s, i) and panel.price(s, i)
        report = {}
        monthly = tranche_book(panel, eligible, lambda s, i: momentum_score(panel, s, i), n=n,
                               K=K, lag=lag, report=report)
        context.setdefault("series", {})[label] = monthly
        context.setdefault("reports", {})[label] = report
        return {"series": [(label, monthly)],
                "notes": ["turnover %.0f%% of the sleeve a year, %.0f stock switches a year "
                          "(%.0f orders across tranches), %.1f distinct names held on average"
                          % (report.get("turnover", 0) * 100, report.get("trades", 0),
                             report.get("orders", 0), report.get("names", 0))]}
    run.labels = [label]
    return run


def daily_rank_test(label, exit_rank, which="sp500", n=10):
    def run(panel, context):
        ok = context["universes"][which]
        report = {}
        monthly = daily_momentum_book(panel, lambda s, i: s in ok and panel.member(s, i), n=n,
                                      exit_rank=exit_rank, report=report)
        context.setdefault("series", {})[label] = monthly
        return {"series": [(label, monthly)],
                "notes": ["turnover %.0f%% of the sleeve a year, %.0f stock switches a year"
                          % (report.get("turnover", 0) * 100, report.get("trades", 0))]}
    return run


def bad_news_exit(panel, context, max_ar=-0.05, min_volume=3.0):
    """A held name trailing the universe by more than |max_ar| on min_volume times its average
    volume: the news-backed drop that drifts on (Chan 2003; Savor 2012)."""
    feats = features(panel, context)

    def rule(s, d):
        f = feats[s]
        ar, v, avg = f["ar"][d], panel.f[s]["v"][d], f["vol50"][d]
        return ar == ar and avg == avg and avg > 0 and ar <= max_ar and v >= min_volume * avg
    return rule


def frog_pick(panel, context, n, pool=3):
    def pick(ranked, i):
        candidates = ranked[:pool * n]
        scored = [(info_discreteness(panel, context, s, i), s) for s in candidates]
        scored = [(d, s) for d, s in scored if d is not None]
        return [s for _, s in sorted(scored)[:n]]
    return pick


def ea_agree_pick(panel, context, n, which="sp500"):
    """T8 alternative, the agreement filter: the 12-1 top n, but a pick whose latest
    earnings-like move went against it (EAR < 0) is swapped for the next 12-1 name whose
    latest EAR is known and >= 0. A name with no earnings-like day found is kept. The latest
    EAR is the newest block's, whether or not three blocks were found."""
    def latest(s, i):
        found = ea_blocks(panel, context, s, i, which)
        return found[-1][1] if found else None

    def pick(ranked, i):
        kept = [s for s in ranked[:n] if latest(s, i) is None or latest(s, i) >= 0]
        for s in ranked[n:]:
            if len(kept) >= n:
                break
            value = latest(s, i)
            if value is not None and value >= 0:
                kept.append(s)
        return kept
    return pick


def sector_reversal_pick(lowest=True, pool=20, min_peers=5):
    """T10: of the 12-1 top `pool`, hold the n with the lowest (default) or highest (mirror)
    last-month return relative to the median of the ranked names in the same GICS sector,
    sector labels compared as universe.norm reads them. A sector with fewer than
    `min_peers` ranked names, or no label, is measured against the median of all of them.
    Ties go to the better 12-1 rank; a top-pool name with no last-month return sorts last."""
    def make(panel, context, n):
        import universe

        def sector(s):
            return universe.norm((panel.meta.get(s) or {}).get("sector", ""))

        def pick(ranked, i):
            prior = month_end_before(panel, i, 1)
            if prior is None:
                return ranked[:n]
            r1 = {}
            for s in ranked:
                a, b = panel.price(s, prior), panel.price(s, i)
                if a and b:
                    r1[s] = b / a - 1.0
            groups = {}
            for s, r in r1.items():
                groups.setdefault(sector(s), []).append(r)
            everyone = median(list(r1.values()))
            rank = {s: k for k, s in enumerate(ranked)}
            relative = {}
            for s in ranked[:pool]:
                if s not in r1:
                    continue
                peers = groups.get(sector(s), [])
                base = median(peers) if sector(s) and len(peers) >= min_peers else everyone
                relative[s] = r1[s] - base
            sign = 1.0 if lowest else -1.0
            have = sorted(relative, key=lambda s: (sign * relative[s], rank[s]))
            return (have + [s for s in ranked[:pool] if s not in relative])[:n]
        return pick
    return make


def book_test(label, sleeve_label, core_weight=0.6, which="sp500"):
    """The 60/40 book proxy: core_weight in the equal-weight universe (standing in for the
    core fund, which has too short a history), the rest in a sleeve, rebalanced monthly."""
    def run(panel, context):
        sleeve = context.get("series", {}).get(sleeve_label)
        if sleeve is None:
            return {"notes": ["run %s first" % sleeve_label]}
        core = monthly_from_daily(panel, features(panel, context) and context["ew"][which])
        return {"series": [(label, blend([(core_weight, core), (1 - core_weight, sleeve)]))]}
    return run


def event_test(label, signal_fn, hold, slots=5, which="sp500", idle=True, cost=COST,
               switch=0.0, yearly=False):
    """A short-horizon rule on the daily engine; idle money parked in the equal-weight index
    (as if in the core fund) unless idle is False. Switching costs into and out of the core
    are NOT charged, which flatters every idle line."""
    def run(panel, context):
        ok = context["universes"][which]
        feats = features(panel, context)
        eligible = lambda s, i: s in ok and panel.member(s, i)
        index = context["ew"]["sp500"] if idle else None
        daily, trades = event_book(panel, eligible, signal_fn(panel, feats, eligible), hold,
                                   slots,
                                   cost=cost, idle=index, switch=switch)
        monthly = monthly_from_daily(panel, daily)
        context.setdefault("series", {})[label] = monthly
        notes = [trade_summary(trades)]
        if yearly:
            base = dict(monthly_from_daily(panel, context["ew"]["sp500"]))
            mine = dict(monthly)
            years = sorted({m[:4] for m in mine})
            wins, line = 0, []
            for year in years:
                a = b = 1.0
                for m, r in mine.items():
                    if m.startswith(year):
                        a *= 1.0 + r
                        b *= 1.0 + base.get(m, 0.0)
                wins += a > b + 1e-9
                line.append("%s %+.0f/%+.0f" % (year, (a - 1) * 100, (b - 1) * 100))
            notes.append("beat the index in %d of %d calendar years (rule/index %%): %s"
                         % (wins, len(years), "  ".join(line)))
        return {"series": [(label, monthly)], "notes": notes}
    return run


def placebo_of(real_maker, salt=7):
    """Same days, random stocks: on each day the real rule signals k of n eligible names, fire
    on each eligible name with probability k/n by a fixed hash. If this does as well as the
    real rule, the edge was market timing or simply holding stocks, not the stock chosen."""
    import zlib

    def make(panel, feats, eligible):
        real = real_maker(panel, feats, eligible)
        odds = {}
        for i in range(len(panel.days)):
            names = [s for s in panel.symbols if eligible(s, i)]
            k = sum(1 for s in names if real(s, i) is not None)
            if k:
                odds[i] = k / len(names)

        def signal(s, i):
            p = odds.get(i)
            if not p:
                return None
            draw = zlib.crc32(("%s|%d|%d" % (s, i, salt)).encode()) / 2.0 ** 32
            return draw if draw < p else None
        return signal
    return make


def gap_book_test(label, gap_label, mom_label="mom10", core_weight=0.6):
    """The book with its core managed by the gap rule: core_weight of the account waits in
    the index (the core fund) and moves into news-gap trades when they fire."""
    def run(panel, context):
        series = context.get("series", {})
        if gap_label not in series or mom_label not in series:
            return {"notes": ["run %s and %s first" % (gap_label, mom_label)]}
        return {"series": [(label, blend([(core_weight, series[gap_label]),
                                          (1 - core_weight, series[mom_label])]))]}
    return run


def news_gap(min_ar=0.05, min_volume=3.0, min_gap=0.0, uptrend=False):
    """A day whose return beats the universe by min_ar on volume min_volume times its 50-day
    average, optionally opening min_gap above the prior close: the keyless proxy for an
    earnings or news surprise (Chan 2003; Gervais, Kaniel and Mingelgrin 2001)."""
    def make(panel, feats, eligible=None):
        def signal(s, i):
            f = feats[s]
            ar, v, avg = f["ar"][i], panel.f[s]["v"][i], f["vol50"][i]
            if not (ar == ar and avg == avg and avg > 0 and v == v):
                return None
            if ar < min_ar or v < min_volume * avg:
                return None
            if min_gap and not (f["gap"][i] == f["gap"][i] and f["gap"][i] >= min_gap):
                return None
            if uptrend:
                sma = f["sma200"][i]
                if not (sma == sma and panel.f[s]["a"][i] > sma):
                    return None
            return ar
        return signal
    return make


def breakout(min_volume=2.0):
    """A close above the highest close of the prior 252 days, on volume min_volume times
    its 50-day average (George and Hwang 2004; breakouts with volume)."""
    def make(panel, feats, eligible=None):
        def signal(s, i):
            f = feats[s]
            hi, a, v, avg = f["hi252"][i], panel.f[s]["a"][i], panel.f[s]["v"][i], f["vol50"][i]
            if not (hi == hi and a == a and avg == avg and avg > 0):
                return None
            if a > hi and v >= min_volume * avg:
                return v / avg
            return None
        return signal
    return make


def big_drop(max_ar=-0.05, min_volume=2.0):
    """A day the stock trails the universe by more than |max_ar| on heavy volume while above
    its 200-day average: the overreaction rebound, which Chan (2003) finds for no-news drops."""
    def make(panel, feats, eligible=None):
        def signal(s, i):
            f = feats[s]
            ar, v, avg, sma = f["ar"][i], panel.f[s]["v"][i], f["vol50"][i], f["sma200"][i]
            if not (ar == ar and avg == avg and avg > 0 and sma == sma):
                return None
            if ar <= max_ar and v >= min_volume * avg and panel.f[s]["a"][i] > sma:
                return -ar
            return None
        return signal
    return make


TESTS["base_spus"] = universe_test("spus")
TESTS["base_sp500"] = universe_test("sp500")
TESTS["base_sp500_nopit"] = universe_test("sp500", point_in_time=False)
TESTS["base_sp900"] = universe_test("sp900")
TESTS["ew_sp500"] = equal_weight_test("sp500")

# Momentum sleeve variants, all on the not-haram S&P 500, point in time, top 10 unless named.
TESTS["mom10"] = sleeve_test("mom10")
TESTS["mom5"] = sleeve_test("mom5", n=5)
TESTS["mom20"] = sleeve_test("mom20", n=20)
TESTS["residual10"] = sleeve_test("residual10", lambda p, c: (
    lambda s, i: residual_score(p, c, s, i)))
TESTS["frog10"] = sleeve_test("frog10", pick_fn=frog_pick)
TESTS["voladj10"] = sleeve_test("voladj10", lambda p, c: (
    lambda s, i: (lambda m, v: m / v if m is not None and v else None)(
        momentum_score(p, s, i), daily_vol(p, c, s, i))))
TESTS["intermediate10"] = sleeve_test("intermediate10", lambda p, c: (
    lambda s, i: momentum_score(p, s, i, start=12, skip=7)))
TESTS["high52w10"] = sleeve_test("high52w10", lambda p, c: (
    lambda s, i: (lambda a, h: a / h if a and h == h and h > 0 else None)(
        p.price(s, i), features(p, c)[s]["hi252"][i])))
TESTS["mom10_sector3"] = sleeve_test("mom10_sector3", sector_cap=3)
TESTS["mom10_trend"] = sleeve_test("mom10_trend", exposure=trend_exposure)
TESTS["mom10_volmanaged"] = sleeve_test("mom10_volmanaged", exposure=vol_managed_exposure)
TESTS["book60"] = book_test("book 60/40", "mom10")
TESTS["book40"] = book_test("book 40/60", "mom10", core_weight=0.4)
TESTS["book20"] = book_test("book 20/80", "mom10", core_weight=0.2)

# Holding longer, pre-registered 2026-10-07: does trading less often cost anything? The data
# alone decides. A variant replaces monthly 12-1 top 10 only if it trades at least 30% less,
# its full-period CAGR is within 0.5 points or better, it is within 2 points in each half and
# the last five years, and its worst drawdown is no more than 3 points deeper. A tie goes to
# lower turnover, as costs are certain and return differences are noise. The bad-news exit is
# a different kind: it adds trades, so it must beat mom10 on CAGR in both halves, or cut the
# worst drawdown by 5 points with CAGR within 0.5.
TESTS["hold_band15"] = sleeve_test("hold band: top 15", buffer=15)
TESTS["hold_band20"] = sleeve_test("hold band: top 20", buffer=20)
TESTS["hold_band30"] = sleeve_test("hold band: top 30", buffer=30)
TESTS["repick_quarterly"] = sleeve_test("re-pick every 3 months", every=3)
TESTS["daily_rank30"] = daily_rank_test("daily rank, exit past 30", 30)
TESTS["daily_rank50"] = daily_rank_test("daily rank, exit past 50", 50)
TESTS["mom10_badexit"] = sleeve_test("mom10 + bad-news exit", exit_rule=bad_news_exit,
                                     idle=lambda p, c: features(p, c) and c["ew"]["sp500"])

# Fill lag and the hold-band robustness check, pre-registered 2026-10-07 before running.
# Fill lag (from the research synthesis): the live 7-day window counts as safe if mom10 and
# the 60/40 book at L=3 and L=5 are within 1.0 point a year of L=1 in the full sample and
# each half, and the order of mom10, mom5 and mom10_trend does not flip between L=1 and L=5;
# if not, reliable scheduling becomes the first priority. Hold band 30 passed the holding
# rule once on the final universe while bands 15 and 20 failed; it is adopted only if, at
# BOTH L=1 and L=5, it passes the holding rule against mom10 at the same lag, AND bands 25
# and 35 also pass it at L=1.
for L in (1, 3, 5):
    TESTS["mom10_L%d" % L] = sleeve_test("mom10 L=%d" % L, lag=L)
    TESTS["book60_L%d" % L] = book_test("book 60/40 L=%d" % L, "mom10 L=%d" % L)
for L in (1, 5):
    TESTS["mom5_L%d" % L] = sleeve_test("mom5 L=%d" % L, n=5, lag=L)
    TESTS["trend_L%d" % L] = sleeve_test("mom10_trend L=%d" % L, exposure=trend_exposure, lag=L)
    TESTS["band30_L%d" % L] = sleeve_test("hold band 30 L=%d" % L, buffer=30, lag=L)
TESTS["band25_L1"] = sleeve_test("hold band 25 L=1", buffer=25, lag=1)
TESTS["band35_L1"] = sleeve_test("hold band 35 L=1", buffer=35, lag=1)

# Short-horizon "temporary gap" rules on the daily engine, idle money in the index.
TESTS["newsgap20"] = event_test("news gap, hold 20", news_gap(), 20)
TESTS["newsgap60"] = event_test("news gap, hold 60", news_gap(), 60)
TESTS["newsgap_open20"] = event_test("news gap + open gap, 20", news_gap(min_gap=0.02), 20)
TESTS["breakout20"] = event_test("52w breakout, hold 20", breakout(), 20)
TESTS["breakout60"] = event_test("52w breakout, hold 60", breakout(), 60)
TESTS["bigdrop5"] = event_test("big drop rebound, hold 5", big_drop(), 5)

# Robustness of the news-gap rule, pre-registered 2026-10-07 after newsgap60 passed: one
# parameter at a time around the default (5% over the index, 3x volume, 60 days, 5 slots),
# harsher costs, a same-days placebo, and the whole book. Adopted only if every neighbour
# stays positive per trade with 6 of 8 beating the index, the harsh-cost line beats the
# index in both halves, the placebo trails by 1.5 points a year or more, and the gap-managed
# book beats the plain 60/40 in both halves.
TESTS["gap_default"] = event_test("gap 5%/3x/60d/5", news_gap(), 60, yearly=True)
TESTS["gap_hold40"] = event_test("gap hold 40", news_gap(), 40)
TESTS["gap_hold90"] = event_test("gap hold 90", news_gap(), 90)
TESTS["gap_ar4"] = event_test("gap 4% over index", news_gap(min_ar=0.04), 60)
TESTS["gap_ar7"] = event_test("gap 7% over index", news_gap(min_ar=0.07), 60)
TESTS["gap_vol2"] = event_test("gap volume 2x", news_gap(min_volume=2.0), 60)
TESTS["gap_vol4"] = event_test("gap volume 4x", news_gap(min_volume=4.0), 60)
TESTS["gap_slots10"] = event_test("gap 10 slots", news_gap(), 60, slots=10)
TESTS["gap_uptrend"] = event_test("gap above 200-day", news_gap(uptrend=True), 60)
TESTS["gap_harsh"] = event_test("gap costs .3% + .1% switch", news_gap(), 60, cost=0.003,
                                switch=0.001)
TESTS["gap_placebo"] = event_test("gap placebo, same days", placebo_of(news_gap()), 60)


def placebo_distribution(label, real_maker, hold, draws=40, which="sp500"):
    """The real rule's CAGR against `draws` placebos, each a different random choice of
    names on the same days. One draw is noise (a review found single-draw CAGRs spread by
    over 3 points); the percentile is the evidence."""
    def run(panel, context):
        ok = context["universes"][which]
        feats = features(panel, context)
        eligible = lambda s, i: s in ok and panel.member(s, i)
        index = context["ew"]["sp500"]

        def cagr(maker):
            daily, _ = event_book(panel, eligible, maker(panel, feats, eligible), hold, 5,
                                  idle=index)
            m = stats(monthly_from_daily(panel, daily))
            return m["cagr"] if m else None
        real = cagr(real_maker)
        fakes = sorted(c for c in (cagr(placebo_of(real_maker, salt=k)) for k in range(draws))
                       if c is not None)
        beaten = sum(1 for c in fakes if c < real) if real is not None else 0
        return {"notes": ["real %.1f%% a year; %d placebos from %.1f%% to %.1f%%, median %.1f%%;"
                          " the real rule beat %d of %d (%.0fth percentile)"
                          % (real * 100, len(fakes), fakes[0] * 100, fakes[-1] * 100,
                             fakes[len(fakes) // 2] * 100, beaten, len(fakes),
                             100.0 * beaten / len(fakes))]} if fakes and real else {
            "notes": ["no result"]}
    return run


TESTS["gap_placebo_many"] = placebo_distribution("news gap vs 40 placebos", news_gap(), 60)
TESTS["book_plain"] = book_test("book 60 core/40 mom", "mom10")
TESTS["book_gapcore"] = gap_book_test("book 60 gap-core/40 mom", "gap 5%/3x/60d/5")
TESTS["book_gapcore10"] = gap_book_test("book 60 gap-core10/40 mom", "gap 10 slots")


# T7 to T11, pre-registered by the research synthesis of 2026-10-07 and recorded here on
# 2026-10-08, before any of them was run. Yardstick T1: mom10 at L=1 (filled at the close
# after the signal), checked again at L=5 against mom10 at L=5. 0.20% per side throughout.
#
# House replacement rule: at L=1 and 0.20% per side, on common months, the variant beats 12-1
# by at least 1.0 point a year in full-sample CAGR, AND beats it in the first half, the
# second half and the last 5 years, AND its worst drawdown is no more than 5 points deeper,
# AND its full-sample edge keeps its sign at L=5. Otherwise 12-1 stays.
#
# T7 skip_window. 12-0 = adj[ME(i)] / adj[ME(i-12)] - 1. 12-1w = adj[the trading day 5
# sessions before ME(i)] / adj[ME(i-12)] - 1. Nothing else. Judged by the house rule.
#
# T8 eamom10_proxy. At month-end i, for each eligible stock, split the 252 trading days
# ending at i into four 63-day blocks; in each block take the day d with the highest
# (v_d / vol50_d) divided by that day's universe median of v/vol50 (neutralises
# quad-witching and index days); skip the block if that ratio < 2. ar = the stock's daily
# return minus the equal-weight universe return. EAR_k = sum of ar over
# [d-2, min(d+2, i)], truncated at i, no look-ahead. Require >= 3 found days.
# EAmom = sum of EAR_k. DEFAULT: top 10 by EAmom. ALTERNATIVE (agreement filter): mom10,
# but any pick whose most recent EAR_k < 0 is swapped for the next-ranked 12-1 name whose
# latest EAR_k >= 0. Passes by either path. (R) the house rule. (D) full-sample CAGR within
# 0.5 point of T1, each half and the last 5 years within 2 points, AND worst drawdown at
# least 5 points shallower; decided 2026-10-08, before the run, that D must hold at L=5 as
# well, as every idea must. The SEC 8-K cross-check is dropped: it decides nothing.
#
# T9 intraday_momentum. Data check per stock and window: >= 95% of days with a valid open,
# and < 20% of days whose raw open equals the previous raw close within 1e-6 relative
# (Yahoo back-fill). Score = the sum over trading days d in (ME(i-12), ME(i-1)] of
# ln(a_d / o_d), o on the adjusted basis. Placebo: the sum of ln(o_d / a_{d-1}) over the same
# window. Passes by the house rule against T1 AND the overnight placebo's full-sample CAGR
# is below both the intraday score's and T1's; if the placebo is not worse, it is noise.
#
# T10 sector_reversal_tilt. At month-end i, rank by 12-1 and keep the top 20. For each,
# r1 = adj[ME(i)] / adj[ME(i-1)] - 1 minus the median r1 of the eligible names in the same
# GICS sector (the universe median if the sector has fewer than 5). DEFAULT: hold the 10
# with the LOWEST sector-relative r1. MIRROR: the 10 with the HIGHEST. Judged by the house
# rule, net of the extra turnover; a pass needs every window, no partial credit. The mirror
# tests the opposite hypothesis and decides nothing here.
#
# T11 staggered_tranches. K=3 tranches, each starting with 1/3 of the sleeve; tranche j is
# rebuilt to the then-current top 10 by 12-1 only at month-ends whose month index mod 3 = j
# (fill at the close of i+1) and held 3 months; never rebalanced against each other; each
# pays 0.20% per side on its own trades. Judged by the holding-longer rule of 2026-10-07:
# at least 30% less turnover than T1, full-sample CAGR within 0.5 point, within 2 points in
# each half and the last 5 years, worst drawdown no more than 3 points deeper; a tie goes to
# lower turnover. Like every idea, it must also pass at L=5 against mom10 L=5.
#
# Operational definitions, fixed 2026-10-08 before the run: T8's blocks are days
# [i-251, i-189], [i-188, i-126], [i-125, i-63], [i-62, i]; the daily median is over the
# universe's members that day with volume and a 50-day average, at least 20 of them; ties go
# to the earliest day; "latest EAR" is the newest found block's. T9's previous close is the
# previous valid bar's, and both shares are of the window's trading days. T10's peers are the
# ranked names (eligible with a 12-1 score), sectors compared as universe.norm reads them, a
# blank sector against the universe median, ties to the better 12-1 rank. T11's month index
# is year*12 + month - 1 of the signal month end; every tranche buys at the first month.
# Every rule is applied to unrounded numbers: "beats" is strict, "within x" is one-sided
# (better is always fine), the worst drawdown is the full sample's.
for L in (1, 5):
    TESTS["skip0_L%d" % L] = sleeve_test("mom10 12-0 L=%d" % L, lambda p, c: (
        lambda s, i: momentum_score(p, s, i, skip=0)), lag=L)
    TESTS["skip1w_L%d" % L] = sleeve_test("mom10 12-1w L=%d" % L, lambda p, c: (
        lambda s, i: skip_days_score(p, s, i)), lag=L)
    TESTS["eamom_L%d" % L] = sleeve_test("eamom10 L=%d" % L, lambda p, c: (
        lambda s, i: eamom_score(p, c, s, i)), lag=L)
    TESTS["eaagree_L%d" % L] = sleeve_test("mom10 EA-agree L=%d" % L, pick_fn=ea_agree_pick,
                                           lag=L)
    TESTS["intraday_L%d" % L] = sleeve_test(
        "intraday mom10 L=%d" % L, lambda p, c: (lambda s, i: intraday_score(p, c, s, i)),
        lag=L, notes_fn=intraday_check_notes if L == 1 else None)
    TESTS["secrev_L%d" % L] = sleeve_test("sector reversal L=%d" % L,
                                          pick_fn=sector_reversal_pick(lowest=True), lag=L)
    TESTS["tranche3_L%d" % L] = tranche_test("tranches K=3 L=%d" % L, K=3, lag=L)
TESTS["overnight_L1"] = sleeve_test("overnight placebo L=1", lambda p, c: (
    lambda s, i: intraday_score(p, c, s, i, part="overnight")), lag=1)
TESTS["secmirror_L1"] = sleeve_test("sector mirror L=1",
                                    pick_fn=sector_reversal_pick(lowest=False), lag=1)


# ---------------------------------------------------------------------- judges

BASE = {1: "mom10 L=1", 5: "mom10 L=5"}
JUDGES = []   # {"title", "labels": candidate rows, "fn"(rows, reports, months) -> (verdict, checks)}


def pct(x):
    return x * 100.0


def compare(rows, cand, base):
    """{window: (stats of cand, stats of base)} over the four judging windows."""
    return {name: (stats(window(rows[cand], start, end)), stats(window(rows[base], start, end)))
            for name, start, end in periods(rows[base])}


def turnover_in(report, months):
    """One-side turnover a year over the judged months, from a book's per-month log."""
    log = (report or {}).get("log", {})
    return sum(log.get(m, (0.0, ()))[0] for m in months) / len(months) * 12 if months else 0.0


def thin(*windows):
    return any(c is None or b is None for w in windows for c, b in w.values())


def house_rule(rows, c1, c5):
    """The house replacement rule of the 2026-10-07 research synthesis: [(ok, reason)]."""
    w, w5 = compare(rows, c1, BASE[1]), compare(rows, c5, BASE[5])
    if thin(w, w5):
        return [(False, "too little data in a judging window")]
    (fc, fb), (fc5, fb5) = w["full"], w5["full"]
    edge, edge5 = fc["cagr"] - fb["cagr"], fc5["cagr"] - fb5["cagr"]
    out = [(edge >= 0.01, "full CAGR %.2f%% vs %.2f%%: edge %+.2f, needs >= +1.00"
            % (pct(fc["cagr"]), pct(fb["cagr"]), pct(edge)))]
    for name in ("first half", "second half", "last 5y"):
        c, b = w[name]
        out.append((c["cagr"] > b["cagr"], "%s CAGR %.2f%% vs %.2f%%, needs to be ahead"
                    % (name, pct(c["cagr"]), pct(b["cagr"]))))
    out.append((fc["max_dd"] >= fb["max_dd"] - 0.05,
                "worst fall %.2f%% vs %.2f%%, needs to be no more than 5.00 deeper"
                % (pct(fc["max_dd"]), pct(fb["max_dd"]))))
    out.append((edge5 * edge > 0, "at L=5 the full edge is %+.2f against %+.2f at L=1, needs "
                "the same sign" % (pct(edge5), pct(edge))))
    return out


def rule_d(rows, cand, base, tag):
    """T8's path D: as good, and safer. [(ok, reason)]."""
    w = compare(rows, cand, base)
    if thin(w):
        return [(False, "%s: too little data in a judging window" % tag)]
    fc, fb = w["full"]
    out = [(fc["cagr"] >= fb["cagr"] - 0.005, "%s full CAGR %.2f%% vs %.2f%%, needs to be within "
            "0.50" % (tag, pct(fc["cagr"]), pct(fb["cagr"])))]
    for name in ("first half", "second half", "last 5y"):
        c, b = w[name]
        out.append((c["cagr"] >= b["cagr"] - 0.02, "%s %s CAGR %.2f%% vs %.2f%%, needs to be "
                    "within 2.00" % (tag, name, pct(c["cagr"]), pct(b["cagr"]))))
    out.append((fc["max_dd"] >= fb["max_dd"] + 0.05, "%s worst fall %.2f%% vs %.2f%%, needs to "
                "be at least 5.00 shallower" % (tag, pct(fc["max_dd"]), pct(fb["max_dd"]))))
    return out


def holding_rule(rows, reports, months, cand, base, tag):
    """The holding-longer rule of 2026-10-07: [(ok, reason)]."""
    w = compare(rows, cand, base)
    if thin(w):
        return [(False, "%s: too little data in a judging window" % tag)]
    turn_c, turn_b = turnover_in(reports.get(cand), months), turnover_in(reports.get(base), months)
    fc, fb = w["full"]
    out = [(turn_b > 0 and turn_c <= 0.7 * turn_b, "%s turnover %.0f%% vs %.0f%% a year, needs "
            "at least 30%% less" % (tag, pct(turn_c), pct(turn_b))),
           (fc["cagr"] >= fb["cagr"] - 0.005, "%s full CAGR %.2f%% vs %.2f%%, needs to be within "
            "0.50" % (tag, pct(fc["cagr"]), pct(fb["cagr"])))]
    for name in ("first half", "second half", "last 5y"):
        c, b = w[name]
        out.append((c["cagr"] >= b["cagr"] - 0.02, "%s %s CAGR %.2f%% vs %.2f%%, needs to be "
                    "within 2.00" % (tag, name, pct(c["cagr"]), pct(b["cagr"]))))
    out.append((fc["max_dd"] >= fb["max_dd"] - 0.03, "%s worst fall %.2f%% vs %.2f%%, needs to "
                "be no more than 3.00 deeper" % (tag, pct(fc["max_dd"]), pct(fb["max_dd"]))))
    return out


def judged(title, labels, fn):
    JUDGES.append({"title": title, "labels": list(labels), "fn": fn})


def house_judge(c1, c5):
    def fn(rows, reports, months):
        checks = house_rule(rows, c1, c5)
        return all(ok for ok, _ in checks), checks
    return fn


def ea_judge(c1, c5):
    def fn(rows, reports, months):
        r = house_rule(rows, c1, c5)
        d = rule_d(rows, c1, BASE[1], "L=1") + rule_d(rows, c5, BASE[5], "L=5")
        verdict = all(ok for ok, _ in r) or all(ok for ok, _ in d)
        return verdict, ([(None, "path R, the house rule:")] + r
                         + [(None, "path D, as good and safer, at L=1 and L=5:")] + d)
    return fn


def intraday_judge(rows, reports, months):
    checks = house_rule(rows, "intraday mom10 L=1", "intraday mom10 L=5")
    full = {label: stats(rows[label])
            for label in ("overnight placebo L=1", "intraday mom10 L=1", BASE[1])}
    if all(full.values()):
        night = full["overnight placebo L=1"]["cagr"]
        for label, name in (("intraday mom10 L=1", "intraday"), (BASE[1], "mom10")):
            checks.append((night < full[label]["cagr"], "overnight placebo %.2f%% vs %s %.2f%%, "
                           "needs to be lower" % (pct(night), name, pct(full[label]["cagr"]))))
    else:
        checks.append((False, "too little data for the placebo check"))
    return all(ok for ok, _ in checks), checks


def mirror_judge(rows, reports, months):
    w = compare(rows, "sector mirror L=1", BASE[1])
    return None, [(None, "%s CAGR %.2f%% vs %.2f%%" % (name, pct(c["cagr"]), pct(b["cagr"])))
                  for name, (c, b) in w.items() if c and b]


def tranche_judge(rows, reports, months):
    checks = (holding_rule(rows, reports, months, "tranches K=3 L=1", BASE[1], "L=1")
              + holding_rule(rows, reports, months, "tranches K=3 L=5", BASE[5], "L=5"))
    return all(ok for ok, _ in checks), checks


judged("T7 12-0 momentum", ["mom10 12-0 L=1", "mom10 12-0 L=5"],
       house_judge("mom10 12-0 L=1", "mom10 12-0 L=5"))
judged("T7 12-1 with a 5-day skip", ["mom10 12-1w L=1", "mom10 12-1w L=5"],
       house_judge("mom10 12-1w L=1", "mom10 12-1w L=5"))
judged("T8 earnings-announcement momentum", ["eamom10 L=1", "eamom10 L=5"],
       ea_judge("eamom10 L=1", "eamom10 L=5"))
judged("T8 mom10 with the earnings agreement filter",
       ["mom10 EA-agree L=1", "mom10 EA-agree L=5"],
       ea_judge("mom10 EA-agree L=1", "mom10 EA-agree L=5"))
judged("T9 intraday-only momentum",
       ["intraday mom10 L=1", "intraday mom10 L=5", "overnight placebo L=1"], intraday_judge)
judged("T10 sector reversal inside the top 20", ["sector reversal L=1", "sector reversal L=5"],
       house_judge("sector reversal L=1", "sector reversal L=5"))
judged("T10 mirror, the leaders inside the top 20", ["sector mirror L=1"], mirror_judge)
judged("T11 three staggered tranches", ["tranches K=3 L=1", "tranches K=3 L=5"], tranche_judge)


def judge_all(rows, reports, months):
    """Each judge whose candidate rows are present, applied to the rows as tabled: PASS or
    FAIL with every condition and its numbers, or NOT JUDGED naming the missing rows."""
    lines = []
    for j in JUDGES:
        if not any(label in rows for label in j["labels"]):
            continue
        missing = [label for label in j["labels"] + [BASE[1], BASE[5]] if label not in rows]
        if missing:
            lines.append("%s: NOT JUDGED, missing rows: %s" % (j["title"], ", ".join(missing)))
            continue
        verdict, checks = j["fn"](rows, reports, months)
        lines.append("%s: %s" % (j["title"], "DIAGNOSTIC, decides nothing" if verdict is None
                                 else "PASS" if verdict else "FAIL"))
        for ok, text in checks:
            lines.append("    %s  %s" % ("    " if ok is None else "pass" if ok else "FAIL", text))
        first = j["labels"][0]
        fc, fb = stats(rows[first]), stats(rows[BASE[1]])
        turn_c = turnover_in(reports.get(first), months)
        turn_b = turnover_in(reports.get(BASE[1]), months)
        if fc and fb:
            edge = fc["cagr"] - fb["cagr"]
            extra = ""
            if edge > 0 and turn_c > turn_b:
                extra = ("; it stays ahead unless costs run %.2f%% a side above the 0.20%% "
                         "assumed" % pct(edge / (2.0 * (turn_c - turn_b))))
            lines.append("    turnover %.0f%% vs %.0f%% a year%s" % (pct(turn_c), pct(turn_b),
                                                                   extra))
    return lines


# ---------------------------------------------------------------------- commands

def cmd_fetch(args):
    meta, notes = load_universe(args.indexes or ["sp500"])
    for note in notes:
        print("  %s" % note)
    got = 0
    for number, symbol in enumerate(sorted(meta)):
        if not (CACHE / ("%s.json" % symbol)).exists() and number:
            time.sleep(0.4)
        if cached(symbol):
            got += 1
    (CACHE / "_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    print("daily history cached for %d of %d names" % (got, len(meta)))
    return 0


def load_panel(min_bars=300):
    meta = json.loads((CACHE / "_meta.json").read_text(encoding="utf-8"))
    raw = {}
    for symbol in meta:
        path = CACHE / ("%s.json" % symbol)
        if path.exists():
            series = json.loads(path.read_text(encoding="utf-8"))
            if len(series.get("t", [])) >= min_bars:
                raw[symbol] = series
    return Panel(raw, meta)


def cmd_run(args):
    panel = load_panel()
    print("panel: %d names, %s to %s, %d month ends"
          % (len(panel.symbols), panel.day(0), panel.day(len(panel.days) - 1),
             len(panel.month_ends)))
    import gates
    kept, dropped = screened(panel, gates.load())
    print("screen keeps %d index members and drops %d" % (len(kept), len(dropped)))
    reasons = {}
    for symbol, why in dropped:
        reasons.setdefault(why.split(" (")[0].split(":")[0], []).append(symbol)
    for why, symbols in sorted(reasons.items()):
        print("  dropped %-40s %s" % (why[:40], " ".join(sorted(symbols))))
    spus = {s for s, m in panel.meta.items() if m.get("spus")}
    sp500 = {s for s in kept if panel.meta[s].get("index") == "sp500"}
    undated = sorted(s for s in sp500 | spus if (panel.meta.get(s) or {}).get("added") is None
                     and s in panel.f)
    print("undated names (counted as members throughout, so still pre-inclusion biased): %d: %s"
          % (len(undated), " ".join(undated[:40])))
    print("calendar days dropped as data errors (few names had a bar): %d %s"
          % (len(panel.dropped_days), " ".join(str(date.fromordinal(d))
                                                 for d in panel.dropped_days[:10])))
    context = {"universes": {"spus": spus, "sp500": sp500, "sp900": set(kept)}}
    print("universes: spus %d, sp500 %d, sp500+400 %d; in SPUS but dropped by the screen: %s"
          % (len(spus), len(sp500), len(kept),
             " ".join(sorted(spus & {s for s, _ in dropped})) or "none"))
    names = args.tests or list(TESTS)
    results = []
    for name in names:
        if name not in TESTS:
            print("unknown test %s" % name)
            continue
        started = time.time()
        result = TESTS[name](panel, context)
        for label, monthly in result.get("series", []):
            results.append((label, monthly))
        for line in result.get("notes", []):
            print("  %s: %s" % (name, line))
        print("  %s took %.0fs" % (name, time.time() - started))
    if not results:
        return 0
    # Every row over the SAME months: a test that needs a longer warm-up (residual momentum
    # needs three years) would otherwise skip the 2008 crash that the others are scored on.
    empty = [label for label, monthly in results if not monthly]
    for label in empty:
        print("  %s: no data, left out of the tables" % label)
    results = [(label, monthly) for label, monthly in results if monthly]
    if not results:
        return 0
    firsts = {label: monthly[0][0] for label, monthly in results}
    earliest = min(firsts.values())
    for label, first in sorted(firsts.items(), key=lambda x: (x[1], x[0])):
        if first > earliest:
            print("  %s starts %s; every row is cut to the months all rows share" % (label, first))
    shared = set.intersection(*(set(m for m, _ in monthly) for _, monthly in results))
    results = [(label, [(m, r) for m, r in monthly if m in shared])
               for label, monthly in results]
    months = sorted(shared)
    print("\nall rows scored over the same %d months, %s to %s"
          % (len(months), months[0] if months else "-", months[-1] if months else "-"))
    for label, start, end in periods(results[0][1]):
        print("\n%s%s\n%s" % (label.upper(), "" if start is None and end is None else
                               " (%s to %s)" % (start or "start", end or "end"),
                               table(results, start, end)))
    verdicts = judge_all(dict(results), context.get("reports", {}), months)
    if verdicts:
        print("\nJUDGES (rules pre-registered 2026-10-07 and recorded 2026-10-08 before the run, "
              "applied to unrounded numbers over these months)")
        if months and months[0] > "2008-08":
            print("  note: the judged months start %s, so the 2008 crash is not in them" % months[0])
        for line in verdicts:
            print(line)
    return 0


def cmd_selftest(args):
    checks = []

    def expect(name, condition):
        checks.append((name, bool(condition)))

    stamps = [1704153600 + 86400 * k for k in range(4)]   # 2024-01-02 onwards
    payload = {"chart": {"result": [{"timestamp": stamps, "indicators": {
        "quote": [{"open": [10, 11, None, 12], "high": [11, 12, 12, 13],
                   "low": [9, 10, 10, 11], "close": [10, 11, 11, 12],
                   "volume": [100, 200, 150, None]}],
        "adjclose": [{"adjclose": [5, 5.5, 5.5, 6]}]}}]}}
    series = parse_ohlcv(payload)
    expect("rows with a missing price are dropped", len(series["t"]) == 3)
    expect("open is put on the adjusted basis", series["o"][0] == 5.0 and series["a"][0] == 5.0)
    expect("a missing volume is zero, not a dropped row", series["v"][-1] == 0)

    def make(prices, start=date(2020, 1, 1), volume=None, opens=None, drop=()):
        days, out = [], {k: [] for k in "tohlcav"}
        d = start.toordinal()
        for n, p in enumerate(prices):
            while date.fromordinal(d).weekday() >= 5:
                d += 1
            if n in drop:       # no bar that day: a gap in this name's history
                d += 1
                continue
            for key, value in zip("tohlcav", (d, opens[n] if opens else p, p, p, p, p,
                                              (volume or [1000] * len(prices))[n])):
                out[key].append(value)
            d += 1
        return out

    rising = [100 * 1.001 ** k for k in range(600)]
    flat = [100.0] * 600
    falling = [100 * 0.999 ** k for k in range(600)]
    extra = {"F%d" % k: make([100.0 + 0.01 * k] * 600) for k in range(20)}
    panel = Panel(dict(extra, UP=make(rising), FLAT=make(flat), DOWN=make(falling)),
                  {"UP": {"added": None, "sector": "Tech"}, "FLAT": {"added": None},
                   "DOWN": {"added": None}})
    expect("all symbols share one calendar", len(panel.f["UP"]["a"]) == len(panel.days))
    expect("month ends are the last trading day of each month",
           all(panel.day(i).month != panel.day(i + 1).month for i in panel.month_ends))
    i = panel.month_ends[13]
    expect("12-1 momentum ranks the riser above the faller",
           momentum_score(panel, "UP", i) > 0 > momentum_score(panel, "DOWN", i))
    expect("too early for 12-1 gives no score",
           momentum_score(panel, "UP", panel.month_ends[5]) is None)

    always = lambda s, i: True
    book = monthly_book(panel, always, lambda s, i: momentum_score(panel, s, i), n=1, cost=0.0)
    expect("the monthly book holds the riser and earns its return",
           book and all(r > 0 for _, r in book))
    costly = monthly_book(panel, always, lambda s, i: momentum_score(panel, s, i), n=1,
                          cost=0.05)
    expect("costs are charged on the first buy", costly[0][1] < book[0][1])
    expect("and not again while the holding is unchanged",
           abs(costly[1][1] - book[1][1]) < 1e-12)
    half = monthly_book(panel, always, lambda s, i: momentum_score(panel, s, i), n=1,
                        cost=0.0, exposure=lambda i, held: 0.5)
    expect("half exposure earns about half", abs(half[0][1] - book[0][1] / 2) < 1e-9)
    later = Panel(dict(extra, UP=make(rising), DOWN=make(falling)),
                  {"UP": {"added": panel.days[400]}, "DOWN": {"added": None}})
    member = lambda s, i: later.member(s, i)
    early = monthly_book(later, member, lambda s, i: momentum_score(later, s, i), n=1, cost=0.0)
    expect("a stock is not held before the index added it",
           all(r <= 0 for m, r in early
               if m <= later.day(400).strftime("%Y-%m")))

    # Holding longer: re-picking every third month trades less than every month, an exit
    # rule parks money in the core, and the daily-ranked book holds the riser.
    import zlib
    wobble = {"W%d" % k: make([100.0 * (1.0 + 0.002 * ((zlib.crc32(b"%d%d" % (k, d)) % 21) - 10)
                                        / 10.0) ** d for d in range(600)]) for k in range(30)}
    rot = Panel(wobble, {})
    mom = lambda s, i: momentum_score(rot, s, i)
    monthly_report, slow_report = {}, {}
    monthly_book(rot, always, mom, n=5, report=monthly_report)
    monthly_book(rot, always, mom, n=5, every=3, report=slow_report)
    expect("re-picking every third month trades less than every month",
           slow_report["turnover"] < monthly_report["turnover"])
    parked = monthly_book(panel, always, lambda s, i: momentum_score(panel, s, i), n=1,
                          cost=0.0, exit_rule=lambda s, d: True,
                          idle=[0.0] * len(panel.days))
    expect("an exit rule that always fires leaves the month earning the core, not the stock",
           parked and all(abs(r - (panel.back("UP", panel.month_ends[1] + 2) /
                                   panel.back("UP", panel.month_ends[1]) - 1.0)) < 0.01
                          for _, r in parked[:1]))
    ranked_report = {}
    ranked_daily = daily_momentum_book(panel, always, n=1, exit_rank=2, cost=0.0,
                                       report=ranked_report)
    expect("the daily-ranked book holds the riser and earns from it",
           ranked_daily and sum(r for _, r in ranked_daily) > 0 and ranked_report["trades"] < 1)

    late = monthly_book(panel, always, lambda s, i: momentum_score(panel, s, i), n=1,
                        cost=0.0, lag=3)
    on_time = monthly_book(panel, always, lambda s, i: momentum_score(panel, s, i), n=1,
                           cost=0.0)
    expect("a fill lag shifts the holding period but keeps one return a month",
           len(late) == len(on_time) and late[0][1] != on_time[0][1])

    jump = [100.0] * 300 + [110.0] + [110.0 + 0.1 * k for k in range(299)]
    vol = [1000] * 300 + [5000] + [1000] * 299
    events = Panel(dict(extra, JUMP=make(jump, volume=vol)), {})
    signal = lambda s, i: (1.0 if s == "JUMP" and events.price(s, i - 1)
                           and events.price(s, i) / events.price(s, i - 1) > 1.05 else None)
    curve, trades = event_book(events, always, signal, hold=20, slots=1, cost=0.0)
    expect("the event book takes the signal", len(trades) == 1)
    expect("entering at the NEXT close, not the signal close", trades[0][1] == 301)
    expect("and holding for the set number of days", trades[0][2] - trades[0][1] == 20)
    expect("earning the drift after the jump, not the jump itself",
           abs(trades[0][3] - (112.0 / 110.0 - 1.0)) < 1e-9)
    expect("one curve point per day", len(curve) == len(events.days))
    idle = [0.001] * len(events.days)
    parked, _ = event_book(events, always, lambda s, i: None, hold=5, idle=idle)
    expect("idle money earns the index when asked", abs(sum(parked) - 0.001 * (len(idle) - 0)) < 0.01)

    # The signals fire where they should, from data at or before the signal day only.
    base = [100.0 * 1.0005 ** k for k in range(400)]
    path = base[:350] + [base[349] * 1.08] + [base[349] * 1.08 * 1.0005 ** k for k in range(1, 50)]
    vols = [1000] * 350 + [6000] + [1000] * 49
    sig_panel = Panel(dict(extra, NEWS=make(path, volume=vols)), {})
    context = {"universes": {"sp500": set(sig_panel.symbols)}}
    feats = features(sig_panel, context)
    fires = [i for i in range(len(sig_panel.days))
             if news_gap()(sig_panel, feats)("NEWS", i) is not None]
    expect("the news-gap signal fires on the jump day, on volume, and only then", fires == [350])
    expect("a breakout to a new 52-week high on volume fires too",
           breakout()(sig_panel, feats)("NEWS", 350) is not None
           and breakout()(sig_panel, feats)("NEWS", 349) is None)
    steep = [100.0 * 1.002 ** k for k in range(400)]   # still above its 200-day after -8%
    drop = steep[:350] + [steep[349] * 0.92] + [steep[349] * 0.92] * 49
    drop_panel = Panel(dict(extra, DROP=make(drop, volume=vols)), {})
    dfeats = features(drop_panel, {"universes": {"sp500": set(drop_panel.symbols)}})
    expect("a heavy-volume drop in an uptrend is a rebound signal",
           big_drop()(drop_panel, dfeats)("DROP", 350) is not None)
    everyone = lambda s, i: True
    fake = placebo_of(news_gap())(sig_panel, feats, everyone)
    fired = {(s, i) for s in sig_panel.symbols for i in range(len(sig_panel.days))
             if fake(s, i) is not None}
    expect("the placebo fires only on days the real rule fires", {i for _, i in fired} <= {350})
    expect("and is deterministic", fired == {(s, i) for s in sig_panel.symbols
                                             for i in range(len(sig_panel.days))
                                             if placebo_of(news_gap())(sig_panel, feats,
                                                                        everyone)(s, i)
                                             is not None})
    expect("rolling max looks only backwards",
           list(rolling_max_prior(array("d", [1, 3, 2, 5, 4]), 2))[2:] == [3.0, 3.0, 5.0])

    monthly = [("2020-%02d" % m, 0.01) for m in range(1, 13)] * 2
    expect("1% a month is 12.68% a year", abs(stats(monthly)["cagr"] - 0.126825) < 1e-4)
    expect("four judging windows", [p[0] for p in periods(monthly)]
           == ["full", "first half", "second half", "last 5y"])

    # T7: the 5-day skip reads the close five trading days before the month end.
    i = panel.month_ends[13]
    start_i = month_end_before(panel, i, 12)
    expect("12-1w reads the close five trading days before the month end",
           abs(skip_days_score(panel, "UP", i)
               - (panel.price("UP", i - 5) / panel.price("UP", start_i) - 1.0)) < 1e-12
           and abs(skip_days_score(panel, "UP", i)
                   - (panel.price("UP", i - 4) / panel.price("UP", start_i) - 1.0)) > 1e-9)
    expect("12-0 is momentum with no skip month",
           abs(momentum_score(panel, "UP", i, skip=0)
               - (panel.price("UP", i) / panel.price("UP", start_i) - 1.0)) < 1e-12)

    # T8: the earnings-like days are found block by block, and nothing after i is read.
    def spiked(days, moves, total=600):
        price, prices, vols = 100.0, [], []
        for d in range(total):
            if d in moves:
                price *= 1.0 + moves[d]
            prices.append(price)
            vols.append(6000 if d in days else 1500 if d == days_mid else 1000)
        return make(prices, volume=vols)
    base_calendar = panel.month_ends
    i = base_calendar[16]
    days_mid = i - 100                                   # a 1.5x day in block 3: not enough
    spikes = {i - 200: 0.05, i - 150: 0.03, i - 1: -0.04, i + 1: 0.20}
    ea_panel = Panel(dict(extra, SPIKE=spiked(set(spikes), spikes)), {})
    ea_context = {"universes": {"sp500": set(ea_panel.symbols)}}
    found = ea_blocks(ea_panel, ea_context, "SPIKE", i)
    expect("an earnings-like day is found in each block with a spike, and only there",
           [d for d, _ in found] == [i - 200, i - 150, i - 1])
    ar = features(ea_panel, ea_context)["SPIKE"]["ar"]
    expect("its EAR sums the excess return from two days before to two after, cut at i",
           abs(found[-1][1] - sum(ar[d] for d in range(i - 3, i + 1))) < 1e-12)
    expect("EAmom is the sum of the EARs, and the latest one here went against the stock",
           abs(eamom_score(ea_panel, ea_context, "SPIKE", i) - sum(e for _, e in found)) < 1e-12
           and found[-1][1] < 0)
    later_spikes = {i - 200: 0.05, i - 150: 0.03, i - 1: -0.04, i + 3: -0.30}
    other = Panel(dict(extra, SPIKE=spiked({i - 200, i - 150, i - 1, i + 3}, later_spikes)), {})
    other_context = {"universes": {"sp500": set(other.symbols)}}
    expect("what happens after the signal day changes nothing",
           ea_blocks(other, other_context, "SPIKE", i) == found)
    two = {i - 200: 0.05, i - 1: -0.04}
    two_panel = Panel(dict(extra, SPIKE=spiked(set(two), two)), {})
    expect("fewer than three earnings-like days give no EAmom score",
           eamom_score(two_panel, {"universes": {"sp500": set(two_panel.symbols)}},
                       "SPIKE", i) is None)
    agree = ea_agree_pick(ea_panel, ea_context, 1)
    expect("the agreement filter swaps out a pick whose latest EAR is negative",
           agree(["SPIKE", "F3", "F4"], i) != ["SPIKE"])

    # T9: intraday plus overnight is the whole move, and the data check bites.
    i = panel.month_ends[15]
    A, B = month_end_before(panel, i, 12), month_end_before(panel, i, 1)
    opens = [p * (1.003 if n % 2 else 0.997) for n, p in enumerate(rising)]
    split_panel = Panel(dict(extra, UP=make(rising, opens=opens)), {})
    split_context = {}
    whole = math.log(split_panel.price("UP", B) / split_panel.price("UP", A))
    day_part = intraday_score(split_panel, split_context, "UP", i)
    night_part = intraday_score(split_panel, split_context, "UP", i, part="overnight")
    expect("intraday plus overnight is the whole 12-1 log return",
           abs(day_part + night_part - whole) < 1e-9 and abs(day_part) > 1e-6
           and abs(night_part) > 1e-6
           and abs(whole - math.log(1.0 + momentum_score(split_panel, "UP", i))) < 1e-9)
    def backfilled(every):
        # 0.995, not 0.999: on a 0.1%-a-day riser 0.999 of today's close is within 1e-6
        # of yesterday's, which the check would rightly call back-filled.
        o = [rising[n - 1] if n and n % every == 0 else rising[n] * 0.995
             for n in range(len(rising))]
        bf = Panel(dict(extra, UP=make(rising, opens=o)), {})
        return intraday_score(bf, {}, "UP", i)
    expect("a quarter of opens back-filled fails the data check, a tenth passes it",
           backfilled(4) is None and backfilled(10) is not None)
    ends = set(panel.month_ends)
    gappy = Panel(dict(extra, UP=make(rising, drop={n for n in range(600)
                                                     if n % 14 == 7 and n not in ends})), {})
    expect("about 7% of days with no bar fails the data check",
           gappy.price("UP", A) and gappy.price("UP", B)
           and intraday_score(gappy, {}, "UP", i) is None)

    # T10: the sector-relative pick, on a hand-made month.
    i = panel.month_ends[15]
    prior = month_end_before(panel, i, 1)

    def moved(r):
        return make([100.0] * (prior + 1) + [100.0 * (1.0 + r)] * (600 - prior - 1))
    names, meta = {}, {}
    for k in range(12):
        names["T%d" % k], meta["T%d" % k] = moved(0.10 + 0.001 * k), {"sector": "Tech"}
        names["E%d" % k], meta["E%d" % k] = moved(-0.10 + 0.001 * k), {"sector": "Energy [3]"}
    sect = Panel(dict(extra, **names), meta)
    ranked = [x for k in range(12) for x in ("T%d" % k, "E%d" % k)]
    low = sector_reversal_pick(lowest=True)(sect, {}, 10)(ranked, i)
    high = sector_reversal_pick(lowest=False)(sect, {}, 10)(ranked, i)
    expect("the sector reversal holds each sector's laggards, not the market's",
           sorted(low) == sorted(["T%d" % k for k in range(5)] + ["E%d" % k for k in range(5)]))
    expect("the 21st-ranked name is never held", "T10" not in low and "T10" not in high)
    expect("the mirror holds each sector's leaders",
           sorted(high) == sorted(["T%d" % k for k in range(5, 10)]
                                  + ["E%d" % k for k in range(5, 10)]))
    for k in range(4):
        names["U%d" % k], meta["U%d" % k] = moved(-0.30), {"sector": "Utilities"}
    small = Panel(dict(extra, **names), meta)
    ranked = ["U%d" % k for k in range(4)] + ["T%d" % k for k in range(12)] + \
             ["E%d" % k for k in range(12)]
    picked = sector_reversal_pick(lowest=True)(small, {}, 10)(ranked, i)
    expect("a sector of four is measured against the median of all",
           all("U%d" % k in picked for k in range(4)))

    # T11: one tranche is the monthly book; three trade less; each re-picks in its months.
    mom = lambda s, i: momentum_score(panel, s, i)
    one_report, plain_report = {}, {}
    one = tranche_book(panel, always, mom, n=1, K=1, lag=1, report=one_report)
    plain = monthly_book(panel, always, mom, n=1, lag=1, report=plain_report)
    expect("one tranche is exactly the monthly book",
           [m for m, _ in one] == [m for m, _ in plain]
           and all(abs(a - b) < 1e-12 for (_, a), (_, b) in zip(one, plain))
           and abs(one_report["turnover"] - plain_report["turnover"]) < 1e-12)
    rot_mom = lambda s, i: momentum_score(rot, s, i)
    three_report = {}
    tranche_book(rot, always, rot_mom, n=5, K=3, report=three_report)
    expect("three staggered tranches trade less than a monthly re-pick",
           three_report["turnover"] < monthly_report["turnover"])
    own = True
    for j in range(3):
        rep = {}
        monthly_book(rot, always, rot_mom, n=5, every=3, offset=j, report=rep)
        for m, (turn, _) in sorted(rep["log"].items())[1:]:
            year, month = int(m[:4]), int(m[5:])
            if turn > 0 and (year * 12 + month - 2) % 3 != j:
                own = False
    expect("each tranche trades only after its own months' signals", own)

    # The judges, on toy monthly series.
    toy = ["%04d-%02d" % (2006 + k // 12, k % 12 + 1) for k in range(240)]
    flat_rows = lambda rate: [(m, rate) for m in toy]
    rows = {BASE[1]: flat_rows(0.01), BASE[5]: flat_rows(0.01),
            "mom10 12-0 L=1": flat_rows(0.011), "mom10 12-0 L=5": flat_rows(0.0105)}
    verdict, why = house_judge("mom10 12-0 L=1", "mom10 12-0 L=5")(rows, {}, toy)
    expect("the house rule passes a variant ahead everywhere, at both lags", verdict)
    rows["mom10 12-0 L=1"] = flat_rows(0.0106)      # about 0.8 a year ahead: not enough
    verdict, why = house_judge("mom10 12-0 L=1", "mom10 12-0 L=5")(rows, {}, toy)
    expect("but not one less than a full point a year ahead, saying so",
           not verdict and any(not ok and "needs >= +1.00" in text for ok, text in why))
    rows["mom10 12-0 L=1"] = [(m, 0.012 if k < 180 else 0.009) for k, m in enumerate(toy)]
    verdict, why = house_judge("mom10 12-0 L=1", "mom10 12-0 L=5")(rows, {}, toy)
    expect("and fails one that falls behind in the last five years, saying so",
           not verdict and any(not ok and "last 5y" in text for ok, text in why))
    def fall(depth):
        return [(m, -depth if k == 100 else 0.01) for k, m in enumerate(toy)]
    d_rows = {BASE[1]: fall(0.20), BASE[5]: fall(0.20)}
    d_rows["eamom10 L=1"], d_rows["eamom10 L=5"] = fall(0.14), fall(0.14)
    verdict, _ = ea_judge("eamom10 L=1", "eamom10 L=5")(d_rows, {}, toy)
    expect("path D passes a variant as good with a fall 6 points shallower", verdict)
    d_rows["eamom10 L=1"], d_rows["eamom10 L=5"] = fall(0.16), fall(0.16)
    verdict, _ = ea_judge("eamom10 L=1", "eamom10 L=5")(d_rows, {}, toy)
    expect("and fails one only 4 points shallower", not verdict)
    d_rows["eamom10 L=1"], d_rows["eamom10 L=5"] = fall(0.14), fall(0.16)
    verdict, why = ea_judge("eamom10 L=1", "eamom10 L=5")(d_rows, {}, toy)
    expect("path D must hold at L=5 too, not only at L=1",
           not verdict and any(not ok and text.startswith("L=5 worst fall") for ok, text in why))
    turns = lambda yearly: {"log": {m: (yearly / 12.0, ()) for m in toy}}
    t_rows = {BASE[1]: flat_rows(0.01), BASE[5]: flat_rows(0.01),
              "tranches K=3 L=1": flat_rows(0.01), "tranches K=3 L=5": flat_rows(0.01)}
    t_reports = {BASE[1]: turns(0.8), BASE[5]: turns(0.8),
                 "tranches K=3 L=1": turns(0.5), "tranches K=3 L=5": turns(0.5)}
    expect("the holding rule passes 0.5 a year of turnover against 0.8",
           tranche_judge(t_rows, t_reports, toy)[0])
    t_reports["tranches K=3 L=1"] = turns(0.6)
    expect("and fails 0.6 against 0.8", not tranche_judge(t_rows, t_reports, toy)[0])
    partial = {BASE[1]: flat_rows(0.01), BASE[5]: flat_rows(0.01),
               "mom10 12-0 L=1": flat_rows(0.011)}
    lines = judge_all(partial, {}, toy)
    expect("a judge with a row missing says NOT JUDGED and which",
           any("NOT JUDGED" in x and "mom10 12-0 L=5" in x for x in lines))
    produced = {label for f in TESTS.values() for label in getattr(f, "labels", [])}
    wanted = {label for j in JUDGES for label in j["labels"]} | set(BASE.values())
    expect("every row a judge reads is a test's label, short enough for the table",
           wanted <= produced and all(len(label) <= 26 for label in wanted))

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
    fetch = sub.add_parser("fetch")
    fetch.add_argument("indexes", nargs="*")
    fetch.set_defaults(func=cmd_fetch)
    run = sub.add_parser("run")
    run.add_argument("tests", nargs="*")
    run.set_defaults(func=cmd_run)
    sub.add_parser("selftest").set_defaults(func=cmd_selftest)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
