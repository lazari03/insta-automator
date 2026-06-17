#!/usr/bin/env python3
"""
FIFA World Cup Instagram Reels Automation Bot
Fixed version with proper async handling, deduplicated code, and bug fixes.
"""

import os
import subprocess
import shutil
import tempfile
import logging
import asyncio
import json
import requests
import datetime
import base64
import time
from pathlib import Path
from functools import partial
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update

# ── Logging Configuration ─────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# ── Environment & Configuration ─────────────────────────────────────────────

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_ADMIN_ID = int(os.environ["TELEGRAM_ADMIN_ID"])
IG_ACCESS_TOKEN   = os.environ.get("IG_ACCESS_TOKEN", "")
IG_USER_ID        = os.environ.get("IG_USER_ID", "")
IG_SESSION_B64    = os.environ.get("IG_SESSION", "")
IG_USERNAME       = os.environ.get("IG_USERNAME", "")
IG_PASSWORD       = os.environ.get("IG_PASSWORD", "")
PROXY_URL         = os.environ.get("PROXY_URL", "")
DATA_DIR          = Path(os.environ.get("DATA_DIR", "."))

SEEN_FILE         = DATA_DIR / "seen.json"
POSTED_TODAY_FILE = DATA_DIR / "posted_today.json"
SESSION_FILE      = DATA_DIR / "session.json"
PAUSED_FLAG_FILE  = DATA_DIR / "paused.flag"

CHECK_INTERVAL    = 1800  # 30 minutes
DAILY_LIMIT       = 10
SOURCE_ACCOUNT    = "fifaworldcup"

WC_KEYWORDS = [
    "world cup", "worldcup", "2026", "wc26", "goal", "match",
    "final", "group", "qualifier", "semifinal", "highlight",
    "mbappe", "messi", "ronaldo", "haaland", "neymar", "vinicius",
    "brazil", "argentina", "france", "england", "germany",
    "spain", "portugal", "morocco", "japan", "usa", "mexico",
    "scored", "penalty", "free kick", "hat trick", "assist",
    "winner", "eliminated", "knockout", "quarter", "semi", "fifa"
]

# ── Session Setup ─────────────────────────────────────────────────────────────

def setup_session():
    """Decode base64 session from env var and write to disk."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SESSION_FILE.exists():
        return True
    if not IG_SESSION_B64:
        logging.error("IG_SESSION env var not set")
        return False
    try:
        decoded = base64.b64decode(IG_SESSION_B64).decode("utf-8")
        SESSION_FILE.write_text(decoded)
        logging.info("Session file written from env var")
        return True
    except Exception as e:
        logging.error(f"Session decode error: {e}")
        return False

def get_instagrapi_client():
    """Get logged-in instagrapi client using session file + proxy."""
    from instagrapi import Client
    setup_session()
    cl = Client()
    if PROXY_URL:
        cl.set_proxy(PROXY_URL)
        logging.info(f"Using proxy: {PROXY_URL[:30]}...")
    cl.load_settings(str(SESSION_FILE))
    # Only login if session is expired/invalid; load_settings usually sufficient
    try:
        cl.get_timeline_feed()  # Test if session is valid
    except Exception:
        logging.info("Session expired, re-logging in...")
        cl.login(IG_USERNAME, IG_PASSWORD)
    return cl

# ── State Management ─────────────────────────────────────────────────────────

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

# ── Fetch from @fifaworldcup via instagrapi ──────────────────────────────────

def get_source_videos() -> list:
    """Fetch recent Reels from @fifaworldcup using instagrapi session."""
    cl = get_instagrapi_client()
    user_id = cl.user_id_from_username(SOURCE_ACCOUNT)
    medias = cl.user_medias(user_id, amount=20)
    videos = []
    for m in medias:
        # 1=photo, 2=video, 8=album (carousel)
        if m.media_type == 2:  # Video/Reel only
            caption = m.caption_text or ""
            title = caption[:80] if caption else "FIFA World Cup"
            if not is_wc_related(caption + title):
                continue
            videos.append({
                "id":      str(m.pk),
                "title":   title,
                "caption": caption,
                "url":     str(m.video_url) if m.video_url else "",
                "pk":      str(m.pk),
            })
        elif m.media_type == 8 and m.resources:
            # Album: check if any resource is a video
            for idx, res in enumerate(m.resources):
                if res.media_type == 2 and res.video_url:
                    caption = m.caption_text or ""
                    title = caption[:80] if caption else "FIFA World Cup"
                    if not is_wc_related(caption + title):
                        break
                    videos.append({
                        "id":      f"{m.pk}_{idx}",
                        "title":   title,
                        "caption": caption,
                        "url":     str(res.video_url),
                        "pk":      str(m.pk),
                    })
                    break  # Only take first video from album
    return videos

# ── Video Processing ───────────────────────────────────────────────────────────

def download_video(video: dict, work_dir: str) -> Path:
    raw = Path(work_dir) / "raw.mp4"
    clean = Path(work_dir) / "clean.mp4"

    # Attempt 1: Direct Download via fresh Instagram CDN URL
    if not raw.exists() or raw.stat().st_size < 1000:
        try:
            cl = get_instagrapi_client()
            pk = video.get("pk") or video.get("id")
            info = cl.media_info(pk)
            fresh_url = str(info.video_url)
            logging.info(f"Fresh URL: {fresh_url[:80]}...")

            request_kwargs = {
                "timeout": 120,
                "stream": True,
                "headers": {"User-Agent": "Instagram 275.0.0.27.98 Android"}
            }
            if PROXY_URL:
                request_kwargs["proxies"] = {"http": PROXY_URL, "https": PROXY_URL}

            r = requests.get(fresh_url, **request_kwargs)
            if r.status_code == 200:
                with open(raw, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                logging.info(f"Downloaded via CDN: {raw.stat().st_size} bytes")
        except Exception as e:
            logging.warning(f"Direct CDN download failed: {e}")

    # Attempt 2: Fallback to yt-dlp with Android-Client spoofing
    if not raw.exists() or raw.stat().st_size < 1000:
        logging.info("Triggering yt-dlp fallback...")
        try:
            import yt_dlp
        except ImportError:
            raise RuntimeError("yt-dlp not installed. Install with: pip install yt-dlp")

        ydl_opts = {
            'outtmpl': str(raw),
            'quiet': True,
            'no_warnings': True,
            'source_address': '0.0.0.0',
        }
        if PROXY_URL:
            ydl_opts['proxy'] = PROXY_URL

        try:
            target_url = video.get("url") or f"https://www.instagram.com/reel/{video.get('id')}/"
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([target_url])
        except Exception as e:
            raise RuntimeError(f"All download backends failed: {e}")

    if not raw.exists() or raw.stat().st_size < 1000:
        raise RuntimeError("Download failed — file missing or empty")

    # Strip metadata + ensure 9:16 + force IG-compatible encoding
    result = subprocess.run([
        "ffmpeg", "-y", "-i", str(raw),
        "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1",
        "-metadata", "title=", "-metadata", "comment=",
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264", "-profile:v", "main", "-pix_fmt", "yuv420p",
        "-crf", "23", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-movflags", "+faststart",
        str(clean)
    ], capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        logging.error(f"FFmpeg stderr: {result.stderr}")
        raise RuntimeError(f"FFmpeg processing failed: {result.returncode}")

    if not clean.exists():
        raise RuntimeError("FFmpeg output file missing")

    return clean

def upload_to_fileio(video_path: Path) -> str:
    """Upload video to file.io and return the download link."""
    with open(video_path, "rb") as f:
        r = requests.post(
            "https://file.io",
            files={"file": (video_path.name, f, "video/mp4")},
            data={"expires": "1d"},
            timeout=120
        )
    data = r.json()
    # file.io response format: {"success": true, "status": 200, "id": "...", "key": "...", "link": "...", "expiry": "..."}
    link = data.get("link") or data.get("url")
    if not link:
        raise RuntimeError(f"Upload failed: {data}")
    return link

def post_to_instagram(video_url: str, caption: str) -> dict:
    """Post a Reel to Instagram using the Graph API."""
    if not IG_ACCESS_TOKEN or not IG_USER_ID:
        return {"error": "Instagram not configured"}

    # Step 1: Create media container
    r = requests.post(
        f"https://graph.instagram.com/v22.0/{IG_USER_ID}/media",
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": IG_ACCESS_TOKEN,
        },
        timeout=60
    )
    data = r.json()
    if "id" not in data:
        raise RuntimeError(f"Container creation failed: {data}")
    container_id = data["id"]

    # Step 2: Poll for processing completion
    for attempt in range(60):
        time.sleep(10)
        s = requests.get(
            f"https://graph.instagram.com/v22.0/{container_id}",
            params={"fields": "status_code", "access_token": IG_ACCESS_TOKEN},
            timeout=30
        ).json()
        status = s.get("status_code")
        if status == "FINISHED":
            break
        if status == "ERROR":
            raise RuntimeError(f"IG processing error: {s}")
        logging.info(f"IG processing status: {status} (attempt {attempt + 1})")
    else:
        raise RuntimeError("IG processing timeout")

    # Step 3: Publish
    pub = requests.post(
        f"https://graph.instagram.com/v22.0/{IG_USER_ID}/media_publish",
        data={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN},
        timeout=60
    )
    return pub.json()

# ── Caption Builder ──────────────────────────────────────────────────────────

BASE_HASHTAGS = ["#WorldCup", "#FIFA", "#wcdaily", "#Reels", "#Football"]

KEYWORD_HASHTAGS = [
    ("mbappe",    "#Mbappe"),
    ("messi",     "#Messi"),
    ("ronaldo",   "#Ronaldo"),
    ("haaland",   "#Haaland"),
    ("goal",      "#Goal"),
    ("penalty",   "#Penalty"),
    ("final",     "#Final"),
    ("match",     "#Matchday"),
    ("qualifier", "#Qualifiers")
]

def build_caption(caption: str, title: str = "") -> str:
    base = (caption or title or "FIFA World Cup Highlight")[:300]
    tags = list(BASE_HASHTAGS)
    t = base.lower()
    for kw, tag in KEYWORD_HASHTAGS:
        if kw in t and tag not in tags:
            tags.append(tag)
    return f"{base}\n\n{' '.join(tags)}"

# ── Pipeline ───────────────────────────────────────────────────────────────────

async def run_pipeline():
    """Scrapes content, processes, and posts to Instagram."""
    if PAUSED_FLAG_FILE.exists():
        logging.info("Automation paused via flag file.")
        return False
    if get_posted_today() >= DAILY_LIMIT:
        logging.info("Daily posting limit reached.")
        return False

    seen_ids = load_seen()

    # Run blocking instagrapi call in thread pool
    loop = asyncio.get_running_loop()
    try:
        videos = await loop.run_in_executor(None, get_source_videos)
    except Exception as e:
        logging.error(f"Failed to fetch source videos: {e}")
        notify(f"❌ Fetch error: {e}")
        return False

    for vid in videos:
        if vid["id"] in seen_ids:
            continue

        logging.info(f"Processing video: {vid['id']} - {vid['title']}")
        notify(f"📥 Processing: {vid['title']}")

        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                # Run blocking download in thread pool
                clean_path = await loop.run_in_executor(
                    None, partial(download_video, vid, tmpdir)
                )

                # Upload to temporary hosting
                cdn_url = await loop.run_in_executor(
                    None, upload_to_fileio, clean_path
                )

                caption = build_caption(vid["caption"], vid["title"])

                # Post to Instagram
                result = await loop.run_in_executor(
                    None, partial(post_to_instagram, cdn_url, caption)
                )

                if "id" in result:
                    mark_seen(vid["id"])
                    increment_posted_today()
                    notify(f"🚀 Posted successfully: {result['id']}")
                    logging.info(f"Posted: {result['id']}")
                    return True
                else:
                    error_msg = result.get("error", {}).get("message", str(result))
                    notify(f"❌ Post failed: {error_msg}")
                    logging.error(f"Post failed: {result}")

            except Exception as e:
                logging.error(f"Error processing {vid['id']}: {e}")
                notify(f"⚠️ Error on {vid['id']}: {e}")
                continue

    logging.info("Pipeline completed. No new videos posted.")
    return False

# ── Telegram Bot Commands ────────────────────────────────────────────────────

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_ADMIN_ID:
        return
    await update.message.reply_text(
        "🤖 Bot ready.\n"
        "/run — manual pipeline trigger\n"
        "/pause — toggle automation\n"
        "/status — show current state"
    )

async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_ADMIN_ID:
        return
    await update.message.reply_text("⚡ Running pipeline...")
    posted = await run_pipeline()
    if not posted:
        await update.message.reply_text("ℹ️ No new posts. Daily limit reached or no new content.")

async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_ADMIN_ID:
        return
    if PAUSED_FLAG_FILE.exists():
        PAUSED_FLAG_FILE.unlink()
        await update.message.reply_text("▶️ Automation resumed.")
    else:
        PAUSED_FLAG_FILE.touch()
        await update.message.reply_text("⏸️ Automation paused.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_ADMIN_ID:
        return
    posted = get_posted_today()
    paused = PAUSED_FLAG_FILE.exists()
    seen_count = len(load_seen())
    status = (
        f"📊 Status\n"
        f"Paused: {'Yes' if paused else 'No'}\n"
        f"Posted today: {posted}/{DAILY_LIMIT}\n"
        f"Seen videos: {seen_count}"
    )
    await update.message.reply_text(status)

# ── Background Cron Loop ─────────────────────────────────────────────────────

async def cron_loop():
    """Background task that runs the pipeline periodically."""
    while True:
        try:
            await run_pipeline()
        except Exception as e:
            logging.error(f"Cron loop error: {e}")
            notify(f"🔥 Cron error: {e}")
        await asyncio.sleep(CHECK_INTERVAL)

# ── Main Entry Point ─────────────────────────────────────────────────────────

async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("run", run_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    # Start background cron loop
    asyncio.create_task(cron_loop())

    logging.info("Bot starting...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # Keep running until interrupted
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutdown requested.")