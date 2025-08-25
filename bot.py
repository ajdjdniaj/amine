import os
import telebot
from telebot import types
import time
import yt_dlp
import requests

from PIL import Image
import pytesseract
import io
import re

from flask import Flask, request

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_USERNAME = "aie_tool_channel"  # بدون @
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") # عدل الرابط حسب نطاق مشروعك في Render

BAN_FILE = "banned.txt"
BAN_DURATION = 24 * 60 * 60  # 24 ساعة بالثواني

OWNER_ID = "5883400070"  # ايدي المالك

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

user_links = {}
user_platform = {}
user_video_info = {}
user_state = {}

PLATFORMS = ["يوتيوب", "انستغرام", "تيك توك"]

# --- دوال الحظر والتحقق من القناة ---

def is_banned(user_id):
    if str(user_id) == OWNER_ID:
        return 0
    now = int(time.time())
    try:
        with open(BAN_FILE, "r") as f:
            for line in f:
                uid, ban_until = line.strip().split(":")
                if str(user_id) == uid and now < int(ban_until):
                    return int(ban_until) - now
    except FileNotFoundError:
        pass
    return 0

def ban_user(user_id):
    if str(user_id) == OWNER_ID:
        return
    ban_until = int(time.time()) + BAN_DURATION
    lines = []
    try:
        with open(BAN_FILE, "r") as f:
            lines = [line for line in f if not line.startswith(str(user_id) + ":")]
    except FileNotFoundError:
        pass
    lines.append(f"{user_id}:{ban_until}\n")
    with open(BAN_FILE, "w") as f:
        f.writelines(lines)

def is_user_joined(user_id):
    if str(user_id) == OWNER_ID:
        return True
    try:
        member = bot.get_chat_member(f"@{CHANNEL_USERNAME}", user_id)
        return member.status in ['member', 'creator', 'administrator']
    except:
        return False

def ban_message(chat_id, ban_left=None):
    if ban_left is not None:
        hours = ban_left // 3600
        minutes = (ban_left % 3600) // 60
        time_msg = f"\nالوقت المتبقي: {hours} ساعة و {minutes} دقيقة."
    else:
        time_msg = ""
    bot.send_message(
        chat_id,
        f"❌ تم حظرك من استخدام البوت لمدة 24 ساعة بسبب خروجك من القناة أو التحايل على البوت.{time_msg}\n\n"
        f"يمكنك تصحيح ذلك بالانضمام مجددًا للقناة ثم الانتظار حتى انتهاء مدة الحظر.\n"
        f"رابط القناة: https://t.me/{CHANNEL_USERNAME}",
        reply_markup=types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton("📢 انضم للقناة", url=f"https://t.me/{CHANNEL_USERNAME}")
        )
    )

def check_access(message):
    user_id = message.from_user.id
    if str(user_id) == OWNER_ID:
        return True
    ban_left = is_banned(user_id)
    if ban_left > 0:
        ban_message(message.chat.id, ban_left)
        return False
    if not is_user_joined(user_id):
        ban_user(user_id)
        ban_message(message.chat.id)
        return False
    return True

# --- واجهة البوت ---

def show_main_menu(chat_id, msg_only=False):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🎬 أداة تحميل mp3/mp4", "📡 أداة اختراق WiFi fh")
    if msg_only:
        bot.send_message(
            chat_id,
            "يرجى اختيار الأداة من القائمة بالأسفل 👇",
            reply_markup=markup
        )
    else:
        bot.send_message(
            chat_id,
            "👋 أهلاً بك في البوت الشامل!\n\n"
            "✨ اختر الخدمة التي تريد استخدامها:\n"
            "🎬 أداة تحميل الفيديوهات والصوتيات (mp3/mp4) من يوتيوب أو انستغرام أو تيك توك.\n"
            "📡 أداة اختراق شبكات WiFi fh_.",
            reply_markup=markup
        )
    user_state[chat_id] = "main_menu"

def send_platforms(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for p in PLATFORMS:
        markup.add(p)
    markup.add("🔙 رجوع")
    bot.send_message(
        chat_id,
        "✨ اختر المنصة التي تريد التحميل منها:\n"
        "0️⃣ يوتيوب: تحميل فيديوهات يوتيوب (mp4 أو mp3).\n"
        "1️⃣ انستغرام: تحميل فيديوهات أو ريلز انستغرام (mp4 أو mp3).\n"
        "2️⃣ تيك توك: تحميل فيديوهات تيك توك بدون علامة مائية (mp4 أو mp3).",
        reply_markup=markup
    )
    user_state[chat_id] = "platforms"

@bot.message_handler(commands=['start'])
def start_handler(message):
    if not check_access(message):
        return
    show_main_menu(message.chat.id)

@bot.message_handler(func=lambda m: m.text == "🎬 أداة تحميل mp3/mp4")
def choose_downloader(message):
    if not check_access(message):
        return
    send_platforms(message.chat.id)

@bot.message_handler(func=lambda m: m.text in PLATFORMS)
def ask_for_link(message):
    if not check_access(message):
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

# --- تحميل من يوتيوب عبر savefrom.net ---

def get_savefrom_link(youtube_url, audio=False):
    api_url = "https://worker.sf-tools.com/savefrom.php"
    params = {
        "sf_url": youtube_url,
        "sf_submit": "",
        "new": 2,
        "lang": "ar",
        "app": "",
        "country": "ar",
        "os": "Windows",
        "browser": "Chrome"
    }
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    resp = requests.post(api_url, data=params, headers=headers)
    if resp.status_code == 200:
        data = resp.json()
        if "url" in data and data["url"]:
            if isinstance(data["url"], list):
                if audio:
                    for item in data["url"]:
                        if "mp3" in item.get("type", ""):
                            return item["url"], "mp3"
                for item in data["url"]:
                    if "mp4" in item.get("type", ""):
                        return item["url"], "mp4"
            elif isinstance(data["url"], dict):
                return data["url"].get("url"), data["url"].get("type", "mp4")
    return None, None

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

    if (platform == "يوتيوب" and not ("youtube.com" in url or "youtu.be" in url or "يوتيوب" in url)) or \
       (platform == "انستغرام" and not ("instagram" in url or "انستغرام" in url)) or \
       (platform == "تيك توك" and not ("tiktok" in url or "تيك توك" in url)):
        bot.send_message(
            message.chat.id,
            "❌ هذا الرابط لا يخص المنصة المختارة.\nيرجى اختيار المنصة الصحيحة من جديد.",
        )
        send_platforms(message.chat.id)
        user_platform.pop(message.from_user.id, None)
        return

    user_links[message.from_user.id] = url

    if platform == "يوتيوب":
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("🎬 تحميل الفيديو (mp4)", callback_data=f"yt_video|{url}"),
            types.InlineKeyboardButton("🎵 تحميل الصوت (mp3)", callback_data=f"yt_audio|{url}")
        )
        bot.send_message(message.chat.id, "🎬 اختر نوع التحميل:\n\n🎬 تحميل الفيديو (mp4)\n🎵 تحميل الصوت (mp3)", reply_markup=markup)
        return

    # باقي المنصات (انستغرام/تيك توك) كما هو في كودك
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
            duration = info.get('duration', 0)
            thumb = info.get('thumbnail')
            mins = duration // 60
            secs = duration % 60
            caption = f"🎬 <b>{title}</b>\n⏱️ المدة: {mins}:{secs:02d}\n\n🎬 تحميل الفيديو (mp4) أو 🎵 تحميل الصوت (mp3):"
    except Exception as e:
        thumb = None

    if thumb:
        bot.send_photo(message.chat.id, thumb, caption=caption, parse_mode="HTML", reply_markup=markup)
    else:
        bot.send_message(message.chat.id, caption, parse_mode="HTML", reply_markup=markup)
    bot.send_message(message.chat.id, "⬅️ للرجوع اضغط على زر 🔙 رجوع في الأسفل.", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True).add("🔙 رجوع"))
    user_state[message.chat.id] = "waiting_link"

@bot.callback_query_handler(func=lambda call: call.data.startswith("yt_"))
def process_youtube_download(call):
    if not check_access(call):
        return
    action, url = call.data.split("|", 1)
    msg = bot.send_message(call.message.chat.id, "⏳ جاري التحميل من يوتيوب عبر موقع خارجي...")
    try:
        if action == "yt_audio":
            download_url, filetype = get_savefrom_link(url, audio=True)
        else:
            download_url, filetype = get_savefrom_link(url, audio=False)
        if not download_url:
            bot.edit_message_text("❌ لم أستطع جلب رابط التحميل من يوتيوب. جرب لاحقًا.", call.message.chat.id, msg.message_id)
            return
        filename = "temp_download." + filetype
        r = requests.get(download_url, stream=True)
        with open(filename, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        with open(filename, "rb") as f:
            if filetype == "mp3":
                bot.send_audio(call.message.chat.id, f, caption="✅ تم التحميل من يوتيوب (mp3)!")
            else:
                bot.send_video(call.message.chat.id, f, caption="✅ تم التحميل من يوتيوب (mp4)!")
        os.remove(filename)
        bot.delete_message(call.message.chat.id, msg.message_id)
    except Exception as e:
        bot.edit_message_text(f"❌ حدث خطأ أثناء التحميل:\n{e}", call.message.chat.id, msg.message_id)

# باقي دوال تيك توك/انستغرام/واي فاي كما هي في كودك...

@bot.message_handler(func=lambda m: m.text in ["منصة أخرى", "نفس المنصة"])
def next_action(message):
    if not check_access(message):
        return
    if message.text == "منصة أخرى":
        send_platforms(message.chat.id)
    elif message.text == "نفس المنصة":
        platform = user_platform.get(message.from_user.id, "المنصة")
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        markup.add("🔙 رجوع")
        bot.send_message(message.chat.id, f"📥 أرسل رابط الفيديو من {platform}:", reply_markup=markup)
        user_state[message.chat.id] = "waiting_link"

# ... باقي كود الواي فاي كما هو ...

@bot.message_handler(func=lambda m: True)
def fallback_handler(message):
    if not check_access(message):
        return
    show_main_menu(message.chat.id, msg_only=False)

# ----------------- Webhook Flask -----------------

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    else:
        return '', 403

@app.route('/')
def index():
    return "Webhook set!", 200

if __name__ == '__main__':
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
