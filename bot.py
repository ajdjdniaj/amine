import os
import telebot
from telebot import types
import time
import yt_dlp

from PIL import Image
import pytesseract
import io
import re

from flask import Flask, request

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_USERNAME = "aie_tool_channel"  # بدون @
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # عدل الرابط حسب نطاق مشروعك في Render

BAN_FILE = "banned.txt"
BAN_DURATION = 24 * 60 * 60  # 24 ساعة بالثواني

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

user_links = {}
user_platform = {}
user_video_info = {}
user_state = {}

PLATFORMS = ["يوتيوب", "انستغرام", "تيك توك"]

# --- دوال الحظر والتحقق من القناة ---

def is_banned(user_id):
    now = int(time.time())
    try:
        with open(BAN_FILE, "r") as f:
            for line in f:
                uid, ban_until = line.strip().split(":")
                if str(user_id) == uid and now < int(ban_until):
                    return int(ban_until) - now  # كم باقي من الحظر
    except FileNotFoundError:
        pass
    return 0

def ban_user(user_id):
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

    # تحقق من تطابق الرابط مع المنصة المختارة
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

    caption = "🎬 اختر نوع التحميل:\n\n🎬 تحميل الفيديو (mp4)\n🎵 تحميل الصوت (mp3)"
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("🎬 تحميل الفيديو", callback_data="video"),
        types.InlineKeyboardButton("🎵 تحميل الصوت (mp3)", callback_data="audio")
    )
    # جلب معلومات الفيديو (لإظهار العنوان والصورة فقط)
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

@bot.callback_query_handler(func=lambda call: call.data in ("video", "audio"))
def process_download(call):
    if not check_access(call):
        return
    url = user_links.get(call.from_user.id)
    platform = user_platform.get(call.from_user.id, "المنصة")
    info = user_video_info.get(call.from_user.id)
    if not url:
        bot.answer_callback_query(call.id, "❌ لم يتم العثور على رابط، أرسل الرابط من جديد.")
        return

    action = call.data
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

# ----------- أداة اختراق WiFi fh -----------

def show_wifi_methods(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("✍️ كتابة اسم الراوتر", "🖼️ صورة لجميع الراوترات", "🔙 رجوع")
    bot.send_message(
        chat_id,
        "📡 اختر طريقة إدخال اسم الراوتر:\n"
        "✍️ كتابة اسم الراوتر يدويًا (fh_...)\n"
        "🖼️ أو أرسل صورة لقائمة الشبكات.",
        reply_markup=markup
    )
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

@bot.message_handler(func=lambda m: m.text == "🔁 اختراق WiFi آخر")
def another_wifi(message):
    if not check_access(message):
        return
    show_wifi_methods(message.chat.id)

def extract_ssids_from_text(text):
    return re.findall(r'(fh_[a-zA-Z0-9]{6,7})', text)

def smart_correct_ssid(ssid):
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

    def try_extract(image):
        texts = []
        texts.append(pytesseract.image_to_string(image, lang='eng'))
        img2 = image.convert('L').point(lambda x: 0 if x < 140 else 255, '1')
        texts.append(pytesseract.image_to_string(img2, lang='eng'))
        return texts

    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    image = Image.open(io.BytesIO(downloaded_file))

    max_width = 800
    if image.width > max_width:
        ratio = max_width / image.width
        new_size = (int(image.width * ratio), int(image.height * ratio))
        image = image.resize(new_size)

    texts = try_extract(image)

    all_ssids = []
    seen = set()
    for text in texts:
        found = re.findall(r'(fh_[a-zA-Z0-9]{6,7})', text)
        for ssid in found:
            ssid_corrected = smart_correct_ssid(ssid)
            hex_part = ssid_corrected[3:]
            if ssid_corrected not in seen and all(c in '0123456789abcdef' for c in hex_part.lower()):
                seen.add(ssid_corrected)
                all_ssids.append(ssid_corrected)

    bot.delete_message(message.chat.id, wait_msg.message_id)

    if not all_ssids:
        bot.send_message(
            message.chat.id,
            "❌ لم يتم العثور على أي شبكة تبدأ بـ fh_ في الصورة.\n"
            "يرجى التأكد من وضوح الصورة أو إرسال لقطة شاشة مباشرة من الجهاز."
        )
        return

    reply = ""
    for ssid in all_ssids:
        password = generate_wifi_password(ssid)
        reply += f"📶 <b>{ssid}</b>\n🔑 <code>{password}</code>\n\n"
    reply += "📋 يمكنك نسخ كلمة السر بالضغط عليها."

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔁 اختراق WiFi آخر", "🔙 رجوع")
    bot.send_message(message.chat.id, reply, parse_mode="HTML", reply_markup=markup)

def generate_wifi_password(ssid):
    ssid = ssid.strip().lower()
    if not ssid.startswith("fh_"):
        return None
    hex_part = ssid[3:]
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
    ssid = message.text.strip().lower()
    if not ssid.startswith("fh_"):
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("🔁 اختراق WiFi آخر", "🔙 رجوع")
        bot.send_message(message.chat.id, "❌ لم يتم التعرف على الشبكة. أعد المحاولة.", reply_markup=markup)
        return

    hex_part = ssid[3:]
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
        bot.send_message(
            message.chat.id,
            f"✅ تم توليد كلمة السر الخاصة بالشبكة:\n\n"
            f"🔑 <b>كلمة السر:</b>\n"
            f"<code>{password}</code>\n\n"
            f"📋 يمكنك نسخ كلمة السر بالضغط عليها.",
            parse_mode="HTML",
            reply_markup=markup
        )
    except Exception as e:
        bot.send_message(message.chat.id, "❌ حصل خطأ أثناء توليد كلمة السر.")

@bot.message_handler(func=lambda m: True)
def fallback_handler(message):
    if not check_access(message):
        return
    show_main_menu(message.chat.id, msg_only=True)

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
