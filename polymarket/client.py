"""An authenticated CLOB client from a private key and the environment."""

import os

from py_clob_client_v2.client import ClobClient

from polymarket.hosts import CLOB_HOST


class ConfigError(Exception):
    """The environment does not describe an account to trade from."""


def connect(private_key, client_class=ClobClient):
    """A client_class for private_key, signed in with its derived API key.

    Reads POLYMARKET_FUNDER_ADDRESS, POLYMARKET_SIGNATURE_TYPE (default 1),
    POLYMARKET_CHAIN_ID (default 137) and POLY_BUILDER_CODE when called. Raises
    ConfigError when the funder is missing or a number does not parse. Deriving
    the API key is one authenticated request, and its failure propagates.
    """
    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
    if not funder:
        raise ConfigError("POLYMARKET_FUNDER_ADDRESS is not set")
    kwargs = {
        "host": CLOB_HOST,
        "chain_id": _int("POLYMARKET_CHAIN_ID", 137),
        "key": private_key,
        "signature_type": _int("POLYMARKET_SIGNATURE_TYPE", 1),
        "funder": funder,
    }
    builder_code = os.getenv("POLY_BUILDER_CODE")
    if builder_code:
        kwargs["builder_config"] = {"builderCode": builder_code}
    client = client_class(**kwargs)
    client.set_api_creds(client.create_or_derive_api_key())
    return client


def _int(name, default):
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{name} is not a whole number: {value!r}") from None
