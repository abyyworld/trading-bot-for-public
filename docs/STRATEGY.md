# The strategy and the evidence

What the bot does, exactly, and what the backtests say about it. Everything here can be
checked against the code (`momentum.py`, `gates.py`, `sizing.py`, `trade.py`) and rerun with
`research.py`.

> [!IMPORTANT]
> This is not financial advice. The bot follows fixed rules and can lose money, including a
> fall of about half the account in a crash like 2008's. Every backtest below is flattered,
> and past results do not guarantee future ones. The halal screen is a filter, not a fatwa
> ([SCREEN.md](SCREEN.md)).

## In short

The account is split in two. `core_weight` of it (20% by default) sits in an Islamic index
fund. The rest is split evenly over the 10 stocks with the highest 12-1 momentum among the
S&P 500's members minus businesses whose core is haram. The picks are chosen once a month,
and the bot trades toward them only in the 7 days after each monthly build. Every order
passes the same hard limits, and anything the bot cannot read or price is not traded.

## The book

| Part | Default | Setting |
| --- | --- | --- |
| Core fund | 20% of the account in Invesco MSCI ACWI Islamic, line `MWIXl_EQ` (pence, London Stock Exchange) | `strategy.momentum.core_ticker`, `core_weight` |
| Momentum sleeve | 80% of the account, split evenly over 10 picks: 8% each | `strategy.momentum.picks` |

The account's value is its cash plus everything it holds, so a pick's share is
`(1 - core_weight) / picks` of that total. With 10,000 in the account the core's share is
2,000 and each pick's is 800.

Buy the core fund in the line that matches your account's currency: on Trading 212 any other
line pays a 0.15% currency fee on every buy and every sell. `MWIMl_EQ` is the same fund in
dollars.

## 12-1 momentum

A stock's score is its return over the 12 months to the last complete month, skipping that
latest month: the close at the end of month t-1 divided by the close at the end of month
t-12, minus one, where t is the last complete month.

For a build in October 2026, t is September 2026, and the score is the end-of-August 2026
close over the end-of-September 2025 close, minus one. A stock needs all 13 monthly closes
from September 2025 to September 2026 to be ranked. Prices are Yahoo's monthly closes over a
two-year span. Skipping the latest month is the usual construction in momentum research,
because a stock's latest month tends to partly reverse.

## Which stocks it picks from

The universe is the S&P 500's current members (`strategy.momentum.indexes`, read from
Wikipedia's list) minus businesses whose core is haram, by `compliance.screen` in
`config.json` (`strategy.momentum.universe` `"not_haram"`). The screen fails closed; if it
cannot be trusted, or keeps fewer than `min_universe` (100) names, the holdings of SPUS, an
S&P 500 Shariah fund, are used instead with the named exclusions still applied. With
neither, no picks are built and nothing is bought. [SCREEN.md](SCREEN.md) has the details.

## The monthly build

A run whose saved targets are not from the current month builds new ones. Usually that is
the first run of the month. It needs the internet and takes a few minutes: the index list,
the SPUS holdings file, and two years of monthly closes for every name.

1. **The signal month** is the last complete month. If no stock has a close for it yet, no
   targets are built.
2. **Coverage.** At least 90% of the universe must have the 13 closes it needs, or the build
   is refused. Yahoo can return quarterly bars for long-listed companies, which silently drops
   them from a ranking; a build that ranked only part of its list is not trusted.
3. **Ranking**, best score first. Then, for each name in order:
   - **one slot per company**: a second share class (GOOGL beside GOOG) is skipped;
   - **listed with the broker**: the stock must be in the broker's instrument list as a US
     share, or it is skipped for the next in line;
   - **an independent reference price**: SPUS's holdings file if it holds the stock, else
     Nasdaq's close. A name with neither is skipped for the next in line. When the bot later
     buys a pick it does not hold, it prices it from Yahoo's close converted to the account
     currency, and buys only if that agrees with the reference within 25%
     (`price_check_tolerance`). Two sources must agree before an amount becomes a share
     count.
4. **Fewer than 10 picks found** means no targets this month, so nothing is bought or sold.
   The next run tries again.
5. The targets are saved to `momentum_targets.json` with the build date and the broker
   adapter whose instrument list they were matched to. A run on another adapter (paper, then
   Trading 212, say) builds the month again from its own list, since the brokers' tickers
   can differ.

## The trading window

The bot trades toward the targets on the day of the build and the 7 days after it
(`window_days`). The window counts from the build, so a build that only succeeds on the 5th
trades from the 5th to the 12th. On every other day a run checks and does nothing: swapping
daily would hand the gains to costs.

A pick can be bought only in the month it was picked, and only by the momentum book (no
other rule can buy it). Every run screens the picks again against the current
`compliance.screen`, so an exclusion added mid-month stops any further buying of that pick at
once; inside the window it is then sold as no longer a pick, otherwise in the next month's
window.

## The trades

Inside the window, each run works out:

1. **Sells first.** Anything held that is neither the core fund nor a current pick is sold
   whole.
2. **Each pick and the core toward its share**, inside a band of 10% (`band`):
   - held more than 10% above its share: trimmed back to it;
   - held more than 10% below its share: topped up to it;
   - in between: left alone, so drift alone causes no trades.

   With 10,000 in the account, a pick is topped up only below 720 and trimmed only above
   880, and the core only below 1,800 or above 2,200.
3. **Limits on a buy.** A buy is never asked for past the single-stock position cap, never
   more than `risk.max_order_value` at once, and no order under `min_trade` (25) is proposed.

> [!WARNING]
> **The bot treats the whole account as its own.** In each window it sells every holding that
> is neither the core fund nor a pick, including shares you bought yourself, and sells are
> never capped by value. Use an account that holds nothing else, or set
> `strategy.momentum.mode` to `"shadow"`: the book is then worked out and printed on every run,
> and nothing it proposes is sent.

### Sizing to the cash, and the broker's hold

Trading 212 accepts a market buy only up to about 95% of the free cash, keeping the rest back
against the price moving before the fill. So:

- The gate counts every buy as `value / (1 - risk.broker_hold)` (0.05) against the cash, and
  refuses one that would leave less than `risk.min_cash_buffer` (20).
- When the cash cannot cover a buy's whole share, the book asks only for what the cash surely
  covers, sized to survive a 10% hold (`strategy.momentum.cut_hold`), since the real hold is
  not documented and varies. A refused buy is costly: the ledger has recorded it, so it is not
  retried that day.
- It does so when that part-buy reaches the band, or when no sale is bringing more cash. While
  a sale in the same run is still to fill, it asks for the whole share and lets the gate wait
  for the cash, because a part-buy now would start the 120-hour cooldown with the holding
  short.
- A sale's cash does not count within the batch that sells, since it lands only once the sale
  fills. The second pass spends it.

## From proposal to order

Every proposal, whatever produced it, goes through the same steps (`trade.py`):

1. **Gates** (`gates.py`). A proposal failing any one is dropped, and every reason is printed:
   - the `HALT` file (`risk.killswitch_file`) is absent;
   - a buy is of something on `compliance.allowlist`, or a momentum buy of this month's pick;
     a sell never needs the allowlist, so a holding that fails the screen can always be sold;
   - buy caps: `max_order_value` per order, `max_daily_spend` across every run of the day
     (counted from the ledger), and the position cap, `max_position_value` for a single stock
     or the fund's own entry in `max_position_value_by_ticker`;
   - the cash buffer, with the broker's hold;
   - `max_orders_per_day`;
   - `cooldown_hours_per_instrument` (120): no second order in the same instrument, buy or
     sell, within 120 hours of the last, counted from the broker's order history;
   - one buy and one sell per instrument per batch.
2. **Sizing** (`sizing.py`). Money becomes a share quantity at most 4 decimal places, rounded
   down, and the cap is checked again on the rounded value. The price is the holding's own
   (the broker's valuation divided by the quantity), or for a pick not yet held the checked
   outside price, or a row in `prices.csv`; with none, no order. Selling the whole of a
   holding with more than 4 decimal places (a purchase by value in Trading 212's app can
   leave 8) leaves `risk.min_position_remainder` (2) behind, because the broker refuses both
   selling more than is held and leaving a sliver. A holding the bot bought itself fits in 4
   places and is sold exactly.
3. **The ledger**, one file per account (`orders_placed.json` for Trading 212 live,
   `orders_placed.demo.json` for practice, `orders_placed.paper.json` for paper). One buy and
   one sell per instrument per day. It is written before each request, because the API is
   not idempotent and a request that timed out may still have placed its order. A ledger
   that exists but cannot be read stops every order rather than being read as empty.
4. **Market hours.** The core fund trades in London Stock Exchange hours and the picks in New
   York's (09:30 to 16:00 New York time, with NYSE holidays). An order for a closed market is
   held for a later run, never sent to queue.

Then an order is sent only if all of these hold: `--execute` on the command line,
`execution.execute` true in `config.json`, no `HALT` file, the account's cash and holdings
read in full, today's order history read, and `strategy.momentum.mode` `"live"` for momentum
orders. Anything missing means the run only proposes.

## The second pass

After a run's sells are placed, the run waits until every accepted order shows in the
positions (up to `execution.second_pass.polls` times `wait_seconds`: 6 times 10 seconds),
reads the whole account again and **buys** what the sale's cash now pays for, through the
same gates, sizing, ledger and market-hours check. It never sells. What the run already sent
counts toward the day's orders and spend and starts each cooldown, even before the broker's
history shows it.

It fails closed: an order with no answer, a positions, cash or history read that is refused or
short, or a holding missing from the fresh read means no second pass, and the next run buys
instead. Without it, a sale's cash would wait for the next run. Switch it off with
`execution.second_pass.enabled` false.

## Caps and account size

All amounts are in the account currency. The defaults suit an account of roughly 10,000 to
20,000.

| Key under `risk` | Default | Note |
| --- | --- | --- |
| `max_order_value` | 2500 | Keep it at least one pick's share. A bigger buy is cut to it and finished by a later run. |
| `max_daily_spend` | 7000 | Spans every run of a day. Keep it under the account's value. |
| `max_orders_per_day` | 60 | Room for a full rebuild (ten sells, eleven buys) and retries. |
| `max_position_value` | 1500 | Any single stock. |
| `max_position_value_by_ticker` | 100000 for the core lines | A ceiling against typos, not a concentration limit. |
| `min_cash_buffer` | 20 | Cash left after every buy. |
| `broker_hold` | 0.05 | The broker's hold, counted on every buy. |
| `cooldown_hours_per_instrument` | 120 | Buy or sell. |

A pick's 8% passes the 1,500 single-stock cap once the account passes about 18,750. Past
that, a pick is bought up to the cap and the rest of its share stays in cash, so raise
`max_position_value` as the account grows. Lower all three order caps for a first
real-money run.

## Settings that change the strategy

- `strategy.momentum.core_weight`: a bigger core gives up return for a shallower worst fall:
  about half a point a year at 0.4 and about 1.2 points at 0.6, all flattered (see the
  splits below).
- `strategy.momentum.mode`: `"live"` (shipped) sends through the gates; `"shadow"` is a dry
  run that prints what it would do and sends nothing.
- `strategy.momentum.enabled`: false switches the book off.
- Off by default, and kept for those who want them: scheduled buys and a rebalance
  (`strategy.scheduled_buys`, `strategy.rebalance`), a price-signal brake on scheduled buys
  (`strategy.signals`), and a rule against buying a stock within 5% of its 52-week high
  (`risk.near_high`), which lost return in every window of `backtest.py`.

The research below tested 10 picks re-picked monthly; changing `picks` or the timing leaves
that evidence behind.

## The evidence

### How it was tested

`research.py` replays the rule on daily prices:

- **Universe**: the S&P 500 minus haram businesses, point in time. A stock counts only from
  the day it joined the index, by the "Date added" column of Wikipedia's list.
- **Prices**: Yahoo's daily history.
- **Costs**: 0.20% a side (Trading 212's 0.15% currency fee plus a spread allowance).
- **Fills**: the next trading day after the signal.
- **Period**: every row scored over the same 227 months, November 2007 to September 2026, so
  no row skips the 2008 crash that the others are scored on.
- **Pre-registered**: each test and its pass rule were written down before the data was run,
  and judged on the full sample, each half and the last five years.

### Results

| Rule | A year | Worst fall |
| --- | ---: | ---: |
| Top 10 by 12-1 momentum (the momentum sleeve alone) | 16.2% | -53.7% |
| The same screened list, owned in equal amounts | 13.5% | -42.6% |
| 20% core, 80% momentum (the default) | 16.4% | -48.9% |
| 40% core, 60% momentum | 15.9% | -46.8% |
| 60% core, 40% momentum | 15.2% | -45.1% |
| Top 10 momentum without the join-date fix (biased, for scale) | 36.4% | |

- **Momentum is not steady.** It trailed equal weight from 2007 to 2017 (8.9% against 12.4% a
  year) and led from 2017 (23.9% against 14.6%).
- **The join-date fix matters.** Holding today's index members from before the index added
  them counts a company that ran up and was then added as a winner nobody could have bought.
  That inflates the momentum backtest by about 20 points a year.
- **The not-haram screen did as well as the S&P 500 Shariah index's own list** (SPUS): 16.7%
  against 16.9% a year, 35.7% against 33.8% over the last five years, and a worst fall of
  -51.1% against -52.2%, which passed its non-inferiority test. That comparison was a test
  of its own, so its figures differ slightly from the rows above.
- **Late fills cost nothing measurable.** Filled 1, 3 or 5 trading days after the signal,
  top-10 momentum made 16.2%, 16.6% and 17.5% a year. Scheduled runs that arrive late matter
  for getting a month's trades done inside the window, not for returns.

### What was tested and rejected

Nothing beat plain top-10 12-1 momentum under its pre-registered rule. The usual rule for a
replacement (`house_rule` in `research.py`): against top-10 12-1 momentum filled the next
day, beat it by at least 1.0 point a year over the full sample, be ahead in each half and in
the last five years, fall no more than 5 points deeper at worst, and keep the sign of the edge
when filled 5 trading days late. Ideas that trade less were judged by a holding rule instead:
at least 30% less turnover with returns within 0.5 points over the full sample, within 2
points in each window, and a worst fall no more than 3 points deeper.

- **Momentum variants:** top 5 (16.9% a year, but a worst fall of -61.7%), residual momentum,
  frog-in-the-pan, volatility-adjusted momentum, months 7 to 12, 52-week-high momentum, a cap
  of 3 per sector, and volatility-managed exposure.
- **A 10-month trend filter** (cash while the screened list, owned equally, is below its
  10-month average) cut the worst fall to about -28% for about 1 point a year, with a lower
  Sharpe ratio after 2017: insurance, not an upgrade. It is not part of the bot.
- **Holding longer:** hold bands of 15 to 35, quarterly re-picks, daily ranking with rare
  swaps, and a daily bad-news exit. The monthly re-pick stays.
- **Short-term ideas:** buying heavy-volume dips (-0.30% a trade), RSI(2) dips (0.00% a trade
  after costs), 52-week breakouts, and a news-gap rule that ranked only in the 88th
  percentile against 40 placebos. That matches the literature: large-cap post-earnings drift
  has largely gone since about 2006 (Martineau 2022), and positive news shocks in S&P 500
  stocks tend to reverse (Frank and Sanati 2018).
- **Five more, run on 2026-10-08, 0 of 7 deciding candidates passed:**
  - 12-0 momentum, no skipped month: 14.72%;
  - 12-1 skipping 5 days instead of a month: 16.93%, an edge of +0.95 against the +1.00
    needed, which halved when filled 5 days late;
  - earnings-announcement momentum: 16.02%, with a worst fall of -60.2%;
  - an earnings agreement filter on top-10 momentum: 14.10%;
  - intraday-only momentum: 14.75%, while its overnight placebo made more (17.68%), the
    opposite of what the idea needs;
  - the sector laggards inside the top 20: 9.12%;
  - three staggered tranches, each re-picked every 3 months: failed when filled 5 days late.

Do not re-tune a failed idea on the same data: a pass found that way is noise.

### What to expect

- After the usual decay of published effects, expect the momentum sleeve to beat the core
  fund by about **2.5 points a year**, anywhere from 0 to 5. On the default 20/80 split that
  is roughly **2 points a year on the whole account**, and it may be nothing.
- Expect a fall of **45 to 55%** in a 2008-style crash.
- Momentum can trail for a decade. From 2007 to 2017 it did.

### Why the numbers are too high

- **Missing companies.** Delisted and acquired companies are not in the data, because Yahoo
  has no history for them. Many of them left because they failed, so even 16% a year is too
  high. Compare rows, not levels.
- **Membership dates.** The join-date fix uses Wikipedia's "Date added"; a member with no
  recorded date counts as a member throughout, which still carries some of the old bias.
  `research.py run` prints how many such names there are.
- **Flattered splits.** In the core/momentum splits, the core is an equal-weight stand-in
  built from today's survivors, because the real fund's history is too short. Every split is
  flattered by it.
- **Many tests.** Dozens of variants were run on the same 19 years of data. Writing the rules
  down first stops a backtest becoming a search for the best-looking variant, but with this
  many tests some would pass by luck, so even a single pass would only have been provisional.
  The plain rule stays because nothing beat it, not because it is proven best.
- **Costs and execution.** The backtest charges 0.20% a side and nothing else. The live bot
  trades in a window after each build, inside market hours, with a cash buffer, caps and
  rounding, so its results will differ from any backtest.

## Reproducing it

`research.py` needs internet access (Yahoo, Wikipedia and the SPUS holdings file) and the
standard library only.

```
python3 research.py fetch sp500             download daily prices into research/cache
python3 research.py run mom10_L1 mom10_L5   top-10 momentum, filled 1 and 5 days late
python3 research.py run                     every test, in order (slow)
python3 research.py selftest                offline checks of the harness itself
```

- `fetch` downloads the index list, SPUS's holdings and each name's daily history, once.
  Cached files are reused; delete `research/cache` for fresh prices.
- `run` takes test names, the keys of `TESTS` in `research.py`. `L=n` in a name or label means
  filled n trading days after the signal. A book split needs its momentum row in the same run,
  so put `mom10` before `book20`, `book40` or `book60`, as the full run does.
- The output is a table for the full sample, each half and the last five years, every row
  over the same months: yearly return (CAGR), volatility, Sharpe ratio, worst fall and worst
  12 months. Then each pre-registered judge prints PASS or FAIL with every check and its
  numbers, or NOT JUDGED naming the rows it needs.
- The screen comes from `config.json` when the tests run, so a stricter or looser screen can
  be tested without downloading again.
- Expect small differences from the figures above: Yahoo revises its history, and Wikipedia's
  list changes as companies join and leave the index. The RSI(2) dip result came from an
  earlier script that is not part of this repository.

On GitHub, `.github/workflows/research.yml` runs each non-comment line of
`research/request.txt` as `python3 <line>`, on any push to a branch other than `main` that
changes that file, or from the Actions tab. It has no secrets and read-only permissions,
caches daily prices for a day, and puts the output on the run summary and in a
`research-output` artifact. The shipped file holds:

```
research.py fetch sp500
research.py selftest
research.py run mom10_L1 mom10_L5
```

To propose a new test, write its pass rule down first; [CONTRIBUTING.md](../CONTRIBUTING.md)
explains how.
