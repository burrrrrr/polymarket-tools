#!/usr/bin/env python3
"""
Polymarket Configuration Module

This module provides shared configuration loading and client creation functions
for Polymarket scripts. It handles loading configuration from environment
variables and creating authenticated ClobClient instances.
"""

import os
import sys

from py_clob_client_v2.client import ClobClient

from key_loader import get_private_key


def _build_client_kwargs(config):
    """Build kwargs for CLOB client construction."""
    kwargs = {
        "host": config["host"],
        "chain_id": config["chain_id"],
        "key": config["private_key"],
        "signature_type": config["signature_type"],
        "funder": config["funder"],
    }
    builder_code = os.getenv("POLY_BUILDER_CODE")
    if builder_code:
        kwargs["builder_config"] = {"builderCode": builder_code}
    return kwargs


def load_config():
    """Load configuration from environment variables and key loader."""
    # Load private key from available sources (env var or Bitwarden)
    private_key = get_private_key()

    # Load other configuration from environment variables
    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
    signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
    host = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
    chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))

    if not private_key or not funder:
        print("Error: Missing required configuration.")
        if not private_key:
            print("  - Private key could not be loaded (check POLYMARKET_PRIVATE_KEY or Bitwarden config)")
        if not funder:
            print("  - Please set POLYMARKET_FUNDER_ADDRESS environment variable")
        sys.exit(1)

    return {
        "private_key": private_key,
        "funder": funder,
        "signature_type": signature_type,
        "host": host,
        "chain_id": chain_id,
    }


def create_client(config):
    """Create and authenticate ClobClient (v2 SDK)."""
    base_kwargs = _build_client_kwargs(config)
    client = ClobClient(**base_kwargs)
    resp = client.create_or_derive_api_key()
    client.set_api_creds(resp)
    return client

