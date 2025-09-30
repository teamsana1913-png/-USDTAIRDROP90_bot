import os
import re
import asyncio
import logging
import uuid
from decimal import Decimal
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from typing import List

from sqlalchemy import (
    create_engine, Column, BigInteger, String, DateTime, func,
    Numeric, ForeignKey, Integer
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, Defaults
)
from telegram.constants import ParseMode

# --- 1. CONFIGURATION AND SETUP ---
load_dotenv()

# Load Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") # Should be async: postgresql+asyncpg://...
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")
BOT_USERNAME = os.getenv("BOT_USERNAME", "YourBotUsername")

# Basic Logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
REFERRAL_BONUS = Decimal("10.0")
MIN_WITHDRAWAL = Decimal("100.0")

# Conversation Handler States
SET_WALLET, WITHDRAW_AMOUNT = range(2)

# --- 2. DATABASE MODELS AND SETUP ---
# SQLAlchemy setup for async
engine = create_async_engine(DATABASE_URL)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

def generate_referral_code():
    return str(uuid.uuid4().hex[:10])

# User Model
class User(Base):
    __tablename__ = "users"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    wallet_address = Column(String, nullable=True)
    balance = Column(Numeric(18, 8), default=0.0, nullable=False)
    referral_code = Column(String, unique=True, default=generate_referral_code)
    referred_by = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=True)
    total_invites = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    referrer = relationship("User", remote_side=[telegram_id])

# Withdrawal Request Model
class WithdrawalRequest(Base):
    __tablename__ = "withdrawal_requests"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=False)
    amount = Column(Numeric(18, 8), nullable=False)
    wallet_address = Column(String, nullable=False)
    status = Column(String, default="PENDING", nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    user = relationship("User")

# Function to create tables
async def create_db_and_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# --- 3. BOT MESSAGES AND KEYBOARDS ---

# Messages
WELCOME_TEXT = """👋 Welcome!  
Join our channels to unlock rewards & updates 🚀  
  
👉 @USDTAIRDROPchat  
👉 @OFFLICALUSDTAIRDROP  
👉 @OFFICALUPDATEUSDT  
👉 @NEWPROJECTOFUSDT  
  
✅ After joining, tap Continue to start!"""
VERIFIED_TEXT = "✅ You are now verified!\nChoose an option below ⬇"
BONUS_TEXT = "🎉 Congratulations! You just received 13.3 USDT 💸"
WITHDRAW_PROMPT = "❗ Minimum Withdraw Is 100 USDT\n\n💳 Enter the amount you want to withdraw:"
WITHDRAW_SUCCESS = "✅ Withdrawal request received. Status: PENDING\n⏳ Your withdrawal will be processed within 24 hours."

def balance_text(balance, wallet, ref_link):
    return f"💎 My Balance\n━━━━━━━━━━━━━━━━\n💰 USDT: {balance} ≈ ${balance}\n\n💳 Wallet: {wallet or 'Not set'}\n\n🔗 Your Invite Link:\n{ref_link}"

def referral_text(total_invites, ref_link):
    return f"💸 Get 10 USDT for Every Friend!\n\n📊 Friends Invited: {total_invites}\n\n🔗 Your Referral Link:\n{ref_link}"

def set_wallet_prompt(current_wallet):
    return f"💡 *Your current wallet:* `{current_wallet or 'Not set'}`\n\n✍️ Please send your new BEP20 wallet address."

# Keyboards
continue_button = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Continue", callback_data="verify_user")]])
main_keyboard = ReplyKeyboardMarkup([
    ["🥇 💰 Balance"], ["🥈 🎊 Bonus"], ["🥉 💑 Referral"],
    ["4️⃣ 📤 Withdraw"], ["5️⃣ 💼 Set Wallet"],
], resize_keyboard=True)

# --- 4. BOT HANDLERS AND LOGIC ---

async def get_or_create_user(session: AsyncSession, telegram_id: int, user_data) -> User:
    result = await session.execute(select(User).filter_by(telegram_id=telegram_id))
    user = result.scalars().first()
    if not user:
        user = User(
            telegram_id=telegram_id,
            username=user_data.username,
            first_name=user_data.first_name,
        )
        session.add(user)
        await session.commit()
    return user

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        user = await get_or_create_user(session, update.effective_user.id, update.effective_user)
        # Referral logic
        if context.args:
            referrer_code = context.args[0]
            if not user.referred_by:
                result = await session.execute(select(User).filter_by(referral_code=referrer_code))
                referrer = result.scalars().first()
                if referrer and referrer.telegram_id != user.telegram_id:
                    user.referred_by = referrer.telegram_id
                    referrer.total_invites += 1
                    referrer.balance += REFERRAL_BONUS
                    await session.commit()
    await update.message.reply_text(WELCOME_TEXT, reply_markup=continue_button)

async def continue_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text="✅ Verified!")
    await context.bot.send_message(chat_id=query.message.chat_id, text=VERIFIED_TEXT, reply_markup=main_keyboard)

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        user = await get_or_create_user(session, update.effective_user.id, update.effective_user)
        ref_link = f"https://t.me/{BOT_USERNAME}?start={user.referral_code}"
        await update.message.reply_text(balance_text(user.balance, user.wallet_address, ref_link))

async def bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(BONUS_TEXT)

async def referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        user = await get_or_create_user(session, update.effective_user.id, update.effective_user)
        ref_link = f"https://t.me/{BOT_USERNAME}?start={user.referral_code}"
        await update.message.reply_text(referral_text(user.total_invites, ref_link))

async def set_wallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        user = await get_or_create_user(session, update.effective_user.id, update.effective_user)
        await update.message.reply_text(set_wallet_prompt(user.wallet_address), parse_mode=ParseMode.MARKDOWN_V2)
    return SET_WALLET

async def set_wallet_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text
    if not re.match(r"^0x[a-fA-F0-9]{40}$", address):
        await update.message.reply_text("❌ Invalid BEP20 address format. Please try again.")
        return SET_WALLET
    async with async_session() as session:
        user = await session.get(User, update.effective_user.id)
        user.wallet_address = address
        await session.commit()
    await update.message.reply_text(f"✅ Wallet updated to: {address}")
    return ConversationHandler.END

async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        user = await get_or_create_user(session, update.effective_user.id, update.effective_user)
        if not user.wallet_address:
            await update.message.reply_text("⚠️ Please set your wallet address first using '💼 Set Wallet'.")
            return ConversationHandler.END
    await update.message.reply_text(WITHDRAW_PROMPT)
    return WITHDRAW_AMOUNT

async def withdraw_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = Decimal(update.message.text)
    except Exception:
        await update.message.reply_text("❌ Invalid amount. Please enter a number.")
        return WITHDRAW_AMOUNT

    async with async_session() as session:
        user = await session.get(User, update.effective_user.id, with_for_update=True)
        if amount < MIN_WITHDRAWAL:
            await update.message.reply_text(f"❌ Minimum withdrawal is {MIN_WITHDRAWAL} USDT.")
            return WITHDRAW_AMOUNT
        if amount > user.balance:
            await update.message.reply_text("❌ Insufficient balance.")
            return WITHDRAW_AMOUNT
        
        user.balance -= amount
        new_request = WithdrawalRequest(user_id=user.telegram_id, amount=amount, wallet_address=user.wallet_address)
        session.add(new_request)
        await session.commit()
    
    await update.message.reply_text(WITHDRAW_SUCCESS)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operation cancelled.", reply_markup=main_keyboard)
    return ConversationHandler.END

# --- 5. FASTAPI ADMIN API ---

# API Key Security
api_key_header = APIKeyHeader(name="X-API-KEY", auto_error=False)
async def get_api_key(api_key_header: str = Security(api_key_header)):
    if api_key_header == ADMIN_API_KEY:
        return api_key_header
    raise HTTPException(status_code=403, detail="Could not validate credentials")

# Pydantic Models for API
class UserOut(BaseModel):
    telegram_id: int
    username: str | None
    balance: Decimal
    wallet_address: str | None
    class Config: orm_mode = True

class WithdrawalOut(BaseModel):
    id: int
    user_id: int
    amount: Decimal
    status: str
    class Config: orm_mode = True

# Lifespan manager for FastAPI
@asynccontextmanager
async def lifespan(app: FastAPI):
    # On startup
    await create_db_and_tables()
    
    defaults = Defaults(parse_mode=ParseMode.HTML)
    bot_app = Application.builder().token(BOT_TOKEN).defaults(defaults).build()

    # Add Handlers
    bot_app.add_handler(CommandHandler('start', start))
    bot_app.add_handler(CallbackQueryHandler(continue_callback, pattern='^verify_user$'))
    bot_app.add_handler(MessageHandler(filters.Regex('^🥇 💰 Balance$'), balance))
    bot_app.add_handler(MessageHandler(filters.Regex('^🥈 🎊 Bonus$'), bonus))
    bot_app.add_handler(MessageHandler(filters.Regex('^🥉 💑 Referral$'), referral))
    bot_app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^5️⃣ 💼 Set Wallet$'), set_wallet_start)],
        states={SET_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_wallet_address)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    bot_app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^4️⃣ 📤 Withdraw$'), withdraw_start)],
        states={WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount_handler)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    
    # Run bot in background
    asyncio.create_task(bot_app.run_polling())
    yield
    # On shutdown (if needed)

# FastAPI App Instance
app = FastAPI(lifespan=lifespan)

@app.get("/")
def root():
    return {"Status": "Bot and API are running"}

@app.get("/admin/users", response_model=List[UserOut], dependencies=[Depends(get_api_key)])
async def list_users(skip: int = 0, limit: int = 100):
    async with async_session() as session:
        result = await session.execute(select(User).offset(skip).limit(limit))
        return result.scalars().all()

@app.get("/admin/withdrawals", response_model=List[WithdrawalOut], dependencies=[Depends(get_api_key)])
async def list_withdrawals(status: str = "PENDING"):
    async with async_session() as session:
        result = await session.execute(select(WithdrawalRequest).filter_by(status=status))
        return result.scalars().all()

@app.put("/admin/withdrawals/{req_id}/status", dependencies=[Depends(get_api_key)])
async def update_withdrawal(req_id: int, new_status: str):
    if new_status not in ["PAID", "REJECTED"]:
        raise HTTPException(status_code=400, detail="Status must be PAID or REJECTED")
    async with async_session() as session:
        req = await session.get(WithdrawalRequest, req_id)
        if not req:
            raise HTTPException(status_code=404, detail="Request not found")
        req.status = new_status
        await session.commit()
    return {"status": "success", "new_status": new_status}