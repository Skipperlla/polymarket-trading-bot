"""
TradingEngine - Autonomous trading engine for Polymarket.

Provides:
  - Configurable trading strategies (value betting, spread capture, momentum)
  - Paper-trading / dry-run mode for safe testing
  - Safety controls: max position, max loss, cooldown timers
  - Continuous market scanning and order management loop
  - Position tracking and P&L calculation
  - Automatic order cancellation on shutdown
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from src.client.poly_client.poly_client import PolyClient
from src.client.poly_relayer_client.poly_relayer_client import PolyRelayerClient
from src.service.market_finder import MarketFinder

logger = logging.getLogger("TradingEngine")


# ──────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────


class Strategy(str, Enum):
    """Available trading strategies."""

    VALUE_BET = "value_bet"
    SPREAD_CAPTURE = "spread_capture"
    MOMENTUM = "momentum"
    BTC_5M = "btc_5m"
    MANUAL = "manual"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class TradingConfig:
    """Configuration for the trading engine."""

    # Strategy
    strategy: Strategy = Strategy.BTC_5M

    # Order sizing
    order_size: float = 5.0  # USDC per order
    max_order_size: float = 50.0  # Max single order size
    order_price: float = (
        0.50  # Default limit price (0.50 = fair value for BTC 5m up/down)
    )

    # Risk controls
    max_total_exposure: float = 100.0  # Max total USDC in open positions
    max_positions: int = 5  # Max concurrent positions
    max_daily_loss: float = 25.0  # Stop trading after this much loss in a day
    min_balance: float = 5.0  # Min USDC balance to keep trading
    stop_loss_pct: float = 0.30  # Exit if position drops 30%

    # Market filters
    min_liquidity: float = 500.0  # Min market liquidity
    min_volume_24h: float = 100.0  # Min 24h volume
    max_spread: float = 0.10  # Max spread (ask - bid)
    min_price: float = 0.05  # Don't buy below this price
    max_price: float = 0.95  # Don't buy above this price

    # Timing
    scan_interval_seconds: int = (
        30  # How often to scan for markets (30s for 5m markets)
    )
    order_ttl_seconds: int = (
        240  # Cancel unfilled orders after 4 min (before 5m market closes)
    )
    cooldown_after_trade_seconds: int = 5  # Wait between trades (fast for 5m cycles)

    # Paper trading
    paper_trading: bool = True  # If True, don't place real orders
    paper_balance: float = 1000.0  # Starting paper balance

    # Logging
    verbose: bool = True

    @classmethod
    def from_env(cls) -> "TradingConfig":
        """Build config from environment variables."""
        return cls(
            strategy=Strategy(os.getenv("STRATEGY", "btc_5m")),
            order_size=float(os.getenv("ORDER_SIZE", "5.0")),
            max_order_size=float(os.getenv("MAX_ORDER_SIZE", "50.0")),
            order_price=float(os.getenv("ORDER_PRICE", "0.50")),
            max_total_exposure=float(os.getenv("MAX_TOTAL_EXPOSURE", "100.0")),
            max_positions=int(os.getenv("MAX_POSITIONS", "5")),
            max_daily_loss=float(os.getenv("MAX_DAILY_LOSS", "25.0")),
            min_balance=float(os.getenv("MIN_BALANCE", "5.0")),
            stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "0.30")),
            min_liquidity=float(os.getenv("MIN_LIQUIDITY", "500.0")),
            min_volume_24h=float(os.getenv("MIN_VOLUME_24H", "100.0")),
            max_spread=float(os.getenv("MAX_SPREAD", "0.10")),
            min_price=float(os.getenv("MIN_PRICE", "0.05")),
            max_price=float(os.getenv("MAX_PRICE", "0.95")),
            scan_interval_seconds=int(os.getenv("SCAN_INTERVAL", "30")),
            order_ttl_seconds=int(os.getenv("ORDER_TTL", "240")),
            cooldown_after_trade_seconds=int(os.getenv("COOLDOWN_SECONDS", "5")),
            paper_trading=os.getenv("PAPER_TRADING", "true").lower()
            in (
                "true",
                "1",
                "yes",
            ),
            paper_balance=float(os.getenv("PAPER_BALANCE", "1000.0")),
            verbose=os.getenv("VERBOSE", "true").lower() in ("true", "1", "yes"),
        )


@dataclass
class Position:
    """Represents an open position."""

    market_id: str
    condition_id: str
    token_id: str
    side: str  # "BUY" or "SELL"
    outcome_label: str  # e.g. "Yes", "No", "Up", "Down"
    question: str
    entry_price: float
    size: float  # Number of shares
    cost: float  # Total USDC spent
    order_id: Optional[str] = None
    timestamp: float = 0.0
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    is_paper: bool = False

    def update_pnl(self, current_price: float) -> None:
        """Update unrealised P&L based on current market price."""
        self.current_price = current_price
        if self.side == "BUY":
            self.unrealized_pnl = (current_price - self.entry_price) * self.size
        else:
            self.unrealized_pnl = (self.entry_price - current_price) * self.size

    @property
    def pnl_pct(self) -> float:
        """P&L as a percentage of cost."""
        if self.cost == 0:
            return 0.0
        return self.unrealized_pnl / self.cost


@dataclass
class TradeRecord:
    """Record of a completed trade."""

    market_id: str
    condition_id: str
    token_id: str
    side: str
    outcome_label: str
    question: str
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    entry_time: float
    exit_time: float
    is_paper: bool = False


@dataclass
class EngineState:
    """Runtime state of the trading engine."""

    running: bool = False
    paper_balance: float = 1000.0
    positions: Dict[str, Position] = field(default_factory=dict)
    open_orders: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    trade_history: List[TradeRecord] = field(default_factory=list)
    daily_pnl: float = 0.0
    total_pnl: float = 0.0
    trades_today: int = 0
    last_scan_time: float = 0.0
    last_trade_time: float = 0.0
    start_time: float = 0.0
    errors: List[str] = field(default_factory=list)
    status_message: str = "Idle"

    @property
    def total_exposure(self) -> float:
        """Total USDC currently in open positions."""
        return sum(p.cost for p in self.positions.values())

    @property
    def position_count(self) -> int:
        return len(self.positions)

    def reset_daily(self) -> None:
        """Reset daily counters (call at midnight)."""
        self.daily_pnl = 0.0
        self.trades_today = 0


# ──────────────────────────────────────────────────────────────────────
# Trading Engine
# ──────────────────────────────────────────────────────────────────────


class TradingEngine:
    """
    Autonomous trading engine for Polymarket.

    Scans markets, evaluates opportunities, places orders, and manages
    positions according to the configured strategy and risk controls.
    """

    def __init__(
        self,
        poly_client: Optional[PolyClient] = None,
        relayer_client: Optional[PolyRelayerClient] = None,
        market_finder: Optional[MarketFinder] = None,
        config: Optional[TradingConfig] = None,
        on_trade_callback: Optional[Callable] = None,
        on_status_callback: Optional[Callable] = None,
    ):
        """
        Initialise the trading engine.

        Args:
            poly_client: Initialised PolyClient for order placement.
            relayer_client: Optional PolyRelayerClient for on-chain operations.
            market_finder: MarketFinder instance for market discovery.
            config: TradingConfig (defaults to env-based config).
            on_trade_callback: Called when a trade is executed (for Telegram notifications etc).
            on_status_callback: Called when engine status changes.
        """
        self.client = poly_client
        self.relayer = relayer_client
        self.finder = market_finder or MarketFinder()
        self.config = config or TradingConfig.from_env()
        self.state = EngineState(paper_balance=self.config.paper_balance)
        self.on_trade = on_trade_callback
        self.on_status = on_status_callback
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the trading engine loop."""
        if self.state.running:
            logger.warning("Engine already running.")
            return

        if not self.config.paper_trading and (
            not self.client or not self.client.is_available()
        ):
            logger.error(
                "Cannot start real trading without a working PolyClient. "
                "Set PAPER_TRADING=true or provide valid PRIVATE_KEY."
            )
            return

        self._stop_event.clear()
        self.state.running = True
        self.state.start_time = time.time()
        self.state.status_message = (
            "Running (paper)" if self.config.paper_trading else "Running (LIVE)"
        )

        mode = "PAPER" if self.config.paper_trading else "LIVE"
        logger.info(
            "Trading engine started [%s] strategy=%s size=%.1f",
            mode,
            self.config.strategy.value,
            self.config.order_size,
        )

        self._notify_status(
            f"🚀 Engine started [{mode}] – strategy: {self.config.strategy.value}"
        )
        self._task = asyncio.create_task(self._main_loop())

    async def stop(self) -> None:
        """Gracefully stop the trading engine."""
        if not self.state.running:
            return

        logger.info("Stopping trading engine…")
        self.state.status_message = "Stopping…"
        self._stop_event.set()

        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                self._task.cancel()

        # Cancel all open orders on shutdown (live mode)
        if not self.config.paper_trading and self.client and self.client.is_available():
            await self._cancel_all_open_orders()

        self.state.running = False
        self.state.status_message = "Stopped"
        self._notify_status("🛑 Engine stopped.")
        logger.info("Trading engine stopped. Total P&L: $%.2f", self.state.total_pnl)

    @property
    def is_running(self) -> bool:
        return self.state.running

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _main_loop(self) -> None:
        """Core engine loop: scan → evaluate → trade → manage → repeat."""
        while not self._stop_event.is_set():
            try:
                # Safety checks
                if not self._safety_checks():
                    self.state.status_message = "Paused (safety limit)"
                    await self._sleep(30)
                    continue

                # 1. Scan for market opportunities
                self.state.status_message = "Scanning markets…"
                opportunities = await asyncio.to_thread(self._scan_markets)
                self.state.last_scan_time = time.time()

                # 2. Evaluate and maybe trade
                if opportunities:
                    for opp in opportunities:
                        if self._stop_event.is_set():
                            break

                        if not self._can_open_position():
                            break

                        await self._evaluate_and_trade(opp)

                        # Cooldown between trades
                        await self._sleep(self.config.cooldown_after_trade_seconds)

                # 3. Manage existing positions
                await self._manage_positions()

                # 4. Clean up stale orders
                await self._cleanup_stale_orders()

                # 5. Update status
                self.state.status_message = (
                    f"Scanning… | {self.state.position_count} positions | "
                    f"P&L: ${self.state.total_pnl:+.2f}"
                )

            except Exception as exc:
                err_msg = f"Main loop error: {exc}"
                logger.exception(err_msg)
                self.state.errors.append(err_msg)
                if len(self.state.errors) > 50:
                    self.state.errors = self.state.errors[-50:]
                await self._sleep(10)

            # Wait for next scan interval
            await self._sleep(self.config.scan_interval_seconds)

    async def _sleep(self, seconds: float) -> None:
        """Sleep that respects the stop event."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------
    # Safety checks
    # ------------------------------------------------------------------

    def _safety_checks(self) -> bool:
        """Run safety checks. Returns False if trading should be paused."""
        # Daily loss limit
        if self.state.daily_pnl <= -self.config.max_daily_loss:
            logger.warning(
                "Daily loss limit reached (%.2f). Pausing.", self.state.daily_pnl
            )
            return False

        # Balance check (paper mode)
        if self.config.paper_trading:
            if self.state.paper_balance < self.config.min_balance:
                logger.warning(
                    "Paper balance too low (%.2f). Pausing.", self.state.paper_balance
                )
                return False

        return True

    def _can_open_position(self) -> bool:
        """Check if we can open another position."""
        if self.state.position_count >= self.config.max_positions:
            return False
        if self.state.total_exposure >= self.config.max_total_exposure:
            return False
        # Cooldown
        if (
            time.time() - self.state.last_trade_time
            < self.config.cooldown_after_trade_seconds
        ):
            return False
        return True

    # ------------------------------------------------------------------
    # Market scanning
    # ------------------------------------------------------------------

    def _scan_markets(self) -> List[Dict[str, Any]]:
        """
        Scan for trading opportunities based on the current strategy.

        Returns a list of opportunity dicts, each containing:
          - market: full market dict from Gamma API
          - token_ids: extracted token IDs
          - signal: trading signal info (side, price, score)
        """
        strategy = self.config.strategy

        if strategy == Strategy.BTC_5M:
            return self._scan_btc_5m()
        elif strategy == Strategy.VALUE_BET:
            return self._scan_value_bets()
        elif strategy == Strategy.SPREAD_CAPTURE:
            return self._scan_spread_capture()
        elif strategy == Strategy.MOMENTUM:
            return self._scan_momentum()
        else:
            return self._scan_value_bets()  # Default fallback

    def _scan_value_bets(self) -> List[Dict[str, Any]]:
        """
        Value betting: find markets where one outcome is underpriced.

        Look for outcomes priced below a threshold that might have higher
        true probability (based on volume, liquidity, and price history).
        """
        markets = self.finder.fetch_markets(
            limit=50,
            active=True,
            closed=False,
            accepting_orders=True,
            order="volume24hr",
            ascending=False,
        )

        opportunities = []
        for m in markets:
            if not self._passes_market_filters(m):
                continue

            # Skip markets we already have positions in
            if m.get("id") in self.state.positions:
                continue

            token_ids = MarketFinder.extract_token_ids(m)
            if not token_ids:
                continue

            info = MarketFinder.extract_market_info(m)

            # Value bet: look for outcomes priced between min_price and 0.35
            # with decent volume (suggests real interest)
            for outcome_prices_pair in [
                ("yes", info["outcome_prices"][0] if info["outcome_prices"] else None),
                (
                    "no",
                    info["outcome_prices"][1]
                    if len(info["outcome_prices"]) > 1
                    else None,
                ),
            ]:
                label, price = outcome_prices_pair
                if price is None:
                    continue

                if (
                    self.config.min_price <= price <= 0.40
                    and info["volume_24h"] > self.config.min_volume_24h
                ):
                    tid_key = f"{label}_token_id"
                    tid = token_ids.get(tid_key)
                    if tid:
                        opportunities.append(
                            {
                                "market": m,
                                "token_ids": token_ids,
                                "signal": {
                                    "side": "BUY",
                                    "token_id": tid,
                                    "price": price,
                                    "outcome": label,
                                    "score": (0.5 - price) * info["volume_24h"] / 1000,
                                    "reason": f"Value bet: {label} @ ${price:.2f}",
                                },
                            }
                        )

        # Sort by score descending (best opportunities first)
        opportunities.sort(key=lambda x: x["signal"]["score"], reverse=True)
        return opportunities[:5]

    def _scan_spread_capture(self) -> List[Dict[str, Any]]:
        """
        Spread capture: find markets with wide spreads and place orders
        inside the spread to earn the bid-ask difference.
        """
        markets = self.finder.fetch_markets(
            limit=50,
            active=True,
            closed=False,
            accepting_orders=True,
            order="liquidityNum",
            ascending=False,
        )

        opportunities = []
        for m in markets:
            if not self._passes_market_filters(m):
                continue

            if m.get("id") in self.state.positions:
                continue

            token_ids = MarketFinder.extract_token_ids(m)
            if not token_ids:
                continue

            info = MarketFinder.extract_market_info(m)
            spread = info["spread"]

            # Look for spreads > 3 cents (profitable after fees)
            if spread >= 0.03 and info["liquidity"] >= self.config.min_liquidity:
                mid = (info["best_bid"] + info["best_ask"]) / 2

                if self.config.min_price <= mid <= self.config.max_price:
                    tid = token_ids.get("yes_token_id")
                    if tid:
                        # Place a buy order slightly above best bid
                        buy_price = round(info["best_bid"] + 0.01, 2)
                        opportunities.append(
                            {
                                "market": m,
                                "token_ids": token_ids,
                                "signal": {
                                    "side": "BUY",
                                    "token_id": tid,
                                    "price": buy_price,
                                    "outcome": "yes",
                                    "score": spread * info["liquidity"] / 100,
                                    "reason": f"Spread capture: spread={spread:.3f} bid={info['best_bid']:.2f} ask={info['best_ask']:.2f}",
                                },
                            }
                        )

        opportunities.sort(key=lambda x: x["signal"]["score"], reverse=True)
        return opportunities[:3]

    def _scan_momentum(self) -> List[Dict[str, Any]]:
        """
        Momentum strategy: buy outcomes that have been trending up.
        Uses 1-hour and 1-day price changes as momentum signals.
        """
        markets = self.finder.fetch_markets(
            limit=50,
            active=True,
            closed=False,
            accepting_orders=True,
            order="volume24hr",
            ascending=False,
        )

        opportunities = []
        for m in markets:
            if not self._passes_market_filters(m):
                continue

            if m.get("id") in self.state.positions:
                continue

            token_ids = MarketFinder.extract_token_ids(m)
            if not token_ids:
                continue

            info = MarketFinder.extract_market_info(m)
            one_day_change = float(m.get("oneDayPriceChange") or 0)
            one_hour_change = float(m.get("oneHourPriceChange") or 0)

            # Positive momentum in both timeframes
            if one_day_change > 0.02 and one_hour_change > 0.005:
                price = info["last_trade_price"]
                if self.config.min_price <= price <= self.config.max_price:
                    tid = token_ids.get("yes_token_id")
                    if tid:
                        opportunities.append(
                            {
                                "market": m,
                                "token_ids": token_ids,
                                "signal": {
                                    "side": "BUY",
                                    "token_id": tid,
                                    "price": round(min(price, info["best_ask"]), 2),
                                    "outcome": "yes",
                                    "score": (one_day_change + one_hour_change * 5)
                                    * info["volume_24h"]
                                    / 100,
                                    "reason": f"Momentum: 1h={one_hour_change:+.3f} 1d={one_day_change:+.3f}",
                                },
                            }
                        )

        opportunities.sort(key=lambda x: x["signal"]["score"], reverse=True)
        return opportunities[:3]

    def _scan_btc_5m(self) -> List[Dict[str, Any]]:
        """
        BTC 5-minute market strategy: find next BTC 5m market and trade it.
        """
        market = self.finder.find_next_btc_5m_market()
        if not market:
            return []

        if market.get("id") in self.state.positions:
            return []

        token_ids = MarketFinder.extract_token_ids(market)
        if not token_ids:
            return []

        # For BTC 5m, we buy the "Up" outcome at our configured price
        tid = token_ids.get("up_token_id")
        if not tid:
            return []

        return [
            {
                "market": market,
                "token_ids": token_ids,
                "signal": {
                    "side": "BUY",
                    "token_id": tid,
                    "price": self.config.order_price,
                    "outcome": "up",
                    "score": 1.0,
                    "reason": "BTC 5m up/down market",
                },
            }
        ]

    # ------------------------------------------------------------------
    # Market filters
    # ------------------------------------------------------------------

    def _passes_market_filters(self, market: Dict[str, Any]) -> bool:
        """Check if a market passes all configured filters."""
        if not market.get("acceptingOrders"):
            return False
        if market.get("closed"):
            return False

        liq = float(market.get("liquidityNum") or market.get("liquidity") or 0)
        vol = float(market.get("volume24hr") or 0)
        spread = float(market.get("spread") or 999)

        if liq < self.config.min_liquidity:
            return False
        if vol < self.config.min_volume_24h:
            return False
        if spread > self.config.max_spread:
            return False

        return True

    # ------------------------------------------------------------------
    # Trade execution
    # ------------------------------------------------------------------

    async def _evaluate_and_trade(self, opportunity: Dict[str, Any]) -> None:
        """Evaluate an opportunity and place a trade if conditions are met."""
        signal = opportunity["signal"]
        market = opportunity["market"]
        question = market.get("question", "Unknown")
        market_id = market.get("id", "")

        side = signal["side"]
        token_id = signal["token_id"]
        price = signal["price"]
        outcome = signal["outcome"]
        reason = signal["reason"]
        size = self.config.order_size

        # Validate price bounds
        if not (self.config.min_price <= price <= self.config.max_price):
            logger.debug("Skipping %s: price %.2f out of bounds", question[:40], price)
            return

        # Don't exceed max exposure
        remaining_budget = self.config.max_total_exposure - self.state.total_exposure
        cost = price * size
        if cost > remaining_budget:
            size = remaining_budget / price
            if size < 1:
                logger.debug("Skipping %s: insufficient budget", question[:40])
                return
            cost = price * size

        logger.info(
            "Trading signal: %s | %s %s %.0f @ $%.2f | %s",
            question[:50],
            side,
            outcome,
            size,
            price,
            reason,
        )

        if self.config.paper_trading:
            await self._paper_trade(market, signal, size)
        else:
            await self._live_trade(market, signal, size)

    async def _paper_trade(
        self,
        market: Dict[str, Any],
        signal: Dict[str, Any],
        size: float,
    ) -> None:
        """Execute a paper (simulated) trade."""
        price = signal["price"]
        cost = price * size
        market_id = market.get("id", "")
        question = market.get("question", "Unknown")

        if self.state.paper_balance < cost:
            logger.info(
                "Paper: insufficient balance ($%.2f < $%.2f)",
                self.state.paper_balance,
                cost,
            )
            return

        self.state.paper_balance -= cost

        position = Position(
            market_id=market_id,
            condition_id=MarketFinder.extract_condition_id(market) or "",
            token_id=signal["token_id"],
            side=signal["side"],
            outcome_label=signal["outcome"],
            question=question,
            entry_price=price,
            size=size,
            cost=cost,
            order_id=f"paper_{int(time.time())}_{market_id}",
            timestamp=time.time(),
            current_price=price,
            is_paper=True,
        )

        pos_key = f"{market_id}_{signal['outcome']}"
        self.state.positions[pos_key] = position
        self.state.last_trade_time = time.time()
        self.state.trades_today += 1

        msg = (
            f"📝 Paper {signal['side']} | {question[:50]}\n"
            f"   {signal['outcome'].upper()} {size:.0f} @ ${price:.2f} = ${cost:.2f}\n"
            f"   Balance: ${self.state.paper_balance:.2f} | {signal.get('reason', '')}"
        )
        logger.info(msg)
        self._notify_trade(msg)

    async def _live_trade(
        self,
        market: Dict[str, Any],
        signal: Dict[str, Any],
        size: float,
    ) -> None:
        """Execute a live trade via the CLOB client."""
        if not self.client or not self.client.is_available():
            logger.error("PolyClient not available for live trading.")
            return

        price = signal["price"]
        market_id = market.get("id", "")
        question = market.get("question", "Unknown")

        # Place limit order
        resp = await asyncio.to_thread(
            self.client.place_limit_order,
            token_id=signal["token_id"],
            side=signal["side"],
            price=price,
            size=size,
        )

        if resp is None:
            logger.error("Failed to place live order for %s", question[:40])
            self.state.errors.append(f"Order failed: {question[:40]}")
            return

        order_id = None
        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("id") or resp.get("order_id")

        cost = price * size
        position = Position(
            market_id=market_id,
            condition_id=MarketFinder.extract_condition_id(market) or "",
            token_id=signal["token_id"],
            side=signal["side"],
            outcome_label=signal["outcome"],
            question=question,
            entry_price=price,
            size=size,
            cost=cost,
            order_id=order_id,
            timestamp=time.time(),
            current_price=price,
            is_paper=False,
        )

        pos_key = f"{market_id}_{signal['outcome']}"
        self.state.positions[pos_key] = position

        if order_id:
            self.state.open_orders[order_id] = {
                "pos_key": pos_key,
                "placed_at": time.time(),
                "market_id": market_id,
            }

        self.state.last_trade_time = time.time()
        self.state.trades_today += 1

        msg = (
            f"🔴 LIVE {signal['side']} | {question[:50]}\n"
            f"   {signal['outcome'].upper()} {size:.0f} @ ${price:.2f} = ${cost:.2f}\n"
            f"   Order ID: {order_id or 'N/A'} | {signal.get('reason', '')}"
        )
        logger.info(msg)
        self._notify_trade(msg)

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    # Grace period: don't trigger stop-loss within this many seconds of opening
    STOP_LOSS_GRACE_SECONDS = 60

    async def _manage_positions(self) -> None:
        """Update positions and check for stop-loss / take-profit."""
        positions_to_close: List[str] = []

        for pos_key, pos in self.state.positions.items():
            try:
                # Get current price
                if self.config.paper_trading:
                    current_price = await self._get_paper_price(pos)
                else:
                    current_price = await self._get_live_price(pos)

                if current_price is not None and current_price > 0:
                    pos.update_pnl(current_price)
                else:
                    # Could not fetch a valid price — skip stop-loss evaluation
                    logger.debug("No valid price for %s, keeping entry price", pos_key)
                    continue

                # Grace period: skip stop-loss check for newly opened positions
                age = time.time() - pos.timestamp
                if age < self.STOP_LOSS_GRACE_SECONDS:
                    logger.debug(
                        "Position %s in grace period (%.0fs < %ds), skipping stop-loss",
                        pos_key,
                        age,
                        self.STOP_LOSS_GRACE_SECONDS,
                    )
                    continue

                # Stop-loss check
                if pos.pnl_pct <= -self.config.stop_loss_pct:
                    logger.info(
                        "Stop-loss triggered for %s (%.1f%%)",
                        pos.question[:40],
                        pos.pnl_pct * 100,
                    )
                    positions_to_close.append(pos_key)
                    continue

                # Take profit: if price >= 0.90, sell
                if pos.side == "BUY" and pos.current_price >= 0.90:
                    logger.info(
                        "Take-profit for %s (price=%.2f)",
                        pos.question[:40],
                        pos.current_price,
                    )
                    positions_to_close.append(pos_key)

            except Exception as exc:
                logger.warning("Error managing position %s: %s", pos_key, exc)

        # Close marked positions
        for pos_key in positions_to_close:
            await self._close_position(pos_key)

    async def _get_paper_price(self, pos: Position) -> Optional[float]:
        """Get simulated current price for a paper position.

        Tries multiple lookup strategies:
          1. Fetch market by its numeric ID (fastest, most reliable)
          2. Fetch market by condition ID
          3. Fetch market by slug search (fallback)
          4. Return entry_price as last resort (position stays flat)
        """
        try:
            market = None

            # Strategy 1: fetch by market ID (most reliable)
            if pos.market_id:
                market = await asyncio.to_thread(
                    self.finder.fetch_market_by_id, pos.market_id
                )

            # Strategy 2: fetch by condition ID
            if not market and pos.condition_id:
                market = await asyncio.to_thread(
                    self.finder.fetch_market_by_condition_id, pos.condition_id
                )

            # Strategy 3: slug-based search for BTC 5m markets
            if not market and "btc" in pos.question.lower() and "5" in pos.question:
                market_data = await asyncio.to_thread(
                    self.finder.find_next_btc_5m_market
                )
                if market_data and market_data.get("id") == pos.market_id:
                    market = market_data

            if market:
                info = MarketFinder.extract_market_info(market)
                prices = info.get("outcome_prices", [])
                outcomes = info.get("outcomes", [])

                # Try to match outcome label to get the right price
                for i, out in enumerate(outcomes):
                    if out.lower() == pos.outcome_label.lower() and i < len(prices):
                        price = prices[i]
                        if price > 0:
                            return price

                # Fallback: use last trade price if it's valid
                ltp = info.get("last_trade_price")
                if ltp and ltp > 0:
                    return ltp

                # Fallback: use bestBid as a conservative estimate
                best_bid = info.get("best_bid")
                if best_bid and best_bid > 0:
                    return best_bid

        except Exception as exc:
            logger.debug("_get_paper_price error for %s: %s", pos.market_id, exc)

        # Last resort: return entry price (position stays flat, no false stop-loss)
        return pos.entry_price

    async def _get_live_price(self, pos: Position) -> Optional[float]:
        """Get current price from the CLOB for a live position."""
        if not self.client or not self.client.is_available():
            return None
        try:
            price = await asyncio.to_thread(self.client.get_price, pos.token_id, "BUY")
            return price
        except Exception:
            return None

    async def _close_position(self, pos_key: str) -> None:
        """Close a position (sell it or record P&L for paper)."""
        pos = self.state.positions.get(pos_key)
        if not pos:
            return

        pnl = pos.unrealized_pnl
        exit_price = pos.current_price

        if not self.config.paper_trading and self.client and self.client.is_available():
            # Place sell order
            resp = await asyncio.to_thread(
                self.client.place_market_order,
                token_id=pos.token_id,
                side="SELL",
                amount=pos.size,
            )
            if resp and isinstance(resp, dict):
                logger.info("Position closed via market sell: %s", resp)

        if self.config.paper_trading:
            self.state.paper_balance += pos.cost + pnl

        self.state.daily_pnl += pnl
        self.state.total_pnl += pnl

        trade_record = TradeRecord(
            market_id=pos.market_id,
            condition_id=pos.condition_id,
            token_id=pos.token_id,
            side=pos.side,
            outcome_label=pos.outcome_label,
            question=pos.question,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size=pos.size,
            pnl=pnl,
            entry_time=pos.timestamp,
            exit_time=time.time(),
            is_paper=pos.is_paper,
        )
        self.state.trade_history.append(trade_record)

        del self.state.positions[pos_key]

        msg = (
            f"{'📝' if pos.is_paper else '🔴'} CLOSED | {pos.question[:50]}\n"
            f"   {pos.outcome_label.upper()} {pos.size:.0f} @ entry=${pos.entry_price:.2f} exit=${exit_price:.2f}\n"
            f"   P&L: ${pnl:+.2f} | Total: ${self.state.total_pnl:+.2f}"
        )
        logger.info(msg)
        self._notify_trade(msg)

    # ------------------------------------------------------------------
    # Order cleanup
    # ------------------------------------------------------------------

    async def _cleanup_stale_orders(self) -> None:
        """Cancel orders that have been open too long."""
        if self.config.paper_trading:
            return  # No real orders to cancel in paper mode

        if not self.client or not self.client.is_available():
            return

        now = time.time()
        stale_ids = []

        for order_id, info in list(self.state.open_orders.items()):
            if now - info["placed_at"] > self.config.order_ttl_seconds:
                stale_ids.append(order_id)

        if stale_ids:
            for oid in stale_ids:
                try:
                    await asyncio.to_thread(self.client.cancel_order, oid)
                    logger.info("Cancelled stale order: %s", oid)
                except Exception as exc:
                    logger.warning("Failed to cancel stale order %s: %s", oid, exc)

                self.state.open_orders.pop(oid, None)

    async def _cancel_all_open_orders(self) -> None:
        """Cancel all open orders (called on shutdown)."""
        if not self.client or not self.client.is_available():
            return

        try:
            resp = await asyncio.to_thread(self.client.cancel_all)
            logger.info("Cancelled all orders on shutdown: %s", resp)
        except Exception as exc:
            logger.warning("Failed to cancel all orders: %s", exc)

        self.state.open_orders.clear()

    # ------------------------------------------------------------------
    # Notification helpers
    # ------------------------------------------------------------------

    def _notify_trade(self, message: str) -> None:
        """Send trade notification via callback."""
        if self.on_trade:
            try:
                self.on_trade(message)
            except Exception:
                pass

    def _notify_status(self, message: str) -> None:
        """Send status notification via callback."""
        if self.on_status:
            try:
                self.on_status(message)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public status methods
    # ------------------------------------------------------------------

    def get_status_summary(self) -> str:
        """Get a human-readable status summary."""
        mode = "📝 PAPER" if self.config.paper_trading else "🔴 LIVE"
        uptime = ""
        if self.state.start_time:
            elapsed = time.time() - self.state.start_time
            hours = int(elapsed // 3600)
            minutes = int((elapsed % 3600) // 60)
            uptime = f"{hours}h {minutes}m"

        lines = [
            f"{'═' * 30}",
            f"{mode} Trading Engine",
            f"{'═' * 30}",
            f"Strategy: {self.config.strategy.value}",
            f"Status: {self.state.status_message}",
            f"Uptime: {uptime}",
            "",
            f"💰 Balance: ${self.state.paper_balance:.2f}"
            if self.config.paper_trading
            else "",
            f"📊 Positions: {self.state.position_count}/{self.config.max_positions}",
            f"💵 Exposure: ${self.state.total_exposure:.2f}/{self.config.max_total_exposure:.2f}",
            f"📈 Total P&L: ${self.state.total_pnl:+.2f}",
            f"📅 Daily P&L: ${self.state.daily_pnl:+.2f}",
            f"🔄 Trades today: {self.state.trades_today}",
        ]

        if self.state.positions:
            lines.append("")
            lines.append("Open Positions:")
            for key, pos in self.state.positions.items():
                lines.append(
                    f"  • {pos.question[:35]} | {pos.outcome_label.upper()} "
                    f"{pos.size:.0f} @ ${pos.entry_price:.2f} → ${pos.current_price:.2f} "
                    f"(${pos.unrealized_pnl:+.2f})"
                )

        if self.state.trade_history:
            recent = self.state.trade_history[-3:]
            lines.append("")
            lines.append("Recent Trades:")
            for tr in reversed(recent):
                lines.append(
                    f"  • {tr.question[:35]} | {tr.outcome_label.upper()} "
                    f"P&L: ${tr.pnl:+.2f}"
                )

        return "\n".join(line for line in lines if line is not None)

    def get_positions_summary(self) -> str:
        """Get a summary of open positions."""
        if not self.state.positions:
            return "No open positions."

        lines = []
        for key, pos in self.state.positions.items():
            lines.append(
                f"• {pos.question[:50]}\n"
                f"  {pos.side} {pos.outcome_label.upper()} {pos.size:.0f} "
                f"@ ${pos.entry_price:.2f} → ${pos.current_price:.2f}\n"
                f"  P&L: ${pos.unrealized_pnl:+.2f} ({pos.pnl_pct:+.1%})"
            )
        return "\n".join(lines)

    def get_trade_history_summary(self, limit: int = 10) -> str:
        """Get a summary of recent trades."""
        if not self.state.trade_history:
            return "No trade history."

        recent = self.state.trade_history[-limit:]
        lines = []
        for tr in reversed(recent):
            ts = datetime.fromtimestamp(tr.exit_time, tz=timezone.utc).strftime(
                "%m/%d %H:%M"
            )
            emoji = "✅" if tr.pnl >= 0 else "❌"
            lines.append(
                f"{emoji} {ts} | {tr.question[:40]}\n"
                f"   {tr.side} {tr.outcome_label.upper()} {tr.size:.0f} "
                f"@ ${tr.entry_price:.2f}→${tr.exit_price:.2f} "
                f"P&L: ${tr.pnl:+.2f}"
            )
        return "\n".join(lines)

    def update_config(self, **kwargs) -> None:
        """Update trading config parameters at runtime."""
        for key, value in kwargs.items():
            if hasattr(self.config, key):
                old = getattr(self.config, key)
                setattr(self.config, key, value)
                logger.info("Config updated: %s = %s (was %s)", key, value, old)

    # ------------------------------------------------------------------
    # Manual trading helpers (for Telegram commands)
    # ------------------------------------------------------------------

    async def manual_buy(
        self,
        market_id: str,
        outcome: str = "yes",
        price: Optional[float] = None,
        size: Optional[float] = None,
    ) -> str:
        """
        Manually buy an outcome in a market (via Telegram command).

        Args:
            market_id: Gamma API market ID.
            outcome: "yes" or "no".
            price: Limit price (uses config default if None).
            size: Order size (uses config default if None).

        Returns:
            Status message string.
        """
        price = price or self.config.order_price
        size = size or self.config.order_size

        # Fetch market data
        market = await asyncio.to_thread(self.finder.fetch_market_by_id, market_id)
        if not market:
            return f"❌ Market {market_id} not found."

        if not market.get("acceptingOrders"):
            return f"❌ Market is not accepting orders."

        token_ids = MarketFinder.extract_token_ids(market)
        if not token_ids:
            return f"❌ Could not extract token IDs."

        tid_key = f"{outcome.lower()}_token_id"
        token_id = token_ids.get(tid_key)
        if not token_id:
            return f"❌ No token ID for outcome '{outcome}'."

        signal = {
            "side": "BUY",
            "token_id": token_id,
            "price": price,
            "outcome": outcome.lower(),
            "score": 0,
            "reason": "Manual order",
        }

        await self._evaluate_and_trade(
            {"market": market, "token_ids": token_ids, "signal": signal}
        )

        return (
            f"✅ Order placed: BUY {outcome.upper()} {size:.0f} @ ${price:.2f}\n"
            f"Market: {market.get('question', '')[:50]}"
        )

    async def manual_sell_position(self, pos_key: str) -> str:
        """Manually close a position."""
        if pos_key not in self.state.positions:
            return f"❌ Position '{pos_key}' not found."

        await self._close_position(pos_key)
        return f"✅ Position '{pos_key}' closed."

    async def manual_cancel_all(self) -> str:
        """Manually cancel all open orders."""
        await self._cancel_all_open_orders()
        return "✅ All open orders cancelled."
