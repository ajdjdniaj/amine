# bot.py (النسخة الاحترافية الكاملة - تنظيف تلقائي + واي فاي محسن)
import os
import time
import tempfile
import io
import re
import csv
import logging
from datetime import datetime, timedelta

from flask import Flask, request
import telebot
from telebot import types
import telebot.apihelper

import yt_dlp
from PIL import Image
import pytesseract

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool

# ===== Logging =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ===== إعداد البيئة =====
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "aie_tool_channel")  # بدون @
PORT = int(os.environ.get("PORT", 10000))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN غير معرف")
if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL غير معرف")

OWNER_ID = int(os.environ.get("OWNER_ID", "5883400070"))
BAN_DURATION = 5 * 60  # 5 دقائق

# ===== إعداد قاعدة البيانات =====
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL غير معرف")

# أنشئ pool عند بدء التطبيق
DB_MIN_CONN = 1
DB_MAX_CONN = 6 
try:
    pool = SimpleConnectionPool(DB_MIN_CONN, DB_MAX_CONN, DATABASE_URL, cursor_factory=RealDictCursor, sslmode='require')
    logging.info("Connection pool created.")
except Exception as e:
    logging.exception("Failed to create connection pool: %s", e)
    raise

def get_db_conn():
    try:
        conn = pool.getconn()
        return conn
    except Exception as e:
        logging.exception("get_db_conn error: %s", e)
        raise

def put_db_conn(conn):
    try:
        pool.putconn(conn)
    except Exception:
        try:
            conn.close()
        except:
            pass

def init_db():
    sql = """
    CREATE TABLE IF NOT EXISTS users (
      user_id BIGINT PRIMARY KEY,
      first_seen TIMESTAMP DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS joined_users (
      user_id BIGINT PRIMARY KEY,
      joined_at TIMESTAMP DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS bans (
      user_id BIGINT PRIMARY KEY,
      ban_until BIGINT
    );
    """
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        logging.info("DB initialized.")
    finally:
        put_db_conn(conn)

init_db()

# ===== إعداد البوت =====
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ===== الذاكرة المؤقتة =====
user_links = {}
user_platform = {}
user_video_info = {}
user_state = {}

# [نظام التتبع الجديد للحذف]
# لتخزين معرف آخر رسالة بوت (القائمة الكبيرة) لكل مستخدم
user_last_bot_message = {} 

PLATFORMS = ["يوتيوب", "انستغرام", "تيك توك"]

# ===== دوال مساعدة للتنظيف (Cleanup) =====
def delete_last_bot_msg(chat_id):
    """حذف آخر رسالة محفوظة للبوت (القوائم الكبيرة)"""
    msg_id = user_last_bot_message.get(chat_id)
    if msg_id:
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass # قد تكون محذوفة بالفعل
        user_last_bot_message.pop(chat_id, None)

def send_and_track(chat_id, text, reply_markup=None, parse_mode=None):
    """إرسال رسالة وحفظ معرفها ليتم حذفها لاحقاً"""
    sent = bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    user_last_bot_message[chat_id] = sent.message_id
    return sent

# ===== دوال قاعدة البيانات (الحظر والتحقق) =====
def is_banned(user_id):
    if int(user_id) == OWNER_ID:
        return 0
    now_ts = datetime.utcnow()
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT ban_until FROM bans WHERE user_id = %s", (int(user_id),))
                row = cur.fetchone()
                if not row:
                    return 0
                ban_until = row['ban_until']
                if ban_until and now_ts < ban_until:
                    return int((ban_until - now_ts).total_seconds())
                else:
                    cur.execute("DELETE FROM bans WHERE user_id = %s", (int(user_id),))
                    return 0
    finally:
        put_db_conn(conn)

def ban_user(user_id, duration=BAN_DURATION):
    if int(user_id) == OWNER_ID:
        return
    ban_until_dt = datetime.utcnow() + timedelta(seconds=duration)
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bans (user_id, ban_until) VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET ban_until = EXCLUDED.ban_until
                """, (int(user_id), ban_until_dt))
    finally:
        put_db_conn(conn)

def save_user(user_id):
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (int(user_id),))
    finally:
        put_db_conn(conn)

def save_joined_user(user_id):
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO joined_users (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (int(user_id),))
    finally:
        put_db_conn(conn)

def has_joined_before(user_id):
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM joined_users WHERE user_id = %s", (int(user_id),))
                return cur.fetchone() is not None
    finally:
        put_db_conn(conn)

def is_user_joined(user_id):
    try:
        if int(user_id) == OWNER_ID:
            return True
        member = bot.get_chat_member(f"@{CHANNEL_USERNAME}", int(user_id))
        status = getattr(member, "status", None)
        return status in ('member', 'creator', 'administrator')
    except telebot.apihelper.ApiException as e:
        logging.exception("ApiException in is_user_joined: %s", e)
        return False
    except Exception as e:
        logging.exception("Unexpected error in is_user_joined: %s", e)
        return False

# ===== رسائل التحقق =====
def send_welcome_with_channel(chat_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("📢 انضم للقناة", url=f"https://t.me/{CHANNEL_USERNAME}"),
        types.InlineKeyboardButton("✅ تحقق", callback_data="check_join")
    )
    bot.send_message(
        chat_id,
        f""" welcome

🔒 لاستخدام البوت يجب عليك أولاً الانضمام إلى القناة الرسمية:
⚠️ *تنبيه مهم*:  لن تستطيع استخدام البوت حتى تنضم للقناة.

بعد الانضمام للقناة اضغط على زر ✅ تحقق بالأسفل للمتابعة.""",
        reply_markup=markup,
        parse_mode="Markdown"
    )

def send_ban_with_check(chat_id, ban_left):
    minutes = (ban_left % 3600) // 60
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("📢 انضم للقناة", url=f"https://t.me/{CHANNEL_USERNAME}"),
        types.InlineKeyboardButton("✅ تحقق من جديد", callback_data="recheck")
    )
    bot.send_message(
        chat_id,
        f"❌ تم حظرك لمدة 5 دقائق.\nالوقت المتبقي: {minutes} دقيقة.\nانضم ثم تحقق.",
        reply_markup=markup
    )

def send_warning_join(chat_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("📢 انضم للقناة", url=f"https://t.me/{CHANNEL_USERNAME}"),
        types.InlineKeyboardButton("✅ تحقق", callback_data="check_join")
    )
    bot.send_message(chat_id, "⚠️ يجب الانضمام للقناة أولاً.", reply_markup=markup)

def check_access(message_or_call):
    if isinstance(message_or_call, telebot.types.CallbackQuery):
        user_id = message_or_call.from_user.id
        chat_id = message_or_call.message.chat.id
    else:
        user_id = message_or_call.from_user.id
        chat_id = message_or_call.chat.id

    ban_left = is_banned(user_id)
    if ban_left > 0:
        send_ban_with_check(chat_id, ban_left)
        return False
    if not is_user_joined(user_id):
        if has_joined_before(user_id):
            ban_user(user_id)
            send_ban_with_check(chat_id, BAN_DURATION)
        else:
            send_warning_join(chat_id)
        return False
    return True

# ===== أوامر المالك (Admin) =====
@bot.message_handler(commands=['get_users'])
def get_users_handler(message):
    if int(message.from_user.id) != OWNER_ID: return
    try:
        conn = get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id, first_seen FROM users ORDER BY first_seen DESC")
                rows = cur.fetchall()
        put_db_conn(conn)
        if not rows:
            bot.send_message(message.chat.id, "لا يوجد مستخدمين.")
            return
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w", newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["user_id", "first_seen"])
            for r in rows: writer.writerow([r['user_id'], r['first_seen']])
        with open(path, "rb") as f:
            bot.send_document(message.chat.id, f, caption="Users CSV")
        os.remove(path)
    except Exception as e:
        bot.send_message(message.chat.id, "Error fetching users.")

@bot.message_handler(commands=['get_banned'])
def get_banned_handler(message):
    if int(message.from_user.id) != OWNER_ID: return
    try:
        conn = get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id, ban_until FROM bans ORDER BY ban_until DESC")
                rows = cur.fetchall()
        put_db_conn(conn)
        if not rows:
            bot.send_message(message.chat.id, "لا يوجد محظورين.")
            return
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w", newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["user_id", "ban_until"])
            for r in rows: writer.writerow([r['user_id'], r['ban_until']])
        with open(path, "rb") as f:
            bot.send_document(message.chat.id, f, caption="Banned CSV")
        os.remove(path)
    except:
        pass

@bot.message_handler(commands=['stats'])
def stats_handler(message):
    if int(message.from_user.id) != OWNER_ID: return
    try:
        conn = get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM users")
                u = cur.fetchone()['c']
                cur.execute("SELECT COUNT(*) AS c FROM joined_users")
                j = cur.fetchone()['c']
                cur.execute("SELECT COUNT(*) AS c FROM bans WHERE ban_until > now()")
                b = cur.fetchone()['c']
        put_db_conn(conn)
        bot.send_message(message.chat.id, f"📊 Stats:\nUsers: {u}\nJoined: {j}\nBanned: {b}")
    except: pass

@bot.message_handler(commands=['ban_user'])
def ban_user_command(message):
    if int(message.from_user.id) != OWNER_ID: return
    try:
        parts = message.text.split()
        if len(parts) == 2:
            ban_user(parts[1], 3153600000)
            bot.reply_to(message, "تم الحظر.")
    except: pass

@bot.message_handler(commands=['unban_user'])
def unban_user_command(message):
    if int(message.from_user.id) != OWNER_ID: return
    try:
        parts = message.text.split()
        if len(parts) == 2:
            conn = get_db_conn()
            with conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM bans WHERE user_id=%s", (int(parts[1]),))
            put_db_conn(conn)
            bot.reply_to(message, "تم إلغاء الحظر.")
    except: pass

# ===== منطق واجهة البوت والتحكم في الرسائل (Message Flow) =====

def show_main_menu(chat_id, msg_only=False):
    # [تنظيف] حذف القوائم القديمة
    delete_last_bot_msg(chat_id)

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🎬 أداة تحميل mp3/mp4", "📡 أداة اختراق WiFi fh")
    
    text = "👋 أهلاً بك في البوت الشامل!\n✨ اختر الخدمة:"
    if msg_only: text = "👇 اختر الأداة:"
    
    # [تتبع] هذه ليست قائمة كبيرة جداً لكن يمكن تتبعها لحذفها عند الدخول لخدمة
    send_and_track(chat_id, text, reply_markup=markup)
    user_state[chat_id] = "main_menu"

def send_platforms(chat_id):
    # [تنظيف]
    delete_last_bot_msg(chat_id)

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for p in PLATFORMS:
        markup.add(p)
    markup.add("🔙 رجوع")
    
    # [تتبع]
    send_and_track(chat_id, "يرجى اختيار منصة:", reply_markup=markup)
    user_state[chat_id] = "platforms"

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id
    save_user(user_id)
    if check_access(message):
        send_welcome_with_channel(message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "check_join")
def check_join_callback(call):
    chat_id = call.message.chat.id
    if is_user_joined(call.from_user.id):
        save_joined_user(call.from_user.id)
        # حذف رسالة التحقق
        try: bot.delete_message(chat_id, call.message.message_id)
        except: pass
        
        show_main_menu(chat_id)
    else:
        try: bot.answer_callback_query(call.id, "⚠️ لم تنضم بعد!")
        except: pass

@bot.callback_query_handler(func=lambda call: call.data == "recheck")
def recheck_callback(call):
    chat_id = call.message.chat.id
    if is_user_joined(call.from_user.id):
        save_joined_user(call.from_user.id)
        try: bot.delete_message(chat_id, call.message.message_id)
        except: pass
        show_main_menu(chat_id)
    else:
        ban_user(call.from_user.id)
        send_ban_with_check(chat_id, BAN_DURATION)

# --- قسم التحميل ---
@bot.message_handler(func=lambda m: m.text == "🎬 أداة تحميل mp3/mp4")
def choose_downloader(message):
    if not check_access(message): return
    
    # [تنظيف] حذف القائمة الرئيسية القديمة
    delete_last_bot_msg(message.chat.id)

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for p in PLATFORMS:
        markup.add(p)
    markup.add("🔙 رجوع")
    
    # رسالة القائمة الكبيرة
    msg = (
        "✨ اختر المنصة التي تريد التحميل منها:\n"
        "0️⃣ يوتيوب: تحميل فيديوهات يوتيوب (mp4 أو mp3).\n"
        "1️⃣ انستغرام: تحميل فيديوهات أو ريلز انستغرام (mp4 أو mp3).\n"
        "2️⃣ تيك توك: تحميل فيديوهات تيك توك بدون علامة مائية (mp4 أو mp3)."
    )
    # [تتبع] نحفظ هذه الرسالة لحذفها بمجرد أن يختار المستخدم منصة
    send_and_track(message.chat.id, msg, reply_markup=markup)
    user_state[message.chat.id] = "platforms"

@bot.message_handler(func=lambda m: m.text in PLATFORMS)
def ask_for_link(message):
    if not check_access(message): return
    
    # [تنظيف هام] هنا يتم حذف القائمة الكبيرة (يوتيوب، تيك توك...)
    delete_last_bot_msg(message.chat.id)

    if message.text in ["يوتيوب", "انستغرام"]:
        # رسالة صيانة نظيفة
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True).add("🔙 رجوع")
        send_and_track(message.chat.id, "⚠️ هذه الخدمة في صيانة حاليًا. اختر منصة أخرى.", reply_markup=markup)
        user_state[message.chat.id] = "platforms" # نبقيه في نفس الحالة ليعود
        return

    user_platform[message.from_user.id] = message.text
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add("🔙 رجوع")
    
    send_and_track(message.chat.id, f"📥 أرسل رابط الفيديو من {message.text}:", reply_markup=markup)
    user_state[message.chat.id] = "waiting_link"

@bot.message_handler(func=lambda m: m.text == "🔙 رجوع")
def back_handler(message):
    if not check_access(message): return
    
    # [تنظيف] حذف الرسالة الحالية
    delete_last_bot_msg(message.chat.id)

    state = user_state.get(message.chat.id, "main_menu")
    if state == "waiting_link":
        # العودة لاختيار المنصة (عرض القائمة الكبيرة مجدداً)
        choose_downloader(message)
    elif state == "platforms":
        show_main_menu(message.chat.id)
    elif state in ["wifi_methods", "wifi_name_or_image"]:
        show_main_menu(message.chat.id)
    else:
        show_main_menu(message.chat.id)

@bot.message_handler(func=lambda m: m.text and m.text.startswith("http"))
def handle_link(message):
    if not check_access(message): return
    
    # التحقق من أن المستخدم في وضع انتظار الرابط
    if user_state.get(message.chat.id) != "waiting_link":
        delete_last_bot_msg(message.chat.id)
        send_platforms(message.chat.id)
        return

    # [تنظيف] حذف رسالة "أرسل الرابط"
    delete_last_bot_msg(message.chat.id)

    platform = user_platform.get(message.from_user.id)
    url = message.text.strip()
    
    # تحقق بسيط من الرابط
    valid = False
    if platform == "تيك توك" and ("tiktok" in url or "تيك توك" in url): valid = True
    # (يمكن إضافة باقي المنصات)

    if platform == "تيك توك" and not valid:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True).add("🔙 رجوع")
        send_and_track(message.chat.id, "❌ الرابط لا يبدو صحيحاً لتيك توك.", reply_markup=markup)
        return

    user_links[message.from_user.id] = url
    
    wait_msg = bot.send_message(message.chat.id, "🎬 جاري جلب المعلومات...")
    
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("🎬 فيديو (mp4)", callback_data="video"),
        types.InlineKeyboardButton("🎵 صوت (mp3)", callback_data="audio")
    )
    
    try:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'Video')
            duration = info.get('duration', 0) or 0
            mins, secs = divmod(duration, 60)
            
            caption = f"🎬 <b>{title}</b>\n⏱️ {mins}:{secs:02d}\n\nاختر الصيغة:"
            bot.edit_message_text(caption, message.chat.id, wait_msg.message_id, parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        logging.error("YTDL Error: %s", e)
        bot.edit_message_text("❌ لم يتم العثور على الفيديو.", message.chat.id, wait_msg.message_id)

@bot.callback_query_handler(func=lambda call: call.data in ("video", "audio"))
def process_download(call):
    if not check_access(call): return
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    
    url = user_links.get(call.from_user.id)
    if not url:
        bot.answer_callback_query(call.id, "❌ انتهت الجلسة.")
        return

    bot.edit_message_text("⏳ **جاري التحميل...**", chat_id, msg_id, parse_mode="Markdown")
    
    tmpdir = tempfile.mkdtemp()
    action = call.data
    
    try:
        ydl_opts = {
            'outtmpl': os.path.join(tmpdir, '%(title)s.%(ext)s'),
            'format': 'best',
            'noplaylist': True,
            'quiet': True,
        }
        if action == "audio":
            ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '192'}]
            
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if action == "video": filename = ydl.prepare_filename(info)
            else: filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + ".mp3"

        if os.path.exists(filename) and os.path.getsize(filename) < 50*1024*1024:
            with open(filename, "rb") as f:
                if action == "video": bot.send_video(chat_id, f, caption="✅ تم!")
                else: bot.send_audio(chat_id, f, caption="✅ تم!")
            # [تنظيف] حذف رسالة "جاري التحميل" بعد الإرسال
            try: bot.delete_message(chat_id, msg_id)
            except: pass
        else:
            bot.edit_message_text("❌ الملف كبير جداً.", chat_id, msg_id)

    except Exception as e:
        logging.error("DL error: %s", e)
        bot.edit_message_text("❌ فشل التحميل.", chat_id, msg_id)
    finally:
        try:
            for f in os.listdir(tmpdir): os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)
        except: pass

    # خيارات ما بعد التحميل
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add("منصة أخرى", "🔙 رجوع")
    send_and_track(chat_id, "💡 ماذا تريد أن تفعل الآن؟", reply_markup=markup)
    user_state[chat_id] = "waiting_link"

# --- قسم WiFi ---
def show_wifi_methods(chat_id):
    delete_last_bot_msg(chat_id)
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("✍️ كتابة اسم الراوتر", "🖼️ صورة لجميع الراوترات", "🔙 رجوع")
    
    send_and_track(chat_id, "📡 اختر طريقة إدخال اسم الراوتر:", reply_markup=markup)
    user_state[chat_id] = "wifi_methods"

@bot.message_handler(func=lambda m: m.text == "📡 أداة اختراق WiFi fh")
def wifi_request(message):
    if not check_access(message): return
    show_wifi_methods(message.chat.id)

@bot.message_handler(func=lambda m: m.text == "✍️ كتابة اسم الراوتر")
def manual_ssid(message):
    if not check_access(message): return
    delete_last_bot_msg(message.chat.id)
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔙 رجوع")
    sent = bot.send_message(message.chat.id, "🔍 أرسل اسم شبكة WiFi (يجب أن تبدأ بـ fh_):", reply_markup=markup)
    # تتبع يدوي للرسالة
    user_last_bot_message[message.chat.id] = sent.message_id
    
    bot.register_next_step_handler(sent, generate_password_with_back)
    user_state[message.chat.id] = "wifi_name_or_image"

def generate_password_with_back(message):
    if message.text == "🔙 رجوع":
        show_wifi_methods(message.chat.id)
        return
    generate_password(message)

@bot.message_handler(func=lambda m: m.text == "🖼️ صورة لجميع الراوترات")
def ask_for_wifi_image(message):
    if not check_access(message): return
    delete_last_bot_msg(message.chat.id)
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔙 رجوع")
    sent = bot.send_message(message.chat.id, "📸 أرسل صورة للقائمة:", reply_markup=markup)
    user_last_bot_message[message.chat.id] = sent.message_id
    
    bot.register_next_step_handler(sent, process_wifi_image_with_back)
    user_state[message.chat.id] = "wifi_name_or_image"

def process_wifi_image_with_back(message):
    if message.text == "🔙 رجوع":
        show_wifi_methods(message.chat.id)
        return
    process_wifi_image(message)

# دوال المعالجة المنطقية
def smart_correct_ssid(ssid):
    # دعم اللواحق: fh_xxxx_5g -> fh_xxxx
    parts = ssid.split('_')
    if len(parts) >= 2: ssid = f"{parts[0]}_{parts[1]}"
    
    if ssid.startswith("fh_"):
        prefix, rest = "fh_", ssid[3:]
        rest = rest.replace('l', '1').replace('I', '1').replace('O', '0').replace('o', '0')
        if len(rest) == 6 and rest[3] == '0': rest = rest[:3] + 'a' + rest[4:]
        return prefix + rest
    return ssid

def generate_wifi_password(ssid):
    ssid = ssid.strip().lower()
    parts = ssid.split('_')
    if len(parts) < 2 or parts[0] != "fh": return None
    
    hex_part = parts[1]
    if not all(c in '0123456789abcdef' for c in hex_part): return None
    
    table = {'0':'f','1':'e','2':'d','3':'c','4':'b','5':'a','6':'9','7':'8','8':'7','9':'6','a':'5','b':'4','c':'3','d':'2','e':'1','f':'0'}
    encoded = ''.join(table.get(c, c) for c in hex_part)
    return f"wlan{encoded}"

def process_wifi_image(message):
    if not check_access(message): return
    
    wait_msg = bot.send_message(message.chat.id, "⏳ جاري المعالجة...")
    
    try:
        if not message.photo:
            bot.delete_message(message.chat.id, wait_msg.message_id)
            bot.send_message(message.chat.id, "❌ ليست صورة.")
            return
        
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        img = Image.open(io.BytesIO(downloaded))
        
        # تحسين الحجم
        if img.width > 800: img = img.resize((800, int(800*img.height/img.width)))
        
        # OCR
        texts = [pytesseract.image_to_string(img)]
        texts.append(pytesseract.image_to_string(img.convert('L').point(lambda x: 0 if x<140 else 255, '1')))
        
        all_ssids = set()
        for t in texts:
            # Regex يلتقط fh_xxxx أو fh_xxxx_yyy
            found = re.findall(r'(fh_[a-fA-F0-9]+(?:_[a-zA-Z0-9]+)?)', t, re.IGNORECASE)
            for s in found:
                corrected = smart_correct_ssid(s.lower())
                parts = corrected.split('_')
                if len(parts) >= 2 and all(c in '0123456789abcdef' for c in parts[1]):
                    all_ssids.add(corrected)
        
        bot.delete_message(message.chat.id, wait_msg.message_id)
        
        if not all_ssids:
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True).add("🔙 رجوع")
            bot.send_message(message.chat.id, "❌ لم يتم العثور على شبكات fh.", reply_markup=markup)
            bot.register_next_step_handler(message, process_wifi_image_with_back)
            return

        reply = ""
        for ssid in all_ssids:
            pw = generate_wifi_password(ssid)
            if pw: reply += f"📶 <b>{ssid}</b>\n🔑 <code>{pw}</code>\n\n"
            
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True).add("🔁 مرة أخرى", "🔙 رجوع")
        send_and_track(message.chat.id, reply or "❌ شبكات غير مدعومة", reply_markup=markup, parse_mode="HTML")

    except Exception as e:
        logging.error("OCR Error: %s", e)
        try: bot.delete_message(message.chat.id, wait_msg.message_id)
        except: pass

def generate_password(message):
    if not check_access(message): return
    ssid = message.text.strip().lower()
    
    # دالة التوليد تتعامل الآن مع اللواحق
    pw = generate_wifi_password(ssid) 
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True).add("🔁 مرة أخرى", "🔙 رجوع")
    
    if pw:
        send_and_track(message.chat.id, f"✅ <b>{ssid}</b>\n🔑 <code>{pw}</code>", reply_markup=markup, parse_mode="HTML")
    else:
        bot.send_message(message.chat.id, "❌ صيغة خاطئة أو شبكة غير مدعومة.", reply_markup=markup)

@bot.message_handler(func=lambda m: True)
def fallback(message):
    if check_access(message): show_main_menu(message.chat.id)

# ===== Webhook =====
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    return '', 403

if __name__ == '__main__':
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    app.run(host="0.0.0.0", port=PORT)
