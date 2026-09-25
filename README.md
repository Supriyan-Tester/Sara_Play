# Sara Play — Setup Guide

Gallery-based Telegram mini app: browse drawing video thumbnails, tap one to see
"Play Now (2 ads, with consent)" or "Share" (no ad required). Backend is a single
Flask app on Render that also serves the mini app page itself — no separate
frontend host needed.

## 1. Create the bot
1. Message **@BotFather** on Telegram → `/newbot` → follow prompts.
2. Save the token it gives you (`BOT_TOKEN`).

## 2. Get your Telegram user ID
Message **@userinfobot** — it replies with your numeric ID. Use this as `ADMIN_ID`.

## 3. Deploy the backend (Render) — this now includes the mini app page
1. Push this whole project to a GitHub repo (keep `static_webapp/index.html` in place —
   Flask serves it directly from there).
2. Go to render.com → New → Web Service → connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app`
5. Add environment variables:
   - `BOT_TOKEN` = your bot token
   - `BASE_URL` = leave blank on first deploy, then set to the real `.onrender.com` URL
     Render gives you once it's live, and redeploy (see note in step 3 below)
   - `ADMIN_ID` = your Telegram numeric ID
   - `DATABASE_URL` = your Render Postgres connection string (see step 4)
6. Deploy. On boot, the app sets the Telegram webhook and mini app URL automatically —
   you do **not** need a separate `WEBAPP_URL` env var anymore; it defaults to
   `{BASE_URL}/webapp` on this same service.

## 4. Set up persistent storage (Postgres)
SQLite resets every time Render redeploys — use Postgres so videos and unlocks survive.
1. Render dashboard → New → PostgreSQL (free tier is fine).
2. Copy its **Internal Database URL** (starts with `postgres://`).
3. Add it as `DATABASE_URL` on your web service. `models.py` already rewrites the
   `postgres://` prefix to use the `psycopg` (v3) driver automatically.

## 5. Set up AdsGram (real rewarded ads)
AdsGram has two things that must both exist and be linked correctly:
- A **platform**, type **Mini App** (not "Bot" — that's a different ad product and
  won't work with this SDK-based flow).
- An **ad block** under that platform, type **Reward** (gives a plain numeric
  `blockId` — a `bot-XXXXX` style ID from a "Bot" platform will NOT work here).

Steps:
1. Register at partner.adsgram.ai, create a platform with type **Mini App**, using
   your app's URL: `https://your-backend.onrender.com/webapp`
2. Create a **Reward**-type block under it. Fill in the Reward URL field exactly as:
   ```
   https://your-backend.onrender.com/ad-complete?userid=[userId]
   ```
   (The literal `[userId]` stays as-is — AdsGram substitutes it automatically.)
3. Copy the resulting numeric `blockId` into `static_webapp/index.html`:
   ```js
   AdController = window.Adsgram.init({ blockId: "YOUR_REAL_BLOCK_ID" });
   ```
4. **New platforms/blocks go through AdsGram moderation** before they'll actually
   serve ads — check `partner.adsgram.ai/platforms` for approval status. Until
   approved, "Play Now" will show "Ad was skipped or failed to load" even with
   correct code — that's expected, not a bug.
5. AdsGram's policy requires (a) explicit user consent before any ad SDK call, and
   (b) a non-ad path to still get value from the app. Both are already built in:
   the consent overlay in `static_webapp/index.html` gates all AdsGram SDK calls,
   and the "Share" button works with zero ads involved.

## 6. Custom domain (optional) — use your own domain instead of `*.onrender.com`
Since the mini app is now served by this same Render service, you can point a
subdomain of a domain you already own (e.g. `baglity.com`) straight at it —
no third-party frontend host needed at all:
1. Render dashboard → your web service → **Settings → Custom Domains → Add**.
2. Enter something like `app.baglity.com`. Render gives you a DNS target (usually
   a CNAME value).
3. Wherever baglity.com's DNS is managed (registrar or Cloudflare), add a CNAME
   record: Host = `app`, Value = *(what Render gave you)*.
4. Once DNS propagates (minutes to a few hours), Render auto-issues a free SSL cert.
5. From then on, `https://app.baglity.com` and `https://your-app.onrender.com` both
   serve the same app. You can use either as `BASE_URL` — just stay consistent, since
   changing it later means re-setting the Telegram webhook and updating the AdsGram
   platform URL and Reward URL to match.

## 7. Adding a video
1. Send any video file to the bot in a private chat, as the `ADMIN_ID` account.
2. Reply to that video with:
   ```
   /addvideo Title Here | Caption for the channel post
   ```
   The `| Caption` part is optional — if you skip it, the title is reused as the
   caption. This is typed inline in the same command; the bot doesn't prompt for
   it separately.
3. Bot asks for a thumbnail photo next — send one. **A thumbnail is required** for
   the video to appear in the gallery at all (the gallery API only returns videos
   that have one). Or send `/skipthumbnail` to skip the channel post, but note the
   video still won't show in the gallery without a thumbnail.

## 8. Auto-post to your channels (managed via the bot — no env vars, no redeploys)
1. Add the bot as an **admin** of your channel (needs "post messages" permission).
2. In your chat with the bot, send `/addchannel`.
3. Forward any message *from that channel* to the bot (must be a real forward, not
   retyped) — the bot reads the channel's ID from the forward and registers it.
4. Repeat for as many channels as you want — every `/addvideo` + thumbnail now
   posts to **all** registered channels automatically.
5. `/listchannels` — see everything currently registered, plus the current hub link.
6. `/removechannel <id>` — remove one (id comes from `/listchannels`).

## 9. Admin-only menu + "Join Our Channels" button
- `/sethub https://t.me/your_main_channel` — sets the link behind the "🔗 Join Our
  Channels" button shown under plain `/start`. Also managed via the bot, not an env var.
- The bot's Menu button is scoped automatically: everyone sees only `/start`; your
  own chat (matching `ADMIN_ID`) also sees `/addvideo`, `/skipthumbnail`,
  `/addchannel`, `/listchannels`, `/removechannel`, and `/sethub`. No manual
  setup — this runs on every deploy.

## 10. Test the full flow
1. `/start` the bot → tap "Watch Video 😇" → gallery should load thumbnails.
2. Tap a video → detail view → "Play Now" → consent screen → (once AdsGram
   approves your platform) two ads play in sequence → video is delivered in chat.
3. Or tap "Share" → opens Telegram's share sheet with a deep link, no ad required.

## Notes
- The client-side `tg.sendData()` unlock can technically be spoofed by a determined
  user. The `/ad-complete` endpoint is the real, server-verified path — AdsGram's
  own Reward URL callback hits it directly, independent of anything the browser says.
- `/debug/unlocks?key=<your BOT_TOKEN's numeric prefix>` is a diagnostic endpoint
  for inspecting the database during testing. Remove it before a real public launch —
  it's not properly authenticated.
