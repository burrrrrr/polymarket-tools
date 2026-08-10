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

## Installation

1. Ensure you have Python 3.9 or higher installed.

2. Install the required dependencies:

```bash
pip install -r requirements.txt
```

## NixOS Installation

### Prerequisites

- Flake support enabled
- [direnv](https://direnv.net/) (optional, for automatic shell activation)

### Quick Start

```bash
# Enter development environment
nix develop

# Install Python dependencies (first time only)
pip install -r requirements.txt
```

### With direnv (Recommended)

For automatic shell activation when entering the project directory, configure `.envrc`:

```bash
use flake
```

Then run:

```bash
direnv allow
```

Now the development environment activates automatically when you `cd` into the project directory.

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

