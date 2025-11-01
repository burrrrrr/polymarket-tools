#!/usr/bin/env python3
"""
Private Key Loader - Multi-Source Abstraction

This module provides a unified interface for loading private keys from multiple
sources, with automatic fallback support. It supports:
- Environment variables (simplest, works everywhere)
- Bitwarden (secure password manager)
- Extensible for other password managers

Priority order (unless overridden):
1. Environment variable (POLYMARKET_PRIVATE_KEY)
2. Bitwarden (if available and configured)
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    load_dotenv(env_path)
else:
    # Also try loading from current directory
    load_dotenv()

# Track if Bitwarden was used to load the key
_bitwarden_used = False


def _get_private_key_from_env():
    """Get private key from environment variable."""
    return os.getenv("POLYMARKET_PRIVATE_KEY")


def _get_private_key_from_bitwarden():
    """Get private key from Bitwarden."""
    global _bitwarden_used
    try:
        from bitwarden_loader import get_private_key_from_bitwarden, check_bitwarden_cli

        # Check if Bitwarden CLI is available before trying to use it
        if not check_bitwarden_cli():
            return None

        # Mark that Bitwarden was used
        _bitwarden_used = True
        return get_private_key_from_bitwarden()
    except (ImportError, SystemExit):
        # Bitwarden not available or loader exits on error, return None to allow fallback
        return None
    except Exception:
        # Any other error, return None to allow fallback
        return None


def get_private_key():
    """
    Get private key from available sources with priority-based fallback.

    Priority order (unless POLYMARKET_KEY_SOURCE is set):
    1. Environment variable (POLYMARKET_PRIVATE_KEY)
    2. Bitwarden (if available and configured)

    If POLYMARKET_KEY_SOURCE is set to "bitwarden", it will try Bitwarden first.
    If POLYMARKET_KEY_SOURCE is set to "env", it will only try environment variable.

    Returns:
        str: The private key, or None if not found from any source

    Raises:
        SystemExit: If no private key can be loaded and error messages are printed
    """
    # Check if source is explicitly specified
    key_source = os.getenv("POLYMARKET_KEY_SOURCE", "").lower()

    if key_source == "env":
        # Only try environment variable
        private_key = _get_private_key_from_env()
        if not private_key:
            print("Error: POLYMARKET_PRIVATE_KEY environment variable not set.", file=sys.stderr)
            print("Please set POLYMARKET_PRIVATE_KEY or use a different key source.", file=sys.stderr)
            sys.exit(1)
        return private_key

    elif key_source == "bitwarden":
        # Only try Bitwarden
        private_key = _get_private_key_from_bitwarden()
        if not private_key:
            print("Error: Could not load private key from Bitwarden.", file=sys.stderr)
            print("Please ensure Bitwarden CLI is installed and configured.", file=sys.stderr)
            sys.exit(1)
        return private_key

    else:
        # Default priority: env first, then Bitwarden
        # Try environment variable first
        private_key = _get_private_key_from_env()
        if private_key:
            return private_key

        # Fall back to Bitwarden
        private_key = _get_private_key_from_bitwarden()
        if private_key:
            return private_key

        # If neither worked, show helpful error message
        print("Error: Could not load private key from any source.", file=sys.stderr)
        print("", file=sys.stderr)
        print("Please use one of the following methods:", file=sys.stderr)
        print("  1. Set POLYMARKET_PRIVATE_KEY environment variable", file=sys.stderr)
        print("  2. Configure Bitwarden (set BW_POLYMARKET_ITEM_ID or BW_POLYMARKET_ITEM_NAME)", file=sys.stderr)
        print("  3. Set POLYMARKET_KEY_SOURCE=env to force environment variable only", file=sys.stderr)
        print("  4. Set POLYMARKET_KEY_SOURCE=bitwarden to force Bitwarden only", file=sys.stderr)
        sys.exit(1)


def cleanup_key_source():
    """
    Cleanup function to lock Bitwarden vault if it was used.

    This should be called after operations complete to ensure the vault is locked.
    It's safe to call even if Bitwarden wasn't used - it will simply do nothing.

    Returns:
        bool: True if cleanup succeeded (or wasn't needed), False if it failed
    """
    global _bitwarden_used

    if not _bitwarden_used:
        return False

    try:
        from bitwarden_loader import lock_bitwarden_vault
        success = lock_bitwarden_vault()
        if success:
            print("Bitwarden vault locked successfully.")
        else:
            print("Warning: Failed to lock Bitwarden vault. Please lock it manually.", file=sys.stderr)
        return success
    except ImportError:
        # Bitwarden loader not available
        print("Warning: Bitwarden loader not available, cannot lock vault.", file=sys.stderr)
        return False
    except Exception as e:
        # Any error during locking, but don't fail
        print(f"Warning: Error during Bitwarden vault cleanup: {e}", file=sys.stderr)
        return False

