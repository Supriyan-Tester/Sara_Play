import os
import json
import datetime
import requests
from flask import Flask, request, jsonify, Response
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Update,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat
)

from models import Session, Video, Unlock

BOT_TOKEN = os.environ["BOT_TOKEN"]           # from @BotFather
WEBAPP_URL = os.environ["WEBAPP_URL"].rstrip("/")   # e.g. https://your-site.netlify.app
BASE_URL = os.environ["BASE_URL"].rstrip("/")       # e.g. https://your-backend.onrender.com
ADMIN_ID = int(os.environ["ADMIN_ID"])              # your own Telegram numeric user id
CHANNEL_ID = os.environ.get("CHANNEL_ID")           # e.g. "@your_channel" or "-1001234567890"
HUB_CHANNEL_URL = os.environ.get("HUB_CHANNEL_URL") # e.g. "https://t.me/your_main_channel"

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__)

BOT_USERNAME = bot.get_me().username  # cached once at startup, used to build share links

# In-memory: tracks which admin is mid-way through adding a thumbnail for a
# video. Fine for a single-admin workflow; resets on redeploy, but that's not
# a problem since you'd only be mid-flow for a few seconds at a time.
pending_thumbnail = {}  # admin_user_id -> video_id


# ---------- CORS (the Netlify mini app calls this backend from a different origin) ----------
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return response


# ---------- Bot handlers ----------

@bot.message_handler(commands=["start"])
def handle_start(message):
    print(f"[DEBUG] handle_start called. text={message.text!r} from={message.from_user.id}")
    args = message.text.split()
    video_id = args[1] if len(args) > 1 else None

    if not video_id:
        # Plain /start: send the gallery entry point instead of a single video.
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(
            "🎨 Browse Drawing Videos",
            web_app=WebAppInfo(url=f"{WEBAPP_URL}/?user_id={message.from_user.id}")
        ))
        if HUB_CHANNEL_URL:
            markup.add(InlineKeyboardButton("🔗 Join Our Channels", url=HUB_CHANNEL_URL))
        bot.send_message(
            message.chat.id,
            "Welcome! Tap below to browse drawing videos.",
            reply_markup=markup
        )
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

    # Deep link opens the gallery mini app directly on this video's detail view,
    # where the person can choose "Play Now" (ad) or "Share" themselves.
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(
        "🎨 Open Video",
        web_app=WebAppInfo(
            url=f"{WEBAPP_URL}/?video_id={video_id}&user_id={message.from_user.id}"
        )
    ))
    bot.send_message(
        message.chat.id,
        f"\"{video.title}\" is ready to view:",
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
    """
    Admin-only: reply to a video with:
        /addvideo Title | Optional caption for the channel post

    The part after "|" is used as the channel announcement caption; if
    omitted, the title is reused as the caption. After this, the bot asks
    for a thumbnail photo to complete the channel post (see handle_photo).
    A thumbnail is also required for the video to appear in the gallery.
    """
    if message.from_user.id != ADMIN_ID:
        return

    if not message.reply_to_message or not message.reply_to_message.video:
        bot.reply_to(message, "Reply to a video message with /addvideo Title | Caption")
        return

    raw = message.text.replace("/addvideo", "").strip()
    if "|" in raw:
        title, caption = raw.split("|", 1)
        title, caption = title.strip(), caption.strip()
    else:
        title = raw or "Untitled"
        caption = title

    file_id = message.reply_to_message.video.file_id

    session = Session()
    video = Video(title=title, file_id=file_id, caption=caption)
    session.add(video)
    session.commit()
    video_id = video.id
    session.close()

    share_link = f"https://t.me/{BOT_USERNAME}?start={video_id}"

    pending_thumbnail[message.from_user.id] = video_id
    bot.reply_to(
        message,
        f"Saved as video_id={video_id}\nShare link: {share_link}\n\n"
        f"Now send a thumbnail photo (required for it to show up in the gallery), "
        f"or send /skipthumbnail to skip the channel post (it still won't appear "
        f"in the gallery without a thumbnail)."
    )


@bot.message_handler(commands=["skipthumbnail"])
def handle_skip_thumbnail(message):
    """Admin-only: posts the pending video to the channel without a thumbnail."""
    if message.from_user.id != ADMIN_ID:
        return
    video_id = pending_thumbnail.pop(message.from_user.id, None)
    if not video_id or not CHANNEL_ID:
        return
    post_to_channel(video_id, thumbnail_file_id=None)
    bot.reply_to(message, "Posted to channel without a thumbnail.")


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    """
    Admin-only: if the admin is mid-way through /addvideo (waiting on a
    thumbnail), the next photo they send completes that channel post AND
    is what the gallery mini app displays for this video.
    """
    if message.from_user.id != ADMIN_ID:
        return

    video_id = pending_thumbnail.pop(message.from_user.id, None)
    if not video_id:
        return  # not expecting a thumbnail right now — ignore this photo

    thumbnail_file_id = message.photo[-1].file_id  # largest size
    session = Session()
    video = session.get(Video, video_id)
    if video:
        video.thumbnail_file_id = thumbnail_file_id
        session.commit()
    session.close()

    if CHANNEL_ID:
        post_to_channel(video_id, thumbnail_file_id)
    bot.reply_to(message, "Thumbnail saved — this video will now show up in the gallery.")


def post_to_channel(video_id, thumbnail_file_id):
    """Sends the 'new video' announcement to CHANNEL_ID with a Watch Now button."""
    session = Session()
    video = session.get(Video, video_id)
    session.close()
    if not video or not CHANNEL_ID:
        return

    share_link = f"https://t.me/{BOT_USERNAME}?start={video_id}"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("Watch Now", url=share_link))
    caption = video.caption or video.title

    if thumbnail_file_id:
        bot.send_photo(CHANNEL_ID, thumbnail_file_id, caption=caption, reply_markup=markup)
    else:
        bot.send_message(CHANNEL_ID, caption, reply_markup=markup)


# ---------- Gallery API (used by the mini app landing page) ----------

@app.route("/api/videos")
def api_videos():
    """Returns every video that has a thumbnail, newest first, for the gallery grid."""
    session = Session()
    rows = (
        session.query(Video)
        .filter(Video.thumbnail_file_id.isnot(None))
        .order_by(Video.id.desc())
        .all()
    )
    session.close()

    return jsonify([
        {
            "id": v.id,
            "title": v.title,
            "thumbnail_url": f"{BASE_URL}/api/thumbnail/{v.id}",
            "share_link": f"https://t.me/{BOT_USERNAME}?start={v.id}",
        }
        for v in rows
    ])


@app.route("/api/thumbnail/<int:video_id>")
def api_thumbnail(video_id):
    """
    Proxies a video's thumbnail from Telegram's file storage so the mini app
    can load it as a normal <img> URL, without ever exposing BOT_TOKEN to the
    browser.
    """
    session = Session()
    video = session.get(Video, video_id)
    session.close()

    if not video or not video.thumbnail_file_id:
        return "", 404

    file_info = bot.get_file(video.thumbnail_file_id)
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
    tg_response = requests.get(file_url, timeout=10)

    if tg_response.status_code != 200:
        return "", 502

    return Response(tg_response.content, mimetype="image/jpeg")


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
    raw = request.get_data(as_text=True)
    print(f"[DEBUG] webhook received raw body: {raw[:500]}")
    try:
        json_data = request.get_json()
        print(f"[DEBUG] parsed json: {json_data}")
        update = Update.de_json(json_data)
        print(f"[DEBUG] update.message: {update.message if update else 'update is None'}")
        bot.process_new_updates([update])
        print("[DEBUG] process_new_updates finished without raising")
    except Exception:
        import traceback
        traceback.print_exc()
    return "OK"


@app.route("/")
def index():
    return "Bot is running."


@app.route("/debug/unlocks")
def debug_unlocks():
    """Admin-only diagnostic: shows current Unlock rows so we can see DB state
    directly instead of guessing. Remove this before any real launch."""
    key = request.args.get("key")
    if key != BOT_TOKEN.split(":")[0]:  # cheap guard, not real auth
        return "forbidden", 403

    session = Session()
    rows = session.query(Unlock).order_by(Unlock.id.desc()).limit(20).all()
    session.close()

    return {
        "unlocks": [
            {
                "id": u.id,
                "user_id": u.user_id,
                "video_id": u.video_id,
                "ad_watched": u.ad_watched,
                "unlocked_at": str(u.unlocked_at),
            }
            for u in rows
        ]
    }


# Set the Telegram webhook at import time, so it runs whether the app is
# started via `python app.py` (dev) or `gunicorn app:app` (Render/production).
# Gunicorn imports this module rather than running it as __main__, so the
# webhook setup can't live inside an `if __name__ == "__main__":` guard.
bot.remove_webhook()
bot.set_webhook(url=f"{BASE_URL}/webhook/{BOT_TOKEN}")

# Command menu (the "Menu" button next to the message box) is scoped per chat:
# everyone sees just /start; only your own chat with the bot sees the admin
# commands. This runs at import time for the same reason webhook setup does.
bot.set_my_commands(
    [BotCommand("start", "Browse drawing videos")],
    scope=BotCommandScopeDefault()
)
bot.set_my_commands(
    [
        BotCommand("start", "Browse drawing videos"),
        BotCommand("addvideo", "Add a new video (reply to a video)"),
        BotCommand("skipthumbnail", "Post pending video without a thumbnail"),
    ],
    scope=BotCommandScopeChat(ADMIN_ID)
)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
