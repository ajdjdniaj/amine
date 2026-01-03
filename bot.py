# bot.py (النسخة الكاملة: واجهة أزرار شفافة + أدوات المالك + واي فاي + تحميل)
# bot.py
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

# ===== إعدادات التسجيل (Logging) =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ===== إعداد البيئة =====
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "aie_tool_channel")
PORT = int(os.environ.get("PORT", 10000))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN غير معرف")
if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL غير معرف")

OWNER_ID = int(os.environ.get("OWNER_ID", "5883400070"))
BAN_DURATION = 5 * 60  # مدة الحظر: 5 دقائق

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

PLATFORMS = ["يوتيوب", "انستغرام", "تيك توك"]

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
        status = getattr(member, "status", "")
        return status in ('member', 'creator', 'administrator')
    except:
        return False

# ===== رسائل التحقق والحظر =====
def send_welcome_with_channel(chat_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("📢 انضم للقناة", url=f"https://t.me/{CHANNEL_USERNAME}"),
        types.InlineKeyboardButton("✅ تحقق", callback_data="check_join")
    )
    bot.send_message(chat_id, "⚠️ **عذراً!**\nيجب عليك الانضمام إلى القناة أولاً لاستخدام البوت.", reply_markup=markup, parse_mode="Markdown")

def send_ban_with_check(chat_id, ban_left):
    # حساب الدقائق والثواني بدقة
    mins = ban_left // 60
    secs = ban_left % 60
    
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("📢 انضم للقناة", url=f"https://t.me/{CHANNEL_USERNAME}"),
        types.InlineKeyboardButton("✅ تحقق من جديد", callback_data="recheck")
    )
    
    msg_text = (
        f"❌ **تم حظرك مؤقتًا**\n"
        f"بسبب مغادرة القناة أو عدم الانضمام.\n\n"
        f"⏳ **الوقت المتبقي لفك الحظر:**\n"
        f"{mins} دقيقة و {secs} ثانية"
    )
    bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="Markdown")

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
            send_welcome_with_channel(chat_id)
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
            bot.send_document(message.chat.id, f, caption="قائمة المستخدمين (CSV)")
        os.remove(path)
    except: pass

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
            bot.send_document(message.chat.id, f, caption="قائمة المحظورين (CSV)")
        os.remove(path)
    except: pass

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
        bot.send_message(message.chat.id, f"📊 الإحصائيات:\n👥 المستخدمين: {u}\n✅ المشتركين: {j}\n⛔ المحظورين: {b}")
    except: pass

@bot.message_handler(commands=['ban_user'])
def ban_user_command(message):
    if int(message.from_user.id) != OWNER_ID: return
    try:
        parts = message.text.split()
        if len(parts) == 2:
            ban_user(parts[1], 3153600000)
            bot.reply_to(message, "✅ تم الحظر بنجاح.")
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
            bot.reply_to(message, "✅ تم إلغاء الحظر.")
    except: pass

# ===== القوائم (Reply Keyboards) =====
def show_main_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🎬 أداة تحميل mp3/mp4", "📡 أداة اختراق WiFi fh")
    bot.send_message(chat_id, "👋 أهلاً بك! اختر الخدمة من الأسفل:", reply_markup=markup)
    user_state[chat_id] = "main_menu"

def show_wifi_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("✍️ كتابة اسم الراوتر", "🖼️ صورة لجميع الراوترات", "🔙 رجوع")
    bot.send_message(chat_id, "📡 اختر طريقة الإدخال:", reply_markup=markup)
    user_state[chat_id] = "wifi_menu"

def show_platforms(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("يوتيوب", "انستغرام", "تيك توك", "🔙 رجوع")
    bot.send_message(chat_id, "📥 اختر المنصة للتحميل:", reply_markup=markup)
    user_state[chat_id] = "platforms"

# ===== معالجة الأوامر والرسائل =====
@bot.message_handler(commands=['start'])
def start_handler(message):
    save_user(message.from_user.id)
    if check_access(message):
        show_main_menu(message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data in ["check_join", "recheck"])
def check_join_callback(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    if is_user_joined(user_id):
        save_joined_user(user_id)
        try: bot.delete_message(chat_id, call.message.message_id)
        except: pass
        show_main_menu(chat_id)
    else:
        if call.data == "recheck":
            ban_user(user_id)
            send_ban_with_check(chat_id, BAN_DURATION)
        else:
            bot.answer_callback_query(call.id, "⚠️ لم تنضم للقناة بعد!")

# --- قسم الواي فاي (WiFi) ---
@bot.message_handler(func=lambda m: m.text == "📡 أداة اختراق WiFi fh")
def wifi_entry(message):
    if check_access(message): show_wifi_menu(message.chat.id)

@bot.message_handler(func=lambda m: m.text == "✍️ كتابة اسم الراوتر")
def wifi_manual(message):
    if not check_access(message): return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True).add("🔙 رجوع")
    sent = bot.send_message(message.chat.id, "🔍 أرسل اسم الشبكة (مثال: fh_xxxx):", reply_markup=markup)
    bot.register_next_step_handler(sent, process_manual_wifi)

def process_manual_wifi(message):
    if message.text == "🔙 رجوع":
        show_wifi_menu(message.chat.id)
        return
    ssid = message.text.strip().lower()
    pw = generate_wifi_password(ssid)
    if pw:
        bot.send_message(message.chat.id, f"✅ <b>الشبكة:</b> {ssid}\n🔑 <b>كلمة السر:</b> <code>{pw}</code>", parse_mode="HTML")
    else:
        bot.send_message(message.chat.id, "❌ اسم الشبكة غير صحيح أو غير مدعوم.")
    
    sent = bot.send_message(message.chat.id, "🔍 أرسل اسماً آخر أو اضغط رجوع:", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add("🔙 رجوع"))
    bot.register_next_step_handler(sent, process_manual_wifi)

@bot.message_handler(func=lambda m: m.text == "🖼️ صورة لجميع الراوترات")
def wifi_photo(message):
    if not check_access(message): return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True).add("🔙 رجوع")
    sent = bot.send_message(message.chat.id, "📸 أرسل صورة تحتوي على أسماء الشبكات:", reply_markup=markup)
    bot.register_next_step_handler(sent, process_photo_wifi)

def process_photo_wifi(message):
    if message.text == "🔙 رجوع":
        show_wifi_menu(message.chat.id)
        return
    
    if not message.photo:
        sent = bot.send_message(message.chat.id, "❌ يرجى إرسال صورة صالحة.", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add("🔙 رجوع"))
        bot.register_next_step_handler(sent, process_photo_wifi)
        return

    wait = bot.send_message(message.chat.id, "⏳ جاري معالجة الصورة...")
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        img = Image.open(io.BytesIO(downloaded))
        
        if img.width > 800: img = img.resize((800, int(800*img.height/img.width)))
        
        texts = [pytesseract.image_to_string(img)]
        texts.append(pytesseract.image_to_string(img.convert('L').point(lambda x: 0 if x<140 else 255, '1')))
        
        found_ssids = set()
        for t in texts:
            matches = re.findall(r'(fh_[a-fA-F0-9]+(?:_[a-zA-Z0-9]+)?)', t, re.IGNORECASE)
            for m in matches:
                clean = smart_correct_ssid(m.lower())
                parts = clean.split('_')
                if len(parts) >= 2 and all(c in '0123456789abcdef' for c in parts[1]):
                    found_ssids.add(clean)
        
        try: bot.delete_message(message.chat.id, wait.message_id)
        except: pass

        if not found_ssids:
            bot.send_message(message.chat.id, "❌ لم يتم العثور على شبكات fh في الصورة.")
        else:
            msg = ""
            for s in found_ssids:
                pw = generate_wifi_password(s)
                if pw: msg += f"📶 <b>{s}</b>\n🔑 <code>{pw}</code>\n\n"
            bot.send_message(message.chat.id, msg or "❌ الشبكات الموجودة غير مدعومة.", parse_mode="HTML")
            
    except Exception as e:
        logging.error("OCR Error: %s", e)
        bot.send_message(message.chat.id, "❌ حدث خطأ أثناء المعالجة.")
    
    sent = bot.send_message(message.chat.id, "📸 أرسل صورة أخرى أو اضغط رجوع:", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add("🔙 رجوع"))
    bot.register_next_step_handler(sent, process_photo_wifi)

# --- قسم التحميل (Download) ---
@bot.message_handler(func=lambda m: m.text == "🎬 أداة تحميل mp3/mp4")
def dl_entry(message):
    if check_access(message): show_platforms(message.chat.id)

@bot.message_handler(func=lambda m: m.text in PLATFORMS)
def dl_platform(message):
    if not check_access(message): return
    if message.text in ["يوتيوب", "انستغرام"]:
        bot.send_message(message.chat.id, "⚠️ هذه الخدمة قيد الصيانة حالياً.")
        return
    
    user_platform[message.from_user.id] = message.text
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True).add("🔙 رجوع")
    bot.send_message(message.chat.id, f"📥 أرسل رابط الفيديو من {message.text}:", reply_markup=markup)
    user_state[message.chat.id] = "waiting_link"

@bot.message_handler(func=lambda m: m.text == "🔙 رجوع")
def back_btn(message):
    if not check_access(message): return
    state = user_state.get(message.chat.id)
    if state == "waiting_link": show_platforms(message.chat.id)
    elif state in ["wifi_menu", "platforms"]: show_main_menu(message.chat.id)
    else: show_main_menu(message.chat.id)

@bot.message_handler(func=lambda m: m.text and m.text.startswith("http"))
def dl_link(message):
    if not check_access(message): return
    if user_state.get(message.chat.id) != "waiting_link":
        show_main_menu(message.chat.id)
        return

    user_links[message.from_user.id] = message.text.strip()
    wait = bot.send_message(message.chat.id, "🔎 جاري البحث عن الفيديو...")
    
    try:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(message.text, download=False)
            title = info.get('title', 'فيديو')
            duration = info.get('duration', 0)
            m, s = divmod(duration, 60)
            
            try: bot.delete_message(message.chat.id, wait.message_id)
            except: pass
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🎥 فيديو (MP4)", callback_data="vid"),
                       types.InlineKeyboardButton("🎵 صوت (MP3)", callback_data="aud"))
            
            bot.send_message(message.chat.id, f"🎬 <b>{title}</b>\n⏱️ {m}:{s:02d}", reply_markup=markup, parse_mode="HTML")
            
    except Exception:
        try: bot.delete_message(message.chat.id, wait.message_id)
        except: pass
        bot.send_message(message.chat.id, "❌ فشل جلب معلومات الفيديو.")

@bot.callback_query_handler(func=lambda call: call.data in ["vid", "aud"])
def dl_process(call):
    chat_id = call.message.chat.id
    url = user_links.get(call.from_user.id)
    if not url:
        bot.answer_callback_query(call.id, "❌ انتهت الجلسة.")
        return

    bot.edit_message_text("⏳ **جاري التحميل... يرجى الانتظار**", chat_id, call.message.message_id, parse_mode="Markdown")
    
    tmp = tempfile.mkdtemp()
    try:
        ydl_opts = {'outtmpl': os.path.join(tmp, '%(title)s.%(ext)s'), 'quiet': True}
        if call.data == "aud":
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3'}]
        else:
            ydl_opts['format'] = 'best'

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            fpath = ydl.prepare_filename(info)
            if call.data == "aud": fpath = fpath.rsplit('.', 1)[0] + ".mp3"

        if os.path.exists(fpath) and os.path.getsize(fpath) < 50*1024*1024:
            with open(fpath, "rb") as f:
                if call.data == "vid": bot.send_video(chat_id, f, caption="✅ تم التحميل بنجاح!")
                else: bot.send_audio(chat_id, f, caption="✅ تم التحميل بنجاح!")
            try: bot.delete_message(chat_id, call.message.message_id)
            except: pass
        else:
            bot.edit_message_text("❌ الملف كبير جداً (أكبر من 50 ميجابايت).", chat_id, call.message.message_id)

    except Exception as e:
        logging.error(e)
        bot.edit_message_text("❌ فشل التحميل.", chat_id, call.message.message_id)
    finally:
        try:
            for f in os.listdir(tmp): os.remove(os.path.join(tmp, f))
            os.rmdir(tmp)
        except: pass

# --- دوال مساعدة (Helpers) ---
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
