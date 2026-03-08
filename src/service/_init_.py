"""
Polymarket Trading Bot – Service layer.

Exports:
  - PolymarketBot: Unified bot interface (CLOB + relayer + market finder)
  - MarketFinder: Gamma API market discovery
  - TradingEngine: Autonomous trading engine
  - TradingConfig: Engine configuration
  - Strategy: Available trading strategies
"""

from src.service.market_finder import MarketFinder
from src.service.polymarket_bot import PolymarketBot
from src.service.trading_engine import Strategy, TradingConfig, TradingEngine

__all__ = [
    "MarketFinder",
    "PolymarketBot",
    "Strategy",
    "TradingConfig",
    "TradingEngine",
]
