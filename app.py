import os
import logging
import json
import psycopg2
import re
from datetime import datetime, timedelta, time
import pytz
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, ConversationHandler, filters, JobQueue, CallbackQueryHandler
import math
import validators
from time import sleep

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Your credentials
BOT_TOKEN = "7386306627:AAHdCm0OMiitG09dEbD0qmjbNT-pvq0Ny6A"
DATABASE_URL = "postgresql://postgres.unceacyznxuawksbfctj:Aster#123#@aws-1-eu-north-1.pooler.supabase.com:6543/postgres"
ADMIN_IDS = [8188464845]

# Admin locations (hardcoded)
ADMIN_LOCATIONS = [
    (9.020238599143552, 38.82560078203035),
    (9.017190196514154, 38.75281767667821),
    (8.98208254568819, 38.75948863161473),
    (8.980054995596422, 38.77906699321482),
    (8.985448934391043, 38.79958228020363),
    (9.006143350714895, 38.78995524036579)
]

# Time zone for East Africa Time (EAT, UTC+3)
EAT = pytz.timezone('Africa/Nairobi')

# Conversation states
(
    MAIN_MENU, REGISTER_NAME, REGISTER_PHONE, REGISTER_LOCATION, CONFIRM_REGISTRATION,
    CHOOSE_PLAN, CHOOSE_DATE, MEAL_SELECTION, CONFIRM_MEAL, PAYMENT_UPLOAD,
    RESCHEDULE_MEAL, ADMIN_UPDATE_MENU, ADMIN_ANNOUNCE, ADMIN_DAILY_ORDERS,
    ADMIN_DELETE_MENU, SET_ADMIN_LOCATION, ADMIN_APPROVE_PAYMENT
) = range(17)

# Database connection helper
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        conn.set_session(autocommit=False)
        return conn
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        raise

# Haversine distance calculation
def haversine(lat1, lon1, lat2, lon2):
    R = 6371  # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

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

        # Create referrals table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS public.referrals (
                id SERIAL PRIMARY KEY,
                referrer_id BIGINT,
                referred_id BIGINT,
                referral_code VARCHAR(50) UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (referrer_id) REFERENCES public.users(telegram_id),
                FOREIGN KEY (referred_id) REFERENCES public.users(telegram_id)
            )
        ''')
        cur.execute("ALTER TABLE public.referrals DISABLE ROW LEVEL SECURITY")

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
    text = f"📋 የምግብ ዝርዝር ለሳምንቱ መጀመሪያ {week_start} (ለመሰረዝ የተወሰነ ንጥል ይምረጡ):\n\n"
    for idx, item in enumerate(sorted_items, 1):
        text += f"{idx}. {item['day']}: {item['name']} - {item['price']:.2f} ብር\n"
    return text

def get_main_keyboard(user_id):
    keyboard = [
        ['🍽 ምግብ ዝርዝር', '🛒 ምዝገባ'],
        ['📋 የእኔ ምዝገባ', '📅 የእኔ ምግቦች'],
        ['📞 እውቂያ', '🔗 ግብዣ', '🍴 ምግብ ምረጥ']
    ]
    if user_id in ADMIN_IDS:
        keyboard.extend([
            ['🔐 ምግብ ዝርዝር አዘምን', '🔐 ምግብ ዝርዝር ሰርዝ'],
            ['🔐 ተመዝጋቢዎችን ተመልከት', '🔐 ክፍያዎችን ተመልከት'],
            ['🔐 ክፍያዎችን አረጋግጥ', '🔐 የዕለት ትዕዛዞች'],
            ['🔐 ማስታወቂያ', '🔐 ቦታ አዘጋጅ'],
            ['🔐 ቦታዎችን ተመልከት']
        ])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# Start command with updated onboarding message
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Onboarding message in Amharic with command list
        onboarding_text = (
            "👋 እንኳን ወደ ኦዝ ኪችን የምግብ ምዝገባ በደና መጡ!\n"
            "ትኩስ እና ጣፋጭ ምግቦችን በነጻ ለእርስዎ እናደርሳለን።\n\n"
            "የአገልግሎቱ መግለጫዎች እና ሂደቶች?\n"
            "1️⃣ የምዝገባ እቅድዎን እና ቀን ይምረጡ\n"
            "2️⃣ የሚወዷቸውን ምግቦች ከምግብ ዝርዝር ውስጥ ይምረጡ (ወይንም ከፈለጉ በሼፍ ውሳኔ)\n"
            "3️⃣ በየቀኑ የማስታወሻ መልክት ያገኛሉ እና አስፈላጊ ሆኖ ሲገኝ የመሰረዝ እና ወደሌላ የጊዜ ማዘዋወር ይቻላል።\n\n"
            "📋 የሚገኙ ትዕዛዞች:\n"
            "🍽 /menu - የሳምንቱን ምግብ ዝርዝር ይመልከቱ\n"
            "🛒 /subscribe - የምዝገባ እቅድ ይምረጡ\n"
            "📋 /my_subscription - የምዝገባ ሁኔታን ይመልከቱ\n"
            "📅 /my_meals - የመረጧቸውን ምግቦች ይመልከቱ\n"
            "📞 /contact - ስልክ ቁጥር ያዘምኑ\n"
            "🔗 /refer - ጓደኛን ይጋብዙ\n"
            "❓ /help - ይህን የእገዛ መልእክት ይመልከቱ\n"
            "🍴 /select_meals - ምግቦችዎን ይምረጡ"
        )
        keyboard = get_main_keyboard(user.id)

        # Add admin commands
        if user.id in ADMIN_IDS:
            onboarding_text += (
                "\n\n🔐 የአስተዳዳሪ ትዕዛዞች:\n"
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

        # Check if user is registered
        cur.execute("SELECT full_name, phone_number FROM public.users WHERE telegram_id = %s", (user.id,))
        user_data = cur.fetchone()

        if user_data and user_data[0] and user_data[1]:
            await update.message.reply_text(
                f"👋 እንኳን ተመልሰው መጡ {user.first_name}!\n\n{onboarding_text}",
                reply_markup=keyboard
            )
            return MAIN_MENU
        else:
            await update.message.reply_text(
                f"{onboarding_text}\n\n"
                "👉 ከታች በመመዝገብ ይጀምሩ\n"
                "እባክዎ ሙሉ ስምዎን ያስገቡ።",
                reply_markup=ReplyKeyboardMarkup([['⬅️ ተመለስ']], resize_keyboard=True)
            )
            return REGISTER_NAME
    except Exception as e:
        logger.error(f"Error in start for user {user.id}: {e}")
        await update.message.reply_text("❌ በመጀመር ላይ ስህተት ተከስቷል። እባክዎ እንደገና ይሞክሩ።")
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Help command
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    commands_text = (
        "👋 እንኳን ወደ ኦዝ ኪችን የምግብ ምዝገባ በደና መጡ!\n"
        "ትኩስ እና ጣፋጭ ምግቦችን በነጻ ለእርስዎ እናደርሳለን።\n\n"
        "የአገልግሎቱ መግለጫዎች እና ሂደቶች?\n"
        "1️⃣ የምዝገባ እቅድዎን እና ቀን ይምረጡ\n"
        "2️⃣ የሚወዷቸውን ምግቦች ከምግብ ዝርዝር ውስጥ ይምረጡ (ወይንም ከፈለጉ በሼፍ ውሳኔ)\n"
        "3️⃣ በየቀኑ የማስታወሻ መልክት ያገኛሉ እና አስፈላጊ ሆኖ ሲገኝ የመሰረዝ እና ወደሌላ የጊዜ ማዘዋወር ይቻላል።\n\n"
        "📋 የሚገኙ ትዕዛዞች:\n"
        "🍽 /menu - የሳምንቱን ምግብ ዝርዝር ይመልከቱ\n"
        "🛒 /subscribe - የምዝገባ እቅድ ይምረጡ\n"
        "📋 /my_subscription - የምዝገባ ሁኔታን ይመልከቱ\n"
        "📅 /my_meals - የመረጧቸውን ምግቦች ይመልከቱ\n"
        "📞 /contact - ስልክ ቁጥር ያዘምኑ\n"
        "🔗 /refer - ጓደኛን ይጋብዙ\n"
        "❓ /help - ይህን የእገዛ መልእክት ይመልከቱ\n"
        "🍴 /select_meals - ምግቦችዎን ይምረጡ"
    )

    if user.id in ADMIN_IDS:
        commands_text += (
            "\n\n🔐 የአስተዳዳሪ ትዕዛዞች:\n"
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

    await update.message.reply_text(
        commands_text,
        reply_markup=get_main_keyboard(user.id)
    )
    return MAIN_MENU

# Registration: Full name
async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message.text == '⬅️ ተመለስ':
        return await cancel(update, context)
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
            "እባክዎ ስልክ ቁጥርዎን ያካፍሉ።",
            reply_markup=ReplyKeyboardMarkup(
                [[{"text": "📱 እውቂያ አጋራ", "request_contact": True}, '⬅️ ተመለስ']],
                resize_keyboard=True,
                one_time_keyboard=True
            )
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

# Registration: Phone number
async def register_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message.text == '⬅️ ተመለስ':
        return await cancel(update, context)
    phone_number = update.message.contact.phone_number if update.message.contact else update.message.text
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
                [[{"text": "📍 ቦታ አጋራ", "request_location": True}, {"text": "ዝለል"}, '⬅️ ተመለስ']],
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
    if update.message.text == '⬅️ ተመለስ':
        return await cancel(update, context)
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

        # Check distance if location is coordinates
        if location and location.startswith('(') and ',' in location:
            try:
                match = re.match(r'\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)', location)
                if match:
                    user_lat = float(match.group(1))
                    user_lng = float(match.group(2))
                    dists = [haversine(user_lat, user_lng, lat, lng) for lat, lng in ADMIN_LOCATIONS]
                    min_dist = min(dists)
                    if min_dist > 1:
                        await update.message.reply_text(
                            f"❌ በእርስዎ ቦታ አገልግሎት አንሰጥም (ርቀት: {min_dist:.2f}ኪ.ሜ > 1ኪ.ሜ)። እባክዎ በ1ኪ.ሜ ርቀት ውስጥ ያለ ቦታ ያጋሩ።"
                        )
                        return REGISTER_LOCATION
            except Exception as e:
                logger.error(f"Error calculating distance for user {user.id}: {e}")
                await update.message.reply_text("❌ ቦታ በማስኬድ ላይ ስህተት። እባክዎ ተገቢ ቦታ ያጋሩ ወይም 'ዝለል' ይፃፉ።")
                return REGISTER_LOCATION

        # Display entered information
        registration_text = (
            "ያስገቡት መረጃ:\n\n"
            f"ሙሉ ስም: {context.user_data.get('full_name', 'የለም')}\n"
            f"ስልክ ቁጥር: {context.user_data.get('phone_number', 'የለም')}\n"
            f"የመላኪያ ቦታ: {context.user_data.get('location', 'የለም')}\n\n"
            "መረጃውን ያረጋግጡ። ትክክል ከሆነ 'መረጃው ትክክል ነው ቀጥል' ይምረጡ፣ ካልሆነ 'አስተካክል' ይምረጡ።"
        )
        keyboard = [['✅ መረጃው ትክክል ነው ቀጥል', '⛔ አስተካክል'], ['⬅️ ተመለስ']]
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

    if choice == '⬅️ ተመለስ':
        return await cancel(update, context)
    elif choice == '⛔ አስተካክል':
        context.user_data.clear()
        await update.message.reply_text(
            "እባክዎ ሙሉ ስምዎን ያስገቡ።",
            reply_markup=ReplyKeyboardMarkup([['⬅️ ተመለስ']], resize_keyboard=True)
        )
        return REGISTER_NAME
    elif choice == '✅ መረጃው ትክክል ነው ቀጥል':
        await update.message.reply_text(
            "📦 የምዝገባ እቅድዎን ይምረጡ:\n\n"
            "🍽️ የምሳ\n"
            "🥘 የእራት\n",
            reply_markup=ReplyKeyboardMarkup(
                [['🍽️ የምሳ', '🥘 የእራት'], ['⬅️ ተመለስ']],
                resize_keyboard=True
            )
        )
        return CHOOSE_PLAN
    else:
        await update.message.reply_text(
            "❌ እባክዎ '✅ መረጃው ትክክል ነው ቀጥል' ወይም '⛔ አስተካክል' ይምረጡ።",
            reply_markup=ReplyKeyboardMarkup(
                [['✅ መረጃው ትክክል ነው ቀጥል', '⛔ አስተካክል'], ['⬅️ ተመለስ']],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        return CONFIRM_REGISTRATION

# Choose subscription plan
async def choose_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    choice = update.message.text
    if choice == '/subscribe' or '🛒' in choice:
        await update.message.reply_text(
            "📦 የምዝገባ እቅድዎን ይምረጡ:\n\n"
            "🍽️ የምሳ\n"
            "🥘 የእራት\n",
            reply_markup=ReplyKeyboardMarkup(
                [['🍽️ የምሳ', '🥘 የእራት'], ['⬅️ ተመለስ']],
                resize_keyboard=True
            )
        )
        return CHOOSE_PLAN

    plans = {
        '🍽️ የምሳ': {'type': 'lunch', 'price_per_meal': 0, 'duration_days': 30},
        '🥘 የእራት': {'type': 'dinner', 'price_per_meal': 0, 'duration_days': 30}
    }

    if choice == '⬅️ ተመለስ':
        return await cancel(update, context)

    if choice not in plans:
        await update.message.reply_text(
            "❌ የማይሰራ ምርጫ። እባክዎ '🍽️ የምሳ' ወይም '🥘 የእራት' ይምረጡ።",
            reply_markup=ReplyKeyboardMarkup(
                [['🍽️ የምሳ', '🥘 የእራት'], ['⬅️ ተመለስ']],
                resize_keyboard=True
            )
        )
        return CHOOSE_PLAN

    context.user_data['plan'] = plans[choice]
    await update.message.reply_text(
        "📅 ለምግቦችዎ ቀናት ይምረጡ (ከሰኞ እስከ እሑድ):",
        reply_markup=ReplyKeyboardMarkup(
            [['ሰኞ', 'ማክሰኞ', 'እሮብ'],
             ['ሐሙስ', 'አርብ', 'ቅዳሜ'],
             ['እሑድ', 'ጨርሻል', '⬅️ ተመለስ']],
            resize_keyboard=True
        )
    )
    context.user_data['selected_dates'] = []
    return CHOOSE_DATE

# Choose dates
async def choose_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    choice = update.message.text
    valid_days = ['ሰኞ', 'ማክሰኞ', 'እሮብ', 'ሐሙስ', 'አርብ', 'ቅዳሜ', 'እሑድ']
    valid_days_en = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

    if choice == '⬅️ ተመለስ':
        await update.message.reply_text(
            "📦 የምዝገባ እቅድዎን ይምረጡ:\n\n"
            "🍽️ የምሳ\n"
            "🥘 የእራት\n",
            reply_markup=ReplyKeyboardMarkup(
                [['🍽️ የምሳ', '🥘 የእራት'], ['⬅️ ተመለስ']],
                resize_keyboard=True
            )
        )
        return CHOOSE_PLAN
    elif choice == 'ጨርሻል':
        selected_dates = context.user_data.get('selected_dates', [])
        if not selected_dates:
            await update.message.reply_text(
                "❌ ቢያንስ አንድ ቀን ይምረጡ።",
                reply_markup=ReplyKeyboardMarkup(
                    [['ሰኞ', 'ማክሰኞ', 'እሮብ'],
                     ['ሐሙስ', 'አርብ', 'ቅዳሜ'],
                     ['እሑድ', 'ጨርሻል', '⬅️ ተመለስ']],
                    resize_keyboard=True
                )
            )
            return CHOOSE_DATE

        conn = None
        cur = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            plan = context.user_data.get('plan')
            expiry_date = datetime.now(EAT) + timedelta(days=plan['duration_days'])
            
            # Convert Amharic days to English for storage
            selected_dates_en = [valid_days_en[valid_days.index(day)] for day in selected_dates]
            
            # Check if selected_dates column exists
            cur.execute("""
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                AND table_name = 'subscriptions'
                AND column_name = 'selected_dates'
            """)
            if not cur.fetchone():
                logger.error("selected_dates column missing in subscriptions table")
                await update.message.reply_text(
                    "❌ የዳታቤዝ ቅንብር ስህተት። እባክዎ ድጋፍ ያነጋግሩ ወይም ቆይተው እንደገና ይሞክሩ።",
                    reply_markup=get_main_keyboard(user.id)
                )
                return MAIN_MENU

            cur.execute(
                "INSERT INTO public.subscriptions (user_id, plan_type, meals_remaining, selected_dates, expiry_date, status) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (user.id, plan['type'], len(selected_dates), json.dumps(selected_dates_en), expiry_date, 'pending')
            )
            subscription_id = cur.fetchone()[0]
            conn.commit()

            context.user_data['subscription_id'] = subscription_id
            # Proceed to meal selection
            await update.message.reply_text(
                f"📝 {len(selected_dates)} ቀን መርጠዋል\n\n"
                "አሁን፣ ምግቦችዎን ለመምረጥ /select_meals ይጠቀሙ።",
                reply_markup=ReplyKeyboardMarkup([['🍴 ምግብ ምረጥ', 'ሰርዝ'], ['⬅️ ተመለስ']], resize_keyboard=True)
            )
            return MAIN_MENU
        except Exception as e:
            logger.error(f"Error saving subscription for user {user.id}: {e}")
            await update.message.reply_text(
                "❌ ምዝገባ በማስኬድ ላይ ስህተት። እባክዎ እንደገና ይሞክሩ ወይም ድጋፍ ያነጋግሩ።",
                reply_markup=ReplyKeyboardMarkup(
                    [['ሰኞ', 'ማክሰኞ', 'እሮብ'],
                     ['ሐሙስ', 'አርብ', 'ቅዳሜ'],
                     ['እሑድ', 'ጨርሻል', '⬅️ ተመለስ']],
                    resize_keyboard=True
                )
            )
            return CHOOSE_DATE
        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()
    elif choice in valid_days:
        selected_dates = context.user_data.get('selected_dates', [])
        if choice not in selected_dates:
            selected_dates.append(choice)
            context.user_data['selected_dates'] = selected_dates
        await update.message.reply_text(
            f"✅ {choice} ታክሏል። ተጨማሪ ቀናት ይምረጡ ወይም 'ጨርሻል' ይጫኑ።",
            reply_markup=ReplyKeyboardMarkup(
                [['ሰኞ', 'ማክሰኞ', 'እሮብ'],
                 ['ሐሙስ', 'አርብ', 'ቅዳሜ'],
                 ['እሑድ', 'ጨርሻል', '⬅️ ተመለስ']],
                resize_keyboard=True
            )
        )
        return CHOOSE_DATE
    else:
        await update.message.reply_text(
            "❌ የማይሰራ ምርጫ። እባክዎ ቀን ወይም 'ጨርሻል' ይምረጡ።",
            reply_markup=ReplyKeyboardMarkup(
                [['ሰኞ', 'ማክሰኞ', 'እሮብ'],
                 ['ሐሙስ', 'አርብ', 'ቅዳሜ'],
                 ['እሑድ', 'ጨርሻል', '⬅️ ተመለስ']],
                resize_keyboard=True
            )
        )
        return CHOOSE_DATE

# Show weekly menu
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            await update.message.reply_text(
                "❌ ለዚህ ሳምንት ምግብ ዝርዝር የለም። አስተዳዳሪዎች፣ እባክዎት ምግብ ዝርዝሩን በ /admin_update_menu ያዘምኑ።",
                reply_markup=get_main_keyboard(update.effective_user.id)
            )
            return MAIN_MENU

        menu_items = json.loads(menu[0]) if isinstance(menu[0], str) else menu[0]
        if not menu_items or not isinstance(menu_items, list):
            logger.error(f"Invalid menu data for week {week_start}: {menu_items}")
            await update.message.reply_text(
                "❌ የማይሰራ የምግብ ዝርዝር ውሂብ። አስተዳዳሪዎች፣ እባክዎት ምግብ ዝርዝሩን በ /admin_update_menu ያዘምኑ።",
                reply_markup=get_main_keyboard(update.effective_user.id)
            )
            return MAIN_MENU

        # Validate menu items
        valid_items = [
            item for item in menu_items 
            if isinstance(item, dict) and all(key in item for key in ['id', 'name', 'price', 'day', 'category'])
        ]
        if not valid_items:
            await update.message.reply_text(
                "❌ ለዚህ ሳምንት ተገቢ የምግብ ንጥሎች የሉም።",
                reply_markup=get_main_keyboard(update.effective_user.id)
            )
            return MAIN_MENU

        # Sort by day for consistent display
        valid_days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        day_order = {day: idx for idx, day in enumerate(valid_days)}
        valid_items.sort(key=lambda x: day_order.get(x['day'], len(valid_days)))

        menu_text = f"📋 የምግብ ዝርዝር ለሳምንቱ መጀመሪያ {week_start}:\n\n"
        menu_text += "የጾም ምግብ ዝርዝር\n"
        fasting_items = [item for item in valid_items if item['category'] == 'fasting']
        for idx, item in enumerate(fasting_items, 1):
            menu_text += f"{idx}. {item['name']} …….. {item['price']:.2f} ብር\n"
        
        menu_text += "\nየፍስክ ምግብ ዝርዝር\n"
        non_fasting_items = [item for item in valid_items if item['category'] == 'non_fasting']
        for idx, item in enumerate(non_fasting_items, 1):
            menu_text += f"{idx + len(fasting_items)}. {item['name']} …….. {item['price']:.2f} ብር\n"

        menu_text += "\nምግቦችዎን ለመምረጥ /select_meals ይጠቀሙ።"
        await update.message.reply_text(menu_text, reply_markup=get_main_keyboard(update.effective_user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error fetching menu for week starting {week_start}: {e}")
        await update.message.reply_text("❌ ምግብ ዝርዝር መጫን ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=get_main_keyboard(update.effective_user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Select meals
async def select_meals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Check for active or pending subscription
        cur.execute(
            "SELECT id, plan_type, meals_remaining, selected_dates FROM public.subscriptions WHERE user_id = %s AND status IN ('pending', 'active')",
            (user.id,)
        )
        subscription = cur.fetchone()
        if not subscription:
            await update.message.reply_text(
                "❌ ምግቦችን ለመምረጥ ምዝገባ ያስፈልጋል። /subscribe ይጠቀሙ።",
                reply_markup=get_main_keyboard(user.id)
            )
            return MAIN_MENU

        subscription_id, plan_type, meals_remaining, selected_dates_json = subscription
        selected_dates_en = json.loads(selected_dates_json) if isinstance(selected_dates_json, str) else selected_dates_json
        if meals_remaining <= 0 or not selected_dates_en:
            await update.message.reply_text(
                "❌ በምዝገባዎ ውስጥ ምንም ቀሪ ምግቦች ወይም የተመረጡ ቀናት የሉም። እባክዎ አዲስ እቅድ ይመዝገቡ።",
                reply_markup=get_main_keyboard(user.id)
            )
            return MAIN_MENU

        # Convert English days to Amharic for display
        valid_days_en = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        valid_days_am = ['ሰኞ', 'ማክሰኞ', 'እሮብ', 'ሐሙስ', 'አርብ', 'ቅዳሜ', 'እሑድ']
        selected_dates = [valid_days_am[valid_days_en.index(day)] for day in selected_dates_en]

        # Default menu items
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

        # Store data for meal selection
        context.user_data['subscription_id'] = subscription_id
        context.user_data['menu_items'] = default_menu
        context.user_data['meals_remaining'] = meals_remaining
        context.user_data['selected_dates'] = selected_dates
        context.user_data['selected_dates_en'] = selected_dates_en
        today = datetime.now(EAT).date()
        context.user_data['week_start'] = today - timedelta(days=today.weekday())
        context.user_data['selected_meals'] = {day: [] for day in selected_dates}  # Dict with day as key, list of items
        context.user_data['current_day_index'] = 0  # Track which day is being selected

        # Start with the first day
        first_day = selected_dates[0]
        menu_text = (
            f"📜 ለ{first_day} ምግብ ይምረጡ:\n\n"
            f"የተመረጡ ቀናት: {', '.join(selected_dates)}\n"
            f"ቀሪ ምግቦች: {meals_remaining}\n\n"
            "የጾም ምግብ ዝርዝር (160.00 ብር ለእያንዳንዱ):\n"
            "1. ምስር ወጥ\n"
            "2. ጎመን\n"
            "3. ሽሮ\n"
            "4. ፓስታ\n"
            "5. ፍርፍር\n"
            "6. የጾም በሼፍ ውሳኔ\n\n"
            "የፍስክ ምግብ ዝርዝር (260.00 ብር ለእያንዳንዱ):\n"
            "7. ምስር በስጋ\n"
            "8. ጎመን በስጋ\n"
            "9. ቦዘና ሽሮ\n"
            "10. ፓስታ በስጋ\n"
            "11. ጥብስ/ቋንጣ ፍርፍር\n"
            "12. የፍስክ በሼፍ ውሳኔ\n\n"
            "📝 ለ{first_day} የምግብ ቁጥር ያስገቡ (ለምሳሌ፣ '1' ወይም 'ሼፍ' ለሼፍ ውሳኔ)።\n"
            "ለመሰረዝ 'ሰርዝ' ይፃፉ።"
        )

        await update.message.reply_text(
            menu_text,
            reply_markup=ReplyKeyboardMarkup([['ሼፍ', 'ሰርዝ'], ['⬅️ ተመለስ']], resize_keyboard=True)
        )
        return MEAL_SELECTION
    except Exception as e:
        logger.error(f"Error starting meal selection for user {user.id}: {e}")
        await update.message.reply_text("❌ ምግቦችን መጫን ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

async def process_meal_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    menu_items = context.user_data.get('menu_items', [])
    selected_dates = context.user_data.get('selected_dates', [])
    selected_dates_en = context.user_data.get('selected_dates_en', [])
    week_start = context.user_data.get('week_start')
    current_day_index = context.user_data.get('current_day_index', 0)
    valid_days_en = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

    # Validate user data
    if not all([menu_items, selected_dates, selected_dates_en, week_start]):
        await update.message.reply_text(
            "❌ የክፍለ-ጊዜ ማብቂያ ወይም ምግብ ዝርዝር የለም። እባክዎ ከ /select_meals ጋር እንደገና ይጀምሩ።",
            reply_markup=get_main_keyboard(user.id)
        )
        context.user_data.clear()
        return MAIN_MENU

    # Handle cancellation
    if text == 'ሰርዝ':
        await update.message.reply_text(
            "❌ የምግብ ምርጫ ተሰርዟል።",
            reply_markup=get_main_keyboard(user.id)
        )
        context.user_data.clear()
        return MAIN_MENU

    # Handle back navigation
    if text == '⬅️ ተመለስ':
        return await cancel(update, context)

    # Handle next day
    if text == 'ቀጣይ ቀን':
        if not context.user_data['selected_meals'][selected_dates[current_day_index]]:
            await update.message.reply_text(
                "❌ ቢያንስ አንድ ምግብ ይምረጡ ለዚህ ቀን።",
                reply_markup=ReplyKeyboardMarkup([['ሼፍ', 'ቀጣይ ቀን', 'ጨርሻል', 'ሰርዝ'], ['⬅️ ተመለስ']], resize_keyboard=True)
            )
            return MEAL_SELECTION
        context.user_data['current_day_index'] = current_day_index + 1
        if current_day_index + 1 >= len(selected_dates):
            return await confirm_meal_selection(update, context)
        current_day = selected_dates[current_day_index + 1]
        await update.message.reply_text(
            f"📜 ለ{current_day} ምግብ ይምረጡ:\n\n"
            "የጾም ምግብ ዝርዝር (160.00 ብር ለእያንዳንዱ):\n"
            "1. ምስር ወጥ\n"
            "2. ጎመን\n"
            "3. ሽሮ\n"
            "4. ፓስታ\n"
            "5. ፍርፍር\n"
            "6. የጾም በሼፍ ውሳኔ\n\n"
            "የፍስክ ምግብ ዝርዝር (260.00 ብር ለእያንዳንዱ):\n"
            "7. ምስር በስጋ\n"
            "8. ጎመን በስጋ\n"
            "9. ቦዘና ሽሮ\n"
            "10. ፓስታ በስጋ\n"
            "11. ጥብስ/ቋንጣ ፍርፍር\n"
            "12. የፍስክ በሼፍ ውሳኔ\n\n"
            f"📝 ለ{current_day} የምግብ ቁጥር ያስገቡ (ለምሳሌ፣ '1' ወይም 'ሼፍ' ለሼፍ ውሳኔ)።\n"
            "ለመሰረዝ 'ሰርዝ' ይፃፉ።\n"
            "ተጨማሪ ምግብ ይጨምሩ ወይም 'ቀጣይ ቀን' ይጫኑ።",
            reply_markup=ReplyKeyboardMarkup([['ሼፍ', 'ቀጣይ ቀን', 'ጨርሻል', 'ሰርዝ'], ['⬅️ ተመለስ']], resize_keyboard=True)
        )
        return MEAL_SELECTION

    # Handle finish
    if text == 'ጨርሻል':
        return await confirm_meal_selection(update, context)

    # Validate current day
    try:
        current_day = selected_dates[current_day_index]
        current_day_en = selected_dates_en[current_day_index]
        if current_day_en not in valid_days_en:
            raise ValueError(f"Invalid day: {current_day_en}")
    except (IndexError, ValueError) as e:
        logger.error(f"Error accessing day data for user {user.id}: {e}")
        await update.message.reply_text(
            "❌ የተመረጡ ቀናት ስህተት። እባክዎ ከ /select_meals ጋር እንደገና ይጀምሩ።",
            reply_markup=get_main_keyboard(user.id)
        )
        context.user_data.clear()
        return MAIN_MENU

    selected_meals = context.user_data.get('selected_meals', {current_day: []})

    # Handle chef's choice
    if text.lower() == 'ሼፍ':
        category = 'fasting' if current_day_index % 2 == 0 else 'non_fasting'
        available_items = [item for item in menu_items if item.get('category') == category]
        if available_items:
            item = available_items[0]
            meal_date = week_start + timedelta(days=valid_days_en.index(current_day_en))
            selected_meals[current_day].append({
                'day': current_day,
                'day_en': current_day_en,
                'item': item,
                'meal_date': meal_date
            })
            context.user_data['selected_meals'] = selected_meals
            await update.message.reply_text(
                f"✅ ለ{current_day} በሼፍ ውሳኔ: {item['name']} ተመረጠ።"
            )
        else:
            await update.message.reply_text(
                f"❌ ለ{current_day} በ{category} ምድብ ምግብ የለም። እባክዎ በእጅ ይምረጡ።",
                reply_markup=ReplyKeyboardMarkup([['ሼፍ', 'ቀጣይ ቀን', 'ጨርሻል', 'ሰርዝ'], ['⬅️ ተመለስ']], resize_keyboard=True)
            )
            return MEAL_SELECTION
    else:
        try:
            item_idx = int(text) - 1
            if 0 <= item_idx < len(menu_items):
                item = menu_items[item_idx]
                meal_date = week_start + timedelta(days=valid_days_en.index(current_day_en))
                selected_meals[current_day].append({
                    'day': current_day,
                    'day_en': current_day_en,
                    'item': item,
                    'meal_date': meal_date
                })
                context.user_data['selected_meals'] = selected_meals
                await update.message.reply_text(
                    f"✅ ለ{current_day} {item['name']} ተመረጠ።"
                )
            else:
                await update.message.reply_text(
                    f"❌ የማይሰራ የምግብ ቁጥር {text}። 1 እስከ {len(menu_items)} መካከል ይምረጡ።",
                    reply_markup=ReplyKeyboardMarkup([['ሼፍ', 'ቀጣይ ቀን', 'ጨርሻል', 'ሰርዝ'], ['⬅️ ተመለስ']], resize_keyboard=True)
                )
                return MEAL_SELECTION
        except ValueError:
            await update.message.reply_text(
                f"❌ የማይሰራ ግብዓት '{text}'። ንጥል ያስገቡ (ለምሳሌ '1') ወይም 'ሼፍ'።",
                reply_markup=ReplyKeyboardMarkup([['ሼፍ', 'ቀጣይ ቀን', 'ጨርሻል', 'ሰርዝ'], ['⬅️ ተመለስ']], resize_keyboard=True)
            )
            return MEAL_SELECTION

    # Ask for more or next
    await update.message.reply_text(
        f"ለ{current_day} ተጨማሪ ምግብ ይጨምሩ? ወይም 'ቀጣይ ቀን' ወይም 'ጨርሻል' ይጫኑ።",
        reply_markup=ReplyKeyboardMarkup([['ሼፍ', 'ቀጣይ ቀን', 'ጨርሻል', 'ሰርዝ'], ['⬅️ ተመለስ']], resize_keyboard=True)
    )
    return MEAL_SELECTION

async def confirm_meal_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    selected_meals = context.user_data.get('selected_meals', {})
    total_price = 0
    order_text = "የመረጡት ቀን እና ምግብ ዝርዝር\n"
    valid_days_en = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    for day in selected_meals:
        for selection in selected_meals[day]:
            item = selection['item']
            meal_date = selection['meal_date'].strftime('%Y/%m/%d')
            order_text += f"- {day} ({meal_date}): {item['name']}\n"
            total_price += item['price']
    order_text += f"\nጠቅላላ ዋጋ: {total_price:.2f} ብር\n\n"
    order_text += "ምርጫውን ያረጋግጡ?"

    context.user_data['total_price'] = total_price

    await update.message.reply_text(
        order_text,
        reply_markup=ReplyKeyboardMarkup(
            [['✅ የምግብ ዝርዝሩ ትክክል ነው', '⛔ አስተካክል'], ['ሰርዝ', '⬅️ ተመለስ']],
            resize_keyboard=True
        )
    )
    return CONFIRM_MEAL

async def confirm_meal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_input = update.message.text
    conn = None
    cur = None

    if user_input == 'ሰርዝ' or user_input == '⬅️ ተመለስ':
        context.user_data.clear()
        await update.message.reply_text(
            "❌ የምግብ ምርጫ ተሰርዟል።",
            reply_markup=get_main_keyboard(user.id)
        )
        return MAIN_MENU

    if user_input == '⛔ አስተካክል':
        # Reset to meal selection
        context.user_data['current_day_index'] = 0
        context.user_data['selected_meals'] = {day: [] for day in context.user_data['selected_dates']}
        selected_dates = context.user_data.get('selected_dates', [])
        if not selected_dates:
            await update.message.reply_text(
                "❌ ምንም ቀናት አልተመረጡም። እባክዎ ከ /select_meals ጋር እንደገና ይጀምሩ።",
                reply_markup=get_main_keyboard(user.id)
            )
            context.user_data.clear()
            return MAIN_MENU
        await update.message.reply_text(
            f"📜 ለመረጡት ቀናት ምግቦች እንደገና ይምረጡ:\n\n"
            f"የተመረጡ ቀናት: {', '.join(selected_dates)}\n"
            "የጾም ምግብ ዝርዝር\n"
            "1. ምስር ወጥ …….. 160ብር\n"
            "2. ጎመን …….. 160ብር\n"
            "3. ሽሮ …….. 160ብር\n"
            "4. ፓስታ …….. 160ብር\n"
            "5. ፍርፍር …….. 160ብር\n"
            "6. የጾም በሼፍ ውሳኔ …….. 160ብር\n\n"
            "የፍስክ ምግብ ዝርዝር\n"
            "7. ምስር በስጋ …….. 260ብር\n"
            "8. ጎመን በስጋ …….. 260ብር\n"
            "9. ቦዘና ሽሮ …….. 260ብር\n"
            "10. ፓስታ በስጋ …….. 260ብር\n"
            "11. ጥብስ/ቋንጣ ፍርፍር …….. 260ብር\n"
            "12. የፍስክ በሼፍ ውሳኔ …….. 260ብር\n\n"
            f"ለ{selected_dates[0]} የምግብ ቁጥር ያስገቡ (ለምሳሌ '1') ወይም 'ሼፍ'።\n"
            "ለመሰረዝ 'ሰርዝ' ይፃፉ።",
            reply_markup=ReplyKeyboardMarkup([['ሼፍ', 'ቀጣይ ቀን', 'ጨርሻል', 'ሰርዝ'], ['⬅️ ተመለስ']], resize_keyboard=True)
        )
        return MEAL_SELECTION

    if user_input != '✅ የምግብ ዝርዝሩ ትክክል ነው':
        await update.message.reply_text(
            "❌ እባክዎ '✅ የምግብ ዝርዝሩ ትክክል ነው' ወይም '⛔ አስተካክል' ይምረጡ።",
            reply_markup=ReplyKeyboardMarkup(
                [['✅ የምግብ ዝርዝሩ ትክክል ነው', '⛔ አስተካክል'], ['ሰርዝ', '⬅️ ተመለስ']],
                resize_keyboard=True
            )
        )
        return CONFIRM_MEAL

    try:
        total_price = context.user_data.get('total_price', 0)
        if total_price <= 0:
            raise ValueError("Invalid total price")

        # Prepare payment prompt
        order_text = f"📝 ጠቅላላ ዋጋ: {total_price:.2f} ብር\n\n"
        order_text += "ክፍያ ማረጋገጫ ምስል ያስገቡ ለመቀጠል።"

        await update.message.reply_text(
            order_text,
            reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '⬅️ ተመለስ']], resize_keyboard=True)
        )
        return PAYMENT_UPLOAD
    except Exception as e:
        logger.error(f"Error proceeding to payment for user {user.id}: {e}")
        await update.message.reply_text(
            "❌ ወደ ክፍያ ማቋቋም ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።",
            reply_markup=get_main_keyboard(user.id)
        )
        context.user_data.clear()
        return MAIN_MENU

async def payment_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message.text and update.message.text.lower() in ['ሰርዝ', '⬅️ ተመለስ']:
        await update.message.reply_text(
            "❌ ምዝገባ ተሰርዟል።",
            reply_markup=get_main_keyboard(user.id)
        )
        context.user_data.clear()
        return MAIN_MENU

    if not update.message.photo:
        await update.message.reply_text(
            "❌ የክፍያ ማረጋገጫ ምስል ያስገቡ።",
            reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '⬅️ ተመለስ']], resize_keyboard=True)
        )
        return PAYMENT_UPLOAD

    photo = update.message.photo[-1]
    file = await photo.get_file()
    receipt_url = file.file_path

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
                "❌ ስህተት: ምዝገባ ወይም የክፍያ ውሂብ የለም። እባክዎ ከ /subscribe ጋር እንደገና ይጀምሩ።",
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

        # Save orders - group by meal_date
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

        # Notify admins about new payment
        for admin_id in ADMIN_IDS:
            try:
                if not validators.url(receipt_url):
                    logger.warning(f"Invalid receipt URL for payment {payment_id}: {receipt_url}")
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"🔔 ከተጠቃሚ {user.id} አዲስ ክፍያ {total_price:.2f} ብር። የማረጋገጫ URL የለም: {receipt_url}",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("አረጋግጥ", callback_data=f"approve_payment_{payment_id}"),
                             InlineKeyboardButton("ውድቅ", callback_data=f"reject_payment_{payment_id}")]
                        ])
                    )
                    continue

                try:
                    await context.bot.send_photo(
                        chat_id=admin_id,
                        photo=receipt_url,
                        caption=f"🔔 ከተጠቃሚ {user.id} አዲስ ክፍያ {total_price:.2f} ብር። እባክዎ ይፈትሹ።",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("አረጋግጥ", callback_data=f"approve_payment_{payment_id}"),
                             InlineKeyboardButton("ውድቅ", callback_data=f"reject_payment_{payment_id}")]
                        ])
                    )
                except Exception as e:
                    logger.error(f"Error sending photo to admin {admin_id} for payment {payment_id}: {e}")
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"🔔 ከተጠቃሚ {user.id} አዲስ ክፍያ {total_price:.2f} ብር። የማረጋገጫ ምስል መላክ አልተሳካም (ስህተት: {str(e)})። የማረጋገጫ URL: {receipt_url}",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("አረጋግጥ", callback_data=f"approve_payment_{payment_id}"),
                             InlineKeyboardButton("ውድቅ", callback_data=f"reject_payment_{payment_id}")]
                        ])
                    )
            except Exception as e:
                logger.error(f"Error notifying admin {admin_id} for payment {payment_id}: {e}")

        # Notify admins about new order
        order_text = f"🔔 ከተጠቃሚ {user.id} አዲስ ትዕዛዝ:\n"
        for day in selected_meals:
            for selection in selected_meals[day]:
                order_text += f"- {selection['meal_date'].strftime('%Y-%m-%d')}: {selection['item']['name']}\n"
        order_text += f"ጠቅላላ: {total_price:.2f} ብር"
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=order_text
                )
            except Exception as e:
                logger.error(f"Error notifying admin {admin_id} about new order: {e}")

        await update.message.reply_text(
            "📤 የክፍያ ማረጋገጫ ተልኳል። ለአስተዳዳሪ አረጋግጥ ይጠብቃል።",
            reply_markup=get_main_keyboard(user.id)
        )
        context.user_data.clear()
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error processing payment for user {user.id}: {e}")
        await update.message.reply_text(
            "❌ ማረጋገጫ በማስገባት ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።",
            reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '⬅️ ተመለስ']], resize_keyboard=True)
        )
        return PAYMENT_UPLOAD
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

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
                            caption=f"ክፍያ #{payment_id}\nተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\nመጠን: {amount:.2f} ብር",
                            reply_markup=reply_markup
                        )
                    except Exception as e:
                        logger.error(f"Error sending photo for payment {payment_id} to admin {user.id}: {e}")
                        await context.bot.send_message(
                            chat_id=user.id,
                            text=f"ክፍያ #{payment_id}\nተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\nመጠን: {amount:.2f} ብር\nየስምልጣ URL: {receipt_url}\n(ማሳወቂያ: ስቶ ማሳየት ስህተት ተከሰተ: {str(e)})",
                            reply_markup=reply_markup
                        )
                else:
                    await context.bot.send_message(
                        chat_id=user.id,
                        text=f"ክፍያ #{payment_id}\nተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\nመጠን: {amount:.2f} ብር\nየስምልጣ URL: {receipt_url or 'የለም'} (የማይሰራ ወይም የለም URL)",
                        reply_markup=reply_markup
                    )
            except Exception as e:
                logger.error(f"Error processing payment {payment_id} for admin {user.id}: {e}")
                await context.bot.send_message(
                    chat_id=user.id,
                    text=f"ክፍያ #{payment_id}\nተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\nመጠን: {amount:.2f} ብር\nስህተት: የክፍያ ዝርዝር ማስተካከል አልተሳካም",
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
            await context.bot.send_message(
                chat_id=user_id,
                text="✅ የእርስዎ ክፍያ ተቀበለ! ምግቦችዎ ተደረጉ።",
                reply_markup=get_main_keyboard(user_id)
            )
        elif action == 'reject':
            cur.execute(
                "UPDATE public.payments SET status = 'rejected' WHERE id = %s",
                (payment_id,)
            )
            # Delete associated orders and subscription
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

# My Subscription
async def my_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
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
                "❌ ንቁ ወይም ተጠባቂ ምዝገባዎች የሉም። /subscribe ይጠቀሙ አንድ ያጀምሩ።",
                reply_markup=get_main_keyboard(user.id)
            )
            return MAIN_MENU

        subscription_id, plan_type, meals_remaining, selected_dates_json, expiry_date, status = subscription
        selected_dates_en = json.loads(selected_dates_json) if isinstance(selected_dates_json, str) else selected_dates_json
        valid_days_en = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        valid_days_am = ['ሰኞ', 'ማክሰኞ', 'እሮብ', 'ሐሙስ', 'አርብ', 'ቅዳሜ', 'እሑድ']
        selected_dates = [valid_days_am[valid_days_en.index(day)] for day in selected_dates_en]
        text = (
            f"📋 የእርስዎ ምዝገባ:\n"
            f"እቅድ: {plan_type.capitalize()}\n"
            f"ቀሪ ምግቦች: {meals_remaining}\n"
            f"የተመረጡ ቀናት: {', '.join(selected_dates)}\n"
            f"የጊዜ ጫና: {expiry_date.strftime('%Y-%m-%d')}\n"
            f"ሁኔታ: {status.capitalize()}\n\n"
            "ምግቦችዎን ለመምረጥ /select_meals ይጠቀሙ።"
        )
        await update.message.reply_text(text, reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error fetching subscription for user {user.id}: {e}")
        await update.message.reply_text("❌ የምዝገባ ዝርዝር መጫን ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# My Meals
async def my_meals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT meal_date, items FROM public.orders WHERE user_id = %s AND status = 'confirmed' "
            "ORDER BY meal_date",
            (user.id,)
        )
        orders = cur.fetchall()
        if not orders:
            await update.message.reply_text(
                "❌ ተደረጉ ምግቦች የሉም። ምግቦች ለመምረጥ /select_meals ይጠቀሙ።",
                reply_markup=get_main_keyboard(user.id)
            )
            return MAIN_MENU

        text = "📅 የተደረጉ ምግቦችዎ:\n\n"
        for meal_date, items_json in orders:
            items = json.loads(items_json) if isinstance(items_json, str) else items_json
            text += f"ቀን: {meal_date}\n"
            for item in items:
                text += f"- {item['name']} ({item['category']})\n"
            text += "\n"
        await update.message.reply_text(text, reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error fetching meals for user {user.id}: {e}")
        await update.message.reply_text("❌ የምግብ ዝርዝር መጫን ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# Contact Update
async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "እባክዎ ስልክ ቁጥርዎን ያካፍሉ።",
        reply_markup=ReplyKeyboardMarkup(
            [[{"text": "📱 እውቂያ አጋራ", "request_contact": True}, "ሰርዝ", '⬅️ ተመለስ']],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    )
    return REGISTER_PHONE

# Refer a Friend
async def refer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        referral_code = f"REF{user.id}_{int(datetime.now(EAT).timestamp())}"
        cur.execute(
            "INSERT INTO public.referrals (referrer_id, referral_code) VALUES (%s, %s)",
            (user.id, referral_code)
        )
        conn.commit()
        await update.message.reply_text(
            f"🔗 የእርስዎ የግብዣ ኮድ: {referral_code}\n"
            "ይህን ኮድ ከጓደኞችዎ ጋር ይጋብዙ ኦዝ ኪችን እንዲገቡ!",
            reply_markup=get_main_keyboard(user.id)
        )
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error generating referral for user {user.id}: {e}")
        await update.message.reply_text("❌ የግብዣ ኮድ በመፍጠር ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=get_main_keyboard(user.id))
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
        await update.message.reply_text("❌ አብራሪ የለዎትም።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU

    await update.message.reply_text(
        "📋 አዲሱን ምግብ ዝርዝር በJSON ቅርጽ ያስገቡ (ለምሳሌ፣ [{'id': 1, 'name': 'Dish', 'price': 100, 'day': 'Monday', 'category': 'fasting'}])።",
        reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '⬅️ ተመለስ']], resize_keyboard=True)
    )
    return ADMIN_UPDATE_MENU

async def process_admin_update_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አብራሪ የለዎትም።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU

    if update.message.text.lower() in ['ሰርዝ', '⬅️ ተመለስ']:
        await update.message.reply_text("❌ የምግብ ዝርዝር ማዘመን ተሰርዟል።", reply_markup=get_main_keyboard(user.id))
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
        await update.message.reply_text("✅ ምግብ ዝርዝር በተሳካ ሁኔታ ተዘመነ።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error updating menu: {e}")
        await update.message.reply_text("❌ የማይሰራ JSON ወይም ምግብ ዝርዝር ማዘመን ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '⬅️ ተመለስ']], resize_keyboard=True))
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
            reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '⬅️ ተመለስ']], resize_keyboard=True)
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

    if update.message.text.lower() in ['ሰርዝ', '⬅️ ተመለስ']:
        await update.message.reply_text("❌ የምግብ ዝርዝር ማስወገድ ተሰርዟል።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU

    try:
        item_idx = int(update.message.text) - 1
        menu_items = context.user_data.get('menu_items', [])
        week_start = context.user_data.get('week_start')
        if not (0 <= item_idx < len(menu_items)):
            await update.message.reply_text(
                f"❌ የማይሰራ የንጥል ቁጥር። 1 እስከ {len(menu_items)} መካከል ይምረጡ።",
                reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '⬅️ ተመለስ']], resize_keyboard=True)
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
        await update.message.reply_text("❌ የምግብ ዝርዝር ንጥል በማስወገድ ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '⬅️ ተመለስ']], resize_keyboard=True))
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

        text = "📋 ንቁ/ተጠባቂ ተመዝጋቢዎች:\n\n"
        for full_name, username, plan_type, meals_remaining, expiry_date in subscribers:
            text += (
                f"ስም: {full_name or 'የለም'} (@{username or 'የለም'})\n"
                f"እቅድ: {plan_type.capitalize()}\n"
                f"ቀሪ ምግቦች: {meals_remaining}\n"
                f"ጫና: {expiry_date.strftime('%Y-%m-%d')}\n\n"
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

        text = "💸 የክፍያ ታሪክ:\n\n"
        for payment_id, full_name, username, amount, status, created_at in payments:
            text += (
                f"ክፍያ #{payment_id}\n"
                f"ተጠቃሚ: {full_name or 'የለም'} (@{username or 'የለም'})\n"
                f"መጠን: {amount:.2f} ብር\n"
                f"ሁኔታ: {status.capitalize()}\n"
                f"ቀን: {created_at.strftime('%Y-%m-%d %H:%M')}\n\n"
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
            # If no orders for today, show for the current week
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
            text = f"📅 ለ{week_start} - {week_end} ሳምንት ትዕዛዞች (ዛሬ የለም):\n\n"
        else:
            text = f"📅 ለ{today} ትዕዛዞች:\n\n"

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
        reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '⬅️ ተመለስ']], resize_keyboard=True)
    )
    return ADMIN_ANNOUNCE

async def process_admin_announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ አብራሪ የለዎትም።", reply_markup=get_main_keyboard(user.id))
        return MAIN_MENU

    if update.message.text.lower() in ['ሰርዝ', '⬅️ ተመለስ']:
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
        await update.message.reply_text("❌ ማስታወቂያ በማላክ ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=ReplyKeyboardMarkup([['ሰርዝ', '⬅️ ተመለስ']], resize_keyboard=True))
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
            [[{"text": "📍 ቦታ አጋራ", "request_location": True}, "ዝለል", '⬅️ ተመለስ']],
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

    if update.message.text in ['⬅️ ተመለስ', 'ዝለል']:
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
            await update.message.reply_text("❌ የማይሰራ ቦታ። እባክዎ እንደገና ይሞክሩ ወይም 'ዝለል' ይፃፉ።", reply_markup=ReplyKeyboardMarkup([["ዝለል", '⬅️ ተመለስ']], resize_keyboard=True))
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
        await update.message.reply_text("❌ ቦታ በማዘጋጀት ላይ ስህተት። እባክዎ እንደገና ይሞክሩ።", reply_markup=ReplyKeyboardMarkup([["ዝለል", '⬅️ ተመለስ']], resize_keyboard=True))
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

        text = "📍 የአስተዳዳሪ ቦታዎች:\n\n"
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

        # Conversation handler
        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler('start', start),
                CommandHandler('help', help_command),
                CommandHandler('menu', show_menu),
                CommandHandler('subscribe', choose_plan),
                CommandHandler('my_subscription', my_subscription),
                CommandHandler('my_meals', my_meals),
                CommandHandler('contact', contact),
                CommandHandler('refer', refer),
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
                CommandHandler('cancel', cancel)
            ],
            states={
                MAIN_MENU: [
                    MessageHandler(filters.Regex('^🍽 ምግብ ዝርዝር$'), show_menu),
                    MessageHandler(filters.Regex('^🛒 ምዝገባ$'), choose_plan),
                    MessageHandler(filters.Regex('^📋 የእኔ ምዝገባ$'), my_subscription),
                    MessageHandler(filters.Regex('^📅 የእኔ ምግቦች$'), my_meals),
                    MessageHandler(filters.Regex('^📞 እውቂያ$'), contact),
                    MessageHandler(filters.Regex('^🔗 ግብዣ$'), refer),
                    MessageHandler(filters.Regex('^🍴 ምግብ ምረጥ$'), select_meals),
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
                REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
                REGISTER_PHONE: [
                    MessageHandler(filters.CONTACT | (filters.TEXT & ~filters.COMMAND), register_phone)
                ],
                REGISTER_LOCATION: [
                    MessageHandler(filters.LOCATION | (filters.TEXT & ~filters.COMMAND), register_location)
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