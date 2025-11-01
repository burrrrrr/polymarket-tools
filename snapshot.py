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

from py_clob_client.clob_types import OpenOrderParams

from config import load_config, create_client
from key_loader import cleanup_key_source


def snapshot_orders(client):
    """Fetch all open orders and return them as a list."""
    try:
        print("Fetching open orders...")
        open_orders = client.get_orders(OpenOrderParams())
        print(f"Found {len(open_orders)} open order(s)")
        return open_orders
    except Exception as e:
        print(f"Error fetching orders: {e}")
        sys.exit(1)


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

