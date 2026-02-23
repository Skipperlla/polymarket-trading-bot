"""
PolymarketBot - Trading bot for Polymarket BTC 5-minute up/down markets
Uses PolyClient for trading operations and PolyRelayerClient for on-chain operations
"""
import json
import os
import time
import threading
from datetime import datetime
from typing import Optional, Dict, Any, List, Callable

try:
    import websocket
    import requests
    WEBSOCKET_AVAILABLE = True
except ImportError:
    websocket = None  # type: ignore
    WEBSOCKET_AVAILABLE = False

from src.client.poly_client.poly_client import PolyClient
from src.client.poly_relayer_client.poly_relayer_client import PolyRelayerClient


class PolymarketBot:
    """
    Trading bot for Polymarket BTC 5-minute up/down markets
    
    This class provides a unified interface for:
    - Trading operations via PolyClient (market/limit orders)
    - On-chain operations via PolyRelayerClient (merge/redeem tokens)
    - Real-time data via WebSocket connections
    """
    
    def __init__(
        self,
        private_key: str,
        host: str = "https://clob.polymarket.com",
        chain_id: int = 137,
        poly_client: PolyClient = None,
        poly_relayer_client: PolyRelayerClient = None,
        signature_type: int = 2,
        builder_api_key: Optional[str] = None,
        builder_secret: Optional[str] = None,
        builder_passphrase: Optional[str] = None,
        funder: Optional[str] = None,
        relayer_url: Optional[str] = None
    ):
        """
        Initialize PolymarketBot
        
        Args:
            private_key: Private key for signing transactions
            host: CLOB API host URL (default: "https://clob.polymarket.com")
            chain_id: Blockchain chain ID (default: 137 for Polygon)
            signature_type: Signature type for orders (default: 1)
            funder: Optional funder address
            relayer_url: Optional relayer URL for on-chain operations
            builder_api_key: Optional Builder API key
            builder_secret: Optional Builder API secret
            builder_passphrase: Optional Builder API passphrase
        """
        self.private_key = private_key
        self.host = host
        self.chain_id = chain_id
        self.funder = funder
        self.current_market = None
        self.current_market_id = ""
        
        # Gamma API endpoints
        self.base_url = "https://gamma-api.polymarket.com"
        self.api_url = "https://gamma-api.polymarket.com/markets"
        
        # Use provided PolyClient or create new one
        if poly_client is not None:
            self.poly_client = poly_client
        else:
            self.poly_client = PolyClient(
                private_key=private_key,
                host=host,
                chain_id=chain_id,
                signature_type=signature_type,
                funder=funder
            )
        
        # Use provided PolyRelayerClient or create new one
        if poly_relayer_client is not None:
            self.relayer_client = poly_relayer_client
        else:
            if relayer_url:
                self.relayer_client = PolyRelayerClient(
                    relayer_url=relayer_url,
                    chain_id=chain_id,
                    private_key=private_key,
                    builder_api_key=builder_api_key,
                    builder_secret=builder_secret,
                    builder_passphrase=builder_passphrase
                )
            else:
                self.relayer_client = None
                print("Warning: RELAYER_URL not provided. On-chain operations will not be available.")
        
        # WebSocket configuration
        self.ws_url = os.getenv("CLOB_WS_URL")
        self.ws = None
        self.ws_thread = None
        self.connected = False
        self.running = False
        
        # WebSocket callbacks
        self.on_message_callback: Optional[Callable] = None
        self.on_connect_callback: Optional[Callable] = None
        self.on_disconnect_callback: Optional[Callable] = None
        self.on_error_callback: Optional[Callable] = None
    
    def get_current_timestamp(self) -> int:
        """
        Get current Unix timestamp
        
        Returns:
            Current Unix timestamp as integer
        """
        return int(time.time())
    
    def generate_slug(self, timestamp: Optional[int] = None) -> str:
        """
        Generate BTC up/down 5-minute market slug from timestamp
        
        Format: btc-updown-5m-{timestamp}
        
        Args:
            timestamp: Unix timestamp. If None, uses current time
            
        Returns:
            Market slug string
        """

        
        # Round UP to next 5-minute interval
        # 5 minutes = 300 seconds
        # Formula: ((timestamp + 299) // 300) * 300
        market_timestamp = ((timestamp) // 300) * 300
        
        return f"btc-updown-5m-{market_timestamp}"
    
    def find_active_market(self, slug: Optional[str] = None) -> Optional[Dict[Any, Any]]:
        """
        Find active BTC 5-minute up/down market using Gamma API
        
        Args:
            slug: Market slug. If None, generates from current timestamp
            
        Returns:
            Market data dictionary or None if not found
        """
        current_timestamp = self.get_current_timestamp()
        
        if slug is None:
            slug = self.generate_slug(current_timestamp)
        
        try:
            
            # Use Gamma API to fetch market by slug
            response = requests.get(f"{self.base_url}/events/slug/{slug}")

            
            if response.status_code == 200:
                market_data = response.json()
                self.current_market = market_data.get("markets")[0]
                self.current_market_id = market_data.get("markets")[0].get("conditionId")
                return market_data
            else:
                print(f"Market not found: {slug} (Status: {response.status_code})")
                return None
                
        except requests.exceptions.RequestException as e:
            print(f"Error fetching market: {e}")
            return None
    
    def find_next_active_market(self) -> Optional[Dict[Any, Any]]:
        """
        Find the next active BTC 5-minute market.
        The active market timestamp is the NEXT 5-minute interval (rounded up).
        
        Returns:
            Market data dictionary or None if not found
        """
        current_timestamp = self.get_current_timestamp()
        market_timestamp = ((current_timestamp + 299) // 300) * 300
        slug = self.generate_slug(market_timestamp)
        market = self.find_active_market(slug)
        
        if market:
            print(f"Found next active market: {slug}")
            return market
        
        print(f"No next active market found for timestamp: {market_timestamp}")
        return None
    
    def place_market_order(
        self,
        token_id: str,
        side: str,
        size: float
    ) -> Optional[Dict[Any, Any]]:
        """
        Place a market order on Polymarket CLOB using PolyClient
        
        Args:
            token_id: The token ID to trade
            side: "BUY" or "SELL"
            size: Size of the order
            
        Returns:
            Order response dictionary or None if failed
        """
        if not self.poly_client.is_available():
            print("Error: PolyClient not available.")
            return None
        
        return self.poly_client.place_market_order(
            token_id=token_id,
            side=side,
            size=size
        )
    
    def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float
    ) -> Optional[Dict[Any, Any]]:
        """
        Place a limit order on Polymarket CLOB using PolyClient
        
        Args:
            token_id: The token ID to trade
            side: "BUY" or "SELL"
            price: Price per share (0.0 to 1.0)
            size: Size of the order
            
        Returns:
            Order response dictionary or None if failed
        """
        if not self.poly_client.is_available():
            print("Error: PolyClient not available.")
            return None
        
        return self.poly_client.place_limit_order(
            token_id=token_id,
            side=side,
            price=price,
            size=size
        )
    
    def merge_tokens(
        self,
        condition_id: str,
        amount: int
    ) -> Optional[Any]:
        """
        Merge outcome tokens back into collateral using PolyRelayerClient
        
        Args:
            condition_id: The condition ID of the market (bytes32)
            amount: Amount to merge (in token units, e.g., 1 * 10^6 for 1 token with 6 decimals)
            
        Returns:
            Transaction response object or None if failed
        """
        if not self.relayer_client or not self.relayer_client.is_available():
            print("Error: PolyRelayerClient not available. Cannot merge tokens.")
            return None
        
        return self.relayer_client.merge_tokens(
            condition_id=condition_id,
            amount=amount
        )
    
    def redeem_positions(
        self,
        condition_id: str,
        index_sets: Optional[List[int]] = None
    ) -> Optional[Any]:
        """
        Redeem winning outcome tokens for collateral using PolyRelayerClient
        
        Args:
            condition_id: The condition ID of the market (bytes32)
            index_sets: Index sets to redeem (default: [1, 2] for binary markets)
            
        Returns:
            Transaction response object or None if failed
        """
        if not self.relayer_client or not self.relayer_client.is_available():
            print("Error: PolyRelayerClient not available. Cannot redeem positions.")
            return None
        
        return self.relayer_client.redeem_positions(
            condition_id=condition_id,
            index_sets=index_sets
        )
    
    def connect_websocket(self, ws_url: Optional[str] = None, debug: bool = False) -> bool:
        """
        Connect to Polymarket CLOB WebSocket
        
        Args:
            ws_url: WebSocket URL (defaults to CLOB_WS_URL env var)
            debug: Enable debug mode
            
        Returns:
            True if connected successfully, False otherwise
        """
        if not WEBSOCKET_AVAILABLE:
            print("Error: websocket-client not installed. Install with: pip install websocket-client")
            return False
        
        if self.connected:
            return True
        
        ws_url = ws_url or self.ws_url
        if not ws_url:
            print("Error: WebSocket URL not provided. Set CLOB_WS_URL environment variable or pass ws_url parameter.")
            return False
        
        self._debug = debug
        
        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open
        )
        
        self.running = True
        
        def run_ws():
            try:
                self.ws.run_forever(
                    ping_interval=20,
                    ping_timeout=10
                )
            except Exception as e:
                self.connected = False
                if self._debug:
                    print(f"WebSocket error: {e}")
        
        self.ws_thread = threading.Thread(target=run_ws, daemon=True)
        self.ws_thread.start()
        
        # Wait for connection
        timeout = 15
        start_time = time.time()
        while not self.connected and (time.time() - start_time) < timeout:
            time.sleep(0.1)
        
        if not self.connected:
            return False
        
        return True
    
    def disconnect_websocket(self):
        """Disconnect from WebSocket"""
        self.running = False
        self.connected = False
        
        if self.ws:
            self.ws.close()
    
    def is_websocket_connected(self) -> bool:
        """Check if WebSocket is connected"""
        return self.connected
    
    def _on_message(self, ws, message):
        """Handle incoming WebSocket messages"""
        if not message or len(message) == 0:
            return
        
        # Skip binary ping/pong frames
        if isinstance(message, bytes):
            return
        
        try:
            data = json.loads(message)
            
            # Handle if data is a list (array of events)
            if isinstance(data, list):
                for item in data:
                    self._process_message(item)
            else:
                self._process_message(data)
                
        except json.JSONDecodeError:
            # Skip non-JSON messages (like ping/pong)
            pass
        except Exception as e:
            if hasattr(self, '_debug') and self._debug:
                print(f"Error processing message: {e}")
            if self.on_error_callback:
                self.on_error_callback(e)
    
    def _process_message(self, data: Dict):
        """Process a single message object"""
        if not isinstance(data, dict):
            return
        
        # Call user-defined callback if set
        if self.on_message_callback:
            self.on_message_callback(data)
        
        # Handle ping/pong
        event_type = data.get("event_type") or data.get("type") or data.get("event")
        if event_type == "ping":
            pong_msg = {"type": "pong"}
            if self.ws:
                self.ws.send(json.dumps(pong_msg))
    
    def _on_open(self, ws):
        """Handle WebSocket connection opened"""
        self.connected = True
        if self.on_connect_callback:
            self.on_connect_callback()
    
    def _on_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket connection closed"""
        self.connected = False
        if self.on_disconnect_callback:
            self.on_disconnect_callback(close_status_code, close_msg)
    
    def _on_error(self, ws, error):
        """Handle WebSocket errors"""
        if self.on_error_callback:
            self.on_error_callback(error)
        elif hasattr(self, '_debug') and self._debug:
            print(f"WebSocket error: {error}")
    
    def set_websocket_callbacks(
        self,
        on_message: Optional[Callable] = None,
        on_connect: Optional[Callable] = None,
        on_disconnect: Optional[Callable] = None,
        on_error: Optional[Callable] = None
    ):
        """
        Set WebSocket callback functions
        
        Args:
            on_message: Callback for incoming messages (receives message dict)
            on_connect: Callback for connection opened
            on_disconnect: Callback for connection closed (receives code, msg)
            on_error: Callback for errors (receives error)
        """
        self.on_message_callback = on_message
        self.on_connect_callback = on_connect
        self.on_disconnect_callback = on_disconnect
        self.on_error_callback = on_error
    
    def get_token_ids(self, market: Optional[Dict[Any, Any]] = None) -> Optional[Dict[str, str]]:
        """
        Get Up and Down token IDs from market data using clobTokenIds
        
        Args:
            market: Market data dictionary from Gamma API. If None, returns None
            
        Returns:
            Dictionary with 'up_token_id' and 'down_token_id' keys, or None if not found
        """
        if not market:
            return None
        
        try:
            # Extract token IDs from market data
            markets = market.get('markets', [])

            if not markets or len(markets) == 0:
                print("Market data does not contain markets array")
                return None
            
            # Get the first market (should be the main market)
            main_market = markets[0]
            clob_token_ids_raw = main_market.get('clobTokenIds', None)
            
            if clob_token_ids_raw is None:
                print("Market does not contain clobTokenIds")
                return None
            
            # clobTokenIds might be a stringified JSON array, parse it if needed
            if isinstance(clob_token_ids_raw, str):
                try:
                    clob_token_ids = json.loads(clob_token_ids_raw)
                except json.JSONDecodeError:
                    print(f"Failed to parse clobTokenIds as JSON: {clob_token_ids_raw}")
                    return None
            elif isinstance(clob_token_ids_raw, list):
                clob_token_ids = clob_token_ids_raw
            else:
                print(f"clobTokenIds is not a string or list: {type(clob_token_ids_raw)}")
                return None

            if len(clob_token_ids) < 2:
                print(f"Market does not have enough clobTokenIds: {clob_token_ids}")
                return None
            
            # Extract Up and Down token IDs
            up_token_id = clob_token_ids[0]
            down_token_id = clob_token_ids[1]
            
            if up_token_id and down_token_id:
                return {
                    'up_token_id': up_token_id,
                    'down_token_id': down_token_id
                }
            else:
                print(f"Could not extract token IDs from clobTokenIds: {clob_token_ids}")
                return None
                
        except Exception as e:
            print(f"Error extracting token IDs: {e}")
            import traceback
            traceback.print_exc()
            return None
