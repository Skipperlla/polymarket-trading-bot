"""
Poly5M Telegram bot — Full working implementation.

Features:
  - Market search and browsing (trending, search by keyword)
  - Paper trading and live trading via TradingEngine
  - Position management (view, close)
  - Wallet balance display
  - Settings management (strategy, order size, price, risk params)
  - Engine start/stop/status controls
  - Trade notifications

Run from project root:
  python -m src.tg_service.tg_bot

Requires env vars: TELEGRAM_BOT_TOKEN, PRIVATE_KEY
Optional: MONGO_URI, PAPER_TRADING, STRATEGY, ORDER_SIZE, etc.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("PolyTradingBot")

# Project root and src on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
if str(PROJECT_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT.parent))

# Load .env
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT.parent / ".env")
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

import yaml
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, Conflict, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from telegram.ext import AIORateLimiter

    RATE_LIMITER_AVAILABLE = True
except ImportError:
    AIORateLimiter = None  # type: ignore
    RATE_LIMITER_AVAILABLE = False

from src.service.market_finder import MarketFinder
from src.service.polymarket_bot import PolymarketBot
from src.service.trading_engine import Strategy, TradingConfig, TradingEngine

# ──────────────────────────────────────────────────────────────────────
# Config from env
# ──────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_USER_IDS_RAW = os.getenv("ADMIN_USER_IDS", "")
ADMIN_USER_IDS = set()
if ADMIN_USER_IDS_RAW:
    for uid in ADMIN_USER_IDS_RAW.split(","):
        uid = uid.strip()
        if uid.isdigit():
            ADMIN_USER_IDS.add(int(uid))

# ──────────────────────────────────────────────────────────────────────
# Keyboards
# ──────────────────────────────────────────────────────────────────────

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["🤖 Trading Engine", "📊 Markets"],
        ["💼 Positions", "👛 Wallet"],
        ["⚙️ Settings", "📖 Help"],
    ],
    resize_keyboard=True,
)


def engine_inline(is_running: bool = False) -> InlineKeyboardMarkup:
    if is_running:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🛑 Stop Engine", callback_data="engine:stop"),
                    InlineKeyboardButton("📊 Status", callback_data="engine:status"),
                ],
                [
                    InlineKeyboardButton(
                        "💼 Positions", callback_data="engine:positions"
                    ),
                    InlineKeyboardButton("📜 History", callback_data="engine:history"),
                ],
                [InlineKeyboardButton("← Main Menu", callback_data="main")],
            ]
        )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📝 Start Paper", callback_data="engine:start_paper"
                ),
                InlineKeyboardButton(
                    "🔴 Start Live", callback_data="engine:start_live"
                ),
            ],
            [
                InlineKeyboardButton("📊 Status", callback_data="engine:status"),
            ],
            [InlineKeyboardButton("← Main Menu", callback_data="main")],
        ]
    )


def markets_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔥 Trending", callback_data="markets:trending"),
                InlineKeyboardButton("🔍 Search", callback_data="markets:search"),
            ],
            [
                InlineKeyboardButton("💰 Value Bets", callback_data="markets:value"),
                InlineKeyboardButton(
                    "⏰ Expiring Soon", callback_data="markets:expiring"
                ),
            ],
            [
                InlineKeyboardButton("₿ BTC 5min", callback_data="markets:btc5m"),
            ],
            [InlineKeyboardButton("← Main Menu", callback_data="main")],
        ]
    )


def market_action_inline(market_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📗 Buy Yes", callback_data=f"buy:{market_id}:yes"
                ),
                InlineKeyboardButton("📕 Buy No", callback_data=f"buy:{market_id}:no"),
            ],
            [
                InlineKeyboardButton(
                    "📊 Order Book", callback_data=f"book:{market_id}"
                ),
                InlineKeyboardButton("🔙 Back", callback_data="markets:trending"),
            ],
        ]
    )


def settings_inline(config: TradingConfig) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Strategy: {config.strategy.value}",
                    callback_data="set:strategy",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"Order Size: ${config.order_size:.1f}",
                    callback_data="set:order_size",
                ),
                InlineKeyboardButton(
                    f"Order Price: ${config.order_price:.2f}",
                    callback_data="set:order_price",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"Max Exposure: ${config.max_total_exposure:.0f}",
                    callback_data="set:max_total_exposure",
                ),
                InlineKeyboardButton(
                    f"Max Positions: {config.max_positions}",
                    callback_data="set:max_positions",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"Max Daily Loss: ${config.max_daily_loss:.0f}",
                    callback_data="set:max_daily_loss",
                ),
                InlineKeyboardButton(
                    f"Stop Loss: {config.stop_loss_pct:.0%}",
                    callback_data="set:stop_loss_pct",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"Scan Interval: {config.scan_interval_seconds}s",
                    callback_data="set:scan_interval_seconds",
                ),
            ],
            [InlineKeyboardButton("← Main Menu", callback_data="main")],
        ]
    )


def strategy_inline() -> InlineKeyboardMarkup:
    buttons = []
    for s in Strategy:
        buttons.append(
            InlineKeyboardButton(s.value, callback_data=f"setstrategy:{s.value}")
        )
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("← Back", callback_data="settings")])
    return InlineKeyboardMarkup(rows)


# ──────────────────────────────────────────────────────────────────────
# Telegram message helpers
# ──────────────────────────────────────────────────────────────────────

_TELEGRAM_MAX_TEXT = 4096


def _truncate(text: str, max_len: int = 4000) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 20] + "\n…(truncated)"


def _escape_md(text: str) -> str:
    """Escape text for Telegram MarkdownV2 (but we use Markdown mode mostly)."""
    return text


def _format_market(m: dict, index: int = 0) -> str:
    """Format a market dict into a short summary string."""
    info = MarketFinder.extract_market_info(m)
    prefix = f"{index}. " if index > 0 else ""
    prices = info.get("outcome_prices", [])
    price_str = " / ".join(f"${p:.2f}" for p in prices) if prices else "N/A"
    outcomes = info.get("outcomes", [])
    outcome_str = " / ".join(outcomes) if outcomes else "Yes / No"

    return (
        f"{prefix}*{info['question'][:55]}*\n"
        f"   ID: `{info['id']}` | {outcome_str}: {price_str}\n"
        f"   Vol24h: ${info['volume_24h']:,.0f} | Liq: ${info['liquidity']:,.0f} | Spread: ${info['spread']:.3f}"
    )


def _format_market_detail(m: dict) -> str:
    """Format a market dict into a detailed view."""
    info = MarketFinder.extract_market_info(m)
    prices = info.get("outcome_prices", [])
    outcomes = info.get("outcomes", [])
    token_ids = MarketFinder.extract_token_ids(m)

    lines = [
        f"📊 *{info['question']}*\n",
        f"🆔 Market ID: `{info['id']}`",
        f"🔑 Condition: `{info['condition_id']}`",
        "",
    ]

    for i, (out, price) in enumerate(zip(outcomes, prices)):
        lines.append(f"   {out}: *${price:.3f}*")

    lines.extend(
        [
            "",
            f"📈 Best Bid: ${info['best_bid']:.3f} | Ask: ${info['best_ask']:.3f}",
            f"📉 Spread: ${info['spread']:.3f}",
            f"💵 Last Trade: ${info['last_trade_price']:.3f}",
            f"📊 Volume (24h): ${info['volume_24h']:,.2f}",
            f"💧 Liquidity: ${info['liquidity']:,.2f}",
            f"📅 End: {info.get('end_date', 'N/A')}",
            f"✅ Accepting Orders: {'Yes' if info['accepting_orders'] else 'No'}",
            f"📦 Min Size: {info['order_min_size']}",
        ]
    )

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Bot state (per-application)
# ──────────────────────────────────────────────────────────────────────


class BotState:
    """Holds shared state for the Telegram bot."""

    def __init__(self):
        self.bot: Optional[PolymarketBot] = None
        self.engine: Optional[TradingEngine] = None
        self.config: TradingConfig = TradingConfig.from_env()
        self.finder: MarketFinder = MarketFinder()
        self._trade_notifications: List[str] = []

    def init_bot(self) -> PolymarketBot:
        """Initialise the PolymarketBot if not already done."""
        if self.bot is not None:
            return self.bot

        private_key = os.getenv("PRIVATE_KEY", "")
        host = os.getenv("HOST", "https://clob.polymarket.com")
        chain_id = int(os.getenv("CHAIN_ID", "137"))
        signature_type = int(os.getenv("SIGNATURE_TYPE", "0"))
        funder = os.getenv("FUNDER")
        relayer_url = os.getenv("RELAYER_URL")

        self.bot = PolymarketBot(
            private_key=private_key,
            host=host,
            chain_id=chain_id,
            signature_type=signature_type,
            funder=funder,
            relayer_url=relayer_url,
            builder_api_key=os.getenv("BUILDER_API_KEY"),
            builder_secret=os.getenv("BUILDER_SECRET"),
            builder_passphrase=os.getenv("BUILDER_PASS_PHRASE"),
            market_finder=self.finder,
        )
        return self.bot

    def init_engine(self, paper: bool = True) -> TradingEngine:
        """Initialise (or re-initialise) the TradingEngine."""
        bot = self.init_bot()
        self.config.paper_trading = paper

        self.engine = TradingEngine(
            poly_client=bot.poly_client if bot else None,
            relayer_client=bot.relayer_client if bot else None,
            market_finder=self.finder,
            config=self.config,
            on_trade_callback=self._on_trade,
            on_status_callback=self._on_status,
        )
        return self.engine

    def _on_trade(self, message: str):
        self._trade_notifications.append(message)
        # Keep only last 50
        if len(self._trade_notifications) > 50:
            self._trade_notifications = self._trade_notifications[-50:]
        logger.info("[Trade] %s", message)

    def _on_status(self, message: str):
        logger.info("[Status] %s", message)

    def pop_notifications(self) -> List[str]:
        notes = list(self._trade_notifications)
        self._trade_notifications.clear()
        return notes


# Global state
state = BotState()


# ──────────────────────────────────────────────────────────────────────
# Command handlers
# ──────────────────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id
    username = user.username or ""
    name = user.full_name or user.first_name or ""
    logger.info("User started bot: user_id=%s @%s (%s)", user_id, username, name)

    state.init_bot()

    text = (
        f"👋 Welcome to *Polymarket Trading Bot*, {name}!\n\n"
        "I can help you:\n"
        "• 🤖 Run autonomous trading (paper or live)\n"
        "• 📊 Browse & search markets\n"
        "• 💼 Manage positions\n"
        "• 👛 Check wallet balance\n\n"
        "Use the menu below to get started."
    )
    await update.effective_message.reply_text(
        text, reply_markup=MAIN_MENU, parse_mode="Markdown"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📖 *Help — Polymarket Trading Bot*\n\n"
        "*Commands:*\n"
        "/start — Start the bot\n"
        "/engine — Trading engine controls\n"
        "/markets — Browse markets\n"
        "/search <query> — Search markets\n"
        "/trending — Trending markets\n"
        "/positions — View open positions\n"
        "/wallet — Wallet balance\n"
        "/settings — Bot settings\n"
        "/status — Engine status\n"
        "/help — This help message\n\n"
        "*Trading Modes:*\n"
        "• 📝 Paper Trading — Simulated with virtual balance\n"
        "• 🔴 Live Trading — Real USDC orders via CLOB\n\n"
        "*Strategies:*\n"
        "• value\\_bet — Find underpriced outcomes\n"
        "• spread\\_capture — Earn bid-ask spread\n"
        "• momentum — Follow price trends\n"
        "• btc\\_5m — BTC 5-minute up/down markets\n\n"
        "*Safety:*\n"
        "• Always test with paper trading first\n"
        "• Set conservative risk limits\n"
        "• Never share your PRIVATE\\_KEY"
    )
    await update.effective_message.reply_text(
        text, reply_markup=MAIN_MENU, parse_mode="Markdown"
    )


async def cmd_engine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_running = state.engine is not None and state.engine.is_running
    mode = ""
    if is_running:
        mode = " (📝 Paper)" if state.config.paper_trading else " (🔴 LIVE)"
    text = (
        f"🤖 *Trading Engine*{mode}\n\n"
        f"Status: {'🟢 Running' if is_running else '⚪ Stopped'}\n"
        f"Strategy: {_escape_md(state.config.strategy.value)}\n"
        f"Order Size: ${state.config.order_size:.1f}\n"
    )
    if is_running and state.engine:
        text += (
            f"Positions: {state.engine.state.position_count}/{state.config.max_positions}\n"
            f"P&L: ${state.engine.state.total_pnl:+.2f}\n"
            f"Trades Today: {state.engine.state.trades_today}\n"
        )
    await update.effective_message.reply_text(
        text, reply_markup=engine_inline(is_running), parse_mode="Markdown"
    )


async def cmd_markets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = "📊 *Markets*\n\nChoose a category to browse:"
    await update.effective_message.reply_text(
        text, reply_markup=markets_inline(), parse_mode="Markdown"
    )


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text(
            "🔍 Usage: `/search <keyword>`\n\nExample: `/search bitcoin`",
            parse_mode="Markdown",
        )
        return

    query = " ".join(context.args)
    await _do_search(update, query)


async def _do_search(update: Update, query: str) -> None:
    msg = await update.effective_message.reply_text(f"🔍 Searching for '{query}'…")

    markets = await asyncio.to_thread(
        state.finder.search_markets, query, 10, True, 0, 0
    )

    if not markets:
        await msg.edit_text(
            f"🔍 No markets found for '{query}'.\n\nTry a different keyword.",
            parse_mode="Markdown",
        )
        return

    lines = [f"🔍 *Search results for '{query}':*\n"]
    for i, m in enumerate(markets[:8], 1):
        lines.append(_format_market(m, i))
        lines.append("")

    text = _truncate("\n".join(lines))

    # Build keyboard with market IDs for detail view
    buttons = []
    for m in markets[:8]:
        mid = m.get("id", "")
        q = (m.get("question") or "")[:25]
        buttons.append(InlineKeyboardButton(f"📊 {q}", callback_data=f"mdetail:{mid}"))
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("← Markets", callback_data="markets_menu")])

    await msg.edit_text(
        text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown"
    )


async def cmd_trending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_trending(update)


async def _show_trending(update: Update, edit_message=None) -> None:
    target = edit_message or update.effective_message
    if edit_message is None:
        msg = await target.reply_text("🔥 Loading trending markets…")
    else:
        msg = edit_message
        await msg.edit_text("🔥 Loading trending markets…")

    markets = await asyncio.to_thread(state.finder.get_trending_markets, 8, 500, 200)

    if not markets:
        await msg.edit_text("🔥 No trending markets found right now.")
        return

    lines = ["🔥 *Trending Markets*\n"]
    for i, m in enumerate(markets[:8], 1):
        lines.append(_format_market(m, i))
        lines.append("")

    text = _truncate("\n".join(lines))

    buttons = []
    for m in markets[:8]:
        mid = m.get("id", "")
        q = (m.get("question") or "")[:25]
        buttons.append(InlineKeyboardButton(f"📊 {q}", callback_data=f"mdetail:{mid}"))
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("← Markets", callback_data="markets_menu")])

    await msg.edit_text(
        text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown"
    )


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not state.engine:
        await update.effective_message.reply_text(
            "💼 *Positions*\n\nNo trading engine running. Start one first!",
            reply_markup=engine_inline(False),
            parse_mode="Markdown",
        )
        return

    summary = state.engine.get_positions_summary()
    text = f"💼 *Open Positions*\n\n{summary}"
    text = _truncate(text)

    buttons = []
    for key in list(state.engine.state.positions.keys())[:10]:
        pos = state.engine.state.positions[key]
        label = f"Close: {pos.question[:20]}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"close_pos:{key}"))
    rows = [[b] for b in buttons]
    rows.append([InlineKeyboardButton("← Main Menu", callback_data="main")])

    await update.effective_message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown"
    )


async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot = state.init_bot()

    text_parts = ["👛 *Wallet*\n"]

    # USDC balance
    try:
        balance = await asyncio.to_thread(bot.get_balance)
        if balance is not None:
            text_parts.append(f"💰 USDC Balance: *${balance:.2f}*")
        else:
            text_parts.append("💰 USDC Balance: _(unavailable)_")
    except Exception as exc:
        text_parts.append(f"💰 USDC Balance: _(error: {exc})_")

    # CLOB / relayer status
    status = bot.get_status()
    text_parts.extend(
        [
            "",
            f"🔗 CLOB: {'✅ Connected' if status['poly_client_available'] else '❌ Unavailable'}",
            f"⛓️ Relayer: {'✅ Connected' if status['relayer_available'] else '❌ Unavailable'}",
            f"🌐 Host: `{status['host']}`",
            f"🔢 Chain: {status['chain_id']}",
        ]
    )

    # Paper balance
    if state.engine:
        text_parts.extend(
            [
                "",
                f"📝 Paper Balance: *${state.engine.state.paper_balance:.2f}*",
            ]
        )

    # Open orders count
    try:
        orders = await asyncio.to_thread(bot.get_open_orders)
        text_parts.append(f"\n📋 Open Orders: {len(orders)}")
    except Exception:
        text_parts.append(f"\n📋 Open Orders: _(unavailable)_")

    text = _truncate("\n".join(text_parts))
    await update.effective_message.reply_text(
        text, reply_markup=MAIN_MENU, parse_mode="Markdown"
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "⚙️ *Settings*\n\n"
        "Tap a setting to change it.\n"
        "Current values are shown on each button."
    )
    await update.effective_message.reply_text(
        text, reply_markup=settings_inline(state.config), parse_mode="Markdown"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not state.engine:
        await update.effective_message.reply_text(
            "📊 *Status*\n\nNo trading engine running.",
            reply_markup=engine_inline(False),
            parse_mode="Markdown",
        )
        return

    summary = state.engine.get_status_summary()
    text = f"```\n{summary}\n```"
    text = _truncate(text)
    await update.effective_message.reply_text(
        text, reply_markup=engine_inline(state.engine.is_running), parse_mode="Markdown"
    )


# ──────────────────────────────────────────────────────────────────────
# Menu text handler
# ──────────────────────────────────────────────────────────────────────


async def main_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    user_id = update.effective_user.id

    # Handle pending setting input
    pending = context.user_data.get("pending_setting")
    if pending:
        del context.user_data["pending_setting"]
        try:
            if pending in (
                "order_size",
                "order_price",
                "max_total_exposure",
                "max_daily_loss",
                "stop_loss_pct",
            ):
                new_val = float(text)
            elif pending in ("max_positions", "scan_interval_seconds"):
                new_val = int(text)
            else:
                new_val = text

            if hasattr(state.config, pending):
                setattr(state.config, pending, new_val)
                label = pending.replace("_", " ").title()
                await update.effective_message.reply_text(
                    f"✅ *{label}* updated to `{new_val}`",
                    reply_markup=settings_inline(state.config),
                    parse_mode="Markdown",
                )
                # Update engine config if running
                if state.engine:
                    state.engine.update_config(**{pending: new_val})
            else:
                await update.effective_message.reply_text(
                    f"⚠️ Unknown setting: {pending}",
                    parse_mode="Markdown",
                )
        except (ValueError, TypeError):
            await update.effective_message.reply_text(
                f"⚠️ Invalid value. Please enter a valid number.",
                parse_mode="Markdown",
            )
        return

    # Handle pending search
    pending_search = context.user_data.get("pending_search")
    if pending_search:
        del context.user_data["pending_search"]
        await _do_search(update, text)
        return

    # Menu button routing
    if text == "🤖 Trading Engine":
        await cmd_engine(update, context)
    elif text == "📊 Markets":
        await cmd_markets(update, context)
    elif text == "💼 Positions":
        await cmd_positions(update, context)
    elif text == "👛 Wallet":
        await cmd_wallet(update, context)
    elif text == "⚙️ Settings":
        await cmd_settings(update, context)
    elif text == "📖 Help":
        await cmd_help(update, context)
    else:
        await update.effective_message.reply_text(
            "Use the menu buttons below 👇", reply_markup=MAIN_MENU
        )


# ──────────────────────────────────────────────────────────────────────
# Callback query handler
# ──────────────────────────────────────────────────────────────────────


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = (q.data or "").strip()
    user_id = update.effective_user.id
    logger.info("user_id=%s callback: %s", user_id, data)

    try:
        await q.answer()
    except Exception:
        pass

    # ── Main menu ──
    if data == "main":
        await q.edit_message_text(
            "Choose an action from the menu below 👇",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🤖 Engine", callback_data="engine:status"
                        ),
                        InlineKeyboardButton(
                            "📊 Markets", callback_data="markets_menu"
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "💼 Positions", callback_data="engine:positions"
                        ),
                        InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
                    ],
                ]
            ),
            parse_mode="Markdown",
        )
        return

    # ── Engine controls ──
    if data == "engine:start_paper":
        await _start_engine(q, paper=True)
        return

    if data == "engine:start_live":
        private_key = os.getenv("PRIVATE_KEY", "")
        if not private_key:
            await q.edit_message_text(
                "❌ Cannot start live trading: PRIVATE\\_KEY not set.",
                reply_markup=engine_inline(False),
                parse_mode="Markdown",
            )
            return
        await _start_engine(q, paper=False)
        return

    if data == "engine:stop":
        await _stop_engine(q)
        return

    if data == "engine:status":
        if state.engine:
            summary = state.engine.get_status_summary()
            text = f"```\n{summary}\n```"
        else:
            text = "📊 No engine running."
        text = _truncate(text)
        is_running = state.engine is not None and state.engine.is_running
        await q.edit_message_text(
            text, reply_markup=engine_inline(is_running), parse_mode="Markdown"
        )
        return

    if data == "engine:positions":
        if state.engine:
            summary = state.engine.get_positions_summary()
            text = f"💼 *Open Positions*\n\n{summary}"
        else:
            text = "💼 No engine running."
        text = _truncate(text)
        is_running = state.engine is not None and state.engine.is_running
        await q.edit_message_text(
            text, reply_markup=engine_inline(is_running), parse_mode="Markdown"
        )
        return

    if data == "engine:history":
        if state.engine:
            summary = state.engine.get_trade_history_summary(10)
            text = f"📜 *Trade History*\n\n{summary}"
        else:
            text = "📜 No engine running."
        text = _truncate(text)
        is_running = state.engine is not None and state.engine.is_running
        await q.edit_message_text(
            text, reply_markup=engine_inline(is_running), parse_mode="Markdown"
        )
        return

    # ── Markets ──
    if data == "markets_menu":
        await q.edit_message_text(
            "📊 *Markets*\n\nChoose a category:",
            reply_markup=markets_inline(),
            parse_mode="Markdown",
        )
        return

    if data == "markets:trending":
        await _show_trending_callback(q)
        return

    if data == "markets:search":
        context.user_data["pending_search"] = True
        await q.edit_message_text(
            "🔍 *Search Markets*\n\nType your search keyword:",
            parse_mode="Markdown",
        )
        return

    if data == "markets:value":
        await _show_value_bets(q)
        return

    if data == "markets:expiring":
        await _show_expiring(q)
        return

    if data == "markets:btc5m":
        await _show_btc5m(q)
        return

    # ── Market detail ──
    if data.startswith("mdetail:"):
        market_id = data.split(":", 1)[1]
        await _show_market_detail(q, market_id)
        return

    # ── Buy order ──
    if data.startswith("buy:"):
        parts = data.split(":")
        if len(parts) >= 3:
            market_id = parts[1]
            outcome = parts[2]
            await _place_order(q, context, market_id, outcome)
        return

    # ── Order book ──
    if data.startswith("book:"):
        market_id = data.split(":", 1)[1]
        await _show_order_book(q, market_id)
        return

    # ── Close position ──
    if data.startswith("close_pos:"):
        pos_key = data.split(":", 1)[1]
        await _close_position(q, pos_key)
        return

    # ── Settings ──
    if data == "settings":
        await q.edit_message_text(
            "⚙️ *Settings*\n\nTap a setting to change it.",
            reply_markup=settings_inline(state.config),
            parse_mode="Markdown",
        )
        return

    if data == "set:strategy":
        await q.edit_message_text(
            "⚙️ *Select Strategy:*",
            reply_markup=strategy_inline(),
            parse_mode="Markdown",
        )
        return

    if data.startswith("setstrategy:"):
        strategy_val = data.split(":", 1)[1]
        try:
            state.config.strategy = Strategy(strategy_val)
            if state.engine:
                state.engine.update_config(strategy=state.config.strategy)
            await q.edit_message_text(
                f"✅ Strategy set to *{strategy_val}*",
                reply_markup=settings_inline(state.config),
                parse_mode="Markdown",
            )
        except ValueError:
            await q.edit_message_text(
                f"⚠️ Unknown strategy: {strategy_val}",
                reply_markup=settings_inline(state.config),
                parse_mode="Markdown",
            )
        return

    if data.startswith("set:"):
        setting_name = data.split(":", 1)[1]
        label = setting_name.replace("_", " ").title()
        current = getattr(state.config, setting_name, "?")
        context.user_data["pending_setting"] = setting_name
        await q.edit_message_text(
            f"⚙️ *{label}*\n\nCurrent value: `{current}`\n\nEnter new value:",
            parse_mode="Markdown",
        )
        return

    # Fallback
    logger.warning("Unhandled callback: %s", data)


# ──────────────────────────────────────────────────────────────────────
# Engine start/stop helpers
# ──────────────────────────────────────────────────────────────────────


def _escape_md(text: str) -> str:
    """Escape Markdown special characters for Telegram."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


async def _start_engine(q, paper: bool) -> None:
    # Stop existing engine if running
    if state.engine and state.engine.is_running:
        await state.engine.stop()

    engine = state.init_engine(paper=paper)
    mode = "📝 Paper" if paper else "🔴 LIVE"
    strategy_name = _escape_md(state.config.strategy.value)

    await q.edit_message_text(
        f"🚀 Starting {mode} trading engine…\n\n"
        f"Strategy: {strategy_name}\n"
        f"Order Size: ${state.config.order_size:.1f}\n"
        f"Scan Interval: {state.config.scan_interval_seconds}s",
        parse_mode="Markdown",
    )

    await engine.start()

    await asyncio.sleep(1)

    await q.edit_message_text(
        f"🟢 {mode} trading engine is *running*!\n\n"
        f"Strategy: {strategy_name}\n"
        f"Order Size: ${state.config.order_size:.1f}\n"
        f"Max Positions: {state.config.max_positions}\n"
        f"Max Exposure: ${state.config.max_total_exposure:.0f}\n\n"
        f"The engine will scan markets every {state.config.scan_interval_seconds}s "
        f"and trade automatically.",
        reply_markup=engine_inline(True),
        parse_mode="Markdown",
    )


async def _stop_engine(q) -> None:
    if not state.engine or not state.engine.is_running:
        await q.edit_message_text(
            "⚪ Engine is not running.",
            reply_markup=engine_inline(False),
            parse_mode="Markdown",
        )
        return

    await q.edit_message_text("🛑 Stopping engine…", parse_mode="Markdown")

    summary_before = state.engine.get_status_summary()
    await state.engine.stop()

    await q.edit_message_text(
        f"🛑 Engine stopped.\n\n```\n{_truncate(summary_before, 3500)}\n```",
        reply_markup=engine_inline(False),
        parse_mode="Markdown",
    )


# ──────────────────────────────────────────────────────────────────────
# Market display helpers
# ──────────────────────────────────────────────────────────────────────


async def _show_trending_callback(q) -> None:
    await q.edit_message_text("🔥 Loading trending markets…")

    markets = await asyncio.to_thread(state.finder.get_trending_markets, 8, 500, 200)

    if not markets:
        await q.edit_message_text(
            "🔥 No trending markets found.",
            reply_markup=markets_inline(),
            parse_mode="Markdown",
        )
        return

    lines = ["🔥 *Trending Markets*\n"]
    for i, m in enumerate(markets[:8], 1):
        lines.append(_format_market(m, i))
        lines.append("")

    text = _truncate("\n".join(lines))

    buttons = []
    for m in markets[:8]:
        mid = m.get("id", "")
        qtext = (m.get("question") or "")[:25]
        buttons.append(
            InlineKeyboardButton(f"📊 {qtext}", callback_data=f"mdetail:{mid}")
        )
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("← Markets", callback_data="markets_menu")])

    await q.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown"
    )


async def _show_value_bets(q) -> None:
    await q.edit_message_text("💰 Finding value bets…")

    markets = await asyncio.to_thread(
        state.finder.find_undervalued_markets, 0.20, 500, 8
    )

    if not markets:
        await q.edit_message_text(
            "💰 No undervalued markets found right now.",
            reply_markup=markets_inline(),
            parse_mode="Markdown",
        )
        return

    lines = ["💰 *Undervalued Markets* (outcome < $0.20)\n"]
    for i, m in enumerate(markets[:8], 1):
        lines.append(_format_market(m, i))
        lines.append("")

    text = _truncate("\n".join(lines))

    buttons = []
    for m in markets[:6]:
        mid = m.get("id", "")
        qtext = (m.get("question") or "")[:25]
        buttons.append(
            InlineKeyboardButton(f"📊 {qtext}", callback_data=f"mdetail:{mid}")
        )
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("← Markets", callback_data="markets_menu")])

    await q.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown"
    )


async def _show_expiring(q) -> None:
    await q.edit_message_text("⏰ Finding markets expiring soon…")

    markets = await asyncio.to_thread(state.finder.find_close_to_expiry, 48, 500, 8)

    if not markets:
        await q.edit_message_text(
            "⏰ No markets expiring within 48 hours.",
            reply_markup=markets_inline(),
            parse_mode="Markdown",
        )
        return

    lines = ["⏰ *Markets Expiring Soon*\n"]
    for i, m in enumerate(markets[:8], 1):
        hours = m.get("_hours_until_expiry", "?")
        lines.append(_format_market(m, i))
        lines.append(f"   ⏰ Expires in {hours}h")
        lines.append("")

    text = _truncate("\n".join(lines))

    buttons = []
    for m in markets[:6]:
        mid = m.get("id", "")
        qtext = (m.get("question") or "")[:25]
        buttons.append(
            InlineKeyboardButton(f"📊 {qtext}", callback_data=f"mdetail:{mid}")
        )
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("← Markets", callback_data="markets_menu")])

    await q.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown"
    )


async def _show_btc5m(q) -> None:
    await q.edit_message_text("₿ Looking for BTC 5-minute market…")

    market = await asyncio.to_thread(state.finder.find_next_btc_5m_market)

    if not market:
        await q.edit_message_text(
            "₿ No active BTC 5-minute market found right now.\n\n"
            "These markets may only be available during certain hours.",
            reply_markup=markets_inline(),
            parse_mode="Markdown",
        )
        return

    text = _format_market_detail(market)
    mid = market.get("id", "")

    await q.edit_message_text(
        text,
        reply_markup=market_action_inline(mid),
        parse_mode="Markdown",
    )


async def _show_market_detail(q, market_id: str) -> None:
    await q.edit_message_text("📊 Loading market details…")

    market = await asyncio.to_thread(state.finder.fetch_market_by_id, market_id)

    if not market:
        await q.edit_message_text(
            f"❌ Market {market_id} not found.",
            reply_markup=markets_inline(),
            parse_mode="Markdown",
        )
        return

    text = _format_market_detail(market)
    text = _truncate(text)

    await q.edit_message_text(
        text,
        reply_markup=market_action_inline(market_id),
        parse_mode="Markdown",
    )


async def _show_order_book(q, market_id: str) -> None:
    bot = state.init_bot()

    market = await asyncio.to_thread(state.finder.fetch_market_by_id, market_id)
    if not market:
        await q.edit_message_text(f"❌ Market {market_id} not found.")
        return

    token_ids = MarketFinder.extract_token_ids(market)
    if not token_ids:
        await q.edit_message_text(f"❌ Could not extract token IDs.")
        return

    yes_tid = token_ids.get("yes_token_id", "")

    if bot.poly_client and bot.poly_client.is_available() and yes_tid:
        try:
            book = await asyncio.to_thread(bot.poly_client.get_order_book, yes_tid)
            if book:
                question = (market.get("question") or "?")[:40]
                lines = [f"📊 *Order Book: {question}*\n"]

                if hasattr(book, "asks") and book.asks:
                    lines.append("*Asks (Sell):*")
                    for ask in list(book.asks)[:5]:
                        price = getattr(ask, "price", "?")
                        size = getattr(ask, "size", "?")
                        lines.append(f"  ${price} × {size}")

                if hasattr(book, "bids") and book.bids:
                    lines.append("\n*Bids (Buy):*")
                    for bid in list(book.bids)[:5]:
                        price = getattr(bid, "price", "?")
                        size = getattr(bid, "size", "?")
                        lines.append(f"  ${price} × {size}")

                text = "\n".join(lines)
                await q.edit_message_text(
                    text,
                    reply_markup=market_action_inline(market_id),
                    parse_mode="Markdown",
                )
                return
        except Exception as exc:
            logger.warning("Order book fetch failed: %s", exc)

    # Fallback: show prices from Gamma API
    info = MarketFinder.extract_market_info(market)
    text = (
        f"📊 *Order Book (summary)*\n\n"
        f"Best Bid: ${info['best_bid']:.3f}\n"
        f"Best Ask: ${info['best_ask']:.3f}\n"
        f"Spread: ${info['spread']:.3f}\n"
        f"Last Trade: ${info['last_trade_price']:.3f}\n\n"
        f"_(Full order book requires CLOB client connection)_"
    )
    await q.edit_message_text(
        text,
        reply_markup=market_action_inline(market_id),
        parse_mode="Markdown",
    )


# ──────────────────────────────────────────────────────────────────────
# Order placement via engine
# ──────────────────────────────────────────────────────────────────────


async def _place_order(q, context, market_id: str, outcome: str) -> None:
    if not state.engine:
        # Create engine in paper mode if none exists
        state.init_engine(paper=True)

    if not state.engine:
        await q.edit_message_text(
            "❌ Could not initialise trading engine.",
            parse_mode="Markdown",
        )
        return

    await q.edit_message_text(
        f"🔄 Placing {'📝 paper' if state.config.paper_trading else '🔴 LIVE'} "
        f"order: BUY {outcome.upper()} in market {market_id}…",
        parse_mode="Markdown",
    )

    try:
        result = await state.engine.manual_buy(
            market_id=market_id,
            outcome=outcome,
            price=state.config.order_price,
            size=state.config.order_size,
        )
    except Exception as exc:
        result = f"❌ Error: {exc}"

    is_running = state.engine.is_running
    await q.edit_message_text(
        result,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💼 Positions", callback_data="engine:positions"
                    ),
                    InlineKeyboardButton("📊 Markets", callback_data="markets_menu"),
                ],
                [InlineKeyboardButton("← Main Menu", callback_data="main")],
            ]
        ),
        parse_mode="Markdown",
    )


async def _close_position(q, pos_key: str) -> None:
    if not state.engine:
        await q.edit_message_text("❌ No engine running.")
        return

    try:
        result = await state.engine.manual_sell_position(pos_key)
    except Exception as exc:
        result = f"❌ Error: {exc}"

    await q.edit_message_text(
        result,
        reply_markup=engine_inline(state.engine.is_running),
        parse_mode="Markdown",
    )


# ──────────────────────────────────────────────────────────────────────
# Error handler
# ──────────────────────────────────────────────────────────────────────


async def post_init(application) -> None:
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("engine", "Trading engine controls"),
        BotCommand("markets", "Browse markets"),
        BotCommand("search", "Search markets by keyword"),
        BotCommand("trending", "Trending markets"),
        BotCommand("positions", "View open positions"),
        BotCommand("wallet", "Wallet balance"),
        BotCommand("settings", "Bot settings"),
        BotCommand("status", "Engine status"),
        BotCommand("help", "Help & FAQ"),
    ]
    await application.bot.set_my_commands(commands)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    exc = context.error
    if isinstance(exc, RetryAfter):
        logger.warning("Rate limit (RetryAfter %ss)", exc.retry_after)
        return
    if isinstance(exc, Conflict):
        logger.error(
            "Conflict (409): Another bot instance using this token. "
            "Stop other instances and restart."
        )
        sys.exit(1)
    msg = getattr(exc, "message", None) or str(exc) or ""
    if isinstance(exc, BadRequest) and "query" in msg.lower():
        logger.warning("Callback query expired: %s", msg)
        return
    logger.exception("Unhandled error: %s", exc, exc_info=exc)


# ──────────────────────────────────────────────────────────────────────
# Build & run
# ──────────────────────────────────────────────────────────────────────


def build_application() -> Application:
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set!")
        sys.exit(1)

    builder = Application.builder().token(BOT_TOKEN)

    if RATE_LIMITER_AVAILABLE and AIORateLimiter is not None:
        try:
            builder = builder.rate_limiter(
                AIORateLimiter(
                    overall_max_rate=10, overall_time_period=1, max_retries=3
                )
            )
        except RuntimeError as e:
            if "rate-limiter" in str(e).lower() or "aiolimiter" in str(e).lower():
                logger.warning(
                    "Rate limiter not available. Running without throttling."
                )
            else:
                raise

    app = builder.post_init(post_init).build()

    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("engine", cmd_engine))
    app.add_handler(CommandHandler("markets", cmd_markets))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("trending", cmd_trending))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("status", cmd_status))

    # Callback query handler
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Text message handler (menu buttons & setting inputs)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_text))

    # Error handler
    app.add_error_handler(error_handler)

    return app


def main() -> None:
    if not BOT_TOKEN:
        print("Set TELEGRAM_BOT_TOKEN to run the bot.")
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger.setLevel(logging.INFO)

    app = build_application()
    logger.info("Polymarket Trading Bot starting (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
