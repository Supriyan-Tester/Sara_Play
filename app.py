import os
import json
import datetime
import threading
import requests
from flask import Flask, request, jsonify, Response, send_from_directory
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Update,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat
)

from models import Session, Video, Unlock, Channel, Setting

BOT_TOKEN = os.environ["BOT_TOKEN"]           # from @BotFather
BASE_URL = os.environ["BASE_URL"].rstrip("/")       # e.g. https://your-backend.onrender.com
ADMIN_ID = int(os.environ["ADMIN_ID"])              # your own Telegram numeric user id
# Channels to auto-post to, and the "Join Our Channels" hub link, are no longer
# env vars — manage them with /addchannel, /listchannels, /removechannel, and
# /sethub instead, so changing them doesn't need a redeploy. See get_setting/
# set_setting below.

# The mini app page is now served by this same Flask app (see /webapp route
# below), so it's always same-origin with the API — no separate frontend
# host, no CORS, no WEBAPP_URL to misconfigure. Override with an env var only
# if you deliberately want to host the page somewhere else again.
WEBAPP_URL = os.environ.get("WEBAPP_URL", f"{BASE_URL}/webapp").rstrip("/")

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__)

BOT_USERNAME = bot.get_me().username  # cached once at startup, used to build share links

# In-memory: tracks which admin is mid-way through adding a thumbnail for a
# video, or is expecting a forwarded message to register a channel. Fine for
# a single-admin workflow; resets on redeploy, but that's not a problem since
# you'd only be mid-flow for a few seconds at a time.
pending_thumbnail = {}   # admin_user_id -> video_id
pending_channel_add = set()  # admin_user_ids currently expecting a forward


def get_setting(key, default=None):
    session = Session()
    row = session.get(Setting, key)
    session.close()
    return row.value if row else default


def set_setting(key, value):
    session = Session()
    row = session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))
    session.commit()
    session.close()


AUTO_DELETE_SECONDS = 30 * 60  # 30 minutes


def schedule_delete(chat_id, message_id, delay_seconds=AUTO_DELETE_SECONDS):
    """
    Deletes a message (and, for a video message, the file with it) after
    delay_seconds. Runs in a background thread so it doesn't block the
    request that sent the message. Bots can always delete their own
    messages in a private chat, no admin rights needed.

    Note: this timer lives only in this process's memory — if the server
    restarts within the 30-minute window (a redeploy, a crash), that
    specific pending deletion is lost. Fine for this app's scale; if you
    need it to survive restarts, persist (chat_id, message_id, delete_at)
    to the database instead and sweep it with a periodic job.
    """
    def _delete():
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass  # message may already be gone (user deleted it, etc.)

    threading.Timer(delay_seconds, _delete).start()


def tutorial_button():
    """
    A 'Tutorial' button meant to sit next to every Watch Now / Watch Video
    button, in the bot and in channel posts alike. It's a plain URL deep
    link (not a web_app button) so it also works from inside channels,
    where web_app buttons aren't allowed. Tapping it opens the bot and
    triggers /start tutorial, handled in handle_start below.
    """
    return InlineKeyboardButton(
        "📖 Tutorial", url=f"https://t.me/{BOT_USERNAME}?start=tutorial"
    )


def send_tutorial(chat_id):
    """Sends the admin-uploaded tutorial video, or a friendly notice if none is set yet."""
    tutorial_file_id = get_setting("tutorial_video_file_id")
    if not tutorial_file_id:
        bot.send_message(chat_id, "The tutorial video hasn't been uploaded yet — check back soon!")
        return
    bot.send_video(chat_id, tutorial_file_id, caption="📖 How to use Sara Play")


def send_delivery_link(chat_id, video_id):
    """
    Sent once the ad is confirmed watched (from either delivery path).
    Instead of pushing the file straight into the chat, this sends an
    "Open" button pointing at a get<video_id> deep link. Tapping it reopens
    the bot and triggers handle_start, which does the actual file send —
    that's where /ad-complete's and handle_webapp_data's own re-verification
    against the Unlock row happens.
    """
    deep_link = f"https://t.me/{BOT_USERNAME}?start=get{video_id}"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📥 Open to get your video", url=deep_link), tutorial_button())
    bot.send_message(
        chat_id,
        "Your video is ready! Tap below to open the bot and receive it:",
        reply_markup=markup
    )


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

    # Tutorial deep link: from the "📖 Tutorial" button, works everywhere
    # (channels, the bot itself) since it's a plain t.me URL button.
    if video_id == "tutorial":
        send_tutorial(message.chat.id)
        return

    # Delivery link: "get<video_id>", sent to the user as the "Open" button
    # after they finish watching the ad(s). Only hands over the file if this
    # user actually has a watched-ad unlock for that video.
    if video_id and video_id.startswith("get") and video_id[3:].isdigit():
        real_id = int(video_id[3:])
        session = Session()
        unlock = session.query(Unlock).filter_by(
            user_id=message.from_user.id, video_id=real_id, ad_watched=True
        ).first()
        video = session.get(Video, real_id) if unlock else None
        session.close()

        if not video:
            bot.send_message(
                message.chat.id,
                "This link isn't valid — watch the ad again from the app to get a new one."
            )
            return

        # Add "Watch Video" button to let users browse other videos in the mini app
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(
            "Watch Video 😇",
            web_app=WebAppInfo(url=f"{WEBAPP_URL}/?user_id={message.from_user.id}")
        ), tutorial_button())
        sent = bot.send_video(
            message.chat.id,
            video.file_id,
            caption="Enjoy 🎬\n\n⏱ This message will auto-delete in 30 minutes — save it if you want to keep it.",
            reply_markup=markup
        )
        schedule_delete(sent.chat.id, sent.message_id)
        return

    if not video_id:
        # Plain /start: send the gallery entry point instead of a single video.
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(
            "Watch Video 😇",
            web_app=WebAppInfo(url=f"{WEBAPP_URL}/?user_id={message.from_user.id}")
        ), tutorial_button())
        hub_url = get_setting("hub_channel_url")
        if hub_url:
            markup.add(InlineKeyboardButton("🔗 Join Our Channels", url=hub_url))
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
        "Open Video 😇",
        web_app=WebAppInfo(
            url=f"{WEBAPP_URL}/?video_id={video_id}&user_id={message.from_user.id}"
        )
    ), tutorial_button())
    bot.send_message(
        message.chat.id,
        f"\"{video.title}\" is ready to view:",
        reply_markup=markup
    )


@bot.message_handler(content_types=["web_app_data"])
def handle_webapp_data(message):
    """
    Fires when the mini app calls tg.sendData(...) after the ad finishes.
    Now only marks the unlock; the delivery link is shown in the mini app itself.
    """
    data = json.loads(message.web_app_data.data)
    if data.get("action") != "ad_watched":
        return

    video_id = int(data["video_id"])
    session = Session()
    video = session.get(Video, video_id)
    if not video:
        session.close()
        return

    unlock = session.query(Unlock).filter_by(
        user_id=message.from_user.id, video_id=video_id
    ).first()

    # Both this client-side path and AdsGram's server-side Reward URL
    # callback (/ad-complete) can fire for the same unlock. Only mark once.
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

    # No longer send message to bot — link now shown in mini app instead


@bot.message_handler(commands=["tutorial"])
def handle_tutorial_command(message):
    """Anyone can call /tutorial directly to get the how-to video."""
    send_tutorial(message.chat.id)


@bot.message_handler(commands=["settutorial"])
def handle_settutorial(message):
    """
    Admin-only: reply to a video message with /settutorial to set (or replace)
    the tutorial video sent by the "📖 Tutorial" button and the /tutorial command.
    """
    if message.from_user.id != ADMIN_ID:
        return
    if not message.reply_to_message or not message.reply_to_message.video:
        bot.reply_to(message, "Reply to a video message with /settutorial to set the tutorial video.")
        return

    file_id = message.reply_to_message.video.file_id
    set_setting("tutorial_video_file_id", file_id)
    bot.reply_to(
        message,
        "✅ Tutorial video saved — it'll now be sent whenever someone taps the "
        "📖 Tutorial button or sends /tutorial."
    )


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


@bot.message_handler(commands=["listvideos"])
def handle_listvideos(message):
    """Admin-only: lists every video with its id, title/caption, and whether
    it has a thumbnail (videos without one don't show up in the gallery)."""
    if message.from_user.id != ADMIN_ID:
        return

    session = Session()
    videos = session.query(Video).order_by(Video.id).all()
    session.close()

    if not videos:
        bot.reply_to(message, "No videos added yet — use /addvideo to add one.")
        return

    lines = [f"📋 {len(videos)} video(s) total:\n"]
    for v in videos:
        status = "✅ in gallery" if v.thumbnail_file_id else "⚠️ no thumbnail — hidden from gallery"
        lines.append(f"#{v.id} — {v.title} ({status})")
        if v.caption and v.caption != v.title:
            lines.append(f"     caption: {v.caption}")

    text = "\n".join(lines)

    # Telegram caps messages at ~4096 chars — split into chunks if the list is long.
    chunk_size = 3500
    chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
    bot.reply_to(message, chunks[0])
    for chunk in chunks[1:]:
        bot.send_message(message.chat.id, chunk)


@bot.message_handler(commands=["deletevideo"])
def handle_deletevideo(message):
    """Admin-only: /deletevideo <id> — permanently removes a video from the
    database (and the gallery). See /listvideos for ids. Doesn't retract
    copies already posted in channels or already sent to users."""
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /deletevideo <id>  (see /listvideos for ids)")
        return

    video_id = int(parts[1])
    session = Session()
    video = session.get(Video, video_id)
    if not video:
        session.close()
        bot.reply_to(message, f"No video with id {video_id}.")
        return

    title = video.title
    session.delete(video)
    session.query(Unlock).filter_by(video_id=video_id).delete()  # clean up related unlock records too
    session.commit()
    session.close()

    bot.reply_to(
        message,
        f"🗑 Deleted video #{video_id} — \"{title}\".\n\n"
        f"Note: this only removes it from the bot's database and gallery — any "
        f"copies already posted in channels or already sent to users aren't retracted."
    )


@bot.message_handler(commands=["skipthumbnail"])
def handle_skip_thumbnail(message):
    """Admin-only: posts the pending video to the channel(s) without a thumbnail."""
    if message.from_user.id != ADMIN_ID:
        return
    video_id = pending_thumbnail.pop(message.from_user.id, None)
    if not video_id:
        return
    post_to_channel(video_id, thumbnail_file_id=None)
    bot.reply_to(message, "Posted to channel(s) without a thumbnail.")


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

    post_to_channel(video_id, thumbnail_file_id)
    bot.reply_to(message, "Thumbnail saved — this video will now show up in the gallery.")


def post_to_channel(video_id, thumbnail_file_id):
    """Sends the 'new video' announcement to every registered channel with a Watch Now button."""
    session = Session()
    video = session.get(Video, video_id)
    channels = session.query(Channel).all()
    session.close()
    if not video or not channels:
        return

    share_link = f"https://t.me/{BOT_USERNAME}?start={video_id}"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("Watch Now", url=share_link), tutorial_button())
    caption = video.caption or video.title

    for channel in channels:
        try:
            if thumbnail_file_id:
                bot.send_photo(channel.chat_id, thumbnail_file_id, caption=caption, reply_markup=markup)
            else:
                bot.send_message(channel.chat_id, caption, reply_markup=markup)
        except Exception as e:
            print(f"[WARN] failed to post to channel {channel.chat_id}: {e}")


@bot.message_handler(commands=["addchannel"])
def handle_addchannel(message):
    """Admin-only: starts the flow to register a new channel to auto-post to."""
    if message.from_user.id != ADMIN_ID:
        return
    pending_channel_add.add(message.from_user.id)
    bot.reply_to(
        message,
        "Forward any message from the channel you want to add "
        "(the bot must already be an admin there with post permission)."
    )


@bot.message_handler(
    func=lambda m: m.forward_from_chat is not None,
    content_types=["text", "photo", "video", "document", "audio", "voice", "sticker", "animation"]
)
def handle_forwarded_for_channel(message):
    """Completes /addchannel when the admin forwards a message from the target channel."""
    if message.from_user.id != ADMIN_ID or message.from_user.id not in pending_channel_add:
        return
    pending_channel_add.discard(message.from_user.id)

    chat = message.forward_from_chat
    if chat is None:
        bot.reply_to(message, "Couldn't read that channel — try forwarding again.")
        return

    session = Session()
    existing = session.query(Channel).filter_by(chat_id=str(chat.id)).first()
    if existing:
        session.close()
        bot.reply_to(message, f"'{chat.title}' is already registered.")
        return

    session.add(Channel(chat_id=str(chat.id), title=chat.title))
    session.commit()
    session.close()
    bot.reply_to(message, f"Added channel: {chat.title}")


@bot.message_handler(commands=["listchannels"])
def handle_listchannels(message):
    """Admin-only: shows registered post channels and the current hub link."""
    if message.from_user.id != ADMIN_ID:
        return
    session = Session()
    channels = session.query(Channel).all()
    session.close()

    hub_url = get_setting("hub_channel_url")
    lines = [f"{c.id}: {c.title or c.chat_id}" for c in channels] or ["No channels added yet."]
    lines.append(f"\nHub link (Join Our Channels button): {hub_url or '(not set — use /sethub)'}")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["removechannel"])
def handle_removechannel(message):
    """Admin-only: /removechannel <id> — id comes from /listchannels."""
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /removechannel <id>  (see /listchannels for ids)")
        return

    session = Session()
    channel = session.get(Channel, int(parts[1]))
    if channel:
        session.delete(channel)
        session.commit()
        bot.reply_to(message, "Removed.")
    else:
        bot.reply_to(message, "Channel id not found.")
    session.close()


@bot.message_handler(commands=["sethub"])
def handle_sethub(message):
    """Admin-only: /sethub <url> — sets the link behind the Join Our Channels button."""
    if message.from_user.id != ADMIN_ID:
        return
    url = message.text.replace("/sethub", "").strip()
    if not url.startswith("http"):
        bot.reply_to(message, "Usage: /sethub https://t.me/your_main_channel")
        return
    set_setting("hub_channel_url", url)
    bot.reply_to(message, f"Hub link set to: {url}")


@bot.message_handler(commands=["promote"])
def handle_promote(message):
    """
    Admin-only: /promote <video_id> [channel1] [channel2] ...
    Shares a video to your saved channels for marketing.
    
    Usage:
      /promote 5                    → Share to ALL saved channels
      /promote 5 @Channel1 @Ch2     → Share only to specified channels
    """
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ You don't have permission to use this command.")
        return
    
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /promote <video_id> [@channel1 @channel2 ...]")
        return
    
    try:
        video_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid video_id. Use: /promote <number>")
        return
    
    # Get video from database
    session = Session()
    video = session.get(Video, video_id)
    session.close()
    
    if not video:
        bot.reply_to(message, f"❌ Video {video_id} not found.")
        return
    
    if not video.file_id or not video.thumbnail_file_id:
        bot.reply_to(message, f"❌ Video {video_id} doesn't have a file or thumbnail.")
        return
    
    # Get target channels
    target_channels = parts[2:] if len(parts) > 2 else None
    
    # Get all saved channels from database
    session = Session()
    db_channels = session.query(Channel).all()
    session.close()
    
    if not db_channels:
        bot.reply_to(message, "❌ No channels saved yet. Use /addchannel first.")
        return
    
    # Filter channels to promote to
    if target_channels:
        # User specified specific channels
        promote_to = [ch for ch in db_channels if ch.username in target_channels or f"@{ch.username}" in target_channels]
        if not promote_to:
            bot.reply_to(message, f"❌ None of the specified channels were found in your saved channels.")
            return
    else:
        # Promote to all channels
        promote_to = db_channels
    
    # Share to each channel
    success_count = 0
    failed_channels = []
    
    for channel in promote_to:
        try:
            # Create watch button
            watch_link = f"https://t.me/{BOT_USERNAME}?start={video_id}"
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("👀 Watch Video", url=watch_link), tutorial_button())
            
            # Send video with title and button
            bot.send_video(
                channel.chat_id,
                video.file_id,
                caption=f"🎨 {video.title}\n\n[Open in Sara Play to watch]",
                reply_markup=markup,
                parse_mode="HTML"
            )
            success_count += 1
        except Exception as e:
            failed_channels.append(f"{channel.username}: {str(e)[:50]}")
    
    # Send summary
    summary = f"✅ Promoted video #{video_id} to {success_count}/{len(promote_to)} channels.\n"
    if failed_channels:
        summary += f"\n❌ Failed:\n" + "\n".join(failed_channels)
    
    bot.reply_to(message, summary)


# ---------- Mini app page (served directly, no separate frontend host) ----------

WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static_webapp")


@app.route("/webapp")
@app.route("/webapp/")
def webapp():
    return send_from_directory(WEBAPP_DIR, "index.html")


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


@app.route("/api/complete-ad", methods=["POST"])
def api_complete_ad():
    """
    Called by the mini app after ads complete. Marks the unlock as watched
    and returns the deep link so the mini app can display it and let the user
    open the bot to receive the video.
    """
    try:
        data = request.get_json()
        user_id = int(data.get("user_id"))
        video_id = int(data.get("video_id"))
    except (ValueError, TypeError):
        return jsonify({"error": "invalid user_id or video_id"}), 400

    session = Session()
    video = session.get(Video, video_id)
    if not video:
        session.close()
        return jsonify({"error": "video not found"}), 404

    unlock = session.query(Unlock).filter_by(
        user_id=user_id, video_id=video_id
    ).first()

    # If already watched, just return the link
    if unlock and unlock.ad_watched:
        session.close()
        delivery_link = f"https://t.me/{BOT_USERNAME}?start=get{video_id}"
        return jsonify({"delivery_link": delivery_link}), 200

    # Mark as watched for the first time
    if not unlock:
        unlock = Unlock(user_id=user_id, video_id=video_id)
        session.add(unlock)
    unlock.ad_watched = True
    unlock.unlocked_at = datetime.datetime.utcnow()
    session.commit()
    session.close()

    delivery_link = f"https://t.me/{BOT_USERNAME}?start=get{video_id}"
    return jsonify({"delivery_link": delivery_link}), 200


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

    video_id = unlock.video_id
    video = session.get(Video, video_id)
    session.close()

    if video:
        send_delivery_link(int(user_id), video_id)

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
    [
        BotCommand("start", "Browse drawing videos"),
        BotCommand("tutorial", "Watch the how-to tutorial"),
    ],
    scope=BotCommandScopeDefault()
)
bot.set_my_commands(
    [
        BotCommand("start", "Browse Videos"),
        BotCommand("tutorial", "Watch the how-to tutorial"),
        BotCommand("settutorial", "Reply to a video with this to set it as the tutorial"),
        BotCommand("addvideo", "Reply to a video: /addvideo Title | Caption"),
        BotCommand("listvideos", "List every video with its id, title, and caption"),
        BotCommand("deletevideo", "/deletevideo <id> — permanently remove a video"),
        BotCommand("skipthumbnail", "Post the pending /addvideo without a thumbnail"),
        BotCommand("addchannel", "Start registering a channel (then forward a msg from it)"),
        BotCommand("listchannels", "List registered channels + their ids, and the hub link"),
        BotCommand("removechannel", "/removechannel <id> — id comes from /listchannels"),
        BotCommand("sethub", "/sethub <url> — sets the 'Join Our Channels' button link"),
        BotCommand("promote", "/promote <video_id> [@ch1 @ch2] — resend a video to channels"),
    ],
    scope=BotCommandScopeChat(ADMIN_ID)
)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
