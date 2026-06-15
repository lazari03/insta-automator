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

# Source: FIFA World Cup Instagram
SOURCE_IG_USER    = "fifaworldcup"

WC_KEYWORDS = [
    "world cup", "worldcup", "2026", "wc26", "goal", "match",
    "final", "group", "qualifier", "semifinal", "highlight",
    "mbappe", "messi", "ronaldo", "haaland", "neymar", "vinicius",
    "brazil", "argentina", "france", "england", "germany",
    "spain", "portugal", "morocco", "japan", "usa", "mexico",
    "scored", "penalty", "free kick", "hat trick", "assist",
    "winner", "eliminated", "knockout", "quarter", "semi", "fifa"
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

def is_wc_related(text):
    t = (text or "").lower()
    return any(kw in t for kw in WC_KEYWORDS)

# ── Fetch from Instagram source via Graph API ─────────────────────────────────

def get_source_ig_videos() -> list:
    """
    Fetch recent Reels from @fifaworldcup using Instagram Graph API.
    Requires IG_ACCESS_TOKEN with instagram_basic permission.
    """
    if not IG_ACCESS_TOKEN or not IG_USER_ID:
        raise RuntimeError("Instagram not configured")

    # Search for the fifaworldcup account ID first
    # Then fetch their media — only works if they are in your test users
    # or your app is approved for pages_read_engagement

    # Alternative: use IG Basic Display API to fetch by username
    # For now fetch from a hardcoded known ID or use oEmbed
    url = f"https://graph.instagram.com/v18.0/{IG_USER_ID}/media"
    params = {
        "fields": "id,media_type,caption,media_url,permalink,timestamp",
        "access_token": IG_ACCESS_TOKEN,
        "limit": 20
    }
    r = requests.get(url, params=params, timeout=15)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"IG API error: {data['error']}")

    videos = []
    for item in data.get("data", []):
        if item.get("media_type") in ("VIDEO", "REELS"):
            caption = item.get("caption", "")
            videos.append({
                "id":        item["id"],
                "title":     caption[:80] if caption else "FIFA World Cup",
                "caption":   caption,
                "url":       item.get("media_url", ""),
                "permalink": item.get("permalink", ""),
            })
    return videos

def get_source_videos_ytdlp() -> list:
    """
    Fallback: use yt-dlp to fetch from Instagram profile.
    Works on Railway for Instagram (unlike YouTube).
    """
    cmd = [
        "yt-dlp",
        f"https://www.instagram.com/{SOURCE_IG_USER}/reels/",
        "--flat-playlist",
        "--playlist-items", "1-20",
        "--print", '{"id":"%(id)s","title":"%(title)s","url":"%(webpage_url)s"}',
        "--quiet", "--no-warnings",
        "--no-check-certificates",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    videos = []
    for line in result.stdout.strip().splitlines():
        try:
            v = json.loads(line)
            videos.append(v)
        except Exception:
            continue
    return videos

def get_all_wc_videos() -> list:
    """Try Graph API first, fall back to yt-dlp."""
    try:
        videos = get_source_ig_videos()
        logging.info(f"Graph API: {len(videos)} videos fetched")
        return [v for v in videos if is_wc_related(v.get("caption", "") + v.get("title", ""))]
    except Exception as e:
        logging.warning(f"Graph API failed: {e} — trying yt-dlp")

    try:
        videos = get_source_videos_ytdlp()
        logging.info(f"yt-dlp: {len(videos)} videos fetched")
        return [v for v in videos if is_wc_related(v.get("title", ""))]
    except Exception as e:
        logging.error(f"yt-dlp also failed: {e}")
        return []

# ── Video download ────────────────────────────────────────────────────────────

def download_video(video: dict, work_dir: str) -> Path:
    """
    Download video — tries direct media_url first (fastest),
    then falls back to yt-dlp with the permalink.
    """
    raw   = Path(work_dir) / "raw.mp4"
    clean = Path(work_dir) / "clean.mp4"

    # Try direct URL download first (from Graph API media_url)
    direct_url = video.get("url", "")
    if direct_url and direct_url.startswith("http"):
        try:
            r = requests.get(direct_url, timeout=60, stream=True)
            if r.status_code == 200:
                with open(raw, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                logging.info("Downloaded via direct URL")
        except Exception as e:
            logging.warning(f"Direct download failed: {e}")

    # Fallback: yt-dlp with permalink or constructed URL
    if not raw.exists() or raw.stat().st_size < 1000:
        permalink = video.get("permalink") or f"https://www.instagram.com/p/{video['id']}/"
        subprocess.run([
            "yt-dlp", permalink,
            "-o", str(raw),
            "-f", "best[ext=mp4]/best",
            "--quiet", "--no-warnings",
            "--no-check-certificates",
        ], timeout=120, capture_output=True)

    if not raw.exists() or raw.stat().st_size < 1000:
        raise RuntimeError("Download failed — file empty or missing")

    # Strip metadata + ensure 9:16
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

# ── Upload + post ─────────────────────────────────────────────────────────────

def upload_to_fileio(video_path: Path) -> str:
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

def post_to_instagram(video_url: str, caption: str) -> dict:
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
    for _ in range(30):
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

def build_caption(original_caption: str, title: str = "") -> str:
    base = (original_caption or title or "FIFA World Cup Highlight")[:300]
    tags = "#WorldCup #FIFA #wcdaily #Reels #Football"
    t = base.lower()
    extra = []
    if "mbappe"    in t: extra.append("#Mbappe")
    if "messi"     in t: extra.append("#Messi")
    if "ronaldo"   in t: extra.append("#Ronaldo")
    if "haaland"   in t: extra.append("#Haaland")
    if "goal"      in t: extra.append("#Goal")
    if "penalty"   in t: extra.append("#Penalty")
    if "final"     in t: extra.append("#WorldCupFinal")
    if "brazil"    in t: extra.append("#Brazil")
    if "argentina" in t: extra.append("#Argentina")
    if extra:
        tags += " " + " ".join(extra[:4])
    return f"{base}\n\n⚽ {tags}"

# ── Process one video ─────────────────────────────────────────────────────────

def process_video(video: dict) -> bool:
    work_dir = tempfile.mkdtemp()
    try:
        notify(f"⬇️ Downloading:\n{video.get('title','')[:60]}")
        clean     = download_video(video, work_dir)
        notify("☁️ Uploading to temp host...")
        video_url = upload_to_fileio(clean)
        caption   = build_caption(video.get("caption", ""), video.get("title", ""))
        notify("📲 Posting to Instagram...")
        result    = post_to_instagram(video_url, caption)
        if "id" in result:
            increment_posted_today()
            mark_seen(video["id"])
            notify(
                f"✅ Posted!\n"
                f"📹 {video.get('title','')[:60]}\n"
                f"🆔 {result['id']}\n"
                f"📊 {get_posted_today()}/{DAILY_LIMIT} today"
            )
            return True
        else:
            notify(f"⚠️ IG error: {result}")
            mark_seen(video["id"])
            return False
    except Exception as e:
        notify(f"❌ Failed:\n{str(e)[:200]}")
        return False
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

# ── Monitor loop ──────────────────────────────────────────────────────────────

async def monitor_loop(app):
    await asyncio.sleep(5)
    notify(
        "🤖 WCDAILY Bot started\n"
        f"Source: @{SOURCE_IG_USER}\n"
        f"Daily limit: {DAILY_LIMIT} posts\n"
        f"Checking every {CHECK_INTERVAL//60} min\n"
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

            videos = get_all_wc_videos()
            seen   = load_seen()
            new    = [v for v in videos if v["id"] not in seen]

            if new:
                notify(f"🔔 {len(new)} new WC video(s) — posting...")
                for video in new:
                    if get_posted_today() >= DAILY_LIMIT:
                        notify(f"⏸ Daily limit hit. {len(new)} queued for tomorrow.")
                        break
                    if Path("paused.flag").exists():
                        break
                    process_video(video)
                    await asyncio.sleep(30)

        except Exception as e:
            notify(f"❌ Monitor error: {str(e)[:200]}")

        await asyncio.sleep(CHECK_INTERVAL)

# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏆 WCDAILY Bot\n\n"
        f"Source: @{SOURCE_IG_USER}\n"
        f"Daily limit: {DAILY_LIMIT} posts\n\n"
        "Commands:\n"
        "/check — scan now + show queue\n"
        "/next — post next in queue\n"
        "/status — integrations + stats\n"
        "/today — posts count today\n"
        "/pause — pause auto-posting\n"
        "/resume — resume posting"
    )

async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🔍 Scanning @{SOURCE_IG_USER}...")
    try:
        videos = get_all_wc_videos()
        seen   = load_seen()
        new    = [v for v in videos if v["id"] not in seen]
        msg = (
            f"📊 Scan results:\n"
            f"WC videos found: {len(videos)}\n"
            f"Already posted: {len(videos) - len(new)}\n"
            f"Queue: {len(new)}\n"
            f"Posted today: {get_posted_today()}/{DAILY_LIMIT}\n"
        )
        if new:
            msg += "\nNext in queue:\n"
            msg += "\n".join(f"• {v.get('title','')[:50]}" for v in new[:5])
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_next(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Finding next in queue...")
    try:
        videos = get_all_wc_videos()
        seen   = load_seen()
        queue  = [v for v in videos if v["id"] not in seen]
        if not queue:
            await update.message.reply_text("✅ Queue empty — no new WC videos.")
            return
        video = queue[0]
        await update.message.reply_text(
            f"▶️ Posting:\n{video.get('title','')[:60]}\n"
            f"Queue remaining: {len(queue) - 1}"
        )
        work_dir = tempfile.mkdtemp()
        try:
            clean     = download_video(video, work_dir)
            await update.message.reply_text("☁️ Uploading...")
            video_url = upload_to_fileio(clean)
            caption   = build_caption(video.get("caption", ""), video.get("title", ""))
            await update.message.reply_text("📲 Posting to Instagram...")
            result    = post_to_instagram(video_url, caption)
            if "id" in result:
                increment_posted_today()
                mark_seen(video["id"])
                await update.message.reply_text(
                    f"✅ Posted!\n"
                    f"📹 {video.get('title','')[:60]}\n"
                    f"🆔 {result['id']}\n"
                    f"📊 {get_posted_today()}/{DAILY_LIMIT} today\n"
                    f"📋 {len(queue) - 1} left in queue"
                )
            else:
                await update.message.reply_text(f"⚠️ IG error: {result}")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

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
    app.add_handler(CommandHandler("next",   cmd_next))
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