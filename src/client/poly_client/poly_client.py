"""
PolyClient - Full working wrapper for Polymarket CLOB Client.

Handles:
  - API credential management (create/derive)
  - Limit order placement (GTC)
  - Market order placement (FOK)
  - Order cancellation (single, batch, all, by market)
  - Order book queries (midpoint, price, spread, book)
  - Open order retrieval
  - Balance & allowance checks
"""

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("PolyClient")


def _validate_funder(funder: Optional[str]) -> Optional[str]:
    """Return funder only if it is a valid 0x-prefixed Ethereum address."""
    if not funder:
        return None
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", funder):
        logger.warning(
            "Ignoring invalid FUNDER value (not a 42-char hex address). "
            "Falling back to signer address."
        )
        return None
    return funder


try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        AssetType,
        BalanceAllowanceParams,
        BookParams,
        MarketOrderArgs,
        OpenOrderParams,
        OrderArgs,
        OrderType,
    )
    from py_clob_client.order_builder.constants import BUY, SELL

    CLOB_AVAILABLE = True
except ImportError:
    ClobClient = None  # type: ignore
    BalanceAllowanceParams = None  # type: ignore
    AssetType = None  # type: ignore
    BookParams = None  # type: ignore
    OrderArgs = None  # type: ignore
    MarketOrderArgs = None  # type: ignore
    OrderType = None  # type: ignore
    OpenOrderParams = None  # type: ignore
    BUY = "BUY"  # type: ignore
    SELL = "SELL"  # type: ignore
    CLOB_AVAILABLE = False


class PolyClient:
    """
    Full-featured client wrapper for Polymarket CLOB API.

    Provides limit/market order placement, cancellation, order book queries,
    balance checks, and open-order management.
    """

    def __init__(
        self,
        private_key: str,
        host: str = "https://clob.polymarket.com",
        chain_id: int = 137,
        signature_type: int = 0,
        funder: Optional[str] = None,
    ):
        self.host = host
        self.private_key = private_key
        self.chain_id = chain_id
        self.signature_type = signature_type
        self.funder = _validate_funder(funder)
        self.client: Optional[Any] = None

        if not CLOB_AVAILABLE or ClobClient is None:
            logger.error(
                "py-clob-client not installed. Run: pip install py-clob-client"
            )
            return

        try:
            self.client = ClobClient(
                self.host,
                key=self.private_key,
                chain_id=self.chain_id,
                signature_type=self.signature_type,
                funder=self.funder,
            )
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            logger.info("PolyClient initialised (host=%s, chain=%s)", host, chain_id)
        except Exception as exc:
            logger.exception("Failed to initialise ClobClient: %s", exc)
            self.client = None

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True when the underlying CLOB client is ready."""
        return self.client is not None

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC",
    ) -> Optional[Dict[str, Any]]:
        """
        Place a GTC (Good-Till-Cancelled) limit order.

        Args:
            token_id: CLOB token ID (long numeric string from clobTokenIds).
            side: "BUY" or "SELL".
            price: Price per share in [0.01 .. 0.99].
            size: Number of shares (must meet market's orderMinSize).
            order_type: "GTC" (default), "GTD", or "FOK".

        Returns:
            API response dict on success, None on failure.
        """
        if not self.client:
            logger.error("CLOB client not initialised – cannot place limit order.")
            return None

        if not CLOB_AVAILABLE or OrderArgs is None or OrderType is None:
            logger.error("py-clob-client classes not available.")
            return None

        side_upper = side.upper()
        if side_upper not in ("BUY", "SELL"):
            logger.error("Invalid side '%s'. Must be BUY or SELL.", side)
            return None

        if not (0.0 < price < 1.0):
            logger.error("Price must be in (0, 1), got %s", price)
            return None

        if size <= 0:
            logger.error("Size must be > 0, got %s", size)
            return None

        # Map string to OrderType enum
        ot_map = {
            "GTC": OrderType.GTC,
            "GTD": OrderType.GTD,
            "FOK": OrderType.FOK,
        }
        ot = ot_map.get(order_type.upper(), OrderType.GTC)

        try:
            # Round price to 2 decimal places (Polymarket tick size)
            price = round(price, 2)

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=BUY if side_upper == "BUY" else SELL,
            )
            signed_order = self.client.create_order(order_args)
            resp = self.client.post_order(signed_order, ot)
            logger.info(
                "Limit %s order posted – token=%s price=%.2f size=%.1f → %s",
                side_upper,
                token_id[:20] + "…",
                price,
                size,
                resp,
            )
            return resp
        except Exception as exc:
            logger.exception("place_limit_order failed: %s", exc)
            return None

    def place_market_order(
        self,
        token_id: str,
        side: str,
        amount: float,
    ) -> Optional[Dict[str, Any]]:
        """
        Place an FOK (Fill-Or-Kill) market order.

        Args:
            token_id: CLOB token ID.
            side: "BUY" or "SELL".
            amount: Dollar amount to spend (BUY) or shares to sell (SELL).

        Returns:
            API response dict on success, None on failure.
        """
        if not self.client:
            logger.error("CLOB client not initialised – cannot place market order.")
            return None

        if not CLOB_AVAILABLE or MarketOrderArgs is None or OrderType is None:
            logger.error("py-clob-client classes not available.")
            return None

        side_upper = side.upper()
        if side_upper not in ("BUY", "SELL"):
            logger.error("Invalid side '%s'.", side)
            return None

        if amount <= 0:
            logger.error("Amount must be > 0, got %s", amount)
            return None

        try:
            mo_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=BUY if side_upper == "BUY" else SELL,
            )
            signed_order = self.client.create_market_order(mo_args)
            resp = self.client.post_order(signed_order, OrderType.FOK)
            logger.info(
                "Market %s order posted – token=%s amount=%.2f → %s",
                side_upper,
                token_id[:20] + "…",
                amount,
                resp,
            )
            return resp
        except Exception as exc:
            logger.exception("place_market_order failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def cancel_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Cancel a single order by its ID."""
        if not self.client:
            logger.error("CLOB client not initialised.")
            return None
        try:
            resp = self.client.cancel(order_id)
            logger.info("Order cancelled: %s → %s", order_id, resp)
            return resp
        except Exception as exc:
            logger.exception("cancel_order failed: %s", exc)
            return None

    def cancel_orders(self, order_ids: List[str]) -> Optional[Dict[str, Any]]:
        """Cancel a batch of orders."""
        if not self.client:
            return None
        try:
            resp = self.client.cancel_orders(order_ids)
            logger.info("Cancelled %d orders → %s", len(order_ids), resp)
            return resp
        except Exception as exc:
            logger.exception("cancel_orders failed: %s", exc)
            return None

    def cancel_all(self) -> Optional[Dict[str, Any]]:
        """Cancel all open orders for the wallet."""
        if not self.client:
            return None
        try:
            resp = self.client.cancel_all()
            logger.info("cancel_all → %s", resp)
            return resp
        except Exception as exc:
            logger.exception("cancel_all failed: %s", exc)
            return None

    def cancel_market_orders(self, condition_id: str) -> Optional[Dict[str, Any]]:
        """Cancel all orders for a specific market (condition ID)."""
        if not self.client:
            return None
        try:
            resp = self.client.cancel_market_orders(market=condition_id)
            logger.info("cancel_market_orders(%s) → %s", condition_id[:16], resp)
            return resp
        except Exception as exc:
            logger.exception("cancel_market_orders failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Open orders
    # ------------------------------------------------------------------

    def get_open_orders(
        self,
        market: Optional[str] = None,
        asset_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Retrieve open orders, optionally filtered by market or asset.

        Returns:
            List of order dicts (may be empty).
        """
        if not self.client:
            return []

        if not CLOB_AVAILABLE or OpenOrderParams is None:
            return []

        try:
            params_kwargs: Dict[str, Any] = {}
            if market:
                params_kwargs["market"] = market
            if asset_id:
                params_kwargs["asset_id"] = asset_id
            params = OpenOrderParams(**params_kwargs)
            orders = self.client.get_orders(params)
            if orders is None:
                return []
            return list(orders) if not isinstance(orders, list) else orders
        except Exception as exc:
            logger.exception("get_open_orders failed: %s", exc)
            return []

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single order by ID."""
        if not self.client:
            return None
        try:
            return self.client.get_order(order_id)
        except Exception as exc:
            logger.exception("get_order failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Order-book / pricing
    # ------------------------------------------------------------------

    def get_order_book(self, token_id: str) -> Optional[Any]:
        """Return full order book for a token ID."""
        if not self.client:
            return None
        try:
            return self.client.get_order_book(token_id)
        except Exception as exc:
            logger.exception("get_order_book failed: %s", exc)
            return None

    def get_order_books(self, token_ids: List[str]) -> Optional[List[Any]]:
        """Return order books for multiple token IDs."""
        if not self.client or not CLOB_AVAILABLE or BookParams is None:
            return None
        try:
            params = [BookParams(token_id=tid) for tid in token_ids]
            return self.client.get_order_books(params)
        except Exception as exc:
            logger.exception("get_order_books failed: %s", exc)
            return None

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Return the midpoint price for a token."""
        if not self.client:
            return None
        try:
            mid = self.client.get_midpoint(token_id)
            return float(mid) if mid is not None else None
        except Exception as exc:
            logger.exception("get_midpoint failed: %s", exc)
            return None

    def get_price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        """Return the current price for a token on the given side."""
        if not self.client:
            return None
        try:
            p = self.client.get_price(token_id, side=side.upper())
            return float(p) if p is not None else None
        except Exception as exc:
            logger.exception("get_price failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Balance & allowance
    # ------------------------------------------------------------------

    def get_collateral_balance(self) -> Optional[float]:
        """Fetch USDC (collateral) balance for the wallet."""
        if not self.client or not CLOB_AVAILABLE or BalanceAllowanceParams is None:
            return None
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = self.client.get_balance_allowance(params)
            if result and hasattr(result, "balance"):
                return float(result.balance) / 1e6  # USDC has 6 decimals
            return None
        except Exception as exc:
            logger.exception("get_collateral_balance failed: %s", exc)
            return None

    def get_conditional_balance(self, token_id: str) -> Optional[float]:
        """Fetch conditional-token balance for a specific token."""
        if not self.client or not CLOB_AVAILABLE or BalanceAllowanceParams is None:
            return None
        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
            )
            result = self.client.get_balance_allowance(params)
            if result and hasattr(result, "balance"):
                return float(result.balance) / 1e6
            return None
        except Exception as exc:
            logger.exception("get_conditional_balance failed: %s", exc)
            return None

    def update_balance_allowance(self, token_id: Optional[str] = None) -> Optional[Any]:
        """Refresh / approve allowance for collateral or a conditional token."""
        if not self.client or not CLOB_AVAILABLE or BalanceAllowanceParams is None:
            return None
        try:
            if token_id:
                params = BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                )
            else:
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            return self.client.update_balance_allowance(params)
        except Exception as exc:
            logger.exception("update_balance_allowance failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Market data from CLOB
    # ------------------------------------------------------------------

    def get_markets(self, next_cursor: Optional[str] = None) -> Optional[Any]:
        """Get paginated markets from CLOB API."""
        if not self.client:
            return None
        try:
            if next_cursor:
                return self.client.get_simplified_markets(next_cursor=next_cursor)
            return self.client.get_simplified_markets()
        except Exception as exc:
            logger.exception("get_markets failed: %s", exc)
            return None

    def get_market(self, condition_id: str) -> Optional[Any]:
        """Get a single CLOB market by condition ID."""
        if not self.client:
            return None
        try:
            return self.client.get_market(condition_id)
        except Exception as exc:
            logger.exception("get_market failed: %s", exc)
            return None
