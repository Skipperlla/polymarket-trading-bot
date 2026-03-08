"""
Poly5M Telegram bot — Paper Trading, Real Trading, Wallet, Referrals, Settings, Help.
Run from project root with .env containing TELEGRAM_BOT_TOKEN and MONGO_URI.

Note: Core skilled implementations (balance, bridge, user keys, withdrawal, token checks)
are removed for public sharing. Implement your own or restore from private codebase.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("PolyArbBot5M")

# Project root and src on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

# Load .env so TELEGRAM_BOT_TOKEN, MONGO_URI, etc. are set (repo root first, then src)
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT.parent / ".env")
load_dotenv(PROJECT_ROOT / ".env")

import yaml
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, Conflict, RetryAfter
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# Core skilled code removed for public sharing — use stubs
def _stub_check_allowance(*a, **kw):
    return False, "⚠️ Balance/allowance check implementation removed for public sharing."


def _stub_fetch_balance(*a, **kw):
    return 0.0


async def _stub_fetch_deposit_addresses(*a, **kw):
    return {}


async def _stub_create_withdrawal_addresses(*a, **kw):
    return {"error": "Withdrawal/bridge implementation removed for public sharing."}


try:
    from telegram_bot.balance import (
        check_and_update_allowance_sync,
        fetch_proxy_balance_sync,
    )
except ImportError:
    check_and_update_allowance_sync = _stub_check_allowance
    fetch_proxy_balance_sync = _stub_fetch_balance

try:
    from telegram_bot.bridge import (
        WITHDRAW_CHAINS,
        create_withdrawal_addresses,
        fetch_deposit_addresses,
    )
except ImportError:
    fetch_deposit_addresses = _stub_fetch_deposit_addresses
    create_withdrawal_addresses = _stub_create_withdrawal_addresses
    WITHDRAW_CHAINS = {
        "polygon": {"label": "Polygon", "tokens": {}},
        "solana": {"label": "Solana", "tokens": {}},
    }

try:
    from telegram_bot.constants import (
        BOT_TOKEN,
        MAX_CONCURRENT_TRADING_SESSIONS,
        MIN_DEPOSIT_USD,
        MIN_WITHDRAWAL_USDC,
        POSITION_MANAGER_SCRIPT,
        PROJECT_ROOT,
        REPO_ROOT,
        TRADING_BINARY_DEFAULT,
        TRADING_BINARY_RAW,
        TRADING_SCRIPT,
        UPGRADE_CONTACT,
    )
except ImportError:
    BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    REPO_ROOT = PROJECT_ROOT
    MIN_DEPOSIT_USD = 3
    MIN_WITHDRAWAL_USDC = 5
    TRADING_BINARY_RAW = ""
    TRADING_BINARY_DEFAULT = PROJECT_ROOT / "scripts" / "test_5m_core"
    TRADING_SCRIPT = PROJECT_ROOT / "scripts" / "test_5m_core.py"
    POSITION_MANAGER_SCRIPT = PROJECT_ROOT / "scripts" / "position_manager.py"
    UPGRADE_CONTACT = "Contact admin"
    MAX_CONCURRENT_TRADING_SESSIONS = 10

try:
    from telegram_bot.trial import can_use_real_trading, start_trial_if_needed
except ImportError:

    async def can_use_real_trading(uid):
        return False, "Trial implementation removed for public sharing."

    async def start_trial_if_needed(uid):
        return None


try:
    from telegram_bot.keyboards import (
        MAIN_MENU,
        TRADING_STOP_KEYBOARD,
        help_inline,
        referrals_inline,
        settings_all_inline,
        wallet_inline,
        withdraw_chain_inline,
        withdraw_confirm_inline,
        withdraw_token_inline,
    )
except ImportError:
    MAIN_MENU = ReplyKeyboardMarkup(
        [["🔄 Arbitrage Bot"], ["👛 Wallet", "⚙️ Settings", "📖 Help"]],
        resize_keyboard=True,
    )
    TRADING_STOP_KEYBOARD = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛑 Stop", callback_data="trading:stop")]]
    )

    def wallet_inline(*a):
        return InlineKeyboardMarkup([])

    def withdraw_chain_inline():
        return InlineKeyboardMarkup([])

    def withdraw_token_inline(*a):
        return InlineKeyboardMarkup([])

    def withdraw_confirm_inline():
        return InlineKeyboardMarkup([])

    def referrals_inline():
        return InlineKeyboardMarkup([])

    def help_inline():
        return InlineKeyboardMarkup([])

    def settings_all_inline(*a):
        return InlineKeyboardMarkup([])


async def _stub_get_private_key_and_funder(uid, path):
    return ("", "", "")


async def _stub_ensure_private_key(uid):
    pass


async def _stub_update_user_meta(uid, **kw):
    pass


try:
    from telegram_bot.user_keys import get_private_key_and_funder
except ImportError:
    get_private_key_and_funder = _stub_get_private_key_and_funder


# Token check / approval — core implementation removed
def _stub_check_token_status(*a, **kw):
    return False, "⚠️ Token status check implementation removed for public sharing."


def _stub_ensure_approvals(*a, **kw):
    return False, "⚠️ Token approval implementation removed for public sharing."


# Subprocess/trading runner logic removed for public sharing

# Telegram message limit; leave room for prefix and <pre> wrapper
_TELEGRAM_MAX_TEXT = 4096
_LOG_TAIL_LEN = 3400
_STREAM_EDIT_INTERVAL = 1.5
# Messages >512 bytes are treated as "large" by Telegram API and trigger stricter flood control (429)
MAX_TELEGRAM_MESSAGE_BYTES = 512


def _truncate_for_telegram(text: str, max_bytes: int = 500) -> str:
    """Truncate text to fit within max_bytes (UTF-8). Avoids cutting mid-character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    n = max_bytes
    while n > 0:
        try:
            return encoded[:n].decode("utf-8")
        except UnicodeDecodeError:
            n -= 1
    return ""


def _sanitize_log_tail(tail: str) -> str:
    """Remove control characters that can cause Telegram 400 (keep newline and tab)."""
    return "".join(c for c in tail if c in "\n\t\r" or ord(c) >= 32)


def _collapse_ws_lines(tail: str) -> str:
    """Replace each run of [WS] lines with only the latest one, keeping it among the other logs."""
    lines = tail.split("\n")
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "[WS]" not in line:
            result.append(line)
            i += 1
            continue
        run = [line]
        i += 1
        while i < len(lines) and "[WS]" in lines[i]:
            run.append(lines[i])
            i += 1
        result.append(run[-1])
    return "\n".join(result)


def _truncate_log_message(prefix: str, tail: str) -> str:
    """Build prefix + <pre>escaped(tail)</pre>. Collapse [WS] runs first so old logs stay, only bid/ask updates; truncate from start if over limit."""
    tail = _collapse_ws_lines(tail)
    clean = _sanitize_log_tail(tail)
    escaped = html.escape(clean)
    max_tail = _TELEGRAM_MAX_TEXT - len(prefix) - 20
    if len(escaped) > max_tail:
        escaped = escaped[-max_tail:]
    return f"{prefix}\n<pre>{escaped}</pre>"


# _stream_subprocess_to_message and _kill_trading_process removed for public sharing


async def _kill_trading_process(proc: asyncio.subprocess.Process) -> None:
    """Stub — implementation removed for public sharing."""
    pass


# ---------- Per-user config (core implementation can be replaced) ----------
def get_user_config_path(user_id: int) -> Path:
    try:
        from config.config import get_user_config_path as _get

        return _get(user_id, PROJECT_ROOT)
    except ImportError:
        path = PROJECT_ROOT / "config" / "users" / f"{user_id}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


def ensure_user_config(user_id: int, username: str = "", name: str = "") -> Path:
    try:
        from config.config import ensure_user_config as _ensure

        path = _ensure(user_id, PROJECT_ROOT)
    except ImportError:
        path = get_user_config_path(user_id)
    meta = path.with_suffix(".meta")
    if username or name:
        meta.write_text(f"USERNAME={username}\nNAME={name}\n")
    elif not meta.exists():
        meta.write_text("USERNAME=\nNAME=\n")
    return path


def load_user_config(user_id: int) -> dict:
    try:
        from config.config import load_config

        path = ensure_user_config(user_id)
        settings = load_config(str(path))
        return settings.model_dump()
    except ImportError:
        path = ensure_user_config(user_id)
        if path.exists():
            with open(path) as f:
                return yaml.safe_load(f) or {}
        return {"order": {}, "risk": {}, "execution": {}}


def save_user_config(user_id: int, data: dict) -> None:
    path = get_user_config_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True)


# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id
    username = user.username or ""
    name = user.full_name or user.first_name or ""
    logger.info("User started bot: user_id=%s @%s", user_id, username)
    ensure_user_config(user_id, username=username, name=name)
    # Ensure user has a private key (core implementation removed for public sharing)
    await _stub_ensure_private_key(user_id)
    await _stub_update_user_meta(user_id, username=username, name=name)
    await update.effective_message.reply_text(
        "Welcome to **Poly5M**. Choose an action:",
        reply_markup=MAIN_MENU,
        parse_mode="Markdown",
    )


async def main_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    user_id = update.effective_user.id

    pending = context.user_data.get("pending_setting")
    if pending:
        del context.user_data["pending_setting"]
        section, key = pending.split(".", 1)
        cfg = load_user_config(user_id)
        old_val = cfg.get(section, {}).get(key)
        try:
            if section == "order" and key in ("price", "size"):
                new_val = float(text)
            elif section == "risk" and key == "stop_time":
                new_val = int(text) if text.isdigit() else float(text)
            elif isinstance(old_val, float):
                new_val = float(text)
            elif isinstance(old_val, int):
                new_val = int(text)
            else:
                new_val = text
        except (ValueError, TypeError):
            await update.effective_message.reply_text(
                f"⚠️ Invalid value. Expected a number. Setting *{key.replace('_', ' ').title()}* unchanged.",
                parse_mode="Markdown",
            )
            return
        cfg.setdefault(section, {})[key] = new_val
        save_user_config(user_id, cfg)
        label = key.replace("_", " ").title()
        text = _truncate_for_telegram(f"✅ *{label}* → `{new_val}`\n\n{_SETTINGS_TEXT}")
        await update.effective_message.reply_text(
            text,
            reply_markup=settings_all_inline(cfg),
            parse_mode="Markdown",
        )
        return

    if context.user_data.get("pending_withdraw_address"):
        del context.user_data["pending_withdraw_address"]
        chain = context.user_data.get("withdraw_chain", "")
        token = context.user_data.get("withdraw_token", "")
        chain_info = WITHDRAW_CHAINS.get(chain, {})
        chain_label = chain_info.get("label", chain)
        token_sym = (
            chain_info.get("tokens", {}).get(token, {}).get("symbol", token.upper())
        )
        recipient = text.strip()
        # Basic validation
        is_solana = chain == "solana"
        if not is_solana and (not recipient.startswith("0x") or len(recipient) != 42):
            await update.effective_message.reply_text(
                "⚠️ Invalid EVM address. Must start with `0x` and be 42 characters.\nPlease try again:",
                parse_mode="Markdown",
            )
            context.user_data["pending_withdraw_address"] = True
            return
        if is_solana and (len(recipient) < 32 or len(recipient) > 44):
            await update.effective_message.reply_text(
                "⚠️ Invalid Solana address. Please try again:",
            )
            context.user_data["pending_withdraw_address"] = True
            return
        context.user_data["withdraw_recipient"] = recipient
        balance = context.user_data.get("withdraw_balance", 0.0)
        await update.effective_message.reply_text(
            f"📤 *Confirm Withdrawal*\n\n"
            f"*Chain:* {chain_label}\n"
            f"*Token:* {token_sym}\n"
            f"*Recipient:* `{recipient}`\n"
            f"*Available:* ${balance:.2f} USDC\n\n"
            "Your full USDC.e balance will be bridged and swapped to "
            f"{token_sym} on {chain_label}.\n\n"
            "⚠️ Withdrawals are instant and free. Confirm to proceed.",
            reply_markup=withdraw_confirm_inline(),
            parse_mode="Markdown",
        )
        return

    if text == "🔄 Arbitrage Bot":
        logger.info("user_id=%s → Arbitrage bot", user_id)
        await position_manager_run(update, context)
    elif text == "👛 Wallet":
        logger.info("user_id=%s → Wallet", user_id)
        await wallet_screen(update, context)
    elif text == "⚙️ Settings":
        logger.info("user_id=%s → Settings", user_id)
        await settings_screen(update, context)
    elif text == "📖 Help":
        logger.info("user_id=%s → Help", user_id)
        await help_screen(update, context)
    else:
        await update.effective_message.reply_text(
            "Use the menu buttons below.", reply_markup=MAIN_MENU
        )


async def paper_trading(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Implementation removed for public sharing
    await update.effective_message.reply_text(
        "⚠️ Paper Trading implementation removed for public sharing.\n"
        "Implement your own trading runner and subprocess logic.",
        parse_mode="Markdown",
    )


async def real_trading(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Implementation removed for public sharing
    await update.effective_message.reply_text(
        "⚠️ Real Trading implementation removed for public sharing.\n"
        "Implement your own token checks, approval flow, and trading subprocess.",
        parse_mode="Markdown",
    )


async def position_manager_run(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Arbitrage bot — implementation removed for public sharing."""
    await update.effective_message.reply_text(
        "⚠️ Arbitrage bot (merge/force-sell loop) implementation removed for public sharing.\n"
        "Implement your own balance/allowance checks and position manager subprocess.",
        parse_mode="Markdown",
    )


def _shorten_address(addr: str, head: int = 8, tail: int = 6) -> str:
    """Abbreviate address to stay under Telegram 512-byte limit (e.g. 0x1234...abcd)."""
    if not addr or len(addr) <= head + tail + 3:
        return addr
    return f"{addr[:head]}...{addr[-tail:]}"


def _deposit_wallets_text(
    evm: str, svm: str, btc: str = "", load_error: bool = False
) -> str:
    """Build the Deposit Wallets section. Min $3 (Polygon, Solana); Min $10 (ETH, BNB, BTC). Full addresses shown."""
    if load_error:
        return "📥 *Deposit*\n\n⚠️ Could not load addresses. Try again later."
    lines = [
        "📥 *Deposit*",
        f"🟢 Polygon (Min $3)\n`{evm}`",
        f"🟣 Solana (Min $3)\n`{svm}`",
        f"🔵 ETH/BNB (Min $10)\n`{evm}`",
    ]
    if btc:
        lines.append(f"🟠 BTC (Min $10)\n`{btc}`")
    return "\n".join(lines)


async def wallet_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    logger.info("user_id=%s opened Wallet", user_id)
    config_path = get_user_config_path(user_id)
    pk, funder, eoa = await get_private_key_and_funder(user_id, config_path)
    # Balance, display and bridge use proxy (funder) — matches Polymarket UI (Gnosis Safe proxy address)
    config_path_str = str(config_path)
    balance_usd = await asyncio.to_thread(
        fetch_proxy_balance_sync, pk, funder, config_path_str, PROJECT_ROOT
    )
    balance = f"${balance_usd:.2f}"
    addrs = await fetch_deposit_addresses(funder)
    if addrs and (addrs.get("evm") or addrs.get("svm")):
        evm = addrs.get("evm") or ""
        svm = addrs.get("svm") or ""
        btc = addrs.get("btc") or ""
        deposit_text = _deposit_wallets_text(evm, svm, btc)
    else:
        deposit_text = _deposit_wallets_text("", "", "", load_error=True)
    text = (
        "👛 *WALLET*\n" + deposit_text + "\n\n"
        "✅ *Polymarket:* (Don't deposit here)\n"
        f"`{funder}`\n\n"
        f"💰 Balance: {balance} · Min: $3 (Polygon/Solana) or $10 (ETH, BNB, BTC)"
    )
    text = _truncate_for_telegram(text, max_bytes=4096)
    await update.effective_message.reply_text(
        text,
        reply_markup=wallet_inline(funder),
        parse_mode="Markdown",
    )


async def withdraw_destination(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "📤 *Select Withdrawal Destination*\nChoose where you want to receive your funds:",
        reply_markup=withdraw_chain_inline(),
        parse_mode="Markdown",
    )


async def withdraw_token_select(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await update.callback_query.answer()
    data = update.callback_query.data
    if not data.startswith("withdraw:"):
        return
    chain = data.split(":", 1)[1]
    chain_label = {
        "polygon": "Polygon",
        "solana": "Solana",
        "ethereum": "Ethereum",
        "bnb": "BNB",
    }.get(chain, chain)
    await update.callback_query.edit_message_text(
        f"Withdraw from *{chain_label}*\nSelect token:",
        reply_markup=withdraw_token_inline(chain),
        parse_mode="Markdown",
    )


async def withdraw_enter_address(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await update.callback_query.answer()
    data = update.callback_query.data
    if not data.startswith("withdraw_token:"):
        return
    parts = data.split(":")
    chain, token = parts[1], parts[2]
    context.user_data["withdraw_chain"] = chain
    context.user_data["withdraw_token"] = token
    context.user_data["pending_withdraw_address"] = True
    chain_info = WITHDRAW_CHAINS.get(chain, {})
    chain_label = chain_info.get("label", chain)
    token_sym = chain_info.get("tokens", {}).get(token, {}).get("symbol", token.upper())
    # Fetch current balance
    user_id = update.effective_user.id
    config_path = get_user_config_path(user_id)
    pk, funder, _ = await get_private_key_and_funder(user_id, config_path)
    balance_usd = await asyncio.to_thread(
        fetch_proxy_balance_sync, pk, funder, str(config_path), PROJECT_ROOT
    )
    context.user_data["withdraw_balance"] = balance_usd
    await update.callback_query.edit_message_text(
        f"💸 *Withdraw {token_sym} to {chain_label}*\n\n"
        f"Available Balance: `${balance_usd:.2f}` USDC\n\n"
        "Enter your destination wallet address:",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("❌ Cancel", callback_data="wallet")],
            ]
        ),
        parse_mode="Markdown",
    )


async def referrals_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    logger.info("user_id=%s opened Referrals", user.id)
    code = f"ref_{user.id}"  # TODO: from backend
    link = f"https://t.me/YourBotName?start=ref_{code}"
    text = _truncate_for_telegram(
        "🤝 *Referrals*\n"
        f"Code: `{code}`\n"
        f"Link: {link}\n"
        f"Min withdraw: ${MIN_WITHDRAWAL_USDC} USDC"
    )
    await update.effective_message.reply_text(
        text,
        reply_markup=referrals_inline(),
        parse_mode="Markdown",
    )


_SETTINGS_TEXT = (
    "⚙️ *Settings*\n"
    "Order Price, Order Size (used by Arbitrage bot). Risk: Stop Time (seconds before close).\n\n"
    "Select below:"
)


async def settings_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    logger.info("user_id=%s opened Settings", user_id)
    ensure_user_config(user_id)
    cfg = load_user_config(user_id)
    await update.effective_message.reply_text(
        _SETTINGS_TEXT,
        reply_markup=settings_all_inline(cfg),
        parse_mode="Markdown",
    )


async def help_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("user_id=%s opened Help", update.effective_user.id)
    try:
        from telegram_bot.constants import (
            COMMUNITY_OWNER_URL,
            DOCS_URL,
            TUTORIAL_VIDEO_URL,
        )
    except ImportError:
        TUTORIAL_VIDEO_URL = COMMUNITY_OWNER_URL = DOCS_URL = "https://t.me/sei_dev"
    text = _truncate_for_telegram(
        "📚 *Help*\n"
        f"[Tutorial]({TUTORIAL_VIDEO_URL}) · [Developer]({COMMUNITY_OWNER_URL}) · [Docs]({DOCS_URL})\n"
        "Deposit: send to your wallet address. Never share your private key."
    )
    await update.effective_message.reply_text(
        text,
        reply_markup=help_inline(),
        parse_mode="Markdown",
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = (q.data or "").strip()
    user_id = update.effective_user.id
    logger.info("user_id=%s callback: %s", user_id, data)
    if data == "trading:stop":
        trading = context.application.bot_data.get("trading") or {}
        entry = trading.pop(user_id, None)
        if entry:
            logger.info("user_id=%s stop requested, killing trading process", user_id)
            stop_event = entry.get("stop_event")
            if stop_event is not None:
                stop_event.set()
            proc = entry.get("proc")
            msg = entry.get("msg")
            if proc is not None and proc.returncode is None:
                await _kill_trading_process(proc)
            if msg is not None:
                try:
                    await msg.edit_text("🛑 Stopped by user.", reply_markup=None)
                except Exception:
                    pass
        else:
            logger.warning(
                "user_id=%s stop clicked but no running session found", user_id
            )
        await q.answer("Stopped.")
        return
    if data == "main":
        await q.answer()
        await q.edit_message_text("Main menu:", reply_markup=MAIN_MENU)
        return
    if data == "wallet":
        await q.answer()
        user_id = update.effective_user.id
        config_path = get_user_config_path(user_id)
        pk, funder, eoa = await get_private_key_and_funder(user_id, config_path)
        config_path_str = str(config_path)
        balance_usd = await asyncio.to_thread(
            fetch_proxy_balance_sync, pk, funder, config_path_str, PROJECT_ROOT
        )
        balance = f"${balance_usd:.2f}"
        addrs = await fetch_deposit_addresses(funder)
        if addrs and (addrs.get("evm") or addrs.get("svm")):
            evm = addrs.get("evm") or ""
            svm = addrs.get("svm") or ""
            btc = addrs.get("btc") or ""
            deposit_text = _deposit_wallets_text(evm, svm, btc)
        else:
            deposit_text = _deposit_wallets_text("", "", "", load_error=True)
        text = (
            "👛 *WALLET*\n" + deposit_text + "\n\n"
            "✅ *Polymarket:* (Don't deposit here)\n"
            f"`{funder}`\n\n"
            f"💰 Balance: {balance} · Min: $3 (Polygon/Solana) or $10 (ETH, BNB, BTC)"
        )
        text = _truncate_for_telegram(text, max_bytes=4096)
        await q.edit_message_text(
            text, reply_markup=wallet_inline(funder), parse_mode="Markdown"
        )
        return
        # Implementation cmd logic

        await q.answer()
        # Implementation removed for public sharing — never expose private keys
        await q.edit_message_text(
            "⚠️ Private key export implementation removed for public sharing.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "← Back to Settings", callback_data="settings"
                        )
                    ],
                ]
            ),
            parse_mode="Markdown",
        )
        return
    await q.answer()


async def cmd_arbitrage_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await position_manager_run(update, context)


async def cmd_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show upgrade / premium contact when trial has ended."""
    await update.effective_message.reply_text(
        "⏱️ *Trial ended — upgrade to premium*\n\n"
        "Real trading is limited to a 30-minute trial per user. "
        "To continue with real orders, get the premium version.\n\n"
        f"Contact: {UPGRADE_CONTACT}\n\n"
        "Paper trading remains free and unlimited.",
        parse_mode="Markdown",
    )


async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await wallet_screen(update, context)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await settings_screen(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await help_screen(update, context)


BOT_COMMANDS = [
    BotCommand("start", "Start bot"),
    BotCommand("arbitrage_bot", "Arbitrage bot (merge/force-sell)"),
    BotCommand("wallet", "Show your wallet balance"),
    BotCommand("settings", "Bot settings"),
    BotCommand("help", "View help and FAQs"),
    BotCommand("upgrade", "Premium / trial ended"),
]


async def post_init(application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors so they don't crash the bot; log RetryAfter and expired callback queries."""
    exc = context.error
    if isinstance(exc, RetryAfter):
        logger.warning(
            "Telegram rate limit (RetryAfter): wait %s seconds. Update: %s",
            exc.retry_after,
            update,
        )
        return
    if isinstance(exc, Conflict):
        logger.error(
            "Telegram Conflict (409): Another bot instance is using this token. "
            "Stop all other instances (other terminals, servers, or BotFather webhook), then restart."
        )
        sys.exit(1)
    msg = getattr(exc, "message", None) or str(exc) or ""
    if isinstance(exc, BadRequest) and "query" in msg.lower():
        # Callback query expired (e.g. "Query is too old and response timeout expired")
        logger.warning("Telegram callback query expired or invalid: %s", exc.message)
        return
    logger.exception("Unhandled error in Telegram bot: %s", exc, exc_info=exc)


def build_application() -> Application:
    builder = Application.builder().token(BOT_TOKEN)
    try:
        builder = builder.rate_limiter(
            AIORateLimiter(overall_max_rate=10, overall_time_period=1, max_retries=3)
        )
    except RuntimeError as e:
        if "rate-limiter" in str(e).lower() or "aiolimiter" in str(e).lower():
            logger.warning(
                "Rate limiter not available (install python-telegram-bot[rate-limiter]). Running without throttling."
            )
        else:
            raise

    app = builder.post_init(post_init).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("arbitrage_bot", cmd_arbitrage_bot))
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("upgrade", cmd_upgrade))

    # Callback query handler
    app.add_handler(CallbackQueryHandler(callback_handler))

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
    sys.path.insert(0, str(PROJECT_ROOT))
    app = build_application()
    logger.info("Poly5M bot starting (polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
