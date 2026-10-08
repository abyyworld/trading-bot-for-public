# Contributing

Bug reports, fixes, broker adapters and research tests are all welcome. This code can place
real orders with real money, so the bar for changes to what it counts, how it sizes an order
and what it lets through is high, and the rules below exist for that reason.

For questions and ideas, open an issue. Never paste keys, balances, holdings or order details
into one.

## Run the tests

```
python3 run.py selftest               # every module, each in its own process
python3 run.py selftest gates trade   # only these modules
python3 gates.py selftest             # one module, with every check listed
python3 run.py doctor                 # Python, time zones, config and broker settings
```

On Windows, type `py` (or `python`) instead of `python3`, after
`py -m pip install -r requirements.txt` for the time zone data.

Every selftest is offline. The broker, the web and the clock are replaced by fakes, so the
tests need no internet, no credentials and no broker account, and touch none of your files.
Each one ends with a line `N checks, M failed`, which `run.py selftest` reads. The full run
takes about a minute, most of it `research.py`.

CI (`.github/workflows/ci.yml`) runs every selftest and the doctor with Python 3.9 and 3.13,
on every push and pull request. In a public repository, and on any run started by hand
(Actions, CI, Run workflow), it runs on Linux, Windows and macOS; on a push or pull request in
a private copy, on Linux only, because GitHub bills a private repository's Windows minutes at
twice the Linux rate and its macOS minutes at ten times, from the allowance the scheduled
trading runs need. A pull request should pass both locally first.

## House rules

1. **Fail closed.** When something cannot be read or is not known, refuse rather than guess.
   No price means no order. Order history that cannot be read means no execution, because
   assuming zero orders today is how a daily cap silently becomes no cap. An empty list where
   data was expected is an error, not "nothing held".
2. **A selftest case with any change to counting, sizing or gating**, in that module's
   selftest and in the same commit. That covers the gates, the sizing and the ledger, the
   daily order and spend counts, the cooldown, market hours and the momentum proposals. Keep
   tests offline: pass in a fake getter or broker rather than calling the network.
3. **No em dashes or en dashes**, in code, comments or docs. Use commas, colons or
   parentheses. To find any, from the project folder:

   ```
   python3 -c "import pathlib; [print(p, n) for p in pathlib.Path('.').rglob('*') if p.suffix in ('.py', '.md', '.json', '.yml', '.txt') and '.git' not in p.parts for n, line in enumerate(p.read_text(encoding='utf-8').splitlines(), 1) if '\u2013' in line or '\u2014' in line]"
   ```

4. **Data in `config.json`, procedure in code and docs.** One place per fact. A threshold, a
   list or a ticker belongs in `config.json`, with an `about` or `note` beside it saying why.
   Keys named `about` or `note`, or ending in `_note`, are documentation only.
5. **Standard library only.** No third-party packages: `requirements.txt` holds only
   `tzdata`, for Windows. The bot then runs anywhere with nothing to install, and the code
   that can place an order stays small enough to read.
6. **Python 3.9 compatible.** No `match` statements, no `X | Y` type unions, no library
   functions added in 3.10 or later. CI tests 3.9 and 3.13.
7. **Selling is never blocked by the allowlist or the value caps**, so a holding can always
   be exited. The caps bind what can be spent.
8. **Never hand-edit the ledger or the allowlist to make something pass.**
9. **Comments say why.** Plain English, for a reader who has not seen the bug that the code
   prevents.

## Adding a broker

Every module speaks Trading 212's REST dialect through `broker.request`. When
`broker.adapter` in `config.json` names something else, that call goes to
`brokers/<name>.py`, which answers in Trading 212's shapes. So an adapter is translation only:

1. Copy `brokers/template.py` to `brokers/<name>.py` and set `broker.adapter` to `"<name>"`.
2. Implement `request(broker_config, path, method="GET", payload=None, timeout=30)` for each
   endpoint, returning `(status, body)` in the shapes listed in `brokers/__init__.py`. Set
   `NEEDS_CREDENTIALS` and `LABEL`, and read your keys with `broker.secret_value(NAME)`.
3. Mind the parts the gates rely on: money in the account currency; order history that
   cannot be read returned as an error status, never an empty list; a timed-out order
   returned as status `None`, since it may have been placed; quantities at most 4 decimal
   places.
4. Run `python3 broker.py check` against the other broker's practice account until every
   endpoint works, then `python3 run.py once` before any `--execute`.
5. Give it an offline selftest with a fake of the other broker, and add it to `MODULES` in
   `run.py` so `run.py selftest` and CI run it.

`brokers/paper.py` is a complete, tested example. The full guide is
[docs/BROKERS.md](docs/BROKERS.md).

## Proposing a research test

Research here is pre-registered: a test and its pass rule are fixed before any data is run.
That is what stops a backtest turning into a search for the best-looking variant.

1. **Write the rule down first.** Open an issue with the idea, where it comes from, and the
   exact pass rule, before running anything. The usual rule is the house replacement rule
   (`house_rule` in `research.py`). Against plain top-10 12-1 momentum, filled the next
   trading day at 0.20% a side over the same months, the candidate must beat it by at least
   1.0 point a year over the full sample, be ahead in the first half, the second half and the
   last five years, have a worst fall no more than 5 points deeper, and keep the sign of its
   edge when filled 5 trading days late.
2. **Implement it** in `research.py`: register the test in `TESTS`, record its rule and the
   date in a comment beside it, add a judge with `judged(...)` so the verdict prints PASS or
   FAIL mechanically, and add a selftest case on toy data. Commit that before the first run.
3. **Run it.** On GitHub, push a branch other than `main` that changes
   `research/request.txt`, for example to `research.py run mom10_L1 mom10_L5 yourtest_L1`.
   `.github/workflows/research.yml` runs each line with no secrets and read-only permissions,
   and puts the output on the run summary and in a `research-output` artifact. On a computer
   with internet access, `python3 research.py fetch sp500` and then
   `python3 research.py run yourtest_L1` do the same.
4. **Report it whatever it says**: the full sample, each half and the last five years, every
   row over the same months. A fail is a result. Do not re-tune a failed idea on the same
   data; a pass found that way is noise.

Quote `research.py` only. `backtest.py` is older and its levels are inflated.

## Keep your own copy private if it commits a ledger

The bot writes its order ledgers, one per account (`orders_placed.json` for Trading 212 live,
`orders_placed.demo.json` for practice, `orders_placed.paper.json` for paper), along with
`momentum_targets.json` and `paper_account.json`, into the project folder. They are
deliberately not git-ignored: the GitHub Actions trading workflow commits them back, because a
runner's disk is thrown away after each run and the ledger is what stops an order being placed
twice. They hold cash, holdings and every order, and so do the run summaries and issues that
workflow writes.

- Run `.github/workflows/trading.yml` only in a **private** repository. A fork of a public
  repository cannot be made private, so for trading, create a new private repository and
  push a copy to it. The workflow refuses to run on any account but the paper one unless
  the repository is private.
- In a public fork used for contributing, do not run the trading workflow. Its schedule is
  skipped until the `BOT_ENABLED` variable is `true`, but a manual run on the paper account
  would still commit its ledger and account state where anyone can read them.
- Before every commit, check `git status` and leave out the `orders_placed*.json` ledgers,
  `momentum_targets.json`, `paper_account.json`, `HALT`, `.env` and anything else from your
  own runs.

## Pull requests

- One change per pull request, saying what it changes and why.
- `python3 run.py selftest` and `python3 run.py doctor` pass, and CI is green.
- Update the docs where behaviour changes: `README.md`, `docs/`, and the notes in
  `config.json`.

By contributing you agree that your contribution is licensed under the MIT licence in
[LICENSE](LICENSE).
