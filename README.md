# Polymarket Order Snapshot and Restore

This tool allows you to take a snapshot of all your open Polymarket orders and restore them later if they get automatically cancelled (e.g., due to balance drops).

## Features

- **Snapshot**: Save all open orders to a timestamped JSON file
- **Restore**: Recreate orders from a snapshot file
- **Dry-Run Mode**: Validate orders without submitting them to test restoration
- **Smart Filtering**: Automatically skips orders that already exist on Polymarket
- **Expiration Preservation**: Restores orders with their original expiration times (GTC/GTD)
- **Error Handling**: Gracefully handles API errors and continues processing
- **Summary Reports**: Detailed output showing successful and failed operations
- **Tick Watch**: Fuzzy-search for a market, then place a limit order the instant its minimum tick size shrinks (0.01 -> 0.001), guarded by a check that the market really was pinned at 99c

## Installation

1. Ensure you have Python 3.9 or higher installed.

2. Install the required dependencies:

```bash
pip install -r requirements.txt
```

## Setup

1. Copy the example environment file:

```bash
cp .env.example .env
```

2. Edit `.env` and fill in your credentials:

```env
POLYMARKET_PRIVATE_KEY=your-private-key-here
POLYMARKET_FUNDER_ADDRESS=your-funder-address-here
POLYMARKET_SIGNATURE_TYPE=1
```

**Note:** The private key doesn't have to be in the `.env` file. You can load it from a secure store like Bitwarden instead. See the [Bitwarden Configuration](#bitwarden-configuration) section below.

### Configuration Options

- **POLYMARKET_PRIVATE_KEY** (optional if using Bitwarden): Your wallet's private key. Can be set in `.env` or loaded from Bitwarden.
- **POLYMARKET_FUNDER_ADDRESS** (required): The address that holds your funds
- **POLYMARKET_SIGNATURE_TYPE** (optional): Signature type
  - `0`: EOA/MetaMask (standard wallet)
  - `1`: Email/Magic wallet (default)
  - `2`: Browser wallet proxy
- **POLYMARKET_HOST** (optional): CLOB API endpoint (default: `https://clob.polymarket.com`)
- **POLYMARKET_CHAIN_ID** (optional): Chain ID (default: `137` for Polygon)
- **POLYMARKET_KEY_SOURCE** (optional): Force a specific key source
  - `env`: Only use environment variable (POLYMARKET_PRIVATE_KEY)
  - `bitwarden`: Only use Bitwarden
  - (default): Try environment variable first, then Bitwarden if not found

### Bitwarden Configuration

Instead of storing your private key in the `.env` file, you can securely load it from Bitwarden:

1. **Install Bitwarden CLI**: Follow the [Bitwarden CLI installation guide](https://bitwarden.com/help/cli/)

2. **Store your private key in Bitwarden**:
   - Create a Secure Note item in Bitwarden
   - Store your private key in the notes field
   - Note the item name or ID

3. **Configure the tool** to use Bitwarden by setting one of these in your `.env`:
   ```env
   BW_POLYMARKET_ITEM_NAME=your-item-name
   # OR
   BW_POLYMARKET_ITEM_ID=your-item-id
   ```

4. **Authentication**: The tool will prompt you to log in or unlock your Bitwarden vault when needed. The vault will be automatically locked after operations complete.

## Usage

### Taking a Snapshot

To save all your current open orders to a file:

```bash
python snapshot.py
```

This will create a file named `orders_snapshot_YYYYMMDD_HHMMSS.json` containing all your open orders.

Example output:
```
Polymarket Order Snapshot Tool
========================================
Connected to Polymarket CLOB
Fetching open orders...
Found 5 open order(s)
Snapshot saved to: orders_snapshot_20250124_143022.json

Snapshot complete! Use the following command to restore:
  python restore.py orders_snapshot_20250124_143022.json
```

### Restoring Orders

To restore orders from a snapshot file:

```bash
python restore.py orders_snapshot_20250124_143022.json
```

The script will:
1. Load the snapshot file
2. Fetch your current open orders from Polymarket
3. Filter out orders that already exist (by token ID, side, price, and size)
4. Restore only the orders that don't already exist
5. Preserve original expiration times (GTC or GTD)
6. Display a summary of successful and failed restorations

#### Dry-Run Mode

You can test the restoration process without actually submitting orders:

```bash
python restore.py orders_snapshot_20250124_143022.json --dry-run
```

Dry-run mode will:
- Validate all orders in the snapshot
- Check which orders already exist
- Show what would be restored without actually creating orders
- Still requires API connection to check existing orders

Example output:
```
Polymarket Order Restore Tool
============================================================
Connected to Polymarket CLOB
Loaded snapshot from: orders_snapshot_20250124_143022.json
Snapshot taken: 2025-01-24T14:30:22.123456
Total orders in snapshot: 5
Fetching existing open orders...
Found 1 existing open order(s)

Skipping 1 order(s) that already exist on Polymarket

Restoring 4 order(s)...
------------------------------------------------------------
[1/4] Processing order (GTC (no expiration))... ✓ Success (Order ID: 12345)
[2/4] Processing order (GTD (expires: 2025-12-31 23:59:59))... ✓ Success (Order ID: 12346)
[3/4] Processing order (GTC (no expiration))... ✗ Failed: Insufficient balance
[4/4] Processing order (GTC (no expiration))... ✓ Success (Order ID: 12347)

============================================================
RESTORATION SUMMARY
============================================================
Total orders in snapshot: 5
Skipped (already exist): 1
Total orders processed: 4
Successfully restored: 3
Failed: 1

Skipped orders (already exist on Polymarket):
  - BUY 10.0 @ $0.4500 (Token: 0x1234...)

Successfully restored orders:
  - BUY 10.0 @ $0.4500 (Token: 0x5678..., ID: 12345, GTC (no expiration))
  - SELL 5.0 @ $0.5500 (Token: 0x9abc..., ID: 12346, GTD (expires: 2025-12-31 23:59:59))
  - BUY 20.0 @ $0.3000 (Token: 0xdef0..., ID: 12347, GTC (no expiration))

Failed orders:
  - Order #3 (Token: 0x1111...): Insufficient balance
```

#### Dry-Run Example

```bash
python restore.py orders_snapshot_20250124_143022.json --dry-run
```

Example output:
```
Polymarket Order Restore Tool
(DRY RUN MODE)
============================================================
Connected to Polymarket CLOB
Loaded snapshot from: orders_snapshot_20250124_143022.json
Snapshot taken: 2025-01-24T14:30:22.123456
Total orders in snapshot: 5
Fetching existing open orders...
Found 1 existing open order(s)

Skipping 1 order(s) that already exist on Polymarket

Validating 4 order(s)...
(DRY RUN - No orders will be submitted)
------------------------------------------------------------
[1/4] Processing order (GTC (no expiration))... ✓ Valid (would restore)
[2/4] Processing order (GTD (expires: 2025-12-31 23:59:59))... ✓ Valid (would restore)
[3/4] Processing order (GTC (no expiration))... ✓ Valid (would restore)
[4/4] Processing order (GTC (no expiration))... ✓ Valid (would restore)

============================================================
DRY RUN SUMMARY
============================================================
Total orders in snapshot: 5
Skipped (already exist): 1
Total orders processed: 4
Valid orders (would restore): 4
Failed: 0
```

### Watching for a Tick Size Change

`tick_watch.py` watches one or more markets and places a limit order on each
the moment its own minimum tick size shrinks. Polymarket caps prices at
`1 - tick`, so a market on a 0.01 tick cannot trade above $0.99. When it moves
to a 0.001 tick, $0.999 (99.9c) becomes legal for the first time - and the
point of this tool is to be first in the queue at that price.

The markets you watch together form a batch. Each market in a batch fires
independently: when one market's tick shrinks, its order goes out at once and
the others keep watching.

Run it with no arguments and it walks you through everything:

```bash
python tick_watch.py
```

```
Search markets (or 'q' to quit): fed interest rate
  Searching for 'fed interest rate'...
  40 outcome(s) matched; 1 at 99.0c and not yet on a 0.001 tick.

  #      Bid    Tick   24h Vol  Status              Market -> Outcome
  ------------------------------------------------------------------------
   1    99.0c    0.01   $199.0K  READY               Will there be no change... -> No

Pick a number, or several like 1,3 ('s' to search again, 'q' to quit): 1
Order size in shares (market minimum 5): 100

Add another market? [y/N]: y

1 market(s) in this batch so far.

Search markets (or 'q' to finish adding): ...
```

The picker fuzzy-searches open markets, expands each one into its individual
outcomes, and prices every hit off the real order book. Only valid setups are
listed: at the required price (99c by default) and not yet on the target tick.
With `--no-price-check` every open outcome is listed instead, with a `Status`
column saying why a row is or isn't a valid setup. Anything typed at a prompt
is re-asked rather than guessed at.

Pick several rows at once by separating their numbers with commas, e.g. `1,3,5`.
Each one is validated and sized in turn, and the size prompt offers the last
size as its default. If any number is not on the list, nothing is added and
the prompt asks again.

After each pick is validated and sized, the watcher asks whether to add
another. Answer `y` to go back to the picker, or press Enter to see the plan
for the whole batch and confirm it once. While a batch has markets in it, `q`
at the picker finishes adding instead of quitting. A market that fails
validation (wrong price, already at the target tick) prints why and returns to
the picker without losing the batch. Picking a market that is already in the
batch prints a note and asks again.

An interactive session keeps going until you quit. When a batch ends - every
order placed or rejected, or the batch timed out - the picker comes back for
the next batch, with your last order size offered as the default. Ctrl+C while
watching drops the markets still waiting and returns to the picker; an order
already in flight is allowed to finish. `q` (or Ctrl+C) at the picker with an
empty batch ends the session and lists every order it placed. Credentials are
loaded once, on the first batch, and reused for the rest. Pass `--once` to stop
after a single batch. Runs that name markets with `--slug`, `--condition-id`,
or `--token-id` always stop after one batch.

When a batch ends, a summary lists each market that fired (tick change,
detection path, entry-price check, order, result) and then each market that
never saw its tick change.

Every prompt has a flag, so unattended runs skip the interaction entirely:

```bash
# Fully specified - no prompts at all
python tick_watch.py --slug will-x-happen --outcome YES --size 100 --yes

# Two markets in one batch; each fires on its own tick change
python tick_watch.py --slug will-x-happen --slug will-y-happen --size 100 --yes

# Jump straight to the pick list for a query
python tick_watch.py --search "fed interest rate"

# Rehearse without sending anything
python tick_watch.py --slug will-x-happen --size 250 --dry-run
```

Name markets with `--token-id`, `--condition-id`, and `--slug`. Each flag can be
repeated, and the three can be mixed in one run. `--outcome` (an outcome name
such as `YES`, or a zero-based index) applies to every `--condition-id` and
`--slug`. `--size` is the size of each market's order, not a total. `--search`
seeds the interactive picker and cannot be combined with the other three. Omit
them all for the interactive picker.

A named run resolves every market before it watches anything, and a market
that cannot be resolved stops the run with exit code 1, since that is almost
always a typo. A market that resolves but fails validation is skipped with its
reason, and the rest are still watched. A skip for a real problem (price guard,
size, unreadable book) makes the run exit non-zero, so an unattended caller can
see it.

#### The entry-price guard

The strategy only makes sense on a market pinned at the top of its 0.01 tick,
so the price is checked twice and the order is refused if it does not hold:

- **At startup**, against the live order book. A market that is not at 99c is
  rejected before anything else happens - including before credentials are
  loaded, so a bad pick never triggers a vault unlock.
- **At the tick change**, against the last price observed *before* the change.

That second point is deliberate. The instant a tick opens up, competing orders
reprice the book, so a fresh read at that moment would reject a setup that was
perfectly valid a millisecond earlier. Both detection paths therefore track the
best bid/ask continuously while watching, and the guard inspects the newest
reading taken before the change - refusing to fire if that reading is older
than `--price-max-age` rather than guessing.

The side determines which price is checked: `BUY` looks at the best bid, `SELL`
at the best ask.

#### Key options

| Option | Default | Purpose |
| --- | --- | --- |
| `--size` | *prompted* | Order size in shares, for each market |
| `--price` | `0.999` | Limit price in dollars (99.9c) |
| `--side` | `BUY` | `BUY` or `SELL` |
| `--require-price` | `0.99` | Price the market must be at before the change |
| `--price-tolerance` | `0.005` | Allowed deviation from `--require-price` |
| `--price-max-age` | `5` | Reject if the pre-change price is older than this |
| `--no-price-check` | off | Skip the entry-price guard entirely |
| `--target-tick` | `0.001` | Tick size to wait for |
| `--from-tick` | `0.01` | Tick size expected at startup |
| `--poll-interval` | `0.5` | Seconds between `/books` polls |
| `--post-only` | off | Reject rather than cross the spread |
| `--expiration-seconds` | `0` | Place a GTD order instead of GTC |
| `--retry-window` | `10` | Seconds to retry an order the venue rejects |
| `--timeout` | `0` | Give up on markets still waiting after N seconds, for the whole batch (0 = watch forever) |
| `--dry-run` | off | Watch and report, never submit |
| `--fire-if-already` | off | Order even if already at the target tick |
| `--no-wss` | off | Detect by polling only |
| `--once` | off | Exit after one batch instead of returning to the picker |
| `-y`, `--yes` | off | Skip the confirmation prompt |
| `--skip-region-check` | off | Do not look up the egress region at startup |
| `--allow-blocked-region` | off | Arm the watch even from a blocked region |

Exit codes: `0` success or cancelled, `1` order failed, `2` bad usage, `3`
rejected by the entry-price guard. A batch exits with the highest code among
its markets. In a named run, a market skipped at setup also counts: `3` for a
price-guard rejection, `2` for a size problem, `1` for an unreadable book, and
`0` for a market already at the target tick. A looping session reports each failure
as it happens and exits `0` when you quit.

#### The region preflight

The CLOB geoblock is enforced on the IP that submits the order, and it is
checked at submission time - so a VPN that is not carrying this process turns
the tick change you have been waiting for into a 403. Before any credential
load, the watcher reads its own egress region and refuses to arm when new
orders are blocked from there:

```
[17:32:12.284] Region check: JP-13 via 198.51.100.24 - new orders permitted (website-blocked, API permitted)
[17:32:13.186] Region check: GB-ENG via 203.0.113.7 - API REJECTS NEW ORDERS from here
```

It uses two independent signals, because either alone clears runs that then
fail:

- **A live probe of the hosts.** A blocked request is answered by the edge
  with `451` before it reaches the API, and that happens for exit addresses
  the venue distrusts - a shared VPN IP in a country on no published list
  still gets a 451. Only probing catches this. The market lookup host is
  probed separately and a 451 there is a warning, not a stop: it costs the
  picker and `--slug` but `--token-id` still resolves.
- **The published country and region lists**, which catch the close-only
  jurisdictions. Reads succeed from those, and only order submission is
  rejected, so probing alone would clear them.

Region is read from `polymarket.com/api/geoblock`, falling back to the CDN
edge's country when that endpoint is unreachable. Only the country and region
it reports are used - **not** its `blocked` flag, which encodes the website's
rules and reads `true` for several jurisdictions whose API access is
unrestricted (Ireland, Japan, Malta, the Netherlands). An unrecognised region
is reported and allowed, so a change to the published list cannot silently
stop a run; unreachable endpoints likewise fail open with a warning.

A geoblock rejection at order time is also treated as non-retryable, since it
is not the venue lagging behind a tick change and retrying it only burns
`--retry-window`.

#### How detection works

Two paths run at once and, for each market, the first to see its new tick
wins. Each path is one connection for the whole batch, however many markets it
holds:

1. The WSS market channel
   (`wss://ws-subscriptions-clob.polymarket.com/ws/market`), subscribed once
   for every market in the batch. It pushes `tick_size_change` events and
   carries the tick and top of book on every `book` and `price_change`
   message. This is the fast path; it reconnects on its own if dropped.
2. A REST poll of `/books`, which returns the live tick size *and* the top of
   book for every market still waiting, in one request. This is the honest
   cross-check - a socket that is connected but silently stale is
   indistinguishable from a quiet market - and it bounds how old the guard's
   price reading can be.

Each market has its own fire thread. When a market's tick changes, its thread
runs the entry-price guard and places that market's order while the paths keep
watching the rest.

The WSS path needs the `websockets` package (included in `requirements.txt`).
Without it the watcher logs a warning and runs poll-only.

**Notes:**

- Every network lookup the order path would otherwise make (tick size, neg
  risk, exchange version) is pre-warmed at startup, so when the change lands
  signing is purely local and only the order POST hits the network.
- Orders are pre-signed, but never once and held: a v2 signed order embeds its
  signing timestamp, so one background thread re-signs every waiting market's
  order on a short interval, and a stale payload is signed again at fire time.
  `--no-presign` turns this off.
- The process shortens Python's thread switch interval to 0.5ms. At the
  default 5ms, a fire thread woken by the tick change waited a median 31ms
  (217ms worst case) for another busy thread to give up the interpreter lock.
- The order endpoint can briefly lag the tick change, so an early rejection is
  expected rather than fatal - `--retry-window` keeps retrying through it.
- Gamma's `outcomePrices` is a mid/last price and disagrees with the order book
  often enough to matter (0.99 against a real best bid of 0.98 was observed
  while building this), so every price that gates a decision comes from the
  book, never from Gamma.
- Every update is routed to its market by asset id. A market's `price_change`
  messages cover both complementary outcomes, and the other side of a 99c
  market sits at 1c, so an update for a token outside the batch is dropped.
- If a market is already at the target tick on startup, it is not watched (in
  a named run it is skipped and the rest are still watched). Override with
  `--fire-if-already`, which fires that market's order straight away.
- A `--price` that is already legal at `--from-tick` is rejected up front: no
  tick change is needed to place it.

### Reward Screener

`reward_screener.py` ranks the markets that pay Polymarket liquidity rewards
for one farming strategy, and stores them in `farm.db` (SQLite) for you to
approve or reject. It is read-only. It places, cancels, and signs nothing, and
it needs no credentials.

The strategy targets markets whose favorite outcome trades at 90c or more. You
rest a favorite bid just under the midpoint, and a fill there is wanted when
your own probability beats the price. You also rest a longshot bid far below
the longshot midpoint. Above a 0.90 midpoint Polymarket only pays for
two-sided liquidity, so the longshot bid exists to make the favorite bid
score. Each market lands in one of two modes:

- `floor`: the longshot bid scores at 0.2c or less, so its cost and fill risk
  are negligible.
- `budget`: the longshot bid must sit higher to score. Its capital is naked
  risk, which you cap per market when you approve it.

`reward_farmer.py` reads the approved rows. The screener never changes
your decisions, so rescanning is always safe.

```bash
python reward_screener.py                     # same as 'scan' with defaults
python reward_screener.py scan --mode floor --top 20 --json scan.json
python reward_screener.py list --status approved
python reward_screener.py approve <slug-or-condition-id> --p-true 0.97 --budget 25
python reward_screener.py approve <slug-or-condition-id> --p-true 0.97 --capital 200
python reward_screener.py reject <slug-or-condition-id> --note "resolution risk"
```

A scan takes about a minute. Most of that is `/rewards/markets/current`, which
pages slowly. It prints one line per stage with the counts it kept and the
reasons it skipped the rest, then a table sorted by rank.

Key `scan` options:

- `--min-fav` (default 0.90) is the lowest favorite midpoint to consider.
- `--expiry-buffer-days` (7) stops farming this many days before the market
  ends. `--min-window-days` (30) is the fewest farmable days a market needs.
- `--horizon-cap-days` (90) caps the days counted toward rank.
- `--capital` (500) is the USD behind the favorite bid. `--bid-offset` (0.01)
  is how far below the midpoint it sits.
- `--min-low-score` (0.10) is the lowest per-share score the longshot bid may
  have. The screener picks the lowest longshot price that reaches it.
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
| `cap$` | Capital both bids tie up |
| `APR%` | `est$/d` divided by `cap$`, times 365 |
| `days` | Days left to farm, after the expiry buffer |
| `ahead` | Shares of longshot bids priced above yours, divided by your longshot size. A seller has to fill all of them before reaching your bid, so a low value means more fill risk on the leg you do not want filled |
| `vol24h` | 24-hour volume from Gamma |
| `H` | `H` when Gamma reports holding rewards enabled, `-` when not, `?` when unknown |

Rank is daily yield times the days left, capped at `--horizon-cap-days`. That
is the reward return you can expect over the farm horizon, so long-dated
markets rank higher than short ones with the same yield.

`approve` takes your probability that the favorite wins (`--p-true`) and the
longshot risk budget (`--budget`, default $25). `--capital` sets the USD
behind this market's favorite bid, in place of the farmer's `--capital`.
Without it, a market keeps the capital it already has, or uses the farmer's
default if it has none. `approve` warns, but still approves,
when the planned favorite bid is at or above `--p-true`, because a fill would
lose money. It also warns when a `budget` market's longshot leg needs more
than the budget.

#### Approximations

- The midpoint drops whole price levels smaller than the reward minimum size.
  Polymarket's adjusted midpoint applies the cutoff per order, so a level of
  many small orders counts here but not there.
- Competing score treats every resting order in the book as one maker. Above a
  0.90 midpoint that overstates the competition, so your estimated share is
  conservative. Your own resting orders also count as competition.
- The two outcome books are exact mirrors, so the screener reads only the
  favorite's book.
- In a wide book the planned favorite bid becomes the best bid and pulls the
  midpoint toward itself. The bid is then priced two offsets under the best
  ask, which puts it one offset under the midpoint it creates. Both legs and
  the competition are scored against that shifted midpoint.
- `est$/d` assumes the book stays as it was at scan time, and that no other
  farmer reacts to your orders. Thin books show very high APRs that will not
  survive competition, so read them as a ranking, not a forecast.

The `markets` table in `farm.db` is the interface to the farmer (see
[The farm.db contract](#the-farmdb-contract)). A scan updates the metrics,
rank, end date, and `rewards_active` flag. It never touches your decisions:
`status`, `p_true`, `naked_budget_usd`, `note`, `paused`, `naked_paused`, or
`capital_usd`. A market that leaves the reward set gets `rewards_active = 0`
and keeps its status. Retiring markets is the farmer's job.

### Reward Farmer

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
python reward_farmer.py --live --heartbeat    # real orders with the dead-man switch
```

Each cycle prints one summary line per market, then one line per action:

```text
[16:36:06] some-market: QUOTING mid 96.9c Q1 308.1 Q2 308.1 | hold 0 cancel 0 place 2
[16:36:06]   PLACE  FAV_BID    94.9c x    526.87  score 0.585  some-market
[16:36:06]   PLACE  LONG_BID    0.1c x    735.88  score 0.419  some-market
[16:36:11] some-market: QUOTING mid 96.9c Q1 308.1 Q2 308.1 | hold 2 cancel 0 place 0
```

`score` is the per-share reward score at the current midpoint. `Q1` and `Q2`
are the planned score on each side. Polymarket pays on the smaller one.

#### The four slots

Above a 0.90 midpoint Polymarket pays only for two-sided liquidity:
min(Q_one, Q_two). Q_one sums favorite bids and longshot asks. Q_two sums
favorite asks and longshot bids. The farmer can rest one order in each of
four slots per market:

| Slot | Order | Side | Purpose |
| --- | --- | --- | --- |
| `FAV_BID` | Buy the favorite | Q_one | The main quote. A fill is wanted, because the bid never prices above your `p_true` minus `--ev-margin`. Uses cash. |
| `LONG_ASK` | Sell the longshot | Q_one | Sells longshot shares from an unwanted fill. Priced at the mirror of the favorite bid, so it scores the same. |
| `FAV_ASK` | Sell the favorite | Q_two | Exits favorite shares above their average cost plus `--min-capture`, which frees the capital again. |
| `LONG_BID` | Buy the longshot | Q_two | Exists only to make Q_one count. On a `floor` market it rests at 0.1c to 0.2c and almost never fills. On a `budget` market it rests higher, and its cost plus any longshot shares held stays within the market's approved budget. |

Q_two comes from the covered `FAV_ASK` first, and only the remainder from the
naked `LONG_BID`. When the budget leaves Q_two short of Q_one, the favorite
bid shrinks to match, because score on one side beyond the other earns
nothing. A bid smaller than the market's reward minimum size is dropped,
because it would carry risk without scoring.

The farmer never buys at market. All inventory comes from passive fills of
these orders.

#### Market lifecycle

Each market is in one of five states:

- `QUOTING`: orders rest as planned.
- `COOLDOWN`: the favorite midpoint fell by `--drop-threshold` within
  `--drop-window` seconds. All orders come down for `--cooldown` seconds.
- `BELOW_RANGE`: the midpoint is below `--min-fav`. All orders come down.
  Quoting resumes once the midpoint is 0.5c above `--min-fav`.
- `PAUSED`: you set `paused = 1` on the market. All orders come down. When
  you clear it, the market goes through the checks above again, so it can go
  straight to `COOLDOWN` or `BELOW_RANGE` instead of `QUOTING`.
- `RETIRED`: the market's rewards were removed, it closed or stopped
  accepting orders, you no longer approve it, or it reached its end date
  minus `--expiry-buffer-days`. All orders are cancelled. In live mode the
  database row becomes `retired` with a note. A row you rejected keeps its
  `rejected` status. Retirement wins over a pause.

Every cycle the farmer re-reads the approved rows, their controls, the
settings, and the commands from `farm.db`. A new approval, a rejection, a
pause, or a setting therefore takes effect within one `--interval`. The farmer
reads positions every 60 seconds and each market's CLOB record (reward terms,
tick, trading status) every 5 minutes.

#### Pausing, settings, and stopping

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
seconds options end in `_s`: `capital`, `bid_offset`, `ask_offset`,
`min_capture`, `ev_margin`, `min_fav`, `min_low_score`, `keep_ratio`,
`drop_threshold`, `drop_window_s`, `cooldown_s`, `naked_cooldown_s`, and
`expiry_buffer_days`. Each value is a JSON number. `farm_db.put_setting`
refuses an unknown key or an out-of-range value. A bad row written another
way is ignored, with an `ALERT` event. When the effective config changes, the
farmer logs a `SETTINGS` event with the old and new values. A changed setting
moves the targets at once, but a resting order that still passes the keep
checks below stays where it is.

To stop the farmer, insert a `stop` row into `commands`. The farmer reads it
on its next cycle, stamps `consumed_at` and `result`, and shuts down the same
way as on Ctrl+C. A stop sent while no farmer runs is marked expired when the
next farmer starts, so it does not stop that farmer.

#### Safety mechanisms

- Every order is post-only, so a fill is always a maker fill.
- Every order is good-till-date. It expires at the market's end date minus
  `--expiry-buffer-days`, even if the farmer dies.
- The favorite bid never prices above `p_true - --ev-margin`.
- `--capital` caps each market's favorite exposure. Favorite shares already
  held count against it at cost.
- The approved budget caps the naked longshot leg: the resting longshot bid
  plus longshot shares held, at cost.
- A longshot fill (your longshot shares went up) prints an `ALERT` and pauses
  the longshot bid for `--naked-cooldown` seconds. Without it, the favorite
  bid shrinks to whatever the covered favorite ask still balances.
- An order that is still close to its target stays in place. It must sit on
  the current tick, score at least `--keep-ratio` of the target, keep a size
  within -20% and +25% of the target, and stay within the price cap or
  budget. Otherwise it is replaced. This keeps queue position and avoids
  churning orders on every small move of the midpoint.
- An order the CLOB rejects, such as a post-only cross or an unfunded bid,
  backs off that market's slot for 30 seconds. Each further rejection doubles
  the wait, up to 15 minutes, and an accepted order resets it. The backoff is
  logged once per rejection.
- In live mode, Ctrl+C, SIGTERM, or a `stop` command cancels every order on
  the managed markets before the farmer exits. A normal `--once` exit leaves
  the orders resting.

#### Positions and existing orders

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

#### Key options

| Option | Default | Meaning |
| --- | --- | --- |
| `--db` | `farm.db` | SQLite file written by the screener |
| `--live` | off | Sign and post real orders |
| `--once` | off | Run one cycle and exit |
| `--interval` | 5 | Seconds between cycles |
| `--capital` | 500 | USD per market behind the favorite bid, held favorite shares included at cost |
| `--bid-offset` | 0.01 | Favorite bid distance below the midpoint |
| `--ask-offset` | 0.01 | Favorite ask distance above the midpoint |
| `--min-capture` | 0.005 | Favorite ask minimum above its average cost |
| `--ev-margin` | 0.01 | The favorite bid stays at or below `p_true` minus this |
| `--min-fav` | 0.90 | Stop quoting below this favorite midpoint |
| `--min-low-score` | 0.10 | Minimum per-share score of the longshot bid |
| `--keep-ratio` | 0.8 | Keep an order that scores at least this share of its target |
| `--drop-threshold` | 0.03 | Midpoint fall that starts a cooldown |
| `--drop-window` | 60 | Seconds over which that fall is measured |
| `--cooldown` | 600 | Seconds without orders after a midpoint drop |
| `--naked-cooldown` | 3600 | Seconds without a longshot bid after a longshot fill |
| `--expiry-buffer-days` | 7 | Retire this many days before the end date |
| `--heartbeat` | off | Live only. See the warning below |

#### The heartbeat warning

`--heartbeat` posts a heartbeat to Polymarket every 5 seconds. If the
heartbeats then stop for 10 seconds, because the farmer crashed, the network
dropped, or the machine slept, Polymarket cancels **all** open orders owned by
these API credentials. That may include orders placed by your other tools.
Polymarket does not document whether the scope is the API key or the whole
account. Without `--heartbeat`, a dead farmer leaves its orders resting until
their good-till-date expiry.

#### Going live

1. Run a scan and approve the markets you want, with your `--p-true` and
   `--budget` for each.
2. Run a dry run on the same database and read the output for a few cycles.
   Check the prices, sizes, and the `holding` lines.
3. Make sure the wallet holds enough USDC. At startup the live farmer compares
   your collateral balance with the favorite bids it plans and warns if the
   balance is short.
4. Start `python reward_farmer.py --live`. It loads the private key the same
   way the other tools do (environment or Bitwarden) and locks Bitwarden again
   on exit.
5. Stop it with Ctrl+C or a `stop` command, which cancel the orders on the
   managed markets.

#### The farm.db contract

`farm_db.py` owns the schema and every SQL statement the tools run against
`farm.db`. Opening the file migrates an older one in place: missing columns
are added with `ALTER TABLE` and missing tables are created, so opening it
twice changes nothing. The file runs in WAL mode with a 5-second busy
timeout, because the farmer and the console write at the same time.

Each column has exactly one writer process:

| Table | Writer | Purpose |
| --- | --- | --- |
| `markets` | Screener: scan columns. Console or CLI: `status`, `p_true`, `naked_budget_usd`, `note`, `paused`, `naked_paused`, `capital_usd`. Farmer, live only: `baseline_fav`, `baseline_long` | Candidate markets, your decisions, and the per-market controls |
| `farm_state` | Farmer | One row per managed market, replaced every cycle: phase and reason, midpoints, Q1 and Q2, capital, naked USD in use, and JSON for the position, the four slots (resting order, target, live score, decision and reason), the favorite book inside the reward band with your orders marked, backoffs, and `scoring_pct` (live only) |
| `farmer_status` | Farmer | One row: mode, pid, start time, last cycle, interval, heartbeat, managed count, effective config, and `stopped_at` once it exits |
| `farm_events` | Farmer | Changes only: `PLACE`, `CANCEL`, `FILL`, `PHASE`, `ALERT`, `REJECT`, `BACKOFF`, `RETIRE`, `START`, `STOP`, `SETTINGS`. Rows older than 30 days are deleted at startup |
| `settings` | Console or CLI | Overrides of the farmer's command-line options |
| `commands` | Console inserts, farmer stamps `consumed_at` and `result` | One-off requests. `stop` is the only kind |
| `rewards_daily` | Farmer, live only | Your reward earnings per market per day, fetched every 10 minutes |
| `farm_mids` | Farmer | At most one row per managed market per minute, in dry run too: the midpoint, the book midpoint, and the prices the `FAV_BID` and `LONG_BID` rest at after that cycle (NULL when the slot rests nothing). Rows older than 7 days are deleted at startup. The console draws its midpoint chart from it |

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

## Important Notes

### Token Allowances

If you're using an EOA (Externally Owned Account) or MetaMask wallet, you must set token allowances before trading. This is not required for email/Magic wallets.

**Required approvals:**

1. **USDC** (`0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`)
   - Approve for: `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`
   - Approve for: `0xC5d563A36AE78145C45a50134d48A1215220f80a`
   - Approve for: `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`

2. **Conditional Tokens** (`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`)
   - Approve for the same three contracts above

See the [Polymarket documentation](https://docs.polymarket.com/developers/CLOB/authentication) for more details on setting allowances.

### Error Handling

- The restore script will continue processing even if some orders fail
- Each failure is logged with details about what went wrong
- Common failure reasons:
  - Insufficient balance
  - Market no longer available
  - Invalid token ID
  - Network/API errors

### Snapshot File Format

The snapshot file is a JSON file with the following structure:

```json
{
  "metadata": {
    "timestamp": "2025-01-24T14:30:22.123456",
    "total_orders": 5,
    "snapshot_version": "1.0"
  },
  "orders": [
    {
      "asset_id": "0x1234...",
      "price": "0.45",
      "original_size": "10.0",
      "side": "BUY",
      "expiration": "0",
      "order_type": "GTC",
      ...
    },
    {
      "asset_id": "0x5678...",
      "price": "0.55",
      "original_size": "5.0",
      "side": "SELL",
      "expiration": "1767225599",
      "order_type": "GTD",
      ...
    },
    ...
  ]
}
```

**Key fields:**
- `expiration`: Unix timestamp in seconds. `0` means GTC (Good-Til-Cancelled), any positive value means GTD (Good-Til-Date)
- The restore script preserves the original expiration when recreating orders

## Troubleshooting

### "Missing required environment variables"
- Make sure you've created a `.env` file from `.env.example`
- Verify that `POLYMARKET_FUNDER_ADDRESS` is set
- For the private key, either:
  - Set `POLYMARKET_PRIVATE_KEY` in your `.env` file, OR
  - Configure Bitwarden by setting `BW_POLYMARKET_ITEM_NAME` or `BW_POLYMARKET_ITEM_ID` in your `.env` file

### "Error connecting to Polymarket"
- Check your internet connection
- Verify your credentials are correct
- Ensure the API endpoint is accessible

### "Trading restricted in your region" (403)
- The submitting IP is in a blocked jurisdiction; the check is IP-based and
  happens at order submission
- Confirm the VPN is carrying **this process**: per-app split tunnelling
  matches the interpreter's exact path, so a venv or a second install needs
  its own rule, and a process already running when the rule changed keeps
  its old routing
- Verify with `python -c "import httpx; print(httpx.get('https://clob.polymarket.com/cdn-cgi/trace').text)"` - run it
  with the *same* interpreter and check the `loc=` line
- Exits whose API access is unrestricted: Ireland, Japan, Malta, the
  Netherlands. Most obvious alternatives (UK, France, Germany, Italy, Poland,
  Singapore, New Zealand, Canada, US) are close-only on the API too

### 451 Unavailable For Legal Reasons
- Distinct from the 403 above: this is the edge refusing the request before
  the API sees it, and it can hit an exit address in a country that appears
  on no published restriction list - shared VPN IPs get flagged on their own
- Reconnecting to a different server, even in the same country, usually
  clears it; the block follows the address, not the jurisdiction
- Hosts can disagree: `gamma-api` returning 451 while `clob` is fine breaks
  the picker and `--slug` but leaves `--token-id` working
- Full list: https://docs.polymarket.com/developers/CLOB/geoblock

### "Insufficient balance" errors during restore
- Make sure you have enough USDC and conditional tokens
- Check that token allowances are set (for EOA/MetaMask wallets)

### Orders fail to restore
- Some orders may fail if market conditions have changed
- Check the error messages in the summary for specific reasons
- You can manually review and recreate failed orders if needed

### Using dry-run mode
- Use `--dry-run` to test restoration without submitting orders
- Dry-run still requires API connection to check existing orders
- Useful for validating snapshot files before actual restoration

## References

- [py-clob-client GitHub](https://github.com/Polymarket/py-clob-client)
- [Polymarket CLOB Documentation](https://docs.polymarket.com/developers/CLOB)

