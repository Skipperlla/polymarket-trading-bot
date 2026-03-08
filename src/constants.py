"""Shared constants for Polymarket (Polygon mainnet). Used across the project."""

# ──────────────────────────────────────────────────────────────────────
# Contract addresses (Polygon mainnet)
# ──────────────────────────────────────────────────────────────────────

# USDC.e (bridged USDC) on Polygon – 6 decimals
USDCe_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Conditional Token Framework (CTF) contract
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Polymarket Exchange (order matching)
POLYMARKET_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# Neg-Risk Exchange (for neg-risk markets)
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

# Neg-Risk Adapter
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# ──────────────────────────────────────────────────────────────────────
# API endpoints
# ──────────────────────────────────────────────────────────────────────

# CLOB (Central Limit Order Book) API
CLOB_API_HOST = "https://clob.polymarket.com"

# Gamma API (market data, metadata)
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
GAMMA_MARKETS_URL = f"{GAMMA_API_BASE}/markets"
GAMMA_EVENTS_URL = f"{GAMMA_API_BASE}/events"

# CLOB WebSocket (real-time data)
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ──────────────────────────────────────────────────────────────────────
# Chain config
# ──────────────────────────────────────────────────────────────────────

# Polygon mainnet
POLYGON_CHAIN_ID = 137

# Polygon Amoy testnet
AMOY_CHAIN_ID = 80002

# ──────────────────────────────────────────────────────────────────────
# USDC decimals
# ──────────────────────────────────────────────────────────────────────

USDC_DECIMALS = 6
USDC_UNIT = 10**USDC_DECIMALS  # 1_000_000 = 1 USDC

# ──────────────────────────────────────────────────────────────────────
# Signature types for ClobClient
# ──────────────────────────────────────────────────────────────────────

SIGNATURE_TYPE_EOA = 0  # MetaMask / hardware wallet (direct EOA)
SIGNATURE_TYPE_MAGIC = 1  # Email / Magic wallet (delegated signing)
SIGNATURE_TYPE_PROXY = 2  # Browser wallet proxy (Gnosis Safe proxy)

# ──────────────────────────────────────────────────────────────────────
# Trading defaults
# ──────────────────────────────────────────────────────────────────────

DEFAULT_ORDER_PRICE = 0.46
DEFAULT_ORDER_SIZE = 5.0
DEFAULT_MIN_ORDER_SIZE = 5  # Polymarket minimum order size
DEFAULT_PRICE_TICK = 0.01  # Minimum price increment

# ──────────────────────────────────────────────────────────────────────
# BTC 5-minute market constants
# ──────────────────────────────────────────────────────────────────────

BTC_5M_INTERVAL = 300  # 5 minutes in seconds
BTC_5M_SLUG_PREFIX = "will-btc-go-up-or-down-5-min-"
