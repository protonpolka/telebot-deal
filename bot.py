"""
Telegram-бот для сделок с товарами
Версия для деплоя на Render с PostgreSQL
Улучшенная версия с передачей товаров и картинками
"""

import asyncio
import logging
import os
import base64
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, PhotoSize, InputFile, FSInputFile, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from io import BytesIO

# ================== НАСТРОЙКИ ==================

# Переменные окружения (будут установлены на Render/Railway)
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "0"))
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
DEALS_CHANNEL_ID = int(os.getenv("DEALS_CHANNEL_ID", "0")) if os.getenv("DEALS_CHANNEL_ID") else None
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "support")
IMGBB_API_KEY = os.getenv("IMGBB_API_KEY", "")

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
            
            # Таблица товаров (добавлено поле owner_id для отслеживания текущего владельца)
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS products (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id),
                    owner_id BIGINT REFERENCES users(user_id),
                    name TEXT,
                    category TEXT,
                    email TEXT,
                    password TEXT,
                    price TEXT,
                    trophies TEXT,
                    description TEXT,
                    is_sold BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Добавляем колонку owner_id если её нет (для миграции)
            try:
                await conn.execute('ALTER TABLE products ADD COLUMN IF NOT EXISTS owner_id BIGINT REFERENCES users(user_id)')
                await conn.execute('ALTER TABLE products ADD COLUMN IF NOT EXISTS is_sold BOOLEAN DEFAULT FALSE')
                # Устанавливаем owner_id = user_id для существующих товаров
                await conn.execute('UPDATE products SET owner_id = user_id WHERE owner_id IS NULL')
            except:
                pass
            
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
            
            # Таблица картинок для уведомлений (для админа)
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS notification_images (
                    id SERIAL PRIMARY KEY,
                    key TEXT UNIQUE,
                    file_id TEXT,
                    imgbb_url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица запросов на пополнение/вывод
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS payment_requests (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id),
                    request_type TEXT,
                    method TEXT,
                    amount DECIMAL(10, 2),
                    card_number TEXT,
                    bank_name TEXT,
                    crypto_network TEXT,
                    crypto_address TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Индексы для оптимизации
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(user_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_products_user_id ON products(user_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_products_owner_id ON products(owner_id)')
            
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
    """Получает все товары пользователя (принадлежащие ему, независимо от статуса продажи)"""
    async with db_pool.acquire() as conn:
        products = await conn.fetch(
            '''SELECT * FROM products 
               WHERE owner_id = $1
               ORDER BY created_at DESC''',
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
                'screenshots': [s['file_id'] for s in screenshots],
                'is_sold': product['is_sold']
            })
        
        return result

async def get_user_products_for_sale(user_id: int) -> List[dict]:
    """Получает товары пользователя доступные для продажи (не проданные)"""
    async with db_pool.acquire() as conn:
        products = await conn.fetch(
            '''SELECT * FROM products 
               WHERE owner_id = $1 AND is_sold = FALSE
               ORDER BY created_at DESC''',
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
            '''INSERT INTO products (user_id, owner_id, name, category, email, password, price, trophies, description, is_sold)
               VALUES ($1, $1, $2, $3, $4, $5, $6, $7, $8, FALSE) RETURNING id''',
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

async def transfer_product(product_id: int, new_owner_id: int):
    """Передает товар новому владельцу"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            'UPDATE products SET owner_id = $1, is_sold = TRUE WHERE id = $2',
            new_owner_id, product_id
        )
        logger.info(f"Товар {product_id} передан пользователю {new_owner_id}")

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

async def save_notification_image(key: str, file_id: str, imgbb_url: str = None):
    """Сохраняет картинку для уведомлений"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            '''INSERT INTO notification_images (key, file_id, imgbb_url) 
               VALUES ($1, $2, $3)
               ON CONFLICT (key) DO UPDATE SET file_id = $2, imgbb_url = $3''',
            key, file_id, imgbb_url
        )

async def get_notification_image(key: str) -> Optional[dict]:
    """Получает картинку для уведомлений"""
    async with db_pool.acquire() as conn:
        result = await conn.fetchrow(
            'SELECT file_id, imgbb_url FROM notification_images WHERE key = $1',
            key
        )
        return dict(result) if result else None

async def create_payment_request(user_id: int, request_type: str, data: dict) -> int:
    """Создает запрос на пополнение/вывод"""
    async with db_pool.acquire() as conn:
        request_id = await conn.fetchval(
            '''INSERT INTO payment_requests 
               (user_id, request_type, method, amount, card_number, bank_name, crypto_network, crypto_address, status)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending') RETURNING id''',
            user_id,
            request_type,
            data.get('method'),
            data.get('amount'),
            data.get('card_number'),
            data.get('bank_name'),
            data.get('crypto_network'),
            data.get('crypto_address')
        )
        return request_id

async def get_user_payment_requests(user_id: int) -> List[dict]:
    """Получает запросы пользователя"""
    async with db_pool.acquire() as conn:
        requests = await conn.fetch(
            'SELECT * FROM payment_requests WHERE user_id = $1 ORDER BY created_at DESC LIMIT 20',
            user_id
        )
        return [dict(r) for r in requests]

async def get_users_with_deals(admin_id: int) -> List[int]:
    """Получает пользователей, с которыми админ имел сделки"""
    async with db_pool.acquire() as conn:
        # Находим всех пользователей из сообщений где админ был партнером
        users = await conn.fetch(
            '''SELECT DISTINCT user_id FROM messages 
               WHERE partner_id = $1 OR sender_id = $1''',
            admin_id
        )
        return [u['user_id'] for u in users if u['user_id'] != admin_id]

# ================== IMGBB ФУНКЦИИ ==================

async def upload_photo_to_imgbb(file_id: str) -> Optional[str]:
    """Загружает фото на ImgBB и возвращает URL"""
    if not IMGBB_API_KEY:
        logger.warning("ImgBB API ключ не установлен")
        return None
    
    try:
        import aiohttp
        
        # Скачиваем файл от Telegram
        file = await bot.get_file(file_id)
        file_bytes = await bot.download_file(file.file_path)
        
        # Кодируем в base64
        file_base64 = base64.b64encode(file_bytes.read()).decode('utf-8')
        
        # Загружаем на ImgBB
        async with aiohttp.ClientSession() as session:
            data = {
                'key': IMGBB_API_KEY,
                'image': file_base64
            }
            async with session.post('https://api.imgbb.com/1/upload', data=data) as resp:
                result = await resp.json()
                if result.get('success'):
                    url = result['data']['url']
                    logger.info(f"Фото загружено на ImgBB: {url}")
                    return url
        
        return None
    except Exception as e:
        logger.error(f"Ошибка загрузки на ImgBB: {e}")
        return None

# ================== FSM СОСТОЯНИЯ ==================

class DealStates(StatesGroup):
    waiting_for_username = State()
    selecting_product = State()
    confirming_deal = State()

class ProductStates(StatesGroup):
    selecting_category = State()
    selecting_supercell_type = State()
    waiting_for_email = State()
    waiting_for_password = State()
    waiting_for_trophies = State()
    waiting_for_description = State()
    waiting_for_price = State()
    waiting_for_screenshots = State()
    waiting_for_nft_link = State()
    waiting_for_stars_amount = State()
    waiting_for_product_description = State()

class WithdrawalStates(StatesGroup):
    selecting_method = State()
    waiting_for_card = State()
    selecting_crypto_network = State()
    waiting_for_crypto_address = State()

class DepositStates(StatesGroup):
    selecting_method = State()
    waiting_for_amount = State()
    selecting_bank = State()
    selecting_crypto_network = State()
    waiting_for_crypto_amount = State()

class AdminStates(StatesGroup):
    viewing_users = State()
    replying_to_user = State()
    broadcast_message = State()
    upload_notification_image = State()

# ================== ИНИЦИАЛИЗАЦИЯ БОТА ==================

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Хранилище активных сделок (в памяти)
# Формат: {user_id: [deal1, deal2, ...]}
active_deals: Dict[int, List[dict]] = {}
pending_deals: Dict[int, dict] = {}
# Хранилище текущей активной сделки для общения
current_chat_deal: Dict[int, int] = {}  # {user_id: deal_index}

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
        [InlineKeyboardButton(text="📋 Активные сделки", callback_data="active_deals")],
        [InlineKeyboardButton(text="💳 Пополнить", callback_data="deposit")],
        [InlineKeyboardButton(text="💸 Вывод", callback_data="withdrawal")],
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="add_product")],
        [InlineKeyboardButton(text="📦 Мои товары", callback_data="my_products")],
        [InlineKeyboardButton(text="👥 Реферальная система", callback_data="referral")]
    ]
    
    if user_id and is_admin(user_id):
        buttons.append([InlineKeyboardButton(text="🛠 Админ панель", callback_data="admin_panel")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_chat_keyboard(is_buyer: bool = False):
    """Клавиатура чата"""
    buttons = [
        [InlineKeyboardButton(text="📋 Активные сделки", callback_data="active_deals")]
    ]
    if is_buyer:
        buttons.insert(0, [InlineKeyboardButton(text="✅ Подтвердить получение", callback_data="confirm_receipt")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

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
        [InlineKeyboardButton(text="🔫 STANDOFF 2", callback_data="cat_standoff")],
        [InlineKeyboardButton(text="🎨 NFT GIFT", callback_data="cat_nft")],
        [InlineKeyboardButton(text="⭐️ TELEGRAM STARS", callback_data="cat_stars")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
    ])

async def send_with_image(chat_id: int, text: str, image_key: str = None, reply_markup = None):
    """Отправляет сообщение с картинкой если она есть"""
    if image_key and IMGBB_API_KEY:
        image_data = await get_notification_image(image_key)
        if image_data and image_data.get('imgbb_url'):
            try:
                await bot.send_photo(
                    chat_id,
                    photo=image_data['imgbb_url'],
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode="HTML"
                )
                return
            except Exception as e:
                logger.error(f"Ошибка отправки с картинкой: {e}")
    
    # Если картинки нет или ошибка - отправляем обычное сообщение
    await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="HTML")

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
    
    welcome_text = (
        f"👋 Добро пожаловать, {message.from_user.first_name}!\n\n"
        f"💰 Ваш баланс: {balance:.2f} ₽\n\n"
        f"Выберите действие:"
    )
    
    await send_with_image(
        user_id,
        welcome_text,
        image_key="welcome",
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
            await asyncio.sleep(0.05)
        except Exception as e:
            failed += 1
            logger.error(f"Ошибка отправки пользователю {user['user_id']}: {e}")
    
    await status_msg.edit_text(
        f"✅ Рассылка завершена!\n\n"
        f"Отправлено: {success}\n"
        f"Не доставлено: {failed}"
    )
    
    await state.clear()

@dp.message(Command("setimage"))
async def set_image_command(message: Message, state: FSMContext):
    """Команда для установки картинки к уведомлениям (только главный админ)"""
    if message.from_user.id != MAIN_ADMIN_ID:
        return
    
    await message.answer(
        "📸 Отправьте фото и укажите ключ через пробел\n\n"
        "Доступные ключи:\n"
        "• welcome - приветствие\n"
        "• deposit - пополнение\n"
        "• withdrawal - вывод\n"
        "• deal - сделка\n\n"
        "Пример: отправьте фото с подписью 'welcome'\n"
        "/cancel для отмены"
    )
    await state.set_state(AdminStates.upload_notification_image)

@dp.message(AdminStates.upload_notification_image, F.photo)
async def process_notification_image(message: Message, state: FSMContext):
    """Обработка загрузки картинки для уведомлений"""
    if not message.caption:
        await message.answer("❌ Укажите ключ в подписи к фото (welcome, deposit, withdrawal, deal)")
        return
    
    key = message.caption.strip().lower()
    valid_keys = ['welcome', 'deposit', 'withdrawal', 'deal']
    
    if key not in valid_keys:
        await message.answer(f"❌ Неверный ключ. Используйте: {', '.join(valid_keys)}")
        return
    
    file_id = message.photo[-1].file_id
    
    # Загружаем на ImgBB
    imgbb_url = await upload_photo_to_imgbb(file_id)
    
    if imgbb_url:
        await save_notification_image(key, file_id, imgbb_url)
        await message.answer(f"✅ Картинка для '{key}' сохранена!\nURL: {imgbb_url}")
    else:
        await message.answer("❌ Ошибка загрузки на ImgBB. Проверьте API ключ.")
    
    await state.clear()

@dp.message(AdminStates.upload_notification_image, Command("cancel"))
async def cancel_image_upload(message: Message, state: FSMContext):
    """Отмена загрузки"""
    await state.clear()
    await message.answer("❌ Отменено")

# ================== ОБРАБОТЧИКИ СДЕЛОК ==================

@dp.callback_query(F.data == "deal")
async def process_deal_button(callback: CallbackQuery, state: FSMContext):
    """Кнопка 'Сделка'"""
    await callback.answer()
    
    user_id = callback.from_user.id
    
    await ensure_user_exists(user_id, callback.from_user.username)
    products = await get_user_products_for_sale(user_id)  # Только не проданные
    
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

@dp.callback_query(F.data == "active_deals")
async def show_active_deals(callback: CallbackQuery):
    """Показать активные сделки"""
    await callback.answer()
    
    user_id = callback.from_user.id
    user_deals = active_deals.get(user_id, [])
    
    if not user_deals:
        await callback.message.answer(
            "📋 У вас нет активных сделок",
            reply_markup=get_main_menu(user_id)
        )
        return
    
    text = "📋 <b>Ваши активные сделки:</b>\n\n"
    buttons = []
    
    for idx, deal in enumerate(user_deals):
        partner_id = deal["partner_id"]
        product = deal["product"]
        role = "Продавец" if user_id == deal["seller_id"] else "Покупатель"
        
        # Получаем username партнера
        async with db_pool.acquire() as conn:
            partner = await conn.fetchrow('SELECT username FROM users WHERE user_id = $1', partner_id)
        
        partner_username = partner['username'] if partner else f"user_{partner_id}"
        
        deal_info = f"{idx + 1}. С @{partner_username} ({role})"
        
        buttons.append([
            InlineKeyboardButton(
                text=deal_info,
                callback_data=f"select_deal:{idx}"
            )
        ])
        
        text += f"{idx + 1}. <b>{'Вы продаёте' if role == 'Продавец' else 'Вы покупаете'}</b>\n"
        text += f"   👤 Партнёр: @{partner_username}\n"
        text += f"   📦 Товар: {product['name']}\n"
        text += f"   💰 Цена: {product['price']}\n\n"
    
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")])
    
    await callback.message.answer(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data.startswith("select_deal:"))
async def select_active_deal(callback: CallbackQuery):
    """Выбор активной сделки для общения"""
    await callback.answer()
    
    user_id = callback.from_user.id
    deal_index = int(callback.data.split(":")[1])
    
    user_deals = active_deals.get(user_id, [])
    
    if deal_index >= len(user_deals):
        await callback.answer("❌ Сделка не найдена", show_alert=True)
        return
    
    # Устанавливаем текущую активную сделку
    current_chat_deal[user_id] = deal_index
    
    deal = user_deals[deal_index]
    partner_id = deal["partner_id"]
    product = deal["product"]
    is_buyer = user_id == deal["buyer_id"]
    
    async with db_pool.acquire() as conn:
        partner = await conn.fetchrow('SELECT username FROM users WHERE user_id = $1', partner_id)
    
    partner_username = partner['username'] if partner else f"user_{partner_id}"
    
    await callback.message.answer(
        f"💬 <b>Сделка активна</b>\n\n"
        f"👤 Партнёр: @{partner_username}\n"
        f"📦 Товар: {product['name']}\n"
        f"💰 Цена: {product['price']}\n\n"
        f"Пишите сообщения - они будут переданы партнёру",
        parse_mode="HTML",
        reply_markup=get_chat_keyboard(is_buyer=is_buyer)
    )

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
    
    # Проверка: если пользователь НЕ админ, то партнер ДОЛЖЕН быть админом
    if not is_admin(user_id) and not is_admin(partner_id):
        await message.answer(
            "❌ Вы можете создавать сделки только с администраторами!\n"
            f"Пользователь @{partner_username} не является администратором.",
            reply_markup=get_main_menu(user_id)
        )
        await state.clear()
        return
    
    await state.update_data(partner_id=partner_id, partner_username=partner_username)
    
    products = await get_user_products_for_sale(user_id)  # Только не проданные
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
    
    products = await get_user_products_for_sale(user_id)  # Только не проданные
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
    
    # Создаем сделку
    deal_data = {
        "partner_id": partner_id,
        "product": product,
        "product_id": product_id,
        "seller_id": initiator_id,
        "buyer_id": partner_id
    }
    
    # Добавляем сделку в список активных для обоих
    if initiator_id not in active_deals:
        active_deals[initiator_id] = []
    if partner_id not in active_deals:
        active_deals[partner_id] = []
    
    # Для продавца
    seller_deal = deal_data.copy()
    seller_deal["partner_id"] = partner_id
    active_deals[initiator_id].append(seller_deal)
    
    # Для покупателя
    buyer_deal = deal_data.copy()
    buyer_deal["partner_id"] = initiator_id
    active_deals[partner_id].append(buyer_deal)
    
    # Устанавливаем текущую активную сделку
    current_chat_deal[initiator_id] = len(active_deals[initiator_id]) - 1
    current_chat_deal[partner_id] = len(active_deals[partner_id]) - 1
    
    del pending_deals[initiator_id]
    
    logger.info(f"Сделка создана: продавец {initiator_id} <-> покупатель {partner_id}")
    
    new_balance = await get_user_balance(partner_id)
    
    deal_text_buyer = (
        f"✅ <b>Сделка начата!</b>\n\n"
        f"⚠️ <b>ВАЖНО!</b> ⚠️\n"
        f"🔴 Общайтесь ТОЛЬКО в боте Playerok!\n"
        f"🔴 НЕ переходите в личные сообщения!\n"
        f"🔴 Подтверждайте получение ТОЛЬКО после полной проверки товара!\n\n"
        f"📦 Товар: {product['name']}\n"
        f"💰 Цена: {product['price']}\n"
        f"💳 Списано с баланса: {price:.2f} ₽\n"
        f"💰 Новый баланс: {new_balance:.2f} ₽\n\n"
        f"Теперь вы можете общаться с продавцом через бота.\n"
        f"После получения товара нажмите 'Подтвердить получение'."
    )
    
    await callback.message.answer(
        deal_text_buyer,
        reply_markup=get_chat_keyboard(is_buyer=True),
        parse_mode="HTML"
    )
    
    deal_text_seller = (
        f"✅ <b>Сделка начата!</b>\n\n"
        f"⚠️ <b>ВАЖНО!</b> ⚠️\n"
        f"🔴 Общайтесь ТОЛЬКО в боте Playerok!\n"
        f"🔴 НЕ переходите в личные сообщения!\n\n"
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
            reply_markup=get_chat_keyboard(is_buyer=False),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка уведомления продавца: {e}")

@dp.callback_query(F.data == "confirm_receipt")
async def process_confirm_receipt(callback: CallbackQuery):
    """Подтверждение получения товара"""
    await callback.answer()
    
    user_id = callback.from_user.id
    user_deals = active_deals.get(user_id, [])
    
    if not user_deals:
        await callback.message.answer(
            "❌ У вас нет активной сделки",
            reply_markup=get_main_menu(user_id)
        )
        return
    
    # Получаем текущую сделку
    deal_index = current_chat_deal.get(user_id, 0)
    if deal_index >= len(user_deals):
        deal_index = 0
    
    deal = user_deals[deal_index]
    
    if user_id != deal["buyer_id"]:
        await callback.answer("❌ Только покупатель может подтвердить получение", show_alert=True)
        return
    
    # Показываем предупреждение
    warning_text = (
        f"⚠️ <b>ВНИМАНИЕ!</b> ⚠️\n\n"
        f"Подтверждайте получение товара ТОЛЬКО если:\n\n"
        f"✅ Вы ПОЛУЧИЛИ товар\n"
        f"✅ Вы ПРОВЕРИЛИ все данные\n"
        f"✅ Товар СООТВЕТСТВУЕТ описанию\n"
        f"✅ Вы можете ВОЙТИ в аккаунт\n\n"
        f"❌ После подтверждения деньги уйдут продавцу!\n"
        f"❌ Отменить операцию будет НЕВОЗМОЖНО!\n\n"
        f"Вы уверены что хотите подтвердить?"
    )
    
    buttons = [
        [InlineKeyboardButton(text="✅ Да, подтверждаю", callback_data="final_confirm_receipt")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_confirm")]
    ]
    
    await callback.message.answer(
        warning_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data == "cancel_confirm")
async def cancel_confirmation(callback: CallbackQuery):
    """Отмена подтверждения"""
    await callback.answer("Отменено")
    await callback.message.delete()

@dp.callback_query(F.data == "final_confirm_receipt")
async def final_confirm_receipt(callback: CallbackQuery):
    """Финальное подтверждение получения товара"""
    await callback.answer()
    
    user_id = callback.from_user.id
    user_deals = active_deals.get(user_id, [])
    
    if not user_deals:
        await callback.message.answer(
            "❌ У вас нет активной сделки",
            reply_markup=get_main_menu(user_id)
        )
        return
    
    # Получаем текущую сделку
    deal_index = current_chat_deal.get(user_id, 0)
    if deal_index >= len(user_deals):
        deal_index = 0
    
    deal = user_deals[deal_index]
    
    seller_id = deal["seller_id"]
    buyer_id = deal["buyer_id"]
    product = deal["product"]
    product_id = deal["product_id"]
    price = parse_price(product["price"])
    
    # Считаем комиссию платформы (5%)
    PLATFORM_FEE = 0.05
    seller_amount = price * (1 - PLATFORM_FEE)
    platform_fee = price * PLATFORM_FEE
    
    # Передаем товар покупателю
    await transfer_product(product_id, buyer_id)
    
    # Переводим деньги продавцу (с вычетом комиссии)
    seller_balance = await get_user_balance(seller_id)
    await update_balance(seller_id, seller_balance + seller_amount)
    await increment_deals(seller_id)
    await increment_deals(buyer_id)
    
    # Удаляем эту сделку из списков обоих участников
    seller_deals = active_deals.get(seller_id, [])
    buyer_deals = active_deals.get(buyer_id, [])
    
    # Находим и удаляем сделку
    for idx, sdeal in enumerate(seller_deals):
        if sdeal["product_id"] == product_id and sdeal["buyer_id"] == buyer_id:
            seller_deals.pop(idx)
            break
    
    for idx, bdeal in enumerate(buyer_deals):
        if bdeal["product_id"] == product_id and bdeal["seller_id"] == seller_id:
            buyer_deals.pop(idx)
            break
    
    # Обновляем списки
    if seller_deals:
        active_deals[seller_id] = seller_deals
    else:
        if seller_id in active_deals:
            del active_deals[seller_id]
        if seller_id in current_chat_deal:
            del current_chat_deal[seller_id]
    
    if buyer_deals:
        active_deals[buyer_id] = buyer_deals
    else:
        if buyer_id in active_deals:
            del active_deals[buyer_id]
        if buyer_id in current_chat_deal:
            del current_chat_deal[buyer_id]
    
    logger.info(f"Сделка завершена: продавец {seller_id} <-> покупатель {buyer_id}, сумма: {price}")
    logger.info(f"Товар {product_id} передан покупателю {buyer_id}")
    
    # Отправляем в канал
    await notify_channel_about_deal(buyer_id, seller_id, product)
    
    new_seller_balance = await get_user_balance(seller_id)
    
    try:
        await bot.send_message(
            seller_id,
            f"✅ Сделка успешно завершена!\n"
            f"💰 Цена товара: {price:.2f} ₽\n"
            f"📊 Комиссия платформы (5%): -{platform_fee:.2f} ₽\n"
            f"💵 Вы получили: +{seller_amount:.2f} ₽\n"
            f"💳 Новый баланс: {new_seller_balance:.2f} ₽\n\n"
            f"📦 Товар передан покупателю!\n"
            f"Покупатель подтвердил получение товара!",
            reply_markup=get_main_menu(seller_id)
        )
    except Exception as e:
        logger.error(f"Ошибка уведомления продавца: {e}")
    
    await callback.message.answer(
        f"✅ Сделка успешно завершена!\n"
        f"📦 Товар добавлен в ваш список товаров!\n"
        f"Проверьте: Мои товары",
        reply_markup=get_main_menu(buyer_id)
    )

# ================== МОИ ТОВАРЫ ==================

@dp.callback_query(F.data == "my_products")
async def show_my_products(callback: CallbackQuery):
    """Показать мои товары"""
    await callback.answer()
    
    user_id = callback.from_user.id
    products = await get_user_products(user_id)  # ВСЕ товары
    
    if not products:
        await callback.message.answer(
            "📦 У вас пока нет товаров\n\n"
            "Добавьте товар через меню или купите в сделке!",
            reply_markup=get_main_menu(user_id)
        )
        return
    
    text = "📦 <b>Ваши товары:</b>\n\n"
    
    for idx, product in enumerate(products, 1):
        # Определяем статус товара
        status = "🛒 Куплен" if product.get('is_sold') else "📝 Мой"
        category = product.get('category', '')
        
        text += f"{idx}. <b>{product['name']}</b> {status}\n"
        text += f"   💰 Цена: {product['price']}\n"
        
        # Для NFT, STARS, STANDOFF 2 не показываем email/пароль
        if category not in ["NFT", "STARS", "STANDOFF"]:
            text += f"   📧 Email: {product['email']}\n"
            text += f"   🔑 Пароль: {product['password']}\n"
        
        # Для NFT показываем ссылку
        if category == "NFT":
            text += f"   🔗 Ссылка: {product['email']}\n"
        
        # Для STARS показываем количество
        if category == "STARS" and product.get('trophies'):
            text += f"   ⭐ Количество: {product['trophies']} звёзд\n"
        
        # Для SUPERCELL показываем кубки
        if category == "SUPERCELL" and product.get('trophies'):
            text += f"   🏆 Кубки: {product['trophies']}\n"
        
        # Описание показываем всегда если есть
        if product.get('description'):
            text += f"   📝 Описание: {product['description']}\n"
        
        text += "\n"
    
    await callback.message.answer(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
        ])
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
    
    # Для SUPERCELL, FREE FIRE, PUBG - выбор между ПП или Почта+Пароль
    if category in ["SUPERCELL", "FREEFIRE", "PUBG"]:
        category_names = {
            "SUPERCELL": "🎮 SUPERCELL",
            "FREEFIRE": "🔥 FREE FIRE",
            "PUBG": "🎯 PUBG MOBILE"
        }
        
        buttons = [
            [InlineKeyboardButton(text="🔄 Перепривязка (ПП)", callback_data="account_pp")],
            [InlineKeyboardButton(text="📧 Почта + Пароль", callback_data="account_email")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
        ]
        
        await callback.message.answer(
            f"{category_names.get(category, category)}\n\n"
            "Выберите тип аккаунта:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
        await state.set_state(ProductStates.selecting_supercell_type)
        
    elif category == "NFT":
        await callback.message.answer(
            "🎨 <b>NFT GIFT</b>\n\n"
            "📎 Отправьте ссылку на NFT:",
            parse_mode="HTML"
        )
        await state.set_state(ProductStates.waiting_for_nft_link)
        
    elif category == "STARS":
        await callback.message.answer(
            "⭐️ <b>TELEGRAM STARS</b>\n\n"
            "✨ Введите количество звёзд:",
            parse_mode="HTML"
        )
        await state.set_state(ProductStates.waiting_for_stars_amount)
        
    elif category == "STANDOFF":
        # STANDOFF 2 - только описание, БЕЗ почты/пароля
        await callback.message.answer(
            "🔫 <b>STANDOFF 2</b>\n\n"
            "📝 Опишите что вы продаёте:\n"
            "(подробно опишите товар)",
            parse_mode="HTML"
        )
        await state.set_state(ProductStates.waiting_for_product_description)

@dp.callback_query(ProductStates.selecting_supercell_type)
async def process_account_type(callback: CallbackQuery, state: FSMContext):
    """Обработка типа аккаунта (для SUPERCELL, FREE FIRE, PUBG)"""
    await callback.answer()
    
    data = await state.get_data()
    category = data.get("category")
    
    if callback.data == "account_pp":
        # Перепривязка - БЕЗ почты/пароля
        await state.update_data(account_type="pp", email="ПП", password="ПП")
        
        if category == "SUPERCELL":
            # Для SUPERCELL - сначала скриншоты
            await callback.message.answer(
                "🔄 <b>Перепривязка</b>\n\n"
                "Отправьте скриншоты аккаунта.\n"
                "Когда закончите, введите /done",
                parse_mode="HTML"
            )
            await state.update_data(screenshots=[])
            await state.set_state(ProductStates.waiting_for_screenshots)
        else:
            # Для FREE FIRE и PUBG - сразу описание
            await callback.message.answer(
                "🔄 <b>Перепривязка</b>\n\n"
                "📝 Опишите что вы продаёте:",
                parse_mode="HTML"
            )
            await state.set_state(ProductStates.waiting_for_product_description)
        
    elif callback.data == "account_email":
        # Почта + Пароль
        await state.update_data(account_type="email")
        await callback.message.answer(
            "📧 Введите email (почту) аккаунта:"
        )
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
    
    category = data.get("category")
    
    # Для SUPERCELL с перепривязкой
    if category == "SUPERCELL" and data.get("account_type") == "pp":
        await message.answer("🏆 Введите количество кубков:")
        await state.set_state(ProductStates.waiting_for_trophies)
    # Для SUPERCELL с почтой
    elif category == "SUPERCELL":
        await message.answer("📧 Введите email (почту) аккаунта:")
        await state.set_state(ProductStates.waiting_for_email)
    else:
        # Для остальных категорий
        await message.answer("📧 Введите email (почту) аккаунта:")
        await state.set_state(ProductStates.waiting_for_email)

@dp.message(ProductStates.waiting_for_nft_link)
async def process_nft_link(message: Message, state: FSMContext):
    """Обработка ссылки на NFT"""
    nft_link = message.text.strip()
    await state.update_data(email=nft_link, password="NFT", description=f"Ссылка: {nft_link}")
    
    await message.answer("💰 Введите цену товара (в рублях):")
    await state.set_state(ProductStates.waiting_for_price)

@dp.message(ProductStates.waiting_for_stars_amount)
async def process_stars_amount(message: Message, state: FSMContext):
    """Обработка количества звёзд"""
    stars_amount = message.text.strip()
    await state.update_data(
        email=f"{stars_amount} звёзд", 
        password="STARS",
        trophies=stars_amount,
        description=f"Количество: {stars_amount} звёзд"
    )
    
    await message.answer("💰 Введите цену товара (в рублях):")
    await state.set_state(ProductStates.waiting_for_price)

@dp.message(ProductStates.waiting_for_product_description)
async def process_product_general_description(message: Message, state: FSMContext):
    """Обработка общего описания товара"""
    description = message.text.strip()
    data = await state.get_data()
    category = data.get("category")
    
    # Для категорий без почты/пароля (STANDOFF) устанавливаем заглушки
    if category in ["STANDOFF"]:
        await state.update_data(
            email="-",
            password="-",
            description=description
        )
    else:
        # Для FREE FIRE и PUBG с ПП
        await state.update_data(description=description)
    
    await message.answer("💰 Введите цену товара (в рублях):")
    await state.set_state(ProductStates.waiting_for_price)

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
        # Для SUPERCELL - спрашиваем кубки
        await message.answer("🏆 Введите количество кубков:")
        await state.set_state(ProductStates.waiting_for_trophies)
    else:
        # Для FREE FIRE и PUBG - описание
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
    price_text = message.text.strip()
    
    try:
        price = parse_price(price_text)
        if price <= 0:
            raise ValueError
    except:
        await message.answer("❌ Неверная цена. Введите число больше 0:")
        return
    
    # Считаем комиссию платформы (5%)
    PLATFORM_FEE = 0.05  # 5%
    seller_amount = price * (1 - PLATFORM_FEE)
    
    await state.update_data(price=f"{price:.0f} ₽")
    
    # Показываем что продавец получит
    await message.answer(
        f"💰 <b>Цена установлена: {price:.0f} ₽</b>\n\n"
        f"💵 Вы получите: <b>{seller_amount:.0f} ₽</b>\n"
        f"📊 Комиссия платформы: <b>5%</b> ({price * PLATFORM_FEE:.0f} ₽)",
        parse_mode="HTML"
    )
    
    data = await state.get_data()
    category = data.get("category")
    
    # Для NFT, STARS, STANDOFF - скриншоты НЕ нужны
    if category in ["NFT", "STARS", "STANDOFF"]:
        await finish_product_creation(message, state)
    # Для остальных - если еще нет скриншотов, запрашиваем
    elif not data.get("screenshots"):
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
    
    text = (
        f"💰 Ваш баланс: {balance:.2f} ₽\n\n"
        f"Выберите способ пополнения:"
    )
    
    await send_with_image(
        user_id,
        text,
        image_key="deposit",
        reply_markup=get_deposit_methods_keyboard()
    )
    await state.set_state(DepositStates.selecting_method)

@dp.callback_query(DepositStates.selecting_method, F.data == "deposit_card")
async def process_deposit_card_start(callback: CallbackQuery, state: FSMContext):
    """Начало пополнения картой"""
    await callback.answer()
    
    await callback.message.answer(
        "💰 Введите сумму пополнения (в рублях):"
    )
    await state.set_state(DepositStates.waiting_for_amount)

@dp.message(DepositStates.waiting_for_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    """Обработка суммы пополнения"""
    try:
        amount = float(message.text.strip())
        if amount <= 0:
            raise ValueError
    except:
        await message.answer("❌ Неверная сумма. Введите число больше 0:")
        return
    
    await state.update_data(amount=amount)
    
    # Выбор банка
    banks = [
        [InlineKeyboardButton(text="Сбербанк", callback_data="bank_sber")],
        [InlineKeyboardButton(text="Тинькофф", callback_data="bank_tinkoff")],
        [InlineKeyboardButton(text="Альфа-Банк", callback_data="bank_alfa")],
        [InlineKeyboardButton(text="ВТБ", callback_data="bank_vtb")],
        [InlineKeyboardButton(text="Другой банк", callback_data="bank_other")],
    ]
    
    await message.answer(
        "🏦 Выберите ваш банк:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=banks)
    )
    await state.set_state(DepositStates.selecting_bank)

@dp.callback_query(DepositStates.selecting_bank, F.data.startswith("bank_"))
async def process_deposit_bank(callback: CallbackQuery, state: FSMContext):
    """Выбор банка для пополнения"""
    await callback.answer()
    
    bank = callback.data.replace("bank_", "")
    bank_names = {
        "sber": "Сбербанк",
        "tinkoff": "Тинькофф",
        "alfa": "Альфа-Банк",
        "vtb": "ВТБ",
        "other": "Другой банк"
    }
    
    bank_name = bank_names.get(bank, bank)
    data = await state.get_data()
    amount = data.get("amount")
    
    user_id = callback.from_user.id
    username = callback.from_user.username or f"user_{user_id}"
    
    # Сохраняем запрос
    await create_payment_request(user_id, 'deposit', {
        'method': 'CARD',
        'amount': amount,
        'bank_name': bank_name
    })
    
    # Уведомляем админов
    admin_text = (
        f"💳 <b>Запрос на пополнение (КАРТА)</b>\n\n"
        f"👤 Пользователь: @{username} (ID: {user_id})\n"
        f"💰 Сумма: {amount:.2f} ₽\n"
        f"🏦 Банк: {bank_name}\n"
        f"⏰ Время: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )
    
    try:
        await bot.send_message(MAIN_ADMIN_ID, admin_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка уведомления админа: {e}")
    
    # Показываем ожидание
    await callback.message.answer("⏳ Ожидайте ответа оператора...")
    await asyncio.sleep(5)
    
    await callback.message.answer(
        f"📩 Ваш запрос отправлен!\n\n"
        f"Свяжитесь с поддержкой для завершения пополнения:\n"
        f"@{SUPPORT_USERNAME}",
        reply_markup=get_main_menu(user_id)
    )
    
    await state.clear()

@dp.callback_query(DepositStates.selecting_method, F.data.in_(["deposit_btc", "deposit_eth", "deposit_usdt"]))
async def process_deposit_crypto_start(callback: CallbackQuery, state: FSMContext):
    """Начало пополнения криптой"""
    await callback.answer()
    
    crypto = callback.data.replace("deposit_", "").upper()
    await state.update_data(crypto=crypto)
    
    # Выбор сети
    networks = []
    if crypto == "USDT":
        networks = [
            [InlineKeyboardButton(text="TRC-20 (Tron)", callback_data="network_trc20")],
            [InlineKeyboardButton(text="ERC-20 (Ethereum)", callback_data="network_erc20")],
            [InlineKeyboardButton(text="BEP-20 (BSC)", callback_data="network_bep20")],
        ]
    elif crypto == "BTC":
        networks = [
            [InlineKeyboardButton(text="Bitcoin Network", callback_data="network_btc")],
        ]
    elif crypto == "ETH":
        networks = [
            [InlineKeyboardButton(text="ERC-20 (Ethereum)", callback_data="network_erc20")],
        ]
    
    networks.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")])
    
    await callback.message.answer(
        f"Выберите сеть для {crypto}:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=networks)
    )
    await state.set_state(DepositStates.selecting_crypto_network)

@dp.callback_query(DepositStates.selecting_crypto_network, F.data.startswith("network_"))
async def process_deposit_crypto_network(callback: CallbackQuery, state: FSMContext):
    """Выбор сети крипты для пополнения"""
    await callback.answer()
    
    network = callback.data.replace("network_", "").upper()
    await state.update_data(network=network)
    
    await callback.message.answer(
        "💰 Введите сумму пополнения (в рублях):"
    )
    await state.set_state(DepositStates.waiting_for_crypto_amount)

@dp.message(DepositStates.waiting_for_crypto_amount)
async def process_deposit_crypto_amount(message: Message, state: FSMContext):
    """Обработка суммы пополнения криптой"""
    try:
        amount = float(message.text.strip())
        if amount <= 0:
            raise ValueError
    except:
        await message.answer("❌ Неверная сумма. Введите число больше 0:")
        return
    
    data = await state.get_data()
    crypto = data.get("crypto")
    network = data.get("network")
    
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    
    # Сохраняем запрос
    await create_payment_request(user_id, 'deposit', {
        'method': crypto,
        'amount': amount,
        'crypto_network': network
    })
    
    # Уведомляем админов
    admin_text = (
        f"💳 <b>Запрос на пополнение ({crypto})</b>\n\n"
        f"👤 Пользователь: @{username} (ID: {user_id})\n"
        f"💰 Сумма: {amount:.2f} ₽\n"
        f"🌐 Сеть: {network}\n"
        f"⏰ Время: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )
    
    try:
        await bot.send_message(MAIN_ADMIN_ID, admin_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка уведомления админа: {e}")
    
    await message.answer(
        f"✅ Запрос на пополнение отправлен!\n\n"
        f"Свяжитесь с поддержкой для получения реквизитов:\n"
        f"@{SUPPORT_USERNAME}",
        reply_markup=get_main_menu(user_id)
    )
    
    await state.clear()

@dp.callback_query(F.data == "withdrawal")
async def process_withdrawal(callback: CallbackQuery, state: FSMContext):
    """Вывод"""
    await callback.answer()
    
    user_id = callback.from_user.id
    await ensure_user_exists(user_id, callback.from_user.username)
    
    balance = await get_user_balance(user_id)
    
    text = (
        f"💰 Ваш баланс: {balance:.2f} ₽\n\n"
        f"Выберите способ вывода:"
    )
    
    await send_with_image(
        user_id,
        text,
        image_key="withdrawal",
        reply_markup=get_withdrawal_methods_keyboard()
    )
    await state.set_state(WithdrawalStates.selecting_method)

@dp.callback_query(WithdrawalStates.selecting_method, F.data == "withdraw_card")
async def process_card_withdrawal(callback: CallbackQuery, state: FSMContext):
    """Обработка вывода на карту"""
    await callback.answer()
    
    await callback.message.answer(
        "💳 Введите номер карты для вывода:\n"
        "(16 цифр без пробелов)"
    )
    await state.set_state(WithdrawalStates.waiting_for_card)

@dp.message(WithdrawalStates.waiting_for_card)
async def process_card_number_withdrawal(message: Message, state: FSMContext):
    """Обработка номера карты для вывода"""
    card_number = message.text.strip().replace(" ", "")
    
    if not card_number.isdigit() or len(card_number) < 16:
        await message.answer("❌ Неверный формат карты. Введите 16 цифр:")
        return
    
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    balance = await get_user_balance(user_id)
    
    # Сохраняем запрос
    await create_payment_request(user_id, 'withdrawal', {
        'method': 'CARD',
        'amount': balance,
        'card_number': card_number
    })
    
    # Уведомляем админов
    admin_text = (
        f"💸 <b>Запрос на вывод (КАРТА)</b>\n\n"
        f"👤 Пользователь: @{username} (ID: {user_id})\n"
        f"💰 Сумма: {balance:.2f} ₽\n"
        f"💳 Карта: {card_number}\n"
        f"⏰ Время: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )
    
    try:
        await bot.send_message(MAIN_ADMIN_ID, admin_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка уведомления админа: {e}")
    
    # Показываем ожидание
    await message.answer("⏳ Обрабатываем ваш запрос...")
    await asyncio.sleep(30)
    
    await message.answer(
        f"❌ Произошла ошибка при выводе\n\n"
        f"Напишите в поддержку: @{SUPPORT_USERNAME}",
        reply_markup=get_main_menu(user_id)
    )
    
    await state.clear()

@dp.callback_query(WithdrawalStates.selecting_method, F.data.in_(["withdraw_btc", "withdraw_eth", "withdraw_usdt", "withdraw_cryptobot", "withdraw_stars"]))
async def process_unavailable_withdrawal(callback: CallbackQuery, state: FSMContext):
    """Недоступные методы вывода"""
    await callback.answer("❌ Вывод недоступен. Доступны только карты.", show_alert=True)
    await state.clear()

# ================== ОБРАБОТЧИКИ СООБЩЕНИЙ В СДЕЛКЕ ==================

@dp.message(F.text)
async def process_deal_message(message: Message):
    """Обработка сообщений в сделке"""
    user_id = message.from_user.id
    
    user_deals = active_deals.get(user_id, [])
    
    if not user_deals:
        return
    
    # Получаем индекс текущей активной сделки
    deal_index = current_chat_deal.get(user_id, 0)
    
    if deal_index >= len(user_deals):
        deal_index = 0
        current_chat_deal[user_id] = 0
    
    deal = user_deals[deal_index]
    partner_id = deal["partner_id"]
    username = message.from_user.username or f"user_{user_id}"
    
    await ensure_user_exists(user_id, message.from_user.username)
    await add_message(user_id, partner_id, user_id, f"[{username}]: {message.text}")
    
    try:
        # Находим сделку партнера с этим пользователем
        partner_deals = active_deals.get(partner_id, [])
        partner_deal_index = None
        
        for idx, pdeal in enumerate(partner_deals):
            if pdeal["partner_id"] == user_id and pdeal["product_id"] == deal["product_id"]:
                partner_deal_index = idx
                break
        
        if partner_deal_index is not None:
            partner_deal = partner_deals[partner_deal_index]
            is_buyer = partner_deal["buyer_id"] == partner_id
            
            # Определяем номер сделки для партнера
            deal_number = partner_deal_index + 1
            total_deals = len(partner_deals)
            
            # Помечаем из какой сделки пришло сообщение
            deal_label = f"[Сделка {deal_number}/{total_deals}]" if total_deals > 1 else ""
            
            await bot.send_message(
                partner_id,
                f"{deal_label} 💬 @{username}:\n{message.text}",
                reply_markup=get_chat_keyboard(is_buyer=is_buyer)
            )
            logger.info(f"Сообщение переслано: {user_id} -> {partner_id} (сделка {deal_number})")
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
            [InlineKeyboardButton(text="📸 Установить картинки", callback_data="admin_set_images")],
        ])
    else:
        # Для доп админов показываем пользователей с кем были сделки
        buttons.append([InlineKeyboardButton(text="👥 Мои пользователи", callback_data="admin_my_users")])
    
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")])
    
    await callback.message.answer(
        "🛠 <b>Админ-панель</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data == "admin_my_users")
async def process_admin_my_users(callback: CallbackQuery):
    """Пользователи с кем были сделки (для доп админов)"""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет доступа", show_alert=True)
        return
    
    await callback.answer()
    
    admin_id = callback.from_user.id
    user_ids = await get_users_with_deals(admin_id)
    
    if not user_ids:
        await callback.message.answer(
            "📊 У вас пока нет пользователей\n\n"
            "Пользователи появятся после первой сделки",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]
            ])
        )
        return
    
    async with db_pool.acquire() as conn:
        users = await conn.fetch(
            'SELECT * FROM users WHERE user_id = ANY($1)',
            user_ids
        )
    
    buttons = []
    for user in users:
        username = user['username'] or f"user_{user['user_id']}"
        balance = float(user['balance'])
        
        buttons.append([
            InlineKeyboardButton(
                text=f"@{username} | 💰{balance:.2f}₽",
                callback_data=f"admin_user:{user['user_id']}"
            )
        ])
    
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")])
    
    await callback.message.answer(
        f"👥 <b>Ваши пользователи: {len(users)}</b>\n"
        f"(только те, с кем были сделки)",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data == "admin_set_images")
async def admin_set_images_info(callback: CallbackQuery):
    """Информация об установке картинок"""
    await callback.answer()
    
    await callback.message.answer(
        "📸 <b>Установка картинок для уведомлений</b>\n\n"
        "Используйте команду /setimage\n\n"
        "Доступные ключи:\n"
        "• welcome - приветствие при /start\n"
        "• deposit - пополнение баланса\n"
        "• withdrawal - вывод средств\n"
        "• deal - начало сделки\n\n"
        "Пример:\n"
        "1. Отправьте /setimage\n"
        "2. Отправьте фото с подписью 'welcome'\n"
        "3. Картинка загрузится на ImgBB",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]
        ])
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
    for user in users[:50]:
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
        requests = await get_user_payment_requests(user_id)
    
    info = f"👤 <b>Пользователь:</b> @{user['username'] or f'user_{user_id}'}\n"
    info += f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
    info += f"💰 <b>Баланс:</b> {float(user['balance']):.2f} ₽\n"
    info += f"✅ <b>Сделок:</b> {user['deals_completed']}\n"
    info += f"📦 <b>Товаров:</b> {len(products)}\n"
    info += f"💬 <b>Сообщений:</b> {len(messages)}\n"
    info += f"📋 <b>Запросов пополнения/вывода:</b> {len(requests)}\n\n"
    
    # Показываем последние запросы
    if requests:
        info += "<b>📋 Последние запросы:</b>\n"
        for req in requests[:5]:
            req_type = "💳 Пополнение" if req['request_type'] == 'deposit' else "💸 Вывод"
            method = req['method']
            amount = float(req['amount']) if req['amount'] else 0
            time_str = req['created_at'].strftime('%d.%m %H:%M') if req['created_at'] else ''
            
            info += f"{req_type} {method} - {amount:.2f}₽ ({time_str})\n"
            
            if req['bank_name']:
                info += f"   🏦 {req['bank_name']}\n"
            if req['crypto_network']:
                info += f"   🌐 {req['crypto_network']}\n"
            if req['card_number']:
                info += f"   💳 {req['card_number']}\n"
            if req['crypto_address']:
                info += f"   📍 <code>{req['crypto_address'][:20]}...</code>\n"
    
    buttons = [
        [InlineKeyboardButton(text="✉️ Написать", callback_data=f"admin_reply:{user_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_all_users" if is_main_admin(callback.from_user.id) else "admin_my_users")]
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
        await asyncio.sleep(3600)
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
    
    if IMGBB_API_KEY:
        logger.info("ImgBB API ключ установлен")
    else:
        logger.warning("ImgBB API ключ не установлен - картинки в уведомлениях работать не будут")
    
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
