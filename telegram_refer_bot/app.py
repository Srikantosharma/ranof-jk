import asyncio
import html
import logging
import os
import secrets
from contextlib import suppress
from datetime import datetime, timezone
from typing import Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ChatMemberUpdated, CallbackQuery, InlineKeyboardButton, Message, Update
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

load_dotenv()

# =============================================================================
# Configuration
# =============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "7652741479"))
PORT = int(os.getenv("PORT", "8080"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")

# This channel receives every withdrawal request automatically.
REQUEST_CHANNEL_ID = int(os.getenv("REQUEST_CHANNEL_ID", "-1002985555395"))
PAYMENT_CHANNEL_URL = os.getenv("PAYMENT_CHANNEL_URL", "https://t.me/TTiny_Ranch_Bot").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is required")
if not PAYMENT_CHANNEL_URL:
    raise RuntimeError("PAYMENT_CHANNEL_URL is required")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./bot.db").strip()
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("telegram-refer-bot")

REFERRAL_REWARD = 4
WITHDRAWAL_AMOUNT = 15
LEADERBOARD_LIMIT = 10
BROADCAST_DELAY = 0.04

CHANNELS = [
    {"id": -1002164113779, "url": "https://t.me/+yHZ6leGHUqFmOWE9", "title": "Channel 1"},
    {"id": -1002130367937, "url": "https://t.me/+v95y6KvYOfhiNjk9", "title": "Channel 2"},
    {"id": -1002051656020, "url": "https://t.me/+GZFZJBuLsv9iMzA1", "title": "Channel 3"},
    {"id": -1003232143907, "url": "https://t.me/+MEilvYI8GDhmM2Nl", "title": "Channel 4"},
    {"id": -1002271732066, "url": "https://t.me/incomecryptovip2", "title": "Income Crypto VIP"},
]

REQUIRED_CHANNEL_IDS = {channel["id"] for channel in CHANNELS}

# =============================================================================
# Database models
# =============================================================================
class Base(AsyncAttrs, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    stars: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    referrals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    referrer_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True, index=True)
    referral_rewarded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class Withdrawal(Base):
    __tablename__ = "withdrawals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False, default=WITHDRAWAL_AMOUNT)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    # Used here for the display transaction ID. This keeps the schema backward-compatible.
    admin_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


Index(
    "uq_pending_withdrawal_user",
    Withdrawal.user_id,
    unique=True,
    sqlite_where=(Withdrawal.status == "pending"),
    postgresql_where=(Withdrawal.status == "pending"),
)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)


engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        existing = await session.get(Setting, "maintenance")
        if existing is None:
            session.add(Setting(key="maintenance", value="0"))
            await session.commit()


# =============================================================================
# UI helpers
# =============================================================================
def main_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="👤 Profile", callback_data="profile"),
        InlineKeyboardButton(text="⭐ Balance", callback_data="balance"),
    )
    builder.row(
        InlineKeyboardButton(text="🎁 Refer", callback_data="refer"),
        InlineKeyboardButton(text="🏆 Leaderboard", callback_data="leaderboard"),
    )
    builder.row(InlineKeyboardButton(text="💸 Withdraw", callback_data="withdraw"))
    return builder.as_markup()


def join_keyboard():
    builder = InlineKeyboardBuilder()
    for channel in CHANNELS:
        builder.row(InlineKeyboardButton(text=f"📢 Join {channel['title']}", url=channel["url"]))
    builder.row(InlineKeyboardButton(text="✅ I Joined — Check", callback_data="check_join"))
    return builder.as_markup()


def admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📥 Requests", callback_data="admin_requests"),
        InlineKeyboardButton(text="🔎 Search User", callback_data="admin_search_help"),
    )
    builder.row(
        InlineKeyboardButton(text="🚫 Ban User", callback_data="admin_ban_help"),
        InlineKeyboardButton(text="✅ Unban User", callback_data="admin_unban_help"),
    )
    builder.row(
        InlineKeyboardButton(text="📣 Broadcast", callback_data="admin_broadcast_help"),
        InlineKeyboardButton(text="📊 Stats", callback_data="admin_stats"),
    )
    builder.row(
        InlineKeyboardButton(text="⏸ Stop Bot", callback_data="admin_stop"),
        InlineKeyboardButton(text="▶️ Resume Bot", callback_data="admin_resume"),
    )
    return builder.as_markup()


def withdrawal_keyboard(withdrawal_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Approve", callback_data=f"wd_approve:{withdrawal_id}"),
        InlineKeyboardButton(text="⛔ Reject", callback_data=f"wd_reject:{withdrawal_id}"),
    )
    return builder.as_markup()


def payment_channel_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 Payment Channel", url=PAYMENT_CHANNEL_URL))
    return builder.as_markup()


def safe_username(user: Optional[User]) -> str:
    if user and user.username:
        return f"@{user.username}"
    return "(no username)"


def mention_html(user: Optional[User]) -> str:
    if not user:
        return "Unknown user"
    name = safe_username(user)
    if user.username:
        return html.escape(name)
    display = html.escape(user.first_name or str(user.id))
    return f'<a href="tg://user?id={user.id}">{display}</a>'


def generate_transaction_id(user_id: int, withdrawal_id: int) -> str:
    # Numeric display ID similar to the request-channel format in the screenshot.
    return f"{user_id}{withdrawal_id:06d}{secrets.randbelow(10**8):08d}"


def build_withdrawal_request_text(wd: Withdrawal, user: User, status: str = "PENDING") -> str:
    username = safe_username(user)
    # Telegram-safe HTML. No user input is inserted without escaping.
    return (
        "🔔 <b>New Stars Request Pending Alert!</b>\n\n"
        f"👤 <b>User:</b> {html.escape(username)}\n"
        f"🆔 <b>Telegram ID:</b> <code>{user.id}</code>\n\n"
        f"💳 <b>Stars:</b> {wd.amount}.0 ⭐ <b>(Fee: 0.0%; After Fee {wd.amount}.0 ⭐)</b>\n\n"
        f"📤 <b>Send To (Address):</b> {mention_html(user)}\n\n"
        f"🧾 <b>Transaction ID:</b> <code>{html.escape(wd.admin_note or str(wd.id))}</code>\n\n"
        f"📌 <b>Request ID:</b> <code>#{wd.id}</code>\n"
        f"📊 <b>Status:</b> <b>{html.escape(status)}</b>"
    )


# =============================================================================
# Settings / user helpers
# =============================================================================
async def maintenance_enabled() -> bool:
    async with SessionLocal() as session:
        setting = await session.get(Setting, "maintenance")
        return bool(setting and setting.value == "1")


async def set_maintenance(enabled: bool) -> None:
    async with SessionLocal() as session:
        setting = await session.get(Setting, "maintenance")
        if setting is None:
            setting = Setting(key="maintenance", value="1" if enabled else "0")
            session.add(setting)
        else:
            setting.value = "1" if enabled else "0"
        await session.commit()


async def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def get_or_create_user(tg_user) -> User:
    async with SessionLocal() as session:
        user = await session.get(User, tg_user.id)
        if user is None:
            user = User(
                id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                last_name=tg_user.last_name,
            )
            session.add(user)
        else:
            user.username = tg_user.username
            user.first_name = tg_user.first_name
            user.last_name = tg_user.last_name
        await session.commit()
        return user


async def update_user_profile(tg_user) -> None:
    async with SessionLocal() as session:
        user = await session.get(User, tg_user.id)
        if user:
            user.username = tg_user.username
            user.first_name = tg_user.first_name
            user.last_name = tg_user.last_name
            await session.commit()


async def get_user(user_id: int) -> Optional[User]:
    async with SessionLocal() as session:
        return await session.get(User, user_id)


async def get_active_withdrawal(user_id: int) -> Optional[Withdrawal]:
    async with SessionLocal() as session:
        stmt = (
            select(Withdrawal)
            .where(Withdrawal.user_id == user_id, Withdrawal.status == "pending")
            .order_by(Withdrawal.id.desc())
        )
        return (await session.execute(stmt)).scalars().first()


# =============================================================================
# Membership / referrals
# =============================================================================
async def user_is_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }:
            return True
        if member.status == ChatMemberStatus.RESTRICTED:
            return bool(getattr(member, "is_member", False))
        return False
    except Exception as exc:
        logger.warning("Membership check failed for user=%s chat=%s: %s", user_id, chat_id, exc)
        return False


async def check_all_channels(bot: Bot, user_id: int) -> bool:
    results = await asyncio.gather(
        *(user_is_member(bot, channel["id"], user_id) for channel in CHANNELS)
    )
    return all(results)


async def set_referrer_from_start(user_id: int, ref_code: Optional[str]) -> None:
    if not ref_code or not ref_code.startswith("ref_"):
        return

    try:
        referrer_id = int(ref_code[4:])
    except ValueError:
        return

    if referrer_id == user_id:
        return

    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        if not user or user.referrer_id is not None or user.referral_rewarded:
            return

        referrer = await session.get(User, referrer_id)
        if not referrer or referrer.banned:
            return

        # Only attach a referrer once. The guard prevents changing the referrer later.
        result = await session.execute(
            update(User)
            .where(User.id == user_id, User.referrer_id.is_(None))
            .values(referrer_id=referrer_id)
        )
        if result.rowcount:
            await session.commit()


async def award_referral_if_eligible(user_id: int) -> None:
    """Award exactly one 4-Star referral reward after all required channels are joined."""
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        if not user or user.referrer_id is None or user.referral_rewarded:
            return

        referrer_id = user.referrer_id
        referrer = await session.get(User, referrer_id)
        if not referrer or referrer.banned:
            return

        # Atomic claim prevents duplicate rewards when several Telegram updates arrive together.
        result = await session.execute(
            update(User)
            .where(User.id == user_id, User.referral_rewarded.is_(False))
            .values(referral_rewarded=True)
        )
        if result.rowcount != 1:
            await session.rollback()
            return

        await session.execute(
            update(User)
            .where(User.id == referrer_id)
            .values(
                stars=User.stars + REFERRAL_REWARD,
                referrals=User.referrals + 1,
            )
        )
        await session.commit()


async def gate_and_menu(
    bot: Bot,
    user_id: int,
    chat_id: int,
    send_new_message: bool = False,
) -> bool:
    if await is_admin(user_id):
        if send_new_message:
            await bot.send_message(
                chat_id,
                "✅ Welcome, admin.\n\nChoose an option below:",
                reply_markup=main_keyboard(),
            )
        return True

    user = await get_user(user_id)
    if user and user.banned:
        await bot.send_message(chat_id, "🚫 You are banned from using this bot.")
        return False

    if await maintenance_enabled():
        await bot.send_message(chat_id, "⏸ The bot is temporarily paused by the administrator.")
        return False

    if not await check_all_channels(bot, user_id):
        await bot.send_message(
            chat_id,
            "🔒 <b>Join all 5 required channels to unlock the bot.</b>\n\n"
            "After joining every channel, tap <b>✅ I Joined — Check</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=join_keyboard(),
        )
        return False

    await award_referral_if_eligible(user_id)
    if send_new_message:
        await bot.send_message(
            chat_id,
            "✅ <b>All requirements completed.</b>\n\nChoose an option below:",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )
    return True


# =============================================================================
# Bot / router
# =============================================================================
bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


class AdminStates(StatesGroup):
    search = State()
    ban = State()
    unban = State()
    broadcast = State()


# =============================================================================
# User commands
# =============================================================================
@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject):
    await get_or_create_user(message.from_user)
    await update_user_profile(message.from_user)
    await set_referrer_from_start(message.from_user.id, command.args)

    if not await is_admin(message.from_user.id) and await maintenance_enabled():
        await message.answer("⏸ The bot is temporarily paused by the administrator.")
        return

    await gate_and_menu(
        bot,
        message.from_user.id,
        message.chat.id,
        send_new_message=True,
    )


@router.callback_query(F.data == "check_join")
async def cb_check_join(callback: CallbackQuery):
    user_id = callback.from_user.id
    await update_user_profile(callback.from_user)

    if not await is_admin(user_id) and await maintenance_enabled():
        await callback.answer("Bot is temporarily paused.", show_alert=True)
        return

    ok = await check_all_channels(bot, user_id)
    if not ok:
        await callback.answer("You still need to join all 5 channels.", show_alert=True)
        return

    await award_referral_if_eligible(user_id)
    await callback.message.edit_text(
        "✅ <b>Verified.</b> Welcome to the bot!",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )
    await callback.answer("Verified")


@router.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery):
    if not await gate_and_menu(bot, callback.from_user.id, callback.message.chat.id):
        await callback.answer()
        return

    user = await get_user(callback.from_user.id)
    username = safe_username(user)
    text = (
        "👤 <b>Profile</b>\n\n"
        f"ID: <code>{callback.from_user.id}</code>\n"
        f"Username: {html.escape(username)}\n"
        f"⭐ Stars: <b>{user.stars if user else 0}</b>\n"
        f"👥 Successful referrals: <b>{user.referrals if user else 0}</b>"
    )
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "balance")
async def cb_balance(callback: CallbackQuery):
    if not await gate_and_menu(bot, callback.from_user.id, callback.message.chat.id):
        await callback.answer()
        return

    user = await get_user(callback.from_user.id)
    stars = user.stars if user else 0
    await callback.message.edit_text(
        "⭐ <b>Balance</b>\n\n"
        f"Your referral-program balance: <b>{stars} Stars</b>.\n\n"
        "This is the Star balance tracked by this bot's referral system.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "refer")
async def cb_refer(callback: CallbackQuery):
    if not await gate_and_menu(bot, callback.from_user.id, callback.message.chat.id):
        await callback.answer()
        return

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
    user = await get_user(callback.from_user.id)
    refs = user.referrals if user else 0

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Open Referral Link", url=link))
    builder.row(InlineKeyboardButton(text="⬅️ Back", callback_data="home"))

    text = (
        "🎁 <b>Refer &amp; Earn</b>\n\n"
        f"Reward: <b>{REFERRAL_REWARD} Stars</b> per successful referral.\n"
        f"Your successful referrals: <b>{refs}</b>\n\n"
        "Your unique referral link:\n"
        f"<code>{html.escape(link)}</code>"
    )
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "leaderboard")
async def cb_leaderboard(callback: CallbackQuery):
    if not await gate_and_menu(bot, callback.from_user.id, callback.message.chat.id):
        await callback.answer()
        return

    async with SessionLocal() as session:
        stmt = (
            select(User)
            .where(User.banned.is_(False))
            .order_by(User.referrals.desc(), User.stars.desc(), User.id.asc())
            .limit(LEADERBOARD_LIMIT)
        )
        rows = list((await session.execute(stmt)).scalars().all())

    lines = ["🏆 <b>Top Referrers</b>", ""]
    if not rows:
        lines.append("No referrals yet.")
    else:
        for i, user in enumerate(rows, 1):
            name = safe_username(user) if user.username else (user.first_name or str(user.id))
            lines.append(
                f"<b>{i}.</b> {html.escape(name)} — {user.referrals} referrals — ⭐ {user.stars}"
            )

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "withdraw")
async def cb_withdraw(callback: CallbackQuery):
    if not await gate_and_menu(bot, callback.from_user.id, callback.message.chat.id):
        await callback.answer()
        return

    user = await get_user(callback.from_user.id)
    active = await get_active_withdrawal(callback.from_user.id)
    if active:
        await callback.message.edit_text(
            f"💸 <b>Withdrawal pending</b>\n\n"
            f"Request <code>#{active.id}</code> for <b>{active.amount} Stars</b> is already pending.\n\n"
            "Please wait for admin processing.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )
        await callback.answer()
        return

    stars = user.stars if user else 0
    builder = InlineKeyboardBuilder()
    if stars >= WITHDRAWAL_AMOUNT:
        builder.row(
            InlineKeyboardButton(
                text=f"Withdraw {WITHDRAWAL_AMOUNT} ⭐",
                callback_data="withdraw_15",
            )
        )
    else:
        builder.row(
            InlineKeyboardButton(
                text=f"🔒 {WITHDRAWAL_AMOUNT} ⭐ — Not Enough",
                callback_data="withdraw_locked",
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ Back", callback_data="home"))

    text = (
        "💸 <b>Withdraw</b>\n\n"
        f"Only withdrawal option: <b>{WITHDRAWAL_AMOUNT} Stars</b>.\n"
        f"Your balance: <b>{stars} Stars</b>.\n\n"
        "Once requested, the amount is reserved until the admin approves or rejects the request."
    )
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "withdraw_locked")
async def cb_withdraw_locked(callback: CallbackQuery):
    await callback.answer(
        f"You need at least {WITHDRAWAL_AMOUNT} Stars to withdraw.",
        show_alert=True,
    )


async def create_withdrawal(user_id: int) -> tuple[Optional[Withdrawal], Optional[str]]:
    """Reserve 15 internal Stars and create one pending request atomically."""
    async with SessionLocal() as session:
        pending = await session.execute(
            select(Withdrawal)
            .where(
                Withdrawal.user_id == user_id,
                Withdrawal.status == "pending",
            )
            .limit(1)
        )
        if pending.scalars().first() is not None:
            return None, "pending"

        user = await session.get(User, user_id)
        if not user or user.banned:
            return None, "banned"
        if user.stars < WITHDRAWAL_AMOUNT:
            return None, "insufficient"

        user.stars -= WITHDRAWAL_AMOUNT
        wd = Withdrawal(
            user_id=user_id,
            amount=WITHDRAWAL_AMOUNT,
            status="pending",
        )
        session.add(wd)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return None, "pending"

        # Update admin_note after we have the auto-increment request ID.
        wd.admin_note = generate_transaction_id(user_id, wd.id)
        await session.commit()
        return wd, None


async def fail_withdrawal_and_refund(withdrawal_id: int) -> None:
    async with SessionLocal() as session:
        wd = await session.get(Withdrawal, withdrawal_id)
        if not wd or wd.status != "pending":
            return
        user = await session.get(User, wd.user_id)
        if user:
            user.stars += wd.amount
        wd.status = "failed"
        wd.processed_at = datetime.now(timezone.utc)
        await session.commit()


@router.callback_query(F.data == "withdraw_15")
async def cb_withdraw_15(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not await gate_and_menu(bot, user_id, callback.message.chat.id):
        await callback.answer()
        return

    wd, error = await create_withdrawal(user_id)
    if error == "pending":
        await callback.answer("You already have a pending withdrawal.", show_alert=True)
        return
    if error == "insufficient":
        await callback.answer(f"You need at least {WITHDRAWAL_AMOUNT} Stars.", show_alert=True)
        return
    if error == "banned":
        await callback.answer("Your account is banned.", show_alert=True)
        return
    if wd is None:
        await callback.answer("Could not create withdrawal. Try again.", show_alert=True)
        return

    user = await get_user(user_id)
    if user is None:
        await fail_withdrawal_and_refund(wd.id)
        await callback.answer("Could not load your account. Try again.", show_alert=True)
        return

    try:
        # This is the automatic request-channel post matching the format shown in the screenshot.
        await bot.send_message(
            REQUEST_CHANNEL_ID,
            build_withdrawal_request_text(wd, user, "🟡 PENDING"),
            parse_mode=ParseMode.HTML,
            reply_markup=withdrawal_keyboard(wd.id),
        )
    except Exception:
        logger.exception("Failed to post withdrawal #%s to request channel", wd.id)
        await fail_withdrawal_and_refund(wd.id)
        await callback.message.edit_text(
            "❌ <b>Withdrawal request failed.</b>\n\n"
            "The reserved Stars were returned to your balance. Please try again later.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )
        await callback.answer("Request channel is unavailable.", show_alert=True)
        return

    await callback.message.edit_text(
        "✅ <b>Withdrawal request submitted.</b>\n\n"
        "⏳ <b>Wait few hours, Stars will be sent.</b>\n\n"
        "Your request has been sent to the admin for approval.",
        parse_mode=ParseMode.HTML,
        reply_markup=payment_channel_keyboard(),
    )
    await callback.answer("Withdrawal request sent")


@router.callback_query(F.data == "home")
async def cb_home(callback: CallbackQuery):
    if not await gate_and_menu(bot, callback.from_user.id, callback.message.chat.id):
        await callback.answer()
        return

    await callback.message.edit_text(
        "🏠 <b>Main Menu</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )
    await callback.answer()


# =============================================================================
# Automatic unlock after channel joins
# =============================================================================
@router.chat_member()
async def on_chat_member_update(event: ChatMemberUpdated):
    if event.chat.id not in REQUIRED_CHANNEL_IDS:
        return

    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status
    joined_now = new_status in {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.RESTRICTED,
    }
    was_out = old_status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
    if not (joined_now and was_out):
        return

    user = event.new_chat_member.user
    if user.is_bot or await is_admin(user.id):
        return

    db_user = await get_or_create_user(user)
    if db_user.banned or await maintenance_enabled():
        return

    if await check_all_channels(bot, user.id):
        await award_referral_if_eligible(user.id)
        with suppress(Exception):
            await bot.send_message(
                user.id,
                "✅ <b>You joined all required channels.</b>\n\nThe bot is now unlocked!",
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(),
            )


# =============================================================================
# Admin commands / panel
# =============================================================================
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not await is_admin(message.from_user.id):
        # Deliberately no response for normal users.
        return
    await message.answer(
        "🛠 <b>Admin Panel</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard(),
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    if not await is_admin(message.from_user.id):
        return
    await set_maintenance(True)
    await message.answer("⏸ Bot paused. Admin access remains available.")


@router.message(Command("resume"))
async def cmd_resume(message: Message):
    if not await is_admin(message.from_user.id):
        return
    await set_maintenance(False)
    await message.answer("▶️ Bot resumed.")


@router.message(Command("user"))
async def cmd_user(message: Message, command: CommandObject):
    if not await is_admin(message.from_user.id):
        return
    if not command.args:
        await message.answer("Usage: /user <telegram_id_or_username>")
        return
    await send_user_search_result(message, command.args.strip())


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    if not await is_admin(message.from_user.id):
        return
    if not command.args:
        await message.answer("Usage: /ban <telegram_id>")
        return

    try:
        uid = int(command.args.strip().replace("@", ""))
    except ValueError:
        await message.answer("Use the numeric Telegram ID for banning.")
        return

    async with SessionLocal() as session:
        user = await session.get(User, uid)
        if not user:
            await message.answer("User not found in database.")
            return
        user.banned = True
        await session.commit()

    await message.answer(f"🚫 User <code>{uid}</code> banned.", parse_mode=ParseMode.HTML)


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    if not await is_admin(message.from_user.id):
        return
    if not command.args:
        await message.answer("Usage: /unban <telegram_id>")
        return

    try:
        uid = int(command.args.strip().replace("@", ""))
    except ValueError:
        await message.answer("Use the numeric Telegram ID for unbanning.")
        return

    async with SessionLocal() as session:
        user = await session.get(User, uid)
        if not user:
            await message.answer("User not found in database.")
            return
        user.banned = False
        await session.commit()

    await message.answer(f"✅ User <code>{uid}</code> unbanned.", parse_mode=ParseMode.HTML)


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext, command: CommandObject):
    if not await is_admin(message.from_user.id):
        return

    if command.args:
        await do_broadcast(command.args.strip(), message)
        return

    await state.set_state(AdminStates.broadcast)
    await message.answer(
        "📣 Send the text you want to broadcast to all non-banned users. Send /cancel to cancel."
    )


@router.message(Command("cancel"), AdminStates.broadcast)
async def cancel_broadcast(message: Message, state: FSMContext):
    if await is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Cancelled.")


@router.message(AdminStates.broadcast, F.text)
async def receive_broadcast(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.clear()
    await do_broadcast(message.text, message)


async def do_broadcast(text: str, admin_message: Message) -> None:
    async with SessionLocal() as session:
        stmt = select(User.id).where(User.banned.is_(False))
        user_ids = [row[0] for row in (await session.execute(stmt)).all()]

    sent = 0
    failed = 0
    for user_id in user_ids:
        try:
            await bot.send_message(user_id, text)
            sent += 1
        except Exception as exc:
            failed += 1
            logger.info("Broadcast failed for %s: %s", user_id, exc)
        await asyncio.sleep(BROADCAST_DELAY)

    await admin_message.answer(
        f"📣 Broadcast finished. Sent: {sent}; failed: {failed}."
    )


async def send_user_search_result(message: Message, value: str) -> None:
    value = value.strip()
    async with SessionLocal() as session:
        user = None
        if value.isdigit():
            user = await session.get(User, int(value))
        else:
            username = value.lstrip("@").lower()
            user = (
                await session.execute(
                    select(User).where(func.lower(User.username) == username)
                )
            ).scalars().first()

    if not user:
        await message.answer("User not found.")
        return

    await message.answer(
        "🔎 <b>User</b>\n\n"
        f"ID: <code>{user.id}</code>\n"
        f"Username: {html.escape(safe_username(user))}\n"
        f"Stars: <b>{user.stars}</b>\n"
        f"Referrals: <b>{user.referrals}</b>\n"
        f"Banned: <b>{user.banned}</b>\n"
        f"Created: <code>{html.escape(str(user.created_at))}</code>",
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "admin_requests")
async def cb_admin_requests(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return

    async with SessionLocal() as session:
        stmt = (
            select(Withdrawal)
            .where(Withdrawal.status == "pending")
            .order_by(Withdrawal.id.asc())
            .limit(10)
        )
        requests = list((await session.execute(stmt)).scalars().all())

    if not requests:
        await callback.message.answer("📥 No pending withdrawal requests.")
        await callback.answer()
        return

    for wd in requests:
        user = await get_user(wd.user_id)
        if not user:
            continue
        await callback.message.answer(
            build_withdrawal_request_text(wd, user, "🟡 PENDING"),
            parse_mode=ParseMode.HTML,
            reply_markup=withdrawal_keyboard(wd.id),
        )

    await callback.answer()


@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return

    async with SessionLocal() as session:
        total = (await session.execute(select(func.count(User.id)))).scalar_one()
        banned = (
            await session.execute(
                select(func.count(User.id)).where(User.banned.is_(True))
            )
        ).scalar_one()
        stars = (
            await session.execute(
                select(func.coalesce(func.sum(User.stars), 0))
            )
        ).scalar_one()
        pending = (
            await session.execute(
                select(func.count(Withdrawal.id)).where(Withdrawal.status == "pending")
            )
        ).scalar_one()
        approved = (
            await session.execute(
                select(func.coalesce(func.sum(Withdrawal.amount), 0)).where(
                    Withdrawal.status == "approved"
                )
            )
        ).scalar_one()
        rejected = (
            await session.execute(
                select(func.coalesce(func.sum(Withdrawal.amount), 0)).where(
                    Withdrawal.status.in_(["rejected", "failed"])
                )
            )
        ).scalar_one()

    await callback.message.answer(
        "📊 <b>Bot Stats</b>\n\n"
        f"Users: <b>{total}</b>\n"
        f"Banned: <b>{banned}</b>\n"
        f"Internal Stars held by users: <b>{stars}</b>\n"
        f"Pending withdrawals: <b>{pending}</b>\n"
        f"Approved withdrawals: <b>{approved} Stars</b>\n"
        f"Rejected/failed withdrawals: <b>{rejected} Stars</b>",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == "admin_search_help")
async def cb_admin_search_help(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminStates.search)
    await callback.message.answer(
        "Send a numeric Telegram ID or @username to search. Send /cancel to cancel."
    )
    await callback.answer()


@router.message(Command("cancel"), AdminStates.search)
@router.message(Command("cancel"), AdminStates.ban)
@router.message(Command("cancel"), AdminStates.unban)
async def cancel_admin_state(message: Message, state: FSMContext):
    if await is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Cancelled.")


@router.message(AdminStates.search)
async def receive_admin_search(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.clear()
    await send_user_search_result(message, message.text or "")


@router.callback_query(F.data == "admin_ban_help")
async def cb_admin_ban_help(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminStates.ban)
    await callback.message.answer(
        "Send the numeric Telegram ID to ban. Send /cancel to cancel."
    )
    await callback.answer()


@router.message(AdminStates.ban)
async def receive_admin_ban(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    try:
        uid = int((message.text or "").strip())
    except ValueError:
        await message.answer("Please send a numeric Telegram ID.")
        return

    async with SessionLocal() as session:
        user = await session.get(User, uid)
        if not user:
            await message.answer("User not found.")
            return
        user.banned = True
        await session.commit()

    await state.clear()
    await message.answer(f"🚫 User <code>{uid}</code> banned.", parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "admin_unban_help")
async def cb_admin_unban_help(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminStates.unban)
    await callback.message.answer(
        "Send the numeric Telegram ID to unban. Send /cancel to cancel."
    )
    await callback.answer()


@router.message(AdminStates.unban)
async def receive_admin_unban(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    try:
        uid = int((message.text or "").strip())
    except ValueError:
        await message.answer("Please send a numeric Telegram ID.")
        return

    async with SessionLocal() as session:
        user = await session.get(User, uid)
        if not user:
            await message.answer("User not found.")
            return
        user.banned = False
        await session.commit()

    await state.clear()
    await message.answer(f"✅ User <code>{uid}</code> unbanned.", parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "admin_broadcast_help")
async def cb_admin_broadcast_help(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminStates.broadcast)
    await callback.message.answer(
        "Send the broadcast text now. Send /cancel to cancel."
    )
    await callback.answer()


@router.callback_query(F.data == "admin_stop")
async def cb_admin_stop(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await set_maintenance(True)
    await callback.message.answer(
        "⏸ Bot paused. Existing users are blocked, but admin access remains available."
    )
    await callback.answer()


@router.callback_query(F.data == "admin_resume")
async def cb_admin_resume(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await set_maintenance(False)
    await callback.message.answer("▶️ Bot resumed.")
    await callback.answer()


# =============================================================================
# Admin withdrawal approval / rejection
# =============================================================================
@router.callback_query(F.data.startswith("wd_approve:"))
async def cb_withdraw_approve(callback: CallbackQuery):
    # This check is the authorization barrier: nobody except ADMIN_ID can approve.
    if not await is_admin(callback.from_user.id):
        await callback.answer("Only the bot admin can approve withdrawals.", show_alert=True)
        return

    try:
        wd_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Invalid request.", show_alert=True)
        return

    async with SessionLocal() as session:
        wd = await session.get(Withdrawal, wd_id)
        if not wd or wd.status != "pending":
            await callback.answer("Request already processed or not found.", show_alert=True)
            return

        wd.status = "approved"
        wd.processed_at = datetime.now(timezone.utc)
        uid = wd.user_id
        amount = wd.amount
        txid = wd.admin_note or str(wd.id)
        user = await session.get(User, uid)
        await session.commit()

    approved_text = build_withdrawal_request_text(
        wd,
        user if user else User(id=uid, username=None, first_name=None, last_name=None),
        "✅ APPROVED",
    )
    with suppress(Exception):
        await callback.message.edit_text(
            approved_text,
            parse_mode=ParseMode.HTML,
            reply_markup=None,
        )

    # Important: Telegram does not provide a generic bot API for transferring
    # arbitrary Stars from a bot balance to a user's wallet. Approval therefore
    # means admin approval; the real Stars are sent manually by the admin.
    with suppress(Exception):
        await bot.send_message(
            uid,
            "✅ <b>Your withdrawal has been approved.</b>\n\n"
            "⏳ <b>Wait few hours, Stars will be sent.</b>\n\n"
            f"Transaction ID: <code>{html.escape(txid)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=payment_channel_keyboard(),
        )

    await callback.message.answer(
        f"✅ Request #{wd_id} approved. Please send {amount} Stars manually, then keep the transaction record <code>{html.escape(txid)}</code>.",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer("Approved")


@router.callback_query(F.data.startswith("wd_reject:"))
async def cb_withdraw_reject(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("Only the bot admin can reject withdrawals.", show_alert=True)
        return

    try:
        wd_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Invalid request.", show_alert=True)
        return

    async with SessionLocal() as session:
        wd = await session.get(Withdrawal, wd_id)
        if not wd or wd.status != "pending":
            await callback.answer("Request already processed or not found.", show_alert=True)
            return

        user = await session.get(User, wd.user_id)
        if user:
            user.stars += wd.amount
        wd.status = "rejected"
        wd.processed_at = datetime.now(timezone.utc)
        uid = wd.user_id
        amount = wd.amount
        txid = wd.admin_note or str(wd.id)
        await session.commit()

    rejected_user = user if user else User(id=uid, username=None, first_name=None, last_name=None)
    rejected_text = build_withdrawal_request_text(
        wd,
        rejected_user,
        "❌ REJECTED",
    )
    with suppress(Exception):
        await callback.message.edit_text(
            rejected_text,
            parse_mode=ParseMode.HTML,
            reply_markup=None,
        )

    with suppress(Exception):
        await bot.send_message(
            uid,
            f"↩️ <b>Your withdrawal request #{wd_id} was rejected.</b>\n\n"
            f"{amount} Stars were returned to your referral balance.\n"
            f"Transaction ID: <code>{html.escape(txid)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )

    await callback.message.answer(
        f"↩️ Request #{wd_id} rejected and {amount} Stars refunded.",
    )
    await callback.answer("Rejected")


# =============================================================================
# Normal-user catch-all
# =============================================================================
@router.message()
async def catch_all(message: Message):
    if not message.from_user:
        return

    if await is_admin(message.from_user.id):
        await message.answer("Use /admin to open the admin panel.")
        return

    if await maintenance_enabled():
        await message.answer("⏸ The bot is temporarily paused by the administrator.")
        return

    await get_or_create_user(message.from_user)
    await gate_and_menu(
        bot,
        message.from_user.id,
        message.chat.id,
        send_new_message=True,
    )


# =============================================================================
# Render webhook server
# =============================================================================
async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "telegram-refer-bot"})


async def telegram_webhook(request: web.Request) -> web.Response:
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return web.Response(status=403, text="Forbidden")

    try:
        payload = await request.json()
        update_obj = Update.model_validate(payload, context={"bot": bot})
        await dp.feed_update(bot, update_obj)
        return web.Response(status=200, text="OK")
    except Exception:
        logger.exception("Webhook update failed")
        return web.Response(status=500, text="Internal Server Error")


async def startup(app: web.Application) -> None:
    await init_db()

    await bot.delete_webhook(drop_pending_updates=False)
    base_url = WEBHOOK_BASE_URL or os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("WEBHOOK_BASE_URL or Render's RENDER_EXTERNAL_URL is required")

    webhook_url = f"{base_url}/telegram/webhook"
    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message", "callback_query", "chat_member"],
        drop_pending_updates=False,
    )
    logger.info("Webhook configured: %s", webhook_url)


async def shutdown(app: web.Application) -> None:
    with suppress(Exception):
        await bot.delete_webhook(drop_pending_updates=False)
    await bot.session.close()
    await engine.dispose()


async def main() -> None:
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/telegram/webhook", telegram_webhook)
    app.on_startup.append(startup)
    app.on_cleanup.append(shutdown)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("HTTP server started on port %s", PORT)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
