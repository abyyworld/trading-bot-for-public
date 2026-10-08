#!/usr/bin/env python3
"""
The broker connection: Trading 212, or a plug-in from brokers/. Read only.

Standard library only, so it runs with no pip install.

Every module speaks Trading 212's REST dialect through request() below. When config.json's
broker.adapter names anything other than "trading212", request() hands the call to
brokers/<adapter>.py, which answers in the same shapes: "paper" is a simulated account kept
in a local file, no real money and no credentials (brokers/paper.py), and brokers/template.py
shows how to map another broker. The contract is in brokers/__init__.py.

The Trading 212 contract comes from Trading 212's own published agent skill at
github.com/trading212-labs/agent-skills, not from guesswork. An earlier version of this file
guessed at all three of authentication, endpoint paths and the order payload, and got all
three wrong. That is why every earlier attempt returned 401: there is no single-token scheme.
Authentication is HTTP Basic over a key AND a secret.

    export T212_API_KEY=...        or the same two lines in a .env file in this folder,
    export T212_API_SECRET=...     which must never be committed
    python3 broker.py check        probe every read endpoint and report what works
    python3 broker.py summary      account summary, including cash
    python3 broker.py positions    open positions
    python3 broker.py orders       pending orders
    python3 broker.py instruments  search the instrument list for a ticker

Add --env live to talk to the real account. The default is demo, on purpose. Keys are bound
to one environment AND one account, so demo and live need different pairs, and so do Invest
and Stocks ISA. --env means nothing to a plug-in adapter such as paper.

This file reads. It does not place orders. Placement lives in trade.py, so that reading your
account and spending your money are never the same code path.
"""

import argparse
import base64
import importlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
# Credentials may live here instead of the environment. The environment wins.
ENV_FILE = HERE / ".env"

READ_ONLY = [
    "account_summary", "positions", "pending_orders",
    "history_orders", "history_dividends", "history_transactions",
]


def load_broker_config(path):
    """config.json's broker section, plus config_file: the path it was read from, so an
    adapter that needs another section (paper reads account.currency) reads the same file
    the run was started with."""
    with open(path, encoding="utf-8") as handle:
        config = json.load(handle)
    if "broker" not in config:
        raise SystemExit("config.json has no broker section")
    broker = config["broker"]
    broker.setdefault("config_file", str(Path(path).resolve()))
    return broker


# ---------------------------------------------------------------------- adapters

def adapter_name(broker):
    return str(broker.get("adapter") or "trading212").strip().lower()


def adapter(broker):
    """The plug-in module named by broker.adapter, or None for Trading 212 itself.

    A name that is not a plain module name is refused rather than imported, and a missing
    file stops the run with its path, rather than falling back to Trading 212: an account
    nobody chose is the last thing to trade on."""
    name = adapter_name(broker)
    if name == "trading212":
        return None
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
        raise SystemExit("broker.adapter %r is not a module name. Use paper or trading212, or "
                         "the name of a file in brokers/." % name)
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    module = "brokers.%s" % name
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as error:
        if error.name not in (module, "brokers"):
            raise
        raise SystemExit("broker.adapter is %r, but %s does not exist. Use paper or "
                         "trading212, or write that file from brokers/template.py."
                         % (name, HERE / "brokers" / ("%s.py" % name)))


def adapter_label(broker):
    """What a run prints as the account it is talking to."""
    plugin = adapter(broker)
    if plugin is not None:
        return str(getattr(plugin, "LABEL", "") or "%s adapter" % adapter_name(broker))
    environment = broker.get("environment", "demo")
    if environment == "live":
        return "trading212 live (real money)"
    return "trading212 %s" % environment


def key_warnings(name, raw):
    """The things that silently break a credential, in the order they happen to people."""
    notes = []
    if raw != raw.strip():
        notes.append("%s has leading or trailing whitespace, usually a stray newline" % name)
    stripped = raw.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "'\"":
        notes.append("%s is wrapped in quotes, so they became part of the value" % name)
    if any(c.isspace() for c in stripped):
        notes.append("%s contains a space, so it was truncated or joined with something" % name)
    if stripped.startswith("export ") or "=" in stripped:
        notes.append("%s looks like a whole shell line rather than just the value" % name)
    return notes


def read_env_file(path):
    """{NAME: value} from a .env file: NAME=value lines, an optional leading "export ",
    blank lines and # comments ignored, one pair of surrounding quotes stripped. Nothing
    after the value is treated as a comment, since a secret may contain #. A missing or
    unreadable file is empty. Values are never printed."""
    values = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if name:
            values[name] = value
    return values


def secret_value(name, env_file=None):
    """The raw value of one credential: the environment variable when it is set and not
    empty, else the same name in the .env file, else "". The environment wins, so a value
    exported for one run overrides the file without editing it."""
    raw = os.environ.get(name, "")
    if raw:
        return raw
    return read_env_file(ENV_FILE if env_file is None else env_file).get(name, "")


def credentials(broker, env_file=None):
    """Return (key, secret). Both are required: Basic auth over one value cannot work.

    An adapter that declares NEEDS_CREDENTIALS = False, such as paper, gets ("", "") and
    no error: it has no account to log in to."""
    plugin = adapter(broker)
    if plugin is not None and not getattr(plugin, "NEEDS_CREDENTIALS", True):
        return "", ""
    auth = broker.get("auth", {})
    key_var = auth.get("key_env_var", "T212_API_KEY")
    secret_var = auth.get("secret_env_var", "T212_API_SECRET")
    key_raw = secret_value(key_var, env_file)
    secret_raw = secret_value(secret_var, env_file)

    for note in key_warnings(key_var, key_raw) + key_warnings(secret_var, secret_raw):
        print("warning: %s" % note, file=sys.stderr)

    key, secret = key_raw.strip(), secret_raw.strip()
    missing = [n for n, v in ((key_var, key), (secret_var, secret)) if not v]
    if missing:
        raise SystemExit(
            "%s not set.\n"
            "Trading 212 issues a key AND a secret, and Basic auth needs both. Generate them\n"
            "in the app under Settings, API (Beta), then either export them:\n"
            "    export %s='...'\n"
            "    export %s='...'\n"
            "or write the same two lines, without export, in %s\n"
            "Never commit that file or either value. To try the bot with no account at all,\n"
            "set broker.adapter to paper in config.json."
            % (" and ".join(missing), key_var, secret_var,
               ENV_FILE if env_file is None else env_file))
    return key, secret


def auth_header(key, secret):
    """Authorization: Basic base64(KEY:SECRET), as the published skill documents."""
    return "Basic %s" % base64.b64encode(("%s:%s" % (key, secret)).encode()).decode()


def request(broker, path, key, secret, method="GET", payload=None, timeout=30):
    """Return (status, parsed body or raw text). Never raises on an HTTP error status.

    With a plug-in adapter configured the call is that module's request(), which answers in
    Trading 212's shapes; key and secret are not passed, as the adapter reads its own."""
    plugin = adapter(broker)
    if plugin is not None:
        return plugin.request(broker, path, method=method, payload=payload, timeout=timeout)
    base = broker["environments"][broker["environment"]]
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=body, method=method)
    req.add_header("Authorization", auth_header(key, secret))
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as error:
        text = error.read().decode("utf-8", "replace")
        status = error.code
    except urllib.error.URLError as error:
        return None, "network error: %s" % error.reason
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text.strip()


def history_items(body):
    """The order rows from a /equity/history/orders reply, list or {items: [...]}."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("items") or []
    return []


def created(row):
    order = row.get("order") if isinstance(row.get("order"), dict) else row
    return str(order.get("createdAt") or order.get("dateCreated") or order.get("dateModified")
               or "")


def order_history(fetch, path, cutoff, pages=4, spacing=10.0, sleep=time.sleep):
    """(status, rows, notes): order history back to `cutoff` (an ISO time), across pages.

    One page holds 20 orders by default, so the daily order cap and the per-instrument
    cooldown could only see the 20 most recent orders, and both failed open beyond them.
    This asks for 50 a page, follows nextPagePath until a page reaches back past cutoff,
    and spaces requests, as history allows about six a minute. If the larger page is
    refused it falls back to exactly the old single request, so it can never read less
    than before. A later page failing keeps what was read and says so.
    fetch(path) -> (status, body), path relative to the API base.
    """
    status, body = fetch("%s?limit=50" % path)
    notes = []
    if status != 200:
        notes.append("limit=50 refused (%s), read one default page" % status)
        status, body = fetch(path)
        if status != 200:
            return status, body, notes
    rows = list(history_items(body))
    nxt = body.get("nextPagePath") if isinstance(body, dict) else None
    for page in range(2, pages + 1):
        oldest = min((created(r) for r in rows if created(r)), default="")
        if not nxt or (oldest and oldest < cutoff):
            break
        sleep(spacing)
        relative = str(nxt)
        for prefix in ("/api/v0", "api/v0"):
            if relative.startswith(prefix):
                relative = relative[len(prefix):]
        status, body = fetch(relative)
        if status != 200:
            notes.append("history page %d unreadable (%s); older orders not counted"
                         % (page, status))
            break
        rows += history_items(body)
        nxt = body.get("nextPagePath") if isinstance(body, dict) else None
    else:
        if nxt:
            notes.append("stopped after %d pages; older orders not counted" % pages)
    return 200, rows, notes


def explain(status):
    return {
        200: "ok",
        201: "created",
        400: "the request body was rejected, check the payload shape",
        401: "auth rejected, check the key and secret pair and the environment",
        403: "authenticated but this scope was not granted when the key was made",
        404: "path is wrong, fix it in config.json",
        429: "rate limited, raise request_delay_seconds",
    }.get(status, "unexpected status")


def describe(status, body):
    """explain(status), plus the reply's own detail when it carries one, for printing.

    An adapter's refusal says what to do ("the paper account ... is in GBP but the account
    currency is now EUR; run brokers/paper.py reset"), and explain alone turned a 409 or a
    500 into "unexpected status" with the reason dropped. No status is a network error,
    whose body is already the message."""
    if not status:
        return str(body)
    text = explain(status)
    detail = body.get("detail") if isinstance(body, dict) else None
    if detail:
        text = "%s (%s): %s" % (text, status, str(detail)[:300])
    return text


def cmd_check(args, broker):
    key, secret = credentials(broker)
    plugin = adapter(broker)
    delay = broker.get("request_delay_seconds", 5)

    print("broker:      %s" % adapter_label(broker))
    if plugin is None:
        print("environment: %s  (%s)" % (broker["environment"],
                                         broker["environments"][broker["environment"]]))
        print("auth:        Basic base64(key:secret), key %d chars, secret %d chars\n"
              % (len(key), len(secret)))
    else:
        # An adapter with no rate limit, paper, says so; any other keeps the configured gap.
        delay = getattr(plugin, "REQUEST_DELAY_SECONDS", delay)
        print("adapter:     %s\n" % getattr(plugin, "__file__", adapter_name(broker)))

    print("probing endpoints, %ds apart to stay under the rate limit" % delay if delay
          else "probing endpoints")
    problems = 0
    for index, name in enumerate(READ_ONLY):
        path = broker["endpoints"][name]
        if index:
            time.sleep(delay)
        status, body = request(broker, path, key, secret)
        if status == 200:
            if isinstance(body, list):
                shape = "list of %d" % len(body)
            elif isinstance(body, dict):
                shape = "object with %s" % ", ".join(sorted(body)[:5])
            else:
                shape = "text"
            print("  %-22s %-30s 200  %s" % (name, path, shape))
        else:
            problems += 1
            detail = json.dumps(body) if isinstance(body, (dict, list)) else str(body)
            print("  %-22s %-30s %s  %s" % (name, path, status or "---",
                                            explain(status) if status else detail))
            if status and detail and detail not in ("''", '""', "{}"):
                print("  %-22s server said: %s" % ("", detail[:200]))

    print("\n%d of %d read endpoints worked." % (len(READ_ONLY) - problems, len(READ_ONLY)))
    if problems and plugin is not None:
        print("The adapter answered with errors; its messages are above. It is %s."
              % getattr(plugin, "__file__", adapter_name(broker)))
    elif problems == len(READ_ONLY):
        other = "live" if broker["environment"] == "demo" else "demo"
        print("Everything failed. If it is 401 throughout, the likeliest causes in order are:")
        print("  the key and secret belong to %s rather than %s" % (other, broker["environment"]))
        print("  the key was generated for the other account type, Invest versus Stocks ISA")
        print("  one of the two values is missing or truncated")
        print("Try:  python3 broker.py --env %s check" % other)
    elif problems:
        print("Fix the failing paths under broker.endpoints in config.json, then run again.")
        print("A 403 is not a wrong path, it means that scope was not ticked for the key.")
    return 1 if problems else 0


def cmd_get(args, broker):
    key, secret = credentials(broker)
    status, body = request(broker, broker["endpoints"][args.endpoint], key, secret)
    if status != 200:
        print("%s: %s" % (status, describe(status, body)), file=sys.stderr)
        return 1
    print(json.dumps(body, indent=2))
    return 0


def cmd_instruments(args, broker):
    """Find the exact Trading 212 ticker for a name. They are not plain symbols."""
    key, secret = credentials(broker)
    status, body = request(broker, broker["endpoints"]["instruments"], key, secret, timeout=120)
    if status != 200:
        print("%s: %s" % (status, describe(status, body)), file=sys.stderr)
        return 1
    if not isinstance(body, list):
        print(json.dumps(body, indent=2))
        return 0
    # Several names at once, separated by |, so one dump answers a whole shortlist: the
    # instruments endpoint allows one call every 50 seconds.
    needles = [n.strip() for n in (args.search or "").lower().split("|") if n.strip()]
    limit = 60 * max(1, len(needles))
    shown = 0
    print("%-12s %-36s %-14s %-4s %s" % ("TICKER", "NAME", "ISIN", "CCY", "TYPE"))
    # Currency matters. The same fund appears several times under one ISIN, one line per
    # currency and venue. Buying the wrong line means paying an FX spread on every single
    # purchase, forever, which is a real cost and an easy one to miss.
    for row in sorted(body, key=lambda r: (str(r.get("isin", "")), str(r.get("ticker", "")))):
        blob = " ".join(str(row.get(f, "")) for f in ("ticker", "name", "shortName", "isin"))
        if needles and not any(n in blob.lower() for n in needles):
            continue
        print("%-12s %-36s %-14s %-4s %s"
              % (row.get("ticker", ""), str(row.get("name", ""))[:36], row.get("isin", ""),
                 row.get("currencyCode", "?"), row.get("type", "")))
        shown += 1
        if shown >= limit:
            print("... more matches, narrow the search")
            break
    if not shown:
        print("nothing matched %r in %d instruments" % (args.search, len(body)))
    return 0


def cmd_exchanges(args, broker):
    """Summarise the working schedules: which ids exist, what they span, what covers now."""
    import hours
    from datetime import datetime, timezone

    key, secret = credentials(broker)
    status, body = request(broker, broker["endpoints"]["exchanges"], key, secret, timeout=60)
    if status != 200 or not isinstance(body, list):
        print("%s: %s" % (status, describe(status, body)), file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    print("now is %s\n" % now.isoformat(timespec="minutes"))
    schedules = hours.schedules_by_id(body)
    print("%-5s %-26s %-6s %-22s %-22s %s"
          % ("ID", "EXCHANGE", "WINDOWS", "EARLIEST", "LATEST", "COVERS NOW"))
    names = {}
    for exchange in body:
        for schedule in (exchange.get("workingSchedules") or []):
            names[schedule.get("id")] = exchange.get("name", "")
    for identifier in sorted(schedules):
        windows = sorted(schedules[identifier])
        open_now, _ = hours.is_open(identifier, schedules, now)
        print("%-5s %-26s %-6d %-22s %-22s %s"
              % (identifier, str(names.get(identifier, ""))[:26], len(windows),
                 windows[0][0].isoformat(timespec="minutes"),
                 windows[-1][1].isoformat(timespec="minutes"),
                 "YES" if open_now else "no"))
    future = [i for i in schedules if any(e > now for _, e in schedules[i])]
    print("\n%d schedules parsed, %d have any window ending in the future"
          % (len(schedules), len(future)))
    if not future:
        print("EVERY published window is historical. A live open/closed check cannot be made")
        print("from this payload, so the gate holds every order, which is the safe failure.")
    return 0


def cmd_selftest(args, broker):
    checks = []

    def expect(name, condition):
        checks.append((name, bool(condition)))

    def row(when, ticker="AAA_US_EQ"):
        return {"order": {"ticker": ticker, "createdAt": when}}

    pages = {
        "/h?limit=50": (200, {"items": [row("2026-10-07T10:00:00Z"), row("2026-10-06T10:00:00Z")],
                              "nextPagePath": "/api/v0/h?cursor=2&limit=50"}),
        "/h?cursor=2&limit=50": (200, {"items": [row("2026-10-03T10:00:00Z")],
                                       "nextPagePath": "/api/v0/h?cursor=3&limit=50"}),
        "/h?cursor=3&limit=50": (200, {"items": [row("2026-09-20T10:00:00Z")],
                                       "nextPagePath": "/api/v0/h?cursor=4&limit=50"}),
    }
    asked, waits = [], []

    def fetch(path):
        asked.append(path)
        return pages.get(path, (404, "no such page"))

    status, rows, notes = order_history(fetch, "/h", "2026-10-01T00:00:00Z",
                                        sleep=waits.append)
    expect("history follows pages back past the cooldown cutoff",
           status == 200 and len(rows) == 4)
    expect("and stops once a page reaches past it, not reading forever",
           "/h?cursor=4&limit=50" not in asked)
    expect("nextPagePath's /api/v0 prefix is stripped", "/h?cursor=2&limit=50" in asked)
    expect("requests are spaced for the rate limit", waits and all(w >= 10 for w in waits))

    old_api = {"/h": (200, [row("2026-10-07T10:00:00Z")])}
    status, rows, notes = order_history(lambda p: old_api.get(p, (400, "bad limit")), "/h",
                                        "2026-10-01T00:00:00Z", sleep=lambda x: None)
    expect("a refused limit falls back to the old single request",
           status == 200 and len(rows) == 1 and "refused" in notes[0])
    status, body, notes = order_history(lambda p: (503, "down"), "/h", "x",
                                        sleep=lambda x: None)
    expect("history down is a failure the caller can refuse to trade on", status == 503)

    told = describe(409, {"type": "paper-currency-changed",
                          "detail": "run brokers/paper.py reset to start again in EUR"})
    expect("describe keeps an adapter's own reason, with the status",
           told == "unexpected status (409): run brokers/paper.py reset to start again in EUR")
    expect("describe without a detail is explain, and a network error is its own message",
           describe(429, "rate limited") == explain(429) and describe(401, {"x": 1})
           == explain(401) and describe(None, "network error: timed out")
           == "network error: timed out")
    broken = dict(pages)
    broken["/h?cursor=2&limit=50"] = (500, "oops")
    status, rows, notes = order_history(lambda p: broken.get(p, (404, "")), "/h",
                                        "2026-10-01T00:00:00Z", sleep=lambda x: None)
    expect("a later page failing keeps the first and says so",
           status == 200 and len(rows) == 2 and "page 2 unreadable" in notes[0])

    # ------------------------------------------------------------- .env, credentials, adapters
    import contextlib
    import io
    import shutil
    import tempfile
    import types

    folder = Path(tempfile.mkdtemp(prefix="broker-selftest-"))
    names = ("BROKER_SELFTEST_KEY", "BROKER_SELFTEST_SECRET")
    saved_env = {name: os.environ.pop(name, None) for name in names}
    real_urlopen = urllib.request.urlopen
    try:
        dotenv = folder / ".env"
        dotenv.write_text("# broker keys, never committed\n"
                          "\n"
                          "PLAIN=plain-value\n"
                          "export EXPORTED = \"quoted=value#not-a-comment\"\n"
                          "SINGLE='single'\n"
                          "EMPTY=\n"
                          "not a pair\n", encoding="utf-8")
        values = read_env_file(dotenv)
        expect(".env: NAME=value lines, with comments and blank lines skipped",
               values.get("PLAIN") == "plain-value" and len(values) == 4)
        expect(".env: export and one pair of quotes stripped, = and # kept in the value",
               values.get("EXPORTED") == "quoted=value#not-a-comment"
               and values.get("SINGLE") == "single" and values.get("EMPTY") == "")
        expect(".env: a missing file reads as empty", read_env_file(folder / "none") == {})

        t212 = {"adapter": "trading212", "environment": "demo",
                "auth": {"key_env_var": names[0], "secret_env_var": names[1]}}
        creds = folder / "creds.env"
        creds.write_text("%s=file-key\n%s='file-secret'\n" % names, encoding="utf-8")
        expect("credentials come from .env when the environment has none",
               credentials(t212, creds) == ("file-key", "file-secret"))
        os.environ[names[0]] = "env-key"
        expect("and the environment wins over .env, name by name",
               credentials(t212, creds) == ("env-key", "file-secret"))
        del os.environ[names[0]]
        spaced = folder / "spaced.env"
        spaced.write_text('%s="file-key "\n%s=file-secret\n' % names, encoding="utf-8")
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            pair = credentials(t212, spaced)
        expect("a value from .env is checked like one from the environment, and never printed",
               pair == ("file-key", "file-secret") and names[0] in said.getvalue()
               and "file-key" not in said.getvalue() and "file-secret" not in said.getvalue())
        try:
            credentials(t212, folder / "empty" / ".env")
            stop = ""
        except SystemExit as error:
            stop = str(error)
        expect("neither set: the run stops and names both ways to set them, .env included",
               "%s and %s not set" % names in stop and "export %s=" % names[0] in stop
               and str(folder / "empty" / ".env") in stop)

        config_path = folder / "config.json"
        config_path.write_text(json.dumps({"broker": {"adapter": "paper"}}), encoding="utf-8")
        expect("load_broker_config records the file it read, for an adapter to read more of",
               load_broker_config(config_path)["config_file"] == str(config_path.resolve()))

        fake = types.ModuleType("brokers.selftestfake")
        seen = []

        def fake_request(broker_config, path, method="GET", payload=None, timeout=30):
            seen.append((path, method, payload, timeout))
            return 200, {"answered": "by the adapter"}
        fake.request, fake.NEEDS_CREDENTIALS, fake.LABEL = fake_request, False, "fake (test)"
        sys.modules["brokers.selftestfake"] = fake
        plugged = {"adapter": "selftestfake", "environment": "live"}
        try:
            reply = request(plugged, "/equity/orders/market", "k", "s", method="POST",
                            payload={"ticker": "AAA_US_EQ", "quantity": 1}, timeout=7)
            expect("a plug-in adapter answers in Trading 212's place, the call passed whole",
                   reply == (200, {"answered": "by the adapter"}) and seen == [
                       ("/equity/orders/market", "POST", {"ticker": "AAA_US_EQ", "quantity": 1},
                        7)])
            expect("an adapter declaring NEEDS_CREDENTIALS False needs no key or secret",
                   credentials(plugged) == ("", ""))
            expect("its LABEL is what a run prints, whatever environment says",
                   adapter_label(plugged) == "fake (test)")
        finally:
            sys.modules.pop("brokers.selftestfake", None)

        import brokers.paper as paper
        expect("adapter paper is brokers/paper.py, named in any case",
               adapter({"adapter": "Paper"}) is paper
               and adapter_label({"adapter": "paper"}) == paper.LABEL
               and credentials({"adapter": "paper"}) == ("", ""))
        expect("Trading 212 labels say which account, and real money when it is",
               adapter_label({"environment": "demo"}) == "trading212 demo"
               and adapter_label({"adapter": "trading212", "environment": "live"})
               == "trading212 live (real money)" and adapter({"adapter": ""}) is None)
        for name, needle in (("nosuchbroker", "nosuchbroker.py does not exist"),
                             ("../evil", "not a module name")):
            try:
                adapter({"adapter": name})
                stop = ""
            except SystemExit as error:
                stop = str(error)
            expect("adapter %r: stopped, not run on Trading 212 instead" % name, needle in stop)
        template = {"adapter": "template", "endpoints": {"positions": "/equity/positions"},
                    "auth": t212["auth"]}
        try:
            request(template, "/equity/positions", "", "")
            loud = False
        except NotImplementedError:
            loud = True
        try:
            credentials(template, folder / "empty" / ".env")
            asked = False
        except SystemExit:
            asked = True
        expect("the template raises NotImplementedError and demands credentials",
               loud and asked)

        sent = []

        class Reply:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"cash": {"availableToTrade": 1.0}}'

        def fake_urlopen(req, timeout=None):
            sent.append((req.full_url, req.get_method(), req.get_header("Authorization"),
                         timeout))
            return Reply()
        urllib.request.urlopen = fake_urlopen
        demo = {"environment": "demo", "environments": {"demo": "https://demo.example/api/v0"}}
        reply = request(demo, "/equity/account/summary", "k", "s")
        expect("with no adapter, the Trading 212 request is made exactly as before",
               reply == (200, {"cash": {"availableToTrade": 1.0}}) and sent == [
                   ("https://demo.example/api/v0/equity/account/summary", "GET",
                    auth_header("k", "s"), 30)])
        urllib.request.urlopen = real_urlopen

        paper_config = {"adapter": "paper", "endpoints": dict(paper.ENDPOINTS),
                        "paper": {"state_file": str(folder / "paper.json"), "currency": "GBP",
                                  "starting_cash": 500.0}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cmd_check(None, paper_config)
        expect("broker.py check runs on paper with no credentials and prints its label",
               code == 0 and paper.LABEL in out.getvalue()
               and "6 of 6 read endpoints worked" in out.getvalue())
    finally:
        urllib.request.urlopen = real_urlopen
        for name, value in saved_env.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value
        shutil.rmtree(str(folder), ignore_errors=True)

    width = max(len(n) for n, _ in checks)
    for name, passed in checks:
        print("%s  %s" % ("pass" if passed else "FAIL", name.ljust(width)))
    failed = [n for n, p in checks if not p]
    print("\n%d checks, %d failed" % (len(checks), len(failed)))
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--env", choices=("demo", "live"),
                        help="override the environment in config.json")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("check", help="probe every read endpoint").set_defaults(func=cmd_check)
    for name, endpoint in (("summary", "account_summary"), ("positions", "positions"),
                           ("orders", "pending_orders"), ("history", "history_orders"),
                           ):
        entry = sub.add_parser(name, help="print %s" % endpoint.replace("_", " "))
        entry.set_defaults(func=cmd_get, endpoint=endpoint)

    sub.add_parser("exchanges", help="summarise working schedules").set_defaults(
        func=cmd_exchanges)
    sub.add_parser("selftest", help="check the pager, .env and adapters, offline").set_defaults(
        func=cmd_selftest)

    lookup = sub.add_parser("instruments", help="find the exact ticker for a name")
    lookup.add_argument("search", nargs="?", default="")
    lookup.set_defaults(func=cmd_instruments)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2

    # Offline and independent of config.json, so it runs on a copy with no config at all.
    if args.func is cmd_selftest:
        return cmd_selftest(args, None)
    broker = load_broker_config(args.config)
    if args.env:
        broker["environment"] = args.env
    if adapter(broker) is None and broker.get("environment") == "live":
        print("### LIVE ACCOUNT, real money ###\n", file=sys.stderr)
    return args.func(args, broker)


if __name__ == "__main__":
    sys.exit(main())
