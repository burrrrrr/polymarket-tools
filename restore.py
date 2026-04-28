#!/usr/bin/env python3
"""
Polymarket Order Restore Script

This script reads a snapshot JSON file and recreates all orders that were
saved in the snapshot. Useful for restoring orders that were automatically
cancelled (e.g., due to balance drops).
"""

import argparse
import json
import os
import sys
from datetime import datetime

from py_clob_client_v2.clob_types import OrderArgsV2, OrderType, OpenOrderParams

from config import load_config, create_client
from key_loader import cleanup_key_source


def get_order_token_id(order):
    """Extract token id across snapshot/API variants."""
    return (
        order.get("asset_id")
        or order.get("token_id")
        or order.get("tokenID")
        or order.get("tokenId")
    )


def get_order_size(order):
    """Extract order size across snapshot/API variants."""
    return (
        order.get("original_size")
        or order.get("size")
        or order.get("amount")
        or order.get("makerAmount")
        or 0
    )


def parse_expiration(order):
    """Extract expiration from snapshot order with safe coercion."""
    expiration_raw = (
        order.get("expiration")
        if "expiration" in order
        else order.get("expires_at", 0)
    )
    if isinstance(expiration_raw, str):
        return int(expiration_raw) if expiration_raw else 0
    return int(expiration_raw) if expiration_raw else 0


def get_market_ref(order):
    """Extract market identifier for display/caching."""
    return (
        order.get("market")
        or order.get("market_id")
        or order.get("condition_id")
        or order.get("conditionId")
        or ""
    )


def load_snapshot(filename):
    """Load and validate snapshot file."""
    if not os.path.exists(filename):
        print(f"Error: Snapshot file not found: {filename}")
        sys.exit(1)

    try:
        with open(filename, "r") as f:
            snapshot = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in snapshot file: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error reading snapshot file: {e}")
        sys.exit(1)

    # Validate snapshot structure
    if "orders" not in snapshot:
        print("Error: Invalid snapshot format. Missing 'orders' field.")
        sys.exit(1)

    orders = snapshot["orders"]
    metadata = snapshot.get("metadata", {})

    print(f"Loaded snapshot from: {filename}")
    if metadata:
        print(f"Snapshot taken: {metadata.get('timestamp', 'unknown')}")
        print(f"Total orders in snapshot: {metadata.get('total_orders', len(orders))}")

    return orders


def convert_side(side_str):
    """Convert side string to canonical BUY/SELL values."""
    side_upper = side_str.upper() if isinstance(side_str, str) else str(side_str).upper()
    if side_upper == "BUY":
        return "BUY"
    elif side_upper == "SELL":
        return "SELL"
    else:
        raise ValueError(f"Invalid side value: {side_str}")


def format_expiration(expiration):
    """Format expiration timestamp for display."""
    if expiration <= 0:
        return "GTC (no expiration)"
    try:
        exp_dt = datetime.fromtimestamp(expiration)
        return f"GTD (expires: {exp_dt.strftime('%Y-%m-%d %H:%M:%S')})"
    except (ValueError, OSError):
        return f"GTD (expires: {expiration})"


def get_existing_orders(client):
    """Fetch all existing open orders from Polymarket."""
    try:
        print("Fetching existing open orders...")
        existing_orders = client.get_open_orders(OpenOrderParams())
        print(f"Found {len(existing_orders)} existing open order(s)")
        return existing_orders
    except Exception as e:
        print(f"Warning: Error fetching existing orders: {e}")
        print("Continuing without filtering existing orders...")
        return []


def get_market_name(client, market_id, market_cache=None):
    """Get market name from market ID, with caching."""
    if market_cache is None:
        market_cache = {}

    if market_id in market_cache:
        return market_cache[market_id]

    try:
        market_info = client.get_market(market_id)
        # Market name is typically in 'question' or 'title' field
        market_name = (
            market_info.get("question") or
            market_info.get("title") or
            market_info.get("marketName") or
            f"Market {market_id[:20]}..."
        )
        market_cache[market_id] = market_name
        return market_name
    except Exception:
        # If we can't fetch market info, return a truncated market ID
        market_name = f"Market {market_id[:20]}..."
        market_cache[market_id] = market_name
        return market_name


def get_best_bid_ask(client, token_id, bid_ask_cache=None):
    """Get best bid and ask prices for a token, with caching.

    Returns:
        tuple: (best_bid, best_ask) where:
            - best_bid: float or None (highest buy price)
            - best_ask: float or None (lowest sell price)
    """
    if bid_ask_cache is None:
        bid_ask_cache = {}

    if token_id in bid_ask_cache:
        return bid_ask_cache[token_id]

    try:
        order_book = client.get_order_book(token_id)

        # Extract best bid (highest buy price) from bids
        best_bid = None
        if hasattr(order_book, 'bids') and order_book.bids:
            # Find the highest price among all bids
            bid_prices = [float(bid.price) for bid in order_book.bids]
            best_bid = max(bid_prices) if bid_prices else None
        elif isinstance(order_book, dict):
            bids = order_book.get('bids', [])
            if bids:
                bid_prices = []
                for bid in bids:
                    if isinstance(bid, dict):
                        bid_prices.append(float(bid.get('price', bid)))
                    else:
                        bid_prices.append(float(bid))
                best_bid = max(bid_prices) if bid_prices else None

        # Extract best ask (lowest sell price) from asks
        best_ask = None
        if hasattr(order_book, 'asks') and order_book.asks:
            # Find the lowest price among all asks
            ask_prices = [float(ask.price) for ask in order_book.asks]
            best_ask = min(ask_prices) if ask_prices else None
        elif isinstance(order_book, dict):
            asks = order_book.get('asks', [])
            if asks:
                ask_prices = []
                for ask in asks:
                    if isinstance(ask, dict):
                        ask_prices.append(float(ask.get('price', ask)))
                    else:
                        ask_prices.append(float(ask))
                best_ask = min(ask_prices) if ask_prices else None

        result = (best_bid, best_ask)
        bid_ask_cache[token_id] = result
        return result
    except Exception as e:
        # If we can't fetch order book, return None values (fail open)
        # This ensures network issues don't block all order restoration
        result = (None, None)
        bid_ask_cache[token_id] = result
        return result


def would_fill_immediately(client, order, bid_ask_cache=None):
    """Check if an order would fill immediately based on current market prices.

    Args:
        client: ClobClient instance
        order: Order dict with 'asset_id'/'token_id', 'side', and 'price'
        bid_ask_cache: Optional cache dict for bid/ask prices

    Returns:
        bool: True if order would fill immediately, False otherwise
    """
    token_id = get_order_token_id(order)
    if not token_id:
        return False  # Can't check without token ID

    side_str = order.get("side", "").upper()
    if side_str not in ("BUY", "SELL"):
        return False  # Invalid side

    try:
        order_price = float(order.get("price", 0))
        if order_price <= 0:
            return False  # Invalid price
    except (ValueError, TypeError):
        return False  # Invalid price format

    # Get best bid/ask for this token
    best_bid, best_ask = get_best_bid_ask(client, token_id, bid_ask_cache)

    # Check if order would fill immediately
    if side_str == "BUY":
        # BUY order fills immediately if order_price >= best_ask
        if best_ask is not None and order_price >= best_ask:
            return True
    elif side_str == "SELL":
        # SELL order fills immediately if order_price <= best_bid
        if best_bid is not None and order_price <= best_bid:
            return True

    return False


def normalize_order_key(order):
    """Create a normalized key for order comparison."""
    token_id = get_order_token_id(order) or ""
    side = order.get("side", "").upper()
    price = float(order.get("price", 0))
    size = float(get_order_size(order))

    # Round to avoid floating point precision issues
    price = round(price, 8)
    size = round(size, 8)

    return (token_id, side, price, size)


def filter_existing_orders(snapshot_orders, existing_orders):
    """Filter out orders from snapshot that already exist."""
    if not existing_orders:
        return snapshot_orders, []

    # Create a set of normalized keys for existing orders
    existing_keys = {normalize_order_key(order) for order in existing_orders}

    orders_to_restore = []
    orders_already_exist = []

    for order in snapshot_orders:
        order_key = normalize_order_key(order)
        if order_key in existing_keys:
            orders_already_exist.append(order)
        else:
            orders_to_restore.append(order)

    return orders_to_restore, orders_already_exist


def restore_order(client, order, order_index, dry_run=False):
    """Restore a single order from snapshot."""
    try:
        # Extract order fields
        # The API returns 'asset_id' but OrderArgs expects 'token_id'
        token_id = get_order_token_id(order)
        price = float(order.get("price", 0))
        size = float(get_order_size(order))
        side_str = order.get("side", "")

        # Extract expiration (can be string or int, default to 0 for GTC orders)
        expiration = parse_expiration(order)

        if not token_id:
            raise ValueError("Missing token_id/asset_id in order")
        if price <= 0:
            raise ValueError(f"Invalid price: {price}")
        if size <= 0:
            raise ValueError(f"Invalid size: {size}")
        if not side_str:
            raise ValueError("Missing side in order")

        side = convert_side(side_str)

        # Determine order type based on expiration
        # GTD (Good-Til-Date) if expiration > 0, otherwise GTC (Good-Til-Cancelled)
        order_type = OrderType.GTD if expiration > 0 else OrderType.GTC

        # Create order with expiration
        order_args = OrderArgsV2(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
            expiration=expiration,
        )

        if dry_run:
            # In dry-run mode, just validate the order without submitting
            order_type_str = "GTD" if expiration > 0 else "GTC"
            return {
                "success": True,
                "order_id": "DRY-RUN",
                "token_id": token_id,
                "price": price,
                "size": size,
                "side": side_str,
                "expiration": expiration,
                "order_type": order_type_str,
            }

        # Sign and post order with appropriate order type
        signed_order = client.create_order(order_args)
        response = client.post_order(signed_order, order_type)

        order_id = response.get("id") if isinstance(response, dict) else None
        order_type_str = "GTD" if expiration > 0 else "GTC"
        return {
            "success": True,
            "order_id": order_id,
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": side_str,
            "expiration": expiration,
            "order_type": order_type_str,
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "order": order,
            "order_index": order_index,
        }


def restore_orders(client, orders, dry_run=False):
    """Restore all orders from snapshot."""
    total = len(orders)
    successful = []
    failed = []
    skipped_immediate_fill = []
    market_cache = {}  # Cache market names to avoid repeated API calls
    bid_ask_cache = {}  # Cache bid/ask prices to avoid repeated API calls

    action = "Validating" if dry_run else "Restoring"
    print(f"\n{action} {total} order(s)...")
    if dry_run:
        print("(DRY RUN - No orders will be submitted)")
    print("-" * 60)

    for i, order in enumerate(orders, 1):
        # Show expiration info if available
        expiration = parse_expiration(order)
        exp_info = format_expiration(expiration)

        print(f"[{i}/{total}] Processing order ({exp_info})...", end=" ", flush=True)

        # Check if order would fill immediately
        if would_fill_immediately(client, order, bid_ask_cache):
            skipped_immediate_fill.append(order)
            # Extract order details for skip message
            market_id = get_market_ref(order)
            token_id = get_order_token_id(order) or "unknown"
            side = order.get("side", "unknown")
            size = get_order_size(order) or "unknown"
            price = order.get("price", "unknown")

            # Get market name if we have a market ID
            if market_id:
                market_name = get_market_name(client, market_id, market_cache)
            else:
                market_name = f"Token {token_id[:20]}..."

            # Get best bid/ask to show what price would cause immediate fill
            best_bid, best_ask = get_best_bid_ask(client, token_id, bid_ask_cache)
            matching_price = best_ask if side.upper() == "BUY" else best_bid

            print(f"⊘ Skipped (would fill immediately)")
            print(f"    Details: {side} {size} shares @ ${price} (Market: {market_name})")
            if matching_price is not None:
                price_type = "ask" if side.upper() == "BUY" else "bid"
                print(f"    Reason: Best {price_type} is ${matching_price}")
            continue

        result = restore_order(client, order, i, dry_run=dry_run)

        if result["success"]:
            successful.append(result)
            order_id = result.get('order_id', 'N/A')
            if dry_run:
                print(f"✓ Valid (would restore)")
            else:
                print(f"✓ Success (Order ID: {order_id})")
        else:
            failed.append(result)
            # Extract order details for error message
            order_info = result.get("order", {})
            market_id = get_market_ref(order_info)
            token_id = get_order_token_id(order_info) or "unknown"
            side = order_info.get("side", "unknown")
            size = get_order_size(order_info) or "unknown"
            price = order_info.get("price", "unknown")

            # Get market name if we have a market ID
            if market_id:
                market_name = get_market_name(client, market_id, market_cache)
            else:
                market_name = f"Token {token_id[:20]}..."

            print(f"✗ Failed: {result['error']}")
            print(f"    Details: {side} {size} shares @ ${price} (Market: {market_name})")

    return successful, failed, skipped_immediate_fill


def print_summary(successful, failed, skipped=None, skipped_immediate_fill=None, dry_run=False, client=None):
    """Print restoration summary."""
    print("\n" + "=" * 60)
    title = "DRY RUN SUMMARY" if dry_run else "RESTORATION SUMMARY"
    print(title)
    print("=" * 60)

    total_in_snapshot = (
        len(successful) + len(failed) +
        (len(skipped) if skipped else 0) +
        (len(skipped_immediate_fill) if skipped_immediate_fill else 0)
    )
    print(f"Total orders in snapshot: {total_in_snapshot}")

    if skipped:
        print(f"Skipped (already exist): {len(skipped)}")

    if skipped_immediate_fill:
        print(f"Skipped (would fill immediately): {len(skipped_immediate_fill)}")

    print(f"Total orders processed: {len(successful) + len(failed)}")
    if dry_run:
        print(f"Valid orders (would restore): {len(successful)}")
    else:
        print(f"Successfully restored: {len(successful)}")
    print(f"Failed: {len(failed)}")

    if skipped:
        print("\nSkipped orders (already exist on Polymarket):")
        for order in skipped[:10]:  # Show first 10
            token_id = get_order_token_id(order) or "unknown"
            side = order.get("side", "unknown")
            size = get_order_size(order) or "unknown"
            price = order.get("price", "unknown")
            print(
                f"  - {side} {size} @ ${price} "
                f"(Token: {token_id[:20]}...)"
            )
        if len(skipped) > 10:
            print(f"  ... and {len(skipped) - 10} more")

    if skipped_immediate_fill:
        print("\nSkipped orders (would fill immediately):")
        market_cache = {}  # Cache market names to avoid repeated API calls
        bid_ask_cache = {}  # Cache bid/ask prices to avoid repeated API calls
        for order in skipped_immediate_fill[:10]:  # Show first 10
            market_id = get_market_ref(order)
            token_id = get_order_token_id(order) or "unknown"
            side = order.get("side", "unknown")
            size = get_order_size(order) or "unknown"
            price = order.get("price", "unknown")

            # Get market name if we have a market ID and client
            if market_id and client:
                market_name = get_market_name(client, market_id, market_cache)
            else:
                market_name = f"Token {token_id[:20]}..."

            # Get best bid/ask to show matching price
            best_bid, best_ask = get_best_bid_ask(client, token_id, bid_ask_cache) if client else (None, None)
            matching_price = best_ask if side.upper() == "BUY" else best_bid

            print(f"  - {side} {size} shares @ ${price} (Market: {market_name})")
            if matching_price is not None:
                price_type = "ask" if side.upper() == "BUY" else "bid"
                print(f"    Best {price_type}: ${matching_price}")
        if len(skipped_immediate_fill) > 10:
            print(f"  ... and {len(skipped_immediate_fill) - 10} more")

    if successful:
        label = "Valid orders (would restore):" if dry_run else "Successfully restored orders:"
        print(f"\n{label}")
        for result in successful:
            order_id = result.get('order_id', 'N/A')
            expiration = result.get('expiration', 0)
            exp_info = format_expiration(expiration)
            if dry_run:
                print(
                    f"  - {result['side']} {result['size']} @ ${result['price']:.4f} "
                    f"(Token: {result['token_id'][:20]}..., {exp_info})"
                )
            else:
                print(
                    f"  - {result['side']} {result['size']} @ ${result['price']:.4f} "
                    f"(Token: {result['token_id'][:20]}..., ID: {order_id}, {exp_info})"
                )

    if failed:
        print("\nFailed orders:")
        market_cache = {}  # Cache market names to avoid repeated API calls
        for result in failed:
            order_info = result.get("order", {})
            market_id = get_market_ref(order_info)
            token_id = get_order_token_id(order_info) or "unknown"
            side = order_info.get("side", "unknown")
            size = get_order_size(order_info) or "unknown"
            price = order_info.get("price", "unknown")

            # Get market name if we have a market ID and client
            if market_id and client:
                market_name = get_market_name(client, market_id, market_cache)
            else:
                market_name = f"Token {token_id[:20]}..."

            print(f"  - Order #{result['order_index']}: {side} {size} shares @ ${price}")
            print(f"    Market: {market_name}")
            print(f"    Error: {result['error']}")


def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(
        description="Restore Polymarket orders from a snapshot file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python restore.py orders_snapshot_20250124_143022.json
  python restore.py orders_snapshot_20250124_143022.json --dry-run
        """
    )
    parser.add_argument(
        "snapshot_file",
        help="Path to the snapshot JSON file"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate orders without submitting them (will still check existing orders)"
    )

    args = parser.parse_args()

    print("Polymarket Order Restore Tool")
    if args.dry_run:
        print("(DRY RUN MODE)")
    print("=" * 60)

    # Load snapshot first (doesn't require API connection)
    snapshot_orders = load_snapshot(args.snapshot_file)

    if len(snapshot_orders) == 0:
        print("No orders found in snapshot.")
        # Cleanup key source (e.g., lock vault if Bitwarden was used)
        if cleanup_key_source():
            print("\nKey source secured.")
        return

    # Load configuration and create client (needed for checking existing orders)
    config = load_config()
    client = None
    skipped_orders = []
    failed = []

    try:
        # Create client (needed to check existing orders, even in dry-run mode)
        try:
            client = create_client(config)
            print("Connected to Polymarket CLOB")
        except Exception as e:
            print(f"Error connecting to Polymarket: {e}")
            sys.exit(1)

        # Fetch existing orders and filter out duplicates
        existing_orders = get_existing_orders(client)
        orders_to_restore, skipped_orders = filter_existing_orders(snapshot_orders, existing_orders)

        if skipped_orders:
            print(f"\nSkipping {len(skipped_orders)} order(s) that already exist on Polymarket")

        if len(orders_to_restore) == 0:
            print("\nAll orders from snapshot already exist on Polymarket. Nothing to restore.")
            return

        # Restore orders
        successful, failed, skipped_immediate_fill = restore_orders(client, orders_to_restore, dry_run=args.dry_run)

        # Print summary
        print_summary(successful, failed, skipped=skipped_orders, skipped_immediate_fill=skipped_immediate_fill, dry_run=args.dry_run, client=client)

        if failed:
            sys.exit(1)
    finally:
        # Cleanup key source (e.g., lock vault if Bitwarden was used)
        # This always runs, even if there's an exception or early return
        if cleanup_key_source():
            print("\nKey source secured.")


if __name__ == "__main__":
    main()

