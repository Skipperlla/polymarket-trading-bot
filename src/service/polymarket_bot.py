"""
PolymarketBot - Full working trading bot for Polymarket.

Ties together:
  - PolyClient for CLOB order placement / management
  - PolyRelayerClient for on-chain merge / redeem operations
  - MarketFinder for Gamma API market discovery
  - TradingEngine for autonomous trading
  - WebSocket for real-time data

Provides a unified interface for both Telegram bot and autonomous CLI usage.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

try:
    import websocket

    WEBSOCKET_AVAILABLE = True
except ImportError:
    websocket = None  # type: ignore
    WEBSOCKET_AVAILABLE = False

try:
    import requests

    REQUESTS_AVAILABLE = True
except ImportError:
    requests = None  # type: ignore
    REQUESTS_AVAILABLE = False

from src.client.poly_client.poly_client import PolyClient
from src.client.poly_relayer_client.poly_relayer_client import PolyRelayerClient
from src.service.market_finder import MarketFinder

logger = logging.getLogger("PolymarketBot")


class PolymarketBot:
    """
    Full-featured trading bot for Polymarket.

    Provides a unified interface for:
    - Trading operations via PolyClient (market/limit orders)
    - On-chain operations via PolyRelayerClient (merge/redeem tokens)
    - Market discovery via MarketFinder (Gamma API)
    - Real-time data via WebSocket connections
    """

    def __init__(
        self,
        private_key: str,
        host: str = "https://clob.polymarket.com",
        chain_id: int = 137,
        signature_type: int = 0,
        funder: Optional[str] = None,
        relayer_url: Optional[str] = None,
        builder_api_key: Optional[str] = None,
        builder_secret: Optional[str] = None,
        builder_passphrase: Optional[str] = None,
        poly_client: Optional[PolyClient] = None,
        poly_relayer_client: Optional[PolyRelayerClient] = None,
        market_finder: Optional[MarketFinder] = None,
    ):
        """
        Initialise PolymarketBot.

        Args:
            private_key: Private key for signing transactions.
            host: CLOB API host URL.
            chain_id: Blockchain chain ID (137 for Polygon mainnet).
            signature_type: Signature type (0=EOA, 1=Magic/email, 2=browser proxy).
            funder: Optional funder/proxy address.
            relayer_url: Optional relayer URL for on-chain operations.
            builder_api_key: Optional Builder API key.
            builder_secret: Optional Builder API secret.
            builder_passphrase: Optional Builder API passphrase.
            poly_client: Pre-built PolyClient (skip auto-creation).
            poly_relayer_client: Pre-built PolyRelayerClient (skip auto-creation).
            market_finder: Pre-built MarketFinder (skip auto-creation).
        """
        self.private_key = private_key
        self.host = host
        self.chain_id = chain_id
        self.funder = funder
        self.current_market: Optional[Dict[str, Any]] = None
        self.current_market_id: str = ""

        # Gamma API endpoints
        self.base_url = "https://gamma-api.polymarket.com"
        self.api_url = f"{self.base_url}/markets"

        # ── PolyClient ─────────────────────────────────────────────
        if poly_client is not None:
            self.poly_client = poly_client
        else:
            try:
                self.poly_client = PolyClient(
                    private_key=private_key,
                    host=host,
                    chain_id=chain_id,
                    signature_type=signature_type,
                    funder=funder,
                )
            except Exception as exc:
                logger.error("Failed to create PolyClient: %s", exc)
                self.poly_client = None  # type: ignore[assignment]

        # ── PolyRelayerClient ──────────────────────────────────────
        if poly_relayer_client is not None:
            self.relayer_client = poly_relayer_client
        elif relayer_url:
            try:
                self.relayer_client = PolyRelayerClient(
                    relayer_url=relayer_url,
                    chain_id=chain_id,
                    private_key=private_key,
                    builder_api_key=builder_api_key or os.getenv("BUILDER_API_KEY"),
                    builder_secret=builder_secret or os.getenv("BUILDER_SECRET"),
                    builder_passphrase=builder_passphrase
                    or os.getenv("BUILDER_PASS_PHRASE"),
                )
            except Exception as exc:
                logger.error("Failed to create PolyRelayerClient: %s", exc)
                self.relayer_client = None
        else:
            self.relayer_client = None
            logger.info("RELAYER_URL not provided – on-chain merge/redeem unavailable.")

        # ── MarketFinder ───────────────────────────────────────────
        self.finder = market_finder or MarketFinder()

        # ── WebSocket ──────────────────────────────────────────────
        self.ws_url = os.getenv("CLOB_WS_URL")
        self.ws: Optional[Any] = None
        self.ws_thread: Optional[threading.Thread] = None
        self.connected = False
        self.running = False
        self._debug = False

        # WebSocket callbacks
        self.on_message_callback: Optional[Callable] = None
        self.on_connect_callback: Optional[Callable] = None
        self.on_disconnect_callback: Optional[Callable] = None
        self.on_error_callback: Optional[Callable] = None

        logger.info("PolymarketBot initialised (host=%s, chain=%d)", host, chain_id)

    # ──────────────────────────────────────────────────────────────
    # Timestamp helpers
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def get_current_timestamp() -> int:
        """Get current Unix timestamp."""
        return int(time.time())

    # ──────────────────────────────────────────────────────────────
    # Slug generation (BTC 5-minute markets)
    # ──────────────────────────────────────────────────────────────

    def generate_slug(self, timestamp: Optional[int] = None) -> str:
        """
        Generate BTC up/down 5-minute market slug from timestamp.

        The timestamp is rounded DOWN to the nearest 5-minute boundary.
        Returns a slug string like: will-btc-go-up-or-down-5-min-{rounded}
        """
        return MarketFinder.generate_btc_5m_slug(timestamp)

    # ──────────────────────────────────────────────────────────────
    # Market discovery
    # ──────────────────────────────────────────────────────────────

    def find_active_market(
        self, slug: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Find an active market by slug (or generate slug from current time).

        Args:
            slug: Exact market slug. If None, generates BTC 5m slug.

        Returns:
            Market data dict from Gamma API, or None.
        """
        if slug is None:
            slug = self.generate_slug()

        market = self.finder.fetch_market_by_slug(slug)
        if market and market.get("active") and market.get("acceptingOrders"):
            self.current_market = market
            self.current_market_id = str(market.get("id", ""))
            logger.info("Active market found: %s (id=%s)", slug, self.current_market_id)
            return market

        logger.info("No active market for slug: %s", slug)
        return None

    def find_next_active_market(self) -> Optional[Dict[str, Any]]:
        """
        Find the next active BTC 5-minute market.

        Tries the next 5-minute boundary, then the one after, then the current one.

        Returns:
            Market data dict or None.
        """
        market = self.finder.find_next_btc_5m_market()
        if market:
            self.current_market = market
            self.current_market_id = str(market.get("id", ""))
            return market
        return None

    def search_markets(
        self,
        query: str,
        limit: int = 10,
        min_volume_24h: float = 100,
        min_liquidity: float = 500,
    ) -> List[Dict[str, Any]]:
        """
        Search for markets by keyword.

        Args:
            query: Search term (matched against question, slug, description).
            limit: Max results.
            min_volume_24h: Minimum 24h volume filter.
            min_liquidity: Minimum liquidity filter.

        Returns:
            List of matching market dicts.
        """
        return self.finder.search_markets(
            query=query,
            limit=limit,
            active_only=True,
            min_volume_24h=min_volume_24h,
            min_liquidity=min_liquidity,
        )

    def get_trending_markets(
        self,
        limit: int = 10,
        min_volume_24h: float = 1000,
    ) -> List[Dict[str, Any]]:
        """Get trending markets sorted by 24h volume."""
        return self.finder.get_trending_markets(
            limit=limit,
            min_volume_24h=min_volume_24h,
        )

    # ──────────────────────────────────────────────────────────────
    # Token ID extraction
    # ──────────────────────────────────────────────────────────────

    def get_token_ids(
        self, market: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, str]]:
        """
        Extract Up/Down (or Yes/No) token IDs from market data.

        Args:
            market: Market dict from Gamma API. Uses current_market if None.

        Returns:
            Dict with 'up_token_id'/'down_token_id' (and 'yes_token_id'/'no_token_id'),
            or None if extraction fails.
        """
        market = market or self.current_market
        if not market:
            logger.warning("get_token_ids called with no market data.")
            return None
        return MarketFinder.extract_token_ids(market)

    def get_market_info(
        self, market: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Extract a clean summary of market information."""
        market = market or self.current_market
        if not market:
            return {}
        return MarketFinder.extract_market_info(market)

    # ──────────────────────────────────────────────────────────────
    # Order placement
    # ──────────────────────────────────────────────────────────────

    def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC",
    ) -> Optional[Dict[str, Any]]:
        """
        Place a limit order on Polymarket CLOB.

        Args:
            token_id: The CLOB token ID (from clobTokenIds).
            side: "BUY" or "SELL".
            price: Price per share in (0, 1).
            size: Number of shares.
            order_type: "GTC" (default), "GTD", or "FOK".

        Returns:
            API response dict on success, None on failure.
        """
        if not self.poly_client or not self.poly_client.is_available():
            logger.error("PolyClient not available – cannot place limit order.")
            return None
        return self.poly_client.place_limit_order(
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            order_type=order_type,
        )

    def place_market_order(
        self,
        token_id: str,
        side: str,
        amount: float,
    ) -> Optional[Dict[str, Any]]:
        """
        Place a market order on Polymarket CLOB.

        Args:
            token_id: The CLOB token ID.
            side: "BUY" or "SELL".
            amount: Dollar amount to spend (BUY) or shares to sell (SELL).

        Returns:
            API response dict on success, None on failure.
        """
        if not self.poly_client or not self.poly_client.is_available():
            logger.error("PolyClient not available – cannot place market order.")
            return None
        return self.poly_client.place_market_order(
            token_id=token_id,
            side=side,
            amount=amount,
        )

    def cancel_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Cancel a single order by ID."""
        if not self.poly_client or not self.poly_client.is_available():
            return None
        return self.poly_client.cancel_order(order_id)

    def cancel_all_orders(self) -> Optional[Dict[str, Any]]:
        """Cancel all open orders."""
        if not self.poly_client or not self.poly_client.is_available():
            return None
        return self.poly_client.cancel_all()

    def get_open_orders(self, market: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get open orders, optionally filtered by market condition ID."""
        if not self.poly_client or not self.poly_client.is_available():
            return []
        return self.poly_client.get_open_orders(market=market)

    # ──────────────────────────────────────────────────────────────
    # Order book / pricing
    # ──────────────────────────────────────────────────────────────

    def get_order_book(self, token_id: str) -> Optional[Any]:
        """Get the full order book for a token."""
        if not self.poly_client or not self.poly_client.is_available():
            return None
        return self.poly_client.get_order_book(token_id)

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get the midpoint price for a token."""
        if not self.poly_client or not self.poly_client.is_available():
            return None
        return self.poly_client.get_midpoint(token_id)

    def get_price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        """Get the current price for a token."""
        if not self.poly_client or not self.poly_client.is_available():
            return None
        return self.poly_client.get_price(token_id, side)

    # ──────────────────────────────────────────────────────────────
    # Balance
    # ──────────────────────────────────────────────────────────────

    def get_balance(self) -> Optional[float]:
        """Get USDC balance for the wallet."""
        if not self.poly_client or not self.poly_client.is_available():
            return None
        return self.poly_client.get_collateral_balance()

    def get_conditional_balance(self, token_id: str) -> Optional[float]:
        """Get conditional token balance."""
        if not self.poly_client or not self.poly_client.is_available():
            return None
        return self.poly_client.get_conditional_balance(token_id)

    # ──────────────────────────────────────────────────────────────
    # On-chain operations (relayer)
    # ──────────────────────────────────────────────────────────────

    def merge_tokens(
        self,
        condition_id: str,
        amount: int,
        partition: Optional[List[int]] = None,
    ) -> Optional[Any]:
        """
        Merge outcome tokens back into USDC collateral.

        Requires a working relayer client.

        Args:
            condition_id: Market condition ID.
            amount: Amount in USDC minor units (1 USDC = 1_000_000).
            partition: Index sets (default [1, 2] for binary markets).

        Returns:
            Relayer response or None.
        """
        if not self.relayer_client or not self.relayer_client.is_available():
            logger.error("Relayer not available – cannot merge tokens.")
            return None
        return self.relayer_client.merge_tokens(
            condition_id=condition_id,
            amount=amount,
            partition=partition,
        )

    def redeem_positions(
        self,
        condition_id: str,
        index_sets: Optional[List[int]] = None,
    ) -> Optional[Any]:
        """
        Redeem winning outcome tokens for USDC after market resolution.

        Args:
            condition_id: Market condition ID.
            index_sets: Which outcomes to redeem (default [1, 2]).

        Returns:
            Relayer response or None.
        """
        if not self.relayer_client or not self.relayer_client.is_available():
            logger.error("Relayer not available – cannot redeem positions.")
            return None
        return self.relayer_client.redeem_positions(
            condition_id=condition_id,
            index_sets=index_sets,
        )

    # ──────────────────────────────────────────────────────────────
    # WebSocket
    # ──────────────────────────────────────────────────────────────

    def connect_websocket(
        self, ws_url: Optional[str] = None, debug: bool = False
    ) -> bool:
        """
        Connect to Polymarket CLOB WebSocket for real-time data.

        Args:
            ws_url: WebSocket URL (defaults to CLOB_WS_URL env var).
            debug: Enable debug logging.

        Returns:
            True if connected successfully, False otherwise.
        """
        if not WEBSOCKET_AVAILABLE:
            logger.error(
                "websocket-client not installed. Run: pip install websocket-client"
            )
            return False

        if self.connected:
            return True

        ws_url = ws_url or self.ws_url
        if not ws_url:
            logger.error(
                "WebSocket URL not provided. "
                "Set CLOB_WS_URL env var or pass ws_url parameter."
            )
            return False

        self._debug = debug

        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )

        self.running = True

        def run_ws():
            try:
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                self.connected = False
                if self._debug:
                    logger.error("WebSocket run_forever error: %s", e)

        self.ws_thread = threading.Thread(target=run_ws, daemon=True)
        self.ws_thread.start()

        # Wait for connection
        timeout = 15
        start_time = time.time()
        while not self.connected and (time.time() - start_time) < timeout:
            time.sleep(0.1)

        if not self.connected:
            logger.error("WebSocket connection timed out.")
            return False

        logger.info("WebSocket connected to %s", ws_url)
        return True

    def disconnect_websocket(self) -> None:
        """Disconnect from WebSocket."""
        self.running = False
        self.connected = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

    def is_websocket_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self.connected

    def set_websocket_callbacks(
        self,
        on_message: Optional[Callable] = None,
        on_connect: Optional[Callable] = None,
        on_disconnect: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
    ) -> None:
        """
        Set WebSocket callback functions.

        Args:
            on_message: Callback for incoming messages (receives parsed dict).
            on_connect: Callback for connection established.
            on_disconnect: Callback for connection closed (receives code, msg).
            on_error: Callback for errors (receives error).
        """
        self.on_message_callback = on_message
        self.on_connect_callback = on_connect
        self.on_disconnect_callback = on_disconnect
        self.on_error_callback = on_error

    def subscribe_market(self, token_id: str) -> None:
        """Subscribe to real-time updates for a market token."""
        if not self.ws or not self.connected:
            logger.warning("WebSocket not connected – cannot subscribe.")
            return
        msg = json.dumps(
            {
                "type": "subscribe",
                "channel": "market",
                "assets_ids": [token_id],
            }
        )
        try:
            self.ws.send(msg)
            logger.info("Subscribed to market updates for token %s…", token_id[:20])
        except Exception as exc:
            logger.error("WebSocket subscribe failed: %s", exc)

    def subscribe_user(self) -> None:
        """Subscribe to user-level events (order fills, etc.)."""
        if not self.ws or not self.connected:
            logger.warning("WebSocket not connected – cannot subscribe.")
            return
        msg = json.dumps({"type": "subscribe", "channel": "user"})
        try:
            self.ws.send(msg)
            logger.info("Subscribed to user channel.")
        except Exception as exc:
            logger.error("WebSocket user subscribe failed: %s", exc)

    # ── WebSocket internal callbacks ───────────────────────────────

    def _on_message(self, ws, message: str) -> None:
        """Handle incoming WebSocket messages."""
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            if self._debug:
                logger.debug("WS non-JSON message: %s", message[:200])
            return

        if self._debug:
            logger.debug("WS message: %s", json.dumps(data)[:300])

        # Dispatch to user callback
        if isinstance(data, list):
            for item in data:
                self._process_message(item)
        elif isinstance(data, dict):
            self._process_message(data)

    def _process_message(self, data: Dict[str, Any]) -> None:
        """Process a single parsed WebSocket message."""
        event_type = data.get("event_type") or data.get("type") or ""

        if event_type == "book":
            if self._debug:
                asset_id = data.get("asset_id", "?")
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                logger.debug(
                    "[WS] Book update asset=%s bids=%d asks=%d",
                    str(asset_id)[:20],
                    len(bids),
                    len(asks),
                )
        elif event_type == "price_change":
            if self._debug:
                logger.debug(
                    "[WS] Price change asset=%s price=%s",
                    data.get("asset_id", "?"),
                    data.get("price"),
                )
        elif event_type == "last_trade_price":
            if self._debug:
                logger.debug(
                    "[WS] Last trade asset=%s price=%s",
                    data.get("asset_id", "?"),
                    data.get("price"),
                )

        if self.on_message_callback:
            try:
                self.on_message_callback(data)
            except Exception as exc:
                logger.warning("on_message_callback error: %s", exc)

    def _on_open(self, ws) -> None:
        """Handle WebSocket connection opened."""
        self.connected = True
        logger.info("WebSocket connection opened.")
        if self.on_connect_callback:
            try:
                self.on_connect_callback()
            except Exception:
                pass

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        """Handle WebSocket connection closed."""
        self.connected = False
        logger.info(
            "WebSocket connection closed (code=%s, msg=%s).",
            close_status_code,
            close_msg,
        )
        if self.on_disconnect_callback:
            try:
                self.on_disconnect_callback(close_status_code, close_msg)
            except Exception:
                pass

    def _on_error(self, ws, error) -> None:
        """Handle WebSocket errors."""
        if self.on_error_callback:
            try:
                self.on_error_callback(error)
            except Exception:
                pass
        elif self._debug:
            logger.error("WebSocket error: %s", error)

    # ──────────────────────────────────────────────────────────────
    # High-level workflow helpers
    # ──────────────────────────────────────────────────────────────

    def full_trade_workflow(
        self,
        price: Optional[float] = None,
        size: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Execute the full BTC 5m trading workflow:
          1. Find next active market
          2. Extract token IDs
          3. Place limit orders on both Up and Down outcomes
          4. Return summary

        Args:
            price: Limit price per share (default from ORDER_PRICE env or 0.46).
            size: Order size (default from ORDER_SIZE env or 5.0).

        Returns:
            Dict with workflow results.
        """
        price = price or float(os.getenv("ORDER_PRICE", "0.46"))
        size = size or float(os.getenv("ORDER_SIZE", "5.0"))

        result: Dict[str, Any] = {
            "success": False,
            "market": None,
            "orders": [],
            "errors": [],
        }

        # 1. Find market
        market = self.find_next_active_market()
        if not market:
            result["errors"].append("No active BTC 5m market found.")
            logger.info("Workflow: no active market found.")
            return result

        result["market"] = MarketFinder.extract_market_info(market)

        # 2. Get token IDs
        token_ids = self.get_token_ids(market)
        if not token_ids:
            result["errors"].append("Could not extract token IDs from market.")
            return result

        # 3. Place orders
        up_token = token_ids.get("up_token_id") or token_ids.get("yes_token_id")
        down_token = token_ids.get("down_token_id") or token_ids.get("no_token_id")

        if up_token:
            up_resp = self.place_limit_order(
                token_id=up_token, side="BUY", price=price, size=size
            )
            result["orders"].append(
                {"side": "BUY", "outcome": "Up/Yes", "response": up_resp}
            )
            if up_resp:
                logger.info("Up order placed: %s", up_resp)
            else:
                result["errors"].append("Failed to place Up order.")

        if down_token:
            down_resp = self.place_limit_order(
                token_id=down_token, side="BUY", price=price, size=size
            )
            result["orders"].append(
                {"side": "BUY", "outcome": "Down/No", "response": down_resp}
            )
            if down_resp:
                logger.info("Down order placed: %s", down_resp)
            else:
                result["errors"].append("Failed to place Down order.")

        result["success"] = any(o.get("response") is not None for o in result["orders"])
        return result

    def merge_after_resolution(
        self, condition_id: Optional[str] = None, amount: int = 1_000_000
    ) -> Optional[Any]:
        """
        Merge outcome tokens after a market resolves (convenience wrapper).

        Uses the current market's condition ID if none provided.
        """
        if condition_id is None and self.current_market:
            condition_id = MarketFinder.extract_condition_id(self.current_market)

        if not condition_id:
            logger.error("No condition_id available for merge.")
            return None

        return self.merge_tokens(condition_id=condition_id, amount=amount)

    def redeem_after_resolution(
        self,
        condition_id: Optional[str] = None,
        index_sets: Optional[List[int]] = None,
    ) -> Optional[Any]:
        """
        Redeem winning tokens after market resolution (convenience wrapper).

        Uses the current market's condition ID if none provided.
        """
        if condition_id is None and self.current_market:
            condition_id = MarketFinder.extract_condition_id(self.current_market)

        if not condition_id:
            logger.error("No condition_id available for redeem.")
            return None

        return self.redeem_positions(condition_id=condition_id, index_sets=index_sets)

    # ──────────────────────────────────────────────────────────────
    # Status / info
    # ──────────────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """Get current bot status as a dict."""
        balance = self.get_balance()
        return {
            "poly_client_available": (
                self.poly_client is not None and self.poly_client.is_available()
            ),
            "relayer_available": (
                self.relayer_client is not None and self.relayer_client.is_available()
            ),
            "websocket_connected": self.connected,
            "balance_usdc": balance,
            "current_market_id": self.current_market_id,
            "current_market_question": (
                self.current_market.get("question") if self.current_market else None
            ),
            "host": self.host,
            "chain_id": self.chain_id,
        }

    def __repr__(self) -> str:
        return (
            f"PolymarketBot(host={self.host!r}, chain_id={self.chain_id}, "
            f"client_ok={self.poly_client is not None and self.poly_client.is_available()}, "
            f"relayer_ok={self.relayer_client is not None and self.relayer_client.is_available()})"
        )
