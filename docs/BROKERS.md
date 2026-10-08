# Brokers and adapters

The whole bot speaks one dialect: Trading 212's REST API. Every module reads the account and
places orders through one function, `broker.request`, and expects Trading 212's paths and
response shapes. Any other broker is plugged in as an **adapter**: a small module in
`brokers/` that takes a Trading 212 request, does the equivalent with its own broker, and
answers in Trading 212's shape.

Two brokers ship:

| `broker.adapter` | What it is |
| --- | --- |
| `"paper"` (the default) | `brokers/paper.py`: Trading 212 emulated in a local file. No account, no credentials, no real money. |
| `"trading212"` | Trading 212's own API, built into `broker.py`. See [TRADING212.md](TRADING212.md). |

`brokers/template.py` is a skeleton for writing a third. This page describes the contract
exactly as `broker.py` and `brokers/__init__.py` implement it, how the paper account behaves,
and how to write and test a new adapter. Nothing here is endorsed by any broker, and none of
it is financial advice.

## How the broker is chosen

`config.json` names it:

```json
"broker": {
  "adapter": "paper",
  ...
}
```

`broker.py` reads that value, strips it and lower-cases it, so `"Paper"` works too. Then:

- `"trading212"`, or an empty or missing value, means Trading 212 itself: `broker.py` makes
  the HTTP request, with Basic authentication, to `broker.environments[environment]`.
- Any other value must be a plain module name (`[a-z_][a-z0-9_]*`) and is imported as
  `brokers.<name>`, from the file `brokers/<name>.py`. A name like `../evil` is refused.
- A file that does not exist stops the run with its path. It never falls back to Trading 212:
  an account nobody chose is the last thing to trade on.

Every module calls the same function:

```python
broker.request(broker_config, path, key, secret, method="GET", payload=None, timeout=30)
# -> (status, body)
```

With an adapter configured, that call becomes the adapter's own:

```python
request(broker_config, path, method="GET", payload=None, timeout=30)
# -> (status, body)
```

The key and secret are not passed on: an adapter reads its own credentials.
`broker_config` is the `broker` section of `config.json`, plus one key that
`broker.load_broker_config` adds, `config_file`: the path of the config file the run was
started with, so an adapter can read another section of the same file (the paper adapter
reads `account.currency` this way). `--env` and `execution.environment` mean nothing to an
adapter; they choose between Trading 212's demo and live servers only.

### Module constants

| Constant | Meaning |
| --- | --- |
| `NEEDS_CREDENTIALS` | `False` when there is no key and secret to read, as for paper: `broker.credentials` then returns `("", "")` and asks for nothing. Missing or `True`: `broker.credentials` demands the two variables named in `broker.auth.key_env_var` and `broker.auth.secret_env_var` (default `T212_API_KEY` and `T212_API_SECRET`) and stops the run if either is empty. |
| `LABEL` | What a run prints as the account it is talking to, for example `"paper (simulated, no real money)"`. Without it, `"<name> adapter"`. |
| `REQUEST_DELAY_SECONDS` | Optional. The gap `python3 broker.py check` leaves between its probes. Without it, `broker.request_delay_seconds` from config (5). Paper sets 0. |

## The contract

An adapter returns `(status, body)`:

- `status` is an HTTP-style integer: `200` for success, `400` for a refusal, and so on. It is
  `None` when no answer came back at all (a network error or a timeout).
- `body` is the parsed JSON (a dict or a list) or, failing that, text.
- An error body follows Trading 212's shape, `{"type": "/api-errors/<kind>", "detail":
  "<message>"}`, so the runner can print the reason.

`path` is one of the values under `broker.endpoints` in `config.json`, sometimes with a query
string. The runner uses six of them; `python3 broker.py check` also probes the other three
read endpoints.

| Endpoint name | Path | Method | Read by |
| --- | --- | --- | --- |
| `account_summary` | `/equity/account/summary` | GET | every run |
| `positions` | `/equity/positions` | GET | every run, and the second pass |
| `history_orders` | `/equity/history/orders` | GET | every run (daily order cap, cooldown) |
| `instruments` | `/equity/metadata/instruments` | GET | the monthly build, the market-hours check |
| `exchanges` | `/equity/metadata/exchanges` | GET | the market-hours check |
| `place_market_order` | `/equity/orders/market` | POST | placing an order |
| `pending_orders` | `/equity/orders` | GET | `broker.py check` and `broker.py orders` only |
| `history_dividends` | `/equity/history/dividends` | GET | `broker.py check` only |
| `history_transactions` | `/equity/history/transactions` | GET | `broker.py check` only |

The examples below are synthetic: a GBP account holding a made-up US share, `AAA_US_EQ`,
priced at 100 dollars with the pound at 1.25 dollars.

### account_summary

```json
{
  "currency": "GBP",
  "cash": {"availableToTrade": 78.62},
  "investments": {"currentValue": 920.0}
}
```

- `cash.availableToTrade` is the cash a buy may use, in the **account currency**. Like every
  Trading 212 response it is nested; read flat, early versions misread the account without
  any error.
- `investments.currentValue` is the holdings' value in the account currency. It is how the
  runner tells "holds nothing" from "the positions list came back empty by mistake": if
  `positions` is `[]` while this is over 25, the run places nothing.

### positions

```json
[
  {
    "instrument": {"ticker": "AAA_US_EQ", "currency": "USD"},
    "quantity": 11.5,
    "currentPrice": 100.0,
    "averagePricePaid": 100.0,
    "walletImpact": {"currency": "GBP", "currentValue": 920.0}
  }
]
```

- The ticker is under `instrument.ticker`, spelled exactly as the broker wants it in an
  order. The runner keeps that spelling for the order body.
- `currentPrice` is in the instrument's own units: dollars for a US share, pence for a
  London line quoted in GBX.
- `walletImpact.currentValue` is the holding's value in the **account currency**. The runner
  prices a share as `currentValue / quantity`, so this is what sizes every order for
  something already held. Get it wrong and every order is the wrong size.
- `quantity` may have up to 8 decimal places (a purchase by value in Trading 212's app can
  leave that many).

### history_orders

The runner asks for `?limit=50`, and if that is refused it asks once more with no query.

```json
{
  "items": [
    {
      "order": {
        "id": 3,
        "ticker": "AAA_US_EQ",
        "quantity": 0.5,
        "filledQuantity": 0.5,
        "status": "FILLED",
        "type": "MARKET",
        "createdAt": "2026-10-14T14:00:00.000+00:00"
      },
      "fill": {"price": 100.0, "quantity": 0.5}
    }
  ],
  "nextPagePath": "/api/v0/equity/history/orders?limit=50&cursor=3"
}
```

- Newest first. Each item wraps the order under `order`, with the ticker at `order.ticker`
  and the time at `createdAt`.
- `createdAt` is ISO 8601 with an offset. Use UTC: the daily order count compares the first
  10 characters with today's UTC date, and the cooldown compares the strings.
- `limit` up to 50. `nextPagePath` asks for the next page, with or without its `/api/v0`
  prefix (the runner strips it before asking), and is `null` on the last page. The runner
  follows it, 10 seconds apart, until a page reaches back past the cooldown window, at most 4
  pages. Trading 212's own default page holds only 20 orders, which is why it pages.
- Include every order, those placed by hand too: the 120-hour cooldown counts from them.
- A history that cannot be read must be an error status, never an empty list. An unreadable
  history means today's order count is unknown, and the run refuses to place anything.

### pending_orders, history_dividends, history_transactions

`pending_orders` returns `[]` or the orders not yet filled, in the history's order shape. The
other two return `{"items": [], "nextPagePath": null}` unless the adapter has something to say.
Nothing that trades reads them.

### instruments

```json
[
  {
    "ticker": "AAA_US_EQ",
    "type": "STOCK",
    "currencyCode": "USD",
    "shortName": "AAA",
    "name": "Alpha Corp",
    "workingScheduleId": 900
  }
]
```

- US shares as `SYMBOL_US_EQ`, `type` `STOCK`, `shortName` the plain symbol. The monthly build
  finds each pick by matching its symbol to `shortName` among the `_US_EQ` rows; a pick with
  no row is skipped for the next in line.
- Every instrument the bot may order needs a `workingScheduleId`. With
  `execution.require_market_open` true, as shipped, an instrument with none is never traded.

### exchanges

```json
[
  {
    "id": 1,
    "name": "London Stock Exchange",
    "workingSchedules": [
      {
        "id": 101,
        "timeEvents": [
          {"date": "2026-10-14T07:00:00+00:00", "type": "OPEN"},
          {"date": "2026-10-14T15:30:00+00:00", "type": "CLOSE"}
        ]
      }
    ]
  }
]
```

- The sessions each schedule id is open, as `OPEN` and `CLOSE` events.
- A schedule id with no entry here falls back to `execution.market_hours_fallback` in
  `config.json`, keyed by ticker suffix. The shipped config has `_US_EQ`: 09:30 to 16:00 New
  York time, with the NYSE's holidays and early closes. Trading 212's own exchanges list has
  no NYSE or Nasdaq venue, so on Trading 212 too, US hours come from config.
- Returning `[]` is fine when the fallback covers every instrument traded.
- If this cannot be read, every order is held.

### place_market_order

The runner POSTs:

```json
{"ticker": "AAA_US_EQ", "quantity": 2.5}
```

- Positive `quantity` buys, negative sells. There is no side field.
- At most 4 decimal places. The runner never sends more, and rounds buys down.
- The ticker is spelled exactly: the allowlist's spelling first, then the position's.

Accepted, `200` (or `201`):

```json
{"id": 1, "ticker": "AAA_US_EQ", "quantity": 2.5, "status": "NEW", "filledQuantity": 0}
```

Refused, `400`:

```json
{"type": "/api-errors/selling-equity-not-owned",
 "detail": "Selling more equities than owned, owned: 2.5"}
```

No answer: `(None, "network error: ...")`. The order may still have been placed, so the
runner treats it as unknown: its ledger, written before the request, blocks a resend that
day, and no second pass runs. Return `200` only when the broker has really accepted the
order, and never retry a POST inside the adapter: the API is not idempotent, so a retry can
place the order twice.

### What the runner does with the answers

All of it fails closed:

- Positions or cash unreadable, or positions empty while the summary values holdings: the
  run places nothing.
- Order history unreadable: the run places nothing.
- Exchanges or instruments unreadable: every order is held.
- An order answered with `None`: no second pass in that run.
- An order ledger that exists but cannot be read: the run places nothing.

Each account has its own ledger (`sizing.ledger_path`): `orders_placed.json` for Trading 212
live, `orders_placed.demo.json` for its practice account, and `orders_placed.<adapter>.json`
for any other adapter, so yours is `orders_placed.<name>.json` and never shares a daily cap
with another account. The month's momentum targets are stamped with the adapter that built
them, and a run on a different adapter builds them again from its own instrument list.

## The paper adapter

`brokers/paper.py` is the reference adapter and the default. It keeps a simulated account in
a JSON file and answers every endpoint above in Trading 212's shapes, so the runner, the
gates and the sizing run unchanged against it.

### Settings

Under `broker.paper` in `config.json`:

| Key | As shipped | Meaning |
| --- | --- | --- |
| `state_file` | `"paper_account.json"` | The account, relative to the project folder (or an absolute path). |
| `starting_cash` | `10000.0` | Cash on creation and after a reset, in the account currency. |
| `fx_fee` | `0.0015` | Charged on an instrument whose currency is not the account's, buying and selling. |
| `hold` | `0.05` | A buy may use at most `1 - hold` of the free cash. Keep it equal to `risk.broker_hold`. |
| `symbols` | the core fund | Instruments other than US shares, such as the core fund: `{TICKER: {yahoo, price_unit, currency, venue, name}}`. |
| `currency` | not set | Optional: the account currency, overriding `account.currency`. |
| `indexes` | not set | Optional: which index lists to build the US share list from, else `strategy.momentum.indexes`. |

The shipped `symbols` entry:

```json
"MWIXl_EQ": {"yahoo": "MWIX.L", "price_unit": 0.01, "currency": "GBP", "venue": "LSE",
             "name": "Invesco MSCI ACWI Islamic M-Series UCITS ETF (Acc), GBX line"}
```

`yahoo` is the symbol its price is read under, `price_unit` converts Yahoo's quote into the
currency (0.01 for a line quoted in pence, which Trading 212 lists as GBX), and `venue` gives
it a schedule (`LSE` is the only venue the paper adapter knows). Check the Yahoo symbol
yourself before trusting a paper fill: a wrong symbol prices every paper trade wrongly.

### The account file

- Created on first use with `starting_cash`. Written whole to a temporary file beside it and
  renamed into place, so a crash leaves the previous state, never half of one.
- A file that cannot be read is reported (status 500) and left alone, never replaced.
- Its currency is fixed at creation: `broker.paper.currency`, else the run's
  `account.currency`, else GBP. If the account currency changes later, every request that
  touches the account is refused (status 409, `paper-currency-changed`) until you run
  `reset`.

### Prices and fills

- A market order fills at once, whole, at the latest daily close: for a US share from Yahoo
  (Nasdaq as fallback) in dollars, converted to the account currency at Yahoo's exchange rate;
  for a configured symbol from Yahoo's chart for its `yahoo` symbol, times `price_unit`.
  During a session Yahoo's newest bar can be the day's price so far.
- No spread, no slippage, no partial fills and no queue. A close more than 10 days old is no
  price, and with no price an order is refused rather than filled at a guess.
- Prices and rates are kept in memory for 15 minutes within one process.
- The reply to an accepted order says `"status": "NEW"` and `"filledQuantity": 0`, as Trading
  212's immediate reply does; the history shows it `FILLED`.

### Fees and the hold

- `fx_fee` (0.15%, Trading 212's currency conversion fee) on any instrument whose currency is
  not the account's, both ways. It shows in the history as a `CURRENCY_CONVERSION_FEE`. No
  other costs.
- A buy whose cost plus fee is more than `(1 - hold)` of the free cash is refused as
  `insufficient-free-for-stocks-buy`, as Trading 212 keeps back part of the free funds on a
  market buy.

### Refusals

| Status, type | When |
| --- | --- |
| 400 `invalid-quantity-precision` | More than 4 decimal places. |
| 400 `selling-equity-not-owned` | Selling more than is held. |
| 400 `min-opened-position-exceeded` | A sell that leaves a holding worth under 1.00 of the account currency ("must have opened position at least 1.00"). |
| 400 `insufficient-free-for-stocks-buy` | A buy over `(1 - hold)` of the free cash. |
| 400 `instrument-not-found` | A ticker not spelled exactly as listed. Case matters: `MWIXl_EQ`, never `MWIXL_EQ`. |
| 400 `paper-no-price` | No usable price. |
| 400 `bad-request` | A body that is not `{ticker, quantity}`, a zero quantity, or a bad history query. |
| 503 `instruments-unavailable` | A US share ordered while the US share list cannot be built. |
| 404, 405 | An unknown path, or the wrong method. |

The precision, unknown-ticker and paper-specific types are this adapter's own spelling;
`selling-equity-not-owned`, `min-opened-position-exceeded` and
`insufficient-free-for-stocks-buy` are Trading 212's. Trading 212 does not say which unit its
1.00 is in; the paper adapter reads it as the account currency.

### Market hours

- `exchanges` publishes one venue, the London Stock Exchange: weekdays, 08:00 to 16:30 London
  time, for the next 14 days, summer time included. It knows no UK holidays, so a paper
  account fills the core fund on a bank holiday.
- US shares carry schedule id 900, which `exchanges` does not publish, so their hours come
  from `execution.market_hours_fallback`, exactly as on Trading 212.
- A configured symbol on a venue other than `LSE` gets schedule id 999, which nothing covers,
  so the runner never trades it.
- The paper adapter fills an order whenever it is sent. It is the runner's market-hours check
  that keeps orders inside the session, as it is on the real broker.

### Instruments

- US shares as `SYMBOL_US_EQ`, built from the S&P 500 members (Wikipedia's list, through
  `universe.py`) and SPUS's holdings file, cached in the account file for 7 days. If a
  refresh fails, the old copy is kept. Any share held stays listed, so it can always be sold.
- Every configured symbol, on its venue's schedule.
- Dividends, interest, splits and other corporate actions are not modelled.

### Commands

```
python3 brokers/paper.py show                 cash, holdings and the 10 latest orders
python3 brokers/paper.py reset                start again from starting_cash
python3 brokers/paper.py buy MWIXl_EQ 2000    buy 2,000 of the account currency's worth
python3 brokers/paper.py sell MWIXl_EQ 10     sell 10 shares
python3 brokers/paper.py sell MWIXl_EQ all    sell the whole holding
python3 brokers/paper.py selftest             offline checks
```

Add `--config PATH` before the command to use another config file (and so another
`state_file`). A fresh account looks like this:

```
$ python3 brokers/paper.py show
paper (simulated, no real money), GBP, state in /path/to/halal-momentum-bot/paper_account.json
cash            10000.00
invested            0.00
total           10000.00

TICKER                 QUANTITY          PRICE        VALUE
(nothing held)

latest orders
```

- `buy` is the paper version of the one purchase you make by hand in Trading 212's app. The
  bot sizes every order from a price, and Trading 212 has no quotes endpoint, so it can only
  price what you already hold. Like the app, `buy` spends an amount of money and can leave up
  to 8 decimal places.
- `sell` takes a number of shares at any precision, or `all`, which empties the holding as
  the app's sell-all does.
- Both act at once, like a purchase in the app: they do not wait for the market to open and
  do not pass the bot's gates. They do appear in the order history, so the bot's 120-hour
  cooldown applies to that instrument from then on.
- `reset` keeps the cached US share list. The bot's own ledger for the paper account
  (`orders_placed.paper.json`) still records today's orders, so the bot will not resend them
  today and still counts them against the daily cap; delete that file to start the paper
  ledger afresh too. It holds paper orders only: Trading 212 practice and live keep
  `orders_placed.demo.json` and `orders_placed.json`.
- If `broker.adapter` is not `paper`, the commands still work on the paper file and print a
  note that the bot is not using it.

Without internet access, `show` and `reset` work, and `buy` is refused with
`paper-no-price`, as no price can be read.

## Writing an adapter for another broker

An adapter is translation only: take a Trading 212 request, ask the other broker, answer in
Trading 212's shape. Nothing else in the project changes.

1. Copy the template:

   ```
   cp brokers/template.py brokers/mybroker.py
   ```

2. Set `"adapter": "mybroker"` under `broker` in `config.json`. Set `account.currency` to
   the other account's currency (GBP, EUR or USD).
3. Fill in each function in `brokers/mybroker.py`. Every function the template does not
   implement raises `NotImplementedError`, so a half-finished adapter stops a run loudly
   instead of trading on a guess. `template.endpoint_of` already maps a path, query string
   included, to its endpoint name. Set `LABEL`, and `NEEDS_CREDENTIALS`.
4. Credentials: point `broker.auth.key_env_var` and `broker.auth.secret_env_var` at your
   broker's variable names, so the up-front check asks for the right ones, and read them in
   the adapter with `broker.secret_value("MYBROKER_KEY")`, which looks in the environment and
   then in the project's `.env` file. Never print them.
5. Write a selftest (see below) and add `"brokers/mybroker"` to `MODULES` in `run.py`, so
   `python3 run.py selftest` and CI run it. On GitHub Actions, add it to the list in the
   selftest step of `trading.yml` too, so every trading run checks it first. The trading
   workflow runs any adapter but `paper` only in a private repository.
6. Check it against the other broker's practice account, in this order:

   ```
   python3 broker.py check                  every read endpoint: "6 of 6 read endpoints worked."
   python3 broker.py summary                the account summary, as the bot will read it
   python3 broker.py positions
   python3 broker.py history
   python3 broker.py instruments "apple"    the instrument list, searched
   python3 broker.py exchanges              the schedules, and which is open now
   python3 run.py doctor
   python3 run.py once                      a full run that places nothing
   python3 run.py once --execute            on the practice account only
   ```

7. Run it on the practice account through at least one monthly window, and read every run's
   output, before you point it at real money.

### Checklist

The gates and the sizing rely on every one of these. Get one wrong and the bot can misjudge
an order without any error.

- [ ] **Money in the account currency.** `cash.availableToTrade`,
      `investments.currentValue` and `walletImpact.currentValue` are all in the account's
      currency, with any conversion already applied. `currentPrice` is in the instrument's own
      units.
- [ ] **Tickers in Trading 212 style.** US shares as `SYMBOL_US_EQ`, `type` `STOCK`,
      `shortName` the plain symbol. Pick one spelling per instrument and translate it to and
      from the other broker's symbol inside the adapter, both ways. The core fund's ticker
      must match the one in `compliance.allowlist` and `strategy.momentum.core_ticker`.
- [ ] **At most 4 decimal places.** Pass the quantity on as given. If the other broker takes
      only whole shares, refuse a fractional order with a 400; never round it up.
- [ ] **The sign is the side.** Positive buys, negative sells.
- [ ] **History newest first, with `createdAt`.** Every order, hand-placed ones included,
      wrapped as `{"order": {...}}` with `order.ticker` and `createdAt` in UTC ISO 8601 with
      an offset. Honour `limit` (up to 50) and page with `nextPagePath`, `null` at the end.
- [ ] **Fail closed.** A read that fails is an error status, never an empty list or a zero.
      A POST with no answer is `(None, "...")`. `200` only when the broker accepted the
      order. No retries of a POST.
- [ ] **Hours.** Every tradeable instrument has a `workingScheduleId`; `exchanges` publishes
      its sessions, or returns `[]` and a `market_hours_fallback` entry covers its ticker
      suffix. With neither, the instrument is never traded, which is the safe failure.
- [ ] **Positions reflect fills.** The second pass waits until the positions show every
      accepted order before it spends a sale's cash.
- [ ] **Rate limits.** Respect the other broker's own; set `REQUEST_DELAY_SECONDS` for
      `broker.py check`.
- [ ] **A selftest.** Offline, against a fake of the other broker.

### The selftest

`python3 run.py selftest` runs `python3 brokers/<name>.py selftest` in its own process and
reads the last line of the form `N checks, M failed`. The module must print that line and
exit with status 0 only when nothing failed. The template prints only its docstring, so it
needs a `selftest` command of its own.

Make the selftest offline: replace the HTTP call with a function the test injects, as
`brokers/paper.py` does with its `HOOKS` (a price fetcher, a web getter, an exchange rate and
a clock), and check at least:

- each endpoint's shape, with tickers, money and timestamps converted as the checklist says;
- a refusal comes back as 400 with a `type` and a `detail`;
- a timeout on a POST comes back as status `None`, and a failed read as an error status;
- history paging, newest first, ending with `nextPagePath` `null`;
- more than 4 decimal places refused, and a sell's negative quantity sent as a sell.

`brokers/paper.py`'s own selftest (`python3 brokers/paper.py selftest`) is a complete example.

## Why no Alpaca or Interactive Brokers adapter ships

None could be tested against a real account, and an adapter that has never placed an order
is not one to trust with money. `brokers/template.py` sketches how each Trading 212 endpoint
might map onto Alpaca's and Interactive Brokers' APIs, for orientation only: check every
mapping against the broker's current documentation before relying on it.

On a small account Trading 212's 0.15% currency fee costs less than per-order commissions
elsewhere, so cost alone is little reason to switch. If you write and test an adapter
for another broker, a pull request is welcome; see [CONTRIBUTING.md](../CONTRIBUTING.md).
