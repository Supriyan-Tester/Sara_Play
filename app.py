import os
import json
import time
import datetime
import threading
import requests
from flask import Flask, request, jsonify, Response, send_from_directory
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Update,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat
)

from models import Session, Video, VideoFile, Unlock, Channel, Setting, ScheduledDeletion

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

# Single source of truth for both the "Menu" button (set_my_commands, near the
# bottom of this file) and the /help command below — so the two can never
# show different descriptions for the same command.
PUBLIC_COMMANDS = [
    BotCommand("start", "Browse Saraa videos"),
    BotCommand("tutorial", "Watch the how-to tutorial"),
    BotCommand("help", "List all commands and what they do"),
]
ADMIN_COMMANDS = [
    BotCommand("start", "Browse Videos"),
    BotCommand("tutorial", "Watch the how-to tutorial"),
    BotCommand("help", "List all commands and what they do"),
    BotCommand("settutorial", "Reply to a video with this to set it as the tutorial"),
    BotCommand("addvideo", "/addvideo Title | Caption, send any files, then /donevideos (saved as one item)"),
    BotCommand("donevideos", "Finish an /addvideo batch and save it as one item"),
    BotCommand("listvideos", "List every video with its id, title, and caption"),
    BotCommand("deletevideo", "/deletevideo <id> — permanently remove a video"),
    BotCommand("skipthumbnail", "Post the pending /addvideo without a thumbnail"),
    BotCommand("addchannel", "Start registering a channel (then forward a msg from it)"),
    BotCommand("setforcejoin", "Require users to join a channel (forward a msg from it)"),
    BotCommand("clearforcejoin", "Turn off the force-join requirement"),
    BotCommand("listchannels", "List registered channels + their ids, and the hub link"),
    BotCommand("removechannel", "/removechannel <id> — id comes from /listchannels"),
    BotCommand("sethub", "/sethub <url> — sets the 'Join Our Channels' button link"),
    BotCommand("promote", "/promote <video_id> [@ch1 @ch2] — resend a video to channels"),
]

# In-memory: tracks which admin is mid-way through adding a thumbnail for one
# or more videos, is still collecting videos for a batch (/addvideo ... ->
# /donevideos), or is expecting a forwarded message to register a channel.
# Fine for a single-admin workflow; resets on redeploy, but that's not a
# problem since you'd only be mid-flow for a few minutes at a time.
pending_thumbnail = {}   # admin_user_id -> list of video_ids sharing the next thumbnail
pending_batch = {}       # admin_user_id -> {"title", "caption", "files": [(file_id, file_type), ...]}
pending_channel_add = set()  # admin_user_ids currently expecting a forward
pending_force_join_add = set()  # admin_user_ids currently expecting a forward, for /setforcejoin

# video_id -> (image_bytes, mimetype). Thumbnails almost never change once
# set, but api_thumbnail was re-fetching from Telegram (2 network calls) on
# every single gallery load with no caching at all — this is what was
# actually causing slow image loads. Cleared for a video_id whenever
# handle_photo sets a new thumbnail for it.
_thumbnail_cache = {}




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
DELETION_SWEEP_INTERVAL_SECONDS = 60  # how often the background sweep checks for due deletions


def schedule_delete(chat_id, message_id, delay_seconds=AUTO_DELETE_SECONDS):
    """
    Queues a message (and, for a video message, the file with it) for
    deletion delay_seconds from now. Bots can always delete their own
    messages in a private chat, no admin rights needed.

    Persisted to the database (rather than an in-memory timer) specifically
    so a redeploy, crash, or restart doesn't lose track of it — the
    run_deletion_sweep() background loop below is what actually performs
    the delete once delete_at has passed, whether or not this is the same
    process that originally scheduled it.
    """
    session = Session()
    session.add(ScheduledDeletion(
        chat_id=chat_id,
        message_id=message_id,
        delete_at=datetime.datetime.utcnow() + datetime.timedelta(seconds=delay_seconds)
    ))
    session.commit()
    session.close()


def run_deletion_sweep():
    """
    Runs forever in a background thread, started once at the bottom of this
    file. Every DELETION_SWEEP_INTERVAL_SECONDS, it looks for every queued
    deletion whose time has come — including ones that became due while the
    app was down, e.g. during a redeploy — deletes those messages, and
    clears their rows. This is what makes auto-delete survive restarts:
    nothing here depends on this specific process having been the one
    running when schedule_delete() was originally called.
    """
    while True:
        try:
            session = Session()
            due = session.query(ScheduledDeletion).filter(
                ScheduledDeletion.delete_at <= datetime.datetime.utcnow()
            ).all()
            for row in due:
                try:
                    bot.delete_message(row.chat_id, row.message_id)
                except Exception:
                    pass  # message may already be gone (user deleted it, etc.)
                session.delete(row)
            if due:
                session.commit()
            session.close()
        except Exception as e:
            print(f"[WARN] deletion sweep failed: {e}")
        time.sleep(DELETION_SWEEP_INTERVAL_SECONDS)


def play_button(**kwargs):
    """
    The green "Play Video" button used everywhere the bot offers a video.
    style="success" makes it green on Telegram apps updated after Feb 9, 2026;
    older apps simply show it unstyled. If the installed pyTelegramBotAPI is
    too old to know the style option, fall back to a plain button rather than
    crashing every message that includes one.
    """
    try:
        return InlineKeyboardButton("Play Video", style="success", **kwargs)
    except TypeError:
        return InlineKeyboardButton("Play Video", **kwargs)


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


def extract_file(message):
    """
    Pulls the (file_id, file_type) out of any message that carries a file —
    video, photo, document (zips, pdfs, anything sent "as a file"), audio,
    voice note, or GIF/animation. Returns None if the message has none.
    Order matters: an animation also has a .document attached, so it's
    checked before document.
    """
    if message.video:
        return message.video.file_id, "video"
    if message.animation:
        return message.animation.file_id, "animation"
    if message.photo:
        return message.photo[-1].file_id, "photo"  # largest size
    if message.audio:
        return message.audio.file_id, "audio"
    if message.voice:
        return message.voice.file_id, "voice"
    if message.document:
        return message.document.file_id, "document"
    return None


def send_stored_file(chat_id, file_id, file_type, **kwargs):
    """Sends one stored file using the right Telegram method for its type."""
    senders = {
        "video": bot.send_video,
        "photo": bot.send_photo,
        "document": bot.send_document,
        "audio": bot.send_audio,
        "voice": bot.send_voice,
        "animation": bot.send_animation,
    }
    send = senders.get(file_type or "video", bot.send_video)
    try:
        return send(chat_id, file_id, **kwargs)
    except telebot.apihelper.ApiTelegramException as e:
        if e.error_code == 429:  # sending several files quickly can hit Telegram's flood limit
            time.sleep(int(e.result_json.get("parameters", {}).get("retry_after", 2)) + 1)
            return send(chat_id, file_id, **kwargs)
        raise


def entry_files(video):
    """Every (file_id, file_type) in a gallery entry, in delivery order:
    its first file, then any extras from VideoFile."""
    files = [(video.file_id, video.file_type or "video")]
    session = Session()
    extras = session.query(VideoFile).filter_by(video_id=video.id).order_by(VideoFile.position).all()
    session.close()
    files.extend((f.file_id, f.file_type or "video") for f in extras)
    return files


def deliver_video(chat_id, video):
    """
    Sends EVERY file in this entry straight into chat_id, one after another,
    in the order they were added. The "Play Video" + Tutorial buttons and the
    30-minute auto-delete notice ride on the last file, so they sit at the
    bottom of the batch. Every file sent is queued for auto-delete.

    Called from process_start's get<video_id> branch, i.e. once the person
    has watched the ads and tapped the "Open" button — one ad unlock releases
    the whole entry. Runs in a background thread there (see process_start),
    since sending many files takes a while and Telegram re-sends a webhook
    update that isn't answered quickly, which would deliver everything twice.

    Returns True if at least one file was sent, False if none could be.
    """
    files = entry_files(video)
    markup = InlineKeyboardMarkup()
    markup.add(play_button(
        web_app=WebAppInfo(url=f"{WEBAPP_URL}/?user_id={chat_id}")
    ), tutorial_button())
    notice = "Enjoy 🎬\n\n⏱ This message will auto-delete in 30 minutes — save it if you want to keep it."

    sent_any = False
    for i, (file_id, file_type) in enumerate(files):
        is_last = i == len(files) - 1
        try:
            extra = {"caption": notice, "reply_markup": markup} if is_last else {}
            sent = send_stored_file(chat_id, file_id, file_type, **extra)
            schedule_delete(sent.chat.id, sent.message_id)
            sent_any = True
        except Exception as e:
            print(f"[WARN] deliver_video: file {i + 1}/{len(files)} of video {video.id} "
                  f"failed for chat_id={chat_id}: {e}")
        if not is_last:
            time.sleep(0.4)  # stay under Telegram's per-chat send rate
    return sent_any


# ---------- CORS (the Netlify mini app calls this backend from a different origin) ----------

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return response


# ---------- Bot handlers ----------

def user_has_joined_required_channel(user_id):
    """
    True if no force-join channel is configured, or this user currently
    belongs to it (checked live against Telegram — nothing about membership
    is stored in our own database). Fails open (returns True) if the check
    itself errors out, e.g. the bot lost admin rights there or the channel
    was deleted, so a misconfiguration can't accidentally lock everyone out.
    """
    channel_id = get_setting("force_join_channel_id")
    if not channel_id:
        return True

    try:
        member = bot.get_chat_member(int(channel_id), user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        print(f"[WARN] force-join membership check failed, failing open: {e}")
        return True


def send_join_gate(chat_id, payload):
    """
    Shown instead of the normal /start response when force-join is on and
    this user hasn't joined yet. payload carries whatever they were
    originally trying to reach (None, a video_id, "get<id>", or "tutorial")
    so handle_join_check can continue right where they left off once they
    confirm they've joined.
    """
    channel_url = get_setting("force_join_channel_url")
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("➡️ Join Channel", url=channel_url))
    markup.add(InlineKeyboardButton("✅ I've Joined", callback_data=f"joincheck:{payload or ''}"))
    bot.send_message(
        chat_id,
        "🔒 Please join our channel first to use this bot.\n\n"
        "Tap below to join, then tap \"I've Joined\" to continue.",
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("joincheck:"))
def handle_join_check(call):
    payload = call.data[len("joincheck:"):] or None

    if not user_has_joined_required_channel(call.from_user.id):
        bot.answer_callback_query(
            call.id,
            "You haven't joined yet — join first, then tap this again.",
            show_alert=True
        )
        return

    bot.answer_callback_query(call.id, "✅ Verified!")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass  # not critical if the gate message can't be cleaned up
    process_start(call.message.chat.id, call.from_user.id, payload)


@bot.message_handler(commands=["start"])
def handle_start(message):
    args = message.text.split()
    payload = args[1] if len(args) > 1 else None

    if not user_has_joined_required_channel(message.from_user.id):
        send_join_gate(message.chat.id, payload)
        return

    process_start(message.chat.id, message.from_user.id, payload)


def process_start(chat_id, user_id, video_id):
    """
    The actual /start dispatch logic, separated from handle_start so both
    a normal /start and handle_join_check (continuing after someone
    confirms they've joined) can run it the same way.
    """
    # Tutorial deep link: from the "📖 Tutorial" button, works everywhere
    # (channels, the bot itself) since it's a plain t.me URL button.
    if video_id == "tutorial":
        send_tutorial(chat_id)
        return

    # Delivery link: "get<video_id>", sent to the user as the "Open" button
    # after they finish watching the ad(s). Only hands over the file if this
    # user actually has a watched-ad unlock for that video.
    if video_id and video_id.startswith("get") and video_id[3:].isdigit():
        real_id = int(video_id[3:])
        session = Session()
        unlock = session.query(Unlock).filter_by(
            user_id=user_id, video_id=real_id, ad_watched=True
        ).first()
        video = session.get(Video, real_id) if unlock else None
        session.close()

        if not video:
            bot.send_message(
                chat_id,
                "This link isn't valid — watch the ad again from the app to get a new one."
            )
            return

        threading.Thread(target=deliver_video, args=(chat_id, video), daemon=True).start()
        return

    if not video_id:
        # Plain /start: send the gallery entry point instead of a single video.
        markup = InlineKeyboardMarkup()
        markup.add(play_button(
            web_app=WebAppInfo(url=f"{WEBAPP_URL}/?user_id={user_id}")
        ), tutorial_button())
        hub_url = get_setting("hub_channel_url")
        if hub_url:
            markup.add(InlineKeyboardButton("🔗 Join Our Channels", url=hub_url))
        bot.send_message(
            chat_id,
            "Welcome! Tap below to browse Saraa videos.",
            reply_markup=markup
        )
        return

    session = Session()
    video = session.get(Video, int(video_id))
    session.close()

    if not video:
        bot.send_message(chat_id, "Video not found.")
        return

    # By design, watching the ad(s) unlocks a video for one delivery only —
    # reopening this same share link resets that unlock, so the person has
    # to watch the ad(s) again to get the file again. This is what makes
    # every re-download an ad impression rather than a one-time unlock.
    session = Session()
    unlock = session.query(Unlock).filter_by(
        user_id=user_id, video_id=int(video_id)
    ).first()
    if not unlock:
        unlock = Unlock(user_id=user_id, video_id=int(video_id))
        session.add(unlock)
    unlock.ad_watched = False
    session.commit()
    session.close()

    # Deep link opens the gallery mini app directly on this video's detail view,
    # where the person can choose "Play Now" (ad) or "Share" themselves.
    markup = InlineKeyboardMarkup()
    markup.add(play_button(
        web_app=WebAppInfo(
            url=f"{WEBAPP_URL}/?video_id={video_id}&user_id={user_id}"
        )
    ), tutorial_button())
    bot.send_message(
        chat_id,
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

    # Guards against this firing twice for the same unlock (e.g. a duplicate
    # webhook delivery from Telegram). Only mark it watched once.
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


@bot.message_handler(commands=["help"])
def handle_help(message):
    """
    Lists every available command with its description. Shows just the
    public commands to everyone; shows the full admin list too if the
    sender is the admin. Built from PUBLIC_COMMANDS/ADMIN_COMMANDS above,
    so it always matches whatever's in the Menu button.
    """
    is_admin = message.from_user.id == ADMIN_ID
    commands = ADMIN_COMMANDS if is_admin else PUBLIC_COMMANDS

    lines = ["📋 Available commands:\n"]
    for cmd in commands:
        lines.append(f"/{cmd.command} — {cmd.description}")

    bot.reply_to(message, "\n".join(lines))


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
    Admin-only: /addvideo Title | Optional caption for the channel post

    Starts a batch: send as many files as you want next, one at a time (see
    handle_batch_file below), then /donevideos. "Files" means anything
    Telegram can hold — videos, images, zips/documents, audio, voice notes,
    GIFs — and a single batch can mix types. Everything sent becomes ONE
    gallery entry (one id, one thumbnail, one ad-unlock); whoever completes
    the ads receives all of its files together. A single file works the same
    way: send it, then /donevideos right away.

    You can also reply to a file with this command — that file is simply
    counted as the first one in the batch.
    """
    if message.from_user.id != ADMIN_ID:
        return

    raw = message.text.replace("/addvideo", "").strip()
    if "|" in raw:
        title, caption = raw.split("|", 1)
        title, caption = title.strip(), caption.strip()
    else:
        title = raw or "Untitled"
        caption = title

    files = []
    if message.reply_to_message:
        found = extract_file(message.reply_to_message)
        if found:
            files.append(found)

    pending_batch[message.from_user.id] = {
        "title": title,
        "caption": caption,
        "files": files,
    }

    status = "Got 1 file so far (from your reply)." if files else "No files received yet."
    bot.reply_to(
        message,
        f"Starting \"{title}\" — send as many files as you want (videos, images, "
        f"zips, audio...), one at a time. They'll all be saved together as ONE item. "
        f"{status}\n\n"
        f"When you're done, send /donevideos."
    )


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
    func=lambda m: m.forward_from_chat is not None and m.from_user.id in pending_channel_add,
    content_types=["text", "photo", "video", "document", "audio", "voice", "sticker", "animation"]
)
def handle_forwarded_for_channel(message):
    """
    Completes /addchannel when the admin forwards a message from the target
    channel. Registered — and its func filter checked — before
    handle_batch_file and handle_photo below specifically so a forwarded
    channel post that happens to be a photo or video doesn't get silently
    swallowed by those instead (pyTelegramBotAPI only runs the first handler
    whose filters match a given message). Requiring pending_channel_add in
    the filter itself means this still correctly falls through to those
    other handlers whenever this admin isn't actually mid-/addchannel.
    """
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


@bot.message_handler(commands=["setforcejoin"])
def handle_setforcejoin(message):
    """Admin-only: starts the flow to require users to join a specific
    channel before they can use the bot at all."""
    if message.from_user.id != ADMIN_ID:
        return
    pending_force_join_add.add(message.from_user.id)
    bot.reply_to(
        message,
        "Forward any message from the channel you want to require users to join "
        "(the bot must already be an admin there)."
    )


@bot.message_handler(commands=["clearforcejoin"])
def handle_clearforcejoin(message):
    """Admin-only: turns force-join back off — anyone can use the bot again."""
    if message.from_user.id != ADMIN_ID:
        return
    set_setting("force_join_channel_id", "")
    set_setting("force_join_channel_url", "")
    bot.reply_to(message, "Force-join requirement removed — anyone can use the bot again.")


@bot.message_handler(
    func=lambda m: m.forward_from_chat is not None and m.from_user.id in pending_force_join_add,
    content_types=["text", "photo", "video", "document", "audio", "voice", "sticker", "animation"]
)
def handle_forwarded_for_force_join(message):
    """
    Completes /setforcejoin when the admin forwards a message from the
    target channel. Registered — and its func filter checked — before
    handle_batch_file and handle_photo below for the same reason as
    handle_forwarded_for_channel above: so a forwarded post that happens to
    be a photo/video doesn't get silently swallowed by those instead.
    """
    pending_force_join_add.discard(message.from_user.id)

    chat = message.forward_from_chat
    if chat is None:
        bot.reply_to(message, "Couldn't read that channel — try forwarding again.")
        return

    if chat.username:
        channel_url = f"https://t.me/{chat.username}"
    else:
        try:
            channel_url = bot.export_chat_invite_link(chat.id)
        except Exception:
            bot.reply_to(
                message,
                "This is a private channel and I couldn't create an invite link — "
                "make sure I'm an admin there with 'Invite Users' permission, then try again."
            )
            return

    set_setting("force_join_channel_id", str(chat.id))
    set_setting("force_join_channel_url", channel_url)
    bot.reply_to(
        message,
        f"✅ Users must now join \"{chat.title}\" before using the bot.\nLink: {channel_url}"
    )


@bot.message_handler(
    func=lambda m: m.from_user.id in pending_batch,
    content_types=["video", "photo", "document", "audio", "voice", "animation"]
)
def handle_batch_file(message):
    """
    Admin-only (only the admin can ever be in pending_batch): while an
    /addvideo batch is open, each file sent (not as a reply) gets appended
    to that batch. The pending_batch check lives in the func filter rather
    than inside the function on purpose — photos are also how thumbnails
    arrive (handle_photo, below), and pyTelegramBotAPI runs only the first
    handler that matches, so when no batch is open this must NOT match, or
    it would swallow thumbnail photos.
    """
    batch = pending_batch[message.from_user.id]
    found = extract_file(message)
    if not found:
        return

    batch["files"].append(found)
    kind = found[1]
    bot.reply_to(
        message,
        f"✅ Got file {len(batch['files'])} ({kind}). "
        f"Send another, or /donevideos when finished."
    )


@bot.message_handler(commands=["donevideos"])
def handle_donevideos(message):
    """Admin-only: closes the current /addvideo batch and saves EVERYTHING
    collected as ONE gallery entry — one id, one thumbnail, one ad-unlock —
    holding all the files. The first file lives on the Video row itself and
    the rest in VideoFile, in the order they were sent. Then asks for the
    thumbnail photo."""
    if message.from_user.id != ADMIN_ID:
        return

    batch = pending_batch.pop(message.from_user.id, None)
    if not batch or not batch["files"]:
        bot.reply_to(message, "Nothing in progress — start with /addvideo Title | Caption.")
        return

    title, caption, files = batch["title"], batch["caption"], batch["files"]
    first_id, first_type = files[0]

    session = Session()
    video = Video(title=title, file_id=first_id, file_type=first_type, caption=caption)
    session.add(video)
    session.flush()  # assigns video.id without committing yet
    for position, (file_id, file_type) in enumerate(files[1:], start=1):
        session.add(VideoFile(video_id=video.id, file_id=file_id, file_type=file_type, position=position))
    session.commit()
    video_id = video.id
    session.close()

    pending_thumbnail[message.from_user.id] = [video_id]

    count = len(files)
    kinds = ", ".join(t for _, t in files)
    bot.reply_to(
        message,
        f"Saved \"{title}\" as ONE item (id #{video_id}) with {count} file{'s' if count != 1 else ''}: {kinds}\n"
        f"Link: https://t.me/{BOT_USERNAME}?start={video_id}\n\n"
        f"Whoever watches the ads gets all {count} together. Now send ONE thumbnail photo "
        f"(required for it to show up in the gallery), or send /skipthumbnail to skip the "
        f"channel post (it still won't appear in the gallery without a thumbnail)."
    )


@bot.message_handler(commands=["listvideos"])
def handle_listvideos(message):
    """Admin-only: lists every video with its id, title/caption, and whether
    it has a thumbnail (videos without one don't show up in the gallery)."""
    if message.from_user.id != ADMIN_ID:
        return

    session = Session()
    videos = session.query(Video).order_by(Video.id).all()
    extras = {}  # video_id -> list of extra file types
    for f in session.query(VideoFile).order_by(VideoFile.position).all():
        extras.setdefault(f.video_id, []).append(f.file_type or "video")
    session.close()

    if not videos:
        bot.reply_to(message, "No videos added yet — use /addvideo to add one.")
        return

    lines = [f"📋 {len(videos)} item(s) total:\n"]
    for v in videos:
        status = "✅ in gallery" if v.thumbnail_file_id else "⚠️ no thumbnail — hidden from gallery"
        kinds = [v.file_type or "video"] + extras.get(v.id, [])
        contents = kinds[0] if len(kinds) == 1 else f"{len(kinds)} files: {', '.join(kinds)}"
        lines.append(f"#{v.id} — {v.title} [{contents}] ({status})")
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
    session.query(VideoFile).filter_by(video_id=video_id).delete()  # its extra files
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
    """Admin-only: posts every pending video to the channel(s) without a thumbnail."""
    if message.from_user.id != ADMIN_ID:
        return
    video_ids = pending_thumbnail.pop(message.from_user.id, None)
    if not video_ids:
        return
    for video_id in video_ids:
        post_to_channel(video_id, thumbnail_file_id=None)
    count = len(video_ids)
    bot.reply_to(message, f"Posted {count} video{'s' if count != 1 else ''} to channel(s) without a thumbnail.")


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    """
    Admin-only: if the admin is mid-way through /addvideo -> /donevideos
    (waiting on a thumbnail), the next photo they send is applied to every
    video in that batch — completing each one's channel post AND becoming
    what the gallery mini app displays for all of them.
    """
    if message.from_user.id != ADMIN_ID:
        return

    video_ids = pending_thumbnail.pop(message.from_user.id, None)
    if not video_ids:
        return  # not expecting a thumbnail right now — ignore this photo

    thumbnail_file_id = message.photo[-1].file_id  # largest size
    session = Session()
    for video_id in video_ids:
        video = session.get(Video, video_id)
        if video:
            video.thumbnail_file_id = thumbnail_file_id
        _thumbnail_cache.pop(video_id, None)  # in case this video already had a cached (old) thumbnail
    session.commit()
    session.close()

    for video_id in video_ids:
        post_to_channel(video_id, thumbnail_file_id)

    count = len(video_ids)
    bot.reply_to(
        message,
        f"Thumbnail saved for {count} video{'s' if count != 1 else ''} — "
        f"{'they' if count != 1 else 'it'} will now show up in the gallery."
    )


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
            markup.add(play_button(url=watch_link), tutorial_button())
            
            # Post the thumbnail with the title and button — same as the
            # automatic channel post. Never the files themselves: an entry can
            # now hold many files, and they're only meant to be released
            # after the ads.
            bot.send_photo(
                channel.chat_id,
                video.thumbnail_file_id,
                caption=f"🎨 {video.title}\n\n[Open in Sara Play to watch]",
                reply_markup=markup
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
    browser. Cached in memory after the first fetch — this used to hit
    Telegram's API twice (resolve file path, then download the bytes) on
    every single gallery load, which was the actual cause of slow image
    loading. The Cache-Control header also lets the browser itself skip
    re-requesting it at all on repeat visits.
    """
    if video_id in _thumbnail_cache:
        content, mimetype = _thumbnail_cache[video_id]
        return Response(content, mimetype=mimetype, headers={"Cache-Control": "public, max-age=86400"})

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

    _thumbnail_cache[video_id] = (tg_response.content, "image/jpeg")
    return Response(
        tg_response.content, mimetype="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"}
    )


@app.route("/api/complete-ad", methods=["POST"])
def api_complete_ad():
    """
    Called by the mini app after ads complete. Marks the unlock as watched
    and returns the deep link so the mini app can display it and let the
    user open the bot to receive the video (delivered by handle_start's
    get<video_id> branch, via deliver_video, once they tap it).
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

    delivery_link = f"https://t.me/{BOT_USERNAME}?start=get{video_id}"

    # If already watched, just return the link
    if unlock and unlock.ad_watched:
        session.close()
        return jsonify({"delivery_link": delivery_link}), 200

    # Mark as watched for the first time
    if not unlock:
        unlock = Unlock(user_id=user_id, video_id=video_id)
        session.add(unlock)
    unlock.ad_watched = True
    unlock.unlocked_at = datetime.datetime.utcnow()
    session.commit()
    session.close()

    return jsonify({"delivery_link": delivery_link}), 200


# ---------- Backend endpoints ----------

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


# Set the Telegram webhook at import time, so it runs whether the app is
# started via `python app.py` (dev) or `gunicorn app:app` (Render/production).
# Gunicorn imports this module rather than running it as __main__, so the
# webhook setup can't live inside an `if __name__ == "__main__":` guard.
bot.remove_webhook()
bot.set_webhook(url=f"{BASE_URL}/webhook/{BOT_TOKEN}")

# Command menu (the "Menu" button next to the message box) is scoped per chat:
# everyone sees just /start; only your own chat with the bot sees the admin
# commands. These two lists are also the single source of truth for /help
# (see handle_help above) so the menu and /help can never drift apart.
bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(ADMIN_ID))

# Runs forever in the background, deleting messages whose 30-minute timer has
# come due — including any that came due while the app was restarting, since
# schedule_delete() persists them to the database instead of an in-memory
# timer. daemon=True so this thread doesn't block the process from exiting.
threading.Thread(target=run_deletion_sweep, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
