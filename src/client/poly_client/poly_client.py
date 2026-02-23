"""
PolyClient - Wrapper for Polymarket CLOB Client
"""
from typing import Optional, Dict, Any

try:
    from py_clob_client.clob_types import OrderType, OrderArgs, MarketOrderArgs
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON, ZERO_ADDRESS
    from py_clob_client.order_builder.constants import BUY, SELL
    CLOB_AVAILABLE = True
except ImportError:
    ClobClient = None  # type: ignore
    POLYGON = 137
    ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
    OrderArgs = None  # type: ignore
    MarketOrderArgs = None  # type: ignore
    OrderType = None  # type: ignore
    BUY = "BUY"  # type: ignore
    SELL = "SELL"  # type: ignore
    CLOB_AVAILABLE = False


class PolyClient:
    """
    Client wrapper for Polymarket CLOB API
    
    This class provides a simplified interface to interact with the Polymarket
    CLOB (Central Limit Order Book) API for placing and managing orders.
    """
    
    def __init__(
        self,
        private_key: str,
        host: str,
        chain_id: int,
        signature_type: int,
        funder: Optional[str] = None
    ):
        """
        Initialize PolyClient
        
        Args:
            private_key: Private key for signing transactions
            host: CLOB API host URL (e.g., "https://clob.polymarket.com")
            chain_id: Blockchain chain ID (e.g., 137 for Polygon)
            signature_type: Signature type for orders
            funder: Optional funder address
        """
        self.private_key = private_key
        self.host = host
        self.chain_id = chain_id
        self.signature_type = signature_type
        self.funder = funder
        
        # Initialize CLOB client if available
        if CLOB_AVAILABLE and ClobClient is not None:
            self.client = ClobClient(
                self.host,
                key=self.private_key,
                chain_id=self.chain_id,
                signature_type=self.signature_type,
                funder=self.funder
            )
            # Set API credentials
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
        else:
            self.client = None
            if not CLOB_AVAILABLE:
                print("Warning: py-clob-client not installed. Install with: pip install py-clob-client")
    
    def is_available(self) -> bool:
        """Check if CLOB client is available and initialized"""
        return self.client is not None
    
    def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: OrderType = OrderType.GTC if OrderType else None
    ) -> Optional[Dict[Any, Any]]:
        """
        Place a limit order on Polymarket CLOB
        
        Args:
            token_id: The token ID to trade
            side: "BUY" or "SELL"
            price: Price per share (0.0 to 1.0)
            size: Size of the order
            order_type: Order type (default: GTC - Good Till Cancelled)
            
        Returns:
            Order response dictionary or None if failed
        """
        if not self.client:
            print("Error: CLOB client not initialized.")
            return None
        
        if not CLOB_AVAILABLE or OrderArgs is None:
            print("Error: py-clob-client not available.")
            return None
        
        if side.upper() not in ["BUY", "SELL"]:
            print(f"Error: Invalid side '{side}'. Must be 'BUY' or 'SELL'")
            return None
        
        if not (0.0 <= price <= 1.0):
            print(f"Error: Price must be between 0.0 and 1.0, got {price}")
            return None
        
        try:
            order = OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(size),
                side=BUY if side.upper() == "BUY" else SELL
            )
            
            # Create signed order
            signed = self.client.create_order(order)
            
            # Post order
            order_type = order_type or OrderType.GTC
            resp = self.client.post_order(signed, order_type)
            
            return resp
        except Exception as e:
            print(f"Error placing limit order: {e}")
            return None
    
    def place_market_order(
        self,
        token_id: str,
        side: str,
        size: float
    ) -> Optional[Dict[Any, Any]]:
        """
        Place a market order on Polymarket CLOB
        
        Args:
            token_id: The token ID to trade
            side: "BUY" or "SELL"
            size: Size of the order
            
        Returns:
            Order response dictionary or None if failed
        """
        if not self.client:
            print("Error: CLOB client not initialized.")
            return None
        
        if not CLOB_AVAILABLE or MarketOrderArgs is None:
            print("Error: py-clob-client not available.")
            return None
        
        if side.upper() not in ["BUY", "SELL"]:
            print(f"Error: Invalid side '{side}'. Must be 'BUY' or 'SELL'")
            return None
        
        try:
            order = MarketOrderArgs(
                token_id=token_id,
                amount=float(size),
                side=BUY if side.upper() == "BUY" else SELL,
                order_type=OrderType.FOK
            )
            
            # Create signed market order
            signed = self.client.create_market_order(order)
            
            # Post order with FOK (Fill or Kill) type
            resp = self.client.post_order(signed, OrderType.FOK)
            
            return resp
        except Exception as e:
            print(f"Error placing market order: {e}")
            return None
    
    def cancel_order(
        self,
        order_id: str,
        order_type: OrderType = OrderType.FOK if OrderType else None
    ) -> Optional[Dict[Any, Any]]:
        """
        Cancel an order on Polymarket CLOB
        
        Args:
            order_id: The order ID to cancel
            order_type: Order type (default: IOC - Immediate or Cancel)
            
        Returns:
            Cancel response dictionary or None if failed
        """
        if not self.client:
            print("Error: CLOB client not initialized.")
            return None
        
        if not CLOB_AVAILABLE:
            print("Error: py-clob-client not available.")
            return None
        
        try:
            order_type = order_type or OrderType.IOC
            resp = self.client.cancel_order(order_id, order_type)
            return resp
        except Exception as e:
            print(f"Error canceling order: {e}")
            return None
    
    def get_orders(
        self,
        market: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None
    ) -> Optional[Dict[Any, Any]]:
        """
        Get orders from Polymarket CLOB
        
        Args:
            market: Optional market/condition ID to filter by
            status: Optional status filter (e.g., "OPEN", "FILLED", "CANCELLED")
            limit: Optional limit on number of orders to return
            
        Returns:
            Orders response dictionary or None if failed
        """
        if not self.client:
            print("Error: CLOB client not initialized.")
            return None
        
        try:
            # Use the client's method to get orders
            # Note: Actual method name may vary based on py-clob-client version
            if hasattr(self.client, 'get_orders'):
                return self.client.get_orders(market=market, status=status, limit=limit)
            else:
                print("Warning: get_orders method not available on client")
                return None
        except Exception as e:
            print(f"Error getting orders: {e}")
            return None
