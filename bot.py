#!/usr/bin/env python3
import os, subprocess, shutil, tempfile, logging, asyncio, json, requests
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
CHANNEL_URL       = "https://www.youtube.com/@fifa/shorts"
CHECK_INTERVAL    = 1800  # 30 min

def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()

def mark_seen(vid_id):
    seen = load_seen()
    seen.add(vid_id)
    SEEN_FILE.write_text(json.dumps(list(seen)))

def notify(text):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data={"chat_id": TELEGRAM_ADMIN_ID, "text": text},
        timeout=10
    )

def get_latest_shorts():
    cmd = [
        "yt-dlp", CHANNEL_URL,
        "--flat-playlist", "--playlist-items", "1-5",
        "--print", '{"id":"%(id)s","title":"%(title)s","duration":%(duration)s}',
        "--quiet", "--no-warnings"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    videos = []
    for line in result.stdout.strip().splitlines():
        try:
            v = json.loads(line)
            if v.get("duration") and int(v["duration"]) <= 60:
                v["url"] = f"https://www.youtube.com/shorts/{v['id']}"
                videos.append(v)
        except:
            continue
    return videos

def download_and_strip(url, work_dir):
    raw = Path(work_dir) / "raw.mp4"
    clean = Path(work_dir) / "clean.mp4"
    subprocess.run([
        "yt-dlp", url,
        "-o", str(raw),
        "-f", "best[ext=mp4]/best",
        "--quiet", "--no-warnings"
    ], timeout=120)
    subprocess.run([
        "ffmpeg", "-y", "-i", str(raw),
        "-map", "0", "-map_metadata", "-1",
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(clean)
    ], capture_output=True, timeout=120)
    return clean

def upload_to_fileio(video_path):
    with open(video_path, "rb") as f:
        r = requests.post("https://file.io", files={"file": f}, data={"expires": "1d"}, timeout=120)
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
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true",
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
            raise RuntimeError(f"IG processing error: {s}")

    pub = requests.post(
        f"https://graph.instagram.com/v18.0/{IG_USER_ID}/media_publish",
        data={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN},
        timeout=30
    )
    return pub.json()

async def monitor_loop(app):
    await asyncio.sleep(5)
    notify("🤖 WCDAILY Bot started\nMonitoring: " + CHANNEL_URL)
    while True:
        try:
            seen = load_seen()
            videos = get_latest_shorts()
            new = [v for v in videos if v["id"] not in seen]
            if new:
                notify(f"🔔 Found {len(new)} new Short(s) — processing...")
            for video in new:
                work_dir = tempfile.mkdtemp()
                try:
                    notify(f"⬇️ Downloading: {video['title']}")
                    clean = download_and_strip(video["url"], work_dir)
                    notify("☁️ Uploading to temp host...")
                    video_url = upload_to_fileio(clean)
                    caption = f"{video['title']}\n\n⚽ #WorldCup #FIFA #wcdaily #Shorts"
                    notify("📲 Posting to Instagram...")
                    result = post_to_instagram(video_url, caption)
                    if "id" in result:
                        notify(f"✅ Posted to Instagram!\n📹 {video['title']}\n🆔 Post ID: {result['id']}")
                    else:
                        notify(f"⚠️ IG response: {result}")
                    mark_seen(video["id"])
                except Exception as e:
                    notify(f"❌ Failed: {video['title']}\n{str(e)[:200]}")
                finally:
                    shutil.rmtree(work_dir, ignore_errors=True)
                await asyncio.sleep(10)
        except Exception as e:
            notify(f"❌ Monitor error: {str(e)[:200]}")
        await asyncio.sleep(CHECK_INTERVAL)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 WCDAILY Bot running\n"
        f"Monitoring: {CHANNEL_URL}\n"
        f"Checking every 30 min\n\n"
        f"Commands:\n"
        f"/status — check integrations\n"
        f"/check — force check now"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    seen = load_seen()
    await update.message.reply_text(
        f"{'✅' if IG_ACCESS_TOKEN else '❌'} Instagram token\n"
        f"{'✅' if IG_USER_ID else '❌'} Instagram user ID\n"
        f"👁 {len(seen)} videos already posted"
    )

async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Checking now...")
    videos = get_latest_shorts()
    seen = load_seen()
    new = [v for v in videos if v["id"] not in seen]
    await update.message.reply_text(
        f"Found {len(videos)} Shorts, {len(new)} new\n" +
        "\n".join(f"• {v['title']}" for v in videos[:5])
    )

async def on_error(update, ctx):
    if isinstance(ctx.error, Conflict):
        logging.warning("Conflict — shutting down.")
        raise SystemExit(1)
    logging.error(f"Error: {ctx.error}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("check",  cmd_check))
    app.add_error_handler(on_error)

    async def post_init(application):
        asyncio.create_task(monitor_loop(application))

    app.post_init = post_init
    print("🤖 WCDAILY Bot running...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()