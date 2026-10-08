#!/usr/bin/env python3
"""
A year of daily closes per stock, fetched for the optional near-high rule, and the outside
prices the momentum book checks a pick against.

Trading 212 has no quotes or history endpoint, so this goes outside it, keyless and with the
standard library only, so that nothing has to be installed wherever it runs:

  primary   Yahoo Finance chart API. No cookie or crumb: yfinance itself notes the chart
            endpoint does not need one, and fetching the crumb is what gets rate-limited.
            Yahoo throttles data-centre addresses in waves, so a 429 is retried with back-off.
  fallback  Nasdaq's public quote API, US shares only. It wants browser-like headers.

Stooq is not used: since about April 2026 its CSV download needs a captcha-issued apikey,
and without one it answers 200 with an HTML page rather than an error.

What this is for, and what it is NOT for:

  - gates.near_high_reasons divides the latest close by the highest close of the last 252
    trading days. That is a ratio, so the series only has to be consistent with itself, and
    it is kept in the instrument's own quote currency (dollars for a US share).
  - It must never feed sizing unconverted. Sizing works in the account currency, and a
    dollar close read as an account-currency price would mis-size every order by the
    exchange rate (sizing.account_price prices a holding from the broker's own valuation
    instead). So this writes nothing to prices.csv and fetch_many's output goes only into
    state["price_history"]. The one place a close reaches sizing is
    momentum.outside_prices, for a pick not yet held: converted at a checked exchange rate,
    and accepted only when a second, independent source agrees with it.

Closes must be split-adjusted. A raw close across a split would make a stock look 90
percent below its high the day after a ten-for-one split, and the rule would wave a buy
through at the top. Yahoo's plain close already is; its adjclose also takes dividends out of
past prices, which pulls the old high down and makes the 5 percent rule stricter than it
is configured, so the plain close is preferred.

The newest bar can be today's, mid-session. That is kept on purpose: the question is how
far the price a buy would pay now sits below the high, and the live price answers it better
than yesterday's close.

Fails closed upstream: if every source fails, the stock has no history and the gate refuses
the buy with that reason. Nothing here raises into the trading run.

    python3 pricefeed.py show AAPL_US_EQ MSFT_US_EQ    distance from each 52-week high
    python3 pricefeed.py selftest
"""

import argparse
import http.client
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")
TIMEOUT = 20
BACKOFF = (2, 4)      # seconds between attempts after a 429, a 5xx or a network error
SPACING = 1.0         # seconds between tickers, to stay under any per-address limit

# Trading 212 tickers that differ from the exchange symbol. FB_US_EQ predates the rename.
RENAMED = {"FB": "META"}


def base_symbol(ticker):
    """AAPL_US_EQ -> AAPL, FB_US_EQ -> META. None for anything that is not a US share."""
    ticker = str(ticker or "").strip().upper()
    if not ticker.endswith("_US_EQ"):
        return None
    root = ticker[: -len("_US_EQ")]
    return RENAMED.get(root, root) or None


def parse_yahoo(payload):
    """[(date, close)] oldest first from a Yahoo chart response: the split-adjusted close."""
    chart = (payload or {}).get("chart") or {}
    if chart.get("error"):
        return []
    result = chart.get("result") or []
    if not result:
        return []
    block = result[0] or {}
    stamps = block.get("timestamp") or []
    indicators = block.get("indicators") or {}
    adjusted = ((indicators.get("adjclose") or [{}])[0] or {}).get("adjclose")
    raw = ((indicators.get("quote") or [{}])[0] or {}).get("close")
    closes = raw if raw and len(raw) == len(stamps) else adjusted
    if not closes or len(closes) != len(stamps):
        return []
    series = {}
    for stamp, close in zip(stamps, closes):
        if isinstance(close, (int, float)) and close > 0:
            when = datetime.fromtimestamp(int(stamp), tz=timezone.utc).date()
            series[when] = float(close)  # one close per day; the last write wins
    return sorted(series.items())


def parse_nasdaq(payload):
    """[(date, close)] oldest first from Nasdaq's historical quote JSON (newest first there)."""
    rows = ((((payload or {}).get("data") or {}).get("tradesTable") or {}).get("rows")) or []
    series = {}
    for row in rows:
        try:
            when = datetime.strptime(str(row.get("date", "")).strip(), "%m/%d/%Y").date()
            close = float(str(row.get("close", "")).replace("$", "").replace(",", "").strip())
        except (TypeError, ValueError):
            continue
        if close > 0:
            series[when] = close
    return sorted(series.items())


def http_get(url, headers=None):
    """Body as text. Raises urllib errors; the callers retry and fall back."""
    merged = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    merged.update(headers or {})
    request = urllib.request.Request(url, headers=merged)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read().decode("utf-8", errors="replace")


def _retrying(getter, url, headers, sleep):
    """getter(url, headers), retried after a 429, a 5xx or a network error. Raises the last."""
    last = None
    for wait in (0,) + BACKOFF:
        if wait:
            sleep(wait)
        try:
            return getter(url, headers)
        except urllib.error.HTTPError as error:
            last = error
            if error.code != 429 and error.code < 500:
                raise
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            # HTTPException covers a reply cut off mid-body (IncompleteRead) or malformed
            # (BadStatusLine), which are not OSErrors and used to escape every caller.
            last = error
    raise last


def fetch(ticker, getter=http_get, sleep=time.sleep, today=None):
    """(series, source) or ([], reason). Never raises."""
    symbol = base_symbol(ticker)
    if not symbol:
        return [], "no outside price source mapped for %s" % ticker
    problems = []
    for host in ("query2", "query1"):
        url = ("https://%s.finance.yahoo.com/v8/finance/chart/%s?range=2y&interval=1d"
               "&includeAdjustedClose=true" % (host, symbol))
        try:
            series = parse_yahoo(json.loads(_retrying(getter, url, None, sleep)))
            if series:
                return series, "yahoo"
            problems.append("yahoo %s returned no closes" % host)
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as error:
            problems.append("yahoo %s: %s" % (host, error))
    today = today or datetime.now(timezone.utc).date()
    url = ("https://api.nasdaq.com/api/quote/%s/historical?assetclass=stocks"
           "&fromdate=%s&todate=%s&limit=9999"
           % (symbol, (today - timedelta(days=400)).isoformat(), today.isoformat()))
    headers = {"Referer": "https://www.nasdaq.com/", "Origin": "https://www.nasdaq.com"}
    try:
        series = parse_nasdaq(json.loads(_retrying(getter, url, headers, sleep)))
        if series:
            return series, "nasdaq"
        problems.append("nasdaq returned no closes")
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as error:
        problems.append("nasdaq: %s" % error)
    return [], "; ".join(problems)


def nasdaq_close(symbol, getter=http_get, sleep=time.sleep, today=None):
    """(dollar close, 'nasdaq YYYY-MM-DD') from Nasdaq alone, or (None, reason).

    The momentum book's second, independent price for a pick SPUS does not hold: Yahoo
    supplies the price a buy is sized from, and this must agree with it. Never Yahoo here,
    or the two "sources" would be one."""
    today = today or datetime.now(timezone.utc).date()
    url = ("https://api.nasdaq.com/api/quote/%s/historical?assetclass=stocks"
           "&fromdate=%s&todate=%s&limit=30"
           % (symbol, (today - timedelta(days=14)).isoformat(), today.isoformat()))
    headers = {"Referer": "https://www.nasdaq.com/", "Origin": "https://www.nasdaq.com"}
    try:
        series = parse_nasdaq(json.loads(_retrying(getter, url, headers, sleep)))
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as error:
        return None, "nasdaq: %s" % error
    if not series:
        return None, "nasdaq returned no closes"
    when, close = series[-1]
    if (today - when).days > 7:
        return None, "nasdaq's newest close is from %s" % when.isoformat()
    return close, "nasdaq %s" % when.isoformat()


def fetch_many(tickers, getter=http_get, sleep=time.sleep):
    """({TICKER: series}, {TICKER: note}) for every ticker asked for, spaced out."""
    history, notes = {}, {}
    for number, ticker in enumerate(sorted({str(t).strip().upper() for t in tickers if t})):
        if number:
            sleep(SPACING)
        series, note = fetch(ticker, getter, sleep)
        if series:
            history[ticker] = series
        notes[ticker] = note
    return history, notes


def cmd_show(args):
    import signals
    history, notes = fetch_many(args.tickers)
    for ticker in sorted(notes):
        series = history.get(ticker) or []
        if not series:
            print("%-12s no data: %s" % (ticker, notes[ticker]))
            continue
        below = signals.drawdown(series, 252)
        high = max(close for _, close in series[-252:]) if len(series) >= 252 else None
        print("%-12s %d closes from %s, latest %s %.2f, 52-week high %s, %s"
              % (ticker, len(series), notes[ticker], series[-1][0].isoformat(), series[-1][1],
                 "%.2f" % high if high else "n/a",
                 "%.1f%% below it" % (below * 100) if below is not None else "too little history"))
    return 0


def cmd_nasdaq(args):
    """Whether Nasdaq answers from here: the momentum book's second price source."""
    for symbol in args.symbols:
        close, note = nasdaq_close(symbol.upper())
        print("%-8s %s" % (symbol.upper(), ("%.2f (%s)" % (close, note)) if close else note))
    return 0


def cmd_selftest(args):
    checks = []

    def expect(name, condition):
        checks.append((name, bool(condition)))

    no_wait = lambda seconds: None  # noqa: E731

    expect("a US share maps to its symbol", base_symbol("AAPL_US_EQ") == "AAPL")
    expect("FB maps to META", base_symbol("fb_us_eq") == "META")
    expect("a London fund has no outside source", base_symbol("MWIXl_EQ") is None)

    day = 86400
    yahoo = {"chart": {"result": [{
        "timestamp": [1759708800, 1759708800 + day, 1759708800 + 2 * day],
        "indicators": {"quote": [{"close": [100.0, None, 97.0]}],
                       "adjclose": [{"adjclose": [98.0, None, 97.0]}]}}], "error": None}}
    series = parse_yahoo(yahoo)
    expect("yahoo nulls are skipped", len(series) == 2)
    expect("yahoo uses the close, not the dividend-adjusted one", series[0][1] == 100.0)
    adj_only = {"chart": {"result": [{"timestamp": [1759708800],
                                      "indicators": {"quote": [{}],
                                                     "adjclose": [{"adjclose": [5.0]}]}}]}}
    expect("yahoo falls back to adjclose when there is no close",
           parse_yahoo(adj_only) == [(date(2025, 10, 6), 5.0)])
    expect("a yahoo error reply is no data",
           parse_yahoo({"chart": {"result": None, "error": {"code": "Not Found"}}}) == [])

    nasdaq = {"data": {"tradesTable": {"rows": [
        {"date": "10/02/2026", "close": "$1,234.50"}, {"date": "10/01/2026", "close": "$1,200.00"},
        {"date": "bad", "close": "$1"}]}}}
    expect("nasdaq rows parse oldest first, dollars and commas stripped",
           parse_nasdaq(nasdaq) == [(date(2026, 10, 1), 1200.0), (date(2026, 10, 2), 1234.5)])
    expect("a nasdaq reply with no data is no data", parse_nasdaq({"data": None}) == [])

    calls = []

    def throttled_then_ok(url, headers):
        calls.append(url)
        if len(calls) < 3:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
        return json.dumps(yahoo)

    series, note = fetch("AAPL_US_EQ", throttled_then_ok, no_wait)
    expect("a 429 is retried with back-off and then succeeds", note == "yahoo" and len(calls) == 3)

    def yahoo_down(url, headers):
        if "yahoo" in url:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
        expect("nasdaq gets browser-like referer headers",
               (headers or {}).get("Referer", "").startswith("https://www.nasdaq.com"))
        return json.dumps(nasdaq)

    series, note = fetch("AAPL_US_EQ", yahoo_down, no_wait, today=date(2026, 10, 6))
    expect("nasdaq is used when yahoo keeps refusing", note == "nasdaq" and len(series) == 2)

    def all_down(url, headers):
        raise urllib.error.URLError("blocked")

    series, note = fetch("AAPL_US_EQ", all_down, no_wait)
    expect("every source down is no data with every reason",
           series == [] and "yahoo" in note and "nasdaq" in note)

    def not_found(url, headers):
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    tries = []
    series, note = fetch("AAPL_US_EQ", lambda u, h: tries.append(u) or not_found(u, h), no_wait)
    expect("a 404 is not retried", len(tries) == 3)  # once per source: two yahoo hosts, nasdaq
    series, note = fetch("MWIXl_EQ", all_down, no_wait)
    expect("an unmapped ticker is no data, not an error", series == [] and "no outside" in note)

    fresh = {"data": {"tradesTable": {"rows": [{"date": "10/05/2026", "close": "$250.00"}]}}}
    close, note = nasdaq_close("AAPL", lambda u, h: json.dumps(fresh), no_wait,
                               today=date(2026, 10, 6))
    expect("nasdaq_close gives the newest close and says so",
           close == 250.0 and note == "nasdaq 2026-10-05")
    close, note = nasdaq_close("AAPL", lambda u, h: json.dumps(fresh), no_wait,
                               today=date(2026, 10, 30))
    expect("a stale nasdaq close is no price", close is None and "newest" in note)
    close, note = nasdaq_close("AAPL", all_down, no_wait)
    expect("nasdaq down is no price, with the reason", close is None and "nasdaq" in note)
    asked = []
    nasdaq_close("AAPL", lambda u, h: asked.append(u) or json.dumps(fresh), no_wait,
                 today=date(2026, 10, 6))
    expect("nasdaq_close never asks Yahoo", asked and all("yahoo" not in u for u in asked))

    def cut_off(url, headers):
        raise http.client.IncompleteRead(b"partial")
    close, note = nasdaq_close("AAPL", cut_off, no_wait, today=date(2026, 10, 6))
    expect("a reply cut off mid-body is no price, not a crash", close is None and note)
    series, note = fetch("AAPL_US_EQ", cut_off, no_wait)
    expect("and fetch survives it too", series == [] and note)

    width = max(len(name) for name, _ in checks)
    for name, passed in checks:
        print("%s  %s" % ("pass" if passed else "FAIL", name.ljust(width)))
    failed = [name for name, passed in checks if not passed]
    print("\n%d checks, %d failed" % (len(checks), len(failed)))
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show")
    show.add_argument("tickers", nargs="+")
    show.set_defaults(func=cmd_show)
    nasdaq = sub.add_parser("nasdaq")
    nasdaq.add_argument("symbols", nargs="+")
    nasdaq.set_defaults(func=cmd_nasdaq)
    sub.add_parser("selftest").set_defaults(func=cmd_selftest)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
