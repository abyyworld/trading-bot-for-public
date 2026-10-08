# halal-momentum-bot

A rule-driven investing bot that keeps part of your account in an Islamic index fund and the
rest in the ten strongest stocks of a Shariah-screened ("not haram") S&P 500, re-picked once a
month. It runs on any computer with Python 3.9 or newer, starts in a simulated paper account,
and can trade a Trading 212 account through its API, every order passing the same hard limits.

> [!IMPORTANT]
> **What to expect, honestly**
>
> - After the usual decay of published effects, expect the momentum part to beat the core
>   fund by about 2.5 points a year, anywhere from 0 to 5. On the default 20/80 split that is
>   roughly **2 points a year on the whole account**, and it may be nothing.
> - Expect a fall of **45 to 55%** in a 2008-style crash.
> - Momentum can trail for a decade. From 2007 to 2017 it did worse than simply owning the
>   same stocks in equal amounts.
> - Every backtest here is flattered (companies that were delisted are missing from the
>   data). Past results do not guarantee future ones.
> - This is not financial advice, and the screen is not a fatwa.

## How it works

The account is split in two:

- **Core**: `core_weight` of the account (default 20%) in an Islamic index fund, Invesco MSCI
  ACWI Islamic. The default line is `MWIXl_EQ`, priced in pence on the London Stock Exchange;
  `MWIMl_EQ` is the same fund in dollars.
- **Momentum**: the rest, split evenly over the 10 stocks with the highest 12-1 momentum (the
  return over the last 12 months, skipping the latest month), chosen from the S&P 500 minus
  businesses whose core is haram. At the defaults each pick is about 8% of the account.

On the first run of each month it ranks the screened list on the last complete month and
saves that month's targets. For the 7 days after the build it sells every holding that is
neither the core fund nor one of this month's picks, including shares you bought yourself,
and brings each pick and the core to its share, within a 10% band so that small drift causes
no trades. The rest of the month a run checks and does nothing.

> [!WARNING]
> **The bot treats the whole account as its own.** Sells are never capped by value, so in
> each monthly window anything else in the account is sold. Use an account that holds nothing
> else, or set `strategy.momentum.mode` to `"shadow"` for a dry run: the book is then worked
> out and printed on every run, and nothing it proposes is sent.

```
 broker.py  ---->  momentum.py  ---->  gates.py  ---->  sizing.py  ---->  trade.py
 reads cash,       proposes this       drops what       money to          places what is
 holdings and      month's sells       breaks any       shares, rounded   left, only with
 today's orders    and buys            rule             down; ledger      --execute; after
 (read only)                                            written first     sells, buys again
```

Everything fails closed. No price means no order; order history, cash or holdings that cannot
be read mean nothing is placed; an index list that fails its checks is not used.

## Quick start: paper trading on your own computer

The default broker is `paper`: a simulated account kept in `paper_account.json`, starting with
10,000 of play money in your account currency (GBP unless you change `account.currency`). It
needs no broker account and no keys, only Python 3.9 or newer (check with `python3 --version`)
and the internet: prices come from Yahoo, the S&P 500 list from Wikipedia, and a cross-check
from the holdings file of SPUS, an S&P 500 Shariah fund.

Get the code with git, as below, or download the ZIP from the GitHub page and unpack it. Then:

```
git clone https://github.com/abyyworld/halal-momentum-bot.git
cd halal-momentum-bot
python3 run.py doctor                          # should end: nothing blocks a run
python3 brokers/paper.py buy MWIXl_EQ 2000     # the one-off core fund buy
python3 run.py once                            # propose only, place nothing
python3 run.py once --execute                  # place what passes every gate
python3 brokers/paper.py show                  # cash, holdings and latest orders
python3 run.py loop --execute                  # keep running at the scheduled times
```

- **Why buy the core fund by hand?** Trading 212 has no quotes endpoint, so the bot can only
  price what you already hold. On the real broker you buy the core fund once in the app; this
  is that step. 2,000 is 20% of the paper account.
- **The first run of a month** builds that month's picks: it downloads a few hundred price
  histories and takes a few minutes.
- **Orders wait for their market.** The core fund trades in London hours, the picks in New
  York hours (09:30 to 16:00 New York time). Outside them a run prints `HELD` and a later run
  places the order.
- **`loop`** runs at each time in `schedule.utc_times` and carries on after a failed run;
  Ctrl+C stops it. Without `--execute` it only proposes; `--now` also runs once at the start.
- `python3 run.py selftest` runs every module's offline tests in about a minute and should
  end with `0 failed`. `python3 brokers/paper.py reset` starts the paper account again.

**On Windows**, type `py` (or `python`) where this page says `python3`, and first install the
time zone data that Windows Python lacks; nothing else needs installing anywhere, as the bot is
the standard library only. Forward slashes in paths work on Windows too.

```
py -m pip install -r requirements.txt
py run.py doctor
```

## Moving to Trading 212

Use Trading 212's practice (demo) account first. It behaves like the real one with play money.
The full walk-through, with what the API does and does not do, is in
[docs/TRADING212.md](docs/TRADING212.md).

**The bot treats the whole account as its own.** In each monthly window it sells every holding
that is neither the core fund nor a pick, shares you bought yourself included, and sells are
never capped by value. Use an account that holds nothing else, or first set
`strategy.momentum.mode` to `"shadow"`: the book is then worked out and printed, and nothing is
sent.

1. **Make a practice key.** In Trading 212, switch to the practice account, then generate an
   API key under Settings, API (Beta). You get two values, a key and a secret: save both.
   A pair works for one environment (practice or live) and one account type (Invest or Stocks
   ISA) only. CFD accounts are not supported.
2. **Store it.** Copy `.env.example` to `.env` in the project folder and paste both values
   after the `=`, with no quotes or spaces. `.env` is ignored by git. `T212_API_KEY` and
   `T212_API_SECRET` set in the environment work too, and win over the file.
3. **Point `config.json` at it.** Set `broker.adapter` to `"trading212"`. Leave
   `execution.environment` and `broker.environment` at `"demo"`: runs use the first,
   `broker.py`'s own commands the second, so keep them equal. Nothing from the paper account
   carries over, and no file needs moving: each account has its own order ledger
   (`orders_placed.paper.json` for paper, `orders_placed.demo.json` for practice,
   `orders_placed.json` for live), so paper orders never count against Trading 212's daily
   cap, and the first run on a new adapter rebuilds the month's picks
   (`momentum_targets.json`) from that broker's own tickers. `paper_account.json` is not
   read again until you switch back.
4. **Check the connection and find the fund line in your currency:**

   ```
   python3 run.py doctor
   python3 broker.py check                      # should say 6 of 6 read endpoints worked
   python3 broker.py instruments IE000LFC57H7   # every line of the core fund, by ISIN
   ```

   A 401 throughout usually means the pair belongs to the other environment or account type;
   a 403 means a permission was not ticked when the key was made. Use the fund line in your
   account's currency (`MWIXl_EQ` for GBP, `MWIMl_EQ` for USD): any other pays Trading 212's
   0.15% FX fee on every buy and every sell. Tickers are Trading 212's own and case matters:
   London lines end in a lower-case `l`. Put the line in `strategy.momentum.core_ticker` and
   make sure it is in `compliance.allowlist`.
5. **Buy the core fund once by hand** in the app, about `core_weight` of the account, by
   number of shares rather than by value: the API sends at most 4 decimal places, and a buy
   by value can leave 8. From then on every run prices the fund from your holding. (A row in
   `prices.csv`, `date,ticker,close` in the account currency, is the alternative.)
6. **Run it**: `python3 run.py once`, read what it proposes, then
   `python3 run.py once --execute`. Let it run on practice for a while and read its output.

**Going live** is a separate, deliberate step: a new key pair made on the live account (a
practice pair does not work there), `execution.environment` and `broker.environment` set to
`"live"`, the caps lowered for the first real-money runs, Trading 212's API terms read, an
account holding nothing the bot should not sell (or a first month in `"shadow"` mode), and
`--execute` on the command line. `run.py doctor` then prints
`account: Trading 212 LIVE, REAL MONEY`.

## Running it unattended

Three ways, each step by step in [docs/RUNNING.md](docs/RUNNING.md). Run it in one place
only: each copy keeps its own ledger, so two copies on one account would not see each other's
orders.

- **Your own computer**: `python3 run.py loop --execute` in a terminal that stays open, or
  `python3 run.py once --execute` started at each run time by cron, launchd, systemd or Task
  Scheduler, on a machine that is awake at those times.
- **Docker**: `docker compose up -d --build` keeps the loop running and restarts it after a
  crash or a reboot. The project folder is mounted, so the ledger and the paper account stay
  in it. As shipped the compose file only proposes; add `--execute` to its command.
- **GitHub Actions**: `.github/workflows/trading.yml` runs the bot on GitHub's machines with a
  control panel in the Actions tab. **Use it only in a private repository**: every run writes
  your cash, holdings and orders to the run summary and commits the ledger and account state
  back. A fork of a public repository cannot be made private, so create a new private
  repository and push a copy. The workflow checks: on any account but the paper one, a run
  in a repository that is not private stops before it reads anything. The schedule stays off
  until the repository variable `BOT_ENABLED` is `true`, and a manual `execute` that could
  reach real money needs `PLACE ORDERS` typed into its confirm box.

The default schedule is 13 runs a weekday in UTC: 08:17 to 12:17 inside London's session for
the core fund, and 13:47 to 20:47 across New York's. Extra runs are safe: the cooldown, the
ledger and the 7-day window stop repeats. GitHub drops and delays scheduled runs, sometimes by
hours; the research found that fills a few days late cost nothing measurable.

## The safety model

A strategy only proposes. Every proposal, from any source, passes `gates.py`, which runs no
model, touches no network and has its own tests. A proposal failing any one check is dropped,
with the reason printed. Sells are never value-capped or allowlist-gated, so any holding can
always be exited. `python3 gates.py explain` prints the limits in force.

| Gate | Default | What it stops |
|---|---|---|
| Allowlist | the core fund lines | Buying anything not on `compliance.allowlist`, except this month's screened momentum picks, and those only by the momentum book in their own month. |
| Per-order cap | 2,500 | A single buy bigger than `risk.max_order_value`. |
| Daily spend | 7,000 | Buying more in a day than `risk.max_daily_spend`, across all of the day's runs. |
| Orders per day | 60 | More orders in a day than `risk.max_orders_per_day`. |
| Position cap | 1,500 a stock | Any single stock over `risk.max_position_value`; the core fund has its own cap. |
| Cash buffer and broker hold | 20, 5% | Spending into the last 20 of cash. A buy counts as its value / 0.95, because Trading 212 takes a market buy only up to about 95% of free funds. |
| Cooldown | 120 hours | A second order in the same instrument, counted from the broker's order history. |
| Market hours | on | Any order into a closed market. London hours come from Trading 212's published schedule; New York's are in config, with NYSE holidays listed to 2027-12-31 (extend them yearly). |
| Killswitch | `HALT` | Every order, while a file named `HALT` is in the project folder. |

Then `sizing.py` turns money into shares: at most 4 decimal places, rounded down, with the cap
checked again on what will actually be sent. It writes the order to the account's ledger
(`orders_placed.json` for Trading 212 live, `orders_placed.demo.json` for practice,
`orders_placed.paper.json` for paper) **before** sending it: Trading 212's API is not
idempotent, and a request that timed out may still have placed the order, so the ledger stops
a resend. A ledger that exists but cannot be read stops every order until it is restored,
rather than being read as empty.

An order goes out only when all four are true: `--execute` on the command line,
`execution.execute` true in `config.json`, no `HALT` file, and the order not already in
today's ledger. After a run's sells, a second pass reads the account again and buys with the
cash they freed, under the same gates; it never sells, and any incomplete read cancels it.

**To stop everything**, create an empty file named `HALT` in the project folder (on GitHub,
commit one). Delete it to resume.

**Real money takes three deliberate changes**: `broker.adapter` set to `"trading212"`,
`execution.environment` set to `"live"`, and `--execute` on the command line. As shipped, the
adapter is `paper` and nothing real can be sent.

## The evidence

`research.py` replays the strategy on daily Yahoo prices for the S&P 500 minus haram
businesses. A stock counts only from the day it joined the index (Wikipedia's "Date added"),
every trade costs 0.20% a side (Trading 212's 0.15% FX fee plus a spread allowance), orders
fill the next trading day, and every row is scored over the same 227 months, November 2007 to
September 2026. Each test and its pass rule were written down before the data was run, and
judged on the full sample, each half and the last five years. Rerun it with
`.github/workflows/research.yml`, or on any computer with internet access using
`python3 research.py fetch sp500` and then `python3 research.py run`.

| Rule | A year | Worst fall |
|---|---:|---:|
| Top 10 by 12-1 momentum (the momentum part alone) | 16.2% | -53.7% |
| The same screened list, owned in equal amounts | 13.5% | -42.6% |
| 20% core, 80% momentum (the default) | 16.4% | -48.9% |
| 40% core, 60% momentum | 15.9% | -46.8% |
| 60% core, 40% momentum | 15.2% | -45.1% |
| Top 10 momentum without the join-date fix (biased, for scale) | 36.4% | |

- **Momentum is not steady.** It trailed equal weight from 2007 to 2017 (8.9% against 12.4% a
  year) and led from 2017 (23.9% against 14.6%).
- **The not-haram screen did as well as the S&P 500 Shariah index's own list** (SPUS): 16.7%
  against 16.9% a year, 35.7% against 33.8% over the last five years, and a worst fall of
  -51.1% against -52.2%.
- **Late fills cost nothing measurable.** Filled 1, 3 or 5 trading days late, top-10 momentum
  made 16.2%, 16.6% and 17.5% a year.
- **Tested and rejected under pre-registered rules**: top 5 (16.9%, but a worst fall of
  -61.7%), residual momentum, frog-in-the-pan, volatility-adjusted momentum, months 7 to 12,
  52-week-high momentum, a cap of 3 per sector, volatility-managed exposure, hold bands of 15
  to 35, quarterly re-picks, daily ranking with rare swaps, a daily bad-news exit, 12-0
  momentum, 12-1 skipping 5 days instead of a month, earnings-announcement momentum,
  intraday-only momentum, sector laggards and staggered quarterly tranches. A 10-month trend
  filter cut the worst fall to about -28% for about 1 point a year: insurance, not an upgrade.
- **Short-term ideas failed**: buying heavy-volume dips (-0.30% a trade), RSI(2) dips (0.00% a
  trade after costs), 52-week breakouts and a news-gap rule. That matches the literature:
  large-cap post-earnings drift has largely gone since about 2006 (Martineau 2022), and
  positive news shocks in S&P 500 stocks tend to reverse (Frank and Sanati 2018).

**Why these numbers are too high.** Holding today's index members before the index added them
inflates a momentum backtest by about 20 points a year (the 36.4% row). The join-date fix
removes that, but delisted and acquired companies are still missing, so even 16% is too high.
In the splits the core is an equal-weight stand-in built from survivors, not the real fund, so
every split is flattered too. Published effects also tend to shrink once known. Hence the
expectation at the top of this page.

The full results, the pass rules and how to reproduce them are in
[docs/STRATEGY.md](docs/STRATEGY.md).

## The halal screen, in brief

The standard is "not haram": a company is out only if its core business is haram, and there
are no ratio screens, so a stock no screener has labelled halal is fine if it is not haram.
Excluded: conventional banks, insurers (managed health care included), alcohol, tobacco,
gambling, adult entertainment, pork, weapons and defence primes, interest-based lending,
derivatives exchanges (CME, Cboe, ICE) and Coinbase. The exact lists are data in
`config.json` under `compliance.screen`.

The screen fails closed. A company is kept only if its GICS sub-industry is on a reviewed
list, so a renamed label or a blank cell is dropped and reported. The whole list is refused,
falling back to SPUS's holdings with the named exclusions applied, unless known "canary"
companies (JPM, MO, LMT, UNH and others) are all excluded. Picks are re-screened on every run,
so an exclusion added mid-month revokes a pick at once. Income purification is not handled.

The closest calls, reviewed and worth revisiting yearly: Leidos is excluded (defence about 45
to 50%); Boeing, TransDigm, Textron and Honeywell (defence a third to 40%), Axon (TASERs about
a third), Palantir (defence perhaps 35 to 45%), Moody's and S&P Global (ratings serve the bond
market), the cruise lines (casinos and bars inside the third of revenue that is not tickets)
and Tyson (pork about a tenth) are allowed.

**This is not a fatwa or a certification.** It is a filter that encodes one reading. Check it
with a scholar you trust and edit the lists to match your own judgement. The full screen,
with the reasoning for each decision, is in [docs/SCREEN.md](docs/SCREEN.md).

## What it does not do

- **Day trading.** Trading 212 has no quotes endpoint, scheduled runs can be hours late, and
  retail day trading loses money for nearly everyone who tries it. The dip and gap rules
  failed their backtests too.
- **Crypto.** It is not part of the strategy, and its Shariah status is disputed.
- **Orders by value.** Trading 212's API takes a number of shares (at most 4 decimal places),
  not an amount of money; only the app orders by value. The bot converts money to shares.
- **Live quotes.** There is no quotes endpoint. The bot prices what you hold from your
  holding, and a new pick from Yahoo's close converted to your currency, accepted only if an
  independent source (the SPUS holdings file or Nasdaq) agrees within 25%.
- **Margin, short selling, CFDs or extended hours.** It buys with cash, sells only what it
  holds, and never sends an order into a closed market.
- **Other brokers out of the box.** Only Trading 212 and the paper account ship. An adapter
  for another broker can be written from `brokers/template.py`; see
  [docs/BROKERS.md](docs/BROKERS.md).

## Configuration essentials

Everything is in `config.json`, and every section carries an `about` or `note` explaining its
keys. These are the ones people actually change:

| Key | Default | What it does |
|---|---|---|
| `account.currency` | `"GBP"` | Your account's currency: GBP, EUR or USD. Caps and values are in it. The paper account fixes its currency when created; reset it after a change. |
| `broker.adapter` | `"paper"` | `"paper"` (simulated), `"trading212"`, or the name of a module in `brokers/`. |
| `execution.environment`, `broker.environment` | `"demo"` | Trading 212's server: `"demo"` (practice) or `"live"` (real money). Keep the two equal. |
| `broker.paper.starting_cash` | `10000.0` | The paper account's cash after a reset. |
| `strategy.momentum.core_ticker` | `"MWIXl_EQ"` | The core fund line. Use the one in your account currency; for paper trading it also needs an entry in `broker.paper.symbols`. |
| `strategy.momentum.core_weight` | `0.2` | Share of the account in the core fund. In the research 0.4 gave up about half a point a year (15.9% against 16.4%) and 0.6 about 1.2 points (15.2%), each for a shallower worst fall (-46.8% and -45.1% against -48.9%); all flattered. |
| `strategy.momentum.mode` | `"live"` | `"shadow"` is a dry run: it works everything out and prints it but sends nothing. `"live"` sends through the gates. |
| `risk.max_order_value`, `max_daily_spend`, `max_orders_per_day` | 2500, 7000, 60 | The buy caps. The defaults suit an account of roughly 10,000 to 20,000; lower them for a first real-money run. |
| `risk.max_position_value` | `1500.0` | The cap on any single stock. At the defaults a pick's 8% passes it once the account passes about 18,750; raise it as the account grows. |
| `schedule.utc_times` | 13 times | When `run.py loop` runs, in UTC. On GitHub, change the cron lines in `trading.yml` to match. |

`execution.execute` (true) is the config half of the `--execute` switch: set it to false and
every run only proposes. The optional rules in `strategy` (scheduled buys, a rebalance, price
signals) and `risk.near_high` are off by default.

## Project layout

| File | What it is |
|---|---|
| `run.py` | The launcher: `once`, `loop`, `selftest`, `doctor`. |
| `trade.py` | The runner, and the only file that places orders. |
| `broker.py` | The broker connection and its read-only commands (`check`, `instruments`, ...). |
| `brokers/__init__.py` | The adapter contract: Trading 212's shapes, which every adapter answers in. |
| `brokers/paper.py` | The paper account, Trading 212 emulated in a local file. |
| `brokers/template.py` | A skeleton for another broker's adapter. |
| `momentum.py` | The momentum book: monthly picks and the trades toward them. |
| `universe.py` | The not-haram stock list, from the index members and `compliance.screen`. |
| `gates.py` | The checks every proposal must pass. |
| `sizing.py` | Money to share quantity, and the order ledger. |
| `hours.py` | Market hours, from Trading 212's schedules and the config fallback. |
| `pricefeed.py` | Daily closes from Yahoo, with Nasdaq as fallback. |
| `rules.py` | Optional scheduled buys and rebalance, off by default. |
| `signals.py` | Optional price signals that can only block a scheduled buy. |
| `research.py` | The pre-registered, point-in-time backtests quoted above. |
| `backtest.py` | An older backtest whose levels are inflated; compare its rows, never quote them. |
| `research/request.txt` | The research lines `research.yml` runs. |
| `config.json` | Every setting, documented in place. |
| `prices.csv` | Optional seed prices, `date,ticker,close`, empty as shipped. |
| `requirements.txt` | Only `tzdata`, and only on Windows. |
| `Dockerfile`, `docker-compose.yml` | The bot in a container. |
| `.env.example` | Where Trading 212 keys go, copied to `.env`. |
| `.github/workflows/` | `ci.yml` (the tests: Linux, Windows and macOS in a public repository or on a run started by hand, Linux only on a push to a private copy), `trading.yml` (the bot), `research.yml` (backtests). |

The bot writes these in the project folder:

- the order ledger, one per account: `orders_placed.json` for Trading 212 live,
  `orders_placed.demo.json` for Trading 212 practice, `orders_placed.paper.json` for the
  paper account (and `orders_placed.<adapter>.json` for any other adapter);
- `momentum_targets.json`, this month's picks, stamped with the adapter that built them;
- `paper_account.json`, the paper account.

They are not git-ignored, because the GitHub workflow commits them back, but they hold your
cash, holdings and orders: never push them to a public repository.

## Documentation

- [docs/STRATEGY.md](docs/STRATEGY.md): the strategy in full, the evidence, and how to
  reproduce it.
- [docs/RUNNING.md](docs/RUNNING.md): running unattended on your computer, in Docker or on
  GitHub Actions.
- [docs/TRADING212.md](docs/TRADING212.md): connecting a Trading 212 account, practice first.
- [docs/SCREEN.md](docs/SCREEN.md): the halal screen in full.
- [docs/BROKERS.md](docs/BROKERS.md): writing an adapter for another broker.
- [CONTRIBUTING.md](CONTRIBUTING.md): tests, house rules, and proposing a research test.

## Licence and disclaimer

MIT, see [LICENSE](LICENSE). The software is provided as is, without warranty of any kind.

This project is not financial advice and not a recommendation to buy or sell anything. Its
backtests are flattered and its expectations may be wrong; you can lose money, including a
large part of what you invest. You are responsible for every order placed with it, for
checking the code and the settings before trusting them, for your broker's terms, and for
your own Shariah judgement: the screen is a filter, not a fatwa.

This project is independent and is not affiliated with or endorsed by Trading 212 or by any
fund, index or data provider named here.
