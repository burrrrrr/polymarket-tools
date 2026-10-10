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
rest a favorite bid just under the midpoint, priced where its rewards minus
the cost of its fills are highest (see [The fill-cost model](#the-fill-cost-model)).
You also rest a longshot bid far below
the longshot midpoint. Above a 0.90 midpoint Polymarket only pays for
two-sided liquidity, so the longshot bid exists to make the favorite bid
score. Each market lands in one of two modes:

- `floor`: the longshot bid scores at 0.2c or less, and the shares that
  balance the favorite bid there fit the naked budget, so its fill risk is
  negligible.
- `budget`: the longshot bid must sit higher to score or to fit the budget.
  Its capital is naked risk, which you cap per market when you approve it.

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
- `--capital` (500) is the USD behind the favorite bid.
- `--hurdle-rate` (0.40) sets what a filled share costs, the same setting
  the farmer has. The scan has no p_true, so it costs a fill against the
  book midpoint.
- `--tape-markets` (100) is how many of the top markets read their fill
  history. The fill prior is fit from the 60 randomly drawn markets' histories
  instead, and needs at least 20 of them.
- `--max-lead` (0.005) is the most the favorite bid may sit above the best bid
  already in the book. 0 joins the best bid. The bid also stays one tick under
  the lowest ask.
- `--naked-budget` (10) is the USD the longshot bid may cost. The longshot
  bid takes the cheapest price whose size, enough to balance the favorite
  bid's score and at least the reward minimum, fits this budget. A cheaper
  bid sits deeper in the book, behind more longshot bids, so it fills less
  often. Set it to the budget you would approve with, since the farmer prices
  the bid the same way within the approved budget.
- `--min-low-score` (0.10) caps the longshot bid's price. The bid never sits
  above the cheapest price scoring at least this per share. When no cheaper
  price balances the favorite bid within the budget, the bid sits at that
  price, sized to the budget, and the favorite bid shrinks to the score that
  reaches. A market with no such price gets no longshot bid.
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
longshot risk budget (`--budget`, default $25). `--capital` sets the USD
behind this market's favorite bid, in place of the farmer's `--capital`.
Without it, a market keeps the capital it already has, or uses the farmer's
default if it has none. `approve` warns, but still approves,
when the planned favorite bid is at or above `--p-true`, because a fill would
lose money. It also warns when the planned longshot leg costs more than the
budget.

#### Decided markets

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

#### Approximations

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

#### The fill-cost model

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

#### The four slots

Above a 0.90 midpoint Polymarket pays only for two-sided liquidity:
min(Q_one, Q_two). Q_one sums favorite bids and longshot asks. Q_two sums
favorite asks and longshot bids. The farmer can rest one order in each of
four slots per market:

| Slot | Order | Side | Purpose |
| --- | --- | --- | --- |
| `FAV_BID` | Buy the favorite | Q_one | The main quote. It rests at the price with the best net of its fill cost (see [The fill-cost model](#the-fill-cost-model)), never above your `p_true` minus `--ev-margin`, and only when that net beats what the orders earn without it. Uses cash. |
| `LONG_ASK` | Sell the longshot | Q_one | Sells longshot shares from an unwanted fill. Priced at the mirror of the favorite bid, so it scores the same. |
| `FAV_ASK` | Sell the favorite | Q_two | Exits favorite shares above their average cost plus `--min-capture`, which frees the capital again. |
| `LONG_BID` | Buy the longshot | Q_two | Exists only to make Q_one count. It rests at the cheapest price whose size, enough to balance Q_one, keeps its cost plus any longshot shares held within the market's approved budget, and never above the cheapest price scoring `--min-low-score`. On a `floor` market that is 0.1c to 0.2c, where it almost never fills. On a `budget` market it rests higher. |

Q_two comes from the covered `FAV_ASK` first, and only the remainder from the
naked `LONG_BID`. When no price up to the cheapest one scoring
`--min-low-score` balances Q_one within the budget, the `LONG_BID` sits at
that price, sized to the budget, and the favorite bid shrinks to match, because score on one side beyond the other earns nothing. Near the
edge of the reward band a small move of the midpoint changes the
`LONG_BID`'s score, and so its size, a lot, so it is replaced more often
there. At 0.1c that only moves it further back in the queue. A bid smaller than the market's reward minimum size is dropped,
because it would carry risk without scoring.

The farmer never buys at market. All inventory comes from passive fills of
these orders.

#### Market lifecycle

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
  budget. Otherwise it is replaced. A favorite bid is judged by its net
  instead of its score. It stays while its price is still a candidate, its
  net trails the best price's by no more than `1 - --keep-ratio` of it (or
  $0.02/day, if more), and it beats the orders without it. This keeps queue position and avoids
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
| `--max-lead` | 0.005 | Most the favorite bid may sit above the best bid already in the book. 0 joins it |
| `--ask-offset` | 0.01 | Favorite ask distance above the midpoint |
| `--min-capture` | 0.005 | Favorite ask minimum above its average cost |
| `--ev-margin` | 0.02 | The favorite bid stays at or below `p_true` minus this |
| `--min-fav` | 0.90 | Stop quoting below this favorite midpoint |
| `--min-low-score` | 0.10 | The longshot bid never sits above the cheapest price scoring this per share |
| `--keep-ratio` | 0.8 | Keep an order that scores at least this share of its target, a favorite bid that nets at least this share of the best price's |
| `--hurdle-rate` | 0.40 | Yearly return capital must beat; a filled share costs its price minus what it sells for, discounted at this rate over the weeks the sale takes |
| `--drop-threshold` | 0.03 | Fall in the favorite's ask, where its asks add up to three reward minimums, that starts a cooldown |
| `--drop-window` | 60 | Seconds over which that fall is measured |
| `--cooldown` | 600 | Seconds without orders after an ask drop |
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
| `markets` | Screener: scan columns. Console: `rewards_active` of a candidate, from the Review page's live check. Console or CLI: `status`, `p_true`, `naked_budget_usd`, `note`, `paused`, `naked_paused`, `capital_usd`. Farmer, live only: `baseline_fav`, `baseline_long` | Candidate markets, your decisions, and the per-market controls |
| `farm_state` | Farmer | One row per managed market, replaced every cycle: phase and reason, midpoints, Q1 and Q2, capital, naked USD in use, and JSON for the position, the four slots (resting order, target, live score, decision and reason), the favorite book inside the reward band with your orders marked, backoffs, and `scoring_pct` (live only) |
| `farmer_status` | Farmer | One row: mode, pid, start time, last cycle, interval, heartbeat, managed count, effective config, and `stopped_at` once it exits |
| `farm_events` | Farmer | Changes only: `PLACE`, `CANCEL`, `FILL`, `PHASE`, `ALERT`, `REJECT`, `BACKOFF`, `RETIRE`, `START`, `STOP`, `SETTINGS`. Rows older than 30 days are deleted at startup |
| `settings` | Console or CLI | Overrides of the farmer's command-line options. Keys starting with `scan.` are not farmer options, and the farmer never reports them as unknown settings: `scan.params` holds the console's last scan parameters, and every scan writes `scan.last`, its funnel, and `scan.fill_prior`, the fill prior it fit when at least 20 sampled histories read, which the next scan, the farmer and the console value fills on |
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

### Farm Console

`farm_console` is a local web console for the reward farmer. It shows what
the farmer is doing, live, and it is where you run scans, approve markets, and
control the farmer. Every change it makes goes through `farm_db` into
`farm.db`, and the farmer picks it up on its next cycle.

```bash
python -m farm_console                    # production farm.db on port 8787
python -m farm_console --db scratch.db    # another database
python -m farm_console --port 8788        # another port
```

Then open http://127.0.0.1:8787. Run it from the repository root (or with
the repository on `PYTHONPATH`) so Python can find the `farm_console`
package. `--db` defaults to the repository's `farm.db`, the production
database, wherever you start the console from. A relative `--db` path is
read from the current directory.

Run it next to the farmer on the same database, in its own terminal. Neither
process needs the other: the console shows the last state the farmer
published, with a banner when the farmer is not running. The console reads
`POLYMARKET_FUNDER_ADDRESS` from `.env` to show your holdings on the Review
page. It never reads or handles a private key.

Pages:

| Page | Shows | Actions |
| --- | --- | --- |
| Overview (`/`) | Totals (markets quoting, capital in favorite bids, naked longshot bids against the budgets, farm inventory, the screener's $/day estimate, today's earnings), the alerts that need you, the tracked markets, and the latest events | Each alert links to its action: Resume, Edit budget, Edit p_true, or the farmer status |
| Discover (`/discover`) | The scan parameters, the last scan's funnel (listed, passed the prefilter, assessed on the book, and the skip reasons), and the stored markets by rank, 50 per page. Filter by mode, text in the question or slug, status, and minimum days to the farmer's cutoff | Run a scan |
| Review (`/review/<condition_id>`) | The plan from the last scan: the reward band with the scanned best bid and ask and your planned bids, each planned order with why it is priced that way, the yield figures (gross, fills/day, fill cost and net APR), the resolution rules from Gamma, and what your wallet already holds, which becomes the farmer's baseline. `/review` opens the best-ranked undecided candidate | Approve with p_true, naked budget, and capital; reject with a note; retire an approved market; re-approve a retired one |
| Live markets (`/markets?phase=`) | One card per managed market: phase, midpoint, the reward band with your bids marked, the four slots, Q1/Q2 and naked budget bars, inventory, and the expiry cutoff. Approved markets the farmer has not picked up yet show as waiting | Pause or resume quoting, pause or resume the naked leg, edit p_true and budget, retire |
| Market detail (`/markets/<condition_id>`) | Stat tiles, a 24-hour midpoint chart with the band, your bids and fill and phase markers, the slots, the book inside the band, the position, the lifecycle, fills, and the market's events | The same controls as the card |
| Activity (`/activity?tab=`) | `rewards`: earnings per day and market against the screener estimate. `fills`: each fill with its approximate edge against `p_true`. `inventory`: wallet, baseline, farm shares, and exit orders. `log`: every farm event, filterable by kind and market | None |
| Settings (`/settings`) | The farmer process (mode, pid, start, last cycle, interval, heartbeat) and every setting with its effective value and where it comes from: your override, the farmer's own value, or the built-in default | Stop the farmer, save or reset settings, cancel all managed orders |

#### Running a scan from the console

The Discover page runs the same scan as `python reward_screener.py scan`, in a
background thread of the console. It takes two to three minutes. The page shows
the current stage while it runs, then the counts and the duration, or the
error. Only one scan runs at a time: a second request while one runs gets
"A scan is already in progress". The form starts from the parameters of the
last scan run from the console, or the screener's defaults. A scan stores its
rows and its funnel in one transaction and never changes your decisions.

#### Decisions and controls

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
  the capped bid creates. It also warns when your naked budget is below the
  planned longshot leg, which the scan priced within its own naked budget.
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
  page shows when the farmer consumed it.
- **Settings** forms check each value with the same rules the farmer uses
  and write only the fields you changed, all in one transaction. A value
  that fails shows its error next to the field and nothing is saved.
  **Reset** deletes the override, so the farmer's own value applies again.

There is no live switch. Live mode needs the private key, which the farmer
unlocks from Bitwarden in its own terminal, so you start it there:
`python reward_farmer.py --live --db farm.db`.

#### Security

The console has no login and controls real orders, so it must only be
reachable from your own machine, and only by its own pages:

- It binds `127.0.0.1` only. To reach it from another machine, use an SSH
  tunnel. Never change the bind address.
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
- Every decision and control is a POST. Two GET requests write, the Review
  page's live check and its skip to the next quotable candidate, and they
  only record a candidate's `rewards_active` as Polymarket reports it. No
  request can choose that value.

`farm_console/security.py` holds all of these checks, as a middleware in
front of every route.

#### Live updates

The pages update without a reload. `GET /stream` is a server-sent event
stream that polls `farm.db` once a second and sends three signals with no
HTML: `cycle` when the farmer finishes a cycle, `farm-event` when a new event
appears, and `status` when the farmer starts or stops running. Each live region
of a page requests its own HTML fragment from `/partials/...` when its signal
arrives, using htmx and its SSE extension. After a market control, the regions
that show that market reload at once. There is no client-side state and no
chart JavaScript: the charts are SVG drawn on the server.

The alerts come from a rule table in `farm_console/views.py`: cooldown, below
range, paused, naked budget at 99% or more, a favorite bid scoring under 0.10,
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

#### What the terms mean

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

