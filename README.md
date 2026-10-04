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

