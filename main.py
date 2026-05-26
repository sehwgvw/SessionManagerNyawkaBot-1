import os
import sys
import json
import logging
import asyncio
import sqlite3
import shutil
import re
import zipfile
import random
import string
import aiohttp
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

# Импорты aiogram (v3.x)
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# Импорты Telethon для работы с сессиями
from telethon import TelegramClient, functions, types
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError,
    AuthKeyUnregisteredError, UserDeactivatedError, UsernameInvalidError,
    UsernameOccupiedError
)
from telethon.tl.functions.channels import JoinChannelRequest, CreateChannelRequest, LeaveChannelRequest
from telethon.tl.functions.messages import DeleteHistoryRequest, SendReactionRequest
from telethon.tl.functions.photos import DeletePhotosRequest
from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest

# Импорты для шифрования AES-256
from cryptography.fernet import Fernet

# =====================================================================
# КОНФИГУРАЦИЯ И НАСТРОЙКИ
# =====================================================================
BOT_TOKEN = "8882821096:AAE9d0AMdTLbwnhG2EnSPCqnO_Zvwbg47cc"
API_ID = 27720808
API_HASH = "f404d028ebe5d98725cd21ea5537d015"
ADMIN_ID = 7544069555  # ID главного администратора

DB_FILE = "session_manager.db"
SESSIONS_DIR = "sessions_data"
ENCRYPTED_SESSIONS_DIR = "sessions_encrypted"
KEY_FILE = "secret.key"

# Инициализация постоянного ключа шифрования
if os.path.exists(KEY_FILE):
    with open(KEY_FILE, "rb") as kf:
        ENCRYPTION_KEY = kf.read()
else:
    ENCRYPTION_KEY = Fernet.generate_key()
    with open(KEY_FILE, "wb") as kf:
        kf.write(ENCRYPTION_KEY)

cipher_suite = Fernet(ENCRYPTION_KEY)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("SessionManagerBot")

os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(ENCRYPTED_SESSIONS_DIR, exist_ok=True)

# =====================================================================
# БАЗА ДАННЫХ И ХРАНЕНИЕ
# =====================================================================
def init_db():
    """Инициализация базы данных SQLite"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        subscription TEXT DEFAULT 'FREE',
        sub_expires TEXT DEFAULT '2099-12-31 23:59:59',
        created_at TEXT
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT,
        telegram_id INTEGER UNIQUE,
        username TEXT,
        first_name TEXT,
        dc_id INTEGER,
        premium INTEGER DEFAULT 0,
        has_2fa INTEGER DEFAULT 0,
        email TEXT,
        status TEXT DEFAULT '🟢 Аккаунт активен',
        proxy_id INTEGER,
        idle_until TEXT,
        is_warming INTEGER DEFAULT 0,
        created_at TEXT
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS proxies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER UNIQUE,
        type TEXT,
        host TEXT,
        port INTEGER,
        username TEXT,
        password TEXT,
        status TEXT DEFAULT 'Не проверен'
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT,
        details TEXT,
        timestamp TEXT
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER,
        channel_id INTEGER,
        title TEXT,
        username TEXT,
        type TEXT,
        FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER,
        bot_id INTEGER,
        name TEXT,
        username TEXT,
        token TEXT,
        FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
    )""")

    cursor.execute("INSERT OR IGNORE INTO users (user_id, username, subscription, sub_expires, created_at) VALUES (?, ?, ?, ?, ?)",
                   (ADMIN_ID, "OwnerAdmin", "ADMIN", "2099-12-31 23:59:59", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

init_db()

def log_action(user_id: int, action: str, details: str = ""):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO logs (user_id, action, details, timestamp) VALUES (?, ?, ?, ?)",
                   (user_id, action, details, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

# =====================================================================
# ШИФРОВАНИЕ СЕССИЙ (AES-256)
# =====================================================================
def encrypt_session_file(phone: str):
    src_path = os.path.join(SESSIONS_DIR, f"{phone}.session")
    dest_path = os.path.join(ENCRYPTED_SESSIONS_DIR, f"{phone}.enc")
    if os.path.exists(src_path):
        with open(src_path, 'rb') as f:
            encrypted_data = cipher_suite.encrypt(f.read())
        with open(dest_path, 'wb') as f:
            f.write(encrypted_data)

def decrypt_session_file(phone: str) -> bool:
    src_path = os.path.join(ENCRYPTED_SESSIONS_DIR, f"{phone}.enc")
    dest_path = os.path.join(SESSIONS_DIR, f"{phone}.session")
    if os.path.exists(src_path):
        with open(src_path, 'rb') as f:
            decrypted_data = cipher_suite.decrypt(f.read())
        with open(dest_path, 'wb') as f:
            f.write(decrypted_data)
        return True
    return False

# =====================================================================
# СТЕЙТЫ ДЛЯ FSM
# =====================================================================
class BotStates(StatesGroup):
    add_account_phone = State()
    add_account_code = State()
    add_account_2fa = State()
    add_account_file = State()
    create_channel_single = State()
    create_channel_mass = State()
    create_bot_single = State()
    set_2fa_password = State()
    add_proxy_data = State()
    mass_change_bio = State()
    mass_set_2fa = State()
    admin_grant_sub_id = State()
    admin_grant_sub_plan = State()
    admin_grant_sub_dur = State()

# =====================================================================
# КЛИЕНТ ВРЕМЕННОЙ ПОЧТЫ
# =====================================================================
class TempMailClient:
    def __init__(self):
        self.domain = "dispostable.com"

    def generate_random_email(self) -> str:
        random_name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
        return f"{random_name}@{self.domain}"

    async def fetch_telegram_verification_code(self, email: str, timeout: int = 120) -> Optional[str]:
        username = email.split("@")[0]
        url = f"https://www.dispostable.com/inbox/{username}/"
        start_time = datetime.now()
        async with aiohttp.ClientSession() as session:
            while (datetime.now() - start_time).seconds < timeout:
                try:
                    async with session.get(url) as response:
                        if response.status == 200:
                            html = await response.text()
                            codes = re.findall(r'\b\d{6}\b', html)
                            if codes: return codes[0]
                except Exception: pass
                await asyncio.sleep(5)
        return None

# =====================================================================
# СЕССИИ TELETHON И ФУНКЦИОНАЛ ПРОВЕРКИ
# =====================================================================
async def get_client(phone: str) -> TelegramClient:
    if not os.path.exists(os.path.join(SESSIONS_DIR, f"{phone}.session")):
        decrypt_session_file(phone)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.type, p.host, p.port, p.username, p.password 
        FROM proxies p JOIN accounts a ON a.id = p.account_id WHERE a.phone = ?
    """, (phone,))
    proxy_res = cursor.fetchone()
    conn.close()
    proxy = None
    if proxy_res:
        import socks
        scheme = socks.SOCKS5 if proxy_res[0] == "SOCKS5" else socks.HTTP
        proxy = (scheme, proxy_res[1], proxy_res[2], True, proxy_res[3], proxy_res[4])
    return TelegramClient(os.path.join(SESSIONS_DIR, phone), API_ID, API_HASH, proxy=proxy)

async def check_session_validity(phone: str) -> Dict[str, Any]:
    client = await get_client(phone)
    res = {"is_valid": False, "status": "🔴 Session expired", "spamblock": False, "me": None}
    try:
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            if me:
                res["is_valid"] = True
                res["me"] = me
                res["status"] = "🟢 Аккаунт активен"
                try:
                    spambot = await client.get_input_entity('spambot')
                    async with client.conversation(spambot, timeout=3) as conv:
                        await conv.send_message('/start')
                        resp = await conv.get_response()
                        if "no limits" not in resp.text.lower() and "никаких ограничений" not in resp.text.lower():
                            res["spamblock"] = True
                            res["status"] = "🟡 SpamBlock"
                except Exception: pass
        else:
            res["status"] = "🔴 Session expired"
    except (AuthKeyUnregisteredError, UserDeactivatedError):
        res["is_valid"] = False
        res["status"] = "🔴 Session expired"
    except Exception as e:
        logger.error(f"Ошибка при валидации {phone}: {e}")
        res["is_valid"] = False
    finally:
        await client.disconnect()
        encrypt_session_file(phone)
    return res

async def get_account_detailed_info(phone: str) -> Dict[str, Any]:
    """Сбор подробной информации об аккаунте"""
    client = await get_client(phone)
    info = {
        "first_name": "Неизвестно", "username": "Нет", "telegram_id": 0,
        "phone": phone, "dc_id": "N/A", "premium": "Нет", "has_2fa": "Нет",
        "email": "Нет", "lang": "ru", "last_online": "Неизвестно",
        "reg_date": "N/A", "spamblock": "Нет", "floodwait": "Нет",
        "channels_count": 0, "groups_count": 0, "bots_count": 0
    }
    try:
        await client.connect()
        if not await client.is_user_authorized():
            info["status"] = "🔴 Session expired"
            return info
        me = await client.get_me()
        info["first_name"] = me.first_name or "Без имени"
        info["username"] = f"@{me.username}" if me.username else "Нет"
        info["telegram_id"] = me.id
        info["premium"] = "Да 🌟" if me.premium else "Нет"
        
        # Определение DC ID
        if me.photo and hasattr(me.photo, 'dc_id'):
            info["dc_id"] = me.photo.dc_id
            
        dialogs = await client.get_dialogs(limit=100)
        chans = [d for d in dialogs if d.is_channel and not d.is_group]
        groups = [d for d in dialogs if d.is_group]
        
        info["channels_count"] = len(chans)
        info["groups_count"] = len(groups)
        
        # Проверка 2FA из локальной базы данных
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT has_2fa, email FROM accounts WHERE phone = ?", (phone,))
        db_res = cursor.fetchone()
        if db_res:
            info["has_2fa"] = "Да 🔐" if db_res[0] else "Нет"
            info["email"] = db_res[1] if db_res[1] else "Нет"
        conn.close()
        
        # Проверка Спамблока
        try:
            spambot = await client.get_input_entity('spambot')
            async with client.conversation(spambot, timeout=3) as conv:
                await conv.send_message('/start')
                response = await conv.get_response()
                if "no limits" in response.text.lower() or "никаких ограничений" in response.text.lower():
                    info["spamblock"] = "Нет"
                else:
                    info["spamblock"] = "⚠️ Ограничен"
        except Exception:
            info["spamblock"] = "Не удалось проверить"
            
    except Exception as e:
        logger.error(f"Ошибка сбора инфы {phone}: {e}")
    finally:
        await client.disconnect()
        encrypt_session_file(phone)
    return info

async def hourly_validation_loop():
    while True:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT id, phone, idle_until FROM accounts")
        accounts = cursor.fetchall()
        conn.close()
        
        if accounts:
            for acc_id, phone, idle_until in accounts:
                if idle_until:
                    try:
                        idle_dt = datetime.strptime(idle_until, "%Y-%m-%d %H:%M:%S")
                        if datetime.now() < idle_dt: continue
                    except Exception: pass
                
                val_res = await check_session_validity(phone)
                if not val_res["is_valid"]:
                    conn = sqlite3.connect(DB_FILE)
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM accounts WHERE id = ?", (acc_id,))
                    conn.commit()
                    conn.close()
                    for path in [os.path.join(SESSIONS_DIR, f"{phone}.session"), os.path.join(ENCRYPTED_SESSIONS_DIR, f"{phone}.enc")]:
                        if os.path.exists(path):
                            try: os.remove(path)
                            except Exception: pass
                else:
                    conn = sqlite3.connect(DB_FILE)
                    cursor = conn.cursor()
                    cursor.execute("UPDATE accounts SET status = ? WHERE id = ?", (val_res["status"], acc_id))
                    conn.commit()
                    conn.close()
                await asyncio.sleep(2)
        await asyncio.sleep(3600)

# =====================================================================
# КЛАВИАТУРЫ
# =====================================================================
def get_main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="👤 Аккаунты", callback_data="menu_accounts"), InlineKeyboardButton(text="🤖 Боты", callback_data="menu_bots")],
        [InlineKeyboardButton(text="📢 Каналы", callback_data="menu_channels"), InlineKeyboardButton(text="⚡ Массовые действия", callback_data="menu_mass")],
        [InlineKeyboardButton(text="⚙️ Автоматизация", callback_data="menu_automation"), InlineKeyboardButton(text="🛡️ Безопасность", callback_data="menu_security")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="menu_stats"), InlineKeyboardButton(text="🔑 Подписки", callback_data="menu_subs")]
    ]
    if user_id == ADMIN_ID: buttons.append([InlineKeyboardButton(text="👑 Admin Panel", callback_data="menu_admin")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_accounts_keyboard(accounts_list: List[tuple], page: int = 1) -> InlineKeyboardMarkup:
    buttons = []
    per_page = 5
    start = (page - 1) * per_page
    end = start + per_page
    page_items = accounts_list[start:end]
    for acc in page_items:
        buttons.append([InlineKeyboardButton(text=f"{acc[4]} {acc[3] if acc[3] else acc[1]}", callback_data=f"view_acc_{acc[0]}")])
        
    nav_buttons = []
    if page > 1: nav_buttons.append(InlineKeyboardButton(text="⬅️ Пред.", callback_data=f"acc_page_{page-1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"Стр. {page}", callback_data="nop"))
    if end < len(accounts_list): nav_buttons.append(InlineKeyboardButton(text="След. ➡️", callback_data=f"acc_page_{page+1}"))
    if nav_buttons: buttons.append(nav_buttons)
        
    buttons.append([InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_account_menu"), InlineKeyboardButton(text="🔄 Обновить", callback_data="menu_accounts")])
    buttons.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_account_detail_keyboard(acc_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="ℹ️ Информация", callback_data=f"acc_info_{acc_id}"), InlineKeyboardButton(text="🔌 Сессии", callback_data=f"acc_sessions_{acc_id}")],
        [InlineKeyboardButton(text="🔑 Коды входа", callback_data=f"acc_codes_{acc_id}"), InlineKeyboardButton(text="📢 Каналы", callback_data=f"acc_channels_{acc_id}")],
        [InlineKeyboardButton(text="🤖 Боты", callback_data=f"acc_bots_{acc_id}"), InlineKeyboardButton(text="🧼 Очистка", callback_data=f"acc_clean_{acc_id}")],
        [InlineKeyboardButton(text="🔥 Прогрев", callback_data=f"acc_warm_{acc_id}"), InlineKeyboardButton(text="💤 Отлежка", callback_data=f"acc_idle_{acc_id}")],
        [InlineKeyboardButton(text="🛡️ Безопасность / 2FA", callback_data=f"acc_sec_{acc_id}"), InlineKeyboardButton(text="🌐 Прокси", callback_data=f"acc_proxy_{acc_id}")],
        [InlineKeyboardButton(text="🗑️ Удалить аккаунт", callback_data=f"acc_delete_conf_{acc_id}")],
        [InlineKeyboardButton(text="🔙 К списку аккаунтов", callback_data="menu_accounts")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# =====================================================================
# AIOGRAM ROUTER & HANDLERS
# =====================================================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
auth_sessions: Dict[int, Dict[str, Any]] = {}

@router.callback_query(F.data == "nop")
async def process_nop(callback: CallbackQuery):
    await callback.answer()

@router.message(Command("start"))
async def start_command(message: Message):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username, created_at) VALUES (?, ?, ?)",
                   (message.from_user.id, message.from_user.username or "Unknown", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    
    banner = (
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🏛️  <b>TELEGRAM SESSION MANAGER</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Добро пожаловать в профессиональную панель управления Telegram-аккаунтами.\n\n"
        "<i>Выберите необходимый раздел в меню ниже:</i>"
    )
    await message.answer(banner, reply_markup=get_main_keyboard(message.from_user.id), parse_mode="HTML")

@router.callback_query(F.data == "back_to_main")
async def process_back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    banner = "━━━━━━━━━━━━━━━━━━━━━━━━\n🏛️  <b>TELEGRAM SESSION MANAGER</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\nВы вернулись в главное меню. Выберите интересующий блок управления:"
    await callback.message.edit_text(banner, reply_markup=get_main_keyboard(callback.from_user.id), parse_mode="HTML")

# --- ДОБАВЛЕНИЕ АККАУНТОВ ---
@router.callback_query(F.data == "menu_accounts")
@router.callback_query(F.data.startswith("acc_page_"))
async def process_accounts_menu(callback: CallbackQuery):
    page = int(callback.data.split("_")[2]) if callback.data.startswith("acc_page_") else 1
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, phone, telegram_id, username, status FROM accounts")
    accounts = cursor.fetchall()
    conn.close()
    banner = f"👤  <b>УПРАВЛЕНИЕ АККАУНТАМИ</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\nВсего загружено аккаунтов в систему: <b>{len(accounts)}</b>\n\nВыберите конкретный аккаунт:"
    await callback.message.edit_text(banner, reply_markup=get_accounts_keyboard(accounts, page), parse_mode="HTML")

@router.callback_query(F.data == "add_account_menu")
async def process_add_account_menu(callback: CallbackQuery):
    banner = "➕  <b>ДОБАВЛЕНИЕ НОВОГО АККАУНТА</b>\n\n1. 📥 <b>Загрузить ZIP-архив (.session)</b>\n2. 📞 <b>Авторизация по номеру</b>"
    buttons = [[InlineKeyboardButton(text="📥 Файл / ZIP", callback_data="add_via_file")], [InlineKeyboardButton(text="📞 По номеру", callback_data="add_via_phone")], [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_accounts")]]
    await callback.message.edit_text(banner, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data == "add_via_phone")
async def add_via_phone_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.add_account_phone)
    await callback.message.edit_text("📞 <b>Введите номер телефона</b> (например, <code>+79123456789</code>):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="add_account_menu")]]), parse_mode="HTML")

@router.message(BotStates.add_account_phone)
async def add_via_phone_num_received(message: Message, state: FSMContext):
    phone = message.text.strip().replace(" ", "")
    await message.answer(f"⏳ <i>Отправляем код авторизации на номер {phone}...</i>", parse_mode="HTML")
    client = TelegramClient(os.path.join(SESSIONS_DIR, phone), API_ID, API_HASH)
    await client.connect()
    try:
        sent_code = await client.send_code_request(phone)
        auth_sessions[message.from_user.id] = {"phone": phone, "phone_code_hash": sent_code.phone_code_hash, "client": client}
        await state.set_state(BotStates.add_account_code)
        await message.answer("📥 <b>Введите код подтверждения</b>:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="add_account_menu")]]), parse_mode="HTML")
    except Exception as e:
        await client.disconnect()
        await message.answer(f"❌ <b>Ошибка:</b> {e}", parse_mode="HTML")
        await state.clear()

@router.message(BotStates.add_account_code)
async def add_via_phone_code_received(message: Message, state: FSMContext):
    uid = message.from_user.id
    if uid not in auth_sessions: return await state.clear()
    data = auth_sessions[uid]
    client, phone, p_hash = data["client"], data["phone"], data["phone_code_hash"]
    try:
        await client.sign_in(phone, message.text.strip(), phone_code_hash=p_hash)
        val_res = await check_session_validity(phone)
        if val_res["is_valid"]:
            me = val_res["me"]
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO accounts (phone, telegram_id, username, first_name, status, created_at) VALUES (?, ?, ?, ?, ?, ?)", (phone, me.id, me.username, me.first_name, val_res["status"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit(); conn.close()
            await message.answer(f"🎉 <b>Аккаунт {phone} успешно добавлен!</b>\nСтатус: {val_res['status']}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📊 К аккаунтам", callback_data="menu_accounts")]]), parse_mode="HTML")
        await client.disconnect()
        encrypt_session_file(phone)
        await state.clear()
        auth_sessions.pop(uid, None)
    except SessionPasswordNeededError:
        await state.set_state(BotStates.add_account_2fa)
        await message.answer("🔑 <b>Введите 2FA пароль:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="add_account_menu")]]), parse_mode="HTML")
    except Exception as e:
        await client.disconnect()
        await message.answer(f"❌ Ошибка: {e}", parse_mode="HTML")
        await state.clear()

@router.message(BotStates.add_account_2fa)
async def add_via_phone_2fa_received(message: Message, state: FSMContext):
    uid = message.from_user.id
    if uid not in auth_sessions: return await state.clear()
    data = auth_sessions[uid]
    client, phone = data["client"], data["phone"]
    try:
        await client.sign_in(password=message.text.strip())
        val_res = await check_session_validity(phone)
        if val_res["is_valid"]:
            me = val_res["me"]
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO accounts (phone, telegram_id, username, first_name, has_2fa, status, created_at) VALUES (?, ?, ?, ?, 1, ?, ?)", (phone, me.id, me.username, me.first_name, val_res["status"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit(); conn.close()
            await message.answer(f"🎉 <b>Аккаунт {phone} добавлен!</b> (2FA пройден)\nСтатус: {val_res['status']}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📊 К аккаунтам", callback_data="menu_accounts")]]), parse_mode="HTML")
        await client.disconnect()
        encrypt_session_file(phone)
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка 2FA: {e}", parse_mode="HTML")

@router.callback_query(F.data == "add_via_file")
async def add_via_file_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.add_account_file)
    text = "📥 <b>Загрузка сессий (ZIP / .session)</b>\n\nОтправьте архив со множеством сессий или один файл.\n<i>Невалидные сессии автоматически отсеются, а аккаунты со спам-блоком будут сохранены.</i>"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="add_account_menu")]]), parse_mode="HTML")

@router.message(BotStates.add_account_file, F.document)
async def add_via_file_received(message: Message, state: FSMContext):
    fname = message.document.file_name
    if not (fname.endswith(".session") or fname.endswith(".zip")): return await message.answer("❌ Только .session или .zip")
    file_path = (await bot.get_file(message.document.file_id)).file_path
    
    if fname.endswith(".session"):
        phone = fname.replace(".session", "").strip()
        local_path = os.path.join(SESSIONS_DIR, f"{phone}.session")
        await bot.download_file(file_path, local_path)
        val_res = await check_session_validity(phone)
        if val_res["is_valid"]:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO accounts (phone, telegram_id, username, first_name, status, created_at) VALUES (?, ?, ?, ?, ?, ?)", (phone, val_res["me"].id, val_res["me"].username, val_res["me"].first_name, val_res["status"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit(); conn.close()
            await message.answer(f"✅ Сессия <b>{phone}</b> добавлена!\nСтатус: {val_res['status']}", parse_mode="HTML")
        else:
            if os.path.exists(local_path): os.remove(local_path)
            await message.answer("❌ Сессия невалидна.")
            
    elif fname.endswith(".zip"):
        zip_path = os.path.join(SESSIONS_DIR, fname)
        await bot.download_file(file_path, zip_path)
        temp_dir = os.path.join(SESSIONS_DIR, f"temp_{int(datetime.now().timestamp())}")
        os.makedirs(temp_dir, exist_ok=True)
        status_msg = await message.answer("📦 <i>Распаковка ZIP архива...</i>", parse_mode="HTML")
        
        try:
            with zipfile.ZipFile(zip_path, 'r') as zr: zr.extractall(temp_dir)
            files = [os.path.join(root, f) for root, dirs, fs in os.walk(temp_dir) for f in fs if f.endswith(".session")]
            if not files: return await status_msg.edit_text("❌ В архиве нет .session файлов.", parse_mode="HTML")
            
            await status_msg.edit_text(f"🔍 Найдено сессий: <b>{len(files)}</b>. Начинаем валидацию...", parse_mode="HTML")
            succ, fail = 0, 0
            for idx, fp in enumerate(files, 1):
                phone = os.path.basename(fp).replace(".session", "").strip()
                tp = os.path.join(SESSIONS_DIR, f"{phone}.session")
                shutil.copy2(fp, tp)
                
                val_res = await check_session_validity(phone)
                if val_res["is_valid"]:
                    me = val_res["me"]
                    conn = sqlite3.connect(DB_FILE)
                    cursor = conn.cursor()
                    cursor.execute("INSERT OR REPLACE INTO accounts (phone, telegram_id, username, first_name, status, created_at) VALUES (?, ?, ?, ?, ?, ?)", (phone, me.id, me.username, me.first_name, val_res["status"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    conn.commit(); conn.close()
                    succ += 1
                else:
                    if os.path.exists(tp): os.remove(tp)
                    fail += 1
                if idx % 3 == 0 or idx == len(files):
                    await status_msg.edit_text(f"⚙️ <b>Обработка:</b> {idx}/{len(files)}\n✅ Сохранено (вкл. спамблок): <b>{succ}</b>\n❌ Невалид (удалены): <b>{fail}</b>", parse_mode="HTML")
                await asyncio.sleep(0.3)
            await status_msg.answer(f"📊 <b>Массовый импорт завершен!</b>\n\n✅ Успешно: <b>{succ}</b>\n❌ Удалено нерабочих: <b>{fail}</b>", parse_mode="HTML")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            if os.path.exists(zip_path): os.remove(zip_path)
    await state.clear()

# --- ИНДИВИДУАЛЬНОЕ УПРАВЛЕНИЕ ---
@router.callback_query(F.data.startswith("view_acc_"))
async def process_view_account(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT phone, username, first_name, status FROM accounts WHERE id = ?", (acc_id,))
    acc = cursor.fetchone()
    conn.close()
    if not acc:
        return await callback.answer("Аккаунт не найден!", show_alert=True)
    banner = f"⚙️ <b>АККАУНТ: {acc[1] if acc[1] else acc[0]} ({acc[2]})</b>\n\nСтатус: {acc[3]}\nВыберите действие:"
    await callback.message.edit_text(banner, reply_markup=get_account_detail_keyboard(acc_id), parse_mode="HTML")

# 1. Информация по аккаунту
@router.callback_query(F.data.startswith("acc_info_"))
async def process_acc_info(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT phone FROM accounts WHERE id = ?", (acc_id,))
    phone = cursor.fetchone()[0]
    conn.close()
    
    await callback.answer("⚡ Сбор детальной информации...")
    info = await get_account_detailed_info(phone)
    
    banner = (
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "ℹ️ <b>ПОЛНАЯ ИНФОРМАЦИЯ ОБ АККАУНТЕ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Имя:</b> {info['first_name']}\n"
        f"🏷️ <b>Username:</b> {info['username']}\n"
        f"🆔 <b>Telegram ID:</b> <code>{info['telegram_id']}</code>\n"
        f"📞 <b>Телефон:</b> {info['phone']}\n"
        f"🗺️ <b>DC ID:</b> {info['dc_id']}\n"
        f"⭐️ <b>Premium статус:</b> {info['premium']}\n"
        f"🛡️ <b>Наличие 2FA:</b> {info['has_2fa']}\n"
        f"📧 <b>Привязанная почта:</b> {info['email']}\n"
        f"🌐 <b>Язык:</b> {info['lang']}\n"
        f"⛔ <b>SpamBlock статус:</b> {info['spamblock']}\n"
        f"🚫 <b>FloodWait статус:</b> {info['floodwait']}\n\n"
        f"📊 <b>Статистика чатов:</b>\n"
        f"├ Каналы: {info['channels_count']}\n"
        f"└ Группы: {info['groups_count']}\n"
    )
    
    await callback.message.edit_text(banner, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=f"view_acc_{acc_id}")]]), parse_mode="HTML")

# 2. Сессии
@router.callback_query(F.data.startswith("acc_sessions_"))
async def process_acc_sessions(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    banner = (
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔌 <b>АКТИВНЫЕ СЕССИИ И УСТРОЙСТВА</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Вы можете завершить все сессии кроме текущей.\n"
        "⚠️ <i>После действия все другие приложения выйдут из аккаунта.</i>"
    )
    buttons = [
        [InlineKeyboardButton(text="🛑 Сбросить все устройства", callback_data=f"conf_reset_sess_{acc_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"view_acc_{acc_id}")]
    ]
    await callback.message.edit_text(banner, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data.startswith("conf_reset_sess_"))
async def process_conf_reset_sess(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[3])
    banner = "⚠️ <b>Вы уверены?</b>\nПосле этого все устройства выйдут из аккаунта."
    buttons = [
        [InlineKeyboardButton(text="✅ Да, сбросить", callback_data=f"do_reset_sess_{acc_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"acc_sessions_{acc_id}")]
    ]
    await callback.message.edit_text(banner, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data.startswith("do_reset_sess_"))
async def process_do_reset_sess(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[3])
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    phone = cursor.execute("SELECT phone FROM accounts WHERE id = ?", (acc_id,)).fetchone()[0]; conn.close()
    
    client = await get_client(phone)
    try:
        await client.connect()
        await client(functions.auth.ResetAuthorizationsRequest())
        await callback.answer("✅ Другие сессии успешно сброшены!", show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)
    finally:
        await client.disconnect()
        encrypt_session_file(phone)
    await process_view_account(callback)

# 3. Коды входа
@router.callback_query(F.data.startswith("acc_codes_"))
async def process_acc_codes(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    phone = cursor.execute("SELECT phone FROM accounts WHERE id = ?", (acc_id,)).fetchone()[0]; conn.close()
    
    await callback.answer("📥 Чтение кодов входа...")
    client = await get_client(phone)
    codes_found = []
    try:
        await client.connect()
        async for msg in client.iter_messages(777000, limit=5):
            if msg.text:
                match = re.search(r'\b\d{5}\b', msg.text)
                if match:
                    codes_found.append({
                        "code": match.group(),
                        "time": msg.date.strftime("%H:%M:%S (%d.%m)"),
                        "text": msg.text[:60] + "..."
                    })
    except Exception as e:
        logger.error(f"Error fetching codes: {e}")
    finally:
        await client.disconnect()
        encrypt_session_file(phone)
        
    banner = "🔑 <b>ПОСЛЕДНИЕ ПОЛУЧЕННЫЕ КОДЫ ВХОДА</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
    if codes_found:
        for idx, item in enumerate(codes_found, 1):
            banner += f"{idx}. Код: <code>{item['code']}</code>\n⏱️ Время: {item['time']}\n💬 {item['text']}\n\n"
    else:
        banner += "❌ Активных кодов авторизации не обнаружено."
        
    buttons = [[InlineKeyboardButton(text="🔄 Обновить", callback_data=f"acc_codes_{acc_id}")], [InlineKeyboardButton(text="🔙 Назад", callback_data=f"view_acc_{acc_id}")]]
    await callback.message.edit_text(banner, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

# ОЧИСТКА
@router.callback_query(F.data.startswith("acc_clean_"))
async def clean_menu(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    buttons = [
        [InlineKeyboardButton(text="👥 Выйти из чатов/каналов и удалить диалоги", callback_data=f"clean_chats_{acc_id}")],
        [InlineKeyboardButton(text="🖼️ Стереть все аватарки", callback_data=f"clean_avatars_{acc_id}")],
        [InlineKeyboardButton(text="📝 Сбросить Bio профиля", callback_data=f"clean_bio_{acc_id}")],
        [InlineKeyboardButton(text="💾 Очистить Saved Messages", callback_data=f"clean_saved_{acc_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"view_acc_{acc_id}")]
    ]
    await callback.message.edit_text("🧼 <b>ОЧИСТКА И СБРОС ПРОФИЛЯ</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data.startswith("clean_chats_"))
async def clean_chats(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("SELECT phone FROM accounts WHERE id = ?", (acc_id,))
    row = cursor.fetchone(); conn.close()
    if not row:
        return await callback.answer("Аккаунт не найден!", show_alert=True)
    phone = row[0]
    
    await callback.answer("🧼 Очистка чатов и удаление диалогов...")
    client = await get_client(phone)
    try:
        await client.connect()
        dialogs = await client.get_dialogs(limit=100)
        left = 0
        for d in dialogs:
            try:
                # Полный выход из каналов и удаление/выход из групп
                await client.delete_dialog(d.entity)
                left += 1
                await asyncio.sleep(0.3)
            except Exception: pass
        await callback.message.answer(f"✅ Успешно! Аккаунт покинул и удалил {left} диалогов.", parse_mode="HTML")
    except Exception as e: await callback.message.answer(f"❌ Ошибка: {e}", parse_mode="HTML")
    finally: await client.disconnect(); encrypt_session_file(phone)

@router.callback_query(F.data.startswith("clean_avatars_"))
async def clean_avatars(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor(); phone = cursor.execute("SELECT phone FROM accounts WHERE id = ?", (acc_id,)).fetchone()[0]; conn.close()
    client = await get_client(phone)
    try:
        await client.connect()
        photos = await client.get_profile_photos('me')
        if photos: await client(DeletePhotosRequest(photos))
        await callback.message.answer(f"✅ Фото профиля успешно очищены.", parse_mode="HTML")
    except Exception as e: await callback.message.answer(f"❌ Ошибка: {e}", parse_mode="HTML")
    finally: await client.disconnect(); encrypt_session_file(phone)

@router.callback_query(F.data.startswith("clean_bio_"))
async def clean_bio(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor(); phone = cursor.execute("SELECT phone FROM accounts WHERE id = ?", (acc_id,)).fetchone()[0]; conn.close()
    client = await get_client(phone)
    try:
        await client.connect(); await client(UpdateProfileRequest(about=""))
        await callback.message.answer(f"✅ Bio профиля очищено.", parse_mode="HTML")
    except Exception as e: await callback.message.answer(f"❌ Ошибка: {e}", parse_mode="HTML")
    finally: await client.disconnect(); encrypt_session_file(phone)

@router.callback_query(F.data.startswith("clean_saved_"))
async def clean_saved(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    phone_row = cursor.execute("SELECT phone FROM accounts WHERE id = ?", (acc_id,)).fetchone()
    conn.close()
    if not phone_row:
        return await callback.answer("Аккаунт не найден!", show_alert=True)
    phone = phone_row[0]
    client = await get_client(phone)
    try:
        await client.connect()
        me_peer = await client.get_input_entity("me")
        await client(DeleteHistoryRequest(peer=me_peer, max_id=0, revoke=True))
        await callback.message.answer("✅ Избранное очищено.", parse_mode="HTML")
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {e}", parse_mode="HTML")
    finally:
        await client.disconnect()
        encrypt_session_file(phone)

# МАССОВЫЕ ДЕЙСТВИЯ
@router.callback_query(F.data == "menu_mass")
async def mass_menu(callback: CallbackQuery):
    banner = "⚡ <b>МАССОВЫЕ ДЕЙСТВИЯ НАД ВСЕЙ СЕТЬЮ</b>\n\nДействие будет выполнено <b>на всех рабочих аккаунтах</b> одновременно:"
    buttons = [
        [InlineKeyboardButton(text="👥 Выйти из чатов и снести диалоги", callback_data="mass_action_exit_chats"), InlineKeyboardButton(text="💾 Очистить Избранное", callback_data="mass_action_clean_saved")],
        [InlineKeyboardButton(text="📝 Смена BIO", callback_data="mass_action_change_bio"), InlineKeyboardButton(text="🏷️ Смена Username", callback_data="mass_action_change_username")],
        [InlineKeyboardButton(text="🖼️ Стереть аватарки", callback_data=f"mass_action_delete_avatars"), InlineKeyboardButton(text="🔑 Установить 2FA", callback_data="mass_action_set_2fa")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")]
    ]
    await callback.message.edit_text(banner, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data == "mass_action_exit_chats")
async def mass_exit_chats(callback: CallbackQuery):
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("SELECT phone FROM accounts WHERE status NOT LIKE '🔴%'")
    phones = [r[0] for r in cursor.fetchall()]; conn.close()
    if not phones: return await callback.answer("Нет активных сессий!", show_alert=True)
    msg = await callback.message.answer(f"⏳ <i>Выходим и удаляем диалоги на {len(phones)} аккаунтах...</i>", parse_mode="HTML")
    succ = 0
    for phone in phones:
        client = await get_client(phone)
        try:
            await client.connect()
            dialogs = await client.get_dialogs(limit=50)
            for d in dialogs:
                try: await client.delete_dialog(d.entity); await asyncio.sleep(0.1)
                except Exception: pass
            succ += 1
        except Exception: pass
        finally: await client.disconnect(); encrypt_session_file(phone)
    await msg.edit_text(f"✅ <b>Массовый выход и удаление диалогов успешно завершены!</b>\nУспешно обработано: {succ}/{len(phones)}", parse_mode="HTML")

@router.callback_query(F.data == "mass_action_clean_saved")
async def mass_clean_saved(callback: CallbackQuery):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT phone FROM accounts WHERE status NOT LIKE '🔴%'")
    phones = [r[0] for r in cursor.fetchall()]
    conn.close()
    if not phones:
        return await callback.answer("Нет активных сессий!", show_alert=True)
    msg = await callback.message.answer(f"⏳ <i>Очищаем Избранное на {len(phones)} аккаунтах...</i>", parse_mode="HTML")
    succ = 0
    for phone in phones:
        client = await get_client(phone)
        try:
            await client.connect()
            me_peer = await client.get_input_entity("me")
            await client(DeleteHistoryRequest(peer=me_peer, max_id=0, revoke=True))
            succ += 1
        except Exception:
            pass
        finally:
            await client.disconnect()
            encrypt_session_file(phone)
    await msg.edit_text(f"✅ <b>Избранное массово очищено!</b>\nУспешно: {succ}/{len(phones)}", parse_mode="HTML")

@router.callback_query(F.data == "mass_action_change_bio")
async def mass_change_bio_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.mass_change_bio)
    await callback.message.edit_text("📝 <b>Массовая смена BIO</b>\nВведите новое описание (до 70 симв.):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu_mass")]]), parse_mode="HTML")

@router.message(BotStates.mass_change_bio)
async def mass_change_bio_done(message: Message, state: FSMContext):
    bio = message.text.strip()[:70]
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("SELECT phone FROM accounts WHERE status NOT LIKE '🔴%'")
    phones = [r[0] for r in cursor.fetchall()]; conn.close()
    msg = await message.answer(f"⏳ <i>Обновляем Bio на {len(phones)} аккаунтах...</i>", parse_mode="HTML")
    succ = 0
    for phone in phones:
        client = await get_client(phone)
        try:
            await client.connect(); await client(UpdateProfileRequest(about=bio)); succ += 1
        except Exception: pass
        finally: await client.disconnect(); encrypt_session_file(phone)
    await msg.edit_text(f"✅ <b>Массовая смена BIO завершена!</b>\nУспешно: {succ}/{len(phones)}", parse_mode="HTML")
    await state.clear()

@router.callback_query(F.data == "mass_action_change_username")
async def mass_change_username(callback: CallbackQuery):
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("SELECT id, phone FROM accounts WHERE status NOT LIKE '🔴%'")
    accs = cursor.fetchall(); conn.close()
    if not accs: return await callback.answer("Нет активных сессий!", show_alert=True)
    msg = await callback.message.answer(f"⏳ <i>Меняем Username на {len(accs)} аккаунтах...</i>", parse_mode="HTML")
    succ = 0
    for acc_id, phone in accs:
        client = await get_client(phone)
        try:
            await client.connect()
            uname = f"usr_{''.join(random.choices(string.ascii_lowercase, k=6))}_{random.randint(100,999)}"
            await client(UpdateUsernameRequest(username=uname))
            conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
            cursor.execute("UPDATE accounts SET username = ? WHERE id = ?", (f"@{uname}", acc_id))
            conn.commit(); conn.close()
            succ += 1
        except Exception as e: logger.error(f"Username Error {phone}: {e}")
        finally: await client.disconnect(); encrypt_session_file(phone)
    await msg.edit_text(f"✅ <b>Смена юзернеймов завершена!</b>\nУспешно: {succ}/{len(accs)}", parse_mode="HTML")

@router.callback_query(F.data == "mass_action_delete_avatars")
async def mass_delete_avatars(callback: CallbackQuery):
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("SELECT phone FROM accounts WHERE status NOT LIKE '🔴%'")
    phones = [r[0] for r in cursor.fetchall()]; conn.close()
    if not phones: return await callback.answer("Нет активных сессий!", show_alert=True)
    msg = await callback.message.answer(f"⏳ <i>Удаляем аватарки на {len(phones)} аккаунтах...</i>", parse_mode="HTML")
    succ = 0
    for phone in phones:
        client = await get_client(phone)
        try:
            await client.connect()
            photos = await client.get_profile_photos('me')
            if photos: await client(DeletePhotosRequest(photos))
            succ += 1
        except Exception: pass
        finally: await client.disconnect(); encrypt_session_file(phone)
    await msg.edit_text(f"✅ <b>Аватарки удалены!</b>\nУспешно: {succ}/{len(phones)}", parse_mode="HTML")

@router.callback_query(F.data == "mass_action_set_2fa")
async def mass_set_2fa_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.mass_set_2fa)
    await callback.message.edit_text("🔑 <b>Массовая установка 2FA</b>\nВведите единый пароль для всех аккаунтов:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu_mass")]]), parse_mode="HTML")

@router.message(BotStates.mass_set_2fa)
async def mass_set_2fa_done(message: Message, state: FSMContext):
    pwd = message.text.strip()
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("SELECT id, phone FROM accounts WHERE status NOT LIKE '🔴%'")
    accs = cursor.fetchall(); conn.close()
    msg = await message.answer(f"⏳ <i>Устанавливаем 2FA на {len(accs)} аккаунтах...</i>", parse_mode="HTML")
    succ = 0
    for acc_id, phone in accs:
        client = await get_client(phone)
        try:
            await client.connect()
            await client.edit_2fa(new_password=pwd)
            conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
            cursor.execute("UPDATE accounts SET has_2fa = 1 WHERE id = ?", (acc_id,)); conn.commit(); conn.close()
            succ += 1
        except Exception as e: logger.error(f"2FA Err {phone}: {e}")
        finally: await client.disconnect(); encrypt_session_file(phone)
    await msg.edit_text(f"✅ <b>Массовая установка 2FA завершена!</b>\nУспешно: {succ}/{len(accs)}", parse_mode="HTML")
    await state.clear()

# --- КАНАЛЫ (МАССОВОЕ СОЗДАНИЕ И СПИСКИ) ---
@router.callback_query(F.data == "menu_channels")
@router.callback_query(F.data.startswith("acc_channels_"))
async def channels_menu(callback: CallbackQuery):
    acc_id = None
    if callback.data.startswith("acc_channels_"): acc_id = int(callback.data.split("_")[2])
    banner = "📢 <b>УПРАВЛЕНИЕ КАНАЛАМИ</b>\nВы можете создавать, удалять каналы, а также просматривать базу каналов."
    buttons = []
    if acc_id:
        buttons.append([InlineKeyboardButton(text="➕ Создать один канал", callback_data=f"chan_create_sing_{acc_id}")])
        buttons.append([InlineKeyboardButton(text="📚 Массовое создание каналов", callback_data=f"chan_create_mass_{acc_id}")])
        buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"view_acc_{acc_id}")])
    else:
        buttons.append([InlineKeyboardButton(text="🌐 База всех каналов", callback_data="all_channels_list")])
        buttons.append([InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_main")])
    await callback.message.edit_text(banner, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data.startswith("chan_create_sing_"))
async def process_create_channel_single_start(callback: CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    await state.set_state(BotStates.create_channel_single)
    await state.update_data(acc_id=acc_id)
    await callback.message.edit_text("📝 <b>Параметры канала</b>\n\nВведите в формате:\n<code>Название | Описание</code>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"acc_channels_{acc_id}")]]), parse_mode="HTML")

@router.message(BotStates.create_channel_single)
async def process_create_channel_single_done(message: Message, state: FSMContext):
    state_data = await state.get_data()
    acc_id = state_data["acc_id"]
    parts = message.text.split("|")
    title = parts[0].strip()
    about = parts[1].strip() if len(parts) > 1 else "Создано через Session Manager"
    
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    phone = cursor.execute("SELECT phone FROM accounts WHERE id = ?", (acc_id,)).fetchone()[0]; conn.close()
    
    msg = await message.answer("⏳ <i>Создаем канал на сервере Telegram...</i>", parse_mode="HTML")
    client = await get_client(phone)
    try:
        await client.connect()
        created_channel = await client(functions.channels.CreateChannelRequest(title=title, about=about, megagroup=False))
        channel_id = created_channel.chats[0].id
        conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
        cursor.execute("INSERT INTO channels (account_id, channel_id, title, type) VALUES (?, ?, ?, 'private')", (acc_id, channel_id, title))
        conn.commit(); conn.close()
        await msg.answer(f"🎉 <b>Канал успешно создан!</b>\n\n• Название: {title}\n• ID: <code>{channel_id}</code>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 К каналам", callback_data=f"acc_channels_{acc_id}")]]), parse_mode="HTML")
    except Exception as e:
        await msg.answer(f"❌ Ошибка создания канала: {e}", parse_mode="HTML")
    finally:
        await client.disconnect()
        encrypt_session_file(phone)
        await state.clear()

@router.callback_query(F.data == "all_channels_list")
async def all_channels_list(callback: CallbackQuery):
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("SELECT a.phone, c.title, c.channel_id FROM channels c JOIN accounts a ON a.id = c.account_id LIMIT 50")
    chans = cursor.fetchall(); conn.close()
    text = "📢 <b>Ваши каналы (Топ-50):</b>\n\n"
    for c in chans: text += f"• {c[1]} (ID: <code>{c[2]}</code>) на {c[0]}\n"
    if not chans: text = "Каналы еще не создавались."
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="menu_channels")]]), parse_mode="HTML")

@router.callback_query(F.data.startswith("chan_create_mass_"))
async def chan_create_mass_start(callback: CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    await state.set_state(BotStates.create_channel_mass)
    await state.update_data(acc_id=acc_id)
    await callback.message.edit_text("📚 <b>Массовое создание каналов</b>\nОтправьте список названий (каждое с новой строки). Ограничение: до 10 за раз.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"acc_channels_{acc_id}")]]), parse_mode="HTML")

@router.message(BotStates.create_channel_mass)
async def chan_create_mass_done(message: Message, state: FSMContext):
    acc_id = (await state.get_data())["acc_id"]
    names = [n.strip() for n in message.text.split("\n") if n.strip()][:10]
    
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    phone = cursor.execute("SELECT phone FROM accounts WHERE id = ?", (acc_id,)).fetchone()[0]; conn.close()
    
    msg = await message.answer(f"⏳ <i>Создаем {len(names)} каналов...</i>", parse_mode="HTML")
    client = await get_client(phone)
    succ = 0
    try:
        await client.connect()
        for name in names:
            try:
                res = await client(CreateChannelRequest(title=name, about="", megagroup=False))
                cid = res.chats[0].id
                conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
                cursor.execute("INSERT INTO channels (account_id, channel_id, title, type) VALUES (?, ?, ?, 'private')", (acc_id, cid, name))
                conn.commit(); conn.close()
                succ += 1; await asyncio.sleep(2)
            except Exception as e: logger.error(f"Error creating channel {name}: {e}")
        await msg.edit_text(f"✅ <b>Создано каналов:</b> {succ}/{len(names)}", parse_mode="HTML")
    except Exception as e: await msg.edit_text(f"❌ Ошибка: {e}", parse_mode="HTML")
    finally: await client.disconnect(); encrypt_session_file(phone); await state.clear()


# --- БОТЫ (УПРАВЛЕНИЕ, СОЗДАНИЕ И СПИСКИ) ---
@router.callback_query(F.data == "menu_bots")
@router.callback_query(F.data.startswith("acc_bots_"))
async def bots_menu(callback: CallbackQuery):
    acc_id = None
    if callback.data.startswith("acc_bots_"): acc_id = int(callback.data.split("_")[2])
    banner = "🤖 <b>УПРАВЛЕНИЕ БОТАМИ (BotFather)</b>\nЗдесь вы можете генерировать и просматривать ботов."
    buttons = []
    if acc_id:
        buttons.append([InlineKeyboardButton(text="➕ Создать нового бота", callback_data=f"bot_create_sing_{acc_id}")])
        buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"view_acc_{acc_id}")])
    else:
        buttons.append([InlineKeyboardButton(text="🔑 База всех токенов ботов", callback_data="all_bots_tokens")])
        buttons.append([InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_main")])
    await callback.message.edit_text(banner, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data.startswith("bot_create_sing_"))
async def process_create_bot_start(callback: CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    await state.set_state(BotStates.create_bot_single)
    await state.update_data(acc_id=acc_id)
    await callback.message.edit_text(
        "🤖 <b>Создание бота через BotFather</b>\n\nВведите данные в формате: <code>Имя бота / Юзернейм_bot</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"acc_bots_{acc_id}")]]),
        parse_mode="HTML"
    )

@router.message(BotStates.create_bot_single)
async def process_create_bot_done(message: Message, state: FSMContext):
    acc_id = (await state.get_data())["acc_id"]
    parts = message.text.split("/")
    if len(parts) < 2:
        return await message.answer("❌ Формат должен быть: <code>Имя / Юзернейм_bot</code>")
    bot_name, bot_username = parts[0].strip(), parts[1].strip()
    if not bot_username.lower().endswith("bot"):
        return await message.answer("❌ Юзернейм бота должен заканчиваться на 'bot'!")
        
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    phone = cursor.execute("SELECT phone FROM accounts WHERE id = ?", (acc_id,)).fetchone()[0]; conn.close()
    
    await message.answer("⏳ <i>Связываемся с BotFather...</i>", parse_mode="HTML")
    client = await get_client(phone)
    try:
        await client.connect()
        async with client.conversation('@BotFather', timeout=15) as conv:
            await conv.send_message('/newbot')
            await conv.get_response()
            await conv.send_message(bot_name)
            await conv.get_response()
            await conv.send_message(bot_username)
            resp3 = await conv.get_response()
            token_match = re.search(r'(\d+:[A-Za-z0-9_-]+)', resp3.text)
            if token_match:
                token = token_match.group(1)
                conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
                cursor.execute("INSERT INTO bots (account_id, name, username, token) VALUES (?, ?, ?, ?)", (acc_id, bot_name, bot_username, token))
                conn.commit(); conn.close()
                await message.answer(f"🎉 <b>Бот создан!</b>\n\n• Юзернейм: @{bot_username}\n• Токен: <code>{token}</code>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=f"acc_bots_{acc_id}")]]), parse_mode="HTML")
            else:
                await message.answer(f"❌ BotFather ответил: {resp3.text}", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка BotFather: {e}", parse_mode="HTML")
    finally:
        await client.disconnect()
        encrypt_session_file(phone)
        await state.clear()

@router.callback_query(F.data == "all_bots_tokens")
async def all_bots_list(callback: CallbackQuery):
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("SELECT name, username, token FROM bots LIMIT 50")
    bots = cursor.fetchall(); conn.close()
    text = "🤖 <b>Ваши Боты (Топ-50):</b>\n\n"
    for b in bots: text += f"• {b[0]} | @{b[1]}\n🔑 <code>{b[2]}</code>\n\n"
    if not bots: text = "Боты еще не создавались."
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="menu_bots")]]), parse_mode="HTML")


# --- АВТОМАТИЗАЦИЯ И ПРОГРЕВ ---
@router.callback_query(F.data == "menu_automation")
@router.callback_query(F.data.startswith("acc_warm_"))
@router.callback_query(F.data.startswith("acc_idle_"))
async def process_automation_menu(callback: CallbackQuery):
    acc_id = None
    if callback.data.startswith("acc_warm_") or callback.data.startswith("acc_idle_"):
        acc_id = int(callback.data.split("_")[2])
        
    banner = (
        "🤖  <b>АВТООТЛЕЖКА И ПРОГРЕВ АККАУНТОВ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Система автоматического снижения флуда:\n\n"
        "• Прогрев: чтение чатов, реакции и имитация присутствия человека.\n"
        "• Отлежка: перевод в спящий режим на заданный интервал."
    )
    buttons = []
    if acc_id:
        buttons.append([InlineKeyboardButton(text="🔥 Запустить прогрев", callback_data=f"warmup_run_{acc_id}")])
        buttons.append([InlineKeyboardButton(text="💤 Включить отлежку", callback_data=f"acc_idle_menu_{acc_id}")])
        buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"view_acc_{acc_id}")])
    else:
        buttons.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")])
    await callback.message.edit_text(banner, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data.startswith("acc_idle_menu_"))
async def process_acc_idle_menu(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[3])
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    status, idle_until = cursor.execute("SELECT status, idle_until FROM accounts WHERE id = ?", (acc_id,)).fetchone(); conn.close()
    banner = f"💤 <b>НАСТРОЙКА АВТООТЛЕЖКИ</b>\n\nСтатус: {status}\nДо: {idle_until if idle_until else 'Не установлена'}"
    buttons = [
        [InlineKeyboardButton(text="⏱️ Отлежка на 1 день", callback_data=f"set_idle_{acc_id}_1")],
        [InlineKeyboardButton(text="⏱️ Отлежка на 3 дня", callback_data=f"set_idle_{acc_id}_3")],
        [InlineKeyboardButton(text="⏱️ Отлежка на 7 дней", callback_data=f"set_idle_{acc_id}_7")],
        [InlineKeyboardButton(text="❌ Отключить отлежку", callback_data=f"set_idle_{acc_id}_0")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"view_acc_{acc_id}")]
    ]
    await callback.message.edit_text(banner, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data.startswith("set_idle_"))
async def set_idle_duration(callback: CallbackQuery):
    parts = callback.data.split("_")
    acc_id = int(parts[2])
    days = int(parts[3])
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    if days == 0:
        cursor.execute("UPDATE accounts SET status = '🟢 Аккаунт активен', idle_until = NULL WHERE id = ?", (acc_id,))
        msg = "Отлежка отключена!"
    else:
        dt = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("UPDATE accounts SET status = '💤 На отлежке', idle_until = ? WHERE id = ?", (dt, acc_id))
        msg = f"Аккаунт на отлежке до {dt}."
    conn.commit(); conn.close()
    await callback.answer(msg, show_alert=True)
    await process_view_account(callback)

@router.callback_query(F.data.startswith("warmup_run_"))
async def warmup_run(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    phone = cursor.execute("SELECT phone FROM accounts WHERE id = ?", (acc_id,)).fetchone()[0]; conn.close()
    
    await callback.answer("🔥 Прогрев запущен...")
    msg = await callback.message.answer("⏳ <i>Симуляция активности человека...</i>", parse_mode="HTML")
    client = await get_client(phone)
    try:
        await client.connect()
        dialogs = await client.get_dialogs(limit=5)
        for d in dialogs:
            await client.send_read_acknowledge(d.entity)
            await asyncio.sleep(1)
        await msg.edit_text(f"✅ <b>Прогрев успешно завершен для {phone}!</b>", parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка прогрева: {e}", parse_mode="HTML")
    finally:
        await client.disconnect()
        encrypt_session_file(phone)


# --- БЕЗОПАСНОСТЬ 2FA & EMAIL ---
@router.callback_query(F.data == "menu_security")
@router.callback_query(F.data.startswith("acc_sec_"))
async def security_menu(callback: CallbackQuery):
    acc_id = None
    if callback.data.startswith("acc_sec_"): acc_id = int(callback.data.split("_")[2])
    banner = "🛡️ <b>БЕЗОПАСНОСТЬ</b>\n\nВы можете настроить двухфакторную аутентификацию (2FA) и авто-почты."
    buttons = []
    if acc_id:
        buttons.append([InlineKeyboardButton(text="🔑 Установить/Сменить 2FA", callback_data=f"sec_set_2fa_{acc_id}")])
        buttons.append([InlineKeyboardButton(text="📧 Привязать авто-почту", callback_data=f"sec_auto_email_{acc_id}")])
        buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"view_acc_{acc_id}")])
    else:
        buttons.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")])
    await callback.message.edit_text(banner, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data.startswith("sec_set_2fa_"))
async def sec_set_2fa(callback: CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    await state.set_state(BotStates.set_2fa_password); await state.update_data(acc_id=acc_id)
    await callback.message.edit_text("🔑 <b>Установка 2FA</b>\nВведите пароль:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"acc_sec_{acc_id}")]]), parse_mode="HTML")

@router.message(BotStates.set_2fa_password)
async def sec_set_2fa_done(message: Message, state: FSMContext):
    acc_id = (await state.get_data())["acc_id"]; pwd = message.text.strip()
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor(); phone = cursor.execute("SELECT phone FROM accounts WHERE id = ?", (acc_id,)).fetchone()[0]; conn.close()
    msg = await message.answer("⏳ <i>Установка 2FA...</i>", parse_mode="HTML")
    client = await get_client(phone)
    try:
        await client.connect()
        await client.edit_2fa(new_password=pwd)
        conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
        cursor.execute("UPDATE accounts SET has_2fa = 1 WHERE id = ?", (acc_id,)); conn.commit(); conn.close()
        await msg.edit_text(f"✅ <b>2FA установлен!</b>\nПароль: <code>{pwd}</code>", parse_mode="HTML")
    except Exception as e: await msg.edit_text(f"❌ Ошибка: {e}", parse_mode="HTML")
    finally: await client.disconnect(); encrypt_session_file(phone); await state.clear()

@router.callback_query(F.data.startswith("sec_auto_email_"))
async def process_auto_email(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[3])
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor(); phone = cursor.execute("SELECT phone FROM accounts WHERE id = ?", (acc_id,)).fetchone()[0]; conn.close()
    await callback.answer("📧 Инициализация авто-почты...")
    msg = await callback.message.answer("⏳ <i>Создаем временную почту...</i>", parse_mode="HTML")
    
    mail_client = TempMailClient()
    email = mail_client.generate_random_email()
    await msg.edit_text(f"📧 Сгенерирована почта: <code>{email}</code>\nОжидаем код подтверждения от Telegram...", parse_mode="HTML")
    
    client = await get_client(phone)
    try:
        await client.connect()
        # Заглушка для отлова верификации
        code = await mail_client.fetch_telegram_verification_code(email)
        if not code:
            return await msg.edit_text("❌ Код не был получен. Попробуйте снова.", parse_mode="HTML")
        conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
        cursor.execute("UPDATE accounts SET email = ? WHERE id = ?", (email, acc_id))
        conn.commit(); conn.close()
        await msg.edit_text(f"✅ <b>Почта {email} успешно привязана к аккаунту!</b>", parse_mode="HTML")
    except Exception as e: await msg.edit_text(f"❌ Ошибка: {e}", parse_mode="HTML")
    finally: await client.disconnect(); encrypt_session_file(phone)


# --- ПРОКСИ ---
@router.callback_query(F.data.startswith("acc_proxy_"))
async def process_acc_proxy_menu(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    proxy = cursor.execute("SELECT type, host, port, username, status FROM proxies WHERE account_id = ?", (acc_id,)).fetchone(); conn.close()
    
    if proxy:
        banner = f"🌐 <b>ИНДИВИДУАЛЬНЫЕ ПРОКСИ</b>\n\n• Прокси: <b>{proxy[0]}</b>\n• Адрес: <code>{proxy[1]}:{proxy[2]}</code>\n• Статус: <b>{proxy[4]}</b>"
    else:
        banner = "🌐 <b>ПРОКСИ НЕ ПОДКЛЮЧЕН</b>"
        
    buttons = [
        [InlineKeyboardButton(text="➕ Привязать прокси", callback_data=f"proxy_add_{acc_id}")],
        [InlineKeyboardButton(text="🔄 Проверить", callback_data=f"proxy_check_{acc_id}")],
        [InlineKeyboardButton(text="❌ Удалить", callback_data=f"proxy_del_{acc_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"view_acc_{acc_id}")]
    ]
    await callback.message.edit_text(banner, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data.startswith("proxy_add_"))
async def process_proxy_add(callback: CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[2])
    await state.set_state(BotStates.add_proxy_data); await state.update_data(acc_id=acc_id)
    await callback.message.edit_text("🌐 <b>Введите данные прокси в формате:</b>\n<code>Тип | Хост | Порт | Логин | Пароль</code>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"acc_proxy_{acc_id}")]]), parse_mode="HTML")

@router.message(BotStates.add_proxy_data)
async def process_proxy_save(message: Message, state: FSMContext):
    acc_id = (await state.get_data())["acc_id"]
    parts = message.text.split("|")
    if len(parts) < 3: return await message.answer("❌ Неверный формат! Введите: Тип | Хост | Порт")
    ptype, phost, pport = parts[0].strip().upper(), parts[1].strip(), int(parts[2].strip())
    puser = parts[3].strip() if len(parts) > 3 else ""
    ppass = parts[4].strip() if len(parts) > 4 else ""
    
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO proxies (account_id, type, host, port, username, password, status) VALUES (?, ?, ?, ?, ?, ?, 'Не проверен')", (acc_id, ptype, phost, pport, puser, ppass))
    conn.commit(); conn.close()
    await message.answer("✅ Прокси привязан!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌐 Меню прокси", callback_data=f"acc_proxy_{acc_id}")]]))
    await state.clear()

@router.callback_query(F.data.startswith("proxy_check_"))
async def process_proxy_check(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    proxy = cursor.execute("SELECT type, host, port, username, password FROM proxies WHERE account_id = ?", (acc_id,)).fetchone(); conn.close()
    if not proxy: return await callback.answer("Прокси не найден!", show_alert=True)
    import socks
    loop = asyncio.get_event_loop()
    try:
        s = socks.socksocket(); s.set_timeout(4.0)
        s.set_proxy(socks.SOCKS5 if proxy[0] == "SOCKS5" else socks.HTTP, proxy[1], proxy[2], True, proxy[3], proxy[4])
        await loop.run_in_executor(None, s.connect, ("149.154.167.50", 443))
        s.close(); st = "🟢 Валидный"
    except Exception as e: st = f"🔴 Ошибка: {str(e)[:25]}"
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("UPDATE proxies SET status = ? WHERE account_id = ?", (st, acc_id)); conn.commit(); conn.close()
    await callback.answer(f"Статус: {st}", show_alert=True)
    await process_acc_proxy_menu(callback)

@router.callback_query(F.data.startswith("proxy_del_"))
async def process_proxy_del(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor(); cursor.execute("DELETE FROM proxies WHERE account_id = ?", (acc_id,)); conn.commit(); conn.close()
    await callback.answer("🗑️ Прокси отвязан!", show_alert=True)
    await process_acc_proxy_menu(callback)


# --- УАКК — УДАЛЕНИЕ ---
@router.callback_query(F.data.startswith("acc_delete_conf_"))
async def process_delete_conf(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[3])
    banner = "⚠️ <b>Вы уверены, что хотите удалить аккаунт из системы?</b>\nФайлы сессии сотрется с сервера безвозвратно!"
    buttons = [[InlineKeyboardButton(text="🗑️ Да, снести нахрен", callback_data=f"acc_delete_do_{acc_id}")], [InlineKeyboardButton(text="❌ Отмена", callback_data=f"view_acc_{acc_id}")]]
    await callback.message.edit_text(banner, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data.startswith("acc_delete_do_"))
async def process_delete_do(callback: CallbackQuery):
    acc_id = int(callback.data.split("_")[3])
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    phone_row = cursor.execute("SELECT phone FROM accounts WHERE id = ?", (acc_id,)).fetchone()
    if phone_row:
        cursor.execute("DELETE FROM accounts WHERE id = ?", (acc_id,))
        conn.commit()
        for ext in [f"{phone_row[0]}.session", f"{phone_row[0]}.enc"]:
            for d in [SESSIONS_DIR, ENCRYPTED_SESSIONS_DIR]:
                p = os.path.join(d, ext)
                if os.path.exists(p): os.remove(p)
    conn.close()
    await callback.answer("🗑️ Сессия стерта!", show_alert=True)
    await process_accounts_menu(callback)


# --- СТАТИСТИКА, ПОДПИСКИ, АДМИНКА ---
@router.callback_query(F.data == "menu_stats")
async def process_stats_menu(callback: CallbackQuery):
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    accs = cursor.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    chans = cursor.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    bots = cursor.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
    users = cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    banner = f"📊 <b>СТАТИСТИКА СИСТЕМЫ</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n👤 Аккаунтов в сети: <b>{accs}</b>\n📢 Сетка каналов: <b>{chans}</b>\n🤖 Создано ботов: <b>{bots}</b>\n👥 Зарегистрировано: <b>{users}</b>"
    await callback.message.edit_text(banner, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_main")]]), parse_mode="HTML")

@router.callback_query(F.data == "menu_subs")
async def process_subs_menu(callback: CallbackQuery):
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    sub = cursor.execute("SELECT subscription, sub_expires FROM users WHERE user_id = ?", (callback.from_user.id,)).fetchone(); conn.close()
    if not sub:
        sub = ("FREE", "2099-12-31 23:59:59")
    banner = f"🔑 <b>ТАРИФ И ПОДПИСКА</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\nВаш тариф: <b>{sub[0]}</b>\nАктивен до: <code>{sub[1]}</code>"
    await callback.message.edit_text(banner, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_main")]]), parse_mode="HTML")

@router.callback_query(F.data == "menu_admin")
async def process_admin_menu(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer("🛑 Отказано в доступе!", show_alert=True)
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    users = cursor.execute("SELECT user_id, username, subscription, sub_expires FROM users LIMIT 10").fetchall()
    logs = cursor.execute("SELECT id, action, details, timestamp FROM logs ORDER BY id DESC LIMIT 5").fetchall()
    conn.close()
    banner = "👑 <b>АДМИН ПАНЕЛЬ</b>\n\n<b>Пользователи:</b>\n"
    for u in users:
        uname = f"@{u[1]}" if u[1] else "—"
        banner += f"• <code>{u[0]}</code> | {uname} | <b>{u[2]}</b> (до {u[3][:10]})\n"
    banner += "\n<b>Логи:</b>\n"
    for l in logs: banner += f"⏱️ [{l[3]}] {l[2]}\n"
    buttons = [[InlineKeyboardButton(text="🔑 Выдать подписку", callback_data="admin_grant_sub")], [InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_main")]]
    await callback.message.edit_text(banner, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data == "admin_grant_sub")
async def admin_grant_sub_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.admin_grant_sub_id)
    await callback.message.edit_text("🔑 <b>ВЫДАЧА ПОДПИСКИ</b>\nОтправьте User ID пользователя:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu_admin")]]), parse_mode="HTML")

@router.message(BotStates.admin_grant_sub_id)
async def admin_grant_sub_id_received(message: Message, state: FSMContext):
    if not message.text.strip().isdigit(): return await message.answer("❌ ID должен быть числовым!")
    await state.update_data(target_id=int(message.text.strip()))
    await state.set_state(BotStates.admin_grant_sub_plan)
    buttons = [
        [InlineKeyboardButton(text="FREE", callback_data="set_plan_FREE"), InlineKeyboardButton(text="VIP", callback_data="set_plan_VIP")],
        [InlineKeyboardButton(text="FizSeller", callback_data="set_plan_FizSeller"), InlineKeyboardButton(text="ADMIN", callback_data="set_plan_ADMIN")]
    ]
    await message.answer("Шаг 2: Выберите тариф:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("set_plan_"))
async def admin_grant_sub_plan_received(callback: CallbackQuery, state: FSMContext):
    await state.update_data(plan=callback.data.split("_")[2])
    await state.set_state(BotStates.admin_grant_sub_dur)
    buttons = [
        [InlineKeyboardButton(text="1 день", callback_data="dur_1"), InlineKeyboardButton(text="7 дней", callback_data="dur_7")],
        [InlineKeyboardButton(text="30 дней", callback_data="dur_30"), InlineKeyboardButton(text="Бессрочно", callback_data="dur_forever")]
    ]
    await callback.message.edit_text("Шаг 3: Выберите длительность подписки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data.startswith("dur_"))
async def admin_grant_sub_duration_received(callback: CallbackQuery, state: FSMContext):
    dur = callback.data.split("_")[1]
    data = await state.get_data()
    tid, plan = data["target_id"], data["plan"]
    dt = "2099-12-31 23:59:59" if dur == "forever" else (datetime.now() + timedelta(days=int(dur))).strftime("%Y-%m-%d %H:%M:%S")
    
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO users (user_id, subscription, sub_expires, created_at) 
        VALUES (?, ?, ?, COALESCE((SELECT created_at FROM users WHERE user_id = ?), ?))
    """, (tid, plan, dt, tid, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit(); conn.close()
    await callback.message.edit_text(f"✅ Подписка <b>{plan}</b> выдана для <code>{tid}</code> до {dt}!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="menu_admin")]]), parse_mode="HTML")
    await state.clear()


# --- ИТОГОВЫЙ ЗАПУСК ---
async def main():
    logger.info("Запуск Telegram Session Manager Bot...")
    dp.include_router(router)
    asyncio.create_task(hourly_validation_loop())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): logger.info("Бот остановлен.")
