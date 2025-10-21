import os
import logging
import json
import psycopg2
import re
from datetime import datetime, timedelta, time
import pytz
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, ConversationHandler, filters, CallbackQueryHandler
import math
import validators
from time import sleep
from shapely.geometry import Point, Polygon
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import requests

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Your credentials
BOT_TOKEN = "7386306627:AAHdCm0OMiitG09dEbD0qmjbNT-pvq0Ny6A"
DATABASE_URL = "postgresql://postgres.unceacyznxuawksbfctj:Aster#123#@aws-1-eu-north-1.pooler.supabase.com:6543/postgres"
ADMIN_IDS = [8188464845]

# Admin locations (hardcoded) - treated as polygon vertices (lat, lon)
ADMIN_LOCATIONS = [
    (9.020238599143552, 38.82560078203035),
    (9.017190196514154, 38.75281767667821),
    (8.98208254568819, 38.75948863161473),
    (8.980054995596422, 38.77906699321482),
    (8.985448934391043, 38.79958228020363),
    (9.006143350714895, 38.78995524036579)
]

# Create the delivery polygon (shapely expects (lon, lat))
DELIVERY_POLYGON = Polygon([(lon, lat) for lat, lon in ADMIN_LOCATIONS])

# Time zone for East Africa Time (EAT, UTC+3)
EAT = pytz.timezone('Africa/Nairobi')

# Default menu fallback
default_menu = [
    {'id': 1, 'name': 'ምስር ወጥ', 'price': 160.00, 'category': 'fasting'},
    {'id': 2, 'name': 'ጎመን', 'price': 160.00, 'category': 'fasting'},
    {'id': 3, 'name': 'ሽሮ', 'price': 160.00, 'category': 'fasting'},
    {'id': 4, 'name': 'ፓስታ', 'price': 160.00, 'category': 'fasting'},
    {'id': 5, 'name': 'ፍርፍር', 'price': 160.00, 'category': 'fasting'},
    {'id': 6, 'name': 'የጾም በሼፍ ውሳኔ', 'price': 160.00, 'category': 'fasting'},
    {'id': 7, 'name': 'ምስር በስጋ', 'price': 260.00, 'category': 'non_fasting'},
    {'id': 8, 'name': 'ጎመን በስጋ', 'price': 260.00, 'category': 'non_fasting'},
    {'id': 9, 'name': 'ቦዘና ሽሮ', 'price': 260.00, 'category': 'non_fasting'},
    {'id': 10, 'name': 'ፓስታ በስጋ', 'price': 260.00, 'category': 'non_fasting'},
    {'id': 11, 'name': 'ጥብስ/ቋንጣ ፍርፍር', 'price': 260.00, 'category': 'non_fasting'},
    {'id': 12, 'name': 'የፍስክ በሼፍ ውሳኔ', 'price': 260.00, 'category': 'non_fasting'}
]

# Conversation states
(
    MAIN_MENU, REGISTER_NAME, REGISTER_PHONE, REGISTER_LOCATION, CONFIRM_LOCATION,
    CONFIRM_REGISTRATION, CHOOSE_PLAN, CHOOSE_DATE, MEAL_SELECTION, CONFIRM_MEAL, PAYMENT_UPLOAD,
    RESCHEDULE_MEAL, ADMIN_UPDATE_MENU, ADMIN_ANNOUNCE, ADMIN_DAILY_ORDERS,
    ADMIN_DELETE_MENU, SET_ADMIN_LOCATION, ADMIN_APPROVE_PAYMENT, SUPPORT_MENU,
    WAIT_LOCATION_APPROVAL, USER_CHANGE_LOCATION, RESCHEDULE_DATE, RESCHEDULE_CONFIRM
) = range(23)

# Database connection helper
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        conn.set_session(autocommit=False)
        return conn
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        raise

# Helper to check if user has pending location
def has_pending_location(user_id):
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM public.pending_locations WHERE user_id = %s AND status = 'pending'", (user_id,))
        result = cur.fetchone()
        return result is not None
    except Exception as e:
        logger.error(f"Error checking pending location for user {user_id}: {e}")
        return False
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Initialize database
def init_db():
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Create schema if not exists
        cur.execute("CREATE SCHEMA IF NOT EXISTS public")
        # Create users table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS public.users (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                username VARCHAR(255),
                full_name VARCHAR(255),
                phone_number VARCHAR(20),
                location VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cur.execute("ALTER TABLE public.users DISABLE ROW LEVEL SECURITY")
        # Create subscriptions table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS public.subscriptions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                plan_type VARCHAR(50) NOT NULL,
                meals_remaining INTEGER NOT NULL,
                selected_dates JSONB NOT NULL,
                expiry_date TIMESTAMP NOT NULL,
                status VARCHAR(50) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES public.users(telegram_id) ON DELETE CASCADE
            )
        ''')
        cur.execute("ALTER TABLE public.subscriptions DISABLE ROW LEVEL SECURITY")
        # Add selected_dates column if it doesn't exist
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                    AND table_name = 'subscriptions'
                    AND column_name = 'selected_dates'
                ) THEN
                    ALTER TABLE public.subscriptions ADD COLUMN selected_dates JSONB NOT NULL DEFAULT '[]';
                END IF;
            END$$;
        """)
        # Create categories table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS public.categories (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cur.execute("ALTER TABLE public.categories DISABLE ROW LEVEL SECURITY")
        # Create menu_items table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS public.menu_items (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                description TEXT,
                price DECIMAL(10,2) NOT NULL,
                category_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (category_id) REFERENCES public.categories(id)
            )
        ''')
        cur.execute("ALTER TABLE public.menu_items DISABLE ROW LEVEL SECURITY")
        # Create weekly_menus table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS public.weekly_menus (
                id SERIAL PRIMARY KEY,
                week_start_date DATE NOT NULL,
                menu_items JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cur.execute("ALTER TABLE public.weekly_menus DISABLE ROW LEVEL SECURITY")
        # Add unique constraint
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'unique_week_start_date') THEN
                    ALTER TABLE public.weekly_menus ADD CONSTRAINT unique_week_start_date UNIQUE (week_start_date);
                END IF;
            END$$;
        """)
        # Create orders table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS public.orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                subscription_id INTEGER,
                meal_date DATE NOT NULL,
                items JSONB NOT NULL,
                status VARCHAR(50) DEFAULT 'confirmed',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES public.users(telegram_id) ON DELETE SET NULL,
                FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id) ON DELETE SET NULL
            )
        ''')
        cur.execute("ALTER TABLE public.orders DISABLE ROW LEVEL SECURITY")
        # Create payments table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS public.payments (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                subscription_id INTEGER,
                amount DECIMAL(10,2) NOT NULL,
                receipt_url TEXT,
                status VARCHAR(50) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES public.users(telegram_id) ON DELETE SET NULL,
                FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id) ON DELETE SET NULL
            )
        ''')
        cur.execute("ALTER TABLE public.payments DISABLE ROW LEVEL SECURITY")
        # Create pending_locations table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS public.pending_locations (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                location_text TEXT NOT NULL,
                status VARCHAR(50) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES public.users(telegram_id) ON DELETE CASCADE
            )
        ''')
        cur.execute("ALTER TABLE public.pending_locations DISABLE ROW LEVEL SECURITY")
        # Create settings table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS public.settings (
                key VARCHAR(255) PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cur.execute("ALTER TABLE public.settings DISABLE ROW LEVEL SECURITY")
        # Insert default categories if none exist
        cur.execute("SELECT COUNT(*) FROM public.categories")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO public.categories (name) VALUES ('Main Dishes'), ('Sides'), ('Drinks'), ('Desserts')")
        conn.commit()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        if conn:
            conn.rollback()
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Helper function to ensure user exists
async def ensure_user_exists(user, conn, cur):
    try:
        cur.execute(
            "INSERT INTO public.users (telegram_id, username, full_name) "
            "VALUES (%s, %s, %s) ON CONFLICT (telegram_id) DO UPDATE SET "
            "username = EXCLUDED.username, full_name = EXCLUDED.full_name",
            (user.id, user.username or '', user.full_name or '')
        )
        conn.commit()
        cur.execute("SELECT telegram_id FROM public.users WHERE telegram_id = %s", (user.id,))
        if cur.fetchone():
            logger.info(f"Successfully ensured user {user.id} exists")
            return True
        return False
    except Exception as e:
        logger.error(f"Error ensuring user {user.id} exists: {e}")
        conn.rollback()
        return False

def build_delete_menu_text(menu_items, week_start):
    valid_days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    day_order = {day: idx for idx, day in enumerate(valid_days)}
    sorted_items = sorted(menu_items, key=lambda x: day_order.get(x['day'], len(valid_days)))
    text = f"📋 የምግብ ዝርዝር ለሳምንቱ መጀመሪያ {week_start} (ለማስወገድ የተወሰነ ንጥል ይምረጡ):\n\n"
    for idx, item in enumerate(sorted_items, 1):
        text += f"{idx}. {item['day']}: {item['name']} - {item['price']:.2f} ብር\n\n"
    return text

def get_main_keyboard(user_id):
    if has_pending_location(user_id):
        # Restricted keyboard during location approval
        keyboard = [['⏳ ማረጋገጫ በመጠበቅ ላይ', '💬 ድጋፍ']]
    elif user_id in ADMIN_IDS:
        keyboard = [
            ['🔐 ምግብ ዝርዝር አዘምን', '🔐 ምግብ ዝርዝር ሰርዝ'],
            ['🔐 ተመዝጋቢዎችን ተመልከት', '🔐 ክፍያዎችን ተመልከት'],
            ['🔐 ክፍያዎችን አረጋግጥ', '🔐 የዕለት ትዕዛዞች'],
            ['🔐 ማስታወቂያ', '🔐 ቦታ አዘጋጅ'],
            ['🔐 ቦታዎችን ተመልከት', '🔐 ቦታዎችን አረጋግጥ'],
            ['🔐 የሳምንቱን ሪፖርት አትም']
        ]
    else:
        keyboard = [
            ['🍽 ምግብ ዝርዝር', '🛒 ምዝገባ'],
            ['👤 የእኔ መረጃ', '📅 የእኔ ምግቦች', '🔄 ትዕዛዙን መዘዋወር'],
            ['📞 ድጋፍ']
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# Start command with updated onboarding message
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Onboarding message in Amharic
        onboarding_text = (
            "👋 እንኳን ወደ ኦዝ ኪችን የምግብ ምዝገባ በደና መጡ!\n\n"
            "🍽 ትኩስ እና ጣፋጭ ምግቦችን በነጻ ለእርስዎ እናደርሳለን።\n\n"
            "📋 የአገልግሎቱ መግለጫዎች እና ሂደቶች:\n\n"
            "1️⃣ የምዝገባ እቅድዎን እና ቀን ይምረጡ\n\n"
            "2️⃣ የሚወዷቸውን ምግቦች ከምግብ ዝርዝር ውስጥ ይምረጡ (ወይንም ከፈለጉ በሼፍ ውሳኔ)\n\n"
            "3️⃣ በየቀኑ የማስታወሻ መልክት ያገኛሉ እና አስፈላጊ ሆኖ ሲገኝ የመሰረዝ እና ወደሌላ የጊዜ ማዘዋወር ይቻላል።\n\n"
            "🚀 ይጀምሩ!"
        )
        # Check if user is registered
        cur.execute("SELECT full_name, phone_number, location FROM public.users WHERE telegram_id = %s", (user.id,))
        user_data = cur.fetchone()
        if user_data and user_data[0] and user_data[1] and user_data[2]:
            # Show full main menu
            await update.message.reply_text(
                f"👋 እንኳን ተመልሰው መጡ {user.first_name}!\n\n{onboarding_text}",
                reply_markup=get_main_keyboard(user.id)
            )
            return MAIN_MENU
        else:
            # Show only two buttons for new users
            keyboard = [['📋 ይመዝገቡ', '💬 ድጋፍ']]
            await update.message.reply_text(
                onboarding_text,
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            )
            return MAIN_MENU
    except Exception as e:
        logger.error(f"Error in start for user {user.id}: {e}")
        await update.message.reply_text("❌ በመጀመር ላይ ስህተት ተከስቷል። እባክዎ እንደገና ይሞክሩ።\n\n🔄 እንደገና ይሞክሩ!")
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Support handler
async def support_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📞 አስተዳዳሪውን ያግኙ\n\n"
        "💬 መልክት ለመላክ: @oz_misaka\n\n"
        "📱 ለመደወል: 0928 39 7777\n\n"
        "🚀 ድጋፍ በተቻለአችነት ይረዳሉ!",
        reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
    )
    return SUPPORT_MENU

# Back to main menu
async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT full_name, phone_number, location FROM public.users WHERE telegram_id = %s", (user.id,))
    user_data = cur.fetchone()
    cur.close()
    conn.close()
    if user_data and user_data[0] and user_data[1] and user_data[2]:
        await update.message.reply_text(
            "🧾 ወደ መነሻ ገጽ ተመልሰዋል።\n\n"
            "🍽 ምርጫዎችዎን ይመልከቱ!",
            reply_markup=get_main_keyboard(user.id)
        )
        return MAIN_MENU
    else:
        keyboard = [['📋 ይመዝገቡ', '💬 ድጋፍ']]
        await update.message.reply_text(
            "🧾 ወደ መነሻ ገጽ ተመልሰዋል።\n\n"
            "📋 ይመዝገቡ ወይም ድጋፍ ይጠቀሙ!",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return MAIN_MENU

# Help command (used after payment approval and for "እርዳታ አግኝ")
async def send_help_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    commands_text = (
        "👋 እንኳን ወደ ኦዝ ኪችን የምግብ ምዝገባ በደና መጡ!\n\n"
        "🍽 ትኩስ እና ጣፋጭ ምግቦችን በነጻ ለእርስዎ እናደርሳለን።\n\n"
        "📋 የአገልግሎቱ መግለጫዎች እና ሂደቶች?\n\n"
        "1️⃣ የምዝገባ እቅድዎን እና ቀን ይምረጡ\n\n"
        "2️⃣ የሚወዷቸውን ምግቦች ከምግብ ዝርዝር ውስጥ ይምረጡ (ወይንም ከፈለጉ በሼፍ ውሳኔ)\n\n"
        "3️⃣ በየቀኑ የማስታወሻ መልክት ያገኛሉ እና አስፈላጊ ሆኖ ሲገኝ የመሰረዝ እና ወደሌላ የጊዜ ማዘዋወር ይቻላል።\n\n"
        "🔧 📋 የሚገኙ ትዕዛዞች:\n\n"
        "🍽 /menu - የሳምንቱን ምግብ ዝርዝር ይመልከቱ\n\n"
        "🛒 /subscribe - የምዝገባ እቅድ ይምረጡ\n\n"
        "👤 /my_subscription - የእርስዎ መረጃ ይመልከቱ\n\n"
        "📅 /my_meals - የመረጧቸውን ምግቦች ይመልከቱ\n\n"
        "❓ /help - ይህን የእገዛ መልእክት ይመልከቱ\n\n"
        "🍴 /select_meals - ምግቦችዎን ይምረጡ"
    )
    if user.id in ADMIN_IDS:
        commands_text += (
            "\n\n🔐 🔧 የአስተዳዳሪ ትዕዛዞች:\n\n"
            "/admin_update_menu - የሳምንቱን ምግብ ዝርዝር ያዘምኑ\n\n"
            "/admin_delete_menu - የሳምንቱን ምግብ ዝርዝር ይሰርዙ\n\n"
            "/admin_subscribers - ንቁ ተመዝጋቢዎችን ይመልከቱ\n\n"
            "/admin_payments - ክፍላዎችን ይከታተሉ\n\n"
            "/admin_approve_payment - ተጠባቂ ክፍላዎችን ያረጋግጡ ወይም ውድቅ ያድርጉ\n\n"
            "/admin_daily_orders - የዕለት ትዕዛዝ ዝርዝር ይመልከቱ\n\n"
            "/admin_announce - ማስታወቂያዎችን ይላኩ\n\n"
            "/setadminlocation - የካፌ ቦታ ያዘጋጁ\n\n"
            "/viewlocations - የተጋሩ ቦታዎችን ይመልከቱ\n\n"
            "/admin_approve_locations - ተጠባቂ ቦታዎችን ያረጋጋጡ"
        )
    await update.message.reply_text(commands_text, reply_markup=get_main_keyboard(user.id))

# User Profile Handler
async def user_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if has_pending_location(user.id):
        await update.message.reply_text("⏳ ቦታዎ ለማረጋገጥ በመጠበቅ ላይ ነው። እባክዎ ይጠብቁ።\n\n🔄 እባክዎ ይጠብቁ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT full_name, phone_number, location FROM public.users WHERE telegram_id = %s", (user.id,))
        user_data = cur.fetchone()
        if not user_data or not all(user_data):
            await update.message.reply_text("❌ መረጃዎ የለም። እባክዎ ይመዝገቡ።\n\n🛒 /subscribe ይጠቀሙ!", reply_markup=get_main_keyboard(user.id))
            return MAIN_MENU
        full_name, phone_number, location = user_data
        text = (
            "👤 የእርስዎ መረጃ ዝርዝር\n\n"
            f"📝 ስም: {full_name}\n\n"
            f"📱 ስልክ ቁጥር: {phone_number}\n\n"
            f"📍 አድራሻ: {location}\n\n"
            "🔧 ለመቀየር ምርጫዎችን ይመልከቱ!"
        )
        keyboard = [['🏠 ቦታ ቀይር', '🔙 ተመለስ']]
        await update.message.reply_text(text, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return USER_CHANGE_LOCATION  # Wait for change or back
    except Exception as e:
        logger.error(f"Error fetching user profile for {user.id}: {e}")
        await update.message.reply_text("❌ መረጃዎን መጫን ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Change Location Handler
async def change_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message.text == '🔙 ተመለስ':
        await back_to_main(update, context)
        return MAIN_MENU
    if update.message.text == '🏠 ቦታ ቀይር':
        await update.message.reply_text(
            "📝 እባክዎ የመላኪያ ቦታዎን በጽሑፍ ያስገቡ ወይም የGoogle Map Link ይላኩላን\n\n"
            "📍 **ለምሳሌ:**\n\n"
            "“Bole Edna mall, Alemnesh Plaza, office number 102”\n\n"
            "[https://maps.app.goo.gl/o8EYgQAohNpR3gJE7]\n\n"
            "🚀 ቦታዎን ያስገቡ!",
            reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
        )
        return USER_CHANGE_LOCATION
    # Process the location input
    location = update.message.text.strip()
    if not location:
        await update.message.reply_text(
            "❌ ቦታ አልተስገበም። እባክዎ ቦታዎን በጽሑፍ ያስገቡ።\n\n"
            "🔄 እባክዎ እንደገና ይሞክሩ!",
            reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
        )
        return USER_CHANGE_LOCATION
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Insert into pending_locations
        cur.execute(
            "INSERT INTO public.pending_locations (user_id, location_text) VALUES (%s, %s) RETURNING id",
            (user.id, location)
        )
        pending_id = cur.fetchone()[0]
        conn.commit()
        # Notify admins
        for admin_id in ADMIN_IDS:
            try:
                keyboard = [
                    [InlineKeyboardButton("አረጋግጥ", callback_data=f"approve_location_{pending_id}"),
                     InlineKeyboardButton("ውድቅ አድርግ", callback_data=f"reject_location_{pending_id}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"🔔 አዲስ ቦታ ጥያቆ ከተጠቃሚ {user.id} ({context.user_data.get('full_name', 'የለም')}):\n\n📍 {location}\n\n🔧 ለማረጋገጥ ወይም ለመሰረዝ ይመርጡ!",
                    reply_markup=reply_markup
                )
            except Exception as e:
                logger.error(f"Error notifying admin {admin_id} about location {pending_id}: {e}")
        await update.message.reply_text(
            "📤 ቦታዎ ተልኳል።\n\n"
            "⏳ ከአስተዳዳሪው ማረጋገጫን በትክክል ይጠብቁ።\n\n"
            "🚀 በትክክል ይጠብቁ!",
            reply_markup=get_main_keyboard(user.id)
        )
        context.user_data['pending_location_id'] = pending_id
        return WAIT_LOCATION_APPROVAL
    except Exception as e:
        logger.error(f"Error saving location for user {user.id}: {e}")
        await update.message.reply_text("❌ ቦታ በማስቀመጥ ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!")
        return USER_CHANGE_LOCATION
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Updated My Meals Handler
async def my_meals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if has_pending_location(user.id):
        await update.message.reply_text("⏳ ቦታዎ ለማረጋገጥ በመጠበቅ ላይ ነው። እባክዎ ይጠብቁ።\n\n🔄 እባክዎ ይጠብቁ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    if user.id in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪዎች ምግብ ዝርዝር አያስፈልጋቸውም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Fetch subscription
        cur.execute(
            "SELECT plan_type, meals_remaining, selected_dates FROM public.subscriptions WHERE user_id = %s AND status = 'active'",
            (user.id,)
        )
        subscription = cur.fetchone()
        if not subscription:
            await update.message.reply_text(
                "❌ ንቁ ምዝገባ የለም።\n\n"
                "🛒 /subscribe ይጠቀሙ!",
                reply_markup=get_main_keyboard(user.id)
            )
            return MAIN_MENU
        plan_type, meals_remaining, selected_dates_json = subscription
        selected_dates_en = json.loads(selected_dates_json) if isinstance(selected_dates_json, str) else selected_dates_json
        valid_days_en = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        valid_days_am = ['ሰኞ', 'ማክሰኞ', 'እሮብ', 'ሐሙስ', 'አርብ', 'ቅዳሜ', 'እሑድ']
        selected_dates = [valid_days_am[valid_days_en.index(day)] for day in selected_dates_en]
        # Fetch orders for total price and selected meals
        cur.execute(
            "SELECT meal_date, items FROM public.orders WHERE user_id = %s AND status = 'confirmed' ORDER BY meal_date",
            (user.id,)
        )
        orders = cur.fetchall()
        total_price = 0
        meal_details = []
        for meal_date, items_json in orders:
            items = json.loads(items_json) if isinstance(items_json, str) else items_json
            for item in items:
                total_price += item['price']
                meal_details.append(f"{meal_date.strftime('%Y-%m-%d')}: {item['name']}")
        text = (
            f"🗓️ የተመዘገቡበት ቀን:\n\n"
            f"📅 {', '.join(selected_dates)}\n\n"
            f"🍴 የተመረጡ ምግብ:\n\n"
            f"{', '.join(meal_details) if meal_details else 'አልተመረጡም'}\n\n"
            f"💰 ጠቅላላ ዋጋ: {total_price:.2f} ብር\n\n"
            f"🍽 ቀሪ ምግቦች: {meals_remaining}\n\n"
            "🔧 ለመምረጥ /select_meals ይጠቀሙ!"
        )
        await update.message.reply_text(text, reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error fetching meals for user {user.id}: {e}")
        await update.message.reply_text("❌ የምግብ ዝርዝር መጫን ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Reschedule Start Handler
async def reschedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if has_pending_location(user.id):
        await update.message.reply_text("⏳ ቦታዎ ለማረጋገጥ በመጠበቅ ላይ ነው። እባክዎ ይጠብቁ።\n\n🔄 እባክዎ ይጠብቁ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    if user.id in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪዎች ማዘዋወር አያስፈልጋቸውም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT s.plan_type, o.id as order_id, o.meal_date, o.items, s.expiry_date
            FROM public.orders o
            JOIN public.subscriptions s ON o.subscription_id = s.id
            WHERE o.user_id = %s AND o.status = 'confirmed' AND s.status = 'active'
            ORDER BY o.meal_date
        """, (user.id,))
        orders_data = cur.fetchall()
        if not orders_data:
            await update.message.reply_text(
                "❌ ለማዘዋወር ትዕዛዝ የለም።\n\n"
                "📅 /my_meals ይመልከቱ!",
                reply_markup=get_main_keyboard(user.id)
            )
            return MAIN_MENU
        # Filter eligible orders
        eligible_orders = []
        current_time = datetime.now(EAT)
        lunch_time = time(12, 0)
        dinner_time = time(18, 0)
        for plan_type, order_id, meal_date, items_json, expiry_date in orders_data:
            if meal_date < current_time.date():
                continue
            scheduled_time_obj = lunch_time if plan_type == 'lunch' else dinner_time
            scheduled_time = datetime.combine(meal_date, scheduled_time_obj, tzinfo=EAT)
            if current_time + timedelta(hours=3) >= scheduled_time:
                continue
            items = json.loads(items_json) if isinstance(items_json, str) else items_json
            eligible_orders.append({
                'order_id': order_id,
                'plan_type': plan_type,
                'meal_date': meal_date,
                'items': items,
                'expiry': expiry_date
            })
        if not eligible_orders:
            await update.message.reply_text(
                "❌ ለማዘዋወር ተስማሚ ትዕዛዝ የለም (3 ሰዓት ቀደም ብሎ ይጀምሩ)።\n\n"
                "🔄 እባክዎ ቀደም ብለው ይጠቀሙ!",
                reply_markup=get_main_keyboard(user.id)
            )
            return MAIN_MENU
        # Show eligible orders
        text = "🔄 ለማዘዋወር ትዕዛዞች:\n\n"
        for idx, ord in enumerate(eligible_orders, 1):
            meal_d = ord['meal_date'].strftime('%Y-%m-%d')
            plan_am = 'ምሳ' if ord['plan_type'] == 'lunch' else 'እራት'
            items_names = ', '.join([item['name'] for item in ord['items']])
            text += f"{idx}. {meal_d} ({plan_am}): {items_names}\n\n"
        text += "🔢 ለማዘዋወር ቁጥር ያስገቡ።"
        context.user_data['eligible_orders'] = eligible_orders
        await update.message.reply_text(
            text,
            reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
        )
        return RESCHEDULE_MEAL
    except Exception as e:
        logger.error(f"Error starting reschedule for user {user.id}: {e}")
        await update.message.reply_text("❌ ማዘዋወር መጀመር ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Process Reschedule Order Selection
async def process_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    if text == '🔙 ተመለስ':
        context.user_data.pop('eligible_orders', None)
        return await back_to_main(update, context)
    eligible_orders = context.user_data.get('eligible_orders', [])
    try:
        idx = int(text) - 1
        if 0 > idx or idx >= len(eligible_orders):
            await update.message.reply_text(
                "❌ የማይሰራ ቁጥር።\n\n"
                "🔄 ትክክለኛ ቁጥር ያስገቡ!",
                reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
            )
            return RESCHEDULE_MEAL
        selected_order = eligible_orders[idx]
        context.user_data['selected_order'] = selected_order
        # Compute possible future dates
        current_date = datetime.now(EAT).date()
        expiry_date = selected_order['expiry'].date()
        possible_dates = []
        valid_days_en = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        valid_days_am = ['ሰኞ', 'ማክሰኞ', 'እሮብ', 'ሐሙስ', 'አርብ', 'ቅዳሜ', 'እሑድ']
        conn = None
        cur = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            for i in range(1, (expiry_date - current_date).days + 1):
                candidate = current_date + timedelta(days=i)
                cur.execute(
                    "SELECT 1 FROM public.orders WHERE user_id = %s AND meal_date = %s AND status = 'confirmed'",
                    (user.id, candidate)
                )
                if not cur.fetchone():
                    day_en = valid_days_en[candidate.weekday()]
                    day_am = valid_days_am[valid_days_en.index(day_en)]
                    date_str = candidate.strftime('%Y-%m-%d')
                    button_text = f"{day_am} ({date_str})"
                    possible_dates.append((candidate, button_text))
        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()
        if not possible_dates:
            await update.message.reply_text(
                "❌ ለማዘዋወር ተስማሚ ቀን የለም።\n\n"
                "🔄 እባክዎ እንደገና ይሞክሩ!",
                reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
            )
            return RESCHEDULE_DATE
        context.user_data['possible_dates'] = possible_dates
        # Build keyboard with buttons for possible dates
        keyboard = []
        for _, button_text in possible_dates:
            keyboard.append([button_text])
        keyboard.append(['🔙 ተመለስ'])
        await update.message.reply_text(
            "📅 አዲሱን ቀን ይምረጡ (ከዛሬ ቀጣይ ቀናት እስከ ጫናዎ ውስጥ):\n\n"
            "🚀 ቀን ይምረጡ!",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        )
        return RESCHEDULE_DATE
    except ValueError:
        await update.message.reply_text(
            "❌ ቁጥር ያስገቡ (ለምሳሌ: 1)።\n\n"
            "🔄 ትክክለኛ ቁጥር ያስገቡ!",
            reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
        )
        return RESCHEDULE_MEAL

# Reschedule Date Selection
async def reschedule_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    if text == '🔙 ተመለስ':
        context.user_data.pop('eligible_orders', None)
        context.user_data.pop('selected_order', None)
        context.user_data.pop('possible_dates', None)
        return await back_to_main(update, context)
    selected_order = context.user_data.get('selected_order')
    if not selected_order:
        await update.message.reply_text("❌ ስህተት: ትዕዛዝ አልተመረጠም።\n\n🔄 /select_meals ይጀምሩ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    possible_dates = context.user_data.get('possible_dates', [])
    new_date = None
    for candidate, button_text in possible_dates:
        if text == button_text:
            new_date = candidate
            break
    if not new_date:
        # Invalid selection, reprompt
        keyboard = []
        for _, button_text in possible_dates:
            keyboard.append([button_text])
        keyboard.append(['🔙 ተመለስ'])
        await update.message.reply_text(
            "❌ የማይሰራ ምርጫ።\n\n"
            "📅 ትክክለኛ ቀን ይምረጡ:\n\n"
            "🚀 ቀን ይምረጡ!",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        )
        return RESCHEDULE_DATE
    expiry_date = selected_order['expiry'].date()
    if new_date > expiry_date:
        await update.message.reply_text(
            f"❌ አዲሱ ቀን የምዝገባዎ ጫና ({expiry_date}) ውስጥ መሆን አለበት።\n\n"
            "🔄 ትክክለኛ ቀን ያስገቡ!",
            reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
        )
        return RESCHEDULE_DATE
    # Confirm
    old_date_str = selected_order['meal_date'].strftime('%Y-%m-%d')
    plan_am = 'ምሳ' if selected_order['plan_type'] == 'lunch' else 'እራት'
    items_names = ', '.join([item['name'] for item in selected_order['items']])
    confirm_text = (
        f"🔄 ማዘዋወር ማረጋገጫ:\n\n"
        f"ከ {old_date_str} ({plan_am}) ወደ {new_date}\n\n"
        f"🍴 {items_names}\n\n"
        "✅ ያረጋግጡ?"
    )
    keyboard = [['✅ አረጋግጥ', '⛔ ሰርዝ'], ['🔙 ተመለስ']]
    context.user_data['new_date'] = new_date
    await update.message.reply_text(
        confirm_text,
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )
    return RESCHEDULE_CONFIRM

# Confirm Reschedule
async def confirm_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    choice = update.message.text
    if choice == '🔙 ተመለስ':
        context.user_data.pop('eligible_orders', None)
        context.user_data.pop('selected_order', None)
        context.user_data.pop('new_date', None)
        context.user_data.pop('possible_dates', None)
        return await back_to_main(update, context)
    if choice == '⛔ ሰርዝ':
        context.user_data.pop('eligible_orders', None)
        context.user_data.pop('selected_order', None)
        context.user_data.pop('new_date', None)
        context.user_data.pop('possible_dates', None)
        await update.message.reply_text("❌ ማዘዋወር ተሰርዟል።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    if choice == '✅ አረጋግጥ':
        selected_order = context.user_data.get('selected_order')
        new_date = context.user_data['new_date']
        order_id = selected_order['order_id']
        conn = None
        cur = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "UPDATE public.orders SET meal_date = %s WHERE id = %s",
                (new_date, order_id)
            )
            conn.commit()
            await update.message.reply_text(
                "✅ ትዕዛዙ በተሳካ ሁኔታ ተዘዋወረ!\n\n"
                "🚀 ተዘዋወረ!",
                reply_markup=get_main_keyboard(user.id)
            )
        except Exception as e:
            logger.error(f"Error confirming reschedule for order {order_id}: {e}")
            await update.message.reply_text(
                "❌ ማዘዋወር በማረጋገጥ ላይ ስህተት።\n\n"
                "🔄 እባክዎ እንደገና ይሞክሩ!",
                reply_markup=get_main_keyboard(user.id)
            )
        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()
        context.user_data.pop('eligible_orders', None)
        context.user_data.pop('selected_order', None)
        context.user_data.pop('new_date', None)
        context.user_data.pop('possible_dates', None)
        return MAIN_MENU
    # Reprompt if invalid
    await update.message.reply_text(
        "❌ እባክዎ '✅ አረጋግጥ' ወይም '⛔ ሰርዝ' ይምረጡ።",
        reply_markup=ReplyKeyboardMarkup(
            [['✅ አረጋግጥ', '⛔ ሰርዝ'], ['🔙 ተመለስ']],
            resize_keyboard=True
        )
    )
    return RESCHEDULE_CONFIRM

# Registration: Full name
async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message.text == '🔙 ተመለስ':
        return await back_to_main(update, context)
    await update.message.reply_text(
        "📝 እባክዎ ሙሉ ስምዎን ያስገቡ።\n\n"
        "🚀 ስምዎን ያስገቡ!",
        reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
    )
    return REGISTER_NAME

async def save_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message.text == '🔙 ተመለስ':
        return await back_to_main(update, context)
    context.user_data['full_name'] = update.message.text
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        if not await ensure_user_exists(user, conn, cur):
            await update.message.reply_text("❌ ተጠቃሚ መመዝገብ ላይ ስህተት ተከስቷል።\n\n🔄 እባክዎ እንደገና ይሞክሩ!")
            return MAIN_MENU
        cur.execute(
            "UPDATE public.users SET full_name = %s WHERE telegram_id = %s",
            (context.user_data['full_name'], user.id)
        )
        conn.commit()
        await update.message.reply_text(
            "📱 እባክዎ ስልክ ቁጥርዎን ያስገቡ (ለምሳሌ: 0912345678)።\n\n"
            "🚀 ስልክ ቁጥርዎን ያስገቡ!",
            reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
        )
        return REGISTER_PHONE
    except Exception as e:
        logger.error(f"Error saving name for user {user.id}: {e}")
        await update.message.reply_text("❌ ስም በማስቀመጥ ላይ ስህተት ተከስቷል።\n\n🔄 እባክዎ እንደገና ይሞክሩ!")
        return REGISTER_NAME
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Registration: Phone number (manual input only)
async def register_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message.text == '🔙 ተመለስ':
        return await back_to_main(update, context)
    phone_number = update.message.text.strip()
    # Basic phone validation (Ethiopian format)
    if not re.match(r'^09\d{8}$', phone_number):
        await update.message.reply_text(
            "❌ የማይሰራ ስልክ ቁጥር።\n\n"
            "📱 እባክዎ ትክክለኛ የኢትዮጵያ ቁጥር ያስገቡ (ለምሳሌ: 0912345678)።\n\n"
            "🔄 እንደገና ይሞክሩ!",
            reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
        )
        return REGISTER_PHONE
    context.user_data['phone_number'] = phone_number
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE public.users SET phone_number = %s WHERE telegram_id = %s",
            (phone_number, user.id)
        )
        conn.commit()
        if user.id in ADMIN_IDS:
            # Skip location for admins
            context.user_data['location'] = "Admin Location"
            cur.execute(
                "UPDATE public.users SET location = %s WHERE telegram_id = %s",
                ("Admin Location", user.id)
            )
            conn.commit()
            registration_text = (
                "📋 ያስገቡት መረጃ:\n\n"
                f"📝 ሙሉ ስም: {context.user_data.get('full_name', 'የለም')}\n\n"
                f"📱 ስልክ ቁጥር: {context.user_data.get('phone_number', 'የለም')}\n\n"
                f"📍 የመላኪያ ቦታ: {context.user_data.get('location', 'የለም')}\n\n"
                "✅ መረጃውን ያረጋግጡ።\n\n"
                "🔄 ትክክል ከሆነ 'መረጃው ትክክል ነው ቀጥል' ይምረጡ፣ ካልሆነ 'አስተካክል' ይምረጡ።"
            )
            keyboard = [['✅ መረጃው ትክክል ነው ቀጥል', '⛔ አስተካክል'], ['🔙 ተመለስ']]
            await update.message.reply_text(
                registration_text,
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
            )
            return CONFIRM_REGISTRATION
        else:
            await update.message.reply_text(
                "📝 እባክዎ የመላኪያ ቦታዎን በጽሑፍ ያስገቡ ወይም የGoogle Map Link ይላኩላን\n\n"
                "📍 **ለምሳሌ:**\n\n"
                "“Bole Edna mall, Alemnesh Plaza, office number 102”\n\n"
                "[https://maps.app.goo.gl/o8EYgQAohNpR3gJE7]\n\n"
                "🚀 ቦታዎን ያስገቡ!",
                reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
            )
            return REGISTER_LOCATION
    except Exception as e:
        logger.error(f"Error saving phone for user {user.id}: {e}")
        await update.message.reply_text("❌ ስልክ ቁጥር በማስቀመጥ ላይ ስህተት ተከስቷል።\n\n🔄 እባክዎ እንደገና ይሞክሩ!")
        return REGISTER_PHONE
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Registration: Location (manual text entry only)
async def register_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message.text == '🔙 ተመለስ':
        return await back_to_main(update, context)
    location = update.message.text.strip()
    if not location:
        await update.message.reply_text(
            "❌ ቦታ አልተስገበም።\n\n"
            "📝 እባክዎ ቦታዎን በጽሑፍ ያስገቡ።\n\n"
            "🔄 እንደገና ይሞክሩ!",
            reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
        )
        return REGISTER_LOCATION
    context.user_data['location'] = location
    # Show confirmation before sending to pending
    registration_text = (
        "📋 ያስገቡት መረጃ:\n\n"
        f"📝 ሙሉ ስም: {context.user_data.get('full_name', 'የለም')}\n\n"
        f"📱 ስልክ ቁጥር: {context.user_data.get('phone_number', 'የለም')}\n\n"
        f"📍 የመላኪያ ቦታ: {location}\n\n"
        "✅ መረጃውን ያረጋግጡ።\n\n"
        "🔄 ትክክል ከሆነ 'ትክክል ነዋ' ይምረጡ፣ ካልሆነ 'አስተካክል' ይምረጡ።"
    )
    keyboard = [['ትክክል ነዋ', 'አስተካክል'], ['ሰርዝ', 'ተመለስ']]
    await update.message.reply_text(
        registration_text,
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    )
    return CONFIRM_LOCATION

# Confirm location before sending to pending
async def confirm_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    choice = update.message.text
    if choice == 'ተመለስ':
        return await back_to_main(update, context)
    elif choice == 'ሰርዝ':
        context.user_data.clear()
        await update.message.reply_text(
            "❌ ምዝገባ ተሰርዟል።\n\n🔙 ወደ መነሻ ገጽ!",
            reply_markup=get_main_keyboard(user.id)
        )
        return MAIN_MENU
    elif choice == 'አስተካክል':
        # Go back to edit name
        await update.message.reply_text(
            "📝 እባክዎ ሙሉ ስምዎን ያስገቡ።\n\n"
            "🚀 ስምዎን ያስገቡ!",
            reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
        )
        return REGISTER_NAME
    elif choice == 'ትክክል ነዋ':
        location = context.user_data.get('location')
        conn = None
        cur = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            # Insert into pending_locations
            cur.execute(
                "INSERT INTO public.pending_locations (user_id, location_text) VALUES (%s, %s) RETURNING id",
                (user.id, location)
            )
            pending_id = cur.fetchone()[0]
            conn.commit()
            # Notify admins
            for admin_id in ADMIN_IDS:
                try:
                    keyboard = [
                        [InlineKeyboardButton("አረጋግጥ", callback_data=f"approve_location_{pending_id}"),
                         InlineKeyboardButton("ውድቅ አድርግ", callback_data=f"reject_location_{pending_id}")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"🔔 አዲስ ቦታ ጥያቆ ከተጠቃሚ {user.id} ({context.user_data.get('full_name', 'የለም')}):\n\n📍 {location}\n\n🔧 ለማረጋገጥ ወይም ለመሰረዝ ይመርጡ!",
                        reply_markup=reply_markup
                    )
                except Exception as e:
                    logger.error(f"Error notifying admin {admin_id} about location {pending_id}: {e}")
            await update.message.reply_text(
                "📤 ቦታዎ ተልኳል።\n\n"
                "⏳ ከአስተዳዳሪው ማረጋገጫን በትክክል ይጠብቁ።\n\n"
                "🚀 በትክክል ይጠብቁ!",
                reply_markup=get_main_keyboard(user.id)
            )
            context.user_data['pending_location_id'] = pending_id
            return WAIT_LOCATION_APPROVAL
        except Exception as e:
            logger.error(f"Error saving location for user {user.id}: {e}")
            await update.message.reply_text("❌ ቦታ በማስቀመጥ ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!")
            return CONFIRM_LOCATION
        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()
    else:
        await update.message.reply_text(
            "❌ እባክዎ 'ትክክል ነዋ' ወይም 'አስተካክል' ይምረጡ።\n\n"
            "🔄 ትክክለኛ ምርጫ ይምረጡ!",
            reply_markup=ReplyKeyboardMarkup(
                [['ትክክል ነዋ', 'አስተካክል'], ['ሰርዝ', 'ተመለስ']],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        return CONFIRM_LOCATION

# Wait for location approval
async def wait_location_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT status FROM public.pending_locations WHERE user_id = %s ORDER BY created_at DESC LIMIT 1",
            (user.id,)
        )
        pending = cur.fetchone()
        if pending and pending[0] == 'approved':
            choice = update.message.text
            if choice in ['🍽️ የምሳ', '🥘 የእራት']:
                return await choose_plan(update, context)
            else:
                await update.message.reply_text(
                    "✅ ቦታዎ ተቀበለ!\n\n"
                    "📦 የምዝገባ እቅድዎን ይምረጡ:\n\n"
                    "🍽️ የምሳ\n\n"
                    "🥘 የእራት\n\n"
                    "🚀 እቅድ ይምረጡ!",
                    reply_markup=ReplyKeyboardMarkup(
                        [['🍽️ የምሳ', '🥘 የእራት'], ['🔙 ተመለስ']],
                        resize_keyboard=True
                    )
                )
                return CHOOSE_PLAN
        else:
            await update.message.reply_text(
                "⏳ ቦታዎ ለማረጋገጥ በመጠበቅ ላይ ነው።\n\n"
                "🏠 ወደ መነሻ ገጽ ተመልሱ።\n\n"
                "🔄 እባክዎ ይጠብቁ!",
                reply_markup=get_main_keyboard(user.id)
            )
            return MAIN_MENU
    except Exception as e:
        logger.error(f"Error in wait_location_approval for user {user.id}: {e}")
        await update.message.reply_text(
            "⏳ ቦታዎ ለማረጋገጥ በመጠበቅ ላይ ነው።\n\n"
            "🏠 ወደ መነሻ ገጽ ተመልሱ።\n\n"
            "🔄 እባክዎ ይጠብቁ!",
            reply_markup=get_main_keyboard(user.id)
        )
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Confirm registration
async def confirm_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    choice = update.message.text
    if choice == '🔙 ተመለስ':
        return await back_to_main(update, context)
    elif choice == '⛔ አስተካክል':
        context.user_data.clear()
        await update.message.reply_text(
            "📝 እባክዎ ሙሉ ስምዎን ያስገቡ።\n\n"
            "🚀 ስምዎን ያስገቡ!",
            reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
        )
        return REGISTER_NAME
    elif choice == '✅ መረጃው ትክክል ነው ቀጥል':
        if user.id in ADMIN_IDS:
            await update.message.reply_text(
                "✅ ምዝገባ ተጠናቅቋል!\n\n"
                "🔐 እንደ አስተዳዳሪ ወደ ዋና ገጽ ተመልከት።\n\n"
                "🚀 አስተዳዳሪ ተመልከት!",
                reply_markup=get_main_keyboard(user.id)
            )
            return MAIN_MENU
        else:
            await update.message.reply_text(
                "📦 የምዝገባ እቅድዎን ይምረጡ:\n\n"
                "🍽️ የምሳ\n\n"
                "🥘 የእራት\n\n"
                "🚀 እቅድ ይምረጡ!",
                reply_markup=ReplyKeyboardMarkup(
                    [['🍽️ የምሳ', '🥘 የእራት'], ['🔙 ተመለስ']],
                    resize_keyboard=True
                )
            )
            return CHOOSE_PLAN
    else:
        await update.message.reply_text(
            "❌ እባክዎ '✅ መረጃው ትክክል ነው ቀጥል' ወይም '⛔ አስተካክል' ይምረጡ።\n\n"
            "🔄 ትክክለኛ ምርጫ ይምረጡ!",
            reply_markup=ReplyKeyboardMarkup(
                [['✅ መረጃው ትክክል ነው ቀጥል', '⛔ አስተካክል'], ['🔙 ተመለስ']],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        return CONFIRM_REGISTRATION

# Choose subscription plan
async def choose_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if has_pending_location(user.id):
        await update.message.reply_text("⏳ ቦታዎ ለማረጋገጥ በመጠበቅ ላይ ነው። እባክዎ ይጠብቁ።\n\n🔄 እባክዎ ይጠብቁ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    if user.id in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪዎች ምዝገባ አያስፈልጋቸውም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    choice = update.message.text
    if choice == '/subscribe' or '🛒' in choice:
        await update.message.reply_text(
            "📦 የምዝገባ እቅድዎን ይምረጡ:\n\n"
            "🍽️ የምሳ\n\n"
            "🥘 የእራት\n\n"
            "🚀 እቅድ ይምረጡ!",
            reply_markup=ReplyKeyboardMarkup(
                [['🍽️ የምሳ', '🥘 የእራት'], ['🔙 ተመለስ']],
                resize_keyboard=True
            )
        )
        return CHOOSE_PLAN
    plans = {
        '🍽️ የምሳ': {'type': 'lunch', 'price_per_meal': 0, 'duration_days': 30},
        '🥘 የእራት': {'type': 'dinner', 'price_per_meal': 0, 'duration_days': 30}
    }
    if choice == '🔙 ተመለስ':
        return await back_to_main(update, context)
    if choice not in plans:
        await update.message.reply_text(
            "❌ የማይሰርአ ምርጫ።\n\n"
            "📦 እባክዎ '🍽️ የምሳ' ወይም '🥘 የእራት' ይምረጡ።\n\n"
            "🔄 ትክክለኛ ምርጫ ይምረጡ!",
            reply_markup=ReplyKeyboardMarkup(
                [['🍽️ የምሳ', '🥘 የእራት'], ['🔙 ተመለስ']],
                resize_keyboard=True
            )
        )
        return CHOOSE_PLAN
    context.user_data['plan'] = plans[choice]
    await update.message.reply_text(
        "📅 ለምግቦችዎ ቀናት ይምረጡ (ከዛሬ እስከ ሳምንት መጨረሻ):\n\n"
        "🔄 ቀናት ይምረጡ!",
        reply_markup=ReplyKeyboardMarkup(
            [['ሰኞ', 'ማክሰኞ', 'እሮብ'],
             ['ሐሙስ', 'አርብ', 'ቅዳሜ'],
             ['እሑድ', 'ጨርስ', '🔙 ተመለስ']],
            resize_keyboard=True
        )
    )
    context.user_data['selected_dates'] = []
    return CHOOSE_DATE

# Choose dates
async def choose_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if has_pending_location(user.id):
        await update.message.reply_text("⏳ ቦታዎ ለማረጋገጥ በመጠበቅ ላይ ነው። እባክዎ ይጠብቁ።\n\n🔄 እባክዎ ይጠብቁ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    if user.id in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪዎች ምዝገባ አያስፈልጋቸውም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    choice = update.message.text
    valid_days_am = ['ሰኞ', 'ማክሰኞ', 'እሮብ', 'ሐሙስ', 'አርብ', 'ቅዳሜ', 'እሑድ']
    valid_days_en = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    current_weekday = datetime.now(EAT).weekday()
    days_to_show = [valid_days_am[i] for i in range(current_weekday, 7)]
    if choice == '🔙 ተመለስ':
        await update.message.reply_text(
            "📦 የምዝገባ እቅድዎን ይምረጡ:\n\n"
            "🍽️ የምሳ\n\n"
            "🥘 የእራት\n\n"
            "🚀 እቅድ ይምረጡ!",
            reply_markup=ReplyKeyboardMarkup(
                [['🍽️ የምሳ', '🥘 የእራት'], ['🔙 ተመለስ']],
                resize_keyboard=True
            )
        )
        return CHOOSE_PLAN
    elif choice == 'ጨርስ':
        selected_dates = context.user_data.get('selected_dates', [])
        if not selected_dates:
            # Rebuild keyboard for available days
            keyboard = [days_to_show[i:i+3] for i in range(0, len(days_to_show), 3)]
            keyboard.append(['ጨርስ', '🔙 ተመለስ'])
            await update.message.reply_text(
                "❌ ቢያንስ አንድ ቀን ይምረጠው።\n\n"
                "📅 ቢያንስ አንድ ቀን ይምረጠው!\n\n"
                "🔄 ቀናት ይምረጠው!",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            )
            return CHOOSE_DATE
        conn = None
        cur = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            plan = context.user_data.get('plan')
            expiry_date = datetime.now(EAT) + timedelta(days=plan['duration_days'])
            selected_dates_en_list = [valid_days_en[valid_days_am.index(day)] for day in selected_dates]
            cur.execute(
                "INSERT INTO public.subscriptions (user_id, plan_type, meals_remaining, selected_dates, expiry_date, status) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (user.id, plan['type'], len(selected_dates), json.dumps(selected_dates_en_list), expiry_date, 'pending')
            )
            subscription_id = cur.fetchone()[0]
            conn.commit()
            context.user_data['subscription_id'] = subscription_id
            # Auto proceed to meal selection
            # Fetch current menu
            today = datetime.now(EAT).date()
            week_start = today - timedelta(days=today.weekday())
            cur.execute("SELECT menu_items FROM public.weekly_menus WHERE week_start_date = %s", (week_start,))
            menu_result = cur.fetchone()
            if menu_result and menu_result[0]:
                menu_items_from_db = json.loads(menu_result[0]) if isinstance(menu_result[0], str) else menu_result[0]
                valid_menu_items = [
                    item for item in menu_items_from_db 
                    if isinstance(item, dict) and all(key in item for key in ['id', 'name', 'price', 'category'])
                ]
                if valid_menu_items:
                    menu_items = valid_menu_items
                else:
                    menu_items = default_menu
            else:
                menu_items = default_menu
            context.user_data['menu_items'] = menu_items
            context.user_data['meals_remaining'] = len(selected_dates)
            context.user_data['selected_dates'] = selected_dates
            context.user_data['selected_dates_en'] = selected_dates_en_list
            context.user_data['week_start'] = week_start
            context.user_data['selected_meals'] = {day: [] for day in selected_dates}
            context.user_data['current_day_index'] = 0
            first_day = selected_dates[0]
            fasting_items = [item for item in menu_items if item['category'] == 'fasting']
            non_fasting_items = [item for item in menu_items if item['category'] == 'non_fasting']
            menu_text = (
                f"📜 ለ{first_day} ምግብ ይምረጠው:\n\n"
                f"📅 የተመረጠው ቀናት: {', '.join(selected_dates)}\n\n"
                f"🍽 ቀሪ ምግቦች: {len(selected_dates)}\n\n"
                f"🍲 የጾም ምግብ ዝርዝር:\n\n"
            )
            for idx, item in enumerate(fasting_items, 1):
                menu_text += f"{idx}. {item['name']} - {item['price']:.2f} ብር\n\n"
            menu_text += "🍖 የፍስክ ምግብ ዝርዝር:\n\n"
            for idx, item in enumerate(non_fasting_items, 1):
                menu_text += f"{idx + len(fasting_items)}. {item['name']} - {item['price']:.2f} ብር\n\n"
            menu_text += (
                f"📝 ለ{first_day} የምግብ ቁጥል ያስገቡ (ለምሳሌ፣ '1')።\n\n"
                "🚫 ለመሰረዝ 'ሰርዝ' ይፃፉ።"
            )
            await update.message.reply_text(
                menu_text,
                reply_markup=ReplyKeyboardMarkup([['ጨርስ'], ['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
            )
            context.user_data['menu_shown'] = True
            return MEAL_SELECTION
        except Exception as e:
            logger.error(f"Error saving subscription for user {user.id}: {e}")
            await update.message.reply_text(
                "❌ ምዝገባ በማስኬድ ላይ ስህተት።\n\n"
                "💬 እባክዎ እንደገና ይሞክሩ ወይም ድጋፍ ያነጋግሩ።\n\n"
                "🔄 እንደገና ይሞክሩ!",
                reply_markup=ReplyKeyboardMarkup(
                    [['ሰኞ', 'ማክሰኞ', 'እሮብ'],
                     ['ሐሙስ', 'አርብ', 'ቅዳሜ'],
                     ['እሑድ', 'ጨርስ', '🔙 ተመለስ']],
                    resize_keyboard=True
                )
            )
            return CHOOSE_DATE
        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()
    elif choice in valid_days_am[current_weekday:]:
        selected_dates = context.user_data.get('selected_dates', [])
        if choice in selected_dates:
            # Rebuild keyboard
            keyboard = [days_to_show[i:i+3] for i in range(0, len(days_to_show), 3)]
            keyboard.append(['ጨርስ', '🔙 ተመለስ'])
            await update.message.reply_text(
                f"❌ {choice} ቀደም ብሎ ታክሏል።\n\n"
                "📅 እባክዎ ሌላ ቀን ይምረጠው ወይም 'ጨርስ' ይጫኑ።\n\n"
                "🔄 ቀናት ይምረጠው!",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            )
            return CHOOSE_DATE
        selected_dates.append(choice)
        context.user_data['selected_dates'] = selected_dates
        # Rebuild keyboard
        keyboard = [days_to_show[i:i+3] for i in range(0, len(days_to_show), 3)]
        keyboard.append(['ጨርስ', '🔙 ተመለስ'])
        await update.message.reply_text(
            f"✅ {choice} ተታክሏል።\n\n"
            "📅 ተጨማሪ ቀና቉ ይምረጠው ወይም 'ጨርስ' ይጫኑ።\n\n"
            "🚀 ቀናት ይምረጠው!",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return CHOOSE_DATE
    else:
        # Rebuild keyboard
        keyboard = [days_to_show[i:i+3] for i in range(0, len(days_to_show), 3)]
        keyboard.append(['ጨርስ', '🔙 ተመለስ'])
        await update.message.reply_text(
            "❌ የማይሰራ ምርጫ።\n\n"
            "📅 እባክዎ ቀን ወይም 'ጨርስ' ይምረጠው።\n\n"
            "🔄 ትክክለኛ ምርጫ ይምረጠው!",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return CHOOSE_DATE

# Show weekly menu
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if has_pending_location(user.id):
        await update.message.reply_text("⏳ ቦታዎ ለማረጋገጥ በመጠበቅ ላይ ነው። እባክዎ ይጠብቁ።\n\n🔄 እባክዎ ይጠብቁ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        today = datetime.now(EAT).date()
        week_start = today - timedelta(days=today.weekday())
        cur.execute("SELECT menu_items FROM public.weekly_menus WHERE week_start_date = %s", (week_start,))
        menu_result = cur.fetchone()
        if menu_result and menu_result[0]:
            menu_items = json.loads(menu_result[0]) if isinstance(menu_result[0], str) else menu_result[0]
            valid_items = [
                item for item in menu_items 
                if isinstance(item, dict) and all(key in item for key in ['id', 'name', 'price', 'category'])
            ]
        else:
            valid_items = [
                item for item in default_menu 
                if isinstance(item, dict) and all(key in item for key in ['id', 'name', 'price', 'category'])
            ]
        if not valid_items:
            await update.message.reply_text(
                "❌ በዚህ ሳምንት የታቀዘ ምግቦች የሉም።\n\n"
                "🔄 እባክዎ እንደገና ይሞክሩ!",
                reply_markup=get_main_keyboard(update.effective_user.id)
            )
            return MAIN_MENU
        menu_text = f"📋 የምግብ ዝርዝር ለሳምንቱ መጀመሪያ {week_start}:\n\n"
        menu_text += "🍲 የጾም ምግብ ዝርዝር\n\n"
        fasting_items = [item for item in valid_items if item['category'] == 'fasting']
        for idx, item in enumerate(fasting_items, 1):
            menu_text += f"{idx}. {item['name']} …….. {item['price']:.2f} ብር\n\n"
        menu_text += "🍖 የፍስክ ምግብ ዝርዝር\n\n"
        non_fasting_items = [item for item in valid_items if item['category'] == 'non_fasting']
        for idx, item in enumerate(non_fasting_items, 1):
            menu_text += f"{idx + len(fasting_items)}. {item['name']} …….. {item['price']:.2f} ብር\n\n"
        menu_text += "🍴 ምግቦችዎን ለመምረጥ /select_meals ይጠቀሙ።\n\n🚀 ምግቦችን ይምረጡ!"
        await update.message.reply_text(menu_text, reply_markup=get_main_keyboard(update.effective_user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error in show_menu: {e}")
        await update.message.reply_text("❌ ምግብ ዝርዝር መጫን ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!", reply_markup=get_main_keyboard(update.effective_user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Select meals
async def select_meals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if has_pending_location(user.id):
        await update.message.reply_text("⏳ ቦታዎ ለማረጋገጥ በመጠበቅ ላይ ነው። እባክዎ ይጠብቁ።\n\n🔄 እባክዎ ይጠብቁ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    if user.id in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪዎች ምግብ ምርጫ አያስፈልጋቸውም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, plan_type, meals_remaining, selected_dates FROM public.subscriptions WHERE user_id = %s AND status IN ('pending', 'active')",
            (user.id,)
        )
        subscription = cur.fetchone()
        if not subscription:
            await update.message.reply_text(
                "❌ ምግቦችን ለመምረጥ ምዝገባ ያስፈልጋል።\n\n"
                "🛒 /subscribe ይጠቀሙ።\n\n"
                "🚀 ምዝገባ ይጀምሩ!",
                reply_markup=get_main_keyboard(user.id)
            )
            return MAIN_MENU
        subscription_id, plan_type, meals_remaining, selected_dates_json = subscription
        selected_dates_en = json.loads(selected_dates_json) if isinstance(selected_dates_json, str) else selected_dates_json
        if meals_remaining <= 0 or not selected_dates_en:
            await update.message.reply_text(
                "❌ በምዝገባዎ ውስጥ ምንም ቀሪ ምግቦች ወይም የተመረጡ ቀን የሉም።\n\n"
                "🛒 እባክዎ አዲስ እቅድ ይመዝገቡ።\n\n"
                "🔄 እንደገና ይሞክሩ!",
                reply_markup=get_main_keyboard(user.id)
            )
            return MAIN_MENU
        valid_days_en = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        valid_days_am = ['ሰኞ', 'ማክሰኞ', 'እሮብ', 'ሐሙስ', 'አርብ', 'ቅዳሜ', 'እሑድ']
        selected_dates = [valid_days_am[valid_days_en.index(day)] for day in selected_dates_en]
        # Fetch current menu
        today = datetime.now(EAT).date()
        week_start = today - timedelta(days=today.weekday())
        cur.execute("SELECT menu_items FROM public.weekly_menus WHERE week_start_date = %s", (week_start,))
        menu_result = cur.fetchone()
        if menu_result and menu_result[0]:
            menu_items_from_db = json.loads(menu_result[0]) if isinstance(menu_result[0], str) else menu_result[0]
            valid_menu_items = [
                item for item in menu_items_from_db 
                if isinstance(item, dict) and all(key in item for key in ['id', 'name', 'price', 'category'])
            ]
            if valid_menu_items:
                menu_items = valid_menu_items
            else:
                menu_items = default_menu
        else:
            menu_items = default_menu
        context.user_data['subscription_id'] = subscription_id
        context.user_data['menu_items'] = menu_items
        context.user_data['meals_remaining'] = meals_remaining
        context.user_data['selected_dates'] = selected_dates
        context.user_data['selected_dates_en'] = selected_dates_en
        context.user_data['week_start'] = week_start
        context.user_data['selected_meals'] = {day: [] for day in selected_dates}
        context.user_data['current_day_index'] = 0
        first_day = selected_dates[0]
        fasting_items = [item for item in menu_items if item['category'] == 'fasting']
        non_fasting_items = [item for item in menu_items if item['category'] == 'non_fasting']
        menu_text = (
            f"📜 ምግብ ዝርዝር (ለሁሉም ቀናት ይተገበራል):\n\n"
            f"📅 የተመረጡ ቀናት: {', '.join(selected_dates)}\n\n"
            f"🍽 ቀሪ ምግቦች: {meals_remaining}\n\n"
            "🍲 የጾም ምግብ ዝርዝር:\n\n"
        )
        for idx, item in enumerate(fasting_items, 1):
            menu_text += f"{idx}. {item['name']} - {item['price']:.2f} ብር\n\n"
        menu_text += "🍖 የፍስክ ምግብ ዝርዝር:\n\n"
        for idx, item in enumerate(non_fasting_items, 1):
            menu_text += f"{idx + len(fasting_items)}. {item['name']} - {item['price']:.2f} ብር\n\n"
        menu_text += (
            f"📝 ለ{first_day} የምግብ ቁጥል ያስገቡ (ለምሳሌ፣ '1')።\n\n"
            "🚫 ለመሰረዝ 'ሰርዝ' ይፃፉ።"
        )
        await update.message.reply_text(
            menu_text,
            reply_markup=ReplyKeyboardMarkup([['ጨርስ'], ['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
        )
        context.user_data['menu_shown'] = True
        return MEAL_SELECTION
    except Exception as e:
        logger.error(f"Error starting meal selection for user {user.id}: {e}")
        await update.message.reply_text("❌ ምግቦችን መጫን ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

async def process_meal_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if has_pending_location(user.id):
        await update.message.reply_text("⏳ ቦታዎ ለማረጋገጥ በመጠበቅ ላይ ነው። እባክዎ ይጠብቁ።\n\n🔄 እባክዎ ይጠብቁ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    text = update.message.text.strip()
    menu_items = context.user_data.get('menu_items', [])
    selected_dates = context.user_data.get('selected_dates', [])
    selected_dates_en = context.user_data.get('selected_dates_en', [])
    week_start = context.user_data.get('week_start')
    current_day_index = context.user_data.get('current_day_index', 0)
    valid_days_en = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    if not all([menu_items, selected_dates, selected_dates_en, week_start]):
        await update.message.reply_text(
            "❌ የክፍል-ጊዜ ማብቂያ ወይም ምግብ ዝርዝር የለም።\n\n"
            "🍴 እባክዎ ከ /select_meals ጋር እንደገና ይጀምሩ።\n\n"
            "🔄 እንደገና ይጀምሩ!",
            reply_markup=get_main_keyboard(user.id)
        )
        context.user_data.clear()
        return MAIN_MENU
    if text == 'ሰርዝ':
        await update.message.reply_text(
            "❌ የምግብ ምርጫ ተሰርዟል።\n\n"
            "🔙 ወደ መነሻ ገጽ!",
            reply_markup=get_main_keyboard(user.id)
        )
        context.user_data.clear()
        return MAIN_MENU
    if text == '🔙 ተመለስ':
        return await back_to_main(update, context)
    current_day = selected_dates[current_day_index]
    current_day_en = selected_dates_en[current_day_index]
    if current_day_en not in valid_days_en:
        logger.error(f"Invalid day: {current_day_en}")
        await update.message.reply_text(
            "❌ የተመረጡ ቀናት ስህተት።\n\n"
            "🍴 እባክዎ ከ /select_meals ጋር እንደገና ይጀምሩ።\n\n"
            "🔄 እንደገና ይጀምሩ!",
            reply_markup=get_main_keyboard(user.id)
        )
        context.user_data.clear()
        return MAIN_MENU
    selected_meals = context.user_data.get('selected_meals', {current_day: []})
    if text == 'ጨርስ':
        if len(selected_meals.get(current_day, [])) == 0:
            menu_shown = context.user_data.get('menu_shown', False)
            prompt = f"❌ ለ{current_day} ቢያንስ አንድ ምግብ ይምረጠው።\n\n"
            if menu_shown:
                prompt += f"🔢 ለ{current_day} ቁጥል ያስገቡ (1-{len(menu_items)}):\n\n"
            else:
                fasting_items = [item for item in menu_items if item['category'] == 'fasting']
                non_fasting_items = [item for item in menu_items if item['category'] == 'non_fasting']
                prompt += "🍲 የጾም ምግብ ዝርዝር:\n\n"
                for idx, item in enumerate(fasting_items, 1):
                    prompt += f"{idx}. {item['name']} - {item['price']:.2f} ብር\n\n"
                prompt += "🍖 የፍስክ ምግብ ዝርዝር:\n\n"
                for idx, item in enumerate(non_fasting_items, 1):
                    prompt += f"{idx + len(fasting_items)}. {item['name']} - {item['price']:.2f} ብር\n\n"
                prompt += f"📝 ለ{current_day} የምግብ ቁጥል ያስገቡ (ለምሳሌ፣ '1')።\n\n"
            await update.message.reply_text(
                prompt,
                reply_markup=ReplyKeyboardMarkup([['ጨርስ'], ['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
            )
            return MEAL_SELECTION
        # Proceed to next day
        context.user_data['current_day_index'] = current_day_index + 1
        if current_day_index + 1 >= len(selected_dates):
            return await confirm_meal_selection(update, context)
        next_day = selected_dates[current_day_index + 1]
        menu_shown = context.user_data.get('menu_shown', False)
        if menu_shown:
            next_prompt = (
                f"📅 ለ{next_day} ምግብ ቁጥል ያስገቡ (1-{len(menu_items)}):\n\n"
                f"🚫 ለመሰረዝ 'ሰርዝ' ይፃፉ።"
            )
        else:
            fasting_items = [item for item in menu_items if item['category'] == 'fasting']
            non_fasting_items = [item for item in menu_items if item['category'] == 'non_fasting']
            next_prompt = (
                f"📜 ለ{next_day} ምግብ ይምረጠው:\n\n"
                "🍲 የጾም ምግብ ዝርዝር:\n\n"
            )
            for idx, item in enumerate(fasting_items, 1):
                next_prompt += f"{idx}. {item['name']} - {item['price']:.2f} ብር\n\n"
            next_prompt += "🍖 የፍስክ ምግብ ዝርዝር:\n\n"
            for idx, item in enumerate(non_fasting_items, 1):
                next_prompt += f"{idx + len(fasting_items)}. {item['name']} - {item['price']:.2f} ብር\n\n"
            next_prompt += (
                f"📝 ለ{next_day} የምግብ ቁጥል ያስገቡ (ለምሳሌ፣ '1')።\n\n"
                "🚫 ለመሰረዝ 'ሰርዝ' ይፃፉ።"
            )
            context.user_data['menu_shown'] = True
        await update.message.reply_text(
            next_prompt,
            reply_markup=ReplyKeyboardMarkup([['ጨርስ'], ['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
        )
        return MEAL_SELECTION
    try:
        item_idx = int(text) - 1
        if 0 <= item_idx < len(menu_items):
            item = menu_items[item_idx]
            meal_date = week_start + timedelta(days=valid_days_en.index(current_day_en))
            selected_meals[current_day] = [{
                'day': current_day,
                'day_en': current_day_en,
                'item': item,
                'meal_date': meal_date
            }]
            context.user_data['selected_meals'] = selected_meals
            await update.message.reply_text(
                f"✅ ለ{current_day} {item['name']} ቉ተመረጠ።"
            )
            # Auto proceed to next day
            context.user_data['current_day_index'] = current_day_index + 1
            if current_day_index + 1 >= len(selected_dates):
                return await confirm_meal_selection(update, context)
            next_day = selected_dates[current_day_index + 1]
            menu_shown = context.user_data.get('menu_shown', False)
            if menu_shown:
                next_prompt = (
                    f"📅 ለ{next_day} ምግብ ቁጥል ያስገቡ (1-{len(menu_items)}):\n\n"
                    f"🚫 ለመሰረዝ 'ሰርዝ' ይፃፉ።"
                )
            else:
                fasting_items = [item for item in menu_items if item['category'] == 'fasting']
                non_fasting_items = [item for item in menu_items if item['category'] == 'non_fasting']
                next_prompt = (
                    f"📜 ለ{next_day} ምግብ ይምረጠው:\n\n"
                    "🍲 የጾም ምግብ ዝርዝር:\n\n"
                )
                for idx, item in enumerate(fasting_items, 1):
                    next_prompt += f"{idx}. {item['name']} - {item['price']:.2f} ብር\n\n"
                next_prompt += "🍖 የፍስክ ምግብ ዝርዝር:\n\n"
                for idx, item in enumerate(non_fasting_items, 1):
                    next_prompt += f"{idx + len(fasting_items)}. {item['name']} - {item['price']:.2f} ብር\n\n"
                next_prompt += (
                    f"📝 ለ{next_day} የምግብ ቁጥል ያስገቡ (ለምሳሌ፣ '1')።\n\n"
                    "🚫 ለመሰረዝ 'ሰርዝ' ይፃፉ።"
                )
                context.user_data['menu_shown'] = True
            await update.message.reply_text(
                next_prompt,
                reply_markup=ReplyKeyboardMarkup([['ጨርስ'], ['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
            )
            return MEAL_SELECTION
        else:
            menu_shown = context.user_data.get('menu_shown', False)
            error_prompt = f"❌ የማይሰራ የምግብ ቁጥል {text}።\n\n"
            if menu_shown:
                error_prompt += f"🔢 1 እስከ {len(menu_items)} መካከል ይምረጠውፍፍ።\n\n"
            else:
                fasting_items = [item for item in menu_items if item['category'] == 'fasting']
                non_fasting_items = [item for item in menu_items if item['category'] == 'non_fasting']
                error_prompt += "🍲 የጾም ምግብ ዝርዝር:\n\n"
                for idx, item in enumerate(fasting_items, 1):
                    error_prompt += f"{idx}. {item['name']} - {item['price']:.2f} ብር\n\n"
                error_prompt += "🍖 የፍስክ ምግብ ዝርዝር:\n\n"
                for idx, item in enumerate(non_fasting_items, 1):
                    error_prompt += f"{idx + len(fasting_items)}. {item['name']} - {item['price']:.2f} ብር\n\n"
                error_prompt += f"🔢 1 እስከ {len(menu_items)} መካከል ይምረጠው።\n\n"
                context.user_data['menu_shown'] = True
            error_prompt += "🔄 ትክክለኛ ቁጥል ያስገቡ!"
            await update.message.reply_text(
                error_prompt,
                reply_markup=ReplyKeyboardMarkup([['ጨርስ'], ['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
            )
            return MEAL_SELECTION
    except ValueError:
        menu_shown = context.user_data.get('menu_shown', False)
        error_prompt = f"❌ የማይሰራ ጊዛ '{text}'።\n\n"
        if menu_shown:
            error_prompt += f"🔢 ንጥል ያስገቡ (ለምሳሌ '1' 1-{len(menu_items)} መካከል):\n\n"
        else:
            fasting_items = [item for item in menu_items if item['category'] == 'fasting']
            non_fasting_items = [item for item in menu_items if item['category'] == 'non_fasting']
            error_prompt += "🍲 የጾም ምግብ ዝርዝር:\n\n"
            for idx, item in enumerate(fasting_items, 1):
                error_prompt += f"{idx}. {item['name']} - {item['price']:.2f} ብር\n\n"
            error_prompt += "🍖 የፍስክ ምግብ ዝርዝር:\n\n"
            for idx, item in enumerate(non_fasting_items, 1):
                error_prompt += f"{idx + len(fasting_items)}. {item['name']} - {item['price']:.2f} ብር\n\n"
            error_prompt += f"🔢 ንጥል ያስገቡ (ለምሳሌ '1' 1-{len(menu_items)} መካከል):\n\n"
            context.user_data['menu_shown'] = True
        error_prompt += "🔄 ትክክለኛ ንጥል ያስገቡ!"
        await update.message.reply_text(
            error_prompt,
            reply_markup=ReplyKeyboardMarkup([['ጨርስ'], ['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
        )
        return MEAL_SELECTION

async def confirm_meal_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    selected_meals = context.user_data.get('selected_meals', {})
    total_price = 0
    order_text = "📋 የመረጡት ቀን እና ምግብ ዝርዝር\n\n"
    valid_days_en = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    for day in selected_meals:
        for selection in selected_meals[day]:
            item = selection['item']
            meal_date = selection['meal_date'].strftime('%Y/%m/%d')
            order_text += f"- {day} ({meal_date}): {item['name']} - {item['price']:.2f} ብር\n\n"
            total_price += item['price']
    order_text += f"💰 ጠቅላላ ዋጋ: {total_price:.2f} ብር\n\n"
    order_text += "✅ ምርጫውን ያረጋግጡ?\n\n🚀 ያረጋግጡ!"
    context.user_data['total_price'] = total_price
    await update.message.reply_text(
        order_text,
        reply_markup=ReplyKeyboardMarkup(
            [['✅ የምግብ ዝርዝሩ ትክክል ነዋ', '⛔ አስተካክል'], ['ሰርዝ', '🔙 ተመለስ']],
            resize_keyboard=True
        )
    )
    return CONFIRM_MEAL

async def confirm_meal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if has_pending_location(user.id):
        await update.message.reply_text("⏳ ቦታዎ ለማረጋገጥ በመጠበቅ ላይ ነው።\n\n🔄 እባክዎ ይጠብቁ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    if update.message.text and update.message.text.lower() in ['ሰርዝ', '🔙 ተመለስ']:
        context.user_data.clear()
        await update.message.reply_text(
            "❌ ሥራ ተሰርዟል።\n\n"
            "🔙 ወደ መነሻ ገጽ!",
            reply_markup=get_main_keyboard(user.id)
        )
        return MAIN_MENU
    if update.message.text == '⛔ አስተካክል':
        context.user_data['current_day_index'] = 0
        context.user_data['selected_meals'] = {day: [] for day in context.user_data['selected_dates']}
        selected_dates = context.user_data.get('selected_dates', [])
        if not selected_dates:
            await update.message.reply_text(
                "❌ ምንም ቀናት አልተመረጡም።\n\n"
                "🍴 እባክዎ ከ /select_meals ጋር እንደገና ይጀምሩ።\n\n"
                "🔄 እንደገና ይጀምሩ!",
                reply_markup=get_main_keyboard(user.id)
            )
            context.user_data.clear()
            return MAIN_MENU
        menu_items = context.user_data.get('menu_items', default_menu)
        menu_shown = context.user_data.get('menu_shown', False)
        fasting_items = [item for item in menu_items if item['category'] == 'fasting']
        non_fasting_items = [item for item in menu_items if item['category'] == 'non_fasting']
        menu_text = (
            f"📜 ለመረጡት ቀናት ምግቦች እንደገና ይምረጠው:\n\n"
            f"📅 የተመረጠው ቀናት: {', '.join(selected_dates)}\n\n"
        )
        if not menu_shown:
            menu_text += "🍲 የጾም ምግብ ዝርዝር:\n\n"
            for idx, item in enumerate(fasting_items, 1):
                menu_text += f"{idx}. {item['name']} - {item['price']:.2f} ብር\n\n"
            menu_text += "🍖 የፍስክ ምግብ ዝርዝር:\n\n"
            for idx, item in enumerate(non_fasting_items, 1):
                menu_text += f"{idx + len(fasting_items)}. {item['name']} - {item['price']:.2f} ብር\n\n"
            context.user_data['menu_shown'] = True
        menu_text += (
            f"📝 ለ{selected_dates[0]} የምግብ ቁጥል ያስገቡ (ለምሳሌ '1')።\n\n"
            "🚫 ለመሰረዝ 'ሰርዝ' ይፃፉ።"
        )
        await update.message.reply_text(
            menu_text,
            reply_markup=ReplyKeyboardMarkup([['ጨርስ'], ['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
        )
        return MEAL_SELECTION
    if update.message.text != '✅ የምግብ ዝርዝሩ ትክክል ነዋ':
        await update.message.reply_text(
            "❌ እባክዎ '✅ የምግብ ዝርዝሩ ትክክል ነዋ' ወይም '⛔ አስተካክል' ይምረጠው።",
            reply_markup=ReplyKeyboardMarkup(
                [['✅ የምግብ ዝርዝሩ ትክክል ነዋ', '⛔ አስተካክል'], ['ሰርዝ', '🔙 ተመለስ']],
                resize_keyboard=True
            )
        )
        return CONFIRM_MEAL
    if update.message.text == '✅ የምግብ ዝርዝሩ ትክክል ነዋ':
        total_price = context.user_data.get('total_price', 0)
        if total_price <= 0:
            raise ValueError("Invalid total price")
        order_text = f"💰 📝 📝 ጠቅላላ ዋጋ: {total_price:.2f} ብር\n\n"
        order_text += "💳 ክፍያ ማረጋገጫ ምስል ያስገቡ ለመቀጠል።\n\n"
        order_text += "📤 ምስል ያስገቡ!"
        await update.message.reply_text(
            order_text,
            reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
        )
        return PAYMENT_UPLOAD
    return CONFIRM_MEAL

async def payment_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if has_pending_location(user.id):
        await update.message.reply_text("⏳ ቦታዎ ለማረጋገጥ በመጠበቅ ላይ ነው።\n\n🔄 እባክዎ ይጠብቁ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    if update.message.text and update.message.text.lower() in ['ሰርዝ', '🔙 ተመለስ']:
        await update.message.reply_text(
            "❌ ምዝገባ ተሰርዟል።\n\n"
            "🔙 ወደ መነሻ ገጽ!",
            reply_markup=get_main_keyboard(user.id)
        )
        context.user_data.clear()
        return MAIN_MENU
    if not update.message.photo:
        await update.message.reply_text(
            "❌ የክፍላ ማረጋገጫ ምስል ያስገቡ።\n\n"
            "📤 ምስል ያስገቡ!\n\n"
            "🔄 እባክዎ ምስል ያስገቡ!",
            reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
        )
        return PAYMENT_UPLOAD
    photo = update.message.photo[-1]
    receipt_url = photo.file_id
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        subscription_id = context.user_data.get('subscription_id')
        total_price = context.user_data.get('total_price', 0)
        if not subscription_id or total_price <= 0:
            logger.error(f"Missing or invalid subscription_id or total_price for user {user.id}")
            await update.message.reply_text(
                "❌ ስህተት: የመመዝገቢያዎ ወይም የክፍፍ መረጃዎ አይገኝም።\n\n"
                "🛒 እባክዎ ከ /subscribe ጋር እንደገና ይጀምሩ።\n\n"
                "🔄 እንደገና ይጀምሩ!",
                reply_markup=get_main_keyboard(user.id)
            )
            context.user_data.clear()
            return MAIN_MENU
        cur.execute(
            "INSERT INTO public.payments (user_id, subscription_id, amount, receipt_url, status) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (user.id, subscription_id, total_price, receipt_url, 'pending')
        )
        payment_id = cur.fetchone()[0]
        conn.commit()
        selected_meals = context.user_data.get('selected_meals', {})
        orders_by_date = {}
        for day in selected_meals:
            for selection in selected_meals[day]:
                meal_date = selection['meal_date']
                if meal_date not in orders_by_date:
                    orders_by_date[meal_date] = []
                orders_by_date[meal_date].append(selection['item'])
        for meal_date, items in orders_by_date.items():
            cur.execute(
                "INSERT INTO public.orders (user_id, subscription_id, meal_date, items, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                (user.id, subscription_id, meal_date, json.dumps(items), 'confirmed')
            )
        conn.commit()
        for admin_id in ADMIN_IDS:
            try:
                try:
                    await context.bot.send_photo(
                        chat_id=admin_id,
                        photo=receipt_url,
                        caption=f"🔔 ከተጠቃሚ {user.id} አዲስ ክፋ {total_price:.2f} ብር።\n\n"
                                f"💳 እባክዎ ይፈትሹ።\n\n"
                                "🔧 ለማረጋገጥ ወይም ለመሰረዝ ይመርጡ!",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("አረጋግጥ", callback_data=f"approve_payment_{payment_id}"),
                             InlineKeyboardButton("ውድቅ አድርግ", callback_data=f"reject_payment_{payment_id}")]
                        ])
                    )
                except Exception as e:
                    logger.error(f"Error sending photo to admin {admin_id} for payment {payment_id}: {e}")
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"🔔 ከተጠቃሚ {user.id} አዲስ ክፋ {total_price:.2f} ብር።\n\n"
                             f"⚠️ የማረጋገጫ ምስል መላክ አልተሳካም (ስህተት: {str(e)})።\n\n"
                             "🔗 የማረጋገጫ File ID: {receipt_url}\n\n"
                             "🔧 ለማረጋገጥ ወይም ለመሰረዝ ይመርጡ!",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("አረጋግጥ", callback_data=f"approve_payment_{payment_id}"),
                             InlineKeyboardButton("ውድቅ አድርግ", callback_data=f"reject_payment_{payment_id}")]
                        ])
                    )
            except Exception as e:
                logger.error(f"Error notifying admin {admin_id} for payment {payment_id}: {e}")
        order_text = f"🔔 ከተጠቃሚ {user.id} አዲስ ትዕዛዝ:\n\n"
        for day in selected_meals:
            for selection in selected_meals[day]:
                order_text += f"- {selection['meal_date'].strftime('%Y-%m-%d')}: {selection['item']['name']}\n\n"
        order_text += f"💰 ጠቅላላ: {total_price:.2f} ብር\n\n🔧 ትዕዛዝ ተቀበለ!"
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=order_text
                )
            except Exception as e:
                logger.error(f"Error notifying admin {admin_id} about new order: {e}")
        await update.message.reply_text(
            "📤 ክፋዎ ተልኳል።\n\n"
            "⏳ ከአስተዳዳሪው ማረጋገጫን በትክክል ይጠብቁ።\n\n"
            "🚀 በትክክል ይጠብቁ!",
            reply_markup=get_main_keyboard(user.id)
        )
        context.user_data.clear()
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error processing payment for user {user.id}: {e}")
        await update.message.reply_text(
            "❌ ማረጋገጫ በማስገባት ላይ ስህተት።\n\n"
            "🔄 እባክዎ እንደገና ይሞክሩ።",
            reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
        )
        return PAYMENT_UPLOAD
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Admin: Export PDF Orders Report (with Amharic support for food names only)
async def admin_export_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪ አይደሉም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Fetch all active/pending subscriptions (to handle multiple per user)
        cur.execute("""
            SELECT s.id, s.user_id, s.plan_type, s.meals_remaining, s.selected_dates, s.expiry_date, s.status, s.created_at as sub_created,
                   u.full_name, u.username, u.phone_number, u.location, u.created_at as user_created
            FROM public.subscriptions s
            JOIN public.users u ON s.user_id = u.telegram_id
            WHERE s.status IN ('active', 'pending')
            ORDER BY u.created_at, s.created_at
        """)
        subscriptions_data = cur.fetchall()
        if not subscriptions_data:
            await update.message.reply_text("❌ ለፒዲኤፍ ወጣ የተመዘገቡ ተጠቃሚዎች ወይም ትዕዛዞች የሉም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
            return MAIN_MENU

        # Group by user for better structure, but include all subs
        user_subs = {}
        for row in subscriptions_data:
            sub_id, user_id, plan_type, meals_remaining, selected_dates_json, expiry_date, sub_status, sub_created, full_name, username, phone_number, location, user_created = row
            if user_id not in user_subs:
                user_subs[user_id] = {
                    'full_name': full_name,
                    'username': username,
                    'phone_number': phone_number,
                    'location': location,
                    'user_created': user_created,
                    'subscriptions': []
                }
            user_subs[user_id]['subscriptions'].append({
                'sub_id': sub_id,
                'plan_type': plan_type,
                'meals_remaining': meals_remaining,
                'selected_dates': json.loads(selected_dates_json) if isinstance(selected_dates_json, str) else selected_dates_json,
                'expiry_date': expiry_date,
                'status': sub_status,
                'sub_created': sub_created
            })

        # Generate PDF report
        report_filename = f"orders_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        doc = SimpleDocTemplate(report_filename, pagesize=letter)
        styles = getSampleStyleSheet()

        # Register Amharic font for food names (download if not present)
        font_path = 'NotoSansEthiopic-Regular.ttf'
        try:
            if not os.path.exists(font_path):
                logger.info("Downloading Amharic font...")
                url = "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansEthiopic/NotoSansEthiopic-Regular.ttf"
                response = requests.get(url)
                response.raise_for_status()
                with open(font_path, 'wb') as f:
                    f.write(response.content)
                logger.info("Font downloaded successfully.")
            pdfmetrics.registerFont(TTFont('Amharic', font_path))
            pdfmetrics.registerFont(TTFont('Amharic-Bold', font_path.replace('Regular', 'Bold')))
            amharic_style = ParagraphStyle(
                'AmharicStyle',
                parent=styles['Normal'],
                fontName='Amharic',
                fontSize=10,
                leading=12
            )
            english_style = styles['Normal']  # Use default for English
        except Exception as font_error:
            logger.warning(f"Amharic font setup failed, falling back to default: {font_error}")
            amharic_style = styles['Normal']
            english_style = styles['Normal']

        story = []
        title = Paragraph("Oz Kitchen Orders Report", styles['Title'])
        story.append(title)
        story.append(Spacer(1, 0.5 * inch))

        for user_id, user_info in user_subs.items():
            full_name = user_info['full_name']
            username = user_info['username']
            phone_number = user_info['phone_number']
            location = user_info['location']
            user_created = user_info['user_created']
            subscriptions = user_info['subscriptions']

            # User header (English)
            header_text = f"<b>User:</b> {full_name or 'N/A'} (ID: {user_id})<br/><b>Phone:</b> {phone_number or 'N/A'} | <b>Location:</b> {location or 'N/A'} | <b>Joined:</b> {user_created.strftime('%Y-%m-%d')}"
            p_header = Paragraph(header_text, english_style)
            story.append(p_header)
            story.append(Spacer(1, 0.2 * inch))

            for sub in subscriptions:
                sub_id = sub['sub_id']
                plan_type = sub['plan_type']
                meals_remaining = sub['meals_remaining']
                selected_dates = sub['selected_dates']
                expiry_date = sub['expiry_date']
                sub_status = sub['status']
                sub_created = sub['sub_created']

                # Translate terms
                plan_trans = 'Lunch' if plan_type == 'lunch' else 'Dinner'
                status_trans = 'Pending' if sub_status == 'pending' else 'Active'

                # Subscription details
                sub_text = f"<b>Subscription ID:</b> {sub_id} | <b>Type:</b> {plan_trans} | <b>Meals Left:</b> {meals_remaining} | <b>Expiry:</b> {expiry_date.strftime('%Y-%m-%d')} | <b>Status:</b> {status_trans} | <b>Subscribed:</b> {sub_created.strftime('%Y-%m-%d')}<br/><b>Selected Dates:</b> {', '.join(selected_dates)}"
                p_sub = Paragraph(sub_text, english_style)
                story.append(p_sub)
                story.append(Spacer(1, 0.1 * inch))

                # Fetch payments for this sub
                cur.execute("""
                    SELECT amount, created_at, status
                    FROM public.payments
                    WHERE subscription_id = %s
                    ORDER BY created_at DESC
                """, (sub_id,))
                payments = cur.fetchall()
                total_paid = sum(amount for amount, _, _ in payments) if payments else 0.0

                # Payments (English)
                payments_text = "<b>Payments:</b><br/>"
                if payments:
                    for amount, paid_date, status in payments:
                        status_trans = 'Pending' if status == 'pending' else 'Approved' if status == 'approved' else 'Rejected'
                        payments_text += f"  - Amount: {amount:.2f} ETB | Date Paid: {paid_date.strftime('%Y-%m-%d %H:%M')} | Status: {status_trans}<br/>"
                    payments_text += f"<br/>  <b>Total Paid:</b> {total_paid:.2f} ETB"
                else:
                    payments_text += "None"
                p_payments = Paragraph(payments_text, english_style)
                story.append(p_payments)
                story.append(Spacer(1, 0.2 * inch))

                # Fetch orders for this sub
                cur.execute("""
                    SELECT meal_date, items, created_at as order_created
                    FROM public.orders
                    WHERE subscription_id = %s AND status = 'confirmed'
                    ORDER BY meal_date
                """, (sub_id,))
                orders = cur.fetchall()
                total_order_price = 0.0
                all_items = []
                for meal_date, items_json, order_created in orders:
                    items = json.loads(items_json) if isinstance(items_json, str) else items_json
                    all_items.extend(items)
                    total_order_price += sum(item['price'] for item in items)

                # Orders (English labels, Amharic food names)
                orders_text = f"<b>Food Ordered (Total Value: {total_order_price:.2f} ETB):</b><br/>"
                if orders:
                    for meal_date, items_json, order_created in orders:
                        items = json.loads(items_json) if isinstance(items_json, str) else items_json
                        orders_text += f"  - Date Ordered: {meal_date} (Order Date: {order_created.strftime('%Y-%m-%d %H:%M')})<br/>"
                        for item in items:
                            orders_text += f"    * {item['name']} - {item['price']:.2f} ETB ({item['category']})<br/>"
                else:
                    orders_text += "None"
                p_orders = Paragraph(orders_text, amharic_style)
                story.append(p_orders)
                story.append(Spacer(1, 0.2 * inch))

            story.append(Spacer(1, 0.3 * inch))
            separator = Paragraph("-" * 50, styles['Normal'])
            story.append(separator)
            story.append(Spacer(1, 0.3 * inch))

        doc.build(story)

        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=open(report_filename, 'rb'),
            filename=report_filename,
            caption="📄 Orders Report PDF Exported Successfully! (Updated with latest database data)"
        )
        os.remove(report_filename)  # Clean up

        await update.message.reply_text("✅ PDF Report Exported and Sent!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error generating PDF report: {e}")
        await update.message.reply_text("❌ Error generating PDF report. Please try again.", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Admin: View Admin Location (placeholder for set_admin_location if needed)
async def set_admin_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📍 የካፌ ቦታ ያጋሩ ወይም 'ዝለል'።\n\n🔧 ቦታ ያጋሩ!", reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True))
    return SET_ADMIN_LOCATION

# Admin: Approve or reject location
async def admin_approve_locations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪ አይደሉም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT pl.id, u.full_name, u.username, pl.location_text "
            "FROM public.pending_locations pl JOIN public.users u ON pl.user_id = u.telegram_id "
            "WHERE pl.status = 'pending' ORDER BY pl.created_at DESC"
        )
        locations = cur.fetchall()
        if not locations:
            await update.message.reply_text(
                "📭 ለፍተሻ ተጠባቂ ቦታዎች የሉም።\n\n"
                "🔙 ወደ መነሻ ገጽ!",
                reply_markup=get_main_keyboard(user.id)
            )
            return MAIN_MENU
        for location_id, full_name, username, location_text in locations:
            keyboard = [
                [InlineKeyboardButton("አረጋግጥ", callback_data=f"approve_location_{location_id}"),
                 InlineKeyboardButton("ውድቅ አድርግ", callback_data=f"reject_location_{location_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(
                chat_id=user.id,
                text=f"📍 ቦታ #{location_id}\n\n"
                     f"👤 ተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\n\n"
                     f"📋 ቦታ: {location_text}\n\n"
                     "🔧 ለማረጋገጥ ወይም ለመሰረዝ ይመርጡ!",
                reply_markup=reply_markup
            )
        await update.message.reply_text(
            "📍 ከላይ የቆዩ የቦታ ጥያቄዎች ናቸው።\n\n"
            "🔧 ለማረጋገጥ ወይም ለመሰረዝ ይመርጡ!",
            reply_markup=get_main_keyboard(user.id)
        )
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error fetching pending locations: {e}")
        await update.message.reply_text("❌ ተጠባቂ ቦታዎችን መጫን ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Handle location approval/rejection callback
async def handle_location_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')
    action = data[0]
    location_id = int(data[2])
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, location_text FROM public.pending_locations WHERE id = %s AND status = 'pending'",
            (location_id,)
        )
        location = cur.fetchone()
        if not location:
            await query.edit_message_text("❌ ቦታ አልተሰጠም ወይም ቀደም ብሎ ተከፍሏል።\n\n🔄 እንደገና ይመልከቱ!")
            return
        user_id, location_text = location
        if action == 'approve':
            cur.execute(
                "UPDATE public.pending_locations SET status = 'approved' WHERE id = %s",
                (location_id,)
            )
            cur.execute(
                "UPDATE public.users SET location = %s WHERE telegram_id = %s",
                (location_text, user_id)
            )
            conn.commit()
            await query.edit_message_text("✅ ቦታ ተቀበለ።\n\n🚀 ተቀበለ!")
            # Send direct to subscription plan
            await context.bot.send_message(
                chat_id=user_id,
                text="✅ ቦታዎ ተቀበለ!\n\n"
                     "📦 የምዝገባ እቅድዎን ይምረጡ:\n\n"
                     "🍽️ የምሳ\n\n"
                     "🥘 የእራት\n\n"
                     "🚀 እቅድ ይምረጡ!",
                reply_markup=ReplyKeyboardMarkup(
                    [['🍽️ የምሳ', '🥘 የእራት'], ['🔙 ተመለስ']],
                    resize_keyboard=True
                )
            )
        elif action == 'reject':
            cur.execute(
                "UPDATE public.pending_locations SET status = 'rejected' WHERE id = %s",
                (location_id,)
            )
            conn.commit()
            await query.edit_message_text("❌ ቦታ ተውደቀ።\n\n🚫 ተውደቀ!")
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ ቦታዎ ተሰርዟል።\n\n"
                     "🔄 እባክዎ ከመጀመር ጋር እንደገና ይጀምሩ።\n\n"
                     "🚀 /start ይጠቀሙ!",
                reply_markup=ReplyKeyboardMarkup([['📋 ይመዝገቡ', '💬 ድጋፍ']], resize_keyboard=True)
            )
    except Exception as e:
        logger.error(f"Error processing location callback for location {location_id}: {e}")
        await query.edit_message_text("❌ የቦታ እርምጃ በማስተካከል ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ።")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Admin: Approve or reject payment
async def admin_approve_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪ አይደሉም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT p.id, u.full_name, u.username, p.amount, p.receipt_url, p.user_id, p.subscription_id "
            "FROM public.payments p JOIN public.users u ON p.user_id = u.telegram_id "
            "WHERE p.status = 'pending' ORDER BY p.created_at DESC"
        )
        payments = cur.fetchall()
        if not payments:
            await update.message.reply_text(
                "📭 ለፍተሻ ተጠባቂ ክፍያዎች የሉም።\n\n"
                "🔙 ወደ መነሻ ገጽ!",
                reply_markup=get_main_keyboard(user.id)
            )
            return MAIN_MENU
        for payment_id, full_name, username, amount, receipt_url, user_id, subscription_id in payments:
            keyboard = [
                [InlineKeyboardButton("አረጋግጥ", callback_data=f"approve_payment_{payment_id}"),
                 InlineKeyboardButton("ውድቅ አድርግ", callback_data=f"reject_payment_{payment_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                if receipt_url:
                    try:
                        await context.bot.send_photo(
                            chat_id=user.id,
                            photo=receipt_url,
                            caption=f"💳 ክፍያ #{payment_id}\n\n"
                                    f"👤 ተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\n\n"
                                    f"💰 መጠን: {amount:.2f} ብር\n\n"
                                    "🔧 ለማረጋገጥ ወይም ለመሰረዝ ይመርጡ!",
                            reply_markup=reply_markup
                        )
                    except Exception as e:
                        logger.error(f"Error sending photo for payment {payment_id} to admin {user.id}: {e}")
                        await context.bot.send_message(
                            chat_id=user.id,
                            text=f"💳 ክፍያ #{payment_id}\n\n"
                                 f"👤 ተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\n\n"
                                 f"💰 መጠን: {amount:.2f} ብር\n\n"
                                 f"🔗 የማረጋገጫ File ID: {receipt_url}\n\n"
                                 f"(⚠️ ማሳወቂያ: ስቶ ማሳየት ስህተት ተከሰተ: {str(e)})\n\n"
                                 "🔧 ለማረጋገጥ ወይም ለመሰረዝ ይመርጡ!",
                            reply_markup=reply_markup
                        )
                else:
                    await context.bot.send_message(
                        chat_id=user.id,
                        text=f"💳 ክፍያ #{payment_id}\n\n"
                             f"👤 ተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\n\n"
                             f"💰 መጠን: {amount:.2f} ብር\n\n"
                             f"🔗 የማረጋገጫ File ID: {receipt_url or 'የለም'}\n\n"
                             "🔧 ለማረጋገጥ ወይም ለመሰረዝ ይመርጡ!",
                        reply_markup=reply_markup
                    )
            except Exception as e:
                logger.error(f"Error processing payment {payment_id} for admin {user.id}: {e}")
                await context.bot.send_message(
                    chat_id=user.id,
                    text=f"💳 ክፍያ #{payment_id}\n\n"
                         f"👤 ተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\n\n"
                         f"💰 መጠን: {amount:.2f} ብር\n\n"
                         f"⚠️ ስህተት: የማረጋገጥ ዝርዝር ማስተካከል አልተሳካም\n\n"
                         "🔧 ለማረጋገጥ ወይም ለመሰረዝ ይመርጡ!",
                    reply_markup=reply_markup
                )
        await update.message.reply_text(
            "💳 📷 ከላይ የቆዩ የክፍያ ጥያቆዎች ናቸው።\n\n"
            "🔧 ለማረጋገጥ ወይም ለመሰረዝ አማራጮቹን ይጠቀሙ።\n\n"
            "🚀 እርምጃ ይወስዱ!",
            reply_markup=get_main_keyboard(user.id)
        )
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error fetching pending payments: {e}")
        await update.message.reply_text("❌ ተጠባቂ ክፍያዎችን መጫን ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Handle payment approval/rejection callback
# Handle payment approval/rejection callback
async def handle_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')
    action = data[0]
    payment_id = int(data[2])
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT p.user_id, p.subscription_id, p.amount "
            "FROM public.payments p WHERE p.id = %s AND p.status = 'pending'",
            (payment_id,)
        )
        payment = cur.fetchone()
        if not payment:
            try:
                await query.edit_message_text("❌ ክፍያ አልተሰጠም ወይም ቀደም ብሎ ተከፍሏል።\n🔄 እንደገና ይመልከቱ!")
            except:
                await query.message.reply_text("❌ ክፍያ አልተሰጠም ወይም ቀደም ብሎ ተከፍሏል።\n🔄 እንደገና ይመልከቱ!")
            return

        user_id, subscription_id, amount = payment

        # Fetch orders for detailed message
        cur.execute(
            "SELECT meal_date, items FROM public.orders WHERE subscription_id = %s AND status = 'confirmed'",
            (subscription_id,)
        )
        orders = cur.fetchall()

        if action == 'approve':
            cur.execute(
                "UPDATE public.payments SET status = 'approved' WHERE id = %s",
                (payment_id,)
            )
            cur.execute(
                "UPDATE public.subscriptions SET status = 'active' WHERE id = %s",
                (subscription_id,)
            )
            conn.commit()

            # Notify admin (edit original message safely)
            try:
                await query.edit_message_text("✅ ክፍያ ተቀበለ።\n🚀 ተቀበለ!")
            except Exception as e:
                logger.warning(f"Could not edit admin message: {e}")
                try:
                    await query.message.reply_text("✅ ክፍያ ተቀበለ።\n🚀 ተቀበለ!")
                except:
                    pass

            # Build confirmation message for USER
            detailed_text = "📢 የክፍያ ማረጋገጫ መልእክት!\n"
            detailed_text += f"✅ ክፍያዎ {amount:.2f} ብር ተቀበለ!\n"
            detailed_text += "🍽 የተመረጡ ምግቦችና ቀንት:\n"

            if not orders:
                detailed_text += "   (ምግቦች አልተገኙም)\n"
            else:
                for meal_date, items_json in orders:
                    try:
                        items = json.loads(items_json) if isinstance(items_json, str) else items_json
                        if not isinstance(items, list):
                            items = [items]
                        item_lines = []
                        for item in items:
                            name = item.get('name', 'ያልታወቀ ምግብ')
                            price = item.get('price', 0)
                            item_lines.append(f"{name} ({price:.2f} ብር)")
                        detailed_text += f"📅 {meal_date}: {', '.join(item_lines)}\n"
                    except Exception as parse_err:
                        logger.error(f"Failed to parse items for order on {meal_date}: {parse_err}")
                        detailed_text += f"📅 {meal_date}: (ስህተት በምግብ ዝርዝር)\n"

            detailed_text += f"\n💰 ጠቅላላ መጠን: {amount:.2f} ብር\n"
            detailed_text += "🍴 ምግቦችዎ ዝግጁ ይሆናሉ!\n"
            detailed_text += "🚀 ተጠናቅቀው በደህና!"

            # Send to USER
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=detailed_text,
                    reply_markup=get_main_keyboard(user_id)
                )
            except Exception as send_err:
                logger.error(f"Failed to send approval message to user {user_id}: {send_err}")

        elif action == 'reject':
            # Fetch before deletion
            cur.execute(
                "SELECT meal_date, items FROM public.orders WHERE subscription_id = %s AND status = 'confirmed'",
                (subscription_id,)
            )
            orders_before_delete = cur.fetchall()

            cur.execute("UPDATE public.payments SET status = 'rejected' WHERE id = %s", (payment_id,))
            cur.execute("DELETE FROM public.orders WHERE subscription_id = %s", (subscription_id,))
            cur.execute("DELETE FROM public.subscriptions WHERE id = %s", (subscription_id,))
            conn.commit()

            # Notify admin
            try:
                await query.edit_message_text("❌ ክፍያ ተውደቀ።\n🚫 ተውደቀ!")
            except Exception as e:
                logger.warning(f"Could not edit admin message: {e}")
                try:
                    await query.message.reply_text("❌ ክፍያ ተውደቀ።\n🚫 ተውደቀ!")
                except:
                    pass

            # Build rejection message for USER
            detailed_text = "📢 የክፍያ ማረጋገጫ መልእክት!\n"
            detailed_text += f"❌ ክፍያዎ {amount:.2f} ብር ተውደቀ!\n"

            if orders_before_delete:
                detailed_text += "🍽 የተመረጡ ምግቦችና ቀንት:\n"
                for meal_date, items_json in orders_before_delete:
                    try:
                        items = json.loads(items_json) if isinstance(items_json, str) else items_json
                        if not isinstance(items, list):
                            items = [items]
                        item_lines = []
                        for item in items:
                            name = item.get('name', 'ያልታወቀ ምግብ')
                            price = item.get('price', 0)
                            item_lines.append(f"{name} ({price:.2f} ብር)")
                        detailed_text += f"📅 {meal_date}: {', '.join(item_lines)}\n"
                    except Exception as parse_err:
                        logger.error(f"Failed to parse items for rejected order on {meal_date}: {parse_err}")
                        detailed_text += f"📅 {meal_date}: (ስህተት በምግብ ዝርዝር)\n"
            else:
                detailed_text += "   (ምግቦች አልተገኙም)\n"

            detailed_text += f"\n💰 ጠቅላላ መጠን: {amount:.2f} ብር\n"
            detailed_text += "🛒 እባክዎ ከ /subscribe ጋር እንደገና ይጀምሩ።\n"
            detailed_text += "🔄 እንደገና ይጀምሩ!"

            # Send to USER
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=detailed_text,
                    reply_markup=ReplyKeyboardMarkup([['📋 ይመዝገቡ', '💬 ድጋፍ']], resize_keyboard=True)
                )
            except Exception as send_err:
                logger.error(f"Failed to send rejection message to user {user_id}: {send_err}")

    except Exception as e:
        logger.error(f"Error processing payment callback for payment {payment_id}: {e}")
        try:
            await query.edit_message_text("❌ የክፍያ እርምጃ በማስተካከል ላይ ስህተት።\n🔄 እባክዎ እንደገና ይሞክሩ።")
        except:
            await query.message.reply_text("❌ የክፍያ እርምጃ በማስተካከል ላይ ስህተት።\n🔄 እባክዎ እንደገና ይሞክሩ።")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
# My Subscription → My Info (keep as subscription details)
async def my_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if has_pending_location(user.id):
        await update.message.reply_text("⏳ ቦታዎ ለማረጋገጥ በመጠበቅ ላይ ነው። እባክዎ ይጠብቁ።\n\n🔄 እባክዎ ይጠብቁ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    if user.id in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪዎች ምዝገባ አያስፈልጋቸውም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, plan_type, meals_remaining, selected_dates, expiry_date, status "
            "FROM public.subscriptions WHERE user_id = %s AND status IN ('pending', 'active')",
            (user.id,)
        )
        subscription = cur.fetchone()
        if not subscription:
            await update.message.reply_text(
                "❌ ንቁ ወይም ተጠባቂ ምዝገባዎች የሉም።\n\n"
                "🛒 /subscribe ይጠቀሙ አዲስ ያጀምሩ።\n\n"
                "🚀 ምዝገባ ይጀምሩ!",
                reply_markup=get_main_keyboard(user.id)
            )
            return MAIN_MENU
        subscription_id, plan_type, meals_remaining, selected_dates_json, expiry_date, status = subscription
        selected_dates_en = json.loads(selected_dates_json) if isinstance(selected_dates_json, str) else selected_dates_json
        valid_days_en = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        valid_days_am = ['ሰኞ', 'ማክሰኞ', 'እሮብ', 'ሐሙስ', 'አርብ', 'ቅዳሜ', 'እሑድ']
        selected_dates = [valid_days_am[valid_days_en.index(day)] for day in selected_dates_en]
        text = (
            f"📋 የእርስዎ ምዝገባ:\n\n"
            f"📦 እቅድ: {plan_type.capitalize()}\n\n"
            f"🍽 ቀሪ ምግቦች: {meals_remaining}\n\n"
            f"📅 የተመረጡ ቀናት: {', '.join(selected_dates)}\n\n"
            f"⏰ የጊዜ ጫና: {expiry_date.strftime('%Y-%m-%d')}\n\n"
            f"✅ ሁኔታ: {status.capitalize()}\n\n"
            "🍴 ምግቦችዎን ለመምረጫ /select_meals ይጠቀሙ።\n\n"
            "🚀 ምግቦችን ይምረጠው!"
        )
        await update.message.reply_text(text, reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error fetching subscription for user {user.id}: {e}")
        await update.message.reply_text("❌ የምዝገባ ዝርዝር መጫን ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Admin: Update Menu
async def admin_update_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪ አይደሉም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    await update.message.reply_text(
        "📋 አዲሱን ምግብ ዝርዝር በJSON ቅርጽ ያስገቡ (ለምሳሌ፣ [{'id': 1, 'name': 'Dish', 'price': 100, 'day': 'Monday', 'category': 'fasting'}])።\n\n"
        "🔧 JSON ቅርጽ ያስገቡ!\n\n"
        "🚀 ዝርዝር ያዘምኑ!",
        reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
    )
    return ADMIN_UPDATE_MENU

async def process_admin_update_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪ አይደሉም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    if update.message.text.lower() in ['ሰርዝ', '🔙 ተመለስ']:
        await update.message.reply_text("❌ የምግብ ዝርዝር ማዘመን ተሰርዟል።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    try:
        menu_data = json.loads(update.message.text)
        if not isinstance(menu_data, list):
            raise ValueError("Menu must be a JSON list.")
        today = datetime.now(EAT).date()
        week_start = today - timedelta(days=today.weekday())
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO public.weekly_menus (week_start_date, menu_items) "
            "VALUES (%s, %s) ON CONFLICT (week_start_date) DO UPDATE SET menu_items = EXCLUDED.menu_items",
            (week_start, json.dumps(menu_data))
        )
        conn.commit()
        await update.message.reply_text("✅ ምግብ ዝርዝር በተሳካ ሁኔታ ተዘመነ።\n\n🚀 ተዘመን!\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error updating menu: {e}")
        await update.message.reply_text("❌ የማይሰራ JSON ወይም ምግብ ዝርዝር ማዘመን ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!\n\n🚀 እንደገና ይሞክሩ!", reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True))
        return ADMIN_UPDATE_MENU
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

# Admin: Delete Menu
async def admin_delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪ አይደሉም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        today = datetime.now(EAT).date()
        week_start = today - timedelta(days=today.weekday())
        cur.execute(
            "SELECT menu_items FROM public.weekly_menus WHERE week_start_date = %s",
            (week_start,)
        )
        menu_result = cur.fetchone()
        if not menu_result:
            await update.message.reply_text("❌ ለዚህ ሳምንቹ ምግብ ዝርዝር አልተገኘም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
            return MAIN_MENU
        menu_items = json.loads(menu_result[0]) if isinstance(menu_result[0], str) else menu_result[0]
        if not menu_items:
            await update.message.reply_text("❌ ምግብ ዝርዝሩ ባዶ ነው።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
            return MAIN_MENU
        context.user_data['week_start'] = week_start
        context.user_data['menu_items'] = menu_items
        text = build_delete_menu_text(menu_items, week_start)
        await update.message.reply_text(
            f"{text}\n\n"
            "🔢 ለማስወገድ ንጥል ያስገቡ (ለምሳሌ: '1') ወይም 'ሰርዝ' ይፃፉ።\n\n"
            "🚀 ንጥል ያስገቡ!",
            reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
        )
        return ADMIN_DELETE_MENU
    except Exception as e:
        logger.error(f"Error fetching menu for deletion: {e}")
        await update.message.reply_text("❌ ምግብ ዝርዝር መጫን ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

async def process_admin_delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪ አይደሉም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    if update.message.text.lower() in ['ሰርዝ', '🔙 ተመለስ']:
        await update.message.reply_text("❌ የምግብ ዝርዝር ማስወገድ ተሰርዟል።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    try:
        item_idx = int(update.message.text) - 1
        menu_items = context.user_data.get('menu_items', [])
        week_start = context.user_data.get('week_start')
        if not (0 <= item_idx < len(menu_items)):
            await update.message.reply_text(
                f"❌ የማይሰራ የንጥል ቁጥል።\n\n"
                f"🔢 1 እስከ {len(menu_items)} መካከል ይምረጠው።\n\n"
                "🔄 ትክክለኛ ቁጥል ያስገቡ!",
                reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
            )
            return ADMIN_DELETE_MENU
        menu_items.pop(item_idx)
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE public.weekly_menus SET menu_items = %s WHERE week_start_date = %s",
            (json.dumps(menu_items), week_start)
        )
        conn.commit()
        await update.message.reply_text("✅ የምግብ ዝርዝር ንጥል በተሳካ ሁኔታ ተሰርዟል።\n\n🚀 ተሰርዟል!\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error deleting menu item: {e}")
        await update.message.reply_text("❌ የምግብ ዝርዝር ንጥል በማስወገድ ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!\n\n🚀 እንደገና ይሞክሩ!", reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True))
        return ADMIN_DELETE_MENU
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

# Admin: View Subscribers
async def admin_subscribers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪ አይደሉም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT u.full_name, u.username, s.plan_type, s.meals_remaining, s.expiry_date "
            "FROM public.subscriptions s JOIN public.users u ON s.user_id = u.telegram_id "
            "WHERE s.status IN ('pending', 'active')"
        )
        subscribers = cur.fetchall()
        if not subscribers:
            await update.message.reply_text("❌ ንቁ ወይም ተጠባቂ ተመዝጋቢዎች አልተገኘም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
            return MAIN_MENU
        text = "📋 ንቁ/ተጠባቂ ተመዝጋቢዎች:\n\n"
        for full_name, username, plan_type, meals_remaining, expiry_date in subscribers:
            text += (
                f"👤 ስም: {full_name or 'የለም'} (@{username or 'የለም'})\n\n"
                f"📦 እቅድ: {plan_type.capitalize()}\n\n"
                f"🍽 ቀሪ ምግቦች: {meals_remaining}\n\n"
                f"⏰ ጫና: {expiry_date.strftime('%Y-%m-%d')}\n\n"
                "────────────\n\n"
            )
        await update.message.reply_text(text, reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error fetching subscribers: {e}")
        await update.message.reply_text("❌ ተመዝጋቢዎችን መጫን ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Admin: Track Payments
async def admin_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪ አይደሉም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT p.id, u.full_name, u.username, p.amount, p.status, p.created_at, p.receipt_url "
            "FROM public.payments p JOIN public.users u ON p.user_id = u.telegram_id "
            "ORDER BY p.created_at DESC"
        )
        payments = cur.fetchall()
        if not payments:
            await update.message.reply_text("❌ ክፍያዎች አልተገኘም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
            return MAIN_MENU
        await update.message.reply_text("💸 የክፍያ ታሪክ እየተላከ ነው...", reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True))
        for payment_id, full_name, username, amount, status, created_at, receipt_url in payments:
            caption = f"💳 ክፍያ #{payment_id}\n\n👤 ተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\n💰 መጠን: {amount:.2f} ብር\n✅ ሁኔታ: {status.capitalize()}\n📅 ቀን: {created_at.strftime('%Y-%m-%d %H:%M')}\n🔗 File ID: {receipt_url or 'የለም'}"
            try:
                if receipt_url:
                    await context.bot.send_photo(
                        chat_id=user.id,
                        photo=receipt_url,
                        caption=caption
                    )
                else:
                    await context.bot.send_message(
                        chat_id=user.id,
                        text=caption
                    )
            except Exception as e:
                logger.error(f"Error sending payment {payment_id} details: {e}")
                await context.bot.send_message(
                    chat_id=user.id,
                    text=f"{caption}\n\n⚠️ ምስል ማሳየት ላይ ስህተት።"
                )
        await context.bot.send_message(
            chat_id=user.id,
            text="✅ የክፍያ ታሪክ ተመልክቷል!",
            reply_markup=get_main_keyboard(user.id)
        )
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error fetching payments: {e}")
        await update.message.reply_text("❌ ክፍያዎችን መጫን ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Admin: Daily Orders
async def admin_daily_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪዎች ማዘዋወር አያስፈልጋቸውም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        today = datetime.now(EAT).date()
        cur.execute(
            "SELECT u.full_name, u.username, o.meal_date, o.items "
            "FROM public.orders o JOIN public.users u ON o.user_id = u.telegram_id "
            "WHERE o.meal_date = %s AND o.status = 'confirmed'",
            (today,)
        )
        orders = cur.fetchall()
        if not orders:
            week_start = today - timedelta(days=today.weekday())
            week_end = week_start + timedelta(days=6)
            cur.execute(
                "SELECT u.full_name, u.username, o.meal_date, o.items "
                "FROM public.orders o JOIN public.users u ON o.user_id = u.telegram_id "
                "WHERE o.meal_date BETWEEN %s AND %s AND o.status = 'confirmed' ORDER BY o.meal_date",
                (week_start, week_end)
            )
            orders = cur.fetchall()
            if not orders:
                await update.message.reply_text(f"❌ ለ{week_start} - {week_end} ሳምንት ትዕዛዞች የሉም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
                return MAIN_MENU
            text = f"📅 ለ{week_start} - {week_end} ሳምንት ትዕዛዞች (ዛሬ የለም):\n\n"
        else:
            text = f"📅 ለ{today} ትዕዛዞች:\n\n"
        for full_name, username, meal_date, items_json in orders:
            items = json.loads(items_json) if isinstance(items_json, str) else items_json
            text += f"👤 ተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\n\n📅 ቀን: {meal_date}\n\n"
            for item in items:
                text += f"🍴 - {item['name']} ({item['category']})\n\n"
            text += "────────────\n\n"
        await update.message.reply_text(text, reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error fetching daily orders: {e}")
        await update.message.reply_text("❌ የዕለት ትዕዛዞችን መጫን ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Admin: Announce
async def admin_announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪ አይደሉም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    await update.message.reply_text(
        "📢 ለሁሉም ተጠቃሚዎች ለማስተላለፍ ለማስተላለፍ መልእክቹ ያስገቡ:\n\n"
        "🔧 መልእክት ያስገቡ!\n\n"
        "🚀 ማስታወቂያ ያልፉ!",
        reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
    )
    return ADMIN_ANNOUNCE

async def process_admin_announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪ አይደሉም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    if update.message.text.lower() in ['ሰርዝ', '🔙 ተመለስ']:
        await update.message.reply_text("❌ ማስታወቂያ ተሰርዟል።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    announcement = update.message.text
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT telegram_id FROM public.users")
        users = cur.fetchall()
        for user_id_tuple in users:
            user_id = user_id_tuple[0]
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"📢 ማስታወቂያ:\n\n{announcement}\n\n🚀 በደህና ይጠቀሙ!"
                )
            except Exception as e:
                logger.error(f"Error sending announcement to user {user_id}: {e}")
        await update.message.reply_text("✅ ማስታወቂያ ለሁሉም ተጠቃሚዎች ተላከ።\n\n🚀 ተላከ!\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error sending announcement: {e}")
        await update.message.reply_text("❌ ማስታወቂያ በማላክ ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!\n\n🚀 እንደገና ይሞክሩ!", reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True))
        return ADMIN_ANNOUNCE
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Admin: Set Location
async def process_set_admin_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አስተዳዳሪ አይደሉም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    if update.message.text in ['🔙 ተመለስ', 'ዝለል']:
        await update.message.reply_text("❌ ቦታ ማዘጋጀት ተሰርዟል።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    location = None
    if update.message.location:
        try:
            latitude = float(update.message.location.latitude)
            longitude = float(update.message.location.longitude)
            location = f"({latitude:.6f}, {longitude:.6f})"
        except Exception as e:
            logger.error(f"Error processing location: {e}")
            await update.message.reply_text("❌ የማይሰራ ቦታ።\n\n🔄 እባክዎ እንደገና ይሞክሩ ወይም 'ዝለል' ይፃፉ።\n\n🚀 እንደገና ይሞክሩ!", reply_markup=ReplyKeyboardMarkup([["ዝለል", '🔙 ተመለስ']], resize_keyboard=True))
            return SET_ADMIN_LOCATION
    else:
        location = update.message.text
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO public.settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP",
            (f"admin_location_{user.id}", location)
        )
        conn.commit()
        await update.message.reply_text("✅ ቦታ በተሳካ ሁኔታ ተዘጋጅቷል።\n\n🚀 ተዘጋጅቷል!\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error setting admin location: {e}")
        await update.message.reply_text("❌ ቦታ በማስቀመጥ ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!")
        return SET_ADMIN_LOCATION
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Admin: View Locations
async def view_locations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌አስተዳዳሪ አይደሉም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT key, value FROM public.settings WHERE key LIKE 'admin_location_%'"
        )
        locations = cur.fetchall()
        if not locations:
            await update.message.reply_text("❌ የተዘጋጁ ቦታዎች የሉም።\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
            return MAIN_MENU
        for key, value in locations:
            admin_id = key.replace('admin_location_', '')
            await update.message.reply_text(f"📍 አስተዳዳሪ {admin_id}: {value}\n\n🔧 ቦታ ተመልክቱ!")
            if value.startswith('(') and ',' in value:
                try:
                    match = re.match(r'\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)', value)
                    if match:
                        lat = float(match.group(1))
                        lon = float(match.group(2))
                        await context.bot.send_location(chat_id=user.id, latitude=lat, longitude=lon)
                except Exception as e:
                    logger.error(f"Error sending location for admin {admin_id}: {e}")
                    await update.message.reply_text(f"❌ ለአስተዳዳሪ {admin_id} ማፕ ማሳየት ላይ ስህተት።\n\n🔄 እንደገና ይሞክሩ!")
            else:
                await update.message.reply_text(f"ℹ️ ለአስተዳዳሪ {admin_id}: የማፕ ትውልድ የለም (ጽሑፍ ቦታ)።\n\n🔧 ጽሑፍ ቦታ!")
        await update.message.reply_text("✅ የተጋሩ ቦታዎች ተመልክተዋል።\n\n🚀 ተመልክተዋል!\n\n🔙 ወደ መነሻ ገጽ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error fetching locations: {e}")
        await update.message.reply_text("❌ ቦታዎችን መጫን ላይ ስህተት።\n\n🔄 እባክዎ እንደገና ይሞክሩ!", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Automated reminder functions
async def send_lunch_reminders(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(EAT).date()
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT u.telegram_id, u.full_name, o.items, p.amount
            FROM public.orders o 
            JOIN public.users u ON o.user_id = u.telegram_id
            JOIN public.subscriptions s ON o.subscription_id = s.id
            LEFT JOIN public.payments p ON s.id = p.subscription_id AND p.status = 'approved'
            WHERE o.meal_date = %s AND s.status = 'active' AND s.plan_type = 'lunch'
        """, (today,))
        users_data = cur.fetchall()
        for user_id, full_name, items_json, total_amount in users_data:
            items = json.loads(items_json) if isinstance(items_json, str) else items_json
            message = f"🍽 ምስጋና! {full_name or 'ተጠቃሚ'}\n\n"
            message += "የምሳ ምግብዎ ዝግጁ ሆነ!\n\n"
            for item in items:
                message += f"🍴 {item['name']} - {item['price']:.2f} ብር\n"
            message += f"💰 ጠቅላላ ክፍል: {total_amount or 'የለም'} ብር\n\n"
            message += "🚀 በደህና በታትተው ይጠቀሙ!"
            try:
                await context.bot.send_message(chat_id=user_id, text=message)
            except Exception as e:
                logger.error(f"Error sending lunch reminder to {user_id}: {e}")
    except Exception as e:
        logger.error(f"Error in send_lunch_reminders: {e}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

async def send_dinner_reminders(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(EAT).date()
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT u.telegram_id, u.full_name, o.items, p.amount
            FROM public.orders o 
            JOIN public.users u ON o.user_id = u.telegram_id
            JOIN public.subscriptions s ON o.subscription_id = s.id
            LEFT JOIN public.payments p ON s.id = p.subscription_id AND p.status = 'approved'
            WHERE o.meal_date = %s AND s.status = 'active' AND s.plan_type = 'dinner'
        """, (today,))
        users_data = cur.fetchall()
        for user_id, full_name, items_json, total_amount in users_data:
            items = json.loads(items_json) if isinstance(items_json, str) else items_json
            message = f"🥘 ምስጋና! {full_name or 'ተጠቃሚ'}\n\n"
            message += "የእራት ምግብዎ ዝግጁ ሆነ!\n\n"
            for item in items:
                message += f"🍴 {item['name']} - {item['price']:.2f} ብር\n"
            message += f"💰 ጠቅላላ ክፍል: {total_amount or 'የለም'} ብር\n\n"
            message += "🚀 በደህና በታትተው ይጠቀሙ!"
            try:
                await context.bot.send_message(chat_id=user_id, text=message)
            except Exception as e:
                logger.error(f"Error sending dinner reminder to {user_id}: {e}")
    except Exception as e:
        logger.error(f"Error in send_dinner_reminders: {e}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Cancel command
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data.clear()
    await update.message.reply_text(
        "❌ ሥራ ተሰርዟል።\n\n"
        "🔙 ወደ መነሻ ገጽ!",
        reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
    )
    return MAIN_MENU

# Error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.message:
        await update.message.reply_text("❌ ስህተት ተከሰተ።\n\n🔄 እባክዎ እንደገና ይሞክሩ ወይም ድጋፍ ያነጋግሩ።\n\n🚀 /start ይጠቀሙ!", reply_markup=get_main_keyboard(update.effective_user.id))

# Main function to run the bot
def main():
    try:
        init_db()
        application = Application.builder().token(BOT_TOKEN).build()
        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler('start', start),
                CommandHandler('help', send_help_text),
                CommandHandler('menu', show_menu),
                CommandHandler('subscribe', choose_plan),
                CommandHandler('my_subscription', my_subscription),
                CommandHandler('my_meals', my_meals),
                CommandHandler('select_meals', select_meals),
                CommandHandler('admin_update_menu', admin_update_menu),
                CommandHandler('admin_delete_menu', admin_delete_menu),
                CommandHandler('admin_subscribers', admin_subscribers),
                CommandHandler('admin_payments', admin_payments),
                CommandHandler('admin_approve_payment', admin_approve_payment),
                CommandHandler('admin_daily_orders', admin_daily_orders),
                CommandHandler('admin_announce', admin_announce),
                CommandHandler('setadminlocation', set_admin_location),
                CommandHandler('viewlocations', view_locations),
                CommandHandler('admin_approve_locations', admin_approve_locations),
                CommandHandler('cancel', cancel)
            ],
            states={
                MAIN_MENU: [
                    MessageHandler(filters.Regex('^🍽 ምግብ ዝርዝር$'), show_menu),
                    MessageHandler(filters.Regex('^🛒 ምዝገባ$'), choose_plan),
                    MessageHandler(filters.Regex('^👤 የእኔ መረጃ$'), user_profile),
                    MessageHandler(filters.Regex('^📅 የእኔ ምግቦች$'), my_meals),
                    MessageHandler(filters.Regex('^🔄 ትዕዛዙን መዘዋወር$'), reschedule_start),
                    MessageHandler(filters.Regex('^📞 ድጋፍ$'), support_menu),
                    MessageHandler(filters.Regex('^🔐 ምግብ ዝርዝር አዘምን$'), admin_update_menu),
                    MessageHandler(filters.Regex('^🔐 ምግብ ዝርዝር ሰርዝ$'), admin_delete_menu),
                    MessageHandler(filters.Regex('^🔐 ተመዝጋቢዎችን ተመልከት$'), admin_subscribers),
                    MessageHandler(filters.Regex('^🔐 ክፍያዎችን ተመልከት$'), admin_payments),
                    MessageHandler(filters.Regex('^🔐 ክፍያዎችን አረጋግጥ$'), admin_approve_payment),
                    MessageHandler(filters.Regex('^🔐 የዕለት ትዕዛዞች$'), admin_daily_orders),
                    MessageHandler(filters.Regex('^🔐 ማስታወቂያ$'), admin_announce),
                    MessageHandler(filters.Regex('^🔐 ቦታ አዘጋጅ$'), set_admin_location),
                    MessageHandler(filters.Regex('^🔐 ቦታዎችን ተመልከት$'), view_locations),
                    MessageHandler(filters.Regex('^🔐 ቦታዎችን አረጋግጥ$'), admin_approve_locations),
                    MessageHandler(filters.Regex('^🔐 የሳምንቱን ሪፖርት አትም$'), admin_export_pdf),
                    MessageHandler(filters.Regex('^📋 ይመዝገቡ$'), register_name),
                    MessageHandler(filters.Regex('^💬 ድጋፍ$'), support_menu),
                    MessageHandler(filters.Regex('^⏳ ማረጋገጫ በመጠበቅ ላይ$'), lambda u, c: MAIN_MENU),  # Restricted
                ],
                REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_name)],
                REGISTER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_phone)],  # ✅ Manual only
                REGISTER_LOCATION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, register_location)
                ],
                CONFIRM_LOCATION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_location)
                ],
                CONFIRM_REGISTRATION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_registration)
                ],
                CHOOSE_PLAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_plan)],
                CHOOSE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_date)],
                MEAL_SELECTION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, process_meal_selection)
                ],
                CONFIRM_MEAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_meal)],
                PAYMENT_UPLOAD: [
                    MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), payment_upload)
                ],
                RESCHEDULE_MEAL: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, process_reschedule)
                ],
                RESCHEDULE_DATE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, reschedule_date)
                ],
                RESCHEDULE_CONFIRM: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_reschedule)
                ],
                ADMIN_UPDATE_MENU: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, process_admin_update_menu)
                ],
                ADMIN_DELETE_MENU: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, process_admin_delete_menu)
                ],
                ADMIN_ANNOUNCE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, process_admin_announce)
                ],
                SET_ADMIN_LOCATION: [
                    MessageHandler(filters.LOCATION | (filters.TEXT & ~filters.COMMAND), process_set_admin_location)
                ],
                WAIT_LOCATION_APPROVAL: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, wait_location_approval)
                ],
                USER_CHANGE_LOCATION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, change_location)
                ],
                SUPPORT_MENU: [
                    MessageHandler(filters.Regex('^🔙 ተመለስ$'), back_to_main)
                ],
            },
            fallbacks=[CommandHandler('cancel', cancel)],
            allow_reentry=True
        )
        application.add_handler(conv_handler)
        application.add_handler(CallbackQueryHandler(handle_payment_callback, pattern='^(approve|reject)_payment_'))
        application.add_handler(CallbackQueryHandler(handle_location_callback, pattern='^(approve|reject)_location_'))
        application.add_error_handler(error_handler)
        # Schedule daily reminders
        application.job_queue.run_daily(send_lunch_reminders, time=time(9, 0, tzinfo=EAT))
        application.job_queue.run_daily(send_dinner_reminders, time=time(15, 0, tzinfo=EAT))
        while True:
            try:
                application.run_polling(drop_pending_updates=True, bootstrap_retries=-1, timeout=10, poll_interval=1, allowed_updates=Update.ALL_TYPES)
            except Exception as e:
                logger.error(f"Polling crashed: {e}. Restarting in 10 seconds...")
                sleep(10)
    except Exception as e:
        logger.error(f"Error starting bot: {e}")

if __name__ == '__main__':
    
    main()