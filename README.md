# Video Unlock Bot — Setup Guide

## 1. Create the bot
1. Message **@BotFather** on Telegram → `/newbot` → follow prompts.
2. Save the token it gives you (`BOT_TOKEN`).
3. Send `/mybots` → your bot → **Bot Settings → Menu Button / Web App** if you want the button styled nicely (optional).

## 2. Get your Telegram user ID
Message **@userinfobot** — it replies with your numeric ID. Use this as `ADMIN_ID`.

## 3. Deploy the mini app page (Netlify)
1. Go to netlify.com → drag and drop the `netlify_page` folder (just `index.html`) onto the dashboard.
2. Netlify gives you a URL like `https://random-name-123.netlify.app`.
3. In `netlify_page/index.html`, replace `YOUR_BLOCK_ID` with the block ID from your Adsgram account (see step 5).
4. Re-deploy after editing (drag the folder again, or connect a GitHub repo for auto-deploy).

## 4. Deploy the backend (Render)
1. Push this whole project (minus `netlify_page`, or including it — doesn't matter) to a GitHub repo.
2. Go to render.com → New → Web Service → connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app`
5. Add environment variables in Render's dashboard:
   - `BOT_TOKEN` = your bot token
   - `WEBAPP_URL` = your Netlify URL (from step 3)
   - `BASE_URL` = the Render URL Render assigns you (e.g. `https://your-app.onrender.com`)
   - `ADMIN_ID` = your Telegram numeric ID
6. Deploy. On first boot, the app sets the Telegram webhook automatically.

## 5. Set up Adsgram (real rewarded ads)
1. Register at adsgram.ai as a publisher, add your bot's platform.
2. Create a new ad block → **Reward URL** field must be filled in this exact format
   (AdsGram requires HTTPS, a GET endpoint, and the literal `[userId]` placeholder):
   ```
   https://your-backend.onrender.com/ad-complete?userid=[userId]
   ```
   Use your real Render URL once deployed — you can fill in a placeholder here and edit
   it later in the AdsGram dashboard once you know your final backend URL.
3. Copy the resulting `blockId` into `netlify_page/index.html` (replace `YOUR_BLOCK_ID`).
4. This Reward URL is AdsGram's own server confirming the ad was watched — it's a GET
   request with only the Telegram user ID, no video_id. `app.py` already handles this by
   looking up the most recent video that user was waiting to unlock (set when they tap
   the bot's "Watch Ad to Unlock" button) and sending it from there.

## 6. Add a video
1. Send any video file to your bot in a private chat with you as `ADMIN_ID`.
2. Reply to that video message with `/addvideo My Title`.
3. Bot replies with a shareable link like:
   `https://t.me/your_bot?start=1`

## 7. Test the full flow
1. Open the share link → bot shows "Watch Ad to Unlock" button.
2. Tap it → mini app opens → tap "Watch Ad to Unlock" → Adsgram ad plays.
3. On completion, mini app closes and the bot sends the video.

## Notes
- SQLite (`bot.db`) works for testing, but Render's free tier disk is **ephemeral** — the
  database resets on redeploy. For production, add a free Render Postgres instance and set
  `DATABASE_URL` to its connection string (no code changes needed, `models.py` already reads
  from that env var).
- The client-side `tg.sendData()` unlock can technically be spoofed by a determined user.
  The `/ad-complete` endpoint exists so you can switch to Adsgram's server-to-server postback
  once you're ready — that's the "real" verification path.
