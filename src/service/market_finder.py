"""
MarketFinder - Gamma API market discovery service for Polymarket.

Provides functionality to:
  - Search markets by keyword, slug, or tag
  - Find active/open markets accepting orders
  - Filter by volume, liquidity, end date, spread
  - Extract token IDs and condition IDs from market data
  - Find BTC 5-minute up/down markets (slug-based)
  - Discover trending / high-volume markets
"""

import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("MarketFinder")

# Gamma API base
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
GAMMA_MARKETS_URL = f"{GAMMA_API_BASE}/markets"
GAMMA_EVENTS_URL = f"{GAMMA_API_BASE}/events"

# Default request timeout (seconds)
REQUEST_TIMEOUT = 15


class MarketFinder:
    """
    Service for discovering and filtering Polymarket markets via the Gamma API.
    """

    def __init__(self, base_url: str = GAMMA_API_BASE, timeout: int = REQUEST_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.markets_url = f"{self.base_url}/markets"
        self.events_url = f"{self.base_url}/events"
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "PolymarketBot/1.0",
            }
        )

    # ------------------------------------------------------------------
    # Core API calls
    # ------------------------------------------------------------------

    def fetch_markets(
        self,
        limit: int = 50,
        offset: int = 0,
        active: Optional[bool] = True,
        closed: Optional[bool] = False,
        order: str = "volume24hr",
        ascending: bool = False,
        slug: Optional[str] = None,
        slug_contains: Optional[str] = None,
        tag: Optional[str] = None,
        accepting_orders: Optional[bool] = True,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch markets from the Gamma API with filtering.

        Args:
            limit: Max number of results (default 50, max ~100).
            offset: Pagination offset.
            active: Only active markets (default True).
            closed: Only closed markets (default False).
            order: Sort field (e.g. "volume24hr", "liquidityNum", "createdAt").
            ascending: Sort direction.
            slug: Exact slug match.
            slug_contains: Partial slug match.
            tag: Filter by tag/category.
            accepting_orders: Only markets currently accepting orders.
            extra_params: Any additional query parameters.

        Returns:
            List of market dicts from Gamma API.
        """
        params: Dict[str, Any] = {
            "limit": min(limit, 100),
            "offset": offset,
            "order": order,
            "ascending": str(ascending).lower(),
        }

        if active is not None:
            params["active"] = str(active).lower()
        if closed is not None:
            params["closed"] = str(closed).lower()
        if slug:
            params["slug"] = slug
        if slug_contains:
            params["slug_contains"] = slug_contains
        if tag:
            params["tag"] = tag
        if accepting_orders is not None:
            params["acceptingOrders"] = str(accepting_orders).lower()
        if extra_params:
            params.update(extra_params)

        try:
            resp = self._session.get(
                self.markets_url, params=params, timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            # Sometimes API returns a wrapper
            if isinstance(data, dict) and "data" in data:
                return data["data"]
            return []
        except requests.RequestException as exc:
            logger.error("fetch_markets failed: %s", exc)
            return []
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("fetch_markets JSON parse error: %s", exc)
            return []

    def fetch_market_by_id(self, market_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single market by its numeric ID."""
        try:
            resp = self._session.get(
                f"{self.markets_url}/{market_id}", timeout=self.timeout
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("fetch_market_by_id(%s) failed: %s", market_id, exc)
            return None

    def fetch_market_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """Fetch a single market by its slug (exact match)."""
        results = self.fetch_markets(limit=1, slug=slug)
        return results[0] if results else None

    def fetch_market_by_condition_id(
        self, condition_id: str
    ) -> Optional[Dict[str, Any]]:
        """Fetch a market by its condition ID."""
        results = self.fetch_markets(
            limit=1, extra_params={"conditionId": condition_id}
        )
        return results[0] if results else None

    # ------------------------------------------------------------------
    # Market search / discovery
    # ------------------------------------------------------------------

    def search_markets(
        self,
        query: str,
        limit: int = 20,
        active_only: bool = True,
        min_volume_24h: float = 0,
        min_liquidity: float = 0,
    ) -> List[Dict[str, Any]]:
        """
        Search for markets whose question or slug contains the query string.

        Args:
            query: Search term (checked against question and slug).
            limit: Max results to return.
            active_only: Only return active markets.
            min_volume_24h: Minimum 24h volume filter.
            min_liquidity: Minimum liquidity filter.

        Returns:
            Filtered list of market dicts.
        """
        # Gamma API doesn't have a full-text search, so we fetch a larger set
        # and filter client-side
        all_markets = self.fetch_markets(
            limit=100,
            active=True if active_only else None,
            closed=False if active_only else None,
        )

        query_lower = query.lower()
        filtered = []
        for m in all_markets:
            question = (m.get("question") or "").lower()
            slug = (m.get("slug") or "").lower()
            description = (m.get("description") or "").lower()

            if (
                query_lower not in question
                and query_lower not in slug
                and query_lower not in description
            ):
                continue

            vol_24h = float(m.get("volume24hr") or 0)
            liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)

            if vol_24h < min_volume_24h:
                continue
            if liq < min_liquidity:
                continue

            filtered.append(m)

            if len(filtered) >= limit:
                break

        return filtered

    def get_trending_markets(
        self,
        limit: int = 10,
        min_volume_24h: float = 1000,
        min_liquidity: float = 500,
    ) -> List[Dict[str, Any]]:
        """
        Get trending markets sorted by 24h volume.

        Returns:
            List of active, high-volume markets.
        """
        markets = self.fetch_markets(
            limit=100,
            active=True,
            closed=False,
            order="volume24hr",
            ascending=False,
            accepting_orders=True,
        )

        return [
            m
            for m in markets
            if float(m.get("volume24hr") or 0) >= min_volume_24h
            and float(m.get("liquidityNum") or m.get("liquidity") or 0) >= min_liquidity
        ][:limit]

    def get_markets_by_spread(
        self,
        max_spread: float = 0.05,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Find markets with tight spreads (good for market-making strategies).

        Args:
            max_spread: Maximum spread (bestAsk - bestBid).
            limit: Max results.

        Returns:
            Markets with spread <= max_spread, sorted by spread ascending.
        """
        markets = self.fetch_markets(
            limit=100,
            active=True,
            closed=False,
            accepting_orders=True,
        )

        with_spread = []
        for m in markets:
            spread = float(m.get("spread") or 999)
            if spread <= max_spread:
                m["_spread"] = spread
                with_spread.append(m)

        with_spread.sort(key=lambda x: x["_spread"])
        return with_spread[:limit]

    # ------------------------------------------------------------------
    # BTC 5-minute up/down market discovery
    # ------------------------------------------------------------------

    @staticmethod
    def generate_btc_5m_slug(timestamp: Optional[int] = None) -> str:
        """
        Generate the slug for a BTC 5-minute up/down market.

        Polymarket BTC 5m markets follow the pattern:
            will-btc-go-up-or-down-5-min-{rounded_ts}

        The timestamp is rounded DOWN to the nearest 5-minute boundary.

        Args:
            timestamp: Unix timestamp. If None, uses current time.

        Returns:
            Market slug string.
        """
        if timestamp is None:
            timestamp = int(time.time())

        # Round down to nearest 5-minute boundary (300 seconds)
        rounded = (timestamp // 300) * 300
        return f"will-btc-go-up-or-down-5-min-{rounded}"

    @staticmethod
    def next_btc_5m_timestamp() -> int:
        """
        Get the next 5-minute boundary timestamp (rounded UP from now).
        """
        now = int(time.time())
        return ((now // 300) + 1) * 300

    @staticmethod
    def current_btc_5m_timestamp() -> int:
        """
        Get the current 5-minute boundary timestamp (rounded DOWN from now).
        """
        now = int(time.time())
        return (now // 300) * 300

    def find_btc_5m_market(
        self, timestamp: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Find a BTC 5-minute up/down market for the given timestamp.

        Tries multiple slug patterns since Polymarket may use different naming.

        Args:
            timestamp: Unix timestamp (defaults to current 5m boundary).

        Returns:
            Market dict or None if not found.
        """
        if timestamp is None:
            timestamp = self.current_btc_5m_timestamp()

        rounded = (timestamp // 300) * 300

        # Try several known slug patterns
        slug_patterns = [
            f"will-btc-go-up-or-down-5-min-{rounded}",
            f"btc-updown-5m-{rounded}",
            f"btc-5m-{rounded}",
        ]

        for slug in slug_patterns:
            market = self.fetch_market_by_slug(slug)
            if market:
                logger.info(
                    "Found BTC 5m market: slug=%s id=%s", slug, market.get("id")
                )
                return market

        # Fallback: search by slug_contains
        results = self.fetch_markets(
            limit=5,
            active=True,
            closed=False,
            slug_contains="btc",
            accepting_orders=True,
            extra_params={"order": "startDate", "ascending": "false"},
        )

        for m in results:
            slug = (m.get("slug") or "").lower()
            if "5-min" in slug or "5m" in slug:
                logger.info(
                    "Found BTC 5m market via search: slug=%s id=%s",
                    m.get("slug"),
                    m.get("id"),
                )
                return m

        logger.warning("No BTC 5m market found for timestamp %d", timestamp)
        return None

    def find_next_btc_5m_market(self) -> Optional[Dict[str, Any]]:
        """
        Find the next upcoming BTC 5-minute market.

        Tries:
          1. Next 5m boundary
          2. The one after that
          3. Current boundary (might still be active)
        """
        next_ts = self.next_btc_5m_timestamp()

        # Try next boundary
        market = self.find_btc_5m_market(next_ts)
        if market:
            return market

        # Try one after that
        market = self.find_btc_5m_market(next_ts + 300)
        if market:
            return market

        # Try current boundary
        market = self.find_btc_5m_market(self.current_btc_5m_timestamp())
        if market:
            return market

        return None

    # ------------------------------------------------------------------
    # Token ID extraction
    # ------------------------------------------------------------------

    @staticmethod
    def extract_token_ids(market: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """
        Extract outcome token IDs from a Gamma API market object.

        For binary (Yes/No or Up/Down) markets, returns a dict with
        keys 'yes_token_id' / 'no_token_id' (and aliases
        'up_token_id' / 'down_token_id').

        Args:
            market: Market dict from Gamma API.

        Returns:
            Dict with token IDs, or None if not extractable.
        """
        clob_token_ids_raw = market.get("clobTokenIds")
        outcomes_raw = market.get("outcomes")

        if not clob_token_ids_raw:
            logger.warning("Market %s has no clobTokenIds", market.get("id"))
            return None

        # Parse JSON strings if needed
        if isinstance(clob_token_ids_raw, str):
            try:
                clob_token_ids = json.loads(clob_token_ids_raw)
            except json.JSONDecodeError:
                logger.error("Failed to parse clobTokenIds: %s", clob_token_ids_raw)
                return None
        else:
            clob_token_ids = clob_token_ids_raw

        if isinstance(outcomes_raw, str):
            try:
                outcomes = json.loads(outcomes_raw)
            except json.JSONDecodeError:
                outcomes = ["Yes", "No"]
        else:
            outcomes = outcomes_raw or ["Yes", "No"]

        if len(clob_token_ids) < 2:
            logger.warning(
                "Market %s has fewer than 2 token IDs: %s",
                market.get("id"),
                clob_token_ids,
            )
            return None

        # Map outcomes to token IDs
        result: Dict[str, str] = {}

        for i, (outcome, token_id) in enumerate(zip(outcomes, clob_token_ids)):
            outcome_lower = outcome.lower().strip()
            result[f"outcome_{i}_token_id"] = token_id
            result[f"outcome_{i}_label"] = outcome

            if outcome_lower in ("yes", "up"):
                result["yes_token_id"] = token_id
                result["up_token_id"] = token_id
            elif outcome_lower in ("no", "down"):
                result["no_token_id"] = token_id
                result["down_token_id"] = token_id

        # Fallback if outcomes don't match Yes/No or Up/Down
        if "yes_token_id" not in result:
            result["yes_token_id"] = clob_token_ids[0]
            result["up_token_id"] = clob_token_ids[0]
        if "no_token_id" not in result:
            result["no_token_id"] = clob_token_ids[1]
            result["down_token_id"] = clob_token_ids[1]

        return result

    @staticmethod
    def extract_condition_id(market: Dict[str, Any]) -> Optional[str]:
        """Extract the condition ID from a market dict."""
        return market.get("conditionId") or market.get("condition_id")

    @staticmethod
    def extract_market_info(market: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract a clean summary of market information.

        Returns a dict with commonly needed fields.
        """
        outcomes_raw = market.get("outcomes")
        if isinstance(outcomes_raw, str):
            try:
                outcomes = json.loads(outcomes_raw)
            except json.JSONDecodeError:
                outcomes = []
        else:
            outcomes = outcomes_raw or []

        prices_raw = market.get("outcomePrices")
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except json.JSONDecodeError:
                prices = []
        else:
            prices = prices_raw or []

        return {
            "id": market.get("id"),
            "question": market.get("question"),
            "slug": market.get("slug"),
            "condition_id": market.get("conditionId"),
            "outcomes": outcomes,
            "outcome_prices": [float(p) for p in prices] if prices else [],
            "best_bid": float(market.get("bestBid") or 0),
            "best_ask": float(market.get("bestAsk") or 0),
            "spread": float(market.get("spread") or 0),
            "volume_24h": float(market.get("volume24hr") or 0),
            "volume_total": float(market.get("volumeNum") or market.get("volume") or 0),
            "liquidity": float(
                market.get("liquidityNum") or market.get("liquidity") or 0
            ),
            "active": market.get("active", False),
            "closed": market.get("closed", False),
            "accepting_orders": market.get("acceptingOrders", False),
            "end_date": market.get("endDate"),
            "start_date": market.get("startDate"),
            "neg_risk": market.get("negRisk", False),
            "order_min_size": float(market.get("orderMinSize") or 5),
            "min_tick_size": float(market.get("orderPriceMinTickSize") or 0.01),
            "last_trade_price": float(market.get("lastTradePrice") or 0),
        }

    # ------------------------------------------------------------------
    # Strategy helpers
    # ------------------------------------------------------------------

    def find_undervalued_markets(
        self,
        threshold: float = 0.15,
        min_liquidity: float = 1000,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Find markets where one outcome is priced very low (potential value bet).

        Args:
            threshold: Max price for "cheap" outcome (default 0.15 = 15 cents).
            min_liquidity: Minimum liquidity filter.
            limit: Max results.

        Returns:
            List of markets with at least one outcome priced below threshold.
        """
        markets = self.fetch_markets(
            limit=100,
            active=True,
            closed=False,
            accepting_orders=True,
            order="volume24hr",
            ascending=False,
        )

        results = []
        for m in markets:
            liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
            if liq < min_liquidity:
                continue

            prices_raw = m.get("outcomePrices")
            if isinstance(prices_raw, str):
                try:
                    prices = json.loads(prices_raw)
                except json.JSONDecodeError:
                    continue
            else:
                prices = prices_raw or []

            for p in prices:
                if 0 < float(p) <= threshold:
                    results.append(m)
                    break

            if len(results) >= limit:
                break

        return results

    def find_close_to_expiry(
        self,
        hours_until_expiry: int = 24,
        min_liquidity: float = 500,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Find markets expiring within the given timeframe.

        Good for strategies that exploit price convergence near expiry.

        Args:
            hours_until_expiry: Maximum hours until market end.
            min_liquidity: Minimum liquidity.
            limit: Max results.

        Returns:
            List of markets expiring soon.
        """
        markets = self.fetch_markets(
            limit=100,
            active=True,
            closed=False,
            accepting_orders=True,
            order="endDate",
            ascending=True,
        )

        now = datetime.now(timezone.utc)
        results = []

        for m in markets:
            end_str = m.get("endDate")
            if not end_str:
                continue

            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue

            diff_hours = (end_dt - now).total_seconds() / 3600
            if 0 < diff_hours <= hours_until_expiry:
                liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
                if liq >= min_liquidity:
                    m["_hours_until_expiry"] = round(diff_hours, 1)
                    results.append(m)

            if len(results) >= limit:
                break

        return results

    # ------------------------------------------------------------------
    # Events API
    # ------------------------------------------------------------------

    def fetch_events(
        self,
        limit: int = 20,
        active: bool = True,
        closed: bool = False,
        order: str = "volume",
        ascending: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Fetch events (groups of related markets) from the Gamma API.
        """
        params: Dict[str, Any] = {
            "limit": min(limit, 100),
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        try:
            resp = self._session.get(
                self.events_url, params=params, timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.error("fetch_events failed: %s", exc)
            return []
