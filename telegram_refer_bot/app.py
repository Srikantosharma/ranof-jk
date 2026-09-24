import asyncio
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
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, Message, Update
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func, select, update
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.exc import IntegrityError

load_dotenv()

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "7652741479"))
PORT = int(os.getenv("PORT", "8080"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is required")

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

CHANNELS = [
    {"id": -1002164113779, "url": "https://t.me/+yHZ6leGHUqFmOWE9", "title": "Channel 1"},
    {"id": -1002130367937, "url": "https://t.me/+v95y6KvYOfhiNjk9", "title": "Channel 2"},
    {"id": -1002051656020, "url": "https://t.me/+GZFZJBuLsv9iMzA1", "title": "Channel 3"},
    {"id": -1003232143907, "url": "https://t.me/+MEilvYI8GDhmM2Nl", "title": "Channel 4"},
    {"id": -1002271732066, "url": "https://t.me/incomecryptovip2", "title": "Income Crypto VIP"},
]

# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class Withdrawal(Base):
    __tablename__ = "withdrawals"


    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False, default=WITHDRAWAL_AMOUNT)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    admin_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
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


# -----------------------------------------------------------------------------
# UI helpers
# -----------------------------------------------------------------------------
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
        InlineKeyboardButton(text="✅ Mark Paid", callback_data=f"wd_paid:{withdrawal_id}"),
        InlineKeyboardButton(text="↩️ Reject + Refund", callback_data=f"wd_reject:{withdrawal_id}"),
    )
    return builder.as_markup()


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
        stmt = select(Withdrawal).where(Withdrawal.user_id == user_id, Withdrawal.status == "pending").order_by(Withdrawal.id.desc())
        return (await session.execute(stmt)).scalars().first()


# -----------------------------------------------------------------------------
# Membership / referrals
# -----------------------------------------------------------------------------
async def user_is_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member.status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
            return True
        if member.status == ChatMemberStatus.RESTRICTED:
            return bool(getattr(member, "is_member", False))
        return False
    except Exception as exc:
        logger.warning("Membership check failed for %s in %s: %s", user_id, chat_id, exc)
        return False


async def check_all_channels(bot: Bot, user_id: int) -> bool:
    results = await asyncio.gather(*(user_is_member(bot, channel["id"], user_id) for channel in CHANNELS))
    return all(results)


async def set_referrer_from_start(user_id: int, ref_code: Optional[str]) -> None:
    if not ref_code or not ref_code.startswith("ref_"):
        return
    with suppress(ValueError):
        referrer_id = int(ref_code[4:])
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
        user.referrer_id = referrer_id
        await session.commit()


async def award_referral_if_eligible(user_id: int) -> None:
    # The first UPDATE atomically claims the one-time reward, preventing
    # double-credit if multiple channel updates arrive at the same time.
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        if not user or user.referrer_id is None or user.referral_rewarded:
            return
        referrer_id = user.referrer_id
        referrer = await session.get(User, referrer_id)
        if not referrer or referrer.banned:
            return

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


async def gate_and_menu(bot: Bot, user_id: int, chat_id: int, send_new_message: bool = False) -> bool:
    if await is_admin(user_id):
        await award_referral_if_eligible(user_id)
        if send_new_message:
            await bot.send_message(chat_id, "✅ Welcome, admin.", reply_markup=main_keyboard())
        return True

    user = await get_user(user_id)
    if user and user.banned:
        await bot.send_message(chat_id, "🚫 You are banned from using this bot.")
        return False

    if not await check_all_channels(bot, user_id):
        await bot.send_message(
            chat_id,
            "🔒 To use the bot, please join **all 5 channels** below.\n\nAfter joining, tap **I Joined — Check**.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=join_keyboard(),
        )
        return False

    await award_referral_if_eligible(user_id)
    if send_new_message:
        await bot.send_message(chat_id, "✅ All requirements completed. Choose an option below:", reply_markup=main_keyboard())
    return True


# -----------------------------------------------------------------------------
# Bot / router
# -----------------------------------------------------------------------------
bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


class AdminStates(StatesGroup):
    search = State()
    ban = State()
    unban = State()
    broadcast = State()


@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject):
    await get_or_create_user(message.from_user)
    await update_user_profile(message.from_user)
    await set_referrer_from_start(message.from_user.id, command.args)

    if not await is_admin(message.from_user.id) and await maintenance_enabled():
        await message.answer("⏸ The bot is temporarily paused by the administrator.")
        return
    await gate_and_menu(bot, message.from_user.id, message.chat.id, send_new_message=True)


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
    await callback.message.edit_text("✅ Verified. Welcome to the bot!", reply_markup=main_keyboard())
    await callback.answer("Verified")


@router.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery):
    if not await gate_and_menu(bot, callback.from_user.id, callback.message.chat.id):
        await callback.answer()
        return
    user = await get_user(callback.from_user.id)
    username = f"@{user.username}" if user and user.username else "(no username)"
    text = (
        "👤 **Profile**\n\n"
        f"ID: `{callback.from_user.id}`\n"
        f"Username: {username}\n"
        f"⭐ Stars: **{user.stars if user else 0}**\n"
        f"👥 Successful referrals: **{user.referrals if user else 0}**"
    )
    await callback.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard())
    await callback.answer()


@router.callback_query(F.data == "balance")
async def cb_balance(callback: CallbackQuery):
    if not await gate_and_menu(bot, callback.from_user.id, callback.message.chat.id):
        await callback.answer()
        return
    user = await get_user(callback.from_user.id)
    stars = user.stars if user else 0
    await callback.message.edit_text(
        "⭐ **Balance**\n\n"
        f"Your referral balance: **{stars} Stars**\n\n"
        "These are Stars tracked by this referral program; they are not a live readout of the user's personal Telegram Stars wallet.",
        parse_mode=ParseMode.MARKDOWN,
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
    text = (
        "🎁 **Refer & Earn**\n\n"
        f"Reward: **{REFERRAL_REWARD} Stars** per successful referral.\n"
        f"Your successful referrals: **{refs}**\n\n"
        "Your unique referral link:\n"
        f"`{link}`"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Open Referral Link", url=link))
    builder.row(InlineKeyboardButton(text="⬅️ Back", callback_data="home"))
    await callback.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=builder.as_markup())
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
    lines = ["🏆 **Top Referrers**", ""]
    if not rows:
        lines.append("No referrals yet.")
    else:
        for i, user in enumerate(rows, 1):
            name = f"@{user.username}" if user.username else (user.first_name or str(user.id))
            lines.append(f"**{i}.** {name} — {user.referrals} referrals — ⭐ {user.stars}")
    lines.append("\nThis list is generated from the current database balance/referral count.")
    await callback.message.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard())
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
            f"💸 **Withdrawal pending**\n\nRequest #{active.id} for **{active.amount} Stars** is already pending.\n\nPlease wait for admin processing.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(),
        )
        await callback.answer()
        return
    stars = user.stars if user else 0
    builder = InlineKeyboardBuilder()
    if stars >= WITHDRAWAL_AMOUNT:
        builder.row(InlineKeyboardButton(text=f"Withdraw {WITHDRAWAL_AMOUNT} ⭐", callback_data="withdraw_15"))
    else:
        builder.row(InlineKeyboardButton(text="🔒 15 ⭐ — Not Enough", callback_data="withdraw_locked"))
    builder.row(InlineKeyboardButton(text="⬅️ Back", callback_data="home"))
    text = (
        "💸 **Withdraw**\n\n"
        f"Minimum and only withdrawal option: **{WITHDRAWAL_AMOUNT} Stars**.\n"
        f"Your balance: **{stars} Stars**.\n\n"
        "A withdrawal request is reserved from your internal balance until an admin approves or rejects it."
    )
    await callback.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data == "withdraw_locked")
async def cb_withdraw_locked(callback: CallbackQuery):
    await callback.answer("You need 15 Stars to withdraw.", show_alert=True)


@router.callback_query(F.data == "withdraw_15")
async def cb_withdraw_15(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not await gate_and_menu(bot, user_id, callback.message.chat.id):
        await callback.answer()
        return

    async with SessionLocal() as session:
        # Atomic-ish guard: only reserve if balance >= 15 and no pending request exists.
        pending = await session.execute(
            select(Withdrawal).where(Withdrawal.user_id == user_id, Withdrawal.status == "pending").limit(1)
        )
        if pending.scalars().first() is not None:
            await callback.answer("You already have a pending withdrawal.", show_alert=True)
            return

        user = await session.get(User, user_id)
        if not user or user.stars < WITHDRAWAL_AMOUNT:
            await callback.answer("You need at least 15 Stars.", show_alert=True)
            return

        user.stars -= WITHDRAWAL_AMOUNT
        wd = Withdrawal(user_id=user_id, amount=WITHDRAWAL_AMOUNT, status="pending")
        session.add(wd)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            await callback.answer("You already have a pending withdrawal.", show_alert=True)
            return
        withdrawal_id = wd.id

    await callback.message.edit_text(
        "✅ **Withdrawal requested**\n\n"
        "Please wait a few hours; your Stars will be sent after admin processing.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_keyboard(),
    )

    user = await get_user(user_id)
    username = f"@{user.username}" if user and user.username else "(no username)"
    admin_text = (
        "💸 **New Withdrawal Request**\n\n"
        f"Request ID: `{withdrawal_id}`\n"
        f"User: {username}\n"
        f"Telegram ID: `{user_id}`\n"
        f"Amount: **{WITHDRAWAL_AMOUNT} Stars**\n\n"
        "The 15 internal Stars have been reserved from the user's balance."
    )
    await bot.send_message(ADMIN_ID, admin_text, parse_mode=ParseMode.MARKDOWN, reply_markup=withdrawal_keyboard(withdrawal_id))
    await callback.answer()


@router.callback_query(F.data == "home")
async def cb_home(callback: CallbackQuery):
    if not await gate_and_menu(bot, callback.from_user.id, callback.message.chat.id):
        await callback.answer()
        return
    await callback.message.edit_text("🏠 **Main Menu**", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard())
    await callback.answer()


# -----------------------------------------------------------------------------
# Automatic unlock after channel joins
# -----------------------------------------------------------------------------
@router.chat_member()
async def on_chat_member_update(event: ChatMemberUpdated):
    if event.chat.id not in {c["id"] for c in CHANNELS}:
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
    await get_or_create_user(user)
    if await maintenance_enabled():
        return
    if await check_all_channels(bot, user.id):
        await award_referral_if_eligible(user.id)
        with suppress(Exception):
            await bot.send_message(user.id, "✅ You joined all required channels. The bot is now unlocked!", reply_markup=main_keyboard())


# -----------------------------------------------------------------------------
# Admin commands and panel
# -----------------------------------------------------------------------------
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not await is_admin(message.from_user.id):
        return
    await message.answer("🛠 **Admin Panel**", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard())


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
    await message.answer(f"🚫 User `{uid}` banned.", parse_mode=ParseMode.MARKDOWN)


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
    await message.answer(f"✅ User `{uid}` unbanned.", parse_mode=ParseMode.MARKDOWN)


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext, command: CommandObject):
    if not await is_admin(message.from_user.id):
        return
    if command.args:
        await do_broadcast(command.args.strip(), message)
        return
    await state.set_state(AdminStates.broadcast)
    await message.answer("📣 Send the text you want to broadcast to all non-banned users. Send /cancel to cancel.")


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

    sent = failed = 0
    for user_id in user_ids:
        try:
            await bot.send_message(user_id, text)
            sent += 1
        except Exception as exc:
            failed += 1
            logger.info("Broadcast failed for %s: %s", user_id, exc)
        await asyncio.sleep(0.04)  # ~25 msg/s, safely under normal bot broadcast limits.
    await admin_message.answer(f"📣 Broadcast finished. Sent: {sent}; failed: {failed}.")


async def send_user_search_result(message: Message, value: str) -> None:
    value = value.strip()
    async with SessionLocal() as session:
        user = None
        if value.isdigit():
            user = await session.get(User, int(value))
        else:
            username = value.lstrip("@").lower()
            user = (await session.execute(select(User).where(func.lower(User.username) == username))).scalars().first()
    if not user:
        await message.answer("User not found.")
        return
    await message.answer(
        "🔎 **User**\n\n"
        f"ID: `{user.id}`\n"
        f"Username: @{user.username if user.username else '(none)'}\n"
        f"Stars: **{user.stars}**\n"
        f"Referrals: **{user.referrals}**\n"
        f"Banned: **{user.banned}**\n"
        f"Created: `{user.created_at}`",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.callback_query(F.data == "admin_requests")
async def cb_admin_requests(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    async with SessionLocal() as session:
        stmt = select(Withdrawal).where(Withdrawal.status == "pending").order_by(Withdrawal.id.asc()).limit(10)
        requests = list((await session.execute(stmt)).scalars().all())
    if not requests:
        await callback.message.answer("📥 No pending withdrawal requests.")
        await callback.answer()
        return
    for wd in requests:
        user = await get_user(wd.user_id)
        username = f"@{user.username}" if user and user.username else "(no username)"
        await callback.message.answer(
            "💸 **Pending Withdrawal**\n\n"
            f"Request: `{wd.id}`\n"
            f"User: {username}\n"
            f"Telegram ID: `{wd.user_id}`\n"
            f"Amount: **{wd.amount} Stars**\n"
            f"Created: `{wd.created_at}`",
            parse_mode=ParseMode.MARKDOWN,
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
        banned = (await session.execute(select(func.count(User.id)).where(User.banned.is_(True)))).scalar_one()
        stars = (await session.execute(select(func.coalesce(func.sum(User.stars), 0)))).scalar_one()
        pending = (await session.execute(select(func.count(Withdrawal.id)).where(Withdrawal.status == "pending"))).scalar_one()
        paid = (await session.execute(select(func.coalesce(func.sum(Withdrawal.amount), 0)).where(Withdrawal.status == "paid"))).scalar_one()
    await callback.message.answer(
        "📊 **Bot Stats**\n\n"
        f"Users: **{total}**\n"
        f"Banned: **{banned}**\n"
        f"Internal Stars held by users: **{stars}**\n"
        f"Pending withdrawals: **{pending}**\n"
        f"Paid withdrawal Stars: **{paid}**",
        parse_mode=ParseMode.MARKDOWN,
    )
    await callback.answer()


@router.callback_query(F.data == "admin_search_help")
async def cb_admin_search_help(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminStates.search)
    await callback.message.answer("Send a numeric Telegram ID or @username to search. Send /cancel to cancel.")
    await callback.answer()


@router.message(AdminStates.search)
async def receive_admin_search(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("Cancelled.")
        return
    await state.clear()
    await send_user_search_result(message, message.text or "")


@router.callback_query(F.data == "admin_ban_help")
async def cb_admin_ban_help(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminStates.ban)
    await callback.message.answer("Send the numeric Telegram ID to ban. Send /cancel to cancel.")
    await callback.answer()


@router.message(AdminStates.ban)
async def receive_admin_ban(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
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
    await message.answer(f"🚫 User `{uid}` banned.", parse_mode=ParseMode.MARKDOWN)


@router.callback_query(F.data == "admin_unban_help")
async def cb_admin_unban_help(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminStates.unban)
    await callback.message.answer("Send the numeric Telegram ID to unban. Send /cancel to cancel.")
    await callback.answer()


@router.message(AdminStates.unban)
async def receive_admin_unban(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
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
    await message.answer(f"✅ User `{uid}` unbanned.", parse_mode=ParseMode.MARKDOWN)


@router.callback_query(F.data == "admin_broadcast_help")
async def cb_admin_broadcast_help(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminStates.broadcast)
    await callback.message.answer("Send the broadcast text now. Send /cancel to cancel.")
    await callback.answer()


@router.callback_query(F.data == "admin_stop")
async def cb_admin_stop(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await set_maintenance(True)
    await callback.message.answer("⏸ Bot paused. Existing users will be blocked, but the admin can still operate the panel.")
    await callback.answer()


@router.callback_query(F.data == "admin_resume")
async def cb_admin_resume(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await set_maintenance(False)
    await callback.message.answer("▶️ Bot resumed.")
    await callback.answer()


@router.callback_query(F.data.startswith("wd_paid:"))
async def cb_withdraw_paid(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer()
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
        wd.status = "paid"
        wd.processed_at = datetime.now(timezone.utc)
        await session.commit()
        uid = wd.user_id
        amount = wd.amount
    with suppress(Exception):
        await bot.send_message(uid, f"✅ Your withdrawal request #{wd_id} for {amount} Stars has been marked as paid by admin.")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ Request #{wd_id} marked paid. Remember to send the real Telegram Stars manually.")
    await callback.answer("Marked paid")


@router.callback_query(F.data.startswith("wd_reject:"))
async def cb_withdraw_reject(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer()
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
        await session.commit()
        uid = wd.user_id
        amount = wd.amount
    with suppress(Exception):
        await bot.send_message(uid, f"↩️ Your withdrawal request #{wd_id} was rejected. {amount} Stars were returned to your referral balance.")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"↩️ Request #{wd_id} rejected and balance refunded.")
    await callback.answer("Rejected")


# -----------------------------------------------------------------------------
# Catch-all protection / private chat UI
# -----------------------------------------------------------------------------
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
    await gate_and_menu(bot, message.from_user.id, message.chat.id, send_new_message=True)


# -----------------------------------------------------------------------------
# Webhook server for Render
# -----------------------------------------------------------------------------
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
    app.add_get("/health", health)
    app.add_post("/telegram/webhook", telegram_webhook)
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
