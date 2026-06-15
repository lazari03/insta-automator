#!/usr/bin/env python3
import os, subprocess, shutil, tempfile, logging, asyncio, json, requests, datetime
import xml.etree.ElementTree as ET
from pathlib import Path
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update
from telegram.error import Conflict

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_ADMIN_ID = int(os.environ["TELEGRAM_ADMIN_ID"])
IG_ACCESS_TOKEN   = os.environ.get("IG_ACCESS_TOKEN", "")
IG_USER_ID        = os.environ.get("IG_USER_ID", "")
SEEN_FILE         = Path("seen.json")
POSTED_TODAY_FILE = Path("posted_today.json")
CHECK_INTERVAL    = 1800  # 30 min
DAILY_LIMIT       = 10

# YouTube RSS feeds — no IP blocking, always works
CHANNELS = [
    ("FIFA",       "UCpcTrCXblq78GZrTUTLWeBw"),
    # Add more channel IDs here if needed
    # ("Goal",     "UCnUYZLuoy1rq1aVMwx4aTzw"),
]

WC_KEYWORDS = [
    "world cup", "worldcup", "fifa world cup", "2026", "wc26",
    "goal", "match", "final", "group stage", "qualifier", "semifinal",
    "mbappe", "messi", "ronaldo", "haaland", "neymar", "vinicius",
    "brazil", "argentina", "france", "england", "germany",
    "spain", "portugal", "morocco", "japan", "usa", "mexico",
    "highlight", "highlights", "scored", "penalty", "free kick",
    "freekick", "hat trick", "assist", "winner", "eliminated",
    "knockout", "group", "quarter", "semi"
]

# ── State ─────────────────────────────────────────────────────────────────────

def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()

def mark_seen(vid_id):
    seen = load_seen()
    seen.add(vid_id)
    SEEN_FILE.write_text(json.dumps(list(seen)))

def get_posted_today() -> int:
    today = datetime.date.today().isoformat()
    if POSTED_TODAY_FILE.exists():
        data = json.loads(POSTED_TODAY_FILE.read_text())
        if data.get("date") == today:
            return data.get("count", 0)
    return 0

def increment_posted_today():
    today = datetime.date.today().isoformat()
    count = get_posted_today() + 1
    POSTED_TODAY_FILE.write_text(json.dumps({"date": today, "count": count}))

def notify(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_ADMIN_ID, "text": text},
            timeout=10
        )
    except Exception as e:
        logging.error(f"Notify error: {e}")

def is_wc_related(title):
    t = title.lower()
    return any(kw in t for kw in WC_KEYWORDS)

# ── RSS fetch — no IP blocking ────────────────────────────────────────────────

def fetch_rss(channel_id: str) -> list:
    """Fetch latest videos from YouTube RSS feed. Always works on Railway."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    r = requests.get(url, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"RSS fetch failed: {r.status_code}")

    ns = {
        "atom":  "http://www.w3.org/2005/Atom",
        "yt":    "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }
    root = ET.fromstring(r.text)
    videos = []
    for entry in root.findall("atom:entry", ns):
        vid_id = entry.find("yt:videoId", ns)
        title  = entry.find("atom:title", ns)
        if vid_id is None or title is None:
            continue
        videos.append({
            "id":    vid_id.text,
            "title": title.text,
            "url":   f"https://www.youtube.com/shorts/{vid_id.text}",
        })
    return videos

def get_all_wc_videos() -> list:
    """Fetch from all channels, filter WC related."""
    all_videos = []
    for name, channel_id in CHANNELS:
        try:
            videos = fetch_rss(channel_id)
            wc = [v for v in videos if is_wc_related(v["title"])]
            all_videos.extend(wc)
            logging.info(f"{name}: {len(videos)} videos, {len(wc)} WC related")
        except Exception as e:
            logging.error(f"RSS error for {name}: {e}")
    return all_videos

# ── Video processing ──────────────────────────────────────────────────────────

def download_and_strip(url, work_dir):
    raw   = Path(work_dir) / "raw.mp4"
    clean = Path(work_dir) / "clean.mp4"
    r1 = subprocess.run([
        "yt-dlp", url, "-o", str(raw),
        "-f", "best[ext=mp4]/best",
        "--quiet", "--no-warnings"
    ], timeout=120, capture_output=True)
    if not raw.exists():
        raise RuntimeError(f"Download failed: {r1.stderr.decode()[:200]}")
    subprocess.run([
        "ffmpeg", "-y", "-i", str(raw),
        "-map", "0", "-map_metadata", "-1",
        "-metadata", "title=", "-metadata", "comment=",
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        str(clean)
    ], capture_output=True, timeout=120)
    if not clean.exists():
        raise RuntimeError("FFmpeg processing failed")
    return clean

def upload_to_fileio(video_path):
    with open(video_path, "rb") as f:
        r = requests.post(
            "https://file.io",
            files={"file": f},
            data={"expires": "1d"},
            timeout=120
        )
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"Upload failed: {data}")
    return data["link"]

def post_to_instagram(video_url, caption):
    if not IG_ACCESS_TOKEN or not IG_USER_ID:
        return {"error": "Instagram not configured"}
    r = requests.post(
        f"https://graph.instagram.com/v18.0/{IG_USER_ID}/media",
        data={
            "media_type": "REELS", "video_url": video_url,
            "caption": caption, "share_to_feed": "true",
            "access_token": IG_ACCESS_TOKEN,
        }, timeout=30
    )
    data = r.json()
    if "id" not in data:
        raise RuntimeError(f"Container error: {data}")
    container_id = data["id"]
    import time
    for _ in range(24):
        time.sleep(5)
        s = requests.get(
            f"https://graph.instagram.com/v18.0/{container_id}",
            params={"fields": "status_code", "access_token": IG_ACCESS_TOKEN},
            timeout=15
        ).json()
        if s.get("status_code") == "FINISHED":
            break
        if s.get("status_code") == "ERROR":
            raise RuntimeError(f"IG error: {s}")
    pub = requests.post(
        f"https://graph.instagram.com/v18.0/{IG_USER_ID}/media_publish",
        data={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN},
        timeout=30
    )
    return pub.json()

def build_caption(title):
    t = title.lower()
    tags = "#WorldCup #FIFA #wcdaily #Shorts #Football"
    extra = []
    if "mbappe"    in t: extra.append("#Mbappe")
    if "messi"     in t: extra.append("#Messi")
    if "ronaldo"   in t: extra.append("#Ronaldo #CR7")
    if "haaland"   in t: extra.append("#Haaland")
    if "goal"      in t: extra.append("#Goal #Gol")
    if "penalty"   in t: extra.append("#Penalty")
    if "final"     in t: extra.append("#WorldCupFinal")
    if "brazil"    in t: extra.append("#Brazil")
    if "argentina" in t: extra.append("#Argentina")
    if "france"    in t: extra.append("#France")
    if extra:
        tags += " " + " ".join(extra[:4])
    return f"{title}\n\n⚽ {tags}"

# ── Process one video ─────────────────────────────────────────────────────────

def process_video(video: dict) -> bool:
    """Download, strip, upload, post. Returns True on success."""
    work_dir = tempfile.mkdtemp()
    try:
        notify(f"⬇️ Downloading:\n{video['title']}")
        clean     = download_and_strip(video["url"], work_dir)
        notify("☁️ Uploading...")
        video_url = upload_to_fileio(clean)
        caption   = build_caption(video["title"])
        notify("📲 Posting to Instagram...")
        result    = post_to_instagram(video_url, caption)
        if "id" in result:
            increment_posted_today()
            mark_seen(video["id"])
            notify(
                f"✅ Posted!\n"
                f"📹 {video['title']}\n"
                f"🆔 {result['id']}\n"
                f"📊 {get_posted_today()}/{DAILY_LIMIT} today"
            )
            return True
        else:
            notify(f"⚠️ IG error: {result}")
            mark_seen(video["id"])
            return False
    except Exception as e:
        notify(f"❌ Failed:\n{video['title']}\n{str(e)[:200]}")
        return False
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

# ── Monitor loop ──────────────────────────────────────────────────────────────

async def monitor_loop(app):
    await asyncio.sleep(5)
    notify(
        "🤖 WCDAILY Bot started\n"
        f"Channels: {', '.join(n for n, _ in CHANNELS)}\n"
        f"Daily limit: {DAILY_LIMIT} posts\n"
        f"Check interval: every {CHECK_INTERVAL//60} min\n"
        "Send /check to scan now"
    )
    while True:
        try:
            if Path("paused.flag").exists():
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            posted_today = get_posted_today()
            if posted_today >= DAILY_LIMIT:
                now      = datetime.datetime.now()
                midnight = (now + datetime.timedelta(days=1)).replace(
                    hour=0, minute=5, second=0, microsecond=0
                )
                wait = (midnight - now).total_seconds()
                notify(f"⏸ Daily limit {DAILY_LIMIT} reached. Resuming tomorrow.")
                await asyncio.sleep(wait)
                continue

            videos    = get_all_wc_videos()
            seen      = load_seen()
            new       = [v for v in videos if v["id"] not in seen]

            if not new:
                logging.info("No new WC videos found.")
            else:
                notify(f"🔔 {len(new)} new WC video(s) found — posting...")
                for video in new:
                    if get_posted_today() >= DAILY_LIMIT:
                        notify(f"⏸ Daily limit hit. {len(new)} remaining for tomorrow.")
                        break
                    if Path("paused.flag").exists():
                        break
                    process_video(video)
                    await asyncio.sleep(30)

        except Exception as e:
            notify(f"❌ Monitor error: {str(e)[:200]}")

        await asyncio.sleep(CHECK_INTERVAL)

# ── Telegram commands ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏆 WCDAILY Bot\n\n"
        "Commands:\n"
        "/check — scan RSS now + show queue\n"
        "/post [url] — manually post a YouTube URL\n"
        "/status — integrations + stats\n"
        "/today — posts count today\n"
        "/pause — pause auto-posting\n"
        "/resume — resume auto-posting"
    )

async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Scanning RSS feeds...")
    try:
        videos = get_all_wc_videos()
        seen   = load_seen()
        new    = [v for v in videos if v["id"] not in seen]
        msg = (
            f"📊 Scan results:\n"
            f"WC videos in RSS: {len(videos)}\n"
            f"Already posted: {len(videos) - len(new)}\n"
            f"Queue: {len(new)}\n"
            f"Posted today: {get_posted_today()}/{DAILY_LIMIT}\n"
        )
        if new:
            msg += "\nNext in queue:\n"
            msg += "\n".join(f"• {v['title']}" for v in new[:5])
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manually post any YouTube URL."""
    if not ctx.args:
        await update.message.reply_text(
            "Usage: /post [YouTube URL]\n\n"
            "Example:\n/post https://youtube.com/shorts/xxxxx"
        )
        return
    url = ctx.args[0]
    caption = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else None
    await update.message.reply_text(f"⬇️ Processing:\n{url}")

    work_dir = tempfile.mkdtemp()
    try:
        clean     = download_and_strip(url, work_dir)
        await update.message.reply_text("☁️ Uploading...")
        video_url = upload_to_fileio(clean)
        if not caption:
            # Try to get title from yt-dlp
            r = subprocess.run(
                ["yt-dlp", url, "--print", "%(title)s", "--quiet"],
                capture_output=True, text=True, timeout=30
            )
            title   = r.stdout.strip() or "World Cup Highlight"
            caption = build_caption(title)
        await update.message.reply_text("📲 Posting to Instagram...")
        result = post_to_instagram(video_url, caption)
        if "id" in result:
            increment_posted_today()
            await update.message.reply_text(
                f"✅ Posted to Instagram!\n"
                f"🆔 Post ID: {result['id']}\n"
                f"📝 {caption[:100]}..."
            )
        else:
            await update.message.reply_text(f"⚠️ IG error: {result}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    seen = load_seen()
    await update.message.reply_text(
        f"{'✅' if IG_ACCESS_TOKEN else '❌'} Instagram token\n"
        f"{'✅' if IG_USER_ID else '❌'} Instagram user ID\n"
        f"📊 {get_posted_today()}/{DAILY_LIMIT} posted today\n"
        f"👁 {len(seen)} total posted\n"
        f"{'⏸ PAUSED' if Path('paused.flag').exists() else '▶️ Running'}"
    )

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📊 Posted today: {get_posted_today()}/{DAILY_LIMIT}"
    )

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    Path("paused.flag").write_text("1")
    await update.message.reply_text("⏸ Paused.")

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    Path("paused.flag").unlink(missing_ok=True)
    await update.message.reply_text("▶️ Resumed.")

async def on_error(update, ctx):
    if isinstance(ctx.error, Conflict):
        logging.warning("Conflict — shutting down.")
        raise SystemExit(1)
    logging.error(f"Error: {ctx.error}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("check",  cmd_check))
    app.add_handler(CommandHandler("post",   cmd_post))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("today",  cmd_today))
    app.add_handler(CommandHandler("pause",  cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_error_handler(on_error)

    async def post_init(application):
        asyncio.create_task(monitor_loop(application))

    app.post_init = post_init
    print("🤖 WCDAILY Bot running...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()