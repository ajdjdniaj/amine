import os
import time
import tempfile
import io
import re
import csv
import logging
import threading
from datetime import datetime, timedelta

from flask import Flask, request
import telebot
from telebot import types

import yt_dlp
from PIL import Image
import pytesseract

from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool

# ===== Logging =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ===== إعداد البيئة =====
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "aie_tool_channel") # بدون @
PORT = int(os.environ.get("PORT", 10000))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN غير معرف في متغيرات البيئة")
if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL غير معرف في متغيرات البيئة")

OWNER_ID = int(os.environ.get("OWNER_ID", "5883400070"))
BAN_DURATION = 5 * 60  # 5 دقائق

# ===== إعداد قاعدة البيانات =====
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL غير معرف. ضع رابط الاتصال في متغير البيئة DATABASE_URL")

DB_MIN_CONN = 1
DB_MAX_CONN = 6
pool = SimpleConnectionPool(DB_MIN_CONN, DB_MAX_CONN, DATABASE_URL, cursor_factory=RealDictCursor, sslmode='require')

def get_db_conn():
    return pool.getconn()

def put_db_conn(conn):
    pool.putconn(conn)

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
    ban_until TIMESTAMP
    );
    """
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
    finally:
        put_db_conn(conn)
init_db()

# ===== إعداد البوت و Flask =====
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ===== هياكل الذاكرة المؤقتة =====
user_links = {}
user_platform = {}
user_video_info = {}
user_state = {}

PLATFORMS = ["يوتيوب", "انستغرام", "تيك توك"]

# ===== أوامر المالك =====
@bot.message_handler(commands=['get_users'])
def get_users_handler(message):
    if int(message.from_user.id) != OWNER_ID:
        return
    try:
        conn = get_db_conn()
        rows = []
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id, first_seen FROM users ORDER BY first_seen DESC")
                rows = cur.fetchall()
        put_db_conn(conn)

        if not rows:
            bot.send_message(message.chat.id, "لا يوجد مستخدمين بعد.")
            return

        fd, path = tempfile.mkstemp(suffix=".csv")
        try:
            with os.fdopen(fd, "w", newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(["user_id", "first_seen"])
                for r in rows:
                    writer.writerow([r['user_id'], r['first_seen']])
            with open(path, "rb") as f:
                bot.send_document(message.chat.id, f, caption="قائمة معرفات المستخدمين (CSV)")
        finally:
            try:
                os.remove(path)
            except:
                pass
    except Exception as e:
        bot.send_message(message.chat.id, "حدث خطأ أثناء جلب المستخدمين.")

@bot.message_handler(commands=['get_banned'])
def get_banned_handler(message):
    if int(message.from_user.id) != OWNER_ID:
        return
    try:
        conn = get_db_conn()
        rows = []
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id, ban_until FROM bans ORDER BY ban_until DESC")
                rows = cur.fetchall()
        put_db_conn(conn)

        if not rows:
            bot.send_message(message.chat.id, "لا يوجد محظورين.")
            return

        fd, path = tempfile.mkstemp(suffix=".csv")
        try:
            with os.fdopen(fd, "w", newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(["user_id", "ban_until"])
                for r in rows:
                    writer.writerow([r['user_id'], r['ban_until']])
            with open(path, "rb") as f:
                bot.send_document(message.chat.id, f, caption="قائمة المحظورين (CSV)")
        finally:
            try:
                os.remove(path)
            except:
                pass
    except Exception as e:
        bot.send_message(message.chat.id, "حدث خطأ أثناء جلب المحظورين.")

@bot.message_handler(commands=['get_joined'])
def get_joined_handler(message):
    if int(message.from_user.id) != OWNER_ID:
        return
    try:
        conn = get_db_conn()
        rows = []
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id, joined_at FROM joined_users ORDER BY joined_at DESC")
                rows = cur.fetchall()
        put_db_conn(conn)

        if not rows:
            bot.send_message(message.chat.id, "لا يوجد من نفذ الشرط بعد.")
            return

        fd, path = tempfile.mkstemp(suffix=".csv")
        try:
            with os.fdopen(fd, "w", newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(["user_id", "joined_at"])
                for r in rows:
                    writer.writerow([r['user_id'], r['joined_at']])
            with open(path, "rb") as f:
                bot.send_document(message.chat.id, f, caption="قائمة من نفّذوا الشرط (CSV)")
        finally:
            try:
                os.remove(path)
            except:
                pass
    except Exception as e:
        bot.send_message(message.chat.id, "حدث خطأ أثناء جلب القائمة.")

@bot.message_handler(commands=['ban_user'])
def ban_user_command(message):
    if int(message.from_user.id) != OWNER_ID:
        return
    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "استخدم الأمر بهذا الشكل:\n/ban_user user_id")
            return
        user_id = parts[1]
        ban_user(user_id, duration=100*365*24*60*60)
        bot.reply_to(message, f"تم حظر المستخدم {user_id} نهائيًا.")
    except Exception as e:
        bot.reply_to(message, "حدث خطأ أثناء الحظر.")

@bot.message_handler(commands=['unban_user'])
def unban_user_command(message):
    if int(message.from_user.id) != OWNER_ID:
        return
    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "استخدم الأمر بهذا الشكل:\n/unban_user user_id")
            return
        user_id = parts[1]
        conn = get_db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM bans WHERE user_id = %s", (int(user_id),))
            bot.reply_to(message, f"تم إلغاء الحظر عن المستخدم {user_id}.")
        finally:
            put_db_conn(conn)
    except Exception as e:
        bot.reply_to(message, "حدث خطأ أثناء إلغاء الحظر.")

# ===== رسائل واجهة الاشتراك =====
def send_welcome_with_channel(chat_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("📢 انضم للقناة", url=f"https://t.me/{CHANNEL_USERNAME}"),
        types.InlineKeyboardButton("✅ تحقق", callback_data="check_join")
    )
    bot.send_message(
        chat_id,
        f"""👋 welcome!

🔒 لاستخدام البوت يجب عليك أولاً الانضمام إلى القناة الاساسية:

⚠️ *تنبيه مهم*: لن تستطيع استخدام البوت حتى تنضم. إذا دخلت القناة ثم خرجت منها لاحقًا سيتم حظرك.

بعد الانضمام للقناة اضغط على زر ✅ تحقق بالأسفل للمتابعة.""",
        reply_markup=markup,
        parse_mode="Markdown"
    )

def send_ban_with_check(chat_id, ban_left):
    minutes = ban_left // 60
    seconds = ban_left % 60
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("📢 انضم للقناة", url=f"https://t.me/{CHANNEL_USERNAME}"),
        types.InlineKeyboardButton("✅ تحقق من جديد", callback_data="recheck")
    )
    bot.send_message(
        chat_id,
        f"❌ تم حظرك من استخدام البوت لمدة 5 دقائق بسبب خروجك من القناة.\n"
        f"الوقت المتبقي: {minutes} دقيقة و {seconds} ثانية.\n\n"
        f"انضم للقناة ثم اضغط تحقق من جديد بعد انتهاء الحظر.\n",
        reply_markup=markup
    )

def send_warning_join(chat_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("📢 انضم للقناة", url=f"https://t.me/{CHANNEL_USERNAME}"),
        types.InlineKeyboardButton("✅ تحقق", callback_data="check_join")
    )
    bot.send_message(
        chat_id,
        f"""⚠️ يجب عليك الانضمام إلى القناة أولاً حتى تتمكن من استخدام البوت.

لن تستطيع استخدام البوت حتى تنضم للقناة.

رابط القناة: https://t.me/{CHANNEL_USERNAME}

بعد الانضمام اضغط على زر ✅ تحقق.""",
        reply_markup=markup
    )

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

# ===== واجهة البوت =====
def show_main_menu(chat_id, msg_only=False):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🎬 أداة تحميل mp3/mp4", "📡 أداة اختراق WiFi fh")
    if msg_only:
        bot.send_message(chat_id, "يرجى اختيار الأداة من القائمة بالأسفل 👇", reply_markup=markup)
    else:
        bot.send_message(chat_id,
        "👋 welcome!\n\n"
        "✨ اختر الخدمة التي تريد استخدامها:\n"
        "🎬 أداة تحميل الفيديوهات والصوتيات (mp3/mp4) من يوتيوب أو انستغرام أو تيك توك.\n"
        "📡 أداة اختراق شبكات WiFi fh_.", reply_markup=markup)
    user_state[chat_id] = "main_menu"

def send_platforms(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for p in PLATFORMS:
        markup.add(p)
    markup.add("🔙 رجوع")
    bot.send_message(chat_id, "يرجى اختيار منصة:", reply_markup=markup)
    user_state[chat_id] = "platforms"

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id
    save_user(user_id)
    ban_left = is_banned(user_id)
    if ban_left > 0:
        send_ban_with_check(message.chat.id, ban_left)
        return
    send_welcome_with_channel(message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "check_join")
def check_join_callback(call):
    try:
        bot.answer_callback_query(call.id, text="جاري التحقق...")
    except Exception:
        pass
    user_id = call.from_user.id
    ban_left = is_banned(user_id)
    if ban_left > 0:
        send_ban_with_check(call.message.chat.id, ban_left)
        return
    if is_user_joined(user_id):
        save_joined_user(user_id)
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        markup.add("🎬 أداة تحميل mp3/mp4", "📡 أداة اختراق WiFi fh")
        try:
            bot.edit_message_text(
            "✅ تم التحقق من اشتراكك في القناة!\n\nاختر الخدمة التي تريد استخدامها:",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup
            )
        except Exception:
            bot.send_message(call.message.chat.id, "✅ تم التحقق من اشتراكك في القناة!\n\nاختر الخدمة التي تريد استخدامها:", reply_markup=markup)
        user_state[call.message.chat.id] = "main_menu"
    else:
        if has_joined_before(user_id):
            ban_user(user_id)
            send_ban_with_check(call.message.chat.id, BAN_DURATION)
        else:
            send_warning_join(call.message.chat.id)
            user_state[call.message.chat.id] = "warned"

@bot.callback_query_handler(func=lambda call: call.data == "recheck")
def recheck_callback(call):
    try:
        bot.answer_callback_query(call.id, text="جاري إعادة التحقق...")
    except Exception:
        pass
    user_id = call.from_user.id
    ban_left = is_banned(user_id)
    if ban_left > 0:
        send_ban_with_check(call.message.chat.id, ban_left)
        return
    if is_user_joined(user_id):
        save_joined_user(user_id)
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        markup.add("🎬 أداة تحميل mp3/mp4", "📡 أداة اختراق WiFi fh")
        try:
            bot.edit_message_text(
            "✅ تم التحقق من اشتراكك في القناة!\n\nاختر الخدمة التي تريد استخدامها:",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup
            )
        except Exception:
            bot.send_message(call.message.chat.id, "✅ تم التحقق من اشتراكك في القناة!\n\nاختر الخدمة التي تريد استخدامها:", reply_markup=markup)
        user_state[call.message.chat.id] = "main_menu"
    else:
        ban_user(user_id)
        send_ban_with_check(call.message.chat.id, BAN_DURATION)

@bot.message_handler(func=lambda m: m.text == "🎬 أداة تحميل mp3/mp4")
def choose_downloader(message):
    if not check_access(message):
        return
    send_platforms(message.chat.id)

@bot.message_handler(func=lambda m: m.text in PLATFORMS)
def ask_for_link(message):
    if not check_access(message):
        return
    # وضع يوتيوب وإنستغرام في الصيانة
    if message.text in ["يوتيوب", "انستغرام"]:
        bot.send_message(message.chat.id, "⚠️ هذه الخدمة في صيانة حاليًا. يرجى اختيار منصة أخرى.")
        send_platforms(message.chat.id)
        return
    user_platform[message.from_user.id] = message.text
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add("🔙 رجوع")
    bot.send_message(message.chat.id, f"📥 أرسل رابط الفيديو من {message.text}:", reply_markup=markup)
    user_state[message.chat.id] = "waiting_link"

@bot.message_handler(func=lambda m: m.text == "🔙 رجوع")
def back_handler(message):
    if not check_access(message):
        return
    state = user_state.get(message.chat.id, "main_menu")
    if state == "waiting_link":
        user_platform.pop(message.from_user.id, None)
        send_platforms(message.chat.id)
    elif state == "platforms":
        show_main_menu(message.chat.id, msg_only=True)
    elif state == "wifi_methods":
        show_main_menu(message.chat.id, msg_only=True)
    elif state == "wifi_name_or_image":
        show_wifi_methods(message.chat.id)
    else:
        show_main_menu(message.chat.id, msg_only=True)

@bot.message_handler(func=lambda m: m.text and m.text.startswith("http"))
def handle_link(message):
    if not check_access(message):
        return
    state = user_state.get(message.chat.id)
    if state != "waiting_link":
        bot.send_message(message.chat.id, "❗ يرجى اختيار المنصة أولاً من القائمة بالأسفل.")
        send_platforms(message.chat.id)
        return

    platform = user_platform.get(message.from_user.id)
    url = message.text.strip()
    user_links[message.from_user.id] = url

    caption = "🎬 اختر نوع التحميل:\n\n🎬 تحميل الفيديو (mp4)\n🎵 تحميل الصوت (mp3)"
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("🎬 تحميل الفيديو", callback_data="video"),
        types.InlineKeyboardButton("🎵 تحميل الصوت (mp3)", callback_data="audio")
    )
    try:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            user_video_info[message.from_user.id] = info
            title = info.get('title', 'بدون عنوان')
            duration = info.get('duration', 0) or 0
            mins = duration // 60
            secs = duration % 60
            caption = f"🎬 <b>{title}</b>\n⏱️ المدة: {mins}:{secs:02d}\n\n🎬 تحميل الفيديو (mp4) أو 🎵 تحميل الصوت (mp3):"
    except Exception as e:
        caption = caption

    bot.send_message(message.chat.id, caption, parse_mode="HTML", reply_markup=markup)
    bot.send_message(message.chat.id, "⬅️ للرجوع اضغط على زر 🔙 رجوع في الأسفل.", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True).add("🔙 رجوع"))
    user_state[message.chat.id] = "waiting_link"

def process_download_threaded(call, url, action):
    msg = bot.send_message(call.message.chat.id, "⏳ جاري التحميل، انتظر قليلاً...")
    tmpdir = tempfile.mkdtemp()
    try:
        ydl_opts = {
            'outtmpl': os.path.join(tmpdir, '%(title)s.%(ext)s'),
            'format': 'best',
            'noplaylist': True,
            'quiet': True,
        }
        if action == "audio":
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if action == "video":
                filename = ydl.prepare_filename(info)
            else:
                filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + ".mp3"

        if not os.path.exists(filename):
            bot.edit_message_text("❌ فشل التحميل أو الملف غير موجود.", call.message.chat.id, msg.message_id)
        else:
            max_bytes = 45 * 1024 * 1024
            size = os.path.getsize(filename)
            if size > max_bytes:
                bot.edit_message_text("❌ الملف كبير جداً ولا يمكن إرساله عبر التليجرام.", call.message.chat.id, msg.message_id)
            else:
                with open(filename, "rb") as f:
                    if action == "video":
                        bot.send_video(call.message.chat.id, f, caption="✅ تم التحميل بنجاح! 🎬")
                    else:
                        bot.send_audio(call.message.chat.id, f, caption="✅ تم التحميل بنجاح! 🎵")
                bot.delete_message(call.message.chat.id, msg.message_id)
    except Exception as e:
        bot.edit_message_text("❌ حدث خطأ أثناء التحميل، يرجى إعادة المحاولة.", call.message.chat.id, msg.message_id)
    finally:
        try:
            for root, dirs, files in os.walk(tmpdir):
                for name in files:
                    os.remove(os.path.join(root, name))
            os.rmdir(tmpdir)
        except:
            pass

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add("منصة أخرى", "نفس المنصة", "🔙 رجوع")
    bot.send_message(call.message.chat.id, "💡 ماذا تريد أن تفعل الآن؟", reply_markup=markup)
    user_state[call.message.chat.id] = "waiting_link"

@bot.callback_query_handler(func=lambda call: call.data in ("video", "audio"))
def process_download(call):
    if not check_access(call):
        return
    url = user_links.get(call.from_user.id)
    action = call.data
    if not url:
        bot.answer_callback_query(call.id, "❌ لم يتم العثور على رابط، أرسل الرابط من جديد.")
        return
    bot.answer_callback_query(call.id, "⏳ جاري التحميل، سيتم إرسال الملف عند الانتهاء.")
    threading.Thread(target=process_download_threaded, args=(call, url, action)).start()

# ===== WiFi tool =====
def show_wifi_methods(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("✍️ كتابة اسم الراوتر", "🖼️ صورة لجميع الراوترات", "🔙 رجوع")
    bot.send_message(chat_id,
    "📡 اختر طريقة إدخال اسم الراوتر:\n"
    "✍️ كتابة اسم الراوتر يدويًا (fh_...)\n"
    "🖼️ أو أرسل صورة لقائمة الشبكات.",
    reply_markup=markup)
    user_state[chat_id] = "wifi_methods"

@bot.message_handler(func=lambda m: m.text == "📡 أداة اختراق WiFi fh")
def wifi_request(message):
    if not check_access(message):
        return
    show_wifi_methods(message.chat.id)

@bot.message_handler(func=lambda m: m.text == "✍️ كتابة اسم الراوتر")
def manual_ssid(message):
    if not check_access(message):
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔙 رجوع")
    sent = bot.send_message(message.chat.id, "🔍 أرسل اسم شبكة WiFi (يجب أن تبدأ بـ fh_):", reply_markup=markup)
    bot.register_next_step_handler(sent, generate_password_with_back)
    user_state[message.chat.id] = "wifi_name_or_image"

def generate_password_with_back(message):
    if not check_access(message):
        return
    if message.text == "🔙 رجوع":
        show_wifi_methods(message.chat.id)
        return
    generate_password(message)

@bot.message_handler(func=lambda m: m.text == "🖼️ صورة لجميع الراوترات")
def ask_for_wifi_image(message):
    if not check_access(message):
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔙 رجوع")
    sent = bot.send_message(message.chat.id, "📸 أرسل صورة لقائمة شبكات WiFi الظاهرة في إعدادات هاتفك *الراوترات المدعومة التي تبدا ب fh فقط*.", reply_markup=markup)
    bot.register_next_step_handler(sent, process_wifi_image_with_back)
    user_state[message.chat.id] = "wifi_name_or_image"

def process_wifi_image_with_back(message):
    if not check_access(message):
        return
    if message.text == "🔙 رجوع":
        show_wifi_methods(message.chat.id)
        return
    process_wifi_image(message)

def extract_ssids_from_text(text):
    return re.findall(r'(fh_[a-zA-Z0-9]+(?:_[a-zA-Z0-9]+)?)', text)

def smart_correct_ssid(ssid):
    parts = ssid.split('_')
    if len(parts) >= 2:
        ssid = f"{parts[0]}_{parts[1]}"
    if ssid.startswith("fh_"):
        prefix = "fh_"
        rest = ssid[3:]
        rest = rest.replace('l', '1').replace('I', '1')
        rest = rest.replace('O', '0').replace('o', '0')
        if len(rest) == 6 and rest[3] == '0':
            rest = rest[:3] + 'a' + rest[4:]
        return prefix + rest
    return ssid

@bot.message_handler(content_types=['photo'])
def process_wifi_image(message):
    if not check_access(message):
        return
    wait_msg = bot.send_message(message.chat.id, "⏳ جاري معالجة الصورة، يرجى الانتظار...")
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        image = Image.open(io.BytesIO(downloaded_file))
    except Exception as e:
        bot.delete_message(message.chat.id, wait_msg.message_id)
        bot.send_message(message.chat.id, "❌ حدث خطأ أثناء تنزيل الصورة.")
        return

    max_width = 800
    if image.width > max_width:
        ratio = max_width / image.width
        new_size = (int(image.width * ratio), int(image.height * ratio))
        image = image.resize(new_size)

    def try_extract(image_obj):
        texts = []
        try:
            texts.append(pytesseract.image_to_string(image_obj, lang='eng'))
        except Exception:
            texts.append("")
        try:
            img2 = image_obj.convert('L').point(lambda x: 0 if x < 140 else 255, '1')
            texts.append(pytesseract.image_to_string(img2, lang='eng'))
        except:
            texts.append("")
        return texts

    texts = try_extract(image)
    all_ssids = []
    seen = set()
    for text in texts:
        found = extract_ssids_from_text(text)
        for ssid in found:
            ssid_corrected = smart_correct_ssid(ssid)
            parts = ssid_corrected.split('_')
            if len(parts) >= 2:
                hex_part = parts[1]
                if ssid_corrected not in seen and all(c in '0123456789abcdef' for c in hex_part.lower()):
                    seen.add(ssid_corrected)
                    all_ssids.append(ssid_corrected)

    bot.delete_message(message.chat.id, wait_msg.message_id)

    if not all_ssids:
        bot.send_message(message.chat.id, "❌ لم يتم العثور على أي شبكة تبدأ بـ fh_ في الصورة.\nيرجى التأكد من وضوح الصورة أو إرسال لقطة شاشة مباشرة من الجهاز.")
        return

    reply = ""
    for ssid in all_ssids:
        password = generate_wifi_password(ssid)
        if password:
            reply += f"📶 <b>{ssid}</b>\n🔑 <code>{password}</code>\n\n"
    reply += "📋 يمكنك نسخ كلمة السر بالضغط عليها."
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔁 اختراق WiFi آخر", "🔙 رجوع")
    bot.send_message(message.chat.id, reply, parse_mode="HTML", reply_markup=markup)

def generate_wifi_password(ssid):
    ssid = ssid.strip().lower()
    if not ssid.startswith("fh_"):
        return None
    parts = ssid.split('_')
    if len(parts) < 2:
        return None
    hex_part = parts[1]
    valid_chars = '0123456789abcdef'
    if not all(c in valid_chars for c in hex_part):
        return None
    table = {
        '0': 'f', '1': 'e', '2': 'd', '3': 'c',
        '4': 'b', '5': 'a', '6': '9', '7': '8',
        '8': '7', '9': '6', 'a': '5', 'b': '4',
        'c': '3', 'd': '2', 'e': '1', 'f': '0'
    }
    encoded = ''.join(table.get(c, c) for c in hex_part)
    return f"wlan{encoded}"

def generate_password(message):
    if not check_access(message):
        return
    ssid = message.text.strip().lower()
    if not ssid.startswith("fh_"):
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("🔁 اختراق WiFi آخر", "🔙 رجوع")
        bot.send_message(message.chat.id, "❌ لم يتم التعرف على الشبكة. أعد المحاولة.", reply_markup=markup)
        return
    parts = ssid.split('_')
    if len(parts) < 2:
        bot.send_message(message.chat.id, "❌ صيغة غير صحيحة.")
        return
    hex_part = parts[1]
    valid_chars = '0123456789abcdef'
    if not all(c in valid_chars for c in hex_part):
        bot.send_message(message.chat.id, "❌ صيغة غير صحيحة.")
        return
    table = {
        '0': 'f', '1': 'e', '2': 'd', '3': 'c',
        '4': 'b', '5': 'a', '6': '9', '7': '8',
        '8': '7', '9': '6', 'a': '5', 'b': '4',
        'c': '3', 'd': '2', 'e': '1', 'f': '0'
    }
    try:
        encoded = ''.join(table.get(c, c) for c in hex_part)
        password = f"wlan{encoded}"
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("🔁 اختراق WiFi آخر", "🔙 رجوع")
        bot.send_message(message.chat.id,
        f"✅ تم توليد كلمة السر الخاصة بالشبكة:\n\n"
        f"🔑 <b>كلمة السر:</b>\n"
        f"<code>{password}</code>\n\n"
        f"📋 يمكنك نسخ كلمة السر بالضغط عليها.",
        parse_mode="HTML",
        reply_markup=markup)
    except Exception:
        bot.send_message(message.chat.id, "❌ حصل خطأ أثناء توليد كلمة السر.")

@bot.message_handler(func=lambda m: m.text == "🔁 اختراق WiFi آخر")
def wifi_again_handler(message):
    if not check_access(message):
        return
    show_wifi_methods(message.chat.id)
    
@bot.message_handler(func=lambda m: m.text == "🔁 اختراق WiFi آخر")
def wifi_again_handler(message):
    if not check_access(message):
        return
    show_wifi_methods(message.chat.id)
    
@bot.message_handler(func=lambda m: True)
def fallback_handler(message):
    if not check_access(message):
        return
    show_main_menu(message.chat.id, msg_only=False)

# ===== Webhook endpoints =====
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        try:
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])
            return '', 200
        except Exception:
            return '', 500
    else:
        return '', 403

@app.route('/')
def index():
    return "Webhook set!", 200

if __name__ == '__main__':
    try:
        bot.remove_webhook()
        bot.set_webhook(url=WEBHOOK_URL)
    except Exception as e:
        pass
    app.run(host="0.0.0.0", port=PORT)
