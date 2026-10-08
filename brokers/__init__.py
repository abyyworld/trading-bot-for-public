"""
Broker adapters: the plug-in point for anything other than Trading 212 itself.

Every module in this project speaks Trading 212's REST dialect through
broker.request(broker_config, path, key, secret, method, payload, timeout), which returns
(status, parsed body or text), status None on a network error. When config.json's
broker.adapter is not "trading212", that call is handed to brokers/<adapter>.py:

    request(broker_config, path, method="GET", payload=None, timeout=30) -> (status, body)

broker_config is config.json's broker section; broker.load_broker_config adds config_file,
the path it was read from. path is one of broker.endpoints below, possibly with a query
string. An adapter answers in Trading 212's shapes, status 200 for success:

    account_summary       GET   {currency, cash: {availableToTrade}, investments: {currentValue}}
    positions             GET   [{instrument: {ticker, currency}, quantity, currentPrice,
                                  averagePricePaid, walletImpact: {currentValue, currency}}]
                                currentPrice in the instrument's own units (pence for a GBX
                                line), walletImpact.currentValue in the account currency
    history_orders        GET   {items: [{order: {id, ticker, quantity, filledQuantity,
                                  status, createdAt, type}, fill: {...}}], nextPagePath}
                                newest first; ?limit= up to 50, and nextPagePath (with or
                                without its /api/v0 prefix) asks for the next page, null at
                                the end
    pending_orders        GET   [] or the orders not yet filled
    history_dividends     GET   {items: [], nextPagePath: null}
    history_transactions  GET   {items: [], nextPagePath: null}
    instruments           GET   [{ticker, shortName, name, type, currencyCode,
                                  workingScheduleId}]; US shares as SYMBOL_US_EQ, type STOCK
    exchanges             GET   [{id, name, workingSchedules: [{id, timeEvents:
                                  [{date, type: OPEN|CLOSE}]}]}]; a schedule id with no
                                entry here falls back to config execution.market_hours_fallback
    place_market_order    POST  {ticker, quantity}: positive buys, negative sells, at most 4
                                decimal places, the ticker spelled exactly. 200 with {id,
                                ticker, quantity, status}; a refusal is 400 with {type,
                                detail}. A timeout must be status None, as the order may
                                still have been placed.

Module constants:

    NEEDS_CREDENTIALS      False when there is no key and secret to read (paper), and
                           broker.credentials then returns ("", ""). True otherwise: the
                           adapter reads its own with broker.secret_value(NAME), which looks
                           in the environment and then in .env.
    LABEL                  what a run prints as the account, e.g.
                           "paper (simulated, no real money)".
    REQUEST_DELAY_SECONDS  optional: the gap broker.py check leaves between probes, else
                           broker.request_delay_seconds.

paper.py is the reference adapter, template.py the skeleton for a new one.
"""
