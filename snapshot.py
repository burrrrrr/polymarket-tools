#!/usr/bin/env python3
"""
Polymarket Order Snapshot Script

This script connects to Polymarket and saves a snapshot of all open orders
to a timestamped JSON file. The snapshot can later be used to restore orders
if they get automatically cancelled.
"""

import json
import os
import sys
from datetime import datetime

from py_clob_client_v2.clob_types import OpenOrderParams

from config import load_config, create_client
from key_loader import cleanup_key_source
from restore import get_market_ref, fetch_market_names_bulk


def snapshot_orders(client):
    """Fetch all open orders and return them as a list."""
    try:
        print("Fetching open orders...")
        open_orders = client.get_open_orders(OpenOrderParams())
        print(f"Found {len(open_orders)} open order(s)")
        return open_orders
    except Exception as e:
        print(f"Error fetching orders: {e}")
        sys.exit(1)


def add_market_names(client, orders):
    """Attach the market name to each order so restore can always display it.

    Names are resolved in bulk via the Gamma API (one request per ~40 unique
    markets) rather than one CLOB request per market, which is fast and avoids
    read timeouts on large snapshots. Anything Gamma doesn't return falls back
    to per-market CLOB lookups. Failures are non-fatal: an order left without a
    name will simply have it resolved at restore time.
    """
    unique_market_ids = {
        get_market_ref(order) for order in orders if get_market_ref(order)
    }
    print(
        f"Fetching market names for {len(unique_market_ids)} unique market(s) "
        f"across {len(orders)} order(s)..."
    )

    # Bulk-resolve via Gamma.
    market_cache = fetch_market_names_bulk(unique_market_ids)

    # Fall back to per-market CLOB lookups for anything Gamma didn't return.
    missing = unique_market_ids - market_cache.keys()
    if missing:
        print(f"Resolving {len(missing)} remaining market(s) individually...")
        for market_id in missing:
            try:
                info = client.get_market(market_id)
                name = (
                    info.get("question")
                    or info.get("title")
                    or info.get("marketName")
                )
                if name:
                    market_cache[market_id] = name
            except Exception:
                # Leave it unresolved; restore.py will fetch it on demand.
                pass

    # Stamp resolved names onto every order.
    for order in orders:
        market_id = get_market_ref(order)
        if market_id and market_id in market_cache:
            order["market_name"] = market_cache[market_id]

    return orders


def save_snapshot(orders):
    """Save orders to a timestamped JSON file."""
    # Create snapshots directory if it doesn't exist
    snapshots_dir = "snapshots"
    os.makedirs(snapshots_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"orders_snapshot_{timestamp}.json"
    filepath = os.path.join(snapshots_dir, filename)

    snapshot = {
        "metadata": {
            "timestamp": datetime.now().isoformat(),
            "total_orders": len(orders),
            "snapshot_version": "1.0",
        },
        "orders": orders,
    }

    try:
        with open(filepath, "w") as f:
            json.dump(snapshot, f, indent=2)
        print(f"Snapshot saved to: {filepath}")
        return filepath
    except Exception as e:
        print(f"Error saving snapshot: {e}")
        sys.exit(1)


def main():
    """Main execution function."""
    print("Polymarket Order Snapshot Tool")
    print("=" * 40)

    # Load configuration
    config = load_config()

    try:
        # Create client
        try:
            client = create_client(config)
            print("Connected to Polymarket CLOB")
        except Exception as e:
            print(f"Error connecting to Polymarket: {e}")
            sys.exit(1)

        # Fetch and save orders
        orders = snapshot_orders(client)

        if len(orders) == 0:
            print("No open orders to snapshot.")
            return

        # Enrich orders with market names so restore can always display them
        add_market_names(client, orders)

        filename = save_snapshot(orders)
        print(f"\nSnapshot complete! Use the following command to restore:")
        # Use forward slashes for display (Python accepts them on all platforms)
        display_path = filename.replace("\\", "/")
        print(f"  python restore.py {display_path}")
    finally:
        # Cleanup key source (e.g., lock vault if Bitwarden was used)
        # This always runs, even if there's an exception or early return
        if cleanup_key_source():
            print("Key source secured.")


if __name__ == "__main__":
    main()

