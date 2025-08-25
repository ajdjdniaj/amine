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
    except Exception as e:
        print(f"تحذير: تعذر التحقق من عضوية المستخدم {user_id} في القناة: {e}")
        return True

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
    if message.text in ["يوتيوب", "انستغرام"]:
        bot.send_message(
            message.chat.id,
            "⚠️ هذه الخدمة في صيانة حاليًا. يرجى اختيار خدمة أخرى.",
        )
        send_platforms(message.chat.id)
        return
    # فقط تيك توك يعمل بشكل عادي
    user_state[message.chat.id] = "waiting_link"
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add("🔙 رجوع")
    bot.send_message(message.chat.id, f"📥 أرسل رابط الفيديو من {message.text}:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "🔙 رجوع")
def back_handler(message):
    if not check_access(message):
        return
    state = user_state.get(message.chat.id, "main_menu")
    if state == "waiting_link":
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

    # فقط تيك توك يعمل
    url = message.text.strip()
    if "tiktok" in url or "تيك توك" in url:
        caption = "🎬 اختر نوع التحميل:\n\n🎬 تحميل الفيديو (mp4)\n🎵 تحميل الصوت (mp3)"
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("🎬 تحميل الفيديو", callback_data=f"video|{url}"),
            types.InlineKeyboardButton("🎵 تحميل الصوت (mp3)", callback_data=f"audio|{url}")
        )
        try:
            with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
                info = ydl.extract_info(url, download=False)
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
        return

    # إذا حاول إرسال رابط لأي منصة أخرى (يوتيوب/انستغرام) أعد القائمة
    bot.send_message(
        message.chat.id,
        "⚠️ هذه الخدمة في صيانة حاليًا. يرجى اختيار خدمة أخرى.",
    )
    send_platforms(message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("video|") or call.data.startswith("audio|"))
def process_download(call):
    if not check_access(call):
        return

    # استخراج نوع التحميل والرابط من callback_data
    action, url = call.data.split("|", 1)

    # تحقق أن الرابط من تيك توك فقط
    if not ("tiktok" in url or "تيك توك" in url):
        bot.send_message(
            call.message.chat.id,
            "⚠️ هذه الخدمة في صيانة حاليًا. يرجى اختيار خدمة أخرى.",
        )
        send_platforms(call.message.chat.id)
        return

    msg = bot.send_message(call.message.chat.id, "⏳ جاري التحميل، انتظر قليلاً...")

    try:
        ydl_opts = {
            'outtmpl': '%(title)s.%(ext)s',
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
        with open(filename, "rb") as f:
            if action == "video":
                bot.send_video(call.message.chat.id, f, caption="✅ تم التحميل بنجاح! 🎬")
            else:
                bot.send_audio(call.message.chat.id, f, caption="✅ تم التحميل بنجاح! 🎵")
        os.remove(filename)
        bot.delete_message(call.message.chat.id, msg.message_id)
    except Exception as e:
        bot.edit_message_text(
            "❌ حدث خطأ أثناء التحميل، يرجى إعادة المحاولة.",
            call.message.chat.id, msg.message_id
        )

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add("منصة أخرى", "نفس المنصة", "🔙 رجوع")
    bot.send_message(
        call.message.chat.id,
        "💡 ماذا تريد أن تفعل الآن؟",
        reply_markup=markup
    )
    user_state[call.message.chat.id] = "waiting_link"

# باقي كود الواي فاي كما هو...

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
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    return "Webhook set!", 200

if __name__ == '__main__':
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
