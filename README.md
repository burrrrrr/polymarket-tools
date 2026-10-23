# Polymarket tools

A reward farmer for Polymarket, the web console that controls it, and a keyed
worker that runs two order tools from that console.

- `farm/reward_screener.py` ranks the markets that pay liquidity rewards and
  stores them in `farm.db` for you to approve or reject.
- `farm/reward_farmer.py` rests post-only orders on the approved markets to
  earn those rewards. It is a dry run unless you pass `--live`.
- `python -m console` serves a local web console. It shows what the
  farmer does, and it is where you run scans, approve markets, and control
  the farmer. It holds no wallet key.
- `python -m tools` runs the worker. It holds the wallet key and does what the
  console's **Orders** and **Sniper** pages ask for. It takes snapshots of
  your open orders, places the ones that went missing, and fires tick-change
  watches.

A live farmer, a restore, and a live watch place real orders with real money.
Read [Safety](#safety) before you start anything that holds a key.

## Documentation

Each tool has a page of its own.

| Page | What it covers |
| --- | --- |
| [Reward screener](docs/screener.md) | How a scan runs, its options and columns, and how you approve or reject a market |
| [Reward farmer](docs/farmer.md) | The four order slots, the market lifecycle, the controls, the options, going live, and the `farm.db` contract |
| [How the bids are priced](docs/pricing.md) | The strategy, the longshot bid's three zones, and the fill-cost model |
| [Console](docs/console.md) | The pages, the decisions and controls, the request checks, and the live updates |
| [Orders page](docs/orders.md) | Snapshots of your open orders, and how a restore places the ones that went missing |
| [Sniper page](docs/sniper.md) | Tick-change watches, the guard, and how a watch ends |
| [Deploy to a server](docs/deploy.md) | One cloud VM for the farmer, the worker, and the console: creating it, typing the key at each start, moving the databases, and what is not verified yet |

This page covers what the tools share:

- [How the pieces fit](#how-the-pieces-fit)
- [Install](#install)
- [Set up the environment](#set-up-the-environment)
- [Run the processes](#run-the-processes)
- [Safety](#safety)
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
| Screener | `python -m farm.reward_screener` | Never | Reads Polymarket's public APIs. Writes its scan to `farm.db`. |
| Farmer | `python -m farm.reward_farmer` | Only with `--live` | Reads your decisions, settings, and commands from `farm.db` every cycle and writes what it does there. Records books to `farm_books.db`. With `--live` it posts and cancels orders. |
| Console | `python -m console` | Never | Serves HTTP on `127.0.0.1`. Reads and writes `farm.db` and `tools.db`. Reads the public APIs for a scan, the Review page, and the Sniper search. |
| Worker | `python -m tools` | Yes, unless `--no-key` | Reads the jobs and the watches the console wrote to `tools.db` and writes the results there. Posts the orders of a restore and of a watch. |

The console steers the farmer and the worker only by writing rows. The farmer
reads `farm.db` again every cycle, and the worker reads `tools.db` again at
least every second, so neither needs the console to keep running.

The worker is a process of its own for three reasons:

- A console restart does not stop an armed watch or a restore in progress.
- The wallet key stays out of the process that parses HTTP.
  `tests/test_console_orders.py` and
  `tests/test_console_sniper.py` fail if importing the console loads
  `polymarket.keys`, `polymarket.client`, or the worker.
- The sniper does not share Python's interpreter lock with scans and the
  console's event stream.

The worker never opens `farm.db`, and the farmer never opens `tools.db`, so
neither waits on the other's write lock. When a restore needs to know which
markets the farmer manages, the console reads them from `farm.db` at your
click and puts them in the job.

Both databases run in WAL mode with a 5-second busy timeout, and every column
has one writer process. `farm/farm_db.py` owns the schema of `farm.db` and
every statement run against it. `tools/db.py` does the same for `tools.db`.

### Files

Each folder holds one part of the code. Every folder but `tests/` is a
package.

| Folder | What it holds |
| --- | --- |
| `farm/` | The screener and the farmer |
| `analysis/` | Backtests and probes you run by hand |
| `polymarket/` | The code that talks to Polymarket. It holds no strategy |
| `tools/` | The keyed worker |
| `console/` | The web console |
| `tests/` | The tests |

Four files sit in the root. `data_dir.py` finds the databases for each
program. `run_tests.py` runs every test module. `pyproject.toml` holds the
dependencies and the pytest settings. `.env.example` lists the settings each
process reads at start.

`git ls-files` lists every tracked file. Git ignores the data: `.env`,
`farm.db`, `farm_books.db`, `tools.db`, the old snapshot files in
`snapshots/`, and everything in `data/`.

## Install

You need Python 3.11 or later.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

The code runs from the checkout, and the install brings only the
dependencies. Run every command in this file from the repository root, so
that Python finds the `farm`, `analysis`, `polymarket`, `tools`, and
`console` packages.

Two outside programs are optional. The Bitwarden CLI (`bw`) is needed only
when the wallet key comes from a Bitwarden vault. The `claude` CLI is needed
only for the screen in `farm/triage.py`.

## Set up the environment

1. Copy the example file.

   ```bash
   cp .env.example .env
   ```

2. Make the data folder. The example file sets `POLYMARKET_DATA_DIR=data`,
   and no program makes the folder. See
   [Where the databases live](#where-the-databases-live).

   ```bash
   mkdir data
   ```

3. Set `POLYMARKET_FUNDER_ADDRESS` in `.env` to the address that holds your
   funds and positions.
4. Decide where the wallet key comes from. See
   [Where the wallet key comes from](#where-the-wallet-key-comes-from).

The farmer, the screener, the console, the worker, the scoring probe, and
`python -m analysis.triage_eval` each load `.env` from the repository root
when they start. A variable already set in the real environment wins over its
line in `.env`.

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
| `POLYMARKET_DATA_DIR` | No | The directory where the farmer, the console, and the worker keep their databases. `.env.example` sets it to `data`. See [Where the databases live](#where-the-databases-live). |

An EOA wallet (`POLYMARKET_SIGNATURE_TYPE=0`) must approve Polymarket's
exchange contracts to spend its collateral and its conditional tokens before
it can trade. The installed SDK lists the contract addresses in
the `py_clob_client_v2.config` module.

### Where the wallet key comes from

Three programs read the key: a farmer started with `--live`, the worker
unless you start it with `--no-key`, and `analysis/scoring_probe.py`. A
dry-run farmer, the screener, and the console read no key.

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
python -m farm.reward_farmer --live --key-stdin
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

The farmer, the screener, the console, and the worker look for `farm.db` and
`tools.db` in one data directory. It is the directory `POLYMARKET_DATA_DIR`
names, and the repository root when the variable is not set. The processes
therefore agree wherever you start each one.

- You can set `POLYMARKET_DATA_DIR` in `.env` or in the real environment. A
  relative value is taken from the repository root, so
  `POLYMARKET_DATA_DIR=data` names the `data/` folder of the checkout, which
  Git ignores. `.env.example` sets this value.
- The directory must exist. No program creates it, because a mistyped
  directory would start every process on empty databases. A program that
  cannot find it prints `POLYMARKET_DATA_DIR is not a directory` with the
  path, and exits.
- `--db` on the farmer, the screener, and the worker, and `--db` and
  `--tools-db` on the console, each name one file outright. A relative path
  there is read from the current directory.
- The farmer writes `farm_books.db` in the directory of its `farm.db`.
- `python -m tools import-snapshots` looks for the old snapshot files in the
  `snapshots` folder of the data directory. See
  [Import the old snapshot files](docs/orders.md#import-the-old-snapshot-files).
- `python -m analysis.triage_eval` finds `farm.db` the same way.
  `python -m analysis.exit_backtest fetch` has no default: give it `--db`.
- `python -m analysis.queue_fit fetch` has no default either: give it `--db`
  and `--books`. `python -m analysis.queue_fit fit` needs `--books`.
- `python -m analysis.pricing_backtest fetch` and `run` need `--db` and
  `--books` too.

## Run the processes

A usual session has three terminals, each in the repository root:

```bash
python -m farm.reward_farmer  # the farmer, a dry run
python -m console       # the console, on http://127.0.0.1:8787
python -m tools              # the worker, with the key
```

No process needs another one to be running. The console shows the last state
the farmer and the worker published, with a banner when one of them is down.

With no flags, the three share the `farm.db` and the `tools.db` of the data
directory (see [Where the databases live](#where-the-databases-live)).

### Screener

`python -m farm.reward_screener` runs one scan and exits. It needs no key. See
[Reward screener](docs/screener.md#reward-screener). The console's Discover page runs the
same scan.

### Farmer

`python -m farm.reward_farmer` is a dry run that reads live public data and
sends nothing. `python -m farm.reward_farmer --live` signs and posts real
orders.

See [Reward farmer](docs/farmer.md#reward-farmer), and [Going live](docs/farmer.md#going-live) before the
first live run.

### Console

`python -m console` serves the console on http://127.0.0.1:8787 until Ctrl+C.
[Console](docs/console.md) lists its options.

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

`python -m analysis.scoring_probe` measures how long an order rests before
Polymarket counts it for rewards. It reads the live key, lists your open
orders, and places and cancels nothing. Its module docstring has the details.

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
  the console through one yet. [Security](docs/console.md#security) lists every check.
- **Leave `--heartbeat` off.** If the farmer's heartbeats stop, Polymarket
  cancels open orders, and it is not confirmed whether that means the
  farmer's orders or every order on the account.
  [The heartbeat warning](docs/farmer.md#the-heartbeat-warning) has the open risk.
- **Two console actions place real orders.** Placing a restore needs the
  typed word `PLACE`, and arming a live watch needs the typed word `ARM`.
  The server checks each word. Both tools post post-only orders, so an order
  rests or is refused and never takes liquidity.
- **A post that gets no answer may have placed an order.** The worker never
  sends such a post again. The page then says that an order may rest, and you
  check your open orders. See
  [When a line says the order may rest](docs/orders.md#when-a-line-says-the-order-may-rest)
  and [How a watch ends](docs/sniper.md#how-a-watch-ends).
- **Polymarket blocks new orders by the address that sends them.** A keyed
  worker and a live farmer each check their region before they read a key,
  and neither starts from a place where new orders are rejected. The check
  runs once, at the start. It does not notice a VPN that drops later.
- **The restore and the sniper have open points.**
  [What is not confirmed against the live exchange](docs/sniper.md#what-is-not-confirmed-against-the-live-exchange)
  lists what the first live use still has to confirm.
- **Keep `.env` out of Git.** `.gitignore` already lists it.

## Tests

```bash
python run_tests.py                  # every module in tests/, one process each
python run_tests.py test_farm_db     # only the modules you name
pytest                               # the same tests under pytest
```

`run_tests.py` runs the modules side by side, prints one line for each with
its test count and time, and exits with status 1 when a module fails. Bare
`pytest` works because `pyproject.toml` points it at `tests/` and puts the
repository root and `tests/` on the import path.

The tests never touch the network and never hold a real key. Each one works
in its own temporary directory, with fake clients where the code would talk
to Polymarket.

## Commit messages

```bash
git config core.hooksPath .githooks  # once in each clone
```

`.githooks/commit-msg` then rejects a commit whose subject is not
`area: what changed` in at most 72 characters, or whose body is over 60
words. The hook lists the areas on its `areas` line.

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
