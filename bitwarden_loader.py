#!/usr/bin/env python3
"""
Bitwarden CLI Integration for Loading Private Keys

This module provides secure access to private keys stored in Bitwarden
using the Bitwarden CLI (bw). It handles authentication and retrieval
of secure note items.
"""

import json
import os
import re
import subprocess
import sys
import getpass
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    load_dotenv(env_path)
else:
    # Also try loading from current directory
    load_dotenv()


def check_bitwarden_cli():
    """Check if Bitwarden CLI is available."""
    try:
        result = subprocess.run(
            ["bw", "--version"],
            capture_output=True,
            text=True,
            check=True
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def parse_session_token_from_login_output(output):
    """
    Parse the BW_SESSION token from bw login output.

    The login output may contain lines like:
    - $ export BW_SESSION="token_value"
    - $env:BW_SESSION="token_value"
    - BW_SESSION="token_value"

    Args:
        output: The stdout string from bw login command

    Returns:
        str: The session token if found, None otherwise
    """
    if not output:
        return None

    # Pattern to match quoted session tokens in various formats
    # Matches: BW_SESSION="token" or $ export BW_SESSION="token" or $env:BW_SESSION="token"
    patterns = [
        r'BW_SESSION="([^"]+)"',  # Matches BW_SESSION="token"
        r'export\s+BW_SESSION="([^"]+)"',  # Matches export BW_SESSION="token"
        r'\$env:BW_SESSION="([^"]+)"',  # Matches $env:BW_SESSION="token" (Windows)
    ]

    for pattern in patterns:
        match = re.search(pattern, output)
        if match:
            return match.group(1)

    return None


def _check_bitwarden_status():
    """
    Check Bitwarden CLI status.

    Returns:
        str: Status string ("unauthenticated", "locked", "unlocked", etc.) or None if check fails
    """
    try:
        result = subprocess.run(
            ["bw", "status"],
            capture_output=True,
            text=True,
            check=True
        )
        status_data = json.loads(result.stdout)
        return status_data.get("status", "unknown")
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def _sync_vault(session=None):
    """
    Sync Bitwarden vault.

    Args:
        session: Optional session token to use for sync

    Returns:
        bool: True if sync succeeded, False otherwise
    """
    try:
        env = {**os.environ}
        if session:
            env["BW_SESSION"] = session
        subprocess.run(
            ["bw", "sync"],
            env=env,
            capture_output=True,
            check=True
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _login_to_bitwarden():
    """
    Perform Bitwarden login flow.

    Prompts for email and password, attempts login, extracts session token,
    and syncs vault.

    Returns:
        str: Session token if login successful

    Raises:
        SystemExit: If login fails
    """
    print("Bitwarden CLI is not logged in. Please log in to continue.")
    email = input("Enter your Bitwarden email: ")
    password = getpass.getpass("Enter your Bitwarden master password: ")

    try:
        # Login (bw login requires email, password is provided via stdin)
        login_process = subprocess.Popen(
            ["bw", "login", email],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        stdout, stderr = login_process.communicate(input=password + "\n")

        if login_process.returncode != 0:
            print(f"Warning: Automated login failed: {stderr}", file=sys.stderr)
            print("Please run 'bw login' manually first, then run this script again.", file=sys.stderr)
            sys.exit(1)

        # Try to extract session token from login output
        session = parse_session_token_from_login_output(stdout)
        if session:
            # Session token found, use it immediately
            os.environ["BW_SESSION"] = session
            print("Successfully logged in to Bitwarden.")
            _sync_vault(session)
            return session

        # If session token not found in output, sync and check status
        _sync_vault()
        print("Successfully logged in to Bitwarden.")
        return None  # Will need to check status and unlock
    except Exception as e:
        print(f"Error: Failed to log in to Bitwarden: {e}", file=sys.stderr)
        print("Please run 'bw login' manually first, then run this script again.", file=sys.stderr)
        sys.exit(1)


def _unlock_bitwarden_vault():
    """
    Unlock Bitwarden vault.

    Prompts for master password and unlocks the vault.

    Returns:
        str: Session token if unlock successful

    Raises:
        SystemExit: If unlock fails
    """
    print("Bitwarden vault is locked. Please unlock to continue.")
    master_password = getpass.getpass("Enter your Bitwarden master password: ")

    try:
        result = subprocess.run(
            ["bw", "unlock", "--raw"],
            input=master_password,
            text=True,
            capture_output=True,
            check=True
        )
        session = result.stdout.strip()
        print(f"Session: {session}")
        os.environ["BW_SESSION"] = session
        return session
    except subprocess.CalledProcessError as e:
        print(f"Error: Failed to unlock Bitwarden vault: {e.stderr}", file=sys.stderr)
        sys.exit(1)


def _get_session_from_unlocked_vault():
    """
    Attempt to get session token from an already unlocked vault.

    Returns:
        str: Session token if successful, None otherwise
    """
    try:
        result = subprocess.run(
            ["bw", "unlock", "--raw", "--check"],
            capture_output=True,
            text=True,
            check=True
        )
        session = result.stdout.strip()
        if session:
            os.environ["BW_SESSION"] = session
            return session
    except subprocess.CalledProcessError:
        pass
    return None


def _search_bitwarden_item_by_name(item_name, session):
    """
    Search for a Bitwarden item by name.

    Args:
        item_name: Name of the item to search for
        session: Session token to use

    Returns:
        str: Item ID if found

    Raises:
        SystemExit: If search fails or item not found
    """
    try:
        result = subprocess.run(
            ["bw", "list", "items", "--search", item_name],
            env={**os.environ, "BW_SESSION": session},
            capture_output=True,
            text=True,
            check=True
        )
        items = json.loads(result.stdout)
        if not items:
            print(f"Error: No Bitwarden item found matching '{item_name}'", file=sys.stderr)
            sys.exit(1)
        if len(items) > 1:
            print(f"Warning: Multiple items found matching '{item_name}', using first match", file=sys.stderr)
        return items[0]["id"]
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        print(f"Error: Failed to search Bitwarden items: {e}", file=sys.stderr)
        sys.exit(1)


def get_bitwarden_session():
    """
    Get or create a Bitwarden session token.

    This function handles the complete authentication flow:
    1. Check if existing session is valid
    2. Check Bitwarden status
    3. Handle unauthenticated state → login
    4. Handle locked state → unlock
    5. Handle unlocked state → get session

    Returns:
        str: Session token

    Raises:
        SystemExit: If authentication fails
    """
    # Check if session already exists in environment
    session = os.getenv("BW_SESSION")
    if session:
        # Verify session is still valid
        try:
            subprocess.run(
                ["bw", "status"],
                env={**os.environ, "BW_SESSION": session},
                capture_output=True,
                check=True
            )
            return session
        except subprocess.CalledProcessError:
            # Session expired, need to unlock again
            pass

    # Check Bitwarden status
    status = _check_bitwarden_status()

    # If status check failed, attempt login
    if status is None:
        print("Unable to check Bitwarden status. Attempting login...")
        session = _login_to_bitwarden()
        if session:
            return session
        # If login didn't return session, check status again
        status = _check_bitwarden_status()
        if status is None:
            status = "locked"  # Default to locked if we can't determine

    # Handle unauthenticated state
    if status == "unauthenticated":
        session = _login_to_bitwarden()
        if session:
            return session
        # If login didn't return session, check status again after login
        status = _check_bitwarden_status()
        if status is None:
            status = "locked"  # Default to locked if we can't determine

    # Handle locked state
    if status == "locked":
        return _unlock_bitwarden_vault()

    # Handle unlocked state
    if status == "unlocked":
        session = _get_session_from_unlocked_vault()
        if session:
            return session
        # If we couldn't get session from unlocked vault, try unlock
        return _unlock_bitwarden_vault()

    # Fallback: try to unlock anyway
    return _unlock_bitwarden_vault()


def lock_bitwarden_vault():
    """
    Lock the Bitwarden vault and clear the session.

    This function locks the vault and removes the BW_SESSION from the environment
    to ensure the vault is properly secured after operations complete.

    Returns:
        bool: True if vault was locked successfully, False otherwise
    """
    # Check if Bitwarden CLI is available
    if not check_bitwarden_cli():
        print("Warning: Bitwarden CLI not available, cannot lock vault.", file=sys.stderr)
        return False

    # Check vault status first to determine if locking is needed
    status = _check_bitwarden_status()
    session = os.getenv("BW_SESSION")

    # If vault is already locked or unauthenticated, nothing to do
    if status in ("locked", "unauthenticated"):
        # Clear session from environment if it exists
        if "BW_SESSION" in os.environ:
            del os.environ["BW_SESSION"]
        return True

    # If no session and status is unknown, assume already locked
    if not session and status is None:
        return True

    # If we have a session or vault is unlocked, attempt to lock
    if session or status == "unlocked":
        try:
            # Lock the vault
            env = {**os.environ}
            if session:
                env["BW_SESSION"] = session

            result = subprocess.run(
                ["bw", "lock"],
                env=env,
                capture_output=True,
                text=True,
                check=True
            )

            # Verify the lock succeeded by checking status again
            new_status = _check_bitwarden_status()
            if new_status == "locked":
                # Clear the session from environment
                if "BW_SESSION" in os.environ:
                    del os.environ["BW_SESSION"]
                return True
            else:
                # Lock command succeeded but status check shows it's not locked
                print(f"Warning: Bitwarden lock command succeeded but vault status is '{new_status}'", file=sys.stderr)
                # Still clear session
                if "BW_SESSION" in os.environ:
                    del os.environ["BW_SESSION"]
                return False

        except subprocess.CalledProcessError as e:
            # Lock failed - log the error
            error_msg = e.stderr.strip() if e.stderr else "Unknown error"
            print(f"Error: Failed to lock Bitwarden vault: {error_msg}", file=sys.stderr)
            # Try to clear session anyway
            if "BW_SESSION" in os.environ:
                del os.environ["BW_SESSION"]
            return False
        except Exception as e:
            # Any other error
            print(f"Error: Unexpected error while locking Bitwarden vault: {e}", file=sys.stderr)
            # Try to clear session anyway
            if "BW_SESSION" in os.environ:
                del os.environ["BW_SESSION"]
            return False

    # Fallback: clear session and return True (assume already locked)
    if "BW_SESSION" in os.environ:
        del os.environ["BW_SESSION"]
    return True


def get_private_key_from_bitwarden(item_name=None, item_id=None):
    """
    Retrieve the private key from a Bitwarden secure note item.

    Args:
        item_name: Name of the Bitwarden item to search for (optional)
        item_id: ID of the Bitwarden item (optional, takes precedence)

    Returns:
        str: The private key value from the secure note

    Raises:
        SystemExit: If Bitwarden CLI is not available or item not found
    """
    # Check if Bitwarden CLI is available
    if not check_bitwarden_cli():
        print("Error: Bitwarden CLI (bw) is not available.", file=sys.stderr)
        print("Please install it from: https://bitwarden.com/help/cli/", file=sys.stderr)
        sys.exit(1)

    # Get or create session
    session = get_bitwarden_session()

    # Determine which item to retrieve
    if item_id:
        item_identifier = item_id
    elif item_name:
        item_identifier = _search_bitwarden_item_by_name(item_name, session)
    else:
        # Try environment variable for item name/ID
        item_id_env = os.getenv("BW_POLYMARKET_ITEM_ID")
        item_name_env = os.getenv("BW_POLYMARKET_ITEM_NAME")

        if item_id_env:
            item_identifier = item_id_env
        elif item_name_env:
            item_identifier = _search_bitwarden_item_by_name(item_name_env, session)
        else:
            print("Error: No Bitwarden item specified.", file=sys.stderr)
            print("Please set BW_POLYMARKET_ITEM_ID or BW_POLYMARKET_ITEM_NAME", file=sys.stderr)
            print("or pass item_name or item_id to get_private_key_from_bitwarden()", file=sys.stderr)
            sys.exit(1)

    # Retrieve the item
    try:
        result = subprocess.run(
            ["bw", "get", "item", item_identifier],
            env={**os.environ, "BW_SESSION": session},
            capture_output=True,
            text=True,
            check=True
        )
        item = json.loads(result.stdout)

        # Extract private key from secure note
        if item.get("type") == 2:  # Secure Note type
            notes = item.get("notes", "")
            if notes:
                return notes.strip()
            else:
                print("Error: Bitwarden item has no notes content", file=sys.stderr)
                sys.exit(1)
        else:
            print("Error: Bitwarden item is not a secure note", file=sys.stderr)
            sys.exit(1)

    except subprocess.CalledProcessError as e:
        print(f"Error: Failed to retrieve Bitwarden item: {e.stderr}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON response from Bitwarden: {e}", file=sys.stderr)
        sys.exit(1)

