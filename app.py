import os
import json
import datetime
from flask import Flask, request
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Update
from models import Session, Video, Unlock

BOT_TOKEN = os.environ["BOT_TOKEN"]          # from @BotFather
WEBAPP_URL = os.environ["WEBAPP_URL"].rstrip("/")   # e.g. https://your-site.netlify.app
BASE_URL = os.environ["BASE_URL"].rstrip("/")       # e.g. https://your-backend.onrender.com
ADMIN_ID = int(os.environ["ADMIN_ID"])       # your own Telegram numeric user id

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)


# ---------- Bot handlers ----------

@bot.message_handler(commands=["start"])
def handle_start(message):
    args = message.text.split()
    video_id = args[1] if len(args) > 1 else None

    if not video_id:
        bot.send_message(message.chat.id, "Welcome! Open a video link to get started.")
        return

    session = Session()
    video = session.get(Video, int(video_id))
    session.close()

    if not video:
        bot.send_message(message.chat.id, "Video not found.")
        return

    # Record that this user is now waiting to unlock this video, BEFORE they
    # even open the mini app. AdsGram's Reward URL callback only gives us a
    # user ID (no video_id), so we need this pending record to know which
    # video to send when that callback arrives.
    session = Session()
    unlock = session.query(Unlock).filter_by(
        user_id=message.from_user.id, video_id=int(video_id)
    ).first()
    if not unlock:
        unlock = Unlock(user_id=message.from_user.id, video_id=int(video_id))
        session.add(unlock)
    unlock.ad_watched = False
    session.commit()
    session.close()

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(
        "Watch Ad to Unlock",
        web_app=WebAppInfo(
            url=f"{WEBAPP_URL}/?video_id={video_id}&user_id={message.from_user.id}"
        )
    ))
    bot.send_message(
        message.chat.id,
        f"\"{video.title}\" — watch a short ad to unlock it:",
        reply_markup=markup
    )


@bot.message_handler(content_types=["web_app_data"])
def handle_webapp_data(message):
    """Fires when the Netlify mini app calls tg.sendData(...) after the ad finishes."""
    data = json.loads(message.web_app_data.data)
    if data.get("action") != "ad_watched":
        return

    video_id = int(data["video_id"])
    session = Session()
    video = session.get(Video, video_id)

    if not video:
        bot.send_message(message.chat.id, "Video not found.")
        session.close()
        return

    unlock = session.query(Unlock).filter_by(
        user_id=message.from_user.id, video_id=video_id
    ).first()

    # Both this client-side path and AdsGram's server-side Reward URL
    # callback (/ad-complete) can fire for the same unlock. Only the first
    # one to arrive should actually send the video.
    if unlock and unlock.ad_watched:
        session.close()
        return

    if not unlock:
        unlock = Unlock(user_id=message.from_user.id, video_id=video_id)
        session.add(unlock)
    unlock.ad_watched = True
    unlock.unlocked_at = datetime.datetime.utcnow()
    session.commit()
    session.close()

    bot.send_video(message.chat.id, video.file_id, caption="Unlocked! Enjoy 🎬")


@bot.message_handler(commands=["addvideo"])
def handle_addvideo(message):
    """Admin-only: reply to a video with /addvideo <Title> to register it."""
    if message.from_user.id != ADMIN_ID:
        return
    if not message.reply_to_message or not message.reply_to_message.video:
        bot.reply_to(message, "Reply to a video message with /addvideo Title")
        return

    title = message.text.replace("/addvideo", "").strip() or "Untitled"
    file_id = message.reply_to_message.video.file_id

    session = Session()
    video = Video(title=title, file_id=file_id)
    session.add(video)
    session.commit()
    video_id = video.id
    session.close()

    bot_username = bot.get_me().username
    bot.reply_to(
        message,
        f"Saved as video_id={video_id}\n"
        f"Share link: https://t.me/{bot_username}?start={video_id}"
    )


# ---------- Backend endpoints ----------

@app.route("/ad-complete", methods=["GET"])
def ad_complete():
    """
    AdsGram's Reward URL callback. Configured in the AdsGram dashboard as:
        https://your-backend.onrender.com/ad-complete?userid=[userId]
    AdsGram replaces [userId] with the real Telegram user ID and sends a
    plain GET request — no video_id is included, so we look up the most
    recent video this user was waiting to unlock (see handle_start above)
    and deliver it here, server-verified.
    """
    user_id = request.args.get("userid")
    if not user_id:
        return "missing userid", 400

    session = Session()
    unlock = (
        session.query(Unlock)
        .filter_by(user_id=int(user_id), ad_watched=False)
        .order_by(Unlock.id.desc())
        .first()
    )
    if not unlock:
        session.close()
        return "no pending unlock", 404

    unlock.ad_watched = True
    unlock.unlocked_at = datetime.datetime.utcnow()
    session.commit()

    video = session.get(Video, unlock.video_id)
    session.close()

    if video:
        bot.send_video(int(user_id), video.file_id, caption="Unlocked! Enjoy 🎬")

    return "OK", 200


@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        update = Update.de_json(request.get_json())
        bot.process_new_updates([update])
    except Exception:
        # Print the full traceback to Render's logs instead of failing silently —
        # pyTelegramBotAPI can otherwise swallow handler exceptions quietly.
        import traceback
        traceback.print_exc()
    return "OK"


@app.route("/")
def index():
    return "Bot is running."


# Set the Telegram webhook at import time, so it runs whether the app is
# started via `python app.py` (dev) or `gunicorn app:app` (Render/production).
# Gunicorn imports this module rather than running it as __main__, so the
# webhook setup can't live inside an `if __name__ == "__main__":` guard.
bot.remove_webhook()
bot.set_webhook(url=f"{BASE_URL}/webhook/{BOT_TOKEN}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
