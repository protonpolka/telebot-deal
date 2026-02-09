"""
Telegram-бот для сделок с товарами
Версия для деплоя на Render с PostgreSQL
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, PhotoSize
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ================== НАСТРОЙКИ ==================

# Переменные окружения (будут установлены на Render)
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "0"))
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
DEALS_CHANNEL_ID = int(os.getenv("DEALS_CHANNEL_ID", "0")) if os.getenv("DEALS_CHANNEL_ID") else None
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "support")

# Все админы
ALL_ADMINS = [MAIN_ADMIN_ID] + ADMIN_IDS

# Срок хранения сообщений (дней)
MESSAGE_RETENTION_DAYS = 17

# ================== ЛОГИРОВАНИЕ ==================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ================== БАЗА ДАННЫХ ==================

db_pool = None

async def init_db():
    """Инициализация базы данных"""
    global db_pool
    
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
        
        async with db_pool.acquire() as conn:
            # Таблица пользователей
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    balance DECIMAL(10, 2) DEFAULT 0.0,
                    deals_completed INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица товаров
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS products (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id),
                    name TEXT,
                    category TEXT,
                    email TEXT,
                    password TEXT,
                    price TEXT,
                    trophies TEXT,
                    description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица скриншотов
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS screenshots (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
                    file_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица сообщений
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id),
                    partner_id BIGINT,
                    sender_id BIGINT,
                    text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Индексы для оптимизации
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(user_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_products_user_id ON products(user_id)')
            
        logger.info("База данных инициализирована успешно")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")
        raise

# ================== ФУНКЦИИ РАБОТЫ С БД ==================

async def ensure_user_exists(user_id: int, username: str = None):
    """Создает запись пользователя, если её нет"""
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow('SELECT user_id FROM users WHERE user_id = $1', user_id)
        
        if not existing:
            await conn.execute(
                'INSERT INTO users (user_id, username) VALUES ($1, $2)',
                user_id, username or ""
            )
            logger.info(f"Создан новый пользователь: {user_id} (@{username})")
        elif username:
            await conn.execute(
                'UPDATE users SET username = $1 WHERE user_id = $2',
                username, user_id
            )

async def get_user_balance(user_id: int) -> float:
    """Получает баланс пользователя"""
    async with db_pool.acquire() as conn:
        result = await conn.fetchval('SELECT balance FROM users WHERE user_id = $1', user_id)
        return float(result) if result else 0.0

async def add_balance(user_id: int, amount: float):
    """Добавляет баланс пользователю"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            'UPDATE users SET balance = balance + $1 WHERE user_id = $2',
            amount, user_id
        )
        new_balance = await get_user_balance(user_id)
        logger.info(f"Баланс пользователя {user_id} пополнен на {amount}. Новый баланс: {new_balance}")

async def update_balance(user_id: int, amount: float):
    """Обновляет баланс пользователя"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            'UPDATE users SET balance = $1 WHERE user_id = $2',
            amount, user_id
        )

async def get_user_products(user_id: int) -> List[dict]:
    """Получает все товары пользователя"""
    async with db_pool.acquire() as conn:
        products = await conn.fetch(
            'SELECT * FROM products WHERE user_id = $1 ORDER BY created_at DESC',
            user_id
        )
        
        result = []
        for product in products:
            screenshots = await conn.fetch(
                'SELECT file_id FROM screenshots WHERE product_id = $1',
                product['id']
            )
            
            result.append({
                'id': product['id'],
                'name': product['name'],
                'category': product['category'],
                'email': product['email'],
                'password': product['password'],
                'price': product['price'],
                'trophies': product['trophies'],
                'description': product['description'],
                'screenshots': [s['file_id'] for s in screenshots]
            })
        
        return result

async def add_product(user_id: int, product_data: dict) -> int:
    """Добавляет товар"""
    async with db_pool.acquire() as conn:
        product_id = await conn.fetchval(
            '''INSERT INTO products (user_id, name, category, email, password, price, trophies, description)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id''',
            user_id,
            product_data.get('name'),
            product_data.get('category'),
            product_data.get('email'),
            product_data.get('password'),
            product_data.get('price'),
            product_data.get('trophies'),
            product_data.get('description')
        )
        
        # Добавляем скриншоты
        for file_id in product_data.get('screenshots', []):
            await conn.execute(
                'INSERT INTO screenshots (product_id, file_id) VALUES ($1, $2)',
                product_id, file_id
            )
        
        return product_id

async def add_message(user_id: int, partner_id: int, sender_id: int, text: str):
    """Сохраняет сообщение в БД"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO messages (user_id, partner_id, sender_id, text) VALUES ($1, $2, $3, $4)',
            user_id, partner_id, sender_id, text
        )

async def get_user_messages(user_id: int, limit: int = 10) -> List[dict]:
    """Получает последние сообщения пользователя"""
    async with db_pool.acquire() as conn:
        messages = await conn.fetch(
            'SELECT * FROM messages WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2',
            user_id, limit
        )
        return [dict(m) for m in messages]

async def get_all_users() -> List[dict]:
    """Получает всех пользователей"""
    async with db_pool.acquire() as conn:
        users = await conn.fetch('SELECT * FROM users ORDER BY created_at DESC')
        return [dict(u) for u in users]

async def get_user_by_username(username: str) -> Optional[int]:
    """Находит пользователя по username"""
    normalized = username.lstrip('@').lower()
    async with db_pool.acquire() as conn:
        result = await conn.fetchval(
            'SELECT user_id FROM users WHERE LOWER(username) = $1',
            normalized
        )
        return result

async def increment_deals(user_id: int):
    """Увеличивает счетчик завершенных сделок"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            'UPDATE users SET deals_completed = deals_completed + 1 WHERE user_id = $1',
            user_id
        )

async def cleanup_old_messages():
    """Удаляет старые сообщения"""
    cutoff_date = datetime.now() - timedelta(days=MESSAGE_RETENTION_DAYS)
    async with db_pool.acquire() as conn:
        deleted = await conn.execute(
            'DELETE FROM messages WHERE created_at < $1',
            cutoff_date
        )
        logger.info(f"Удалено старых сообщений: {deleted}")

# ================== FSM СОСТОЯНИЯ ==================

class DealStates(StatesGroup):
    waiting_for_username = State()
    selecting_product = State()
    confirming_deal = State()

class ProductStates(StatesGroup):
    selecting_category = State()
    waiting_for_email = State()
    waiting_for_password = State()
    waiting_for_trophies = State()
    waiting_for_description = State()
    waiting_for_price = State()
    waiting_for_screenshots = State()

class WithdrawalStates(StatesGroup):
    selecting_method = State()
    waiting_for_card = State()

class DepositStates(StatesGroup):
    selecting_method = State()
    waiting_for_amount = State()

class AdminStates(StatesGroup):
    viewing_users = State()
    replying_to_user = State()
    broadcast_message = State()

# ================== ИНИЦИАЛИЗАЦИЯ БОТА ==================

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Хранилище активных сделок (в памяти)
active_deals: Dict[int, dict] = {}
pending_deals: Dict[int, dict] = {}

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================

def is_admin(user_id: int) -> bool:
    return user_id in ALL_ADMINS

def is_main_admin(user_id: int) -> bool:
    return user_id == MAIN_ADMIN_ID

def parse_price(price_str: str) -> float:
    try:
        clean_price = ''.join(c for c in price_str if c.isdigit() or c == '.')
        return float(clean_price) if clean_price else 0.0
    except:
        return 0.0

def get_main_menu(user_id: int = None):
    """Главное меню"""
    buttons = [
        [InlineKeyboardButton(text="🧾 Сделка", callback_data="deal")],
        [InlineKeyboardButton(text="💳 Пополнить", callback_data="deposit")],
        [InlineKeyboardButton(text="💸 Вывод", callback_data="withdrawal")],
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="add_product")],
        [InlineKeyboardButton(text="👥 Реферальная система", callback_data="referral")]
    ]
    
    if user_id and is_admin(user_id):
        buttons.append([InlineKeyboardButton(text="🛠 Админ панель", callback_data="admin_panel")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_chat_keyboard(is_buyer: bool = False):
    """Клавиатура чата"""
    buttons = []
    if is_buyer:
        buttons.append([InlineKeyboardButton(text="✅ Подтвердить получение", callback_data="confirm_receipt")])
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

def get_accept_deal_keyboard(initiator_id: int, product_id: int):
    """Клавиатура принятия сделки"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять сделку", callback_data=f"accept_deal:{initiator_id}:{product_id}")]
    ])

def get_withdrawal_methods_keyboard():
    """Методы вывода"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 CryptoBot", callback_data="withdraw_cryptobot")],
        [
            InlineKeyboardButton(text="₿ BTC", callback_data="withdraw_btc"),
            InlineKeyboardButton(text="Ξ ETH", callback_data="withdraw_eth"),
            InlineKeyboardButton(text="₮ USDT", callback_data="withdraw_usdt")
        ],
        [InlineKeyboardButton(text="⭐️ Звезды", callback_data="withdraw_stars")],
        [InlineKeyboardButton(text="💳 Карты", callback_data="withdraw_card")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
    ])

def get_deposit_methods_keyboard():
    """Методы пополнения"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Карта", callback_data="deposit_card")],
        [
            InlineKeyboardButton(text="₿ BTC", callback_data="deposit_btc"),
            InlineKeyboardButton(text="Ξ ETH", callback_data="deposit_eth"),
            InlineKeyboardButton(text="₮ USDT", callback_data="deposit_usdt")
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
    ])

def get_product_categories_keyboard():
    """Категории товаров"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 SUPERCELL", callback_data="cat_supercell")],
        [InlineKeyboardButton(text="🔥 FREE FIRE", callback_data="cat_freefire")],
        [InlineKeyboardButton(text="🎯 PUBG MOBILE", callback_data="cat_pubg")],
        [InlineKeyboardButton(text="🎨 NFT GIFT", callback_data="cat_nft")],
        [InlineKeyboardButton(text="⭐️ TELEGRAM STARS", callback_data="cat_stars")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
    ])

async def notify_channel_about_deal(buyer_id: int, seller_id: int, product: dict):
    """Отправляет информацию о сделке в канал"""
    if not DEALS_CHANNEL_ID:
        return
    
    async with db_pool.acquire() as conn:
        buyer = await conn.fetchrow('SELECT username FROM users WHERE user_id = $1', buyer_id)
        seller = await conn.fetchrow('SELECT username FROM users WHERE user_id = $1', seller_id)
    
    buyer_username = buyer['username'] if buyer else f"user_{buyer_id}"
    seller_username = seller['username'] if seller else f"user_{seller_id}"
    
    message = (
        f"✅ <b>Новая завершенная сделка</b>\n\n"
        f"🛒 <b>Покупатель:</b> @{buyer_username} (ID: {buyer_id})\n"
        f"💼 <b>Продавец:</b> @{seller_username} (ID: {seller_id})\n\n"
        f"📦 <b>Товар:</b> {product.get('name', 'Не указано')}\n"
        f"💰 <b>Цена:</b> {product.get('price', 'Не указано')}\n"
        f"📂 <b>Категория:</b> {product.get('category', 'Не указано')}\n\n"
        f"📧 <b>Email:</b> {product.get('email', 'Не указано')}\n"
        f"🔑 <b>Пароль:</b> {product.get('password', 'Не указано')}\n"
    )
    
    if product.get('category') == 'SUPERCELL':
        message += f"🏆 <b>Кубки:</b> {product.get('trophies', 'Не указано')}\n"
    
    if product.get('description'):
        message += f"📝 <b>Описание:</b> {product.get('description')}\n"
    
    message += f"\n⏰ <b>Время:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    
    try:
        await bot.send_message(DEALS_CHANNEL_ID, message, parse_mode="HTML")
        
        for screenshot_id in product.get('screenshots', []):
            try:
                await bot.send_photo(DEALS_CHANNEL_ID, screenshot_id)
            except Exception as e:
                logger.error(f"Ошибка отправки скриншота: {e}")
    except Exception as e:
        logger.error(f"Ошибка отправки в канал: {e}")

# ================== ОБРАБОТЧИКИ КОМАНД ==================

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Команда /start"""
    await state.clear()
    
    user_id = message.from_user.id
    username = message.from_user.username
    
    await ensure_user_exists(user_id, username)
    
    logger.info(f"Пользователь {user_id} (@{username}) запустил бота")
    
    balance = await get_user_balance(user_id)
    
    await message.answer(
        f"👋 Добро пожаловать, {message.from_user.first_name}!\n\n"
        f"💰 Ваш баланс: {balance:.2f} ₽\n\n"
        f"Выберите действие:",
        reply_markup=get_main_menu(user_id)
    )

@dp.message(Command("addbalance"))
async def add_balance_command(message: Message):
    """Команда добавления баланса (только главный админ)"""
    if message.from_user.id != MAIN_ADMIN_ID:
        return
    
    try:
        args = message.text.split()
        user_id = int(args[1])
        amount = float(args[2])
        await add_balance(user_id, amount)
    except:
        pass

@dp.message(Command("broadcast"))
async def broadcast_command(message: Message, state: FSMContext):
    """Команда рассылки всем пользователям (только главный админ)"""
    if message.from_user.id != MAIN_ADMIN_ID:
        return
    
    await message.answer(
        "📢 Введите сообщение для рассылки всем пользователям:\n"
        "(отправьте /cancel для отмены)"
    )
    await state.set_state(AdminStates.broadcast_message)

@dp.message(AdminStates.broadcast_message)
async def process_broadcast_message(message: Message, state: FSMContext):
    """Обработка рассылки"""
    if message.text and message.text.startswith("/cancel"):
        await state.clear()
        await message.answer("❌ Рассылка отменена")
        return
    
    users = await get_all_users()
    success = 0
    failed = 0
    
    status_msg = await message.answer("📤 Начинаю рассылку...")
    
    for user in users:
        try:
            await bot.send_message(
                user['user_id'],
                f"📢 <b>Сообщение от администрации:</b>\n\n{message.text}",
                parse_mode="HTML"
            )
            success += 1
            await asyncio.sleep(0.05)  # Защита от флуда
        except Exception as e:
            failed += 1
            logger.error(f"Ошибка отправки пользователю {user['user_id']}: {e}")
    
    await status_msg.edit_text(
        f"✅ Рассылка завершена!\n\n"
        f"Отправлено: {success}\n"
        f"Не доставлено: {failed}"
    )
    
    await state.clear()

# ================== ОБРАБОТЧИКИ СДЕЛОК ==================

@dp.callback_query(F.data == "deal")
async def process_deal_button(callback: CallbackQuery, state: FSMContext):
    """Кнопка 'Сделка'"""
    await callback.answer()
    
    user_id = callback.from_user.id
    
    if user_id in active_deals:
        await callback.message.answer("⚠️ Вы уже находитесь в активной сделке!")
        return
    
    await ensure_user_exists(user_id, callback.from_user.username)
    products = await get_user_products(user_id)
    
    if not products:
        await callback.message.answer(
            "❌ У вас нет добавленных товаров!\n"
            "Сначала добавьте товар через меню.",
            reply_markup=get_main_menu(user_id)
        )
        return
    
    await callback.message.answer(
        "🔍 Введите username второго участника сделки:\n"
        "(можно с @ или без)"
    )
    await state.set_state(DealStates.waiting_for_username)

@dp.message(DealStates.waiting_for_username)
async def process_partner_username(message: Message, state: FSMContext):
    """Обработка username партнера"""
    user_id = message.from_user.id
    partner_username = message.text.strip()
    
    partner_id = await get_user_by_username(partner_username)
    
    if partner_id is None:
        await message.answer(
            "❌ Этот пользователь еще не начал диалог с ботом!\n"
            "Попросите его написать /start",
            reply_markup=get_main_menu(user_id)
        )
        await state.clear()
        return
    
    if partner_id == user_id:
        await message.answer(
            "❌ Нельзя создать сделку с самим собой!",
            reply_markup=get_main_menu(user_id)
        )
        await state.clear()
        return
    
    if partner_id in active_deals:
        await message.answer(
            "❌ Этот пользователь уже находится в активной сделке!",
            reply_markup=get_main_menu(user_id)
        )
        await state.clear()
        return
    
    await state.update_data(partner_id=partner_id, partner_username=partner_username)
    
    products = await get_user_products(user_id)
    buttons = []
    
    for idx, product in enumerate(products):
        buttons.append([
            InlineKeyboardButton(
                text=f"{product['name']} - {product['price']}",
                callback_data=f"select_product:{product['id']}"
            )
        ])
    
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_menu")])
    
    await message.answer(
        "📦 Выберите товар для сделки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await state.set_state(DealStates.selecting_product)

@dp.callback_query(DealStates.selecting_product, F.data.startswith("select_product:"))
async def process_product_selection(callback: CallbackQuery, state: FSMContext):
    """Выбор товара"""
    await callback.answer()
    
    user_id = callback.from_user.id
    product_id = int(callback.data.split(":")[1])
    
    data = await state.get_data()
    partner_id = data["partner_id"]
    partner_username = data["partner_username"]
    
    products = await get_user_products(user_id)
    product = next((p for p in products if p['id'] == product_id), None)
    
    if not product:
        await callback.message.answer("❌ Товар не найден")
        await state.clear()
        return
    
    await state.update_data(product_id=product_id, product=product)
    
    confirmation_text = (
        f"📦 Вы выбрали товар:\n\n"
        f"Название: {product['name']}\n"
        f"Цена: {product['price']}\n"
        f"Покупатель: @{partner_username}\n\n"
        f"Подтвердите создание сделки:"
    )
    
    buttons = [
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_deal_creation")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_menu")]
    ]
    
    await callback.message.answer(
        confirmation_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await state.set_state(DealStates.confirming_deal)

@dp.callback_query(DealStates.confirming_deal, F.data == "confirm_deal_creation")
async def process_confirm_deal_creation(callback: CallbackQuery, state: FSMContext):
    """Подтверждение сделки"""
    await callback.answer()
    
    user_id = callback.from_user.id
    data = await state.get_data()
    
    partner_id = data["partner_id"]
    product = data["product"]
    product_id = data["product_id"]
    
    pending_deals[user_id] = {
        "target_id": partner_id,
        "product": product,
        "product_id": product_id
    }
    
    try:
        deal_info = (
            f"📩 Пользователь @{callback.from_user.username or 'unknown'} "
            f"предлагает сделку!\n\n"
            f"📦 Товар: {product['name']}\n"
            f"💰 Цена: {product['price']}"
        )
        
        await bot.send_message(
            partner_id,
            deal_info,
            reply_markup=get_accept_deal_keyboard(user_id, product_id)
        )
        
        await callback.message.answer(
            f"✅ Запрос на сделку отправлен!\n"
            f"Ожидайте подтверждения...",
            reply_markup=get_main_menu(user_id)
        )
        logger.info(f"Запрос на сделку: {user_id} -> {partner_id}")
        
    except Exception as e:
        logger.error(f"Ошибка отправки запроса: {e}")
        await callback.message.answer(
            "❌ Не удалось отправить запрос пользователю",
            reply_markup=get_main_menu(user_id)
        )
    
    await state.clear()

@dp.callback_query(F.data.startswith("accept_deal:"))
async def process_accept_deal(callback: CallbackQuery):
    """Принятие сделки"""
    await callback.answer()
    
    partner_id = callback.from_user.id
    parts = callback.data.split(":")
    initiator_id = int(parts[1])
    product_id = int(parts[2])
    
    if initiator_id not in pending_deals or pending_deals[initiator_id]["target_id"] != partner_id:
        await callback.message.answer("❌ Эта сделка уже неактуальна")
        return
    
    if initiator_id in active_deals or partner_id in active_deals:
        await callback.message.answer("❌ Один из участников уже в другой сделке")
        del pending_deals[initiator_id]
        return
    
    product = pending_deals[initiator_id]["product"]
    price = parse_price(product["price"])
    
    await ensure_user_exists(partner_id, callback.from_user.username)
    balance = await get_user_balance(partner_id)
    
    if balance < price:
        await callback.message.answer(
            f"❌ Недостаточно средств!\n"
            f"Требуется: {price:.2f} ₽\n"
            f"Ваш баланс: {balance:.2f} ₽\n\n"
            f"Пополните баланс через меню.",
            reply_markup=get_main_menu(partner_id)
        )
        del pending_deals[initiator_id]
        return
    
    # Списываем средства
    await update_balance(partner_id, balance - price)
    
    # Создаем активную сделку
    active_deals[initiator_id] = {
        "partner_id": partner_id,
        "product": product,
        "confirmed": [],
        "seller_id": initiator_id,
        "buyer_id": partner_id
    }
    active_deals[partner_id] = {
        "partner_id": initiator_id,
        "product": product,
        "confirmed": [],
        "seller_id": initiator_id,
        "buyer_id": partner_id
    }
    
    del pending_deals[initiator_id]
    
    logger.info(f"Сделка создана: продавец {initiator_id} <-> покупатель {partner_id}")
    
    new_balance = await get_user_balance(partner_id)
    
    deal_text_buyer = (
        f"✅ Сделка начата!\n\n"
        f"📦 Товар: {product['name']}\n"
        f"💰 Цена: {product['price']}\n"
        f"💳 Списано с баланса: {price:.2f} ₽\n"
        f"💰 Новый баланс: {new_balance:.2f} ₽\n\n"
        f"Теперь вы можете общаться с продавцом через бота.\n"
        f"После получения товара нажмите 'Подтвердить получение'."
    )
    
    await callback.message.answer(
        deal_text_buyer,
        reply_markup=get_chat_keyboard(is_buyer=True)
    )
    
    deal_text_seller = (
        f"✅ Сделка начата!\n\n"
        f"📦 Товар: {product['name']}\n"
        f"💰 Цена: {product['price']}\n"
        f"🔒 Средства заморожены до подтверждения покупателем\n\n"
        f"Теперь вы можете общаться с покупателем через бота.\n"
        f"Отправьте данные товара покупателю."
    )
    
    try:
        await bot.send_message(
            initiator_id,
            deal_text_seller,
            reply_markup=get_chat_keyboard(is_buyer=False)
        )
    except Exception as e:
        logger.error(f"Ошибка уведомления продавца: {e}")

@dp.callback_query(F.data == "confirm_receipt")
async def process_confirm_receipt(callback: CallbackQuery):
    """Подтверждение получения товара"""
    await callback.answer()
    
    user_id = callback.from_user.id
    
    if user_id not in active_deals:
        await callback.message.answer(
            "❌ У вас нет активной сделки",
            reply_markup=get_main_menu(user_id)
        )
        return
    
    deal = active_deals[user_id]
    
    if user_id != deal["buyer_id"]:
        await callback.answer("❌ Только покупатель может подтвердить получение", show_alert=True)
        return
    
    seller_id = deal["seller_id"]
    buyer_id = deal["buyer_id"]
    product = deal["product"]
    price = parse_price(product["price"])
    
    # Переводим деньги продавцу
    seller_balance = await get_user_balance(seller_id)
    await update_balance(seller_id, seller_balance + price)
    await increment_deals(seller_id)
    await increment_deals(buyer_id)
    
    # Удаляем сделку
    del active_deals[seller_id]
    del active_deals[buyer_id]
    
    logger.info(f"Сделка завершена: продавец {seller_id} <-> покупатель {buyer_id}, сумма: {price}")
    
    # Отправляем в канал
    await notify_channel_about_deal(buyer_id, seller_id, product)
    
    new_seller_balance = await get_user_balance(seller_id)
    
    try:
        await bot.send_message(
            seller_id,
            f"✅ Сделка успешно завершена!\n"
            f"💰 +{price:.2f} ₽ к балансу\n"
            f"💳 Ваш баланс: {new_seller_balance:.2f} ₽\n\n"
            f"Покупатель подтвердил получение товара!",
            reply_markup=get_main_menu(seller_id)
        )
    except Exception as e:
        logger.error(f"Ошибка уведомления продавца: {e}")
    
    await callback.message.answer(
        f"✅ Сделка успешно завершена!\n"
        f"Спасибо за покупку!",
        reply_markup=get_main_menu(buyer_id)
    )

# ================== ОБРАБОТЧИКИ ДОБАВЛЕНИЯ ТОВАРА ==================

@dp.callback_query(F.data == "add_product")
async def process_add_product(callback: CallbackQuery, state: FSMContext):
    """Добавление товара"""
    await callback.answer()
    await callback.message.answer(
        "📂 <b>САМЫЕ ПОПУЛЯРНЫЕ ПОЗИЦИИ</b>\n\n"
        "Выберите категорию товара:",
        parse_mode="HTML",
        reply_markup=get_product_categories_keyboard()
    )
    await state.set_state(ProductStates.selecting_category)

@dp.callback_query(ProductStates.selecting_category, F.data.startswith("cat_"))
async def process_category_selection(callback: CallbackQuery, state: FSMContext):
    """Выбор категории"""
    await callback.answer()
    
    category = callback.data.replace("cat_", "").upper()
    await state.update_data(category=category)
    
    if category == "SUPERCELL":
        await callback.message.answer(
            "🎮 <b>SUPERCELL</b>\n\n"
            "Отправьте скриншоты аккаунта.\n"
            "Когда закончите, введите команду /done",
            parse_mode="HTML"
        )
        await state.update_data(screenshots=[])
        await state.set_state(ProductStates.waiting_for_screenshots)
    else:
        await callback.message.answer("📧 Введите email (почту) товара:")
        await state.set_state(ProductStates.waiting_for_email)

@dp.message(ProductStates.waiting_for_screenshots, F.photo)
async def process_screenshot(message: Message, state: FSMContext):
    """Обработка скриншотов"""
    data = await state.get_data()
    screenshots = data.get("screenshots", [])
    
    screenshots.append(message.photo[-1].file_id)
    await state.update_data(screenshots=screenshots)
    
    await message.answer(f"✅ Скриншот {len(screenshots)} добавлен. Отправьте еще или /done")

@dp.message(ProductStates.waiting_for_screenshots, Command("done"))
async def finish_screenshots(message: Message, state: FSMContext):
    """Завершение загрузки скриншотов"""
    data = await state.get_data()
    
    if not data.get("screenshots"):
        await message.answer("❌ Нужно добавить хотя бы один скриншот!")
        return
    
    await message.answer("📧 Введите email (почту) аккаунта:")
    await state.set_state(ProductStates.waiting_for_email)

@dp.message(ProductStates.waiting_for_email)
async def process_product_email(message: Message, state: FSMContext):
    """Обработка email"""
    email = message.text.strip()
    await state.update_data(email=email)
    
    await message.answer(
        "🔑 Введите пароль:\n"
        "(если перепривязка - напишите ПП)"
    )
    await state.set_state(ProductStates.waiting_for_password)

@dp.message(ProductStates.waiting_for_password)
async def process_product_password(message: Message, state: FSMContext):
    """Обработка пароля"""
    password = message.text.strip()
    await state.update_data(password=password)
    
    data = await state.get_data()
    category = data.get("category")
    
    if category == "SUPERCELL":
        await message.answer("🏆 Введите количество кубков:")
        await state.set_state(ProductStates.waiting_for_trophies)
    else:
        await message.answer("📝 Введите описание товара (или /skip чтобы пропустить):")
        await state.set_state(ProductStates.waiting_for_description)

@dp.message(ProductStates.waiting_for_trophies)
async def process_product_trophies(message: Message, state: FSMContext):
    """Обработка кубков"""
    trophies = message.text.strip()
    await state.update_data(trophies=trophies)
    
    await message.answer("📝 Введите описание товара (или /skip чтобы пропустить):")
    await state.set_state(ProductStates.waiting_for_description)

@dp.message(ProductStates.waiting_for_description)
async def process_product_description(message: Message, state: FSMContext):
    """Обработка описания"""
    if message.text and message.text.startswith("/skip"):
        description = None
    else:
        description = message.text.strip()
    
    await state.update_data(description=description)
    
    await message.answer("💰 Введите цену товара (в рублях):")
    await state.set_state(ProductStates.waiting_for_price)

@dp.message(ProductStates.waiting_for_price)
async def process_product_price(message: Message, state: FSMContext):
    """Обработка цены"""
    price = message.text.strip()
    await state.update_data(price=price)
    
    data = await state.get_data()
    category = data.get("category")
    
    if category != "SUPERCELL":
        await message.answer(
            "📸 Отправьте скриншоты товара (фото).\n"
            "Когда закончите, отправьте команду /done"
        )
        await state.update_data(screenshots=[])
        await state.set_state(ProductStates.waiting_for_screenshots)
    else:
        await finish_product_creation(message, state)

async def finish_product_creation(message: Message, state: FSMContext):
    """Финальное создание товара"""
    user_id = message.from_user.id
    data = await state.get_data()
    
    category = data.get("category", "UNKNOWN")
    product_name = f"{category} - {data.get('email', 'no_email')}"
    
    product_data = {
        "name": product_name,
        "category": category,
        "email": data.get("email", "Не указан"),
        "password": data.get("password", "Не указан"),
        "price": data.get("price", "Не указана"),
        "trophies": data.get("trophies"),
        "description": data.get("description"),
        "screenshots": data.get("screenshots", [])
    }
    
    await ensure_user_exists(user_id, message.from_user.username)
    product_id = await add_product(user_id, product_data)
    
    logger.info(f"Товар добавлен пользователем {user_id}: {product_name}")
    
    info_text = (
        f"✅ Товар добавлен!\n\n"
        f"📦 Категория: {category}\n"
        f"📧 Email: {product_data['email']}\n"
        f"💰 Цена: {product_data['price']}\n"
    )
    
    if category == "SUPERCELL":
        info_text += f"🏆 Кубки: {product_data['trophies']}\n"
    
    if product_data['description']:
        info_text += f"📝 Описание: {product_data['description']}\n"
    
    info_text += f"📸 Скриншотов: {len(product_data['screenshots'])}"
    
    await message.answer(
        info_text,
        reply_markup=get_main_menu(user_id)
    )
    
    await state.clear()

# ================== ОБРАБОТЧИКИ ПОПОЛНЕНИЯ/ВЫВОДА ==================

@dp.callback_query(F.data == "deposit")
async def process_deposit(callback: CallbackQuery, state: FSMContext):
    """Пополнение"""
    await callback.answer()
    
    user_id = callback.from_user.id
    await ensure_user_exists(user_id, callback.from_user.username)
    
    balance = await get_user_balance(user_id)
    
    await callback.message.answer(
        f"💰 Ваш баланс: {balance:.2f} ₽\n\n"
        f"Выберите способ пополнения:",
        reply_markup=get_deposit_methods_keyboard()
    )
    await state.set_state(DepositStates.selecting_method)

@dp.callback_query(DepositStates.selecting_method, F.data.startswith("deposit_"))
async def process_deposit_method(callback: CallbackQuery, state: FSMContext):
    """Выбор метода пополнения"""
    await callback.answer()
    
    method = callback.data.replace("deposit_", "").upper()
    
    if method == "CARD":
        method_name = "💳 Карта"
    elif method == "BTC":
        method_name = "₿ BTC"
    elif method == "ETH":
        method_name = "Ξ ETH"
    elif method == "USDT":
        method_name = "₮ USDT"
    else:
        method_name = method
    
    await callback.message.answer(
        f"⏳ Способ: {method_name}\n\n"
        f"Свяжитесь с поддержкой для пополнения:\n"
        f"@{SUPPORT_USERNAME}",
        reply_markup=get_main_menu(callback.from_user.id)
    )
    await state.clear()

@dp.callback_query(F.data == "withdrawal")
async def process_withdrawal(callback: CallbackQuery, state: FSMContext):
    """Вывод"""
    await callback.answer()
    
    user_id = callback.from_user.id
    await ensure_user_exists(user_id, callback.from_user.username)
    
    balance = await get_user_balance(user_id)
    
    await callback.message.answer(
        f"💰 Ваш баланс: {balance:.2f} ₽\n\n"
        f"Выберите способ вывода:",
        reply_markup=get_withdrawal_methods_keyboard()
    )
    await state.set_state(WithdrawalStates.selecting_method)

@dp.callback_query(WithdrawalStates.selecting_method)
async def process_withdrawal_method(callback: CallbackQuery, state: FSMContext):
    """Выбор метода вывода"""
    await callback.answer()
    
    await callback.message.answer(
        f"⏳ Для вывода свяжитесь с поддержкой:\n"
        f"@{SUPPORT_USERNAME}",
        reply_markup=get_main_menu(callback.from_user.id)
    )
    await state.clear()

# ================== ОБРАБОТЧИКИ СООБЩЕНИЙ В СДЕЛКЕ ==================

@dp.message(F.text)
async def process_deal_message(message: Message):
    """Обработка сообщений в сделке"""
    user_id = message.from_user.id
    
    if user_id not in active_deals:
        return
    
    partner_id = active_deals[user_id]["partner_id"]
    username = message.from_user.username or f"user_{user_id}"
    
    await ensure_user_exists(user_id, message.from_user.username)
    await add_message(user_id, partner_id, user_id, f"[{username}]: {message.text}")
    
    try:
        is_buyer = active_deals[partner_id]["buyer_id"] == partner_id
        await bot.send_message(
            partner_id,
            f"💬 @{username}:\n{message.text}",
            reply_markup=get_chat_keyboard(is_buyer=is_buyer)
        )
        logger.info(f"Сообщение переслано: {user_id} -> {partner_id}")
    except Exception as e:
        logger.error(f"Ошибка пересылки сообщения: {e}")
        await message.answer("❌ Не удалось доставить сообщение")

# ================== АДМИН-ПАНЕЛЬ ==================

@dp.callback_query(F.data == "admin_panel")
async def process_admin_panel(callback: CallbackQuery):
    """Админ-панель"""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет доступа", show_alert=True)
        return
    
    await callback.answer()
    
    buttons = []
    
    if is_main_admin(callback.from_user.id):
        buttons.extend([
            [InlineKeyboardButton(text="👥 Все пользователи", callback_data="admin_all_users")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        ])
    
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")])
    
    await callback.message.answer(
        "🛠 <b>Админ-панель</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_button(callback: CallbackQuery, state: FSMContext):
    """Кнопка рассылки"""
    if not is_main_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет доступа", show_alert=True)
        return
    
    await callback.answer()
    await callback.message.answer(
        "📢 Введите сообщение для рассылки:\n"
        "(отправьте /cancel для отмены)"
    )
    await state.set_state(AdminStates.broadcast_message)

@dp.callback_query(F.data == "admin_all_users")
async def process_admin_all_users(callback: CallbackQuery):
    """Все пользователи"""
    if not is_main_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет доступа", show_alert=True)
        return
    
    await callback.answer()
    
    users = await get_all_users()
    
    if not users:
        await callback.message.answer(
            "📊 Нет пользователей",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]
            ])
        )
        return
    
    buttons = []
    for user in users[:50]:  # Показываем первых 50
        username = user['username'] or f"user_{user['user_id']}"
        balance = float(user['balance'])
        
        buttons.append([
            InlineKeyboardButton(
                text=f"@{username} | 💰{balance:.2f}₽ | ✅{user['deals_completed']}",
                callback_data=f"admin_user:{user['user_id']}"
            )
        ])
    
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")])
    
    await callback.message.answer(
        f"👥 <b>Всего пользователей: {len(users)}</b>\n"
        f"(показано первых 50)",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data.startswith("admin_user:"))
async def process_admin_user_details(callback: CallbackQuery, state: FSMContext):
    """Детали пользователя"""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет доступа", show_alert=True)
        return
    
    await callback.answer()
    
    user_id = int(callback.data.split(":")[1])
    
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow('SELECT * FROM users WHERE user_id = $1', user_id)
        
        if not user:
            await callback.message.answer("❌ Пользователь не найден")
            return
        
        products = await get_user_products(user_id)
        messages = await get_user_messages(user_id)
    
    info = f"👤 <b>Пользователь:</b> @{user['username'] or f'user_{user_id}'}\n"
    info += f"🆔 <b>ID:</b> {user_id}\n"
    info += f"💰 <b>Баланс:</b> {float(user['balance']):.2f} ₽\n"
    info += f"✅ <b>Сделок:</b> {user['deals_completed']}\n"
    info += f"📦 <b>Товаров:</b> {len(products)}\n"
    info += f"💬 <b>Сообщений:</b> {len(messages)}\n"
    
    buttons = [
        [InlineKeyboardButton(text="✉️ Написать", callback_data=f"admin_reply:{user_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_all_users")]
    ]
    
    await callback.message.answer(
        info,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data.startswith("admin_reply:"))
async def process_admin_reply_start(callback: CallbackQuery, state: FSMContext):
    """Начало ответа"""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет доступа", show_alert=True)
        return
    
    await callback.answer()
    
    user_id = int(callback.data.split(":")[1])
    
    await state.update_data(reply_to_user=user_id)
    await state.set_state(AdminStates.replying_to_user)
    
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow('SELECT username FROM users WHERE user_id = $1', user_id)
    
    username = user['username'] if user else f"user_{user_id}"
    
    await callback.message.answer(
        f"✉️ Напишите сообщение для @{username}:\n"
        f"(отправьте /cancel для отмены)"
    )

@dp.message(AdminStates.replying_to_user)
async def process_admin_reply_message(message: Message, state: FSMContext):
    """Обработка ответа"""
    if message.text and message.text.startswith("/cancel"):
        await state.clear()
        await message.answer("❌ Отменено")
        return
    
    data = await state.get_data()
    user_id = data.get("reply_to_user")
    
    if not user_id:
        await state.clear()
        return
    
    try:
        await bot.send_message(
            user_id,
            f"📩 <b>Сообщение от поддержки:</b>\n\n{message.text}",
            parse_mode="HTML"
        )
        
        await message.answer("✅ Сообщение отправлено!")
        logger.info(f"Админ {message.from_user.id} отправил сообщение пользователю {user_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")
        await message.answer("❌ Не удалось отправить сообщение")
    
    await state.clear()

# ================== ДРУГИЕ ОБРАБОТЧИКИ ==================

@dp.callback_query(F.data == "referral")
async def process_referral(callback: CallbackQuery):
    """Реферальная система"""
    await callback.answer()
    user_id = callback.from_user.id
    await callback.message.answer(
        f"👥 Ваша реферальная ссылка:\n"
        f"https://t.me/{(await bot.get_me()).username}?start={user_id}\n\n"
        f"Функция в разработке",
        reply_markup=get_main_menu(user_id)
    )

@dp.callback_query(F.data == "back_to_menu")
async def process_back_to_menu(callback: CallbackQuery, state: FSMContext):
    """Возврат в меню"""
    await callback.answer()
    await state.clear()
    
    user_id = callback.from_user.id
    await ensure_user_exists(user_id, callback.from_user.username)
    balance = await get_user_balance(user_id)
    
    await callback.message.answer(
        f"💰 Баланс: {balance:.2f} ₽\n\n"
        f"Главное меню:",
        reply_markup=get_main_menu(user_id)
    )

# ================== ФОНОВЫЕ ЗАДАЧИ ==================

async def cleanup_messages_task():
    """Очистка старых сообщений"""
    while True:
        await asyncio.sleep(3600)  # Каждый час
        await cleanup_old_messages()

# ================== ЗАПУСК БОТА ==================

async def main():
    """Главная функция"""
    logger.info("Инициализация бота...")
    
    # Инициализируем БД
    await init_db()
    
    logger.info("Бот запущен!")
    logger.info(f"ID главного администратора: {MAIN_ADMIN_ID}")
    logger.info(f"Дополнительные админы: {ADMIN_IDS}")
    logger.info(f"Поддержка: @{SUPPORT_USERNAME}")
    
    if DEALS_CHANNEL_ID:
        logger.info(f"Канал для сделок: {DEALS_CHANNEL_ID}")
    
    # Удаляем webhook
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Запускаем фоновые задачи
    asyncio.create_task(cleanup_messages_task())
    
    # Запускаем polling
    try:
        await dp.start_polling(bot)
    finally:
        await db_pool.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
