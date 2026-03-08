"""
Polymarket Trading Bot – Main entry point.

Supports two modes:
  1. Single-shot workflow (default):
       Find next BTC 5m market → place limit orders → optionally merge/redeem.
  2. Autonomous trading engine (--auto):
       Continuously scan markets and trade according to the configured strategy.

Environment variables:
  PRIVATE_KEY          – Required. Wallet private key for signing.
  HOST                 – CLOB API host (default: https://clob.polymarket.com)
  CHAIN_ID             – Polygon chain ID (default: 137)
  SIGNATURE_TYPE       – 0=EOA, 1=Magic, 2=proxy (default: 0)
  FUNDER               – Optional funder/proxy address.
  ORDER_PRICE          – Default limit price (default: 0.46)
  ORDER_SIZE           – Default order size in shares (default: 5.0)
  RELAYER_URL          – Optional relayer URL for merge/redeem.
  BUILDER_API_KEY      – Optional Builder API key.
  BUILDER_SECRET       – Optional Builder API secret.
  BUILDER_PASS_PHRASE  – Optional Builder API passphrase.
  STRATEGY             – Trading strategy: btc_5m, value_bet, spread_capture, momentum (default: btc_5m)
  PAPER_TRADING        – true/false (default: true)
  TELEGRAM_BOT_TOKEN   – If set along with --telegram, starts Telegram bot.
  MONGO_URI            – MongoDB URI for Telegram bot persistence.

Usage:
  python main.py                  # Single-shot BTC 5m workflow
  python main.py --auto           # Autonomous trading engine
  python main.py --telegram       # Telegram bot mode
  python main.py --status         # Print bot status and exit
  python main.py --search "trump" # Search markets by keyword
  python main.py --trending       # Show trending markets
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.service.market_finder import MarketFinder
from src.service.polymarket_bot import PolymarketBot
from src.service.trading_engine import Strategy, TradingConfig, TradingEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def build_bot() -> PolymarketBot:
    """Build a PolymarketBot from environment variables."""
    private_key = os.getenv("PRIVATE_KEY", "")
    if not private_key:
        logger.warning("PRIVATE_KEY not set – trading operations will fail.")

    host = os.getenv("HOST", "https://clob.polymarket.com")
    chain_id = int(os.getenv("CHAIN_ID", "137"))
    signature_type = int(os.getenv("SIGNATURE_TYPE", "0"))
    funder = os.getenv("FUNDER")
    relayer_url = os.getenv("RELAYER_URL")

    return PolymarketBot(
        private_key=private_key,
        host=host,
        chain_id=chain_id,
        signature_type=signature_type,
        funder=funder,
        relayer_url=relayer_url,
        builder_api_key=os.getenv("BUILDER_API_KEY"),
        builder_secret=os.getenv("BUILDER_SECRET"),
        builder_passphrase=os.getenv("BUILDER_PASS_PHRASE"),
    )


def print_market_summary(market: dict) -> None:
    """Pretty-print a market summary."""
    info = MarketFinder.extract_market_info(market)
    print(f"\n{'═' * 60}")
    print(f"  📊 {info['question']}")
    print(f"{'═' * 60}")
    print(f"  ID:            {info['id']}")
    print(f"  Slug:          {info.get('slug', 'N/A')}")
    print(f"  Condition ID:  {info['condition_id']}")
    print(f"  Outcomes:      {info['outcomes']}")
    print(f"  Prices:        {info['outcome_prices']}")
    print(f"  Best Bid/Ask:  ${info['best_bid']:.3f} / ${info['best_ask']:.3f}")
    print(f"  Spread:        ${info['spread']:.3f}")
    print(f"  Last Trade:    ${info['last_trade_price']:.3f}")
    print(f"  Volume (24h):  ${info['volume_24h']:,.2f}")
    print(f"  Liquidity:     ${info['liquidity']:,.2f}")
    print(
        f"  Active:        {info['active']}  |  Accepting Orders: {info['accepting_orders']}"
    )
    print(f"  End Date:      {info['end_date']}")
    print(
        f"  Min Size:      {info['order_min_size']}  |  Tick Size: {info['min_tick_size']}"
    )
    print(f"{'─' * 60}")

    token_ids = MarketFinder.extract_token_ids(market)
    if token_ids:
        yes_tid = token_ids.get("yes_token_id", "N/A")
        no_tid = token_ids.get("no_token_id", "N/A")
        print(
            f"  Yes/Up Token:  {yes_tid[:40]}…"
            if len(yes_tid) > 40
            else f"  Yes/Up Token:  {yes_tid}"
        )
        print(
            f"  No/Down Token: {no_tid[:40]}…"
            if len(no_tid) > 40
            else f"  No/Down Token: {no_tid}"
        )
    print()


# ──────────────────────────────────────────────────────────────────────
# Mode: single-shot workflow
# ──────────────────────────────────────────────────────────────────────


def workflow() -> None:
    """Single-shot BTC 5-minute trading workflow."""
    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        print("❌ PRIVATE_KEY not set – workflow stops.")
        return

    bot = build_bot()

    status = bot.get_status()
    print(f"\n🤖 Bot Status:")
    print(f"   CLOB client: {'✅' if status['poly_client_available'] else '❌'}")
    print(f"   Relayer:     {'✅' if status['relayer_available'] else '❌'}")

    balance = bot.get_balance()
    if balance is not None:
        print(f"   Balance:     ${balance:.2f} USDC")
    else:
        print(f"   Balance:     (could not fetch)")

    print("\n🔍 Searching for next active BTC 5-minute market…")

    result = bot.full_trade_workflow()

    if result["market"]:
        print(f"\n📊 Market: {result['market'].get('question', 'N/A')}")
        print(f"   Condition ID: {result['market'].get('condition_id', 'N/A')}")

    if result["orders"]:
        for order in result["orders"]:
            status_emoji = "✅" if order.get("response") else "❌"
            print(
                f"   {status_emoji} {order['outcome']}: {order.get('response', 'FAILED')}"
            )

    if result["errors"]:
        for err in result["errors"]:
            print(f"   ⚠️  {err}")

    if not result["success"]:
        print("\n❌ Workflow completed with errors.")
    else:
        print("\n✅ Orders placed successfully!")

    # Relayer merge/redeem hint
    if bot.relayer_client and bot.relayer_client.is_available():
        condition_id = result.get("market", {}).get("condition_id")
        if condition_id:
            print(
                f"\n💡 After market resolves, you can merge/redeem:\n"
                f"   bot.merge_tokens(condition_id='{condition_id}', amount=1_000_000)\n"
                f"   bot.redeem_positions(condition_id='{condition_id}')"
            )
    else:
        print("\n💡 Relayer not configured – merge/redeem unavailable.")
        print(
            "   Set RELAYER_URL, BUILDER_API_KEY, BUILDER_SECRET, BUILDER_PASS_PHRASE to enable."
        )


# ──────────────────────────────────────────────────────────────────────
# Mode: autonomous trading
# ──────────────────────────────────────────────────────────────────────


def run_autonomous() -> None:
    """Run the autonomous trading engine."""
    bot = build_bot()
    config = TradingConfig.from_env()

    mode = "PAPER" if config.paper_trading else "LIVE"
    print(f"\n{'═' * 60}")
    print(f"  🤖 Polymarket Autonomous Trading Engine [{mode}]")
    print(f"{'═' * 60}")
    print(f"  Strategy:       {config.strategy.value}")
    print(f"  Order Size:     ${config.order_size:.2f}")
    print(f"  Max Exposure:   ${config.max_total_exposure:.2f}")
    print(f"  Max Positions:  {config.max_positions}")
    print(f"  Max Daily Loss: ${config.max_daily_loss:.2f}")
    print(f"  Scan Interval:  {config.scan_interval_seconds}s")
    if config.paper_trading:
        print(f"  Paper Balance:  ${config.paper_balance:.2f}")
    else:
        balance = bot.get_balance()
        if balance is not None:
            print(f"  USDC Balance:   ${balance:.2f}")
    print(f"{'═' * 60}\n")

    if not config.paper_trading:
        print("⚠️  LIVE TRADING MODE – Real money will be used!")
        print("    Press Ctrl+C to stop at any time.\n")

    engine = TradingEngine(
        poly_client=bot.poly_client,
        relayer_client=bot.relayer_client,
        market_finder=bot.finder,
        config=config,
        on_trade_callback=lambda msg: print(f"  {msg}"),
        on_status_callback=lambda msg: print(f"  {msg}"),
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    shutdown_event = asyncio.Event()

    def handle_signal(*_):
        print("\n🛑 Shutdown signal received…")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    async def run():
        await engine.start()
        # Wait until shutdown signal
        await shutdown_event.wait()
        await engine.stop()
        print(f"\n📊 Final Summary:\n{engine.get_status_summary()}")

    try:
        loop.run_until_complete(run())
    finally:
        loop.close()


# ──────────────────────────────────────────────────────────────────────
# Mode: status
# ──────────────────────────────────────────────────────────────────────


def show_status() -> None:
    """Show bot status and exit."""
    bot = build_bot()
    status = bot.get_status()

    print(f"\n{'═' * 50}")
    print(f"  🤖 Polymarket Bot Status")
    print(f"{'═' * 50}")
    print(f"  CLOB Host:      {status['host']}")
    print(f"  Chain ID:       {status['chain_id']}")
    print(
        f"  CLOB Client:    {'✅ Ready' if status['poly_client_available'] else '❌ Unavailable'}"
    )
    print(
        f"  Relayer:        {'✅ Ready' if status['relayer_available'] else '❌ Unavailable'}"
    )
    print(
        f"  WebSocket:      {'✅ Connected' if status['websocket_connected'] else '⚪ Not connected'}"
    )

    balance = status.get("balance_usdc")
    if balance is not None:
        print(f"  USDC Balance:   ${balance:.2f}")
    else:
        print(f"  USDC Balance:   (unavailable)")

    if status.get("current_market_question"):
        print(f"  Current Market: {status['current_market_question']}")

    # Open orders
    orders = bot.get_open_orders()
    print(f"  Open Orders:    {len(orders)}")
    print(f"{'═' * 50}\n")


# ──────────────────────────────────────────────────────────────────────
# Mode: search / trending
# ──────────────────────────────────────────────────────────────────────


def search_markets(query: str) -> None:
    """Search markets by keyword."""
    finder = MarketFinder()
    print(f"\n🔍 Searching for '{query}'…\n")
    markets = finder.search_markets(query, limit=10)

    if not markets:
        print("  No markets found.")
        return

    for i, m in enumerate(markets, 1):
        info = MarketFinder.extract_market_info(m)
        print(
            f"  {i}. {info['question'][:60]}\n"
            f"     ID: {info['id']} | Vol24h: ${info['volume_24h']:,.0f} | "
            f"Liq: ${info['liquidity']:,.0f} | "
            f"Prices: {info['outcome_prices']}\n"
        )


def show_trending() -> None:
    """Show trending markets."""
    finder = MarketFinder()
    print(f"\n🔥 Trending Markets (by 24h volume):\n")
    markets = finder.get_trending_markets(limit=10, min_volume_24h=500)

    if not markets:
        print("  No trending markets found.")
        return

    for i, m in enumerate(markets, 1):
        info = MarketFinder.extract_market_info(m)
        print(
            f"  {i}. {info['question'][:55]}\n"
            f"     Vol24h: ${info['volume_24h']:,.0f} | Liq: ${info['liquidity']:,.0f} | "
            f"Spread: ${info['spread']:.3f} | "
            f"Prices: {info['outcome_prices']}\n"
        )


# ──────────────────────────────────────────────────────────────────────
# Mode: Telegram bot
# ──────────────────────────────────────────────────────────────────────


def run_telegram() -> None:
    """Start the Telegram bot."""
    from src.tg_service.tg_bot import main as tg_main

    tg_main()


# ──────────────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Polymarket Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                    # Single-shot BTC 5m workflow
  python main.py --auto             # Autonomous trading engine
  python main.py --telegram         # Telegram bot mode
  python main.py --status           # Show bot status
  python main.py --search "bitcoin" # Search markets
  python main.py --trending         # Show trending markets
  python main.py --market 531202    # Show details for a specific market
        """,
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Run autonomous trading engine (continuous loop)",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Start Telegram bot mode",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show bot status and exit",
    )
    parser.add_argument(
        "--search",
        type=str,
        metavar="QUERY",
        help="Search markets by keyword",
    )
    parser.add_argument(
        "--trending",
        action="store_true",
        help="Show trending markets",
    )
    parser.add_argument(
        "--market",
        type=str,
        metavar="ID",
        help="Show details for a specific market by ID",
    )

    args = parser.parse_args()

    if args.auto:
        run_autonomous()
    elif args.telegram:
        run_telegram()
    elif args.status:
        show_status()
    elif args.search:
        search_markets(args.search)
    elif args.trending:
        show_trending()
    elif args.market:
        finder = MarketFinder()
        m = finder.fetch_market_by_id(args.market)
        if m:
            print_market_summary(m)
        else:
            print(f"❌ Market {args.market} not found.")
    else:
        workflow()


if __name__ == "__main__":
    main()
