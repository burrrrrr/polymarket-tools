"""Where Polymarket's services live.

Each host is read from the environment once, when this module is imported, so
an entry point loads .env before it imports anything from this package.
"""

import os

CLOB_HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
GAMMA_HOST = os.getenv("POLYMARKET_GAMMA_HOST", "https://gamma-api.polymarket.com")
DATA_API_HOST = os.getenv("POLYMARKET_DATA_API_HOST", "https://data-api.polymarket.com")
WSS_HOST = os.getenv("POLYMARKET_WSS_HOST", "wss://ws-subscriptions-clob.polymarket.com")
WSS_MARKET_URL = f"{WSS_HOST}/ws/market"
GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
