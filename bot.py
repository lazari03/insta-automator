#!/usr/bin/env python3
import os, subprocess, shutil, tempfile, logging, asyncio, json, requests, datetime, base64
from pathlib import Path
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update
from telegram.error import Conflict

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_ADMIN_ID = int(os.environ["TELEGRAM_ADMIN_ID"])
IG_ACCESS_TOKEN   = os.environ.get("IG_ACCESS_TOKEN", "")
IG_USER_ID        = os.environ.get("IG_USER_ID", "")
IG_SESSION_B64    = os.environ.get("IG_SESSION", "")
SEEN_FILE         = Path("seen.json")
POSTED_TODAY_FILE = Path("posted_today.json")
SESSION_FILE      = Path("session.json")
CHECK_INTERVAL    = 1800
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

# ── Session setup ─────────────────────────────────────────────────────────────

def setup_session():
    """Decode base64 session from env var and write to disk."""
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
    proxy = os.environ.get("PROXY_URL", "")
    if proxy:
        cl.set_proxy(proxy)
        logging.info(f"Using proxy: {proxy[:30]}...")
    cl.load_settings(str(SESSION_FILE))
    cl.login(os.environ.get("IG_USERNAME", ""),
             os.environ.get("IG_PASSWORD", ""))
    return cl

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

# ── Fetch from @fifaworldcup via instagrapi ───────────────────────────────────

def get_source_videos() -> list:
    """Fetch recent Reels from @fifaworldcup using instagrapi session."""
    cl = get_instagrapi_client()
    user_id = cl.user_id_from_username(SOURCE_ACCOUNT)
    medias  = cl.user_medias(user_id, amount=20)
    videos  = []
    for m in medias:
        if m.media_type in (2, 8):  # 2=video, 8=album
            caption = m.caption_text or ""
            title   = caption[:80] if caption else "FIFA World Cup"
            if not is_wc_related(caption + title):
                continue
            videos.append({
                "id":      str(m.pk),
                "title":   title,
                "caption": caption,
                "url":     str(m.video_url) if m.video_url else "",
                "pk":      str(m.pk),
            })
    return videos

# ── Video processing ──────────────────────────────────────────────────────────

def download_video(video: dict, work_dir: str) -> Path:
    raw   = Path(work_dir) / "raw.mp4"
    clean = Path(work_dir) / "clean.mp4"

    # Fetch fresh video info from instagrapi to get non-expired URL
    try:
        cl  = get_instagrapi_client()
        pk  = video.get("pk") or video.get("id")
        # Get fresh media info with current CDN URL
        info = cl.media_info(pk)
        fresh_url = str(info.video_url)
        logging.info(f"Fresh URL: {fresh_url[:80]}")
        r = requests.get(fresh_url, timeout=120, stream=True,
                        headers={"User-Agent": "Instagram 275.0.0.27.98 Android"})
        if r.status_code == 200:
            with open(raw, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
    except Exception as e:
        logging.warning(f"Fresh URL download failed: {e}")

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

def build_caption(caption: str, title: str = "") -> str:
    base = (caption or title or "FIFA World Cup Highlight")[:300]
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
        notify("☁️ Uploading...")
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
        notify(f"❌ Failed: {str(e)[:200]}")
        return False
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

# ── Monitor loop ──────────────────────────────────────────────────────────────

async def monitor_loop(app):
    await asyncio.sleep(5)
    notify(
        "🤖 WCDAILY Bot started\n"
        f"Source: @{SOURCE_ACCOUNT}\n"
        f"Daily limit: {DAILY_LIMIT} posts\n"
        f"Checking every {CHECK_INTERVAL//60} min"
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
                await asyncio.sleep((midnight - now).total_seconds())
                continue

            videos = get_source_videos()
            seen   = load_seen()
            new    = [v for v in videos if v["id"] not in seen]

            if new:
                notify(f"🔔 {len(new)} new WC video(s) found — posting...")
                for video in new:
                    if get_posted_today() >= DAILY_LIMIT:
                        notify(f"⏸ Daily limit hit.")
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
        f"Source: @{SOURCE_ACCOUNT}\n"
        f"Daily limit: {DAILY_LIMIT} posts\n\n"
        "Commands:\n"
        "/check — scan now\n"
        "/next — post next in queue\n"
        "/status — stats\n"
        "/today — posts today\n"
        "/pause — pause\n"
        "/resume — resume"
    )

async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🔍 Scanning @{SOURCE_ACCOUNT}...")
    try:
        videos = get_source_videos()
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
        videos = get_source_videos()
        seen   = load_seen()
        queue  = [v for v in videos if v["id"] not in seen]
        if not queue:
            await update.message.reply_text("✅ Queue empty.")
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
                    f"📋 {len(queue)-1} left"
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
    session_ok = SESSION_FILE.exists() or bool(IG_SESSION_B64)
    await update.message.reply_text(
        f"{'✅' if session_ok else '❌'} Instagram session\n"
        f"{'✅' if IG_ACCESS_TOKEN else '❌'} Instagram token\n"
        f"{'✅' if IG_USER_ID else '❌'} Instagram user ID\n"
        f"📊 {get_posted_today()}/{DAILY_LIMIT} posted today\n"
        f"👁 {len(seen)} total posted\n"
        f"{'⏸ PAUSED' if Path('paused.flag').exists() else '▶️ Running'}"
    )

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📊 Posted today: {get_posted_today()}/{DAILY_LIMIT}")

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

def main():
    setup_session()
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