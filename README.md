# Polymarket tools

A reward farmer for Polymarket, the web console that controls it, and a keyed
worker that runs two order tools from that console.

- `reward_screener.py` ranks the markets that pay liquidity rewards and stores
  them in `farm.db` for you to approve or reject.
- `reward_farmer.py` rests post-only orders on the approved markets to earn
  those rewards. It is a dry run unless you pass `--live`.
- `python -m farm_console` serves a local web console. It shows what the
  farmer does, and it is where you run scans, approve markets, and control
  the farmer. It holds no wallet key.
- `python -m tools` runs the worker. It holds the wallet key and does what the
  console's **Orders** and **Sniper** pages ask for. It takes snapshots of
  your open orders, places the ones that went missing, and fires tick-change
  watches.

A live farmer, a restore, and a live watch place real orders with real money.
Read [Safety](#safety) before you start anything that holds a key.

Contents:

- [How the pieces fit](#how-the-pieces-fit)
- [Install](#install)
- [Set up the environment](#set-up-the-environment)
- [Run the processes](#run-the-processes)
- [Safety](#safety)
- [Reward screener](#reward-screener)
- [Reward farmer](#reward-farmer)
- [Farm console](#farm-console)
- [Orders page](#orders-page)
- [Sniper page](#sniper-page)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)
- [References](#references)

## How the pieces fit

### Processes

Four programs run as separate processes. The browser talks to the console
over HTTP. The processes talk to each other only through rows in two SQLite
files.

```text
browser --HTTP--> console (no key) --rows--> tools.db <--rows-- worker (key)
                     |                                            |- job thread: snapshot, preview, restore
                     |                                            +- sniper threads: control, feed, poll, slow
                     |
                     +--rows--> farm.db <--rows-- farmer (key only with --live)
                                   ^
                                   +--rows-- screener (no key)
```

| Process | Command | Wallet key | What it reads and writes |
| --- | --- | --- | --- |
| Screener | `python reward_screener.py` | Never | Reads Polymarket's public APIs. Writes its scan to `farm.db`. |
| Farmer | `python reward_farmer.py` | Only with `--live` | Reads your decisions, settings, and commands from `farm.db` every cycle and writes what it does there. Records books to `farm_books.db`. With `--live` it posts and cancels orders. |
| Console | `python -m farm_console` | Never | Serves HTTP on `127.0.0.1`. Reads and writes `farm.db` and `tools.db`. Reads the public APIs for a scan, the Review page, and the Sniper search. |
| Worker | `python -m tools` | Yes, unless `--no-key` | Reads the jobs and the watches the console wrote to `tools.db` and writes the results there. Posts the orders of a restore and of a watch. |

The console steers the farmer and the worker only by writing rows. The farmer
reads `farm.db` again every cycle, and the worker reads `tools.db` again at
least every second, so neither needs the console to keep running.

The worker is a process of its own for three reasons:

- A console restart does not stop an armed watch or a restore in progress.
- The wallet key stays out of the process that parses HTTP.
  `test_farm_console_orders.py` and `test_farm_console_sniper.py` fail if
  importing the console loads `polymarket.keys`, `polymarket.client`, or the
  worker.
- The sniper does not share Python's interpreter lock with scans and the
  console's event stream.

The worker never opens `farm.db`, and the farmer never opens `tools.db`, so
neither waits on the other's write lock. When a restore needs to know which
markets the farmer manages, the console reads them from `farm.db` at your
click and puts them in the job.

Both databases run in WAL mode with a 5-second busy timeout, and every column
has one writer process. `farm_db.py` owns the schema of `farm.db` and every
statement run against it. `tools/db.py` does the same for `tools.db`.

### Files

The farmer, the screener, and their analysis modules are single files at the
repository root. The code that talks to Polymarket, the worker, and the
console are packages.

```text
reward_screener.py   the screener: scan, list, approve, reject
reward_farmer.py     the farmer
farm_db.py           farm.db: the schema and every statement
farm_books.py        farm_books.db: the favorite books the farmer records
fill_cost.py         how often a favorite bid fills, and what a fill costs
exit_value.py        what a favorite exit is worth
fill_backtest.py     backtest of the fill forecast
exit_backtest.py     backtest of the exit model
triage.py            a screen of candidate markets that runs the claude CLI
triage_eval.py       the evaluation of that screen
scoring_probe.py     read-only: how long an order rests before it scores

polymarket/          talks to Polymarket and holds no strategy
  hosts.py           where the services live
  http.py            the rate-limit retry, chunked requests, the order connection
  parse.py           numbers and times as the APIs send them
  books.py           order books from the CLOB
  gamma.py           markets by condition id, and the market search
  feed.py            the market websocket channel
  region.py          whether Polymarket takes new orders from this address
  orders.py          open orders, and the one way to post an order
  ticks.py           signing an order at a market's tick
  client.py          an authenticated CLOB client
  keys.py            the wallet key, from the environment or Bitwarden

tools/               the keyed worker
  __main__.py        the entry point: python -m tools
  worker.py          the loop: the liveness record, the jobs, the sniper
  db.py              tools.db: the schema and every statement
  snapshot.py        take a snapshot, import the old JSON files
  restore.py         plan a restore, place it
  sniper.py          fire each armed watch once
  picker.py          rank a market search for the Sniper page

farm_console/        the web console
  __main__.py        the entry point: python -m farm_console
  app.py             wires the pages together
  context.py         what every page shares
  security.py        the request guard
  pages/             one module for each page
  templates/         the pages' HTML
  static/            CSS, htmx, and the console's scripts

test_*.py            the tests, at the repository root
run_tests.py         runs every test module
pyproject.toml       the dependencies and the pytest settings
.env.example         the settings each process reads at start
```

`git ls-files` lists every tracked file. Git ignores the data: `.env`,
`farm.db`, `farm_books.db`, `tools.db`, and the old snapshot files in
`snapshots/`.

## Install

You need Python 3.11 or later.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

The code runs from the checkout, and the install brings only the
dependencies. Run every command in this file from the repository root, so
that Python finds the `polymarket`, `tools`, and `farm_console` packages.

Two outside programs are optional. The Bitwarden CLI (`bw`) is needed only
when the wallet key comes from a Bitwarden vault. The `claude` CLI is needed
only for the screen in `triage.py`.

## Set up the environment

1. Copy the example file.

   ```bash
   cp .env.example .env
   ```

2. Set `POLYMARKET_FUNDER_ADDRESS` in `.env` to the address that holds your
   funds and positions.
3. Decide where the wallet key comes from. See
   [Where the wallet key comes from](#where-the-wallet-key-comes-from).

The farmer, the console, the worker, and the scoring probe each load `.env`
from the repository root when they start. A variable already set in the real
environment wins over its line in `.env`. The screener reads no `.env`.

| Variable | Needed | Meaning |
| --- | --- | --- |
| `POLYMARKET_FUNDER_ADDRESS` | Always | The address that holds the funds and the positions. The farmer reads your positions for it, and the console shows your holdings on the Review page. |
| `POLYMARKET_SIGNATURE_TYPE` | No | How orders are signed. The default is `1`, a Polymarket proxy wallet. `0` is an EOA wallet. |
| `POLYMARKET_CHAIN_ID` | No | The chain id. The default is `137`, Polygon. |
| `POLY_BUILDER_CODE` | No | A builder code to attach to every order. |
| `POLYMARKET_KEY_SOURCE` | No | `env` or `bitwarden` reads the key from that source alone. |
| `POLYMARKET_PRIVATE_KEY` | No | The wallet's private key, when you keep it in the environment. |
| `BW_POLYMARKET_ITEM_ID` | No | The id of the Bitwarden item whose note is the key. |
| `BW_POLYMARKET_ITEM_NAME` | No | A name to search the vault for, when you set no id. The first item found is used. |
| `POLYMARKET_DATA_DIR` | No | The directory where the farmer, the console, and the worker keep their databases. See [Where the databases live](#where-the-databases-live). |

An EOA wallet (`POLYMARKET_SIGNATURE_TYPE=0`) must approve Polymarket's
exchange contracts to spend its collateral and its conditional tokens before
it can trade. The installed SDK lists the contract addresses in
the `py_clob_client_v2.config` module.

### Where the wallet key comes from

Three programs read the key: a farmer started with `--live`, the worker
unless you start it with `--no-key`, and `scoring_probe.py`. A dry-run farmer,
the screener, and the console read no key.

Each of the three reads `POLYMARKET_PRIVATE_KEY` when it is set. Otherwise it
reads the note of a Bitwarden item. `POLYMARKET_KEY_SOURCE=env` or
`POLYMARKET_KEY_SOURCE=bitwarden` reads that source alone.

To keep the key in Bitwarden:

1. Install the [Bitwarden CLI](https://bitwarden.com/help/cli/).
2. Run `bw login` once, by hand. The programs have no login step, and a CLI
   that is not logged in stops the start with an error that says so.
3. Create a Secure Note item in the vault and put the private key in its
   notes field. An item of any other type is refused.
4. Set `BW_POLYMARKET_ITEM_ID` or `BW_POLYMARKET_ITEM_NAME` in `.env`.

When the vault is locked, the program asks for `Bitwarden master password:`
in its terminal, unlocks the vault, reads the note, and locks the vault again
before it does anything else. If the vault does not lock, the program throws
the key away and does not start. A vault you had unlocked yourself, with your
own `BW_SESSION`, is read and left unlocked. The master password and the
session token go only to the `bw` child process. The program never writes
either one to its own environment.

On a machine with no vault, `--key-stdin` makes the farmer or the worker read
the key from one line of standard input and skip both sources:

```bash
python -m tools --key-stdin
python reward_farmer.py --live --key-stdin
```

That lets you unlock the vault on your own machine and send only the key to
a server over SSH. Nobody has run the next command against a server yet, so
treat it as a sketch:

```bash
bw get notes <item-id> | ssh user@203.0.113.10 'cd polymarket-tools && python -m tools --key-stdin'
```

### Host overrides

`POLYMARKET_HOST`, `POLYMARKET_GAMMA_HOST`, `POLYMARKET_DATA_API_HOST`, and
`POLYMARKET_WSS_HOST` replace the default hosts in `polymarket/hosts.py`.
Set them in the real environment, not in `.env`. Every program reads the
hosts before it loads `.env`, so a host that only `.env` names changes
nothing.

### Where the databases live

The farmer, the console, and the worker look for `farm.db` and `tools.db` in
one data directory. It is the directory `POLYMARKET_DATA_DIR` names, and the
repository root when the variable is not set. The three processes therefore
agree wherever you start each one.

- You can set `POLYMARKET_DATA_DIR` in `.env` or in the real environment. A
  relative value is taken from the repository root.
- The directory must exist. No program creates it, because a mistyped
  directory would start every process on empty databases. A program that
  cannot find it prints `Not started: POLYMARKET_DATA_DIR is not a directory`
  with the path, and exits.
- `--db` on the farmer and the worker, and `--db` and `--tools-db` on the
  console, each name one file outright. A relative path there is read from
  the current directory.
- The farmer writes `farm_books.db` in the directory of its `farm.db`.
- The screener is the exception. It reads neither `.env` nor
  `POLYMARKET_DATA_DIR`, and its `--db` defaults to `farm.db` in the current
  directory. When you set a data directory, run the screener with `--db` and
  the path of the `farm.db` in it, or scan from the console's Discover page,
  which writes to the console's `farm.db`.

## Run the processes

A usual session has three terminals, each in the repository root:

```bash
python reward_farmer.py      # the farmer, a dry run
python -m farm_console       # the console, on http://127.0.0.1:8787
python -m tools              # the worker, with the key
```

No process needs another one to be running. The console shows the last state
the farmer and the worker published, with a banner when one of them is down.

With no flags, the three share the `farm.db` and the `tools.db` of the data
directory (see [Where the databases live](#where-the-databases-live)).

### Screener

`python reward_screener.py` runs one scan and exits. It needs no key. See
[Reward screener](#reward-screener). The console's Discover page runs the
same scan.

### Farmer

`python reward_farmer.py` is a dry run that reads live public data and sends
nothing. `python reward_farmer.py --live` signs and posts real orders.

A live start first checks the region this machine connects from and prints
the result, as the worker does. From a place where Polymarket rejects new
orders it prints `Not started: a live farmer would place orders from here.
Start it without --live for a dry run.` and exits with status 1, before it
reads any key. A dry run checks no region.

See [Reward farmer](#reward-farmer), and [Going live](#going-live) before the
first live run.

### Console

```bash
python -m farm_console                          # port 8787
python -m farm_console --port 8788              # another port
python -m farm_console --db scratch.db --tools-db scratch-tools.db
```

It prints `Farm console on http://127.0.0.1:8787 reading <farm.db> and
<tools.db>` and serves until Ctrl+C. See [Farm console](#farm-console).

### Worker

```bash
python -m tools                 # keyed: the environment or Bitwarden
python -m tools --key-stdin     # keyed: the key arrives on standard input
python -m tools --no-key        # no key
python -m tools --db scratch-tools.db
```

A keyed start goes in this order:

1. It checks the region this machine connects from and prints the result, for
   example `Region: IE: new orders permitted (blocked on the website only).`
   From a place where Polymarket rejects new orders it prints
   `Not started: a keyed worker would place orders from here. Start it with
   --no-key to run without one.` and exits with status 1. It has read no key
   at that point.
2. It reads the key and signs in.
3. It prints `Worker on <tools.db>, keyed. Ctrl+C stops it.` and runs.

While it runs, the worker records in `tools.db` every second that it is
alive. The console counts it as running until 10 seconds pass without that
record. This is not the farmer's `--heartbeat`, and Polymarket never sees it.
Every second the worker also starts the oldest queued job, one job at a time,
on a thread of its own. The sniper runs beside the jobs from the start.

Ctrl+C or a terminate signal stops the worker. A snapshot in progress runs to
its end, and a restore ends before its next order. Then the worker prints
`Worker stopped.` A job that a killed worker left running is closed as failed
when the next worker starts, with the error `the worker stopped while this
job was running`.

One worker runs on a `tools.db` at a time. A second one prints `Not started:
a worker is already running on this database (pid <pid>, last beat <time>).`
and changes nothing.

`--no-key` starts a worker that holds no key. Every snapshot, preview, and
restore job fails with `this worker holds no key`, and the sniper adopts only
dry-run watches. Use it to rehearse a watch with no key in the process.

### Scoring probe

`python scoring_probe.py` measures how long an order rests before Polymarket
counts it for rewards. It reads the live key, lists your open orders, and
places and cancels nothing. Its module docstring has the details.

## Safety

- **The console has no login.** It binds `127.0.0.1` only, and anyone who
  can reach its port can pause markets, stop the farmer, place a restore, and
  arm a watch. Never change the bind address. To use the console from another
  machine, forward the port over SSH and open `http://localhost:8787`:

  ```bash
  ssh -L 8787:127.0.0.1:8787 user@203.0.113.10
  ```

  Forward the same port number on both ends. The console answers 400 to any
  request whose `Host` header is not `127.0.0.1:<port>` or `localhost:<port>`
  with its own port. This tunnel follows from that rule, and nobody has run
  the console through one yet. [Security](#security) lists every check.
- **Leave `--heartbeat` off.** If the farmer's heartbeats stop, Polymarket
  cancels open orders, and it is not confirmed whether that means the
  farmer's orders or every order on the account.
  [The heartbeat warning](#the-heartbeat-warning) has the open risk.
- **Two console actions place real orders.** Placing a restore needs the
  typed word `PLACE`, and arming a live watch needs the typed word `ARM`.
  The server checks each word. Both tools post post-only orders, so an order
  rests or is refused and never takes liquidity.
- **A post that gets no answer may have placed an order.** The worker never
  sends such a post again. The page then says that an order may rest, and you
  check your open orders. See
  [When a line says the order may rest](#when-a-line-says-the-order-may-rest)
  and [How a watch ends](#how-a-watch-ends).
- **Polymarket blocks new orders by the address that sends them.** A keyed
  worker and a live farmer each check their region before they read a key,
  and neither starts from a place where new orders are rejected. The check
  runs once, at the start. It does not notice a VPN that drops later.
- **Keep `.env` out of Git.** `.gitignore` already lists it.

## Reward screener

`reward_screener.py` ranks the markets that pay Polymarket liquidity rewards
for one farming strategy, and stores them in `farm.db` (SQLite) for you to
approve or reject. It is read-only. It places, cancels, and signs nothing, and
it needs no credentials.

The strategy targets markets whose favorite outcome trades at 90c or more. You
rest a favorite bid just under the midpoint, priced where its rewards minus
the cost of its fills are highest (see [The fill-cost model](#the-fill-cost-model)).
You also rest a longshot bid far below
the longshot midpoint. Above a 0.90 midpoint Polymarket only pays for
two-sided liquidity, so the longshot bid exists to make the favorite bid
score.

A naked longshot bid at price q is a favorite ask at 1 - q. Whether it fills
depends on the annual return that ask gives a favorite buyer who holds to the
end date, `(1 / (1 - q)) ^ (1 / years left) - 1`, not on its price alone. Of
about 20,000 favorite buys on 250 markets, 0.4% accepted under 2% a year,
2.8% under 5%, 23% under 10%, 74% under 20% and 90% under 30%. That return
puts each longshot price in one of three zones:

- Free, under 5% a year (`FREE_RETURN` in `reward_screener.py`): 0.3c on a
  market 25 days from its end date, 1.1c on one 83 days out. The naked budget
  does not limit a bid there. Where a share costs at most $0.50 for each unit
  of its score (`FREE_MAX_COST`), the bid is sized to balance the favorite
  bid whatever that costs. At 0.1c with a good score that is cents. Near the
  bound it is up to half the favorite bid's score times its size, in
  dollars: about $100 beside a $1,000 favorite bid, and several hundred
  where the favorite bid sits close to the midpoint.
- Budgeted, from 5% to 20% a year: the bid's capital is naked risk, which you
  cap per market when you approve it, and the favorite bid shrinks to what
  the budget balances.
- None, over 20% a year (`MAX_RETURN`, a test value): no longshot bid rests
  there, and so no favorite bid it would balance.

Returns rise as the end date nears, so a price moves from one zone to the
next without the book changing. A scan labels each plan by its longshot bid:
mode `floor` in the free zone and `budget` above it.

`reward_farmer.py` reads the approved rows. The screener never changes
your decisions, so rescanning is always safe.

```bash
python reward_screener.py                     # same as 'scan' with defaults
python reward_screener.py scan --mode floor --top 20 --json scan.json
python reward_screener.py scan --exclude sports,politics
python reward_screener.py list --status approved
python reward_screener.py approve <slug-or-condition-id> --p-true 0.97 --budget 25
python reward_screener.py approve <slug-or-condition-id> --p-true 0.97 --capital 200
python reward_screener.py reject <slug-or-condition-id> --note "resolution risk"
```

Most of a scan is reading the fill histories. `/rewards/markets/current`
takes about ten seconds, read as overlapping pages so a market that leaves
mid-read cannot hide one still listed. It prints one line per stage with the counts it
kept and the reasons it skipped the rest, then a table sorted by rank.

A scan assesses every market twice. The first pass costs fills from the fill
prior the last scan stored (see [The fill-cost model](#the-fill-cost-model)),
only to rank the markets. The top `--tape-markets` of that ranking then read
their fill history: trades from the data API and minute midpoints from the
CLOB, up to nine requests a market. So do 60 markets drawn at random from
every market whose fills the first pass valued, kept or dropped as `fills
cost more than the rewards`. That costs up to 60 more histories, fewer when
the draw lands in the top, about 50 more seconds. A new fill prior is fit
from the random sample's histories alone and stored with the scan. The top
markets are the busiest, while most of the markets the prior values trade
little, so a prior fit from the top would cost every quiet market's fills
too high. The draw includes the markets the first pass dropped, so a stored
prior that ran high cannot narrow the sample to the busy markets it spared.
The second pass values every market on that prior, each market that read its
history on its own history too. A market whose fills cost more than its
rewards is dropped as `fills cost more than the rewards`. A market that read
no history is valued on the prior alone, marked `p` in the table.

Key `scan` options:

- `--min-fav` (default 0.90) is the lowest favorite midpoint to consider.
- `--expiry-buffer-days` (7) stops farming this many days before the market
  ends. `--min-window-days` (30) is the fewest farmable days a market needs.
- `--horizon-cap-days` (90) caps the days counted toward rank.
- `--capital` (500) is the USD behind the favorite bid. A scan sizes its bid
  at all of it. The farmer treats a market's capital as the most it may bid
  and chooses the size.
- `--hurdle-rate` (0.40) sets what a filled share costs, the same setting
  the farmer has. The scan has no p_true, so it costs a fill against the
  book midpoint.
- `--tape-markets` (100) is how many of the top markets read their fill
  history. The fill prior is fit from the 60 randomly drawn markets' histories
  instead, and needs at least 20 of them.
- `--max-lead` (0.005) is the most the favorite bid may sit above the best bid
  already in the book. 0 joins the best bid. The bid also stays one tick under
  the lowest ask.
- `--naked-budget` (10) is the USD a longshot bid outside the free zone may
  cost. The bid rests at the cheapest price offering a favorite buyer under
  5% a year whose share costs at most $0.50 for each unit of its score, 0.1c
  on most markets, sized to balance the favorite bid's score whatever that
  costs. A cheaper price whose covering size fits the budget is taken first.
  Nearer the band edge the score runs to zero and the size that covers runs
  to infinity, so there the budget limits the bid, and it takes
  the cheapest price whose size, enough to balance the favorite bid's score
  and at least the reward minimum, fits this budget. A cheaper bid sits
  deeper in the book, behind more longshot bids, so it fills less often. Set
  it to the budget you would approve with, since the farmer prices the bid
  the same way with the approved budget.
- `--min-low-score` (0.10) caps the longshot bid's price. The bid never sits
  above the cheapest price scoring at least this per share. When no price up
  to that one balances the favorite bid within the budget, the bid sits where
  the budget buys the most score, sized to the budget, and the favorite bid
  shrinks to the score that reaches. A market with no such price gets no
  longshot bid.
- `--fragile-drop` (0.03) skips a market as a `fragile midpoint` when
  pulling its best bid of at least the reward minimum size would drop the
  midpoint by this much. When that maker leaves, our favorite bid follows the
  next bid down and its reward score collapses. A market is also fragile when
  pulling that bid leaves no bid or drops the midpoint under `--min-fav`.
- `--exclude` (sports,esports,crypto) skips, as `excluded category`, every
  market whose listing tags put it in one of these comma-separated
  categories: sports, esports, crypto, politics, geopolitics, economy,
  finance, tech, culture, weather. `--exclude none` keeps them all. Every
  esports market is also tagged sports, so excluding sports excludes esports
  too. The console's scan form has a checkbox for each.
- `--mode` filters the table only. Every assessment is stored.

The table columns:

| Column | Meaning |
| --- | --- |
| `mode` | `floor` or `budget`, as above |
| `fav` | Favorite outcome and its size-adjusted midpoint in cents |
| `pool$` | The market's whole daily reward pool in USD, sponsored rewards included |
| `v` | Max spread from the midpoint that still scores, in cents |
| `min` | Minimum order size that scores, in shares |
| `est$/d` | Your estimated daily reward: pool times your share of the score |
| `fill/d` | Favorite shares a day expected to fill your bid; `h` from the market's history, `p` from a scan's fill prior alone, `d` from the built-in default prior alone |
| `net$/d` | `est$/d` less `fill/d` times the loss per filled share |
| `cap$` | Capital both bids tie up |
| `APR%` | `est$/d` divided by `cap$`, times 365, before fill costs |
| `net%` | `net$/d` divided by `cap$`, times 365 |
| `days` | Days left to farm, after the expiry buffer |
| `ahead` | Shares of longshot bids priced above yours, divided by your longshot size. A seller has to fill all of them before reaching your bid, so a low value means more fill risk on the leg you do not want filled |
| `vol24h` | 24-hour volume from Gamma |
| `H` | `H` when Gamma reports holding rewards enabled, `-` when not, `?` when unknown |

Rank is the net daily yield (`net$/d` over `cap$`) times the days left,
capped at `--horizon-cap-days`. That is the return you can expect over the
farm horizon, so long-dated markets rank higher than short ones with the same
yield.

`approve` takes your probability that the favorite wins (`--p-true`) and the
longshot risk budget outside the free zone (`--budget`, default $25). `--capital` sets the most USD
the farmer may bid on this market's favorite, in place of the farmer's
`--capital`.
Without it, a market keeps the capital it already has, or uses the farmer's
default if it has none. `approve` warns, but still approves,
when the planned favorite bid is at or above `--p-true`, because a fill would
lose money. It also warns when a planned longshot leg outside the free zone
costs more than the budget. The zone is the one the leg's price falls in today:
a leg planned in the free zone leaves it as the end date nears.

### Decided markets

Polymarket closes a market only after its on-chain resolution settles, so a
market whose outcome is already known can stay active and accept orders for
days. The scan skips a market when Gamma shows either signal:

- `event closed`: the parent event is closed, for example the award was
  announced or the game is over.
- `resolution proposed`: a UMA resolution has been proposed, and possibly
  disputed.

Only candidates from the latest scan appear in the review queue and on
Discover, so a market that stops qualifying leaves the queue at the next scan.
A candidate also needs `rewards_active = 1`. Rewards can end between scans,
so the console's Review page checks them live with the farmer's own rule and
records what it finds. A candidate whose rewards ended leaves the queue at
once, and it returns if they come back. Approved, rejected and retired markets
always show. Some decided markets show neither signal until Polymarket updates
them. Read the resolution rules before you approve.

### Approximations

- The midpoint drops whole price levels smaller than the reward minimum size.
  Polymarket's adjusted midpoint applies the cutoff per order, so a level of
  many small orders counts here but not there.
- Competing score treats every resting order in the book as one maker. Above a
  0.90 midpoint that overstates the competition, so your estimated share is
  conservative. Your own resting orders also count as competition.
- The two outcome books are exact mirrors, so the screener reads only the
  favorite's book.
- A favorite bid above the best bid becomes the new best bid and pulls the
  midpoint toward itself. Its score is measured against the midpoint it
  creates, not the book's own. Both legs and the competition are scored
  against that shifted midpoint.
- `est$/d` assumes the book stays as it was at scan time, and that no other
  farmer reacts to your orders. Thin books show very high APRs that will not
  survive competition, so read them as a ranking, not a forecast.

### The fill-cost model

A favorite share bought by a fill ties up capital that could earn the hurdle
rate r (`--hurdle-rate`) elsewhere, until it leaves. The farmer offers it back
through `FAV_ASK`, and that ask sells slowly. Measured on 2026-10-10 by
replaying the sweeps of 169 markets against the buys that followed them, the
ask the farmer rests after a fill sold 67.4% of a 100-share fill and 41.9%
of a 1,000-share fill in 30 days. So a share sells after a hold T, taken as
exponential with rate λ a year and cut short at resolution, Y years out,
where a share still unsold is worth p_true. λ is the rate that sells the
measured fraction in 30 days. The fraction runs in a straight line in the
log of the inventory through the two measured sizes, stays at 67.4% under
100 shares and never goes under 5%. The line gives 2,000 shares 34.2%,
where the replay measured 30.9%. The inventory is the favorite shares the
farmer already holds plus the bid's size, so a fill on top of earlier fills
costs more than the first.

In the replay the shares that sold went at about the ask's price. So a share
bought at b is credited a sale at

    a(b) = min(ask(b), p_true)

where ask(b) is the `FAV_ASK` the farmer would rest once the bid fills. That
ask is the midpoint plus `--ask-offset`, or the midpoint an earlier fill
found plus `--ask-offset` while the midpoint is still under it. It is never
under the average cost of the inventory with the fill added plus
`--min-capture`. With δ = ln(1 + r) and k = λ + δ, the fill loses

    L(b) = b - (a(b) * λ/k * (1 - e^(-kY)) + p_true * e^(-kY))

today. For each favorite bid price b on the tick grid inside the reward band,
with the size Q it rests at once the quotes are balanced,

    net(b) = reward/day(b, Q) - Q * rate(m - b) * max(L(b), 0)

where m is the midpoint once your quotes join the book, and rate(d) is how
many times a day a sell sweep reaches more than d below the midpoint. A sweep
that reaches your bid fills all of it. On a short-dated market, or far under
the midpoint, L(b) is negative: the share is worth more than the bid pays.
A bid rests for its reward, so that fill costs nothing and is never counted
as income.

The farmer chooses Q as well as b. A market's capital is the most its
favorite bid may be, favorite shares already held counted at cost. At each
price the farmer values all of the capital left, then three quarters, a half,
a quarter and an eighth of it, and leaves out a size under the reward
minimum. The reward is a share of the pool, so each further share earns less.
L grows with Q, because a larger inventory sells more slowly. So the size
that nets the most can be under the capital, and the farmer rests that size
at the price where it nets the most. Where the fills of all the capital cost
nothing, no smaller size nets more. The naked budget is a separate limit
where the longshot bid sits outside the free zone: a bid it cannot balance
shrinks to what it balances, whatever the capital. A scan sizes its bid at
all of `--capital`.

A scan holds no shares and has no p_true, so it values a fill with an
inventory of the bid alone and the book midpoint as p_true. The farmer's ask
rests over the midpoint, so a scan's a(b) is the midpoint.

The measurement is the table `SOLD` in `fill_cost.py`, next to the command
that produced it. No fill under 53 days from resolution was replayed for the
30 days.

rate(d) comes from the market's fill history: its last 60 days of trades from
the data API, the makers each trade filled included, and the favorite's
minute midpoints from the CLOB. Trades that sold the favorite or bought the
longshot hit favorite bids. Trades less than a minute apart chain into one
sweep, for at most 10 minutes. A sweep counts at distance d when its deepest
fill traded more than d below the midpoint a minute before it began, and that
midpoint was at or above `--min-fav`. Sweeps within an hour of the last one
counted are one event, because a burst fills a resting bid once. The data API
serves only 2,000 rows of a market, so a busy market's history covers less
than 60 days. Events are counted at 12 distances from 0 to 10c, and rate(d)
runs in a straight line between them.

Few markets see enough sweeps to be rated on their own. A scan therefore
pools the histories of a random sample of the markets it values into a
fill prior: per distance, the events a day across those markets, and a
strength in days that says how much the markets differ around that pooled
rate. The sample is random because the prior values mostly quiet markets,
and the busy top markets would inflate it. A market's rate is its own count
shrunk toward the prior,

    rate(d) = (k + s * p) / (t + s)

where k is the market's events at d, t its days of history, p the pooled rate
and s the prior's strength. A nearer bid never gets a lower rate than a
farther one. A scan needs at least 20 sampled histories to fit a prior; with
fewer, the stored prior stays. Until a scan stores one, a built-in prior fit from
256 markets in October 2026 stands in.

The history also carries an exit clock. Each event counted 1c below the
midpoint starts a clock, which stops at the first minute midpoint, a minute
or more after the sweep began, back at or above the midpoint the sweep found.
A clock still running when the history was read counts its time so far, and
a scan pools the clocks into the prior. Fills were valued on that clock, as
if a share sold once the midpoint was back, until the ask's sales were
measured: the midpoint is back within days and the ask sells over weeks. The
clock is still read and stored with each history, and nothing values a fill
with it.

The model leaves out fills of the longshot bid, and the farmer's own record of
its fills.

The `markets` table in `farm.db` is the interface to the farmer (see
[The farm.db contract](#the-farmdb-contract)). A scan updates the metrics,
rank, end date, and `rewards_active` flag. It never touches your decisions:
`status`, `p_true`, `naked_budget_usd`, `note`, `paused`, `naked_paused`, or
`capital_usd`. A market that leaves the reward set gets `rewards_active = 0`
and keeps its status. Retiring markets is the farmer's job.

## Reward farmer

`reward_farmer.py` farms the liquidity rewards on the markets you approved
with the screener. It runs as a daemon. Every cycle it reads each market's
order book, plans the orders it wants resting, compares them with the orders
it has, and cancels or places the difference.

It is a dry run by default. A dry run reads live public data (books, market
records, and your positions) but sends nothing and needs no private key. It
keeps a simulated order set that never fills, so you can watch orders being
placed once and then held or replaced as the book moves. `--live` signs and
posts real orders.

Both modes publish what the farmer does to `farm_state`, `farmer_status`,
`farm_events`, and `farm_mids` in `farm.db`, so a dry run shows up in the
console the same way a live run does. A dry run still never writes to `markets`.

```bash
python reward_farmer.py                       # dry run on farm.db, forever
python reward_farmer.py --once                # one dry-run cycle, then exit
python reward_farmer.py --db scratch.db       # dry run on another database
python reward_farmer.py --live                # real orders
python reward_farmer.py --live --keep-orders  # Ctrl+C leaves the orders resting
python reward_farmer.py --live --key-stdin    # the wallet key is piped in
```

The farmer also records each quoting market's favorite book to
`farm_books.db`, beside `farm.db`, every five minutes. `farm_books.py` owns
that file, and no other process writes it.

A cycle prints a market's summary line only when it differs from the last
one printed for that market, then one line per action. A market resting
unchanged stays quiet:

```text
[16:36:06] some-market: QUOTING mid 96.9c Q1 308.1 Q2 308.1 | bid 94.9c net $3.12/day | hold 0 cancel 0 place 2
[16:36:06]   PLACE  FAV_BID    94.9c x    526.87  score 0.585  some-market
[16:36:06]   PLACE  LONG_BID    0.1c x    735.88  score 0.419  some-market
[16:36:11] some-market: QUOTING mid 96.9c Q1 308.1 Q2 308.1 | bid 94.9c net $3.12/day | hold 2 cancel 0 place 0
```

`score` is the per-share reward score at the current midpoint. `Q1` and `Q2`
are the planned score on each side. Polymarket pays on the smaller one. The
`bid ... net` part is the favorite bid chosen and its net per day, or why
there is none, such as `no fill history yet` or `no favorite bid pays: best
net -$0.12/day at 93.8c`.

A thread beside the main loop reads each approved market's fill history, so a
cycle never waits on those reads. It reads a market as soon as the farmer
manages it, then every hour, and again at once when `min_fav` changes. A
failed read keeps the last history and is retried after two minutes. Until a
market's first history lands, the farmer places no favorite bid there, and
only the exits rest. A read that found no midpoints counts as no history. The
farmer values fills on the fill prior the last scan stored, read again every
cycle.

### The four slots

Above a 0.90 midpoint Polymarket pays only for two-sided liquidity:
min(Q_one, Q_two). Q_one sums favorite bids and longshot asks. Q_two sums
favorite asks and longshot bids. The farmer can rest one order in each of
four slots per market:

| Slot | Order | Side | Purpose |
| --- | --- | --- | --- |
| `FAV_BID` | Buy the favorite | Q_one | The main quote. It rests at the price and size with the best net of its fill cost (see [The fill-cost model](#the-fill-cost-model)), never above your `p_true` minus `--ev-margin`, never over the market's capital, and only when that net beats what the orders earn without it. Uses cash. |
| `LONG_ASK` | Sell the longshot | Q_one | Sells longshot shares from an unwanted fill. Priced at the mirror of the favorite bid, so it scores the same. |
| `FAV_ASK` | Sell the favorite | Q_two | Exits favorite shares above their average cost plus `--min-capture`, which frees the capital again. |
| `LONG_BID` | Buy the longshot | Q_two | Exists only to make Q_one count. It rests at the cheapest price in the free zone, where a fill gives a favorite buyer under 5% a year, sized to balance Q_one whatever that costs: the approved budget does not limit a bid there. That is 0.1c on most markets. Where no free price scores enough it rests higher, at the cheapest price whose size, enough to balance Q_one, keeps its cost plus any longshot shares held within the market's approved budget, and never above the cheapest price scoring `--min-low-score`. It never rests where a fill would give over 20% a year. |

Q_two comes from the covered `FAV_ASK` first, and only the remainder from the
naked `LONG_BID`. A `LONG_BID` in the free zone is sized without the budget
where a share there costs at most $0.50 for each unit of its score
(`FREE_MAX_COST` in `reward_screener.py`: a 0.1c share must score 0.002), so
the favorite bid is never trimmed for it. That
bound is on the arithmetic, not the dollars: the size that covers is Q_one
divided by the score, and at the band edge the score runs to zero. At the
bound a bid costs $0.50 for each unit of score it covers, so at most
half its score times its size, in dollars, for the favorite bid it balances. Over it
the budget limits the price like any other. Among the free prices that pass,
the bid takes the cheapest: it offers the least return, so it fills least,
and on most markets it is 0.1c. A price is free of the
budget only once it has passed that test on every cycle for ten
minutes (`FREE_DWELL_S` in `reward_farmer.py`): the farmer keeps the lowest
midpoint of those ten minutes, where a longshot share scores least, and tests
each price there too. One cycle that fails puts the price back under the
budget at once. A midpoint that flickers across the test therefore rests the
bid at a dearer price that passed throughout, or under the budget where there
is none. At start the farmer takes
that history from `farm_mids`, and a market with none starts under its
budget. A scan and the Review preview assume the midpoint has held. Once a longshot
bid has filled, the budget, less the shares held at cost, limits every price
until `LONG_ASK` has sold them: the exit adds to Q_one, and a
bid sized without the budget to balance it would grow with every fill. A
resting bid whose price has come to offer 5% a year goes back under the
budget, and one that has come to offer over 20% is replaced by a cheaper one
or cancelled: returns only rise as the end date nears, so each happens once.
When no price up to
the cheapest one scoring `--min-low-score` balances Q_one within the budget,
the `LONG_BID` sits at the price where the budget buys the most score, sized
to the budget, and the favorite bid shrinks to match, because score on one side beyond the other earns nothing. Near the
edge of the reward band a small move of the midpoint changes the
`LONG_BID`'s score, and so its size, a lot, so it is replaced more often
there. At 0.1c that only moves it further back in the queue. A bid smaller than the market's reward minimum size is dropped,
because it would carry risk without scoring.

The farmer never buys at market. All inventory comes from passive fills of
these orders.

### Market lifecycle

Each market is in one of six states:

- `QUOTING`: orders rest as planned.
- `COOLDOWN`: the favorite's ask fell by `--drop-threshold` within
  `--drop-window` seconds. The ask is the price at which the favorite's asks
  add up to three reward minimums, so one minimum-size lot that comes and
  goes on a thin book does not move it. All orders come
  down for `--cooldown` seconds. A falling offer is sellers repricing the
  favorite down. Only the ask counts, because a bid that leaves looks the same
  whether it was cancelled or sold into. When a bid leaves, the farmer keeps
  quoting and re-plans against the bids that remain.
- `BELOW_RANGE`: the midpoint is below `--min-fav`. All orders come down.
  Quoting resumes once the midpoint is 0.5c above `--min-fav`.
- `PAUSED`: you set `paused = 1` on the market. All orders come down. When
  you clear it, the market goes through the checks above again, so it can go
  straight to `COOLDOWN` or `BELOW_RANGE` instead of `QUOTING`.
- `UNREWARDED`: the market left the total reward listing. Its own CLOB
  record keeps showing the old rate after rewards end, so only the listing
  counts. All orders come down, but the farmer keeps managing the market.
  When the listing pays it again, the market goes through the checks above,
  the pause included, and quotes again unless one of them holds it. This
  check wins over a pause.
- `RETIRED`: the market closed or stopped accepting orders, you no longer
  approve it, it reached its end date minus `--expiry-buffer-days`, or Gamma
  shows it is already decided (its event closed or a resolution was
  proposed; checked once a minute). All orders are cancelled. In live mode the
  database row becomes `retired` with a note. A row you rejected keeps its
  `rejected` status. Retirement wins over a pause. If you approve a retired
  market again after the farmer has seen it unapproved, the farmer manages it
  afresh, from a new lifecycle state through the same checks, so an expired
  market retires again at once. A dry run that retires a market leaves its
  row `approved`, so that market stays retired until you retire it and
  approve it again.

Every cycle the farmer re-reads the approved rows, their controls, the
settings, and the commands from `farm.db`. A new approval, a rejection, a
pause, or a setting therefore takes effect within one `--interval`. The farmer
reads positions every 60 seconds and every market's Gamma record (reward terms,
tick, trading status) every 5 minutes, in batched requests on a thread of its
own, and a newly approved market's at once.

### Pausing, settings, and stopping

The console (or any process with write access to `farm.db`) controls a
running farmer through the database. Per-market controls are columns on
`markets` that hold a level, not one-off commands. Setting one twice does
the same as setting it once, and a farmer that restarts reads the same level.

- `paused = 1` puts the market in `PAUSED`, which cancels its orders. Set it
  back to 0 to resume.
- `naked_paused = 1` takes down the `LONG_BID` for as long as it stays set,
  the same way a longshot fill does for `--naked-cooldown` seconds. The
  favorite bid shrinks to whatever the covered `FAV_ASK` still balances.
- `capital_usd` replaces `--capital` for that market only. NULL uses
  `--capital`.
- To retire a market, set its `status` to `retired`. To cancel everything,
  set `paused = 1` on every approved market.

The `settings` table overrides the farmer's command-line options. The
command-line flags give the defaults, and each row replaces one of them
while it exists. The keys are the option names with underscores, and the
seconds options end in `_s`: `capital`, `max_lead`, `ask_offset`,
`min_capture`, `ev_margin`, `min_fav`, `min_low_score`, `keep_ratio`,
`hurdle_rate`, `drop_threshold`, `drop_window_s`, `cooldown_s`,
`naked_cooldown_s`, and `expiry_buffer_days`. Each value is a JSON number.
Opening the database deletes rows of retired settings: `fav_target_score`,
from before fill-cost pricing, `depth_haircut`, `fill_lookback_days` and
`sweep_prior_weight`, from before fill histories, and `stuck_share`, from
before the ask's sales were measured.
`farm_db.put_setting` refuses an unknown key or an out-of-range value. A bad row written another
way is ignored, with an `ALERT` event. When the effective config changes, the
farmer logs a `SETTINGS` event with the old and new values. A changed setting
moves the targets at once, but a resting order that still passes the keep
checks below stays where it is.

To stop the farmer, insert a `stop` row into `commands`. The farmer reads it
on its next cycle, stamps `consumed_at` and `result`, and shuts down the same
way as on Ctrl+C. A stop sent while no farmer runs is marked expired when the
next farmer starts, so it does not stop that farmer.

### Safety mechanisms

- Every order is post-only, so a fill is always a maker fill.
- Every order is good-till-date. It expires at the market's end date minus
  `--expiry-buffer-days`, even if the farmer dies.
- The favorite bid never prices above `p_true - --ev-margin`.
- `--capital` caps each market's favorite exposure. Favorite shares already
  held count against it at cost. The favorite bid rests at the size at or
  under what is left that nets the most.
- The approved budget caps the naked longshot leg outside the free zone: a
  resting longshot bid whose fill would give a favorite buyer 5% a year or
  more, plus longshot shares held, at cost. A longshot bid offering less,
  where a share costs at most $0.50 for each unit of its score, is outside
  it until a longshot bid fills. While the shares from a fill rest an exit,
  the budget limits every price.
- No longshot bid rests where a fill would give a favorite buyer over 20% a
  year. When no cheaper price balances the favorite bid, that bid shrinks or
  does not rest. The exits are unaffected.
- A longshot fill (your longshot shares went up) prints an `ALERT` and pauses
  the longshot bid for `--naked-cooldown` seconds. Without it, the favorite
  bid shrinks to whatever the covered favorite ask still balances.
- An order that is still close to its target stays in place. It must sit on
  the current tick, score at least `--keep-ratio` of the target, keep a size
  within -20% and +25% of the target, and stay within the price cap or
  budget, which a longshot bid resting free of it is not held to. Otherwise
  it is replaced. A favorite bid is judged by its net as
  it rests, at its own price and size, instead of its score and the target's
  size. It stays while its price is still a candidate, its net trails the
  best bid's by no more than `1 - --keep-ratio` of it (or $0.02/day, if
  more), it beats the orders without it, the naked budget still balances at
  least 80% of it, and it is no more than 25% over the capital left. This
  keeps queue position and avoids churning orders on every small move of the
  midpoint.
- An order the CLOB rejects, such as a post-only cross or an unfunded bid,
  backs off that market's slot for 30 seconds. Each further rejection doubles
  the wait, up to 15 minutes, and an accepted order resets it. The backoff is
  logged once per rejection.
- In live mode, Ctrl+C, SIGTERM, a `stop` command, or a crash cancels every
  order on the managed markets before the farmer exits. A normal `--once`
  exit leaves the orders resting. So do Ctrl+C and SIGTERM under
  `--keep-orders`, and a `stop` command that asks to keep the orders. Orders
  left that way rest unmanaged until the farmer starts again: no cooldowns,
  retirement, or fill handling, and they expire only at their good-till-date.
  The next start adopts the ones still close to their targets and replaces
  the rest.

### Positions and existing orders

The farmer reads your positions from the public data API for
`POLYMARKET_FUNDER_ADDRESS` in `.env`. It never sells shares that were in your
wallet before it started managing a market:

- The first time it sees an approved market, it records your holdings of both
  tokens as a baseline (0 when you hold none). In live mode the baseline goes
  into the `baseline_fav` and `baseline_long` columns of `markets`, so it
  survives restarts. A dry run keeps it in memory only.
- On every positions refresh the baseline drops to your current holdings if
  those are lower. A manual sale therefore does not leave shares reserved that
  are gone.
- The farm's inventory is your holdings above the baseline. Only those shares
  are offered through `FAV_ASK` or `LONG_ASK`, or count against the capital
  and budget.

At startup it prints a `holding` line with your holdings, the baseline, and
the farm's share for every managed market where you have a position.

Approximation: the data API reports one average price per token, blended
across baseline and farm shares. The farmer uses that blended average as the
farm inventory's cost, for the `FAV_ASK` exit floor and the capital and budget
caps. When your earlier shares cost more or less than the farm's fills, the
exit floor is off by that difference.

In live mode the farmer adopts the open orders on the markets it manages. It
keeps the ones that match a slot target and cancels the rest, including
orders you placed there by hand. It never reads or touches orders on markets
it does not manage.

### Key options

| Option | Default | Meaning |
| --- | --- | --- |
| `--db` | `farm.db` in the data directory | SQLite file written by the screener. See [Where the databases live](#where-the-databases-live) |
| `--live` | off | Sign and post real orders |
| `--key-stdin` | off | Live only. Read the wallet key from one line of standard input, not from the environment or Bitwarden |
| `--once` | off | Run one cycle and exit |
| `--keep-orders` | off | Live only. Ctrl+C and SIGTERM leave the managed orders resting instead of cancelling them. A crash still cancels them. Not with `--heartbeat` |
| `--interval` | 5 | Seconds between cycles |
| `--capital` | 500 | Most USD a market's favorite bid may be, held favorite shares included at cost. The farmer rests the size at or under it that nets the most |
| `--max-lead` | 0.005 | Most the favorite bid may sit above the best bid already in the book. 0 joins it |
| `--ask-offset` | 0.01 | Favorite ask distance above the midpoint |
| `--min-capture` | 0.005 | Favorite ask minimum above its average cost |
| `--ev-margin` | 0.02 | The favorite bid stays at or below `p_true` minus this |
| `--min-fav` | 0.90 | Stop quoting below this favorite midpoint |
| `--min-low-score` | 0.10 | The longshot bid never sits above the cheapest price scoring this per share |
| `--keep-ratio` | 0.8 | Keep an order that scores at least this share of its target, a favorite bid that nets, at its own price and size, at least this share of the best bid's |
| `--hurdle-rate` | 0.40 | Yearly return capital must beat; a filled share costs its price minus what it sells for, discounted at this rate over the weeks the sale takes |
| `--drop-threshold` | 0.03 | Fall in the favorite's ask, where its asks add up to three reward minimums, that starts a cooldown |
| `--drop-window` | 60 | Seconds over which that fall is measured |
| `--cooldown` | 600 | Seconds without orders after an ask drop |
| `--naked-cooldown` | 3600 | Seconds without a longshot bid after a longshot fill |
| `--expiry-buffer-days` | 7 | Retire this many days before the end date |
| `--heartbeat` | off | Live only. See the warning below |

### The heartbeat warning

`--heartbeat` posts a heartbeat to Polymarket every 5 seconds. If the
heartbeats then stop for 10 seconds, because the farmer crashed, the network
dropped, or the machine slept, Polymarket cancels **all** open orders owned by
these API credentials. That may include orders placed by your other tools.
Polymarket does not document whether the scope is the API key or the whole
account. Without `--heartbeat`, a dead farmer leaves its orders resting until
their good-till-date expiry.

This is an open risk, not a settled fact. Leave `--heartbeat` off until the
scope is confirmed:

- Polymarket's own pages describe the scope two ways. The "Manage Orders"
  page says a lapse cancels all open orders "owned by those CLOB API
  credentials". The "Send heartbeat" reference page says "for the user".
- Every process here signs in with the same API key. `connect` in
  `polymarket/client.py` derives it from the wallet key, for the farmer, the
  worker, and the scoring probe alike. So a lapse could cancel the orders a
  restore or a watch placed, and orders you placed by hand, along with the
  farmer's.
- Before anyone turns `--heartbeat` on, the farmer needs an API key of its
  own, and the scope needs a test on a second, throwaway account. The code
  has no option for a separate key yet.
- Do not test the scope on the main account. If the scope is the whole
  account, the test itself cancels every open order.

`--heartbeat` and `--keep-orders` cannot be combined, because Polymarket
cancels the orders 10 seconds after the heartbeats stop.

### Going live

1. Run a scan and approve the markets you want, with your `--p-true` and
   `--budget` for each.
2. Run a dry run on the same database and read the output for a few cycles.
   Check the prices, sizes, and the `holding` lines.
3. Make sure the wallet holds enough USDC. At startup the live farmer compares
   your collateral balance with the favorite bids it plans and warns if the
   balance is short.
4. Start `python reward_farmer.py --live`. It checks the region it connects
   from and does not start from a place where Polymarket rejects new orders.
   It then reads the wallet key from the environment or from Bitwarden, and
   locks the vault again as soon as the key is read (see
   [Where the wallet key comes from](#where-the-wallet-key-comes-from)).
   Leave `--heartbeat` off.
5. Stop it with Ctrl+C or a `stop` command, which cancel the orders on the
   managed markets.

### The farm.db contract

`farm_db.py` owns the schema and every SQL statement the tools run against
`farm.db`. Opening the file migrates an older one in place: missing columns
are added with `ALTER TABLE` and missing tables are created, so opening it
twice changes nothing. The file runs in WAL mode with a 5-second busy
timeout, because the farmer and the console write at the same time.

Each column has exactly one writer process:

| Table | Writer | Purpose |
| --- | --- | --- |
| `markets` | Screener: scan columns. Console: `rewards_active` of a candidate, from the Review page's live check. Console or CLI: `status`, `p_true`, `naked_budget_usd`, `note`, `paused`, `naked_paused`, `capital_usd`. Farmer, live only: `baseline_fav`, `baseline_long` | Candidate markets, your decisions, and the per-market controls |
| `farm_state` | Farmer | One row per managed market, replaced every cycle: phase and reason, midpoints, Q1 and Q2, capital, naked USD in use, and JSON for the position, the four slots (resting order, target, live score, decision and reason), the favorite book inside the reward band with your orders marked, backoffs, and `scoring_pct` (live only) |
| `farmer_status` | Farmer | One row: mode, pid, start time, last cycle, interval, heartbeat, managed count, effective config, and `stopped_at` once it exits |
| `farm_events` | Farmer | Changes only: `PLACE`, `CANCEL`, `FILL`, `PHASE`, `ALERT`, `REJECT`, `BACKOFF`, `RETIRE`, `START`, `STOP`, `SETTINGS`. Rows older than 30 days are deleted at startup |
| `settings` | Console or CLI | Overrides of the farmer's command-line options. Keys starting with `scan.` are not farmer options, and the farmer never reports them as unknown settings: `scan.params` holds the console's last scan parameters, and every scan writes `scan.last`, its funnel, and `scan.fill_prior`, the fill prior it fit when at least 20 sampled histories read, which the next scan, the farmer and the console value fills on |
| `commands` | Console inserts, farmer stamps `consumed_at` and `result` | One-off requests. `stop` is the only kind |
| `rewards_daily` | Farmer, live only | Your reward earnings per market per day, fetched every 10 minutes |
| `farm_mids` | Farmer | At most one row per managed market per minute, in dry run too: the midpoint, the book midpoint, and the prices the `FAV_BID` and `LONG_BID` rest at after that cycle (NULL when the slot rests nothing). Rows older than 7 days are deleted at startup. The console draws its midpoint chart from it |

This table leaves out four tables: `fav_buys`, `decisions`, `mutes`, and
`triage`. The module docstring of `farm_db.py` lists every table with its
writer.

The one exception to one writer per column: the live farmer retires a market
by changing `status` from `approved` to `retired` and writing a `note`, in a
single conditional `UPDATE`. A status you already changed is never
overwritten.

A `FILL` event comes from a change in your positions, because the data API
reports no fills. Its price is the price of the order that rested in that
slot at the previous cycle, and its `data` says so.

`scoring_pct` and `rewards_daily` come from authenticated reward endpoints
whose response fields Polymarket does not document. The farmer reads them
defensively, logs a warning when the shape does not match, and keeps
farming.

## Farm console

`farm_console` is a local web console for the reward farmer and the order
tools. It shows what the farmer is doing, live, and it is where you run scans,
approve markets, and control the farmer. Every change it makes to the farm
goes through `farm_db` into `farm.db`, and the farmer picks it up on its next
cycle. Its **Orders** and **Sniper** pages queue work for the worker through
`tools.db` (see [Orders page](#orders-page) and [Sniper page](#sniper-page)).

```bash
python -m farm_console                        # port 8787
python -m farm_console --db scratch.db        # another farm database
python -m farm_console --tools-db scratch-tools.db   # another worker database
python -m farm_console --port 8788            # another port
```

Then open http://127.0.0.1:8787. Run it from the repository root (or with
the repository on `PYTHONPATH`) so Python can find the `farm_console`
package. `--db` defaults to `farm.db`, the production database, and
`--tools-db` to `tools.db`, both in the data directory (see
[Where the databases live](#where-the-databases-live)). A relative `--db` or
`--tools-db` path is read from the current directory.

Run it next to the farmer on the same `farm.db` and next to the worker on the
same `tools.db`, each in its own terminal. No process needs another: the
console shows the last state the farmer and the worker published. The sidebar
lists both processes. A farm page shows a banner when the farmer is not
running, and a tool page shows one when the worker is not. The console reads
`POLYMARKET_FUNDER_ADDRESS` from `.env` to show your holdings on the Review
page. It never reads or handles a private key.

Pages:

| Page | Shows | Actions |
| --- | --- | --- |
| Overview (`/`) | Totals (markets quoting, capital in favorite bids, naked longshot bids and the part of them the budgets limit, farm inventory, the screener's $/day estimate, today's earnings), the alerts that need you, the tracked markets, and the latest events | Each alert links to its action: Resume, Edit budget, Edit p_true, or the farmer status |
| Discover (`/discover`) | The scan parameters, the last scan's funnel (listed, passed the prefilter, assessed on the book, and the skip reasons), and the stored markets by rank, 50 per page. Filter by mode, text in the question or slug, status, and minimum days to the farmer's cutoff | Run a scan |
| Review (`/review/<condition_id>`) | The plan from the last scan: the reward band with the scanned best bid and ask and your planned bids, each planned order with why it is priced that way, the yield figures (gross, fills/day, fill cost and net APR), the resolution rules from Gamma, and what your wallet already holds, which becomes the farmer's baseline. `/review` opens the best-ranked undecided candidate | Approve with p_true, naked budget, and capital; reject with a note; retire an approved market; re-approve a retired one |
| Live markets (`/markets?phase=`) | One card per managed market: phase, midpoint, the reward band with your bids marked, the four slots, Q1/Q2 and naked budget bars, inventory, and the expiry cutoff. Approved markets the farmer has not picked up yet show as waiting | Pause or resume quoting, pause or resume the naked leg, edit p_true and budget, retire |
| Market detail (`/markets/<condition_id>`) | Stat tiles, a 24-hour midpoint chart with the band, your bids and fill and phase markers, the slots, the book inside the band, the position, the lifecycle, fills, and the market's events | The same controls as the card |
| Activity (`/activity?tab=`) | `rewards`: earnings per day and market against the screener estimate. `fills`: each fill with its approximate edge against `p_true`. `inventory`: wallet, baseline, farm shares, and exit orders. `log`: every farm event, filterable by kind and market | None |
| Settings (`/settings`) | The farmer process (mode, pid, start, last cycle, interval, heartbeat) and every setting with its effective value and where it comes from: your override, the farmer's own value, or the built-in default | Stop the farmer, save or reset settings, cancel all managed orders |
| Orders (`/orders`) | The snapshot job in progress and every stored snapshot. A snapshot's page (`/orders/snapshots/<id>`) shows its orders and its newest preview or restore | Take a snapshot, preview a restore, place it |
| Sniper (`/sniper`) | A search over the open markets and every watch with the quote the worker last saw, its outcome, and its log. `/sniper/arm` is the form that arms one outcome | Arm a watch, disarm it |

### Running a scan from the console

The Discover page runs the same scan as `python reward_screener.py scan`, in a
background thread of the console. It takes two to three minutes. The page shows
the current stage while it runs, then the counts and the duration, or the
error. Only one scan runs at a time: a second request while one runs gets
"A scan is already in progress". The form starts from the parameters of the
last scan run from the console, or the screener's defaults. A scan stores its
rows and its funnel in one transaction and never changes your decisions.

### Decisions and controls

- **Approve** sets `status` to `approved` with the p_true, naked budget, and
  capital you entered. You enter p_true as a whole percent from 1 to 99, with
  the - and + buttons or the arrow keys stepping it by 1; the database stores
  it as a probability. The naked budget starts at $10 for a new market. A
  blank capital means the farmer's `capital` setting.
  On a market that is already approved, the same form edits those terms.
  After a decision on a candidate, the console opens the next undecided
  candidate. After an edit, it opens the market's page.
- **EV check.** While you type p_true, the Review page shows what the farmer
  will do with it. The farmer never bids above p_true minus `ev_margin`,
  rounded down to the tick. When the planned bid is above that cap, the
  check shows the cap the farmer bids no higher than and its lower score, or
  that the cap is outside the band. The live preview below it shows the price
  the farmer would pick under that cap and its net per day. Until you type a
  p_true, it values the market at the book midpoint, as the scan does. Its
  fills come from the history the scan stored and the stored fill prior. A
  market the scan did not read is valued on the prior alone, and the preview
  says so; with no prior stored yet, it says to run a scan. That score is measured against the midpoint
  the capped bid creates. It also warns when your naked budget is below a
  planned longshot leg outside the free zone, which the scan priced within its own
  naked budget.
  It uses the effective
  `ev_margin`: your override, else the farmer's last value, else the default.
- **Pause and naked leg** set `paused` or `naked_paused` to the level the
  button names, so pressing twice changes nothing. Each button names the
  action it takes, such as Resume while the market is paused.
- **Retire** asks first, naming the market, then sets `status` to `retired`.
  The farmer cancels the market's orders on its next cycle.
- **Cancel all** pauses every approved market. You must type `CANCEL`, and
  the server checks it.
- **Stop farmer** asks first, then inserts a `stop` command. The Settings
  page shows when the farmer consumed it. **Stop, keep orders** inserts a
  `stop` command that asks the farmer to leave its orders resting, as
  `--keep-orders` does.
- **Settings** forms check each value with the same rules the farmer uses
  and write only the fields you changed, all in one transaction. A value
  that fails shows its error next to the field and nothing is saved.
  **Reset** deletes the override, so the farmer's own value applies again.

There is no live switch. Live mode needs the private key, which the farmer
reads in its own terminal, so you start it there:
`python reward_farmer.py --live`.

### Security

The console has no login and controls real orders, so it must only be
reachable from your own machine, and only by its own pages:

- It binds `127.0.0.1` only. To reach it from another machine, use an SSH
  tunnel, as [Safety](#safety) shows. Never change the bind address.
- Every request must carry `Host: 127.0.0.1:<port>` or `localhost:<port>`.
  Anything else gets 400. This blocks DNS rebinding, where a hostile site
  points its own domain at 127.0.0.1.
- Every request other than GET or HEAD needs the console's token, a random
  value made fresh each time the console starts. The pages carry it in a
  `<meta>` tag, htmx sends it as the `X-Console-Token` header, and plain forms
  send it as the `console_token` field. A missing or wrong token gets 403.
  Another web site open in your browser cannot read the token, so it cannot
  forge a request.
- A request other than GET or HEAD whose `Origin` header names another origin
  gets 403, even with the token.
- A request that makes real orders move carries a typed word, which the
  server checks: `CANCEL` to cancel all managed orders, `PLACE` to place a
  restore, and `ARM` to arm a live watch.
- Every decision and control is a POST. Two GET requests write, the Review
  page's live check and its skip to the next quotable candidate, and they
  only record a candidate's `rewards_active` as Polymarket reports it. No
  request can choose that value.

`farm_console/security.py` holds all of these checks, as a middleware in
front of every route.

### Live updates

The pages update without a reload. `GET /stream` is a server-sent event
stream that polls `farm.db` and `tools.db` once a second and sends five
signals with no HTML: `cycle` when the farmer finishes a cycle, `farm-event`
when a new event appears, `status` when the farmer or the worker starts or
stops running, `job` when a worker job is queued, changes state, or logs a
line, and `watch` when a watch is armed, changes, is sampled, or logs a line.
Each live region
of a page requests its own HTML fragment from `/partials/...` when its signal
arrives, using htmx and its SSE extension. After a market control, the regions
that show that market reload at once. There is no client-side state and no
chart JavaScript: the charts are SVG drawn on the server.

The alerts come from a rule table in `farm_console/views.py`: cooldown, below
range, paused, naked budget 99% or more used with no longshot bid resting free
of it, a favorite bid scoring under 0.10,
a slot in backoff, a longshot-bid fill in the last 24 hours, and the farmer not
running. The farmer counts as not running when its last cycle is older than
three intervals, or when it has stopped.

`/api/overview`, `/api/markets`, `/api/markets/<condition_id>`,
`/api/events?after=&before=&limit=&condition_id=`, and `/api/activity?tab=`
return the same data the pages show, as JSON.

The midpoint chart reads `farm_mids`, which the farmer fills with one sample
per market per minute. A market's chart is empty until the farmer has run for
a minute. The resolution rules come from Gamma when you open a Review page
and are kept for 10 minutes. The live check needs the total reward listing,
which includes sponsored pools, leaves out markets paid only by sponsors, and
takes about ten seconds to read. The console
reads it in a background thread when a Review page first asks, then at most
every 5 minutes. Until it lands, the console records no rewards change and
the queue keeps every candidate where it is. If Gamma or the data API is
unreachable, that panel says so and offers a retry; the rest of the page still
works.

htmx 2.0.11 and htmx-ext-sse 2.2.4 are vendored in `farm_console/static/`, so
the console works offline except for the IBM Plex fonts, which fall back to
system fonts, and the Review page's rules and holdings. To upgrade, download
`dist/htmx.min.js` from the `htmx.org` package and `dist/sse.min.js` from the
`htmx-ext-sse` package, and update the versions here.

### What the terms mean

Each term on the console that is not self-explanatory, such as naked budget,
Q1 / Q2, a phase or a slot name, has an info button after it. Hover over it or
Tab to it to see what the term means. Escape closes the explanation.

`farm_console/glossary.py` holds every explanation, so edit them there. The
Settings and scan parameters take their explanations from it too. A template
shows a term's button with `{{ tip('naked_budget') }}`, and an unknown key fails
the page instead of rendering an empty tip. `farm_console/static/tips.js`, with
no dependencies, shows one tooltip on the page body, so a scrolling table never
clips it. `test_farm_console_tips.py` fails when a template uses a key the
glossary lacks, or when the glossary has an entry no page uses.


## Orders page

The **Orders** page (`/orders`) stores snapshots of every open order on the
account. From a snapshot's page you place again the orders that are no longer
open, for example after Polymarket cancelled them when your balance dropped.

The console holds no key, so it only queues a job in `tools.db`. The worker
runs the job. While no worker runs, the page shows a banner with the command
that starts one, and a queued job says `It waits for the worker, which is not
running.` Every job on this page needs a keyed worker.

One job of a kind is queued or running at a time. A preview and a restore
count as one kind. A second request gets a notice such as `A snapshot job is
already queued (job 12).`

### Take a snapshot

1. Open **Orders**.
2. Type a note of at most 200 characters, or leave it empty.
3. Click **Take snapshot**.

The worker lists every open order on the account, the farmer's and your own
alike, and stores them with their market names. With each order it stores
whether the order's market had the status `approved` in `farm.db` at your
click, so that a later restore leaves the farmer's orders to the farmer
whatever the farmer manages by then. A listing that fails stores
nothing, and the job ends `failed` with the error. When Gamma cannot name the
markets, the orders are stored without names and the page shows each market's
condition id.

The job panel shows the job's state (`queued`, `running`, `done`, or
`failed`), its log, and a link to the snapshot once it is stored.

### Import the old snapshot files

The snapshot script this page replaces saved each snapshot as an
`orders_snapshot_*.json` file. To store those files as snapshots, run:

```bash
python -m tools import-snapshots                   # the files in snapshots/
python -m tools import-snapshots path/to/files     # the files somewhere else
```

The import needs no key and no running worker. It takes the same `--db` as
the worker, before or after the command. It prints one line, in this form:

```text
Stored <n> snapshot files with <n> orders in <tools.db>; <n> already stored.
```

A file that was stored before, by name, is skipped, so you can run the import
again. A file that is not a snapshot stops the import with `Import failed:
<file> is not an order snapshot`. The files before it stay stored.

The old files hold local times with no time zone. The import reads them in
the time zone of the machine it runs on and stores them as UTC. Run it on the
machine that took the snapshots.

### Preview a restore

1. On **Orders**, click a snapshot's time in the **Snapshots** table.
2. Click **Preview restore**.

The worker lists the orders that are open now and decides what a restore
would do with each order of the snapshot. A preview places nothing. The page
groups the orders under **Would place** and the four skips below, and each
row says why.

### What a restore places and what it skips

The worker goes through the snapshot's orders in their order. For each one,
the first rule that fits decides.

| Rule | Group on the page | Why |
| --- | --- | --- |
| Nothing of the order was left to fill when the snapshot was taken | **Skipped: nothing left to fill** | There is no size to place. |
| The order's market has the status `approved` in `farm.db` at your click | **Skipped: the farmer's market** | The farmer cancels the orders on its markets that are not its own, and places its own. |
| The order's market had the status `approved` when the snapshot was taken | **Skipped: the farmer's market** | The order was the farmer's, or the farmer would have cancelled it. A market the farmer has stopped managing since gets none of its old orders back. |
| The snapshot has no record of that (an imported file, or a snapshot from before the record was kept), and the market has the status `retired` now | **Skipped: the farmer's market** | The farmer managed the market once, and nothing says whose order this was. An order you placed there by hand is skipped too: place it again by hand. |
| The order expires within 2 minutes from now, or has expired | **Skipped: expired** | The CLOB refuses an order that expires within a minute. The second minute is for the job to reach the order. |
| The same order, by its id, is still open | **Skipped: already open** | It never went missing. This holds however much more of it has filled since. |
| An open order that is not in the snapshot has the same token, side, price, and remaining size | **Skipped: already open** | It was placed again already, by you or by an earlier restore. Each such order covers one snapshot order, so two identical snapshot orders with one copy open give one skip and one place. |
| None of the above | **Would place**, then **Placed** | The order is missing. |

What it places:

- **The remaining size.** A restore places the order's size less what had
  filled when the snapshot was taken, cut to whole hundredths of a share. It
  never places the original size of a partly filled order.
- **The same token, side, and price.** The worker reads each market's tick
  once for the job. An order whose price is not a multiple of that tick is
  not sent.
- **The same expiration.** An order with an expiration goes out
  good-til-date with that expiration. An order with none goes out
  good-til-cancelled.
- **Post-only.** Every order of a restore is post-only. An order that would
  cross the book is refused by the exchange and shows under **Rejected by
  the exchange**. It is never filled as a taker.

### Place a restore

Placing follows a finished preview. The **Place** button shows only under a
finished preview that would place at least one order, and the server refuses
a placing request unless the snapshot's newest preview or restore job is a
finished preview. So every restore follows a preview you could read, and
after a restore you preview again before you place again.

1. Read the preview.
2. Type `PLACE` in the field beside the button. The server checks the word
   and answers `Type PLACE to confirm.` without it.
3. Click **Place N orders**.

These are real orders. What happens next:

- The worker lists the open orders again and makes a new plan. It places
  what is missing then, so the count can differ from the preview.
- A finished preview has no time limit of its own. The limit is on the
  restore job, which a worker must start within 5 minutes of your click. A
  restore that waited longer fails with `preview it again`, so a restore
  queued while the worker was down does not run hours later.
- If the worker cannot list the open orders, the job fails and places
  nothing.
- The worker stores one line for each order as it goes. A placed order shows
  under **Placed** with its new order id. An order the exchange refused shows
  under **Rejected by the exchange** with the exchange's reason. An order
  that was not sent, or whose post got no answer, shows under **Error**.
- A restore ends early when the worker stops, and after 3 posts in a row get
  no answer. The lines stored by then stay, and the job's error says how far
  it got.

A restore is safe to run again while the orders it placed still rest
unfilled. Preview again. Each of those orders is open now, so the new plan
skips it.

A restore cannot tell an order that was cancelled from an order that filled
after the snapshot was taken. Both are missing from your open orders, so
both show under **Would place**. The same holds for an order that an earlier
restore placed and that has filled since, in whole or in part. Read the
preview before you place.

### When a line says the order may rest

An **Error** line is one of two kinds.

`not sent: ...` means the order never left the worker, because the market's
tick could not be read or the order could not be signed. Nothing rests.

`no answer (...): the order may rest: check your open orders` means the post
went out and no usable answer came back: a timeout, a dropped connection, a
5xx status, or an answer the worker could not read. The exchange may have
placed the order or not. The worker does not send it again, because a second
post could place a second order.

To settle it, look at your open orders on Polymarket. A new preview also
shows it. Click **Preview restore** again, and the worker lists your open
orders afresh. An order that rests unfilled is skipped as already open. An
order that does not rest is listed under **Would place**.

## Sniper page

The **Sniper** page (`/sniper`) arms tick-change watches. This page places
real orders, unattended, at a moment you do not choose. Read this whole
section before you arm a live watch.

### What a tick-change watch is

A Polymarket market trades on a price grid, and prices run from one tick to
one minus a tick. On a 0.01 tick the highest legal price is $0.99. When
Polymarket shrinks the market's tick to 0.001, $0.999 becomes legal. Orders
at one price fill in the order they arrived, so the first resting order at
the new best price is first in the queue there.

A watch waits for one outcome token's tick to reach a target tick, and then
posts one limit order, once. It exists to be first in that queue. The watch
is a row in `tools.db`, and the worker holds it, so it outlives a console
restart. One token has at most one open watch.

Every sniper order is post-only. It rests or the exchange refuses it, and it
never takes liquidity. The build chose that default, and the owner has not
confirmed it. The form has no option for an order that takes.

### Find a market and arm a watch

1. Open **Sniper**, type a few words under **Find a market**, and click
   **Search**.
2. Read the results. They are Gamma's search, ranked again. Outcomes that
   are ready for the default setup come first, then the best matches, then
   the highest volume. The page shows at most 25 outcomes. **Bid**, **Ask**,
   and **Tick** come from the live book.
3. Click **Arm** on the outcome you want. The status does not block you. It
   only says whether the outcome fits the default setup.
4. Fill in the **Arm a watch** form, which shows the live book above it.
5. For a live watch, type `ARM` and click **Arm watch**. For a rehearsal,
   tick **Dry run** and click **Arm watch** with no word.

The **Status** column judges each outcome for a BUY under the default guard:

| Status | Meaning |
| --- | --- |
| `ready` | Open, accepting orders, on a tick coarser than 0.001, with a best bid within 0.5c of 99.0c |
| `closed` | The market is closed |
| `not accepting orders` | The market takes no orders |
| `tick 0.001 already` | The tick has reached the target, so a watch would be missed |
| `no book` | The book could not be read |
| `no bid` | The book has no bid |
| `bid at 98.0c` | The best bid is outside the guard's range. The status shows the bid |

The form's fields:

| Field | Default | Rule |
| --- | --- | --- |
| **Side** | `BUY` | `BUY` or `SELL` |
| **Price (dollars)** | `0.999` | A multiple of the target tick, from one tick to one minus a tick |
| **Size (shares)** | empty | A positive number |
| **Target tick** | `0.001` | One of `0.1`, `0.01`, `0.001`, `0.0001` |
| **Expires after (s)** | `0` | `0` for good-til-cancelled, or at least 60 seconds, counted from the signing |
| **Guard the entry price** | ticked | See [The guard](#the-guard) |
| **Required price** | `0.99` | A price between 0 and 1 |
| **Tolerance** | `0.005` | From 0 to 1 |
| **Max quote age (s)** | `5` | A positive number of seconds |
| **Dry run** | not ticked | See [Dry runs and the keyless worker](#dry-runs-and-the-keyless-worker) |

A token that already has an open watch is refused with `This token already
has an open watch.`

### What the worker does with an armed watch

The worker reads the open watches four times a second. It adopts a new one
in these steps:

1. It reads the token's book. If the tick is already at the target or finer,
   the watch closes as `missed` and never fires.
2. It keeps that book's best bid and ask as the first quote.
3. It reads what signing needs from the exchange, and signs the order for
   the target tick ahead of time. It signs again every half second, because
   a signed order carries its signing time, and it never sends a signature
   older than 2 seconds.

If the book or the exchange cannot be read, the watch stays `armed`, its log
says why, and the worker tries again 2 seconds later.

Two threads then look for the tick change: the market websocket, and a read
of the books every half second. The thread that first sees the tick at the
target judges the guard and posts the order itself. A watch fires once.

While the watch is armed, its card shows the tick, the bid, and the ask the
worker last saw, refreshed every second.

### The guard

The guard stops the order when the market is not where you expected it at
the moment the tick changes. With **Guard the entry price** ticked, all of
these must hold or the watch closes as `rejected` and nothing is sent:

- The worker saw a quote before the tick change, with a price on your side
  of the book.
- That quote is no older than **Max quote age (s)**. The default is 5
  seconds.
- The side's own best price in that quote is within **Tolerance** of
  **Required price**. The side's own best price is the best bid for a BUY and
  the best ask for a SELL. The defaults ask for 99.0c within 0.5c.

The guard reads the last quote from before the change on purpose. Once the
finer tick opens, other orders reprice the book, and a quote from after the
change would refuse a setup that was valid a moment earlier. The worker never
takes a quote from an observation that already shows the target tick.

With the box not ticked, the watch has no guard, and the order goes out at
the tick change whatever the price is.

### Dry runs and the keyless worker

A dry run rehearses everything up to the post and sends nothing. Tick **Dry
run: rehearse everything up to the post, send nothing** on the form. A dry
run needs no typed word.

- On a keyed worker a dry run signs the order, as a live watch does, and
  then stops. It ends `rehearsed` with `signed, not sent`.
- On a worker started with `--no-key` it signs nothing. It ends `rehearsed`
  with `not signed (no key), not sent`.
- A dry run can also end `missed`, `rejected`, `disarmed`, or `failed`
  because the order could not be signed. It can never end `fired`. SQLite
  refuses that row.

The closed card of a dry run shows the quote the guard judged, which thread
saw the tick, and the time from the tick to the post. So a dry run on a real
tick change tests your guard settings with no money at risk.

A worker with no key adopts dry-run watches only. A live watch then stays
`armed`, and its card says `Armed. Waiting for a keyed worker: the running
worker holds no key.`

### How a watch ends

A watch is open while it is `armed` or `firing`. Every other state is final.
SQLite refuses any change to the state of a closed watch, so to try again
you arm a new one.

| State | What happened | Is an order on the book? |
| --- | --- | --- |
| `armed` | The watch waits for the tick change, for a worker, or for a keyed worker | No |
| `firing` | The guard passed and the first post has left. The worker waits for the answer or retries | Possibly |
| `fired` | The exchange accepted the order. The card shows its order id | Yes. A real post-only order rests, and the worker does nothing more with it. Cancel it on Polymarket if you do not want it |
| `rehearsed` | A dry run reached the post | No |
| `rejected` | Your guard refused. The card says which check failed | No. Nothing was sent |
| `missed` | The tick was already at the target when the worker adopted the watch | No, with one exception below |
| `disarmed` | You disarmed it before it fired, or while it was retrying after refusals | No |
| `failed` | See the next table | It depends on the reason |

A `failed` watch gives its reason in the sentence on its card:

| The card says | What happened | Is an order on the book? |
| --- | --- | --- |
| `The exchange refused the order: ...` | Every post was refused, and the retry window closed or the refusal was one no retry can change | No |
| `The order could not be signed: ...` | The worker could not sign at the tick | No |
| `The worker stopped inside the retry window after N refusals: ... No order rests.` | You stopped the worker while it was retrying | No |
| `The post got no answer (...). An order may rest: check your open orders.` | A post went out and no usable answer came back | Unknown. Check your open orders on Polymarket |
| `The worker stopped while the order was leaving. An order may rest: check your open orders.` | The worker died between the post and its answer, and the next worker found the watch `firing` | Unknown. Check your open orders on Polymarket |

The card prints the two unknown cases in the warning color. In both, look at
your open orders on Polymarket and cancel the order there if you do not want
it. The worker never sends that order again, and it never resumes a fire
after a restart.

How the worker retries:

- The exchange's order endpoint can lag the tick event, so a refusal of the
  first post is expected. The worker posts again every 0.25 seconds until 10
  seconds have passed since the tick change.
- It stops at once on a refusal that no retry can change: the address is
  blocked, the account cannot fund the order, or the post-only order would
  cross the book.
- It never retries a post that got no answer, because the order may rest and
  a second post could place a second order.
- Every refusal goes into the watch's log with the exchange's message.

The exception for `missed` is a crash at the moment of the post. The worker
records `firing` in `tools.db` up to about 50 milliseconds after the post
starts, and never before it, so that no database write delays the post. If
the worker process dies in that gap, the row is still `armed` although an
order may rest. The next worker then finds the tick at the target and closes
the watch as `missed`. After a worker crash near a tick change, check your
open orders even when the watch says `missed`.

Stopping the worker with Ctrl+C leaves each armed watch `armed`, and the next
worker adopts it again. No watch fires while no worker runs. A tick that
changes in that time is found at the next start, and the watch closes as
`missed`.

### Disarm a watch

Click **Disarm** on an open watch. The console writes the request to
`tools.db`, and the worker answers within a second.

A disarm cannot stop a post that has already left:

- The worker reads the request up to a quarter of a second after your click.
  A tick change inside that time still fires the watch.
- Once the first post has left, a disarm only stops the retries. If the
  exchange accepted the first post, the watch ends `fired` and the order
  rests. If the exchange refused it and the worker was retrying, the watch
  ends `disarmed` and no order rests.

So read the watch's final state after a disarm. `disarmed` means no order.
`fired` means the order went out first.

With no worker running, the card says `Disarm requested. Waiting for the
worker to answer.` The next worker to start, keyed or not, disarms the watch.

### What is not confirmed against the live exchange

As of 2026-10-11 the restore and the sniper have posted only to fake
clients, in the tests and in the build's own checks. The feed's subscribe
message was checked on Polymarket's live market channel, which is read-only.
These points wait for the first live use:

- **The order of the feed's messages.** The guard assumes that the tick
  change arrives before any price at the new tick. If a price at the new
  tick arrives first, the guard judges that price, and it can refuse a valid
  setup or pass one it should refuse. A dry-run watch on a real tick change
  answers this with no money at risk, because the closed card records the
  quote the guard judged.
- **The shape of the exchange's answer to a post.** The worker counts an
  order as placed only when the answer has `success` true, no `errorMsg`,
  and an `orderID`. If the real answer differs, an accepted order shows as a
  refusal or as an order that may rest. Check your open orders after the
  first live restore and the first live fire.
- **The text of the refusals.** The worker recognizes a refusal that no retry
  can change by words in the exchange's message, listed in
  `TERMINAL_REFUSALS` in `polymarket/orders.py`. If the exchange words one
  differently, the worker retries it for the whole 10 seconds instead of
  stopping at once. The exact text of the refusal during the tick race is
  also unknown.
- **The delay under load.** The worker shortens Python's thread switch
  interval to 0.5 ms so that a busy thread cannot hold up the thread that
  sees the tick. A comment in `tools/sniper.py` records the measurement
  behind that, taken on 2026-10-11 with a CPU-bound thread standing in for a
  restore. A parked thread woke after a median 15.5 ms and a worst 110 ms at
  Python's default 5 ms, against 0.08 ms and 0.35 ms at 0.5 ms. Nobody has
  measured it with a real restore running, and a restore's posts share the
  sniper's HTTP connection. Until someone measures it, avoid running a
  restore while a live watch is armed.

[Safety](#safety) has two more open points: the SSH tunnel and the scope of
the farmer's heartbeat.

## Tests

```bash
python run_tests.py                  # every test_*.py, one process a module
python run_tests.py test_farm_db     # only the modules you name
pytest                               # the same tests under pytest
```

`run_tests.py` runs the modules side by side, prints one line for each with
its test count and time, and exits with status 1 when a module fails. Bare
`pytest` works because `pyproject.toml` keeps it out of `handoff/`,
`.claude/`, `.venv/`, and `snapshots/`.

The tests never touch the network and never hold a real key. Each one works
in its own temporary directory, with fake clients where the code would talk
to Polymarket.

## Troubleshooting

### The farmer or the worker does not start

Each program prints why. The common lines:

| Message | What to do |
| --- | --- |
| `POLYMARKET_FUNDER_ADDRESS is not set` | Copy `.env.example` to `.env` and set the address |
| `POLYMARKET_PRIVATE_KEY is not set, and no Bitwarden item is named in BW_POLYMARKET_ITEM_ID or BW_POLYMARKET_ITEM_NAME` | Set one of the three, or start with `--key-stdin` |
| `the Bitwarden CLI (bw) is not installed` | Install it, or keep the key in the environment |
| ``the Bitwarden CLI is not logged in: run `bw login` once`` | Run `bw login` in a terminal |
| `` the Bitwarden vault did not lock again: run `bw lock` `` | Run `bw lock`, then start again |
| `a worker is already running on this database` | Stop the other worker, or wait 10 seconds after it died |
| `the API rejects new orders from here` | See the next two sections |

### "Trading restricted in your region" (403)

- The address that sends the order is in a blocked place. The check is by IP
  address, and it happens when the order is sent.
- Confirm that the VPN carries this process. Split tunnelling by application
  matches the interpreter's exact path, so a virtual environment or a second
  install needs its own rule, and a process that was already running when
  the rule changed keeps its old route.
- To see where the exchange places you, run this with the same interpreter
  and read the `loc=` line:

  ```bash
  python -c "import httpx; print(httpx.get('https://clob.polymarket.com/cdn-cgi/trace').text)"
  ```

- `polymarket/region.py` holds the tables the start-up check uses, copied
  from [Polymarket's geoblock page](https://docs.polymarket.com/developers/CLOB/geoblock).
  Ireland, Japan, Malta, and the Netherlands are blocked on the website only,
  and the API takes orders from them. A place the tables cannot classify is
  allowed, with a message that says it was not checked.

### "451 Unavailable For Legal Reasons"

- A 451 is not the 403 above. The exchange's edge refuses the request before
  the API sees it, and it can refuse an address in a country that is on no
  published list. Shared VPN addresses get flagged on their own.
- A different server, even in the same country, usually clears it. The block
  follows the address, not the country.
- The start-up check probes the exchange and reports `the CLOB refuses this
  address (451)`.
- The hosts can disagree. When Gamma answers 451 and the exchange does not,
  every read of Gamma fails. The Sniper page's search then shows the error.
  Armed watches read only the exchange, and they keep working.

### The console answers 400 or 403

- `Unknown Host header.` with status 400 means the browser did not address
  the console as `127.0.0.1:<port>` or `localhost:<port>`. Through an SSH
  tunnel, forward the console's own port number.
- `Missing or wrong console token.` with status 403 usually means the page
  is older than the console process. Reload the page. The token changes each
  time the console starts.

### A tool page says the worker is not running

Start it with `python -m tools`. A page also says so when the worker runs on
another `tools.db` than the console reads. Give both the same file.

## References

- [py-clob-client-v2](https://github.com/Polymarket/py-clob-client-v2), the
  SDK that signs and posts the orders
- [Polymarket's geoblock page](https://docs.polymarket.com/developers/CLOB/geoblock)
- [The Bitwarden CLI](https://bitwarden.com/help/cli/)
