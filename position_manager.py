#!/usr/bin/env python3
"""
Position Manager Bot - Automated position management for Polymarket BTC 5-min markets

Flow:
1. START MARKET
2. CHECK BALANCE
   - [Insufficient] → STOP BOT
   - [Enough] → Continue
3. CHECK POSITIONS LOOP
   - [Equal Shares] → MERGE → NEXT MARKET
   - [Not Equal]
     - [30s before close?] → FORCE SELL → NEXT MARKET
     - [No] → WAIT 60s → RECHECK
"""
import requests
import os
import time
from datetime import datetime
from typing import Optional, Dict, Any

# Try to load from .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from web3 import Web3
    WEB3_AVAILABLE = True
except ImportError:
    WEB3_AVAILABLE = False
    print("Warning: web3.py not installed. Install with: pip install web3")


from src.service.polymarket_bot import PolymarketBot

def format_time(seconds: int) -> str:
    """Format seconds into readable time string"""
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    return f"{minutes}m {secs}s"


def get_balance(address: str) -> float:
    """
    Get USDC balance for a wallet address using Polygon RPC
    Equivalent to the reference TypeScript function
    
    Args:
        address: Wallet address to check balance for
        
    Returns:
        USDC balance as float
    """
    if not WEB3_AVAILABLE:
        print("⚠️  web3.py not available. Cannot check balance.")
        return 0.0
    
    try:
        # Get Polygon RPC URL from environment
        RPC_URL = os.getenv(
            "POLYGON_RPC",
            "https://go.getblock.us/f3ba334a60f1446c9289381e569b2634"
        )
        
        # USDC contract address on Polygon
        USDC_CONTRACT_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        
        # USDC ERC20 ABI (minimal - just balanceOf)
        USDC_ABI = [
            {
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function"
            }
        ]
        
        # Create RPC provider (equivalent to ethers JsonRpcProvider)
        rpc_provider = Web3(Web3.HTTPProvider(RPC_URL))
        
        if not rpc_provider.is_connected():
            print(f"⚠️  Failed to connect to Polygon RPC: {RPC_URL}")
            return 0.0
        
        # Create USDC contract instance (equivalent to ethers.Contract)
        usdc_contract = rpc_provider.eth.contract(
            address=Web3.to_checksum_address(USDC_CONTRACT_ADDRESS),
            abi=USDC_ABI
        )
        
        # Get balance (equivalent to contract.balanceOf(address))
        balance_usdc = usdc_contract.functions.balanceOf(
            Web3.to_checksum_address(address)
        ).call()
        
        # Format units with 6 decimals (equivalent to ethers.utils.formatUnits(balance, 6))
        balance_usdc_real = balance_usdc / (10 ** 6)
        
        # Return as float (equivalent to parseFloat)
        return float(balance_usdc_real)
        
    except Exception as e:
        print(f"⚠️  Error getting balance: {e}")
        import traceback
        traceback.print_exc()
        return 0.0


def check_balance_sufficient(bot: PolymarketBot, min_balance: float = 0.01) -> bool:
    """
    Check if account has sufficient USDC balance using Polygon RPC
    
    Args:
        bot: PolymarketBot instance
        min_balance: Minimum required balance in USDC
        
    Returns:
        True if balance is sufficient, False otherwise
    """
    if not bot.private_key:
        print("⚠️  Private key not available. Cannot check balance.")
        return True
    
    try:
        # Get wallet address from private key
        if not WEB3_AVAILABLE:
            print("⚠️  web3.py not available. Cannot check balance via RPC.")
            print("⚠️  Assuming sufficient balance. Install web3: pip install web3")
            return True
        funder = os.getenv("FUNDER")
        print(f"📧 Wallet address: {funder}")
        
        # Get balance using the reference function pattern
        balance_float = get_balance(funder)
        
        print(f"💰 USDC Balance: {balance_float:.4f}")
        min_balance = float(os.getenv("ORDER_PRICE")) * float(os.getenv("ORDER_SIZE")) * 2
        print(f"💰 Min balance: {min_balance:.4f}")
        if balance_float < min_balance:
            print(f"❌ Insufficient balance: {balance_float:.4f} < {min_balance:.4f}")
            return False
        
        print(f"✅ Sufficient balance: {balance_float:.4f} >= {min_balance:.4f}")
        return True
        
    except Exception as e:
        print(f"⚠️  Error checking balance: {e}")
        import traceback
        traceback.print_exc()
        # Assume sufficient if we can't check
        print("⚠️  Assuming sufficient balance due to error.")
        return True


def are_positions_equal(positions: Dict[str, float], tolerance: float = 0.01) -> bool:
    """
    Check if UP and DOWN positions are approximately equal
    
    Args:
        positions: Dictionary with 'up_balance' and 'down_balance'
        tolerance: Maximum difference to consider equal
        
    Returns:
        True if positions are equal within tolerance
    """
    up = positions.get("up_balance", 0.0)
    down = positions.get("down_balance", 0.0)
    
    diff = abs(up - down)
    return diff <= tolerance


def get_min_position(positions: Dict[str, float]) -> float:
    """Get the minimum of UP and DOWN positions"""
    up = positions.get("up_balance", 0.0)
    down = positions.get("down_balance", 0.0)
    return min(up, down)


def is_near_market_close( close_time: int, seconds_before: int = 30) -> bool:
    """
    Check if market is closing within specified seconds
    
    Args:
        bot: PolymarketBot instance
        market: Market data dictionary
        seconds_before: How many seconds before close to trigger
        
    Returns:
        True if market closes within seconds_before
    """
    close_time = close_time
    if not close_time:
        # If we can't determine close time, assume not close
        return False
    
    current_time = time.time()
    time_until_close = close_time - current_time
    
    return time_until_close <= seconds_before


def process_market(bot: PolymarketBot, market: Dict[Any, Any], token_ids: Dict[str, str]) -> bool:
    """
    Process a single market according to the flow
    
    Args:
        bot: PolymarketBot instance
        market: Market data dictionary
        token_ids: Dictionary with 'up_token_id' and 'down_token_id'
        
    Returns:
        True if should continue to next market, False if should stop
    """
    print("\n" + "="*60)
    print(f"📊 Processing Market")
    print("="*60)
    
    # Check balance
    print("\n1️⃣  Checking balance...")
    if not check_balance_sufficient(bot):
        print("\n❌ Insufficient balance. Stopping bot.")
        return False
    
    # Get market close time
    dt = datetime.fromisoformat(market.get("endDate").replace("Z", "+00:00"))
    close_time = int(dt.timestamp())
    if close_time:
        close_dt = datetime.fromtimestamp(close_time)
        print(f"⏰ Market closes at: {close_dt.strftime('%H:%M:%S')}")
    
    # Position check loop
    print("\n2️⃣  Entering position check loop...")
    iteration = 0
    
    while True:
        iteration += 1
        current_time = bot.get_current_timestamp()
        close_flag = False
        if close_time:
            time_until_close = close_time - current_time
            if time_until_close > 0:
                print(f"\n⏱️  Time until close: {format_time(time_until_close)}")
            else:
                close_flag = True
                print(f"\n⏱️  Market has closed")
        
        # Check positions
        print(f"\n📈 Checking positions (iteration {iteration})...")

        positions = get_positions(bot, token_ids)

        if positions["up_balance"] > 0.0 and positions["down_balance"] > 0.0:
            if not close_flag:
            # Store market in bot for conditionId extraction
                merge_amount = min(positions["up_balance"], positions["down_balance"])
                print(f"Merge amount: {merge_amount}")
                result = bot.merge_tokens(bot.current_market_id, int(merge_amount * 10 ** 6))
                if result:
                    print(f"✅ Successfully merged {merge_amount:.6f} tokens")
                else:
                    print("❌ Merge failed. Retrying in 60s...")
                    time.sleep(30)
                    continue
        
        
        # Check if 30s before close
        if is_near_market_close(close_time, seconds_before=30):
            print("⏰ Market closes in 30s or less. Force selling all positions...")
            if not close_flag:
                if positions["up_balance"] > 0.0:
                    results = bot.place_market_order(token_ids["up_token_id"], "SELL", positions["up_balance"])
                    if results:
                        print("✅ Force sell completed")
                    else:
                        print("❌ Force sell failed. Retrying in 60s...")
                        time.sleep(1)
                        continue
                if positions["down_balance"] > 0.0:
                    results = bot.place_market_order(token_ids["down_token_id"], "SELL", positions["down_balance"])
                    if results:
                        print("✅ Force sell completed")
                    else:
                        print("❌ Force sell failed. Retrying in 60s...")
                        time.sleep(1)
                        continue
                print("\n➡️  Moving to next market...")
                # Place orders for next epoch market
                # Get order parameters from environment
                order_price = float(os.getenv("ORDER_PRICE", "0.46"))
                order_size = float(os.getenv("ORDER_SIZE", "5.0"))
                
                next_market = bot.find_next_active_market()
                if next_market:
                    next_token_ids = bot.get_token_ids(next_market)
                    if next_token_ids:
                        print(f"\n📋 Placing limit orders for next epoch market...")
                        print(f"  Price: {order_price}, Size: {order_size}")
                        print(f"  UP token: {next_token_ids['up_token_id']}")
                        print(f"  DOWN token: {next_token_ids['down_token_id']}")
                        up_order = bot.place_limit_order(
                            token_id=next_token_ids["up_token_id"],
                            price=order_price,
                            size=order_size,
                            side="BUY"
                        )
                        down_order = bot.place_limit_order(
                            token_id=next_token_ids["down_token_id"],
                            price=order_price,
                            size=order_size,
                            side="BUY"
                        )
                        if up_order and down_order:
                            print("✅ Orders placed for next market. Moving to next market...")
                            return True
                        else:
                            print("⚠️  Failed to place orders for next market")
                            return False
                    else:
                        print("❌ Could not extract token IDs from next market.")
                        return False
                else:
                    print("❌ Could not find next market. ")
                    return False
        
        # Wait 60s and recheck
        print("⏳ Waiting 30s before rechecking...")
        time.sleep(30)


def get_positions_balance(bot: PolymarketBot, token_id: str) -> float:
    """
    Get balance for a specific token
    
    Args:
        token_id: Token ID to check balance for
        
    Returns:
        Balance as float, or 0.0 if error
    """

    params = {
        "market": bot.current_market_id,
        "status": "OPEN",
        "limit": 50,
        "user": bot.funder
    }
    url = os.getenv("GET_POSITION_URL")
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.text != []:
            data = response.json()
            if data[0]["token"] == token_id:
                return float(data[0]["positions"][0]["size"])
            else:
                return float(data[1]["positions"][0]["size"])
        else:
            print("No data found for token_id: ", token_id)
            return 0.0
    except Exception as e:

            return 0.0

def get_positions(bot: PolymarketBot, token_ids: Dict[str, str]) -> Dict[str, float]:
    """
    Get positions (balances) for both UP and DOWN tokens
    
    Args:
        token_ids: Dictionary with 'up_token_id' and 'down_token_id'
        
    Returns:
        Dictionary with 'up_balance' and 'down_balance'
    """
    if not token_ids:
        return {"up_balance": 0.0, "down_balance": 0.0}
    
    up_balance = get_positions_balance(bot, token_ids.get("up_token_id"))
    down_balance = get_positions_balance(bot, token_ids.get("down_token_id"))
    print("up_balance", up_balance * 10 ** 6)
    print("down_balance", down_balance * 10 ** 6)
    return {
        "up_balance": up_balance,
        "down_balance": down_balance
    }


def main():
    """Main function for position management bot"""
    
    # Load configuration
    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        print("❌ ERROR: PRIVATE_KEY not set. Export it or add to .env")
        return
    
    host = os.getenv("HOST", "https://clob.polymarket.com")
    funder = os.getenv("FUNDER")
    chain_id = int(os.getenv("CHAIN_ID", 137))
    signature_type = int(os.getenv("SIGNATURE_TYPE", 2))
    builder_api_key=os.getenv("BUILDER_API_KEY")
    builder_secret=os.getenv("BUILDER_SECRET")
    builder_passphrase=os.getenv("BUILDER_PASS_PHRASE")
    relayer_url=os.getenv("RELAYER_URL")


    # Initialize bot
    print("🤖 Initializing Position Manager Bot...")
    bot = PolymarketBot(
        private_key=private_key,
        host=host,
        relayer_url=relayer_url,
        chain_id=chain_id,
        signature_type=signature_type,
        funder=funder,
        builder_api_key=builder_api_key,
        builder_secret=builder_secret,
        builder_passphrase=builder_passphrase,
    )
    print(bot.poly_client.is_available())
    print(bot.relayer_client.is_available())
    if not bot.poly_client.is_available() or not bot.relayer_client.is_available():
        print("❌ poly client or relayer client failed to initialize. Check credentials.")
        return
    
    print("✅ Bot initialized successfully\n")
    
    # Main loop: process markets continuously
    market_count = 0
    
    try:
        while True:
            market_count += 1
            print("\n" + "="*60)
            print(f"🚀 START MARKET #{market_count}")
            print("="*60)
            
            # Find current market
            print("\n🔍 Finding current BTC 5-min market...")
            market = bot.find_active_market()
            
            if not market:
                print("❌ Could not find active market. Waiting 30s...")
                time.sleep(30)
                continue
            
            # Get token IDs
            token_ids = bot.get_token_ids(market)
            if not token_ids:
                print("❌ Could not extract token IDs. Waiting 30s...")
                time.sleep(30)
                continue
            
            print(f"✅ Market found")
            print(f"  UP token:  {token_ids['up_token_id']}")
            print(f"  DOWN token: {token_ids['down_token_id']}")
            
            # Process market
            should_continue = process_market(bot, market, token_ids)
            
            if not should_continue:
                print("\n🛑 Bot stopped due to insufficient balance or error")
                break
            
            # Wait a bit before moving to next market
            print("\n⏳ Waiting 10s before checking next market...")
            time.sleep(30)
            
    except KeyboardInterrupt:
        print("\n\n🛑 Bot stopped by user (Ctrl+C)")
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()

