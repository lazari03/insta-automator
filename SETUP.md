# WCDAILY — Setup Guide
Full social media manager controlled via Telegram.

---

## What this does

- **Every morning at 8 UTC** → auto-sends you today's matches + yesterday's results in Telegram
- **One tap** → posts match previews, results, standings to Instagram
- **/clip [url]** → downloads YouTube video, cuts into 30-60s Reels, sends you a zip
- **/post [text]** → posts to Instagram and/or TikTok
- **/pause / /resume** → pause all auto-posting anytime
- **/status** → check all integrations at a glance

---

## Step 1 — Create your Telegram bot (2 min)

1. Open Telegram → search @BotFather
2. Send `/newbot`
3. Choose a name: `wcdaily`
4. Copy the token → paste into `.env` as `TELEGRAM_TOKEN`
5. Get your own user ID: search @userinfobot → send `/start` → copy your ID → `TELEGRAM_ADMIN_ID`

---

## Step 2 — Get free football data (2 min)

1. Go to https://www.football-data.org/
2. Register free account
3. Copy your API key → `FOOTBALL_API_KEY`

---

## Step 3 — Instagram setup (20 min, one-time)

You need a **Business or Creator Instagram account** linked to a Facebook Page.

1. Go to https://developers.facebook.com/
2. Create an app → add Instagram Graph API product
3. Get a long-lived access token (valid 60 days, refreshable)
4. Find your Instagram User ID
5. Paste both into `.env`

Tutorial: https://developers.facebook.com/docs/instagram-api/getting-started

---

## Step 4 — Run locally

```bash
# Install dependencies
pip install -r requirements.txt
brew install ffmpeg   # macOS
# apt install ffmpeg  # Ubuntu

# Set up environment
cp .env.example .env
# Edit .env with your values

# Export and run
export $(cat .env | xargs)
python bot.py
```

---

## Step 5 — Deploy free on Railway (runs 24/7)

1. Go to https://railway.app → sign up free
2. New Project → Deploy from GitHub repo
3. Upload these files to a GitHub repo first
4. Add all environment variables in Railway dashboard
5. Add this start command: `python bot.py`

Railway free tier: 500 hours/month — enough for 24/7 if you keep usage low.

**Alternative: Render.com free tier** — same process, also free.

---

## Telegram commands

| Command | What it does |
|---|---|
| `/start` | Show all commands |
| `/today` | Today's World Cup matches |
| `/results` | Recent match results |
| `/standings` | Current group standings |
| `/clip [url]` | Cut YouTube video into Reels zip |
| `/post [text]` | Post to Instagram / TikTok |
| `/autoday` | Trigger daily update now |
| `/schedule` | View scheduled posts |
| `/pause` | Pause all auto-posting |
| `/resume` | Resume auto-posting |
| `/status` | Check all integrations |

---

## Daily automation flow (happens automatically)

```
08:00 UTC every day
  → fetch today's matches from football-data.org
  → fetch yesterday's results
  → send summary to your Telegram
  → tap "Post to IG" to publish instantly
```

---

## Video clip flow

```
You: /clip https://youtube.com/watch?v=xxx
Bot: ⬇️ Downloading...
Bot: ✂️ Cutting clips...
Bot: 📦 Zipping...
Bot: sends you clips.zip (3-8 clips, 9:16, audio stripped)
Bot: [Post all as Reels] button
```

---

## Notes

- Audio is always stripped from clips to avoid copyright flags
- Add your own music in TikTok/IG editor after posting
- Instagram token expires every 60 days — refresh it or set up auto-refresh
- football-data.org free tier: 10 requests/minute — more than enough
- World Cup competition ID is 2000 in football-data.org

---

## Auto Channel Monitor

Add channels to watch in `channel_monitor.py`:

```python
WATCH_CHANNELS = [
    ("FIFA",       "https://www.youtube.com/@FIFA"),
    ("Goal",       "https://www.youtube.com/@goal"),
    ("CBS Sports", "https://www.youtube.com/@CBSSports"),
]
```

Then in `bot.py` main(), add two lines:

```python
from channel_monitor import monitor_loop
loop.create_task(monitor_loop())
```

Now one command starts everything:

```bash
python bot.py
```

### What happens automatically

```
Every 30 min:
  → checks each channel's /shorts tab
  → finds videos not seen before
  → downloads + strips all metadata
  → generates caption via Claude
  → posts to Instagram as Reel
  → sends you Telegram notification ✅
```

### Telegram notifications you get

```
🔔 New Short detected from FIFA
   "Mbappe's insane goal vs Argentina"
   Processing...

✅ Posted to Instagram!
   📹 Mbappe's insane goal vs Argentina
   📝 Pure magic from Mbappe ⚽ #WorldCup #FIFA
   🆔 Post ID: 17846368
```

### Adjust check frequency

In `channel_monitor.py`:
```python
CHECK_INTERVAL_MINUTES = 30  # change to 15 for faster, 60 for slower
```
