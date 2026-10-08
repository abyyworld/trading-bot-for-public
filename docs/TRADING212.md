# Trading 212

How to connect the bot to a Trading 212 account: a practice account first, then, if you
choose, a real one. For the ways to run it (by hand, a scheduler, Docker, GitHub Actions),
see [RUNNING.md](RUNNING.md).

This is not financial advice, and nothing here is endorsed by Trading 212. The bot follows
fixed rules and can lose money. Its Shariah screen is a filter, not a fatwa or a
certification: check `compliance` in `config.json` ([SCREEN.md](SCREEN.md)) with a scholar
you trust.

## Before you start

- **Account types.** The API works with Invest and Stocks ISA accounts. CFD accounts are not
  supported.
- **The bot treats the whole account as its own.** In each monthly trading window it sells
  every holding that is neither the core fund nor one of that month's picks, and sells are
  never capped by value. Use an account that holds nothing else, or first set
  `strategy.momentum.mode` to `"shadow"` in `config.json`: the book is then worked out and
  printed on every run, and nothing it proposes is sent.
- **Read Trading 212's API terms yourself** before you let anything trade unattended. This
  project does not interpret them for you.
- **Why the core is a UCITS fund.** UK retail investors cannot buy US-domiciled ETFs such as
  SPUS or HLAL (PRIIPs rules), so the core is a European (UCITS) Islamic index fund that
  Trading 212 lists.

## 1. Make an API key on the practice account

Start on Trading 212's practice account: virtual money, the same API, and the place to make
your mistakes.

1. Log in to Trading 212 and switch to the practice (demo) account.
2. Open **Settings**, then **API (Beta)**, and generate a key. Menu names can change; look for
   the API section of the settings.
3. Grant the permissions a run uses: reading the account (cash), the portfolio (positions),
   order history and metadata (the instrument list and exchange schedules), and placing
   orders. `python3 broker.py check` also reads pending orders and the dividend and
   transaction history, so grant those too if you want it to report every endpoint working.
   On the practice account it is reasonable to grant everything: it is virtual money, and
   placing orders is what most needs testing.
4. Trading 212 gives you **a key AND a secret**. Save both straight away. The bot
   authenticates with HTTP Basic over `base64(KEY:SECRET)`: one value alone cannot work.

**A key pair is bound to one environment and one account type.** A practice pair works only
against the demo server (`https://demo.trading212.com/api/v0`), a live pair only against the
live one (`https://live.trading212.com/api/v0`). An Invest account and a Stocks ISA need
separate pairs too. So when you move to real money, you generate a new pair.

## 2. Put the key where the bot can read it

**On your computer or in Docker**, copy `.env.example` to `.env` in the project folder and
paste each value straight after the `=`, with no quotes and no spaces:

```
T212_API_KEY=your-key
T212_API_SECRET=your-secret
```

`.env` is ignored by git and left out of Docker images. Never commit it or share it. You can
instead set `T212_API_KEY` and `T212_API_SECRET` as environment variables; a variable that is
set wins over the file.

**On GitHub Actions**, add them as repository secrets with the same names, in a **private**
copy of the project (see [RUNNING.md, section 4](RUNNING.md#4-github-actions)): every run
writes the account's cash, holdings and orders to its summary and commits its ledger, so the
workflow refuses to run on Trading 212 in a repository that is not private. Never put the
key or secret in `config.json` or any committed file.

## 3. Point the bot at Trading 212

In `config.json`:

```
"account":   { "currency": "GBP", ... }           your account's currency: GBP, EUR or USD
"broker":    { "adapter": "trading212",
               "environment": "demo", ... }
"execution": { "environment": "demo", ... }
```

`broker.environment` is what `broker.py` uses when run on its own, and
`execution.environment` is what a run uses: keep them equal. `--env demo` or `--env live` on
the command line overrides both for one command.

`account.currency` must be your account's own currency. Cash, the caps in `risk` and every
proposal are in it, and US share prices are converted into it. If Trading 212 reports the
account in another currency, a run places nothing and says which one to set.

**Coming from the paper account, nothing needs moving.** Each account keeps its own order
ledger in the project folder: `orders_placed.paper.json` for paper, `orders_placed.demo.json`
for Trading 212 practice, `orders_placed.json` for Trading 212 live. So the paper account's
orders never count against Trading 212's daily cap or block a buy as already placed. The
month's picks, `momentum_targets.json`, are stamped with the adapter that built them, and the
first run on Trading 212 builds them again from Trading 212's own instrument list: the paper
account names every US share `SYMBOL_US_EQ`, while Trading 212's ticker sometimes carries a
suffix. `paper_account.json` is not read while the adapter is `trading212`; keep it or delete
it as you like.

Then check:

```
python3 run.py doctor
```

It should say `account: Trading 212 demo, practice money` and that both credentials are set.

## 4. Check the connection with `broker.py`

`broker.py` only reads. It cannot place an order; placing lives in `trade.py`.

```
python3 broker.py check                  # probe every read endpoint
python3 broker.py summary                # account summary, including cash
python3 broker.py positions              # holdings
python3 broker.py orders                 # pending orders
python3 broker.py history                # recent order history
python3 broker.py exchanges              # the exchanges' working schedules
python3 broker.py instruments "islamic"  # find exact tickers
```

`--env` goes before the command: `python3 broker.py --env live check`.

`check` waits a few seconds between probes to stay inside the rate limits, then reports
`6 of 6 read endpoints worked.` If not:

| Status | Meaning |
| --- | --- |
| 401 everywhere | The pair does not match. In order of likelihood: it belongs to the other environment (demo versus live), it was made for the other account type (Invest versus Stocks ISA), or one value is missing or truncated. `check` suggests the command to try. |
| 403 | The key works, but that permission was not granted when it was made. Generate a new key with it. |
| 404 | A path under `broker.endpoints` in `config.json` is wrong. |
| 429 | Rate limited. Wait, or raise `broker.request_delay_seconds`. |

`exchanges` lists each schedule with its time windows and whether it covers the present
moment. Expect European and Canadian venues and no New York ones (see
[the surprises](#things-that-will-surprise-you)).

## 5. Find the right tickers

Trading 212 has its own instrument identifiers: `AAPL_US_EQ`, not `AAPL`. Find them with:

```
python3 broker.py instruments "islamic"
python3 broker.py instruments "islamic|msci acwi"
```

The search ignores case and matches the ticker, the name, the short name and the ISIN.
Several searches separated by `|` share one download of the instrument list, which Trading
212 allows only once every 50 seconds. Each line shows `TICKER`, `NAME`, `ISIN`, `CCY` (the
currency it is quoted in) and `TYPE`.

- **Copy the ticker exactly. Case matters.** London lines end in a lower-case `l`:
  `MWIXl_EQ` is an instrument, `MWIXL_EQ` is refused.
- **Some tickers differ from the exchange symbol**: a renamed or relisted company can carry a
  suffix. Check the name and ISIN, not just the letters.
- **You only need tickers for the core fund and anything you add to `compliance.allowlist`
  yourself.** The momentum book matches its picks to Trading 212's US tickers on its own.

## 6. Choose the core fund line in your account's currency

The default core fund is the Invesco MSCI ACWI Islamic M-Series UCITS ETF (Acc), ISIN
`IE000LFC57H7`: all-country, accumulating, tracking an MSCI Islamic index. Trading 212 lists
it several times, one line per currency:

| Ticker | Quoted in | Use it for |
| --- | --- | --- |
| `MWIXl_EQ` | GBX (pence), London | a GBP account. The default. |
| `MWIMl_EQ` | USD | a USD account |
| `MWIMs_EQ` | CHF | a CHF line exists; the bot does not support a CHF account |

**Buy the line in your account's currency.** Any other line pays Trading 212's 0.15% currency
conversion fee on every buy and every sell, for as long as you hold it.

GBX is pence, a hundredth of a pound. Once the fund is held, the bot prices it from Trading
212's own valuation in the account currency, so the factor of 100 never reaches the sizing.
Never put a pence price in `prices.csv` without dividing it by 100.

### A EUR account, or any other core line

1. Search for a line quoted in your currency:

   ```
   python3 broker.py instruments "islamic|IE000LFC57H7"
   ```

   Look for `EUR` in the `CCY` column. Whether this particular fund has a EUR line on Trading
   212 has not been checked by this project. If it has none, choose another Islamic UCITS
   index fund that does, and check its index and screening yourself; or accept the 0.15% fee
   on another line.

2. Edit `config.json`, using the exact ticker from the search (written `NEWLINE_EQ` below):

   - `account.currency`: `"EUR"`. Momentum then converts US share prices with Yahoo's
     EURUSD rate. Only GBP, EUR and USD are supported: with any other currency the picks get
     no price, so nothing is bought for them.
   - `strategy.momentum.core_ticker`: `"NEWLINE_EQ"`.
   - `compliance.allowlist`: add an entry. Nothing can be bought unless it is on the
     allowlist (or is one of the month's momentum picks):

     ```
     {
       "ticker": "NEWLINE_EQ",
       "name": "the fund's full name, so a mistyped ticker is obvious",
       "isin": "its ISIN",
       "currency": "EUR",
       "source": "the index it tracks, from the fund factsheet; verify yourself",
       "checked_on": "YYYY-MM-DD"
     }
     ```

   - `risk.max_position_value_by_ticker`: add `"NEWLINE_EQ": 100000.0`. The core fund needs
     its own cap, above `core_weight` times your account's value, because the single-stock
     cap `risk.max_position_value` would otherwise refuse every buy of it once the account
     grows.
   - `risk.near_high.exempt`: add it. This only matters if you switch the near-high rule on.
   - `strategy.scheduled_buys`: change the ticker there too, if you use scheduled buys.
   - `broker.paper.symbols`, only for the paper account: an entry with its `yahoo` symbol,
     `price_unit` (1, or 0.01 for a line quoted in pence), `currency`, `venue` and `name`.
     The paper account knows only the London Stock Exchange's hours (`"venue": "LSE"`). A
     line on any other exchange has no published hours there, so the market-hours check
     holds its orders on paper. To paper-trade a EUR account, the simplest course is to keep
     `MWIXl_EQ` as the paper core: the paper account charges it the 0.15% fee, as Trading
     212 would.

3. Check: `python3 run.py doctor`, then `python3 gates.py explain` to see the allowlist and
   limits as the gates read them.

**Changing the core line while you hold the old one sells the old one.** In the next monthly
window the bot sells every holding that is neither the core nor a pick, and the old line is
then neither. The same happens to a second line of the same fund held alongside the core.

## 7. The one purchase you make by hand

Trading 212 has no quotes endpoint: the API will not give a price for an instrument you do
not hold. The bot sizes every order from a price (orders are by number of shares; see below),
so it cannot make the first purchase of the core fund itself. The momentum picks are
different: they are priced from Yahoo's close converted to the account currency, and only if
an independent source (the SPUS holdings file or Nasdaq) agrees within 25%.

So, once, on the account your key belongs to (practice now, live later):

1. In the Trading 212 app, buy the core fund line you configured.
2. **Buy by number of shares, not by value.** A purchase by value can leave a holding with up
   to 8 decimal places, which the API cannot sell exactly (see below). A number of shares
   with at most 4 decimal places keeps every holding sellable to the last share.
3. Any amount will do. From then on the position carries a price, and the bot brings the
   core to `strategy.momentum.core_weight` of the account in each monthly window, within the
   caps: at most `risk.max_order_value` per order and one order per instrument every 120
   hours. Or buy the whole share yourself: `core_weight` times the account's value.

The alternative is a row in `prices.csv` in the project folder, `date,ticker,close`, with
the close in the account currency and no more than 5 days old:

```
date,ticker,close
2026-10-08,MWIXl_EQ,<latest close in pounds>
```

The hand purchase is simpler and proves the ticker and currency line are right before
anything automated touches them.

On the paper account the same step is `python3 brokers/paper.py buy MWIXl_EQ 2000`.

## Things that will surprise you

Most of these were learned against a real account, and the code is built around each one.
The exceptions are said where they come: the 95% in item 9 is from Trading 212's community
forum, and item 13 is untested.

1. **Two values, not one.** Authentication is HTTP Basic over the key and the secret
   together. A 401 on every endpoint almost always means the wrong environment or account
   type, not a bad key.
2. **There is no way to order by amount through the API.** The order body is
   `{ticker, quantity}`; ordering by monetary value is something only the app does. So
   `sizing.py` turns an amount into a number of shares, rounding down.
3. **Buy or sell is the sign of the quantity**: positive buys, negative sells. There is no
   side field.
4. **At most 4 decimal places in a quantity.** More is refused.
5. **No quotes endpoint.** The bot can price only what you hold, hence the hand purchase
   above.
6. **Tickers are Trading 212's own and case-sensitive** (`AAPL_US_EQ`, `MWIXl_EQ`), and some
   differ from the exchange symbol.
7. **Responses are nested.** A position's ticker is at `instrument.ticker`, an order in the
   history is under `order` with its time in `createdAt`, and cash is at
   `cash.availableToTrade`. Read flat, early versions saw an empty portfolio and daily caps
   that never counted anything, without any error.
8. **Order history comes in pages of 20 by default.** The bot asks for 50 at a time and
   follows `nextPagePath`, or the daily order cap and the cooldown would miss older orders.
9. **A buy can use only about 95% of your free cash.** Trading 212 holds the rest back against
   the price moving before the fill. It does not document the figure (the 95% comes from its
   community forum), and the holds seen varied, so the gate counts every buy as value / 0.95
   (`risk.broker_hold`), and a buy sized to the remaining cash is sized to survive a 10% hold
   (`strategy.momentum.cut_hold`). A refused buy blocks a retry for the rest of the day,
   because the ledger has recorded it.
10. **A holding bought by value in the app can have 8 decimal places.** Selling it rounded up
    to 4 is refused as selling more than you own (`selling-equity-not-owned`); rounded down,
    the sliver left over is refused too ("must have opened position at least 1.00", in a
    unit Trading 212 does not state). So a holding with 4 places or fewer is sold exactly, and
    any other whole exit leaves `risk.min_position_remainder` (2 of the account currency)
    behind. Clear that remainder in the app if you like. Buy by number of shares by hand and
    it never happens.
11. **The API is not idempotent.** Trading 212 warns that a repeated request may create a
    second order, and a request that times out may still have placed one. The ledger
    (`orders_placed.json` on live, `orders_placed.demo.json` on practice) is written before
    every request, so a resend is refused.
12. **The exchanges endpoint has no New York venues.** It publishes European and Canadian
    venues, not NYSE or Nasdaq. US hours therefore come from `config.json`
    (`execution.market_hours_fallback`): 09:30 to 16:00 New York time, with the NYSE's
    holidays and early closes listed to 2027-12-31. **Extend that list every year**: past
    `holidays_known_through`, the bot trades on every weekday and says it does not know the
    holidays.
13. **Whether a market order sent while the market is shut queues for the open is
    undocumented and untested here.** The bot never sends one: the market-hours check holds
    it, and it never sets `extendedHours`.
14. **A sale's cash can be spent almost at once.** On Trading 212 it has been seen spendable
    within a minute, which is what lets a run sell and then buy with the proceeds (the second
    pass in `trade.py`). If the cash is not there yet, the next run buys instead.
15. **A line in another currency costs 0.15% each way** in conversion fees.

## Rate limits

- 50 market orders a minute.
- One download of the instrument list every 50 seconds: search several names at once with
  `|`.
- Order history: about six requests a minute. The bot pages it with gaps between requests.

A monthly rebuild places a few dozen orders at most, far inside these. They rule out
intraday trading, which this project does not attempt. `python3 broker.py check` leaves
`broker.request_delay_seconds` (5) between its probes.

## From practice to real money

1. On the practice account: `broker.py check` reports every endpoint working, the core fund
   is bought by hand, and `run.py once` then `run.py once --execute` have run through at
   least one monthly window with output you have read and understood.
2. Read Trading 212's API terms.
3. Generate a **live** key pair, for your account type (Invest or Stocks ISA). Put it in
   `.env`, or in the repository secrets on GitHub, in place of the practice pair.
4. In `config.json`, set `broker.environment` and `execution.environment` to `"live"`. Live
   runs keep their own ledger, `orders_placed.json`, apart from practice's
   `orders_placed.demo.json`.
5. Lower `risk.max_order_value`, `risk.max_daily_spend` and `risk.max_orders_per_day` for
   the first real-money runs, and read `caps_note` and `position_cap_note` in `config.json`
   for how to size them to your account. Consider `strategy.momentum.mode` `"shadow"` for
   the first month.
6. Make the one hand purchase of the core fund on the live account.
7. `python3 run.py doctor` now says `account: Trading 212 LIVE, REAL MONEY`. Then
   `python3 broker.py --env live check`, then `python3 run.py once` and read what it would
   do, and only then `python3 run.py once --execute`, or switch on a schedule.

On GitHub Actions, a manual `execute` on the live account needs `PLACE ORDERS` typed in the
confirm box; the schedule, once `BOT_ENABLED` is `true`, does not ask. To stop everything at
once, create a file named `HALT` in the project folder (see
[RUNNING.md, section 5](RUNNING.md#5-stopping-it)).
