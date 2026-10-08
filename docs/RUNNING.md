# Running the bot

Every way to run it, from one run by hand to a schedule on GitHub's machines, with steps you
can copy and paste. If you plan to use a real Trading 212 account, read
[TRADING212.md](TRADING212.md) as well.

This is not financial advice. The bot follows fixed rules and can lose money, including a
fall of half the account in a crash like 2008's. Its backtests are flattered (delisted
companies are missing from the data), and its Shariah screen is a filter, not a fatwa: check
the lists in `config.json` ([SCREEN.md](SCREEN.md)) with a scholar you trust.

Contents:

1. [By hand, on Windows, macOS or Linux](#1-by-hand)
2. [Scheduled by your operating system](#2-scheduled-by-your-operating-system)
3. [Docker](#3-docker)
4. [GitHub Actions](#4-github-actions)
5. [Stopping it](#5-stopping-it)
6. [Troubleshooting](#6-troubleshooting)

## What the shipped settings do

- `broker.adapter` is `"paper"`: a simulated account kept in a local file, starting with
  10,000 of the account currency (`account.currency`, GBP as shipped). No broker account, no
  credentials, no real money.
- A run only proposes unless you add `--execute`. With `--execute`, an order still needs
  `execution.execute` true in `config.json` (it is, as shipped), no `HALT` file, and every
  check in `gates.py`.
- The momentum book is on (`strategy.momentum.mode` is `"live"`), so `--execute` on the paper
  account places simulated orders. Set it to `"shadow"` for a dry run: the book is worked out
  and printed on every run, and nothing it proposes is sent, whatever else is set.
- **The bot treats the whole account as its own.** In each monthly window it sells every
  holding that is neither the core fund nor one of that month's picks, including shares you
  bought yourself, and sells are never capped by value. On a real broker, use an account that
  holds nothing else, or keep the book in `"shadow"` mode until you have read what it would
  do.

Real money takes three deliberate changes: `broker.adapter` `"trading212"`,
`execution.environment` `"live"`, and `--execute` on the command line (or the GitHub
schedule, which always executes). Nothing in this page changes those for you.

The bot needs internet access when it runs: Yahoo for prices, Wikipedia for the S&P 500
list, the SPUS holdings file and Nasdaq for independent reference prices, and the broker. The
selftests and `run.py doctor` work offline.

**Run it in one place only.** The order ledger lives wherever the bot runs, one file per
account: `orders_placed.json` for Trading 212 live, `orders_placed.demo.json` for Trading
212 practice and `orders_placed.paper.json` for the paper account. Two copies trading the
same account (a laptop and GitHub Actions, say) keep two ledgers, so neither knows what the
other spent today and the daily spending cap holds for each copy separately. If you move the
bot, copy its state files (below) with it.

## 1. By hand

### Install Python 3.9 or newer

**Windows**

1. Download Python from [python.org](https://www.python.org/downloads/) and run the
   installer. Tick "Add python.exe to PATH".
2. Open Command Prompt and check: `py --version`
3. Windows ships Python without the time zone data the market-hours check needs. In the
   project folder (next step), run:

   ```
   py -m pip install -r requirements.txt
   ```

On Windows, type `py` wherever this page says `python3`.

**macOS**

1. Open Terminal and run `python3 --version`. If macOS offers to install the command line
   developer tools, accept: they include Python 3.9, which is enough.
2. Or install a newer Python from [python.org](https://www.python.org/downloads/) or with
   Homebrew (`brew install python`). With python.org's installer, also run
   `Install Certificates.command` in the `/Applications/Python 3.x` folder once, or every
   download fails with `CERTIFICATE_VERIFY_FAILED`.

No `pip install` is needed: the bot uses the standard library only.

**Linux**

Python 3 is usually there already: `python3 --version`. If not,
`sudo apt install python3` (Debian, Ubuntu) or `sudo dnf install python3` (Fedora). No
`pip install` is needed. If `run.py doctor` reports no time zone data, install your
distribution's `tzdata` package (`sudo apt install tzdata`).

### Get the code

```
git clone https://github.com/abyyworld/halal-momentum-bot.git
cd halal-momentum-bot
```

Or use the green Code button on GitHub, Download ZIP, and unzip it. If you want the bot to
run on GitHub Actions, make a private copy instead: see [section 4](#4-github-actions).

Run every command below from this folder.

### Check it

```
python3 run.py doctor
python3 run.py selftest
```

`doctor` checks Python, the time zone data, `config.json`, the broker settings, the
credentials and the schedule, without using the network. It ends with
`nothing blocks a run`, or with the problems to fix (see
[Troubleshooting](#6-troubleshooting)). `selftest` runs every module's own tests against fake
brokers, each in its own process; it takes about a minute and ends with
`N checks, 0 failed`. `python3 run.py selftest gates trade` runs only the modules you name.

### A first run on the paper account

```
python3 brokers/paper.py show
python3 brokers/paper.py buy MWIXl_EQ 2000
python3 run.py once
python3 run.py once --execute
python3 brokers/paper.py show
```

In order:

1. `show` prints the paper account: 10,000 in cash, nothing held.
2. `buy MWIXl_EQ 2000` buys 2,000 worth of the core fund by hand, the paper version of the
   one purchase you make in the Trading 212 app. The bot sizes every order from a price, and
   Trading 212 has no quotes endpoint, so it can only price what you already hold (see
   [TRADING212.md](TRADING212.md#7-the-one-purchase-you-make-by-hand)). 2,000 is the core's 20%
   share of 10,000.
3. `run.py once` proposes and places nothing. The first run of each month builds that
   month's momentum picks: it downloads price histories for several hundred stocks, which
   takes a few minutes, and writes them to `momentum_targets.json`.
4. `run.py once --execute` places what survives every check, here on the paper account.
5. `show` again lists the holdings and the latest orders.

Things you will see that are normal:

- `momentum outside the rebalance window (built ...), holding`: the book trades only in the
  7 days after each monthly build (`strategy.momentum.window_days`). On other days a run
  checks and does nothing.
- `HELD <ticker>: ...`, with a reason such as `closed, next open ...` or `weekend ...`: an
  order waits for its market. The core fund trades in London Stock Exchange hours, US picks
  in New York hours (09:30 to 16:00 New York time). A later run places it.
- `PROPOSE ONLY, nothing was placed:` followed by the reasons, such as
  `--execute was not passed`.
- `DUPLICATE ... already placed today, skipping`: the ledger stopping a repeat.

`python3 brokers/paper.py reset` starts the paper account again from `starting_cash`.
`python3 brokers/paper.py sell MWIXl_EQ all` sells a holding by hand (or a number of shares
instead of `all`).

### Running on a timetable with `run.py loop`

```
python3 run.py loop                    # propose at each scheduled time, place nothing
python3 run.py loop --execute          # place what passes every check
python3 run.py loop --execute --now    # the same, plus one run straight away
```

`loop` runs at each time in `config.json` `schedule.utc_times`, in UTC whatever your
computer's time zone, on weekdays only while `schedule.weekdays_only` is true. As shipped
that is 13 runs a weekday: 08:17 to 12:17 for the London session and 13:47 to 20:47 for New
York's. Extra runs are safe: a run outside a market's hours places nothing there, the
120-hour cooldown blocks a second order in the same instrument, and the ledger blocks a
repeat on the same day.

It prints the next run time and waits. A run that fails is reported and the loop carries on.
It reads the schedule again after every run, so a schedule edit needs no restart; a code
change does. Ctrl+C stops it.

The computer has to be on and awake. If it sleeps through a run, that run happens once when
it wakes.

To keep the loop going after you close the terminal:

- Linux and macOS:
  `nohup python3 -u run.py loop --execute >> ~/halal-momentum-bot.log 2>&1 &`
  (stop it with `pkill -f "run.py loop"`).
- Windows: leave the Command Prompt window open, or put a shortcut to a `.cmd` file that runs
  `py "%USERPROFILE%\halal-momentum-bot\run.py" loop --execute` in your Startup folder (press
  Win+R and type `shell:startup`).
- Linux with systemd: see [A systemd service](#linux-a-systemd-service-for-the-loop) below.

### Command reference

| Command | What it does |
| --- | --- |
| `python3 run.py once [--execute]` | One run, then exit. Same as `python3 trade.py [--execute]`. |
| `python3 run.py loop [--execute] [--now]` | A run at each scheduled time, until stopped. |
| `python3 run.py selftest [MODULE ...]` | Every module's selftest, or only those named, e.g. `gates trade brokers/paper`. |
| `python3 run.py doctor` | Environment and config checks, no network. |

`once`, `loop` and `doctor` take `--config PATH` to use another config file. `once` and
`loop` take `--env demo` or `--env live`, which overrides `execution.environment` for Trading
212 and means nothing to the paper account.

### Where the state lives

Everything is kept in the project folder, the one holding `run.py`, whichever folder you
start it from and whichever `--config` you pass.

| File | What it is |
| --- | --- |
| `orders_placed.json`, `orders_placed.demo.json`, `orders_placed.paper.json` | The order ledger, one per account: Trading 212 live, Trading 212 practice, and the paper account (any other adapter gets `orders_placed.<adapter>.json`). Written before every order, because Trading 212's API is not idempotent: it is what stops a timed-out order being sent twice, and what the daily spending cap counts. Separate files mean a morning on paper never uses up a real account's cap. Entries older than 90 days are dropped. A ledger that exists but cannot be read stops every order until it is restored. Never edit or delete it to make something pass. |
| `momentum_targets.json` | This month's picks and targets (`strategy.momentum.file`), built on the first run of each month and stamped with the adapter that built them. A run on a different adapter builds the month again from that broker's own tickers, so switching from paper to Trading 212 needs no files moved. |
| `paper_account.json` | The paper account's cash, holdings and orders (`broker.paper.state_file`). Not read while `broker.adapter` is anything else. |
| `HALT` | The killswitch (`risk.killswitch_file`). If it exists, nothing is placed. |
| `.env` | Trading 212 credentials, if you use them. Never commit or share it. |
| `prices.csv` | Optional manual prices, `date,ticker,close`, in the account currency. |

## 2. Scheduled by your operating system

Instead of `run.py loop`, you can let the operating system start one run at each time:
`run.py once --execute`. `run.py loop` is the simpler choice, since it needs no scheduler and
handles UTC itself; use a scheduler when you want runs to start without a terminal open.

Schedulers do not read your shell profile, so put Trading 212 credentials in the `.env` file
in the project folder (see [TRADING212.md](TRADING212.md#2-put-the-key-where-the-bot-can-read-it))
rather than in exported variables. Replace `/home/you`, `/Users/you` and the Python path in
the examples with your own: `command -v python3` (Linux, macOS) or `where py` (Windows)
prints it.

### Linux and macOS: cron

Open your crontab with `crontab -e` and add the two lines below. They are the times in
`config.json` `schedule.utc_times`, weekdays only, and they are correct when the computer's
clock is on UTC (`date` prints `UTC`; most cloud servers are):

```
17 8-12  * * 1-5  flock -n /tmp/halal-momentum-bot.lock /usr/bin/python3 /home/you/halal-momentum-bot/run.py once --execute >> /home/you/halal-momentum-bot.log 2>&1
47 13-20 * * 1-5  flock -n /tmp/halal-momentum-bot.lock /usr/bin/python3 /home/you/halal-momentum-bot/run.py once --execute >> /home/you/halal-momentum-bot.log 2>&1
```

`flock -n` skips a run while the previous one is still going, so two runs never overlap. It
comes with Linux (util-linux) but not with macOS: on a Mac, leave out
`flock -n /tmp/halal-momentum-bot.lock`, or better, use launchd below.

If the computer's clock is not on UTC, cron reads these times in local time. Either:

- add `CRON_TZ=UTC` on a line above them, if your cron supports it (cronie does, as used on
  Fedora, RHEL and Arch; check `man 5 crontab`), or
- run every hour on weekdays, which works in any time zone because a run outside market hours
  places nothing:

  ```
  17 * * * 1-5  flock -n /tmp/halal-momentum-bot.lock /usr/bin/python3 /home/you/halal-momentum-bot/run.py once --execute >> /home/you/halal-momentum-bot.log 2>&1
  ```

cron does not catch up on runs missed while the computer was off.

### Linux: a systemd service for the loop

To keep `run.py loop` running in the background and start it at boot, create
`~/.config/systemd/user/halal-momentum-bot.service`:

```
[Unit]
Description=halal-momentum-bot loop

[Service]
ExecStart=/usr/bin/python3 %h/halal-momentum-bot/run.py loop --execute
Environment=PYTHONUNBUFFERED=1
Restart=on-failure

[Install]
WantedBy=default.target
```

Then:

```
systemctl --user daemon-reload
systemctl --user enable --now halal-momentum-bot
journalctl --user -u halal-momentum-bot -f       # watch it
systemctl --user stop halal-momentum-bot         # stop it
loginctl enable-linger "$USER"                   # keep it running when you log out
```

### macOS: launchd

Save this as `~/Library/LaunchAgents/local.halal-momentum-bot.plist`, with your own user
name and Python path in place of `/Users/you` and `/usr/bin/python3`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>local.halal-momentum-bot</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>if [ "$(date -u +%u)" -le 5 ]; then exec /usr/bin/python3 /Users/you/halal-momentum-bot/run.py once --execute; fi</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Minute</key>
    <integer>17</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/Users/you/halal-momentum-bot.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/you/halal-momentum-bot.log</string>
</dict>
</plist>
```

It runs at 17 minutes past every hour and skips Saturday and Sunday (in UTC). Hourly works
in any time zone, because a run outside market hours places nothing. launchd does not start a
second copy while one is running, and a run missed while the Mac slept happens once when it
wakes.

```
plutil -lint ~/Library/LaunchAgents/local.halal-momentum-bot.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.halal-momentum-bot.plist
launchctl kickstart gui/$(id -u)/local.halal-momentum-bot     # one run now, to test
launchctl bootout gui/$(id -u)/local.halal-momentum-bot       # stop and unload it
```

Keep the project folder outside Documents, Desktop and Downloads (your home folder is fine):
macOS blocks background jobs from those folders unless you grant access.

### Windows: Task Scheduler

1. Create `run-bot.cmd` in your user folder (`%USERPROFILE%`, outside the project folder)
   with Notepad:

   ```
   @echo off
   py "%USERPROFILE%\halal-momentum-bot\run.py" once --execute >> "%USERPROFILE%\halal-momentum-bot.log" 2>&1
   ```

2. In Command Prompt, create a task that runs it at 17 minutes past every hour, Monday to
   Friday:

   ```
   schtasks /Create /TN "halal-momentum-bot" /TR "\"%USERPROFILE%\run-bot.cmd\"" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 00:17 /RI 60 /DU 23:59
   ```

   The `\"` quotes inside `/TR` keep a user folder with a space in its name, such as
   `C:\Users\Jane Doe`, in one piece; without them every run of the task fails.

Times and weekdays here are the computer's local time. Hourly works in any time zone,
because a run outside market hours places nothing. Task Scheduler does not start a second
copy while one is running. As created, the task runs only while you are logged on, and the
computer must be awake.

To run it once now as a test, see when it last ran and with what result, pause it, and
remove it:

```
schtasks /Run /TN "halal-momentum-bot"
schtasks /Query /TN "halal-momentum-bot" /V /FO LIST
schtasks /Change /TN "halal-momentum-bot" /DISABLE
schtasks /Delete /TN "halal-momentum-bot" /F
```

## 3. Docker

The same code and commands in a container, kept running and restarted after a crash or a
reboot. Install Docker Desktop (Windows, macOS) or Docker Engine with the Compose plugin
(Linux). The compose file's optional `.env` needs Docker Compose 2.24 or newer
(`docker compose version`).

From the project folder:

```
docker compose run --rm bot doctor        # checks, as on any computer
docker compose run --rm bot selftest
docker compose up -d --build              # start the loop in the background
docker compose logs -f                    # watch it (Ctrl+C stops watching, not the bot)
docker compose down                       # stop it
```

The image's entry point is `python3 run.py`, so `docker compose run --rm bot <command>` runs
any `run.py` command once, such as `once` or `once --execute`. For the paper account's own
commands, change the entry point:

```
docker compose run --rm --entrypoint python3 bot brokers/paper.py show
docker compose run --rm --entrypoint python3 bot brokers/paper.py buy MWIXl_EQ 2000
```

As shipped, the compose file runs `loop` without `--execute`, so it only proposes. To place
orders that pass every check, edit `docker-compose.yml`:

```
    command: ["loop", "--execute"]
```

then `docker compose up -d`. With the paper adapter those orders are simulated.

**The folder.** The project folder is mounted at `/app` in the container. The ledger, the
momentum targets and the paper account are written to your folder and survive the
container, edits to `config.json` take effect at the next run, and a `HALT` file you create
in the folder stops all orders. The code is read from your folder too, so after you update
it, restart with `docker compose restart`.

**Credentials.** For Trading 212, copy `.env.example` to `.env` and fill in both values. The
compose file passes it to the container. `.env` is left out of the image and out of git.

**Linux file ownership.** The container runs as user id 1000. If `id -u` prints something
else, add `user: "<your id>:<your group id>"` under `bot:` in `docker-compose.yml`, or the bot
cannot write its ledger (`docker compose run --rm bot doctor` says so).

Without Compose:

```
docker build -t halal-momentum-bot .
docker run -d --name halal-momentum-bot --restart unless-stopped -v "$(pwd)":/app halal-momentum-bot loop --execute
docker logs -f halal-momentum-bot
docker stop halal-momentum-bot
```

The mount (`-v`) matters: without it the ledger and the paper account live inside the
container and are lost with it. With the folder mounted, the bot reads `.env` from it. In
PowerShell, write `${PWD}` in place of `$(pwd)`.

The container's clock is UTC, which is what the schedule uses.

## 4. GitHub Actions

The workflow `.github/workflows/trading.yml`, named **Trading control panel**, runs the bot
on GitHub's machines: on a schedule, and by hand from the Actions tab in a browser.

### Make your own PRIVATE copy

Every run writes your cash, holdings and orders to the run's summary page, sometimes to an
issue, and commits the order ledger and the paper account back to the repository. In a public
repository all of that is public. The workflow checks this itself: when `broker.adapter` is
anything but `"paper"`, its first step asks GitHub whether the repository is private and, if
it is not (or the answer cannot be read), stops the run before anything else happens and
says why on the summary page and in an issue. The paper account, which is simulated, may run
anywhere. So:

- **Do not fork.** A fork of a public repository is public, and cannot be made private.
- If GitHub shows a **Use this template** button on the project page, use it and choose
  **Private**.
- Otherwise, create an empty repository at [github.com/new](https://github.com/new): choose
  **Private**, and add no README, licence or `.gitignore`. Then copy the project into it:

  ```
  git clone https://github.com/abyyworld/halal-momentum-bot.git my-bot
  cd my-bot
  git remote rename origin upstream
  git remote add origin https://github.com/YOUR-NAME/my-bot.git
  git push -u origin main
  ```

  Later, to bring in updates: `git pull upstream main`, then `git push`.

### Set it up, once

In your private repository on github.com:

1. **Settings, Actions, General, Workflow permissions: Read and write permissions**, then
   Save. The workflow commits the ledger back after every run and opens issues; without
   write access the ledger cannot be saved and the next run cannot see this run's orders.
2. **Trading 212 only** (`broker.adapter` `"trading212"`): Settings, Secrets and variables,
   Actions, **New repository secret**, twice:
   - `T212_API_KEY`: your key
   - `T212_API_SECRET`: your secret

   The paper account needs neither.
3. Edit `config.json` for your account (see [TRADING212.md](TRADING212.md)), commit and push.
   The workflow reads `config.json` from the repository, not from your computer.
4. **To switch the schedule on**, only once manual runs look right: Settings, Secrets and
   variables, Actions, Variables tab, **New repository variable**:
   - `BOT_ENABLED`: `true`

   Until it is exactly `true`, every scheduled run is skipped before it starts, so a fresh
   copy never trades on its own. Manual runs work either way.

### Run a command by hand

Actions tab, **Trading control panel**, **Run workflow**. Leave "Use workflow from" on
`main`: a run uses the config and the ledger of the branch it runs on. Pick a command, fill
in the boxes it needs, and press the green button. The answer is on the run's summary page
when it finishes; nothing is emailed.

| Command | What it runs |
| --- | --- |
| `doctor` | `python3 run.py doctor`: Python, config, broker and credentials, no network. |
| `check` | `python3 broker.py check`: probe each read endpoint, report which work. |
| `portfolio` | `python3 broker.py summary` and `positions`: cash and holdings. |
| `limits` | `python3 gates.py explain` and `rules.py explain`: allowlist, risk limits, strategy. |
| `instruments` | `python3 broker.py instruments "<search>"`: find exact Trading 212 tickers. Names in the search box, separated by `\|`. |
| `exchanges` | `python3 broker.py exchanges`: exchange working schedules. |
| `history` | `python3 broker.py history`: recent order history (first 60 lines). |
| `prices` | `python3 pricefeed.py show <search>`: how far each ticker in the search box (space separated, e.g. `AAPL_US_EQ MSFT_US_EQ`) is below its 52-week high. With the box empty it uses the US shares on `compliance.allowlist`; the shipped allowlist has none, so it only prints a hint. |
| `paper` | `python3 brokers/paper.py <search>`: the paper account. Search box: `show`, `buy TICKER AMOUNT`, `sell TICKER N` or `sell TICKER all`, or `reset`. Empty means `show`. |
| `backtest` | `python3 backtest.py run <search>`, or `python3 backtest.py <search>` when the search starts with `gaps` or `probe`: replay history, read-only and slow. Its absolute returns are inflated; `research.py` is the trustworthy source. |
| `propose` | `python3 run.py once`: what the bot WOULD do. Places nothing. |
| `execute` | `python3 run.py once --execute`: the same, and places what passes every check. |

The other boxes:

- **search**: the argument for `instruments`, `prices`, `paper` and `backtest`.
- **environment**: Trading 212 only. `config` uses `execution.environment` from
  `config.json`; `demo` or `live` overrides it for this run.
- **confirm**: for `execute` on an account that may be real money, type exactly
  `PLACE ORDERS`. Anything other than the paper account or Trading 212's demo counts as
  possibly real money; without the words the run stops before it starts. This guards
  against a mis-click on the dropdown. Scheduled runs do not ask: they were switched on
  deliberately with `BOT_ENABLED`, and they pass the same checks.
- **notify**: tick it to get an issue with the result even when nothing happened.

A first session on the paper account: `doctor`, then `paper` with `buy MWIXl_EQ 2000` in the
search box, then `propose`, then `execute`. On Trading 212: `doctor`, `check`,
`instruments`, `portfolio`, `limits`, `propose`, and only then `execute`.

### What every run does

1. Checks out the branch's latest commit when the job starts, so a run that waited behind
   another sees the ledger that run committed. Only one run happens at a time and a run in
   progress is never cancelled; a run that arrives meanwhile waits for it (GitHub keeps only
   the newest waiting run).
2. Runs the selftests of every module a run uses. If any fails, the command does not run and
   an issue is opened.
3. Runs the command.
4. Commits the order ledgers (`orders_placed*.json`: `orders_placed.json` for Trading 212
   live, `orders_placed.demo.json` for practice, `orders_placed.paper.json` for paper),
   `momentum_targets.json` and `paper_account.json` back to the branch, when they changed,
   as "Order ledger and account state" by `github-actions[bot]`.
   GitHub throws the runner's disk away after every job, so these files are the bot's only
   memory between runs. If the push fails three times, the run fails and the issue says so:
   until the ledger is pushed, the next run cannot see this run's orders, so commit `HALT` if
   in doubt. A branch protection rule that blocks pushes from Actions causes this.
5. Writes the output to the run's summary page. This notifies nobody.
6. Opens an issue, assigned to the repository owner, only when a person is needed: an order
   reached the broker, a command failed, the ledger could not be pushed, or you ticked
   notify. A failure opens ONE issue labelled `bot-failure`; later failures stay on the run
   summary while it is open, and closing it re-arms the alarm. Placed orders always open an
   issue.

Because the bot commits to your repository, run `git pull` before you edit files on your own
computer.

### The schedule

The workflow's cron lines, `17 8-12 * * 1-5` and `47 13-20 * * 1-5`, are the same 13 UTC
times as `config.json` `schedule.utc_times`; change both together. A scheduled run is
`execute`, on the account and environment `config.json` names, with no person in the loop.
Scheduled runs use the workflow file and config of the default branch.

**GitHub drops and delays scheduled runs**, sometimes by hours, and many never happen at all
when its machines are busy. That is why there are 13 a day, and why a run that does land
spends a sale's cash in the same run (the second pass). In the research backtest, filling
orders 1, 3 or 5 trading days after the signal made no measurable difference (16.2%, 16.6%
and 17.5% a year, all flattered by the data), so a late run costs little. What matters is
that some run lands in the 7 days after each monthly build, or that month's trades are
skipped. Check the Actions tab now and then.

Runs in a private repository use your account's monthly allowance of Actions minutes; your
account's billing settings on github.com show how much is left.

### The other workflows

- **CI** (`ci.yml`) runs every selftest and the doctor with Python 3.9 and 3.13 on every push
  and pull request. In your private copy that is on Linux only: GitHub bills a private
  repository's Windows minutes at twice the Linux rate and its macOS minutes at ten times,
  from the same allowance the scheduled trading runs need, and once it is used up those runs
  stop until the month ends. A run started by hand (Actions tab, CI, Run workflow), or any
  run in a public repository, covers Linux, Windows and macOS. No secrets, nothing placed.
- **Research (read-only)** (`research.yml`) runs each line of `research/request.txt` as
  `python3 <line>` when you push a change to that file on a branch other than `main`, or from
  the Actions tab. No secrets, read-only, its own queue.

## 5. Stopping it

**Stop all orders, everywhere:** create an empty file named `HALT` in the project folder.
Every run checks for it before placing anything and reports that it is halted; runs still
read the account and propose. Delete the file to resume.

```
touch HALT          # Linux, macOS
type nul > HALT     # Windows Command Prompt
```

On GitHub, commit it from the browser: Code tab, Add file, Create new file, name it `HALT`,
Commit changes. The next run sees it; a run already going does not.

`HALT` does not cancel an order already sent. Market orders usually fill at once; anything
still pending at Trading 212 can be cancelled in the app.

**Stop the runs themselves:**

| Where | How |
| --- | --- |
| `run.py loop` in a terminal | Ctrl+C. An order cut off mid-run is safe: the ledger is written first. |
| cron | `crontab -e` and delete or comment out the lines. |
| systemd | `systemctl --user disable --now halal-momentum-bot` |
| launchd | `launchctl bootout gui/$(id -u)/local.halal-momentum-bot` |
| Task Scheduler | `schtasks /Change /TN "halal-momentum-bot" /DISABLE` |
| Docker | `docker compose down`, or `docker stop halal-momentum-bot` |
| GitHub schedule | Delete the `BOT_ENABLED` variable (manual runs still work), or Actions tab, Trading control panel, the `...` menu, **Disable workflow**. |

## 6. Troubleshooting

### Messages from `run.py doctor`

| Message | What to do |
| --- | --- |
| `Python 3.x is too old: 3.9 or newer is needed` | Install a newer Python (section 1). On Windows, check `py --version`. |
| `no time zone data (...)` | Windows: `py -m pip install -r requirements.txt`. Linux: install the `tzdata` package. The market-hours check needs it for New York's and London's hours. |
| `config.json starts with a byte order mark: save it as UTF-8 without one` | An editor saved it with a byte order mark. Save it again as "UTF-8", not "UTF-8 with BOM". |
| `cannot read .../config.json: Expecting ...` | The JSON has a syntax error at the line and column given: usually a missing or extra comma, or a missing quote. |
| `cannot read ...: No such file or directory` | The `--config` path is wrong, or `config.json` is missing from the project folder. |
| `config.json has no ... section` | One of `broker`, `compliance`, `risk`, `execution` or `strategy` is missing. Restore it from the original `config.json`. |
| `the bot's modules do not import: ...` | A file is missing or damaged. Download the project again. |
| `broker adapter X does not load: ... does not exist` | `broker.adapter` names a file that is not in `brokers/`. Use `paper` or `trading212`, or write the adapter from `brokers/template.py` (see [BROKERS.md](BROKERS.md)). |
| `T212_API_KEY is not set: ...` (or `T212_API_SECRET`) | Put both values in `.env` in the project folder (copy `.env.example`), or export them. See [TRADING212.md](TRADING212.md). |
| `warn ... is wrapped in quotes, so they became part of the value` | The value set in the environment (an exported variable, or the GitHub secret) includes its quotes: set it again with the value only. In `.env` one pair of surrounding quotes is ignored, so this comes from the environment unless the `.env` value has two pairs. |
| `warn ... has leading or trailing whitespace, usually a stray newline` | Paste the value again, with nothing after it. |
| `warn ... contains a space, so it was truncated or joined with something` | Paste the whole value again, on one line. |
| `warn ... looks like a whole shell line rather than just the value` | The value itself contains `=` or starts with `export`. In `.env`, write the name, one `=`, then the value only: `T212_API_KEY=yourkey`. |
| `warn HALT is present: no run places anything until it is deleted` | Delete `HALT` when you want orders again. |
| `run.py loop cannot run: schedule.utc_times has ...` | Each time must be `HH:MM`, 24-hour, UTC, e.g. `"08:17"`. |
| `the project folder is not writable ...` | The ledger and the paper account are kept there. Fix the folder's permissions; in Docker on Linux, see the user id note in section 3. |
| `warn order ledger ... cannot be read: ...` | The configured account's ledger was cut off (a crash, a full disk, a power cut) or damaged. No run places an order until it reads again, because read as empty it would forget today's orders. Restore it from git (`git checkout -- <file>`) or a backup. Never edit it to make something pass. |
| `note account: Trading 212 LIVE, REAL MONEY` | Not an error: a warning that this config reaches real money. |
| `note execute is false in config.json: every run only proposes, even with --execute` | Set `execution.execute` to `true` when you want orders. |
| `note momentum book in shadow mode ...` | `strategy.momentum.mode` is `"shadow"`: its trades are worked out and printed, never sent. Set it to `"live"` to send them. |

### Messages from a run

| Message | Meaning |
| --- | --- |
| `PROPOSE ONLY, nothing was placed:` `--execute was not passed` | Add `--execute`. |
| `... the HALT file exists` | Delete `HALT` to resume. |
| `... today's order count could not be read, so the daily cap cannot be enforced` | The order history could not be read. The run refuses to place anything rather than assume zero orders. Usually temporary. |
| `... the account could not be fully read (...)` | Cash or holdings could not be read, or the holdings list came back empty while the account says it holds something. Nothing is placed. Usually temporary. If it says the broker reports the account in another currency than `account.currency`, set `account.currency` to the one it names. |
| `... the order ledger ... could not be read (...)` | The account's ledger file exists but is damaged. Nothing is placed until it is restored from git or a backup (see the doctor's message above). |
| `momentum no targets this month: ...` | The month's picks could not be built: usually no internet access, or Yahoo, Wikipedia or the SPUS file did not answer. The next run tries again; nothing is bought without targets. |
| `UNSIZED ... no price available ...: not held, and no usable row in prices.csv` | The bot cannot price something you do not hold. For the core fund, make the one hand purchase ([TRADING212.md](TRADING212.md#7-the-one-purchase-you-make-by-hand)). |
| `REJECTED ...` | A check in `gates.py` refused the proposal; the reason follows. `python3 gates.py explain` prints the limits. |
| `HELD ...: closed ...` or `weekend ...` | Its market is shut; a later run places it. |
| `HELD ...: no published schedule for working schedule id ...` | The broker publishes no hours for that instrument's exchange and `execution.market_hours_fallback` has none, so it is never traded: the safe failure. On the paper account this happens to a `broker.paper.symbols` entry whose `venue` is not `"LSE"`. |
| `FAILED ...` | The broker refused the order; its answer follows. The ledger records it, so it is not retried that day. |
| `CERTIFICATE_VERIFY_FAILED` | macOS with python.org's Python: run `Install Certificates.command` (section 1). |
| `401` from `python3 broker.py check` | The key and secret do not match the environment or the account type. See [TRADING212.md](TRADING212.md). |
| `403` from `python3 broker.py check` | The key works, but that permission was not ticked when it was made. Generate a new key with it. |
