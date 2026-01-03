# bot.py (نسخة Inline Buttons - واجهة نظيفة واحترافية)
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
BAN_DURATION = 5 * 60

# ===== إعداد قاعدة البيانات =====
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL غير معرف")

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
        return pool.getconn()
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
user_state = {}
# لتخزين آخر رسالة قائمة (لحذفها أو تعديلها)
user_last_menu_id = {}

PLATFORMS_MAP = {
    "youtube": "يوتيوب",
    "instagram": "انستغرام",
    "tiktok": "تيك توك"
}

# ===== دوال مساعدة للتنظيف =====
def delete_last_menu(chat_id):
    """حذف رسالة القائمة السابقة إذا وجدت"""
    msg_id = user_last_menu_id.get(chat_id)
    if msg_id:
        try:
            bot.delete_message(chat_id, msg_id)
        except:
            pass
        user_last_menu_id.pop(chat_id, None)

def save_menu_id(chat_id, msg_id):
    user_last_menu_id[chat_id] = msg_id

# ===== دوال DB (User/Ban/Join) =====
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
                if not row: return 0
                ban_until = row['ban_until']
                if ban_until and now_ts < ban_until:
                    return int((ban_until - now_ts).total_seconds())
                else:
                    cur.execute("DELETE FROM bans WHERE user_id = %s", (int(user_id),))
                    return 0
    finally:
        put_db_conn(conn)

def ban_user(user_id, duration=BAN_DURATION):
    if int(user_id) == OWNER_ID: return
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
        if int(user_id) == OWNER_ID: return True
        member = bot.get_chat_member(f"@{CHANNEL_USERNAME}", int(user_id))
        return getattr(member, "status", "") in ('member', 'creator', 'administrator')
    except:
        return False

# ===== التحقق والترحيب =====
def check_access(message_or_call):
    if isinstance(message_or_call, telebot.types.CallbackQuery):
        user_id = message_or_call.from_user.id
        chat_id = message_or_call.message.chat.id
    else:
        user_id = message_or_call.from_user.id
        chat_id = message_or_call.chat.id

    ban_left = is_banned(user_id)
    if ban_left > 0:
        mins = (ban_left % 3600) // 60
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📢 انضم للقناة", url=f"https://t.me/{CHANNEL_USERNAME}"))
        markup.add(types.InlineKeyboardButton("✅ تحقق من جديد", callback_data="recheck_ban"))
        
        # إذا كان نداء callback نعدل الرسالة، وإلا نرسل جديدة
        text = f"❌ تم حظرك لمدة 5 دقائق.\nالمتبقي: {mins} دقيقة."
        if isinstance(message_or_call, telebot.types.CallbackQuery):
            try: bot.edit_message_text(text, chat_id, message_or_call.message.message_id, reply_markup=markup)
            except: pass
        else:
            bot.send_message(chat_id, text, reply_markup=markup)
        return False

    if not is_user_joined(user_id):
        if has_joined_before(user_id):
            ban_user(user_id)
            # استدعاء دالة Ban هنا أو إعادة استدعاء check_access ليعرض الحظر
            return check_access(message_or_call)
        else:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("📢 انضم للقناة", url=f"https://t.me/{CHANNEL_USERNAME}"))
            markup.add(types.InlineKeyboardButton("✅ تحقق", callback_data="check_join"))
            text = "🔒 يجب الانضمام للقناة أولاً لاستخدام البوت."
            if isinstance(message_or_call, telebot.types.CallbackQuery):
                try: bot.edit_message_text(text, chat_id, message_or_call.message.message_id, reply_markup=markup)
                except: pass
            else:
                sent = bot.send_message(chat_id, text, reply_markup=markup)
                save_menu_id(chat_id, sent.message_id)
        return False
    return True

# ===== القوائم (Inline Menus) =====

def send_main_menu(chat_id, edit_msg_id=None):
    """إرسال القائمة الرئيسية (أزرار شفافة)"""
    markup = types.InlineKeyboardMarkup(row_width=1)
    btn1 = types.InlineKeyboardButton("🎬 أداة تحميل mp3/mp4", callback_data="menu_download")
    btn2 = types.InlineKeyboardButton("📡 أداة اختراق WiFi fh", callback_data="menu_wifi")
    markup.add(btn1, btn2)
    
    text = "👋 أهلاً بك!\n✨ اختر الخدمة:"
    
    if edit_msg_id:
        try:
            bot.edit_message_text(text, chat_id, edit_msg_id, reply_markup=markup)
            save_menu_id(chat_id, edit_msg_id)
            return
        except:
            pass # فشل التعديل، نرسل جديد
            
    # إرسال رسالة جديدة إذا لم نعدل
    sent = bot.send_message(chat_id, text, reply_markup=markup)
    save_menu_id(chat_id, sent.message_id)

def send_download_menu(chat_id, edit_msg_id=None):
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_yt = types.InlineKeyboardButton("يوتيوب", callback_data="platform_youtube")
    btn_ig = types.InlineKeyboardButton("انستغرام", callback_data="platform_instagram")
    btn_tk = types.InlineKeyboardButton("تيك توك", callback_data="platform_tiktok")
    btn_back = types.InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")
    
    markup.add(btn_yt, btn_ig)
    markup.add(btn_tk)
    markup.add(btn_back)
    
    text = "✨ اختر المنصة للتحميل:\n(mp4/mp3)"
    
    if edit_msg_id:
        try:
            bot.edit_message_text(text, chat_id, edit_msg_id, reply_markup=markup)
            return
        except: pass
    
    sent = bot.send_message(chat_id, text, reply_markup=markup)
    save_menu_id(chat_id, sent.message_id)

def send_wifi_menu(chat_id, edit_msg_id=None):
    markup = types.InlineKeyboardMarkup(row_width=1)
    btn_manual = types.InlineKeyboardButton("✍️ كتابة اسم الراوتر", callback_data="wifi_manual")
    btn_photo = types.InlineKeyboardButton("🖼️ صورة لجميع الراوترات", callback_data="wifi_photo")
    btn_back = types.InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")
    markup.add(btn_manual, btn_photo, btn_back)
    
    text = "📡 اختر الطريقة:"
    
    if edit_msg_id:
        try:
            bot.edit_message_text(text, chat_id, edit_msg_id, reply_markup=markup)
            return
        except: pass
    
    sent = bot.send_message(chat_id, text, reply_markup=markup)
    save_menu_id(chat_id, sent.message_id)

# ===== Handlers (الأوامر والـ Callbacks) =====

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id
    save_user(user_id)
    # حذف أي قائمة سابقة لتنظيف الشات
    delete_last_menu(message.chat.id)
    
    if check_access(message):
        send_main_menu(message.chat.id)

# معالج الأزرار الشفافة (قلب البوت الجديد)
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    
    # 1. التحقق العام
    if call.data == "check_join":
        if is_user_joined(user_id):
            save_joined_user(user_id)
            bot.answer_callback_query(call.id, "✅ تم التحقق!")
            send_main_menu(chat_id, edit_msg_id=call.message.message_id)
        else:
            bot.answer_callback_query(call.id, "⚠️ لم تنضم بعد!", show_alert=True)
        return

    if call.data == "recheck_ban":
        if check_access(call): # سيعيد التحقق من الوقت
            bot.answer_callback_query(call.id, "✅ انتهى الحظر.")
            send_main_menu(chat_id, edit_msg_id=call.message.message_id)
        else:
            bot.answer_callback_query(call.id, "❌ ما زلت محظوراً.")
        return

    # التحقق من الصلاحية لباقي الأزرار
    if not check_access(call):
        return

    # 2. التنقل في القوائم
    if call.data == "back_to_main":
        user_state[chat_id] = "main_menu" # Reset state
        send_main_menu(chat_id, edit_msg_id=call.message.message_id)
    
    elif call.data == "menu_download":
        send_download_menu(chat_id, edit_msg_id=call.message.message_id)
        user_state[chat_id] = "menu_download"
        
    elif call.data == "menu_wifi":
        send_wifi_menu(chat_id, edit_msg_id=call.message.message_id)
        user_state[chat_id] = "menu_wifi"

    # 3. خيارات التحميل
    elif call.data.startswith("platform_"):
        platform_key = call.data.split("_")[1]
        platform_name = PLATFORMS_MAP.get(platform_key, platform_key)
        
        if platform_key in ["youtube", "instagram"]:
            bot.answer_callback_query(call.id, "⚠️ صيانة مؤقتة", show_alert=True)
            return
            
        user_platform[user_id] = platform_name
        user_state[chat_id] = "waiting_link"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_download"))
        
        text = f"📥 أرسل رابط الفيديو من {platform_name}:"
        try:
            bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=markup)
        except: pass

    # 4. خيارات WiFi
    elif call.data == "wifi_manual":
        user_state[chat_id] = "waiting_ssid"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_wifi"))
        try:
            bot.edit_message_text("🔍 أرسل اسم الشبكة (يجب أن تبدأ بـ fh_):", chat_id, call.message.message_id, reply_markup=markup)
        except: pass
        
    elif call.data == "wifi_photo":
        user_state[chat_id] = "waiting_wifi_photo"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_wifi"))
        try:
            bot.edit_message_text("📸 أرسل صورة لقائمة الشبكات:", chat_id, call.message.message_id, reply_markup=markup)
        except: pass

    # 5. خيارات التحميل (فيديو/صوت) بعد جلب الرابط
    elif call.data in ["dl_video", "dl_audio"]:
        url = user_links.get(user_id)
        if not url:
            bot.answer_callback_query(call.id, "❌ انتهت الجلسة.")
            return
            
        bot.edit_message_text("⏳ **جاري التحميل...**", chat_id, call.message.message_id, parse_mode="Markdown")
        process_media_download(chat_id, call.message.message_id, url, call.data)

# ===== معالجة الرسائل (المدخلات اليدوية فقط) =====

@bot.message_handler(func=lambda m: True, content_types=['text', 'photo'])
def handle_inputs(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    if not check_access(message): return
    
    state = user_state.get(chat_id)
    
    # حالة انتظار الرابط
    if state == "waiting_link" and message.text:
        # حذف رسالة المستخدم لتنظيف الشات (اختياري، يفضله البعض)
        try: bot.delete_message(chat_id, message.message_id)
        except: pass
        
        # حذف رسالة البوت السابقة (طلب الرابط)
        delete_last_menu(chat_id)

        url = message.text.strip()
        platform = user_platform.get(user_id)
        
        # تحقق بسيط
        if platform == "تيك توك" and "tiktok" not in url and "تيك توك" not in url:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_download"))
            sent = bot.send_message(chat_id, "❌ الرابط لا يبدو صحيحاً.", reply_markup=markup)
            save_menu_id(chat_id, sent.message_id)
            return

        user_links[user_id] = url
        
        # إرسال رسالة المعالجة
        wait_msg = bot.send_message(chat_id, "🎬 جاري جلب المعلومات...")
        
        try:
            with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
                info = ydl.extract_info(url, download=False)
                title = info.get('title', 'Video')[:100]
                duration = info.get('duration', 0)
                mins, secs = divmod(duration, 60)
                
                caption = f"🎬 <b>{title}</b>\n⏱️ {mins}:{secs:02d}\n\nاختر الصيغة:"
                markup = types.InlineKeyboardMarkup()
                markup.add(
                    types.InlineKeyboardButton("🎥 فيديو (MP4)", callback_data="dl_video"),
                    types.InlineKeyboardButton("🎵 صوت (MP3)", callback_data="dl_audio")
                )
                markup.add(types.InlineKeyboardButton("🔙 إلغاء", callback_data="menu_download"))
                
                bot.edit_message_text(caption, chat_id, wait_msg.message_id, parse_mode="HTML", reply_markup=markup)
                save_menu_id(chat_id, wait_msg.message_id)
                
        except Exception as e:
            logging.error("YTDL Info Error: %s", e)
            markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_download"))
            bot.edit_message_text("❌ لم يتم العثور على الفيديو.", chat_id, wait_msg.message_id, reply_markup=markup)
            save_menu_id(chat_id, wait_msg.message_id)

    # حالة انتظار اسم الراوتر
    elif state == "waiting_ssid" and message.text:
        # لا نحذف رسالة المستخدم هنا لأنه يكتب نصاً
        delete_last_menu(chat_id)
        
        ssid = message.text.strip().lower()
        pw = generate_wifi_password(ssid)
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔁 مرة أخرى", callback_data="wifi_manual"))
        markup.add(types.InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="back_to_main"))
        
        if pw:
            res = f"✅ <b>{ssid}</b>\n🔑 <code>{pw}</code>"
            sent = bot.send_message(chat_id, res, parse_mode="HTML", reply_markup=markup)
        else:
            sent = bot.send_message(chat_id, "❌ صيغة غير صحيحة أو شبكة غير مدعومة.", reply_markup=markup)
        save_menu_id(chat_id, sent.message_id)

    # حالة انتظار صورة الراوتر
    elif state == "waiting_wifi_photo" and message.photo:
        delete_last_menu(chat_id)
        process_wifi_image(message)

    # أي رسالة أخرى في غير وقتها
    else:
        # إذا أرسل المستخدم رسالة عشوائية، نعيد عرض القائمة الرئيسية
        delete_last_menu(chat_id)
        send_main_menu(chat_id)


# ===== دوال المعالجة (Logic) =====

def process_media_download(chat_id, msg_id, url, action):
    tmpdir = tempfile.mkdtemp()
    try:
        ydl_opts = {
            'outtmpl': os.path.join(tmpdir, '%(title)s.%(ext)s'),
            'format': 'best',
            'quiet': True,
        }
        if action == "dl_audio":
            ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '192'}]
            
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if action == "dl_video": filename = ydl.prepare_filename(info)
            else: filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + ".mp3"

        if os.path.exists(filename) and os.path.getsize(filename) < 50*1024*1024:
            with open(filename, "rb") as f:
                if action == "dl_video": bot.send_video(chat_id, f, caption="✅ تم التحميل!")
                else: bot.send_audio(chat_id, f, caption="✅ تم التحميل!")
            
            # حذف رسالة الانتظار
            try: bot.delete_message(chat_id, msg_id)
            except: pass
            
            # عرض زر للعودة
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("📥 تحميل آخر", callback_data="menu_download"))
            markup.add(types.InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="back_to_main"))
            sent = bot.send_message(chat_id, "💡 ماذا تريد أن تفعل الآن؟", reply_markup=markup)
            save_menu_id(chat_id, sent.message_id)
        else:
            markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_download"))
            bot.edit_message_text("❌ الملف كبير جداً (أكبر من 50MB).", chat_id, msg_id, reply_markup=markup)

    except Exception as e:
        logging.error("DL Error: %s", e)
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 رجوع", callback_data="menu_download"))
        try: bot.edit_message_text("❌ فشل التحميل.", chat_id, msg_id, reply_markup=markup)
        except: pass
    finally:
        try:
            for f in os.listdir(tmpdir): os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)
        except: pass
    
    # إعادة تعيين الحالة لتجنب تكرار الإرسال
    user_state[chat_id] = "menu_download"

# --- WiFi Logic ---
def smart_correct_ssid(ssid):
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
    chat_id = message.chat.id
    wait_msg = bot.send_message(chat_id, "⏳ جاري المعالجة...")
    
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        img = Image.open(io.BytesIO(downloaded))
        if img.width > 800: img = img.resize((800, int(800*img.height/img.width)))
        
        texts = [pytesseract.image_to_string(img)]
        texts.append(pytesseract.image_to_string(img.convert('L').point(lambda x: 0 if x<140 else 255, '1')))
        
        all_ssids = set()
        for t in texts:
            found = re.findall(r'(fh_[a-fA-F0-9]+(?:_[a-zA-Z0-9]+)?)', t, re.IGNORECASE)
            for s in found:
                corrected = smart_correct_ssid(s.lower())
                parts = corrected.split('_')
                if len(parts) >= 2 and all(c in '0123456789abcdef' for c in parts[1]):
                    all_ssids.add(corrected)
        
        bot.delete_message(chat_id, wait_msg.message_id)
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔁 صورة أخرى", callback_data="wifi_photo"))
        markup.add(types.InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="back_to_main"))

        if not all_ssids:
            sent = bot.send_message(chat_id, "❌ لم يتم العثور على شبكات fh.", reply_markup=markup)
            save_menu_id(chat_id, sent.message_id)
            return

        reply = ""
        for ssid in all_ssids:
            pw = generate_wifi_password(ssid)
            if pw: reply += f"📶 <b>{ssid}</b>\n🔑 <code>{pw}</code>\n\n"
            
        sent = bot.send_message(chat_id, reply or "❌ شبكات غير مدعومة", reply_markup=markup, parse_mode="HTML")
        save_menu_id(chat_id, sent.message_id)

    except Exception as e:
        logging.error("OCR Error: %s", e)
        try: bot.delete_message(chat_id, wait_msg.message_id)
        except: pass
        bot.send_message(chat_id, "❌ حدث خطأ في المعالجة.")

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
    try:
        bot.remove_webhook()
        bot.set_webhook(url=WEBHOOK_URL)
    except: pass
    app.run(host="0.0.0.0", port=PORT)
