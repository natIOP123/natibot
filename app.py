import os
import logging
import json
import psycopg2
import re
from datetime import datetime, timedelta
import pytz
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, ConversationHandler, filters, CallbackQueryHandler
import math
import validators
from time import sleep
from shapely.geometry import Point, Polygon

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

# Conversation states
(
    MAIN_MENU, REGISTER_NAME, REGISTER_PHONE, REGISTER_LOCATION, CONFIRM_REGISTRATION,
    ADMIN_UPDATE_MENU, ADMIN_ANNOUNCE, ADMIN_DAILY_ORDERS,
    ADMIN_DELETE_MENU, SET_ADMIN_LOCATION, ADMIN_APPROVE_PAYMENT, SUPPORT_MENU
) = range(12)

# Database connection helper
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        conn.set_session(autocommit=False)
        return conn
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        raise

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
    text = f"📋 የምግብ ዝርዝር ለሳምንቱ መጀመሪያ {week_start} (ለመሰረዝ የተወሰነ ንጥል ይምረጡ):\n"
    for idx, item in enumerate(sorted_items, 1):
        text += f"{idx}. {item['day']}: {item['name']} - {item['price']:.2f} ብር\n"
    return text

def get_main_keyboard(user_id):
    if user_id in ADMIN_IDS:
        keyboard = [
            ['🔐 ምግብ ዝርዝር አዘምን', '🔐 ምግብ ዝርዝር ሰርዝ'],
            ['🔐 ተመዝጋቢዎችን ተመልከት', '🔐 ክፍያዎችን ተመልከት'],
            ['🔐 ክፍያዎችን አረጋግጥ', '🔐 የዕለት ትዕዛዞች'],
            ['🔐 ማስታወቂያ', '🔐 ቦታ አዘጋጅ'],
            ['🔐 ቦታዎችን ተመልከት']
        ]
    else:
        keyboard = [['💬 ድጋፍ']]
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
            "👋 እንኳን ወደ ኦዝ ኪችን የምግብ ምዝገባ በደና መጡ!\n"
            "ትኩስ እና ጣፋጭ ምግቦችን በነጻ ለእርስዎ እናደርሳለን።\n\n"
            "የአገልግሎቱ መግለጫዎች እና ሂደቶች:\n"
            "1️⃣ የምዝገባ እቅድዎን እና ቀን ይምረጡ\n"
            "2️⃣ የሚወዷቸውን ምግቦች ከምግብ ዝርዝር ውስጥ ይምረጡ (ወይንም ከፈለጉ በሼፍ ውሳኔ)\n"
            "3️⃣ በየቀኑ የማስታወሻ መልክት ያገኛሉ እና አስፈላጊ ሆኖ ሲገኝ የመሰረዝ እና ወደሌላ የጊዜ ማዘዋወር ይቻላል።"
        )
        # Check if user is registered
        cur.execute("SELECT full_name, phone_number FROM public.users WHERE telegram_id = %s", (user.id,))
        user_data = cur.fetchone()
        if user_data and user_data[0] and user_data[1]:
            # Show full main menu
            await update.message.reply_text(
                f"👋 እንኳን ተመልሰው መጡ {user.first_name}!\n{onboarding_text}",
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
        await update.message.reply_text("❌ በመጀመር ላይ ስህተት ተከስቷል። እባክዎ እንደገና ይሞክሩ።")
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Support handler
async def support_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📞 የአስተዳዳሪውን ያግኙ፡ 0940406707",
        reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
    )
    return SUPPORT_MENU

# Back to main menu
async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT full_name, phone_number FROM public.users WHERE telegram_id = %s", (user.id,))
    user_data = cur.fetchone()
    cur.close()
    conn.close()
    if user_data and user_data[0] and user_data[1]:
        await update.message.reply_text(
            "🧾 ወደ ዋና ገጽ ተመለስተዋል።",
            reply_markup=get_main_keyboard(user.id)
        )
        return MAIN_MENU
    else:
        keyboard = [['📋 ይመዝገቡ', '💬 ድጋፍ']]
        await update.message.reply_text(
            "🧾 ወደ ዋና ገጽ ተመለስተዋል።",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return MAIN_MENU

# Help command (used after payment approval and for "እርዳታ አግኝ")
async def send_help_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    commands_text = (
        "👋 እንኳን ወደ ኦዝ ኪችን የምግብ ምዝገባ በደና መጡ!\n"
        "ትኩስ እና ጣፋጭ ምግቦችን በነጻ ለእርስዎ እናደርሳለን።\n"
        "የአገልግሎቱ መግለጫዎች እና ሂደቶች?\n"
        "1️⃣ የምዝገባ እቅድዎን እና ቀን ይምረጡ\n"
        "2️⃣ የሚወዷቸውን ምግቦች ከምግብ ዝርዝር ውስጥ ይምረጡ (ወይንም ከፈለጉ በሼፍ ውሳኔ)\n"
        "3️⃣ በየቀኑ የማስታወሻ መልክት ያገኛሉ እና አስፈላጊ ሆኖ ሲገኝ የመሰረዝ እና ወደሌላ የጊዜ ማዘዋወር ይቻላል።"
    )
    if user.id in ADMIN_IDS:
        commands_text += (
            "\n🔐 የአስተዳዳሪ ትዕዛዞች:\n"
            "/admin_update_menu - የሳምንቱን ምግብ ዝርዝር ያዘምኑ\n"
            "/admin_delete_menu - የሳምንቱን ምግብ ዝርዝር ይሰርዙ\n"
            "/admin_subscribers - ንቁ ተመዝጋቢዎችን ይመልከቱ\n"
            "/admin_payments - ክፍያዎችን ይከታተሉ\n"
            "/admin_approve_payment - ተጠባቂ ክፍያዎችን ያረጋግጡ ወይም ውድቅ ያድርጉ\n"
            "/admin_daily_orders - የዕለት ትዕዛዝ ዝርዝር ይመልከቱ\n"
            "/admin_announce - ማስታወቂያዎችን ይላኩ\n"
            "/setadminlocation - የካፌ ቦታ ያዘጋጁ\n"
            "/viewlocations - የተጋሩ ቦታዎችን ይመልከቱ"
        )
    await update.message.reply_text(commands_text, reply_markup=get_main_keyboard(user.id))

# Registration: Full name
async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message.text == '🔙 ተመለስ':
        return await back_to_main(update, context)
    await update.message.reply_text(
        "እባክዎ ሙሉ ስምዎን ያስገቡ።",
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
            await update.message.reply_text("❌ ተጠቃሚ መመዝገብ ላይ ስህተት ተከስቷል። እባክዎ እንደገና ይሞክሩ።")
            return MAIN_MENU
        cur.execute(
            "UPDATE public.users SET full_name = %s WHERE telegram_id = %s",
            (context.user_data['full_name'], user.id)
        )
        conn.commit()
        await update.message.reply_text(
            "እባክዎ ስልክ ቁጥርዎን ያስገቡ (ለምሳሌ: 0912345678)።",
            reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
        )
        return REGISTER_PHONE
    except Exception as e:
        logger.error(f"Error saving name for user {user.id}: {e}")
        await update.message.reply_text("❌ ስም በማስቀመጥ ላይ ስህተት ተከስቷል። እባክዎ እንደገና ይሞክሩ።")
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
            "❌ የማይሰራ ስልክ ቁጥር። እባክዎ ትክክለኛ የኢትዮጵያ ቁጥር ያስገቡ (ለምሳሌ: 0912345678)።",
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
        await update.message.reply_text(
            "እባክዎ የመላኪያ ቦታዎን ያስገቡ ።",
            reply_markup=ReplyKeyboardMarkup(
                [[{"text": "📍 ቦታ አጋራ", "request_location": True}, {"text": "ዝለል"}, '🔙 ተመለስ']],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        return REGISTER_LOCATION
    except Exception as e:
        logger.error(f"Error saving phone for user {user.id}: {e}")
        await update.message.reply_text("❌ ስልክ ቁጥር በማስቀመጥ ላይ ስህተት ተከስቷል። እባክዎ እንደገና ይሞክሩ።")
        return REGISTER_PHONE
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Registration: Location
async def register_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message.text == '🔙 ተመለስ':
        return await back_to_main(update, context)
    location = None
    if update.message.location:
        try:
            latitude = float(update.message.location.latitude)
            longitude = float(update.message.location.longitude)
            location = f"({latitude:.6f}, {longitude:.6f})"
        except (TypeError, ValueError) as e:
            logger.error(f"Error processing location coordinates for user {user.id}: {e}")
            await update.message.reply_text("❌ የማይሰራ ቦታ። እባክዎ ተገቢ ቦታ ያጋሩ ወይም 'ዝለል' ይፃፉ።")
            return REGISTER_LOCATION
    elif update.message.text.lower() != 'ዝለል':
        location = update.message.text
    context.user_data['location'] = location
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE public.users SET location = %s WHERE telegram_id = %s",
            (location, user.id)
        )
        conn.commit()
        # Check if location is coordinates and inside delivery polygon
        if location and location.startswith('(') and ',' in location:
            try:
                match = re.match(r'\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)', location)
                if match:
                    user_lat = float(match.group(1))
                    user_lng = float(match.group(2))
                    user_point = Point(user_lng, user_lat)
                    if not DELIVERY_POLYGON.contains(user_point):
                        await update.message.reply_text(
                            "❌ በእርስዎ ቦታ አገልግሎት አንሰጥም። እባክዎ በማስተናፈሻ አካባቢ ውስጥ ያለ ቦታ ያጋሩ።"
                        )
                        return REGISTER_LOCATION
            except Exception as e:
                logger.error(f"Error checking polygon for user {user.id}: {e}")
                await update.message.reply_text("❌ ቦታ በማስኬድ ላይ ስህተት። እባክዎ ተገቢ ቦታ ያጋሩ ወይም 'ዝለል' ይፃፉ።")
                return REGISTER_LOCATION
        # Display entered information
        registration_text = (
            "ያስገቡት መረጃ:\n"
            f"ሙሉ ስም: {context.user_data.get('full_name', 'የለም')}\n"
            f"ስልክ ቁጥር: {context.user_data.get('phone_number', 'የለም')}\n"
            f"የመላኪያ ቦታ: {context.user_data.get('location', 'የለም')}\n"
            "መረጃውን ያረጋግጡ። ትክክል ከሆነ 'መረጃው ትክክል ነው ቀጥል' ይምረጡ፣ ካልሆነ 'አስተካክል' ይምረጡ።"
        )
        keyboard = [['✅ መረጃው ትክክል ነው ቀጥል', '⛔ አስተካክል'], ['🔙 ተመለስ']]
        await update.message.reply_text(
            registration_text,
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        )
        return CONFIRM_REGISTRATION
    except Exception as e:
        logger.error(f"Error saving location for user {user.id}: {e}")
        await update.message.reply_text("❌ ቦታ በማስቀመጥ ላይ ስህተት። እባክዎ እንደገና ይሞክሩ ወይም 'ዝለል' ይፃፉ።")
        return REGISTER_LOCATION
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
            "እባክዎ ሙሉ ስምዎን ያስገቡ።",
            reply_markup=ReplyKeyboardMarkup([['🔙 ተመለስ']], resize_keyboard=True)
        )
        return REGISTER_NAME
    elif choice == '✅ መረጃው ትክክል ነው ቀጥል':
        await update.message.reply_text(
            "✅ Registration completed successfully!",
            reply_markup=get_main_keyboard(user.id)
        )
        return MAIN_MENU
    else:
        await update.message.reply_text(
            "❌ እባክዎ '✅ መረጃው ትክክል ነው ቀጥል' ወይም '⛔ አስተካክል' ይምረጡ።",
            reply_markup=ReplyKeyboardMarkup(
                [['✅ መረጃው ትክክል ነው ቀጥል', '⛔ አስተካክል'], ['🔙 ተመለስ']],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        return CONFIRM_REGISTRATION

# Admin: Approve or reject payment
async def admin_approve_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አብራሪ የለዎትም።", reply_markup=get_main_keyboard(user.id))
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
                "📭 ለፍተሻ ተጠባቂ ክፍያዎች የሉም።",
                reply_markup=get_main_keyboard(user.id)
            )
            return MAIN_MENU
        for payment_id, full_name, username, amount, receipt_url, user_id, subscription_id in payments:
            keyboard = [
                [InlineKeyboardButton("አረጋግጥ", callback_data=f"approve_payment_{payment_id}"),
                 InlineKeyboardButton("ውድቅ", callback_data=f"reject_payment_{payment_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                if receipt_url and validators.url(receipt_url):
                    try:
                        await context.bot.send_photo(
                            chat_id=user.id,
                            photo=receipt_url,
                            caption=f"ክፍያ #{payment_id}\n"
                                    f"ተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\n"
                                    f"መጠን: {amount:.2f} ብር",
                            reply_markup=reply_markup
                        )
                    except Exception as e:
                        logger.error(f"Error sending photo for payment {payment_id} to admin {user.id}: {e}")
                        await context.bot.send_message(
                            chat_id=user.id,
                            text=f"ክፍያ #{payment_id}\n"
                                 f"ተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\n"
                                 f"መጠን: {amount:.2f} ብር\n"
                                 f"የስምልጣ URL: {receipt_url}\n"
                                 f"(ማሳወቂያ: ስቶ ማሳየት ስህተት ተከሰተ: {str(e)})",
                            reply_markup=reply_markup
                        )
                else:
                    await context.bot.send_message(
                        chat_id=user.id,
                        text=f"ክፍያ #{payment_id}\n"
                             f"ተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\n"
                             f"መጠን: {amount:.2f} ብር\n"
                             f"የስምልጣ URL: {receipt_url or 'የለም'} (የማይሰራ ወይም የለም URL)",
                        reply_markup=reply_markup
                    )
            except Exception as e:
                logger.error(f"Error processing payment {payment_id} for admin {user.id}: {e}")
                await context.bot.send_message(
                    chat_id=user.id,
                    text=f"ክፍያ #{payment_id}\n"
                         f"ተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\n"
                         f"መጠን: {amount:.2f} ብር\n"
                         f"ስህተት: የክፍያ ዝርዝር ማስተካከል አልተሳካም",
                    reply_markup=reply_markup
                )
        await update.message.reply_text(
            "📷 ከላይ ተጠባቂ የክፍያ ስምልጣዎች ናቸው። ንጣፎችን ተጠቀሙ ለአረጋግጥ ወይም ለውድቅ።",
            reply_markup=get_main_keyboard(user.id)
        )
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error fetching pending payments: {e}")
        await update.message.reply_text("❌ ተጠባቂ ክፍያዎችን መጫን ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Handle payment approval/rejection callback
async def handle_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')
    action = data[0]
    payment_id = data[2]
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, subscription_id FROM public.payments WHERE id = %s AND status = 'pending'",
            (payment_id,)
        )
        payment = cur.fetchone()
        if not payment:
            await query.message.reply_text("❌ ክፍያ አልተገኘም ወይም ቀደም ብሎ ተቀነባ ነው።")
            return
        user_id, subscription_id = payment
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
            await query.message.reply_text("✅ ክፍያ ተቀበለ።")
            # Send success message and help text
            await context.bot.send_message(
                chat_id=user_id,
                text="✅ የእርስዎ ክፍያ ተቀበለ! ምግቦችዎ ተደረጉ።"
            )
            fake_update = Update(0, message=type('obj', (object,), {'effective_user': type('obj', (object,), {'id': user_id})}))
            await send_help_text(fake_update, context)
        elif action == 'reject':
            cur.execute(
                "UPDATE public.payments SET status = 'rejected' WHERE id = %s",
                (payment_id,)
            )
            cur.execute(
                "DELETE FROM public.orders WHERE subscription_id = %s",
                (subscription_id,)
            )
            cur.execute(
                "DELETE FROM public.subscriptions WHERE id = %s",
                (subscription_id,)
            )
            conn.commit()
            await query.message.reply_text("❌ ክፍያ ተውደቀ።")
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ የእርስዎ ክፍያ ተውደቀ። እባክዎ ከ /subscribe ጋር እንደገና ይጀምሩ።",
                reply_markup=get_main_keyboard(user_id)
            )
    except Exception as e:
        logger.error(f"Error processing payment callback for payment {payment_id}: {e}")
        await query.message.reply_text("❌ የክፍያ እርምጃ በማስተካከል ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Admin: Update Menu
async def admin_update_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አብራሪ የለዎትም።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    await update.message.reply_text(
        "📋 አዲሱን ምግብ ዝርዝር ያስገቡ, አንድ ንጥል በአንድ መስመር በቅርጽ: day category name price\n"
        "ለምሳሌ:\n"
        "Monday fasting ምስር ወጥ 160\n"
        "Monday non_fasting ምስር በስጋ 260",
        reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
    )
    return ADMIN_UPDATE_MENU

async def process_admin_update_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አብራሪ የለዎትም።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    if update.message.text.lower() in ['ሰርዝ', '🔙 ተመለስ']:
        await update.message.reply_text("❌ የምግብ ዝርዝር ማዘመን ተሰርዟል።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    try:
        lines = update.message.text.strip().split('\n')
        menu_data = []
        for idx, line in enumerate(lines, 1):
            parts = line.strip().split()
            if len(parts) < 4:
                raise ValueError(f"Invalid format on line {idx}: {line}")
            day = parts[0]
            category = parts[1]
            price = float(parts[-1])
            name = ' '.join(parts[2:-1])
            if day not in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']:
                raise ValueError(f"Invalid day on line {idx}: {day}")
            if category not in ['fasting', 'non_fasting']:
                raise ValueError(f"Invalid category on line {idx}: {category}")
            menu_data.append({
                'id': idx,
                'name': name,
                'price': price,
                'day': day,
                'category': category
            })
        if not menu_data:
            raise ValueError("No valid menu items provided.")
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
        await update.message.reply_text("✅ ምግብ ዝርዝር በተሳካ ሁኔታ ተዘመነ።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error updating menu: {e}")
        await update.message.reply_text(f"❌ የማይሰራ ቅርጽ ወይም ምግብ ዝርዝር ማዘመን ላይ ስህተት: {str(e)}። እባክዎ እንደገና ይሞክሩ።", reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True))
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
        await update.message.reply_text("❌ አብራሪ የለዎትም።", reply_markup=get_main_keyboard(user.id))
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
        menu = cur.fetchone()
        if not menu:
            await update.message.reply_text("❌ ለዚህ ሳምንት ምግብ ዝርዝር አልተገኘም።", reply_markup=get_main_keyboard(user.id))
            return MAIN_MENU
        menu_items = json.loads(menu[0]) if isinstance(menu[0], str) else menu[0]
        if not menu_items:
            await update.message.reply_text("❌ ምግብ ዝርዝሩ ባዶ ነው።", reply_markup=get_main_keyboard(user.id))
            return MAIN_MENU
        context.user_data['week_start'] = week_start
        context.user_data['menu_items'] = menu_items
        text = build_delete_menu_text(menu_items, week_start)
        await update.message.reply_text(
            f"{text}\nለማስወገድ የንጥሉን ያስገቡ (ለምሳሌ '1') ወይም 'ሰርዝ'።",
            reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
        )
        return ADMIN_DELETE_MENU
    except Exception as e:
        logger.error(f"Error fetching menu for deletion: {e}")
        await update.message.reply_text("❌ ምግብ ዝርዝር መጫን ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

async def process_admin_delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አብራሪ የለዎትም።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    if update.message.text.lower() in ['ሰርዝ', '🔙 ተመለስ']:
        await update.message.reply_text("❌ የምግብ ዝርዝር ማስወገድ ተሰርዟል።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    try:
        item_idx = int(update.message.text) - 1
        menu_items = context.user_data.get('menu_items', [])
        week_start = context.user_data.get('week_start')
        if not (0 <= item_idx < len(menu_items)):
            await update.message.reply_text(
                f"❌ የማይሰራ የንጥል ቁጥር። 1 እስከ {len(menu_items)} መካከል ይምረጡ።",
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
        await update.message.reply_text("✅ የምግብ ዝርዝር ንጥል በተሳካ ሁኔታ ተሰርዟል።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error deleting menu item: {e}")
        await update.message.reply_text("❌ የምግብ ዝርዝር ንጥል በማስወገድ ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True))
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
        await update.message.reply_text("❌ አብራሪ የለዎትም።", reply_markup=get_main_keyboard(user.id))
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
            await update.message.reply_text("❌ ንቁ ወይም ተጠባቂ ተመዝጋቢዎች አልተገኙም።", reply_markup=get_main_keyboard(user.id))
            return MAIN_MENU
        text = "📋 ንቁ/ተጠባቂ ተመዝጋቢዎች:\n"
        for full_name, username, plan_type, meals_remaining, expiry_date in subscribers:
            text += (
                f"ስም: {full_name or 'የለም'} (@{username or 'የለም'})\n"
                f"እቅድ: {plan_type.capitalize()}\n"
                f"ቀሪ ምግቦች: {meals_remaining}\n"
                f"ጫና: {expiry_date.strftime('%Y-%m-%d')}\n"
            )
        await update.message.reply_text(text, reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error fetching subscribers: {e}")
        await update.message.reply_text("❌ ተመዝጋቢዎችን መጫን ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=get_main_keyboard(user.id))
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
        await update.message.reply_text("❌ አብራሪ የለዎትም።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT p.id, u.full_name, u.username, p.amount, p.status, p.created_at "
            "FROM public.payments p JOIN public.users u ON p.user_id = u.telegram_id "
            "ORDER BY p.created_at DESC"
        )
        payments = cur.fetchall()
        if not payments:
            await update.message.reply_text("❌ ክፍያዎች አልተገኙም።", reply_markup=get_main_keyboard(user.id))
            return MAIN_MENU
        text = "💸 የክፍያ ታሪክ:\n"
        for payment_id, full_name, username, amount, status, created_at in payments:
            text += (
                f"ክፍያ #{payment_id}\n"
                f"ተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\n"
                f"መጠን: {amount:.2f} ብር\n"
                f"ሁኔታ: {status.capitalize()}\n"
                f"ቀን: {created_at.strftime('%Y-%m-%d %H:%M')}\n"
            )
        await update.message.reply_text(text, reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error fetching payments: {e}")
        await update.message.reply_text("❌ ክፍያዎችን መጫን ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=get_main_keyboard(user.id))
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
        await update.message.reply_text("❌ አብራሪ የለዎትም።", reply_markup=get_main_keyboard(user.id))
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
                await update.message.reply_text(f"❌ ለ{week_start} - {week_end} ሳምንት ትዕዛዞች የሉም።", reply_markup=get_main_keyboard(user.id))
                return MAIN_MENU
            text = f"📅 ለ{week_start} - {week_end} ሳምንት ትዕዛዞች (ዛሬ የለም):\n"
        else:
            text = f"📅 ለ{today} ትዕዛዞች:\n"
        for full_name, username, meal_date, items_json in orders:
            items = json.loads(items_json) if isinstance(items_json, str) else items_json
            text += f"ተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\nቀን: {meal_date}\n"
            for item in items:
                text += f"- {item['name']} ({item['category']})\n"
            text += "\n"
        await update.message.reply_text(text, reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error fetching daily orders: {e}")
        await update.message.reply_text("❌ የዕለት ትዕዛዞችን መጫን ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=get_main_keyboard(user.id))
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
        await update.message.reply_text("❌ አብራሪ የለዎትም።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    await update.message.reply_text(
        "📢 ለሁሉም ተጠቃሚዎች ለማስተላለፍ መልእክት ያስገቡ:",
        reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True)
    )
    return ADMIN_ANNOUNCE

async def process_admin_announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አብራሪ የለዎትም።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    if update.message.text.lower() in ['ሰርዝ', '🔙 ተመለስ']:
        await update.message.reply_text("❌ ማስታወቂያ ተሰርዟል።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    announcement = update.message.text
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT telegram_id FROM public.users")
        users = cur.fetchall()
        for user_id, in users:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"📢 ማስታወቂያ: {announcement}"
                )
            except Exception as e:
                logger.error(f"Error sending announcement to user {user_id}: {e}")
        await update.message.reply_text("✅ ማስታወቂያ ለሁሉም ተጠቃሚዎች ተላከ።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error sending announcement: {e}")
        await update.message.reply_text("❌ ማስታወቂያ በማላክ ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '🔙 ተመለስ']], resize_keyboard=True))
        return ADMIN_ANNOUNCE
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Admin: Set Location
async def set_admin_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አብራሪ የለዎትም።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    await update.message.reply_text(
        "📍 የካፌ ቦታ ያጋሩ ወይም 'ዝለል' በእጅ ለመጻፍ።",
        reply_markup=ReplyKeyboardMarkup(
            [[{"text": "📍 ቦታ አጋራ", "request_location": True}, "ዝለል", '🔙 ተመለስ']],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    )
    return SET_ADMIN_LOCATION

async def process_set_admin_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አብራሪ የለዎትም።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    if update.message.text in ['🔙 ተመለስ', 'ዝለል']:
        await update.message.reply_text("❌ ቦታ ማዘጋጀት ተሰርዟል።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    location = None
    if update.message.location:
        try:
            latitude = float(update.message.location.latitude)
            longitude = float(update.message.location.longitude)
            location = f"({latitude:.6f}, {longitude:.6f})"
        except Exception as e:
            logger.error(f"Error processing location: {e}")
            await update.message.reply_text("❌ የማይሰራ ቦታ። እባክዎ እንደገና ይሞክሩ ወይም 'ዝለል' ይፃፉ።", reply_markup=ReplyKeyboardMarkup([["ዝለል", '🔙 ተመለስ']], resize_keyboard=True))
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
        await update.message.reply_text("✅ ቦታ በተሳካ ሁኔታ ተዘጋጅቷል።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error setting admin location: {e}")
        await update.message.reply_text("❌ ቦታ በማዘጋጀት ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=ReplyKeyboardMarkup([["ዝለል", '🔙 ተመለስ']], resize_keyboard=True))
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
        await update.message.reply_text("❌ አብራሪ የለዎትም።", reply_markup=get_main_keyboard(user.id))
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
            await update.message.reply_text("❌ የተዘጋጁ ቦታዎች የሉም።", reply_markup=get_main_keyboard(user.id))
            return MAIN_MENU
        text = "📍 የአስተዳዳሪ ቦታዎች:\n"
        for key, value in locations:
            admin_id = key.replace('admin_location_', '')
            text += f"አስተዳዳሪ {admin_id}: {value}\n"
        await update.message.reply_text(text, reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error fetching locations: {e}")
        await update.message.reply_text("❌ ቦታዎችን መጫን ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
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
        "❌ ሥራ ተሰርዟል።",
        reply_markup=ReplyKeyboardRemove()
    )
    await update.message.reply_text(
        "👋 እንኳን ተመልሰው መጡ! አማራጭ ይምረጡ:",
        reply_markup=get_main_keyboard(user.id)
    )
    return MAIN_MENU

# Error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.message:
        await update.message.reply_text("❌ ስህተት ተከሰተ። እባክዎ እንደገና ይሞክሩ ወይም ድጋፍ ያነጋግሩ።", reply_markup=get_main_keyboard(update.effective_user.id))

# Main function to run the bot
def main():
    try:
        init_db()
        application = Application.builder().token(BOT_TOKEN).build()
        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler('start', start),
                CommandHandler('admin_update_menu', admin_update_menu),
                CommandHandler('admin_delete_menu', admin_delete_menu),
                CommandHandler('admin_subscribers', admin_subscribers),
                CommandHandler('admin_payments', admin_payments),
                CommandHandler('admin_approve_payment', admin_approve_payment),
                CommandHandler('admin_daily_orders', admin_daily_orders),
                CommandHandler('admin_announce', admin_announce),
                CommandHandler('setadminlocation', set_admin_location),
                CommandHandler('viewlocations', view_locations),
                CommandHandler('cancel', cancel)
            ],
            states={
                MAIN_MENU: [
                    MessageHandler(filters.Regex('^📋 ይመዝገቡ$'), register_name),
                    MessageHandler(filters.Regex('^💬 ድጋፍ$'), support_menu),
                    MessageHandler(filters.Regex('^🔐 ምግብ ዝርዝር አዘምን$'), admin_update_menu),
                    MessageHandler(filters.Regex('^🔐 ምግብ ዝርዝር ሰርዝ$'), admin_delete_menu),
                    MessageHandler(filters.Regex('^🔐 ተመዝጋቢዎችን ተመልከት$'), admin_subscribers),
                    MessageHandler(filters.Regex('^🔐 ክፍያዎችን ተመልከት$'), admin_payments),
                    MessageHandler(filters.Regex('^🔐 ክፍያዎችን አረጋግጥ$'), admin_approve_payment),
                    MessageHandler(filters.Regex('^🔐 የዕለት ትዕዛዞች$'), admin_daily_orders),
                    MessageHandler(filters.Regex('^🔐 ማስታወቂያ$'), admin_announce),
                    MessageHandler(filters.Regex('^🔐 ቦታ አዘጋጅ$'), set_admin_location),
                    MessageHandler(filters.Regex('^🔐 ቦታዎችን ተመልከት$'), view_locations),
                ],
                REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_name)],
                REGISTER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_phone)],
                REGISTER_LOCATION: [
                    MessageHandler(filters.LOCATION | (filters.TEXT & ~filters.COMMAND), register_location)
                ],
                CONFIRM_REGISTRATION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_registration)
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
                SUPPORT_MENU: [
                    MessageHandler(filters.Regex('^🔙 ተመለስ$'), back_to_main)
                ],
            },
            fallbacks=[CommandHandler('cancel', cancel)],
            allow_reentry=True
        )
        application.add_handler(conv_handler)
        application.add_handler(CallbackQueryHandler(handle_payment_callback))
        application.add_error_handler(error_handler)
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