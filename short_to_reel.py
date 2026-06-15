#!/usr/bin/env python3
"""
short_to_reel.py
Download a YouTube Short, strip metadata, upload to Instagram as a Reel.
Add this to bot.py or run standalone.

Usage:
  python short_to_reel.py [youtube_shorts_url] [caption]

Or add /reel command to your Telegram bot (see bottom of file).
"""

import os, subprocess, tempfile, shutil, time, requests
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
IG_USER_ID      = os.environ.get("IG_USER_ID", "")

# Where to temporarily store video before upload
WORK_DIR = Path(tempfile.gettempdir()) / "wcdaily_reels"


# ── Step 1: Download + strip metadata ─────────────────────────────────────────

def download_short(url: str, out_dir: Path) -> Path:
    """
    Download YouTube Short via yt-dlp.
    Strips all metadata at download time via postprocessor args.
    Output: clean .mp4, no title/author/description embedded.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / "raw.mp4"
    clean = out_dir / "clean.mp4"

    # Download best quality
    cmd_download = [
        "yt-dlp",
        url,
        "-o", str(raw),
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--quiet",
    ]
    result = subprocess.run(cmd_download, capture_output=True, text=True)
    if result.returncode != 0 or not raw.exists():
        raise RuntimeError(f"Download failed: {result.stderr[-300:]}")

    # Strip ALL metadata using ffmpeg
    # -map_metadata -1  → removes all global metadata (title, artist, comment, etc.)
    # -fflags +bitexact → makes output bit-exact, no encoder fingerprint
    # -map 0            → keep all streams (video + audio)
    cmd_strip = [
        "ffmpeg", "-y",
        "-i", str(raw),
        "-map", "0",
        "-map_metadata", "-1",          # strip all metadata
        "-metadata", "title=",          # blank title
        "-metadata", "comment=",        # blank comment
        "-metadata", "description=",    # blank description
        "-metadata", "artist=",
        "-metadata", "album=",
        "-fflags", "+bitexact",         # no encoder fingerprint
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "fast",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(clean),
    ]
    result2 = subprocess.run(cmd_strip, capture_output=True, text=True)
    if result2.returncode != 0:
        raise RuntimeError(f"Metadata strip failed: {result2.stderr[-300:]}")

    raw.unlink(missing_ok=True)
    return clean


# ── Step 2: Host video temporarily ────────────────────────────────────────────

def upload_to_tmphost(video_path: Path) -> str:
    """
    Upload video to file.io (free, ephemeral, auto-deletes after 1 download).
    Returns a public URL Instagram can pull from.
    
    Alternative: use your Google Drive (make file public, get direct link)
    or any public URL you control.
    """
    with open(video_path, "rb") as f:
        r = requests.post(
            "https://file.io",
            files={"file": f},
            data={"expires": "1d"},  # auto-delete after 1 day
            timeout=120
        )
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"Upload to temp host failed: {data}")
    return data["link"]


# ── Step 3: Post to Instagram as Reel ─────────────────────────────────────────

def post_reel(video_url: str, caption: str) -> dict:
    """
    Post a video URL to Instagram as a Reel via Graph API.
    The video must be publicly accessible (Instagram pulls it server-side).
    """
    if not IG_ACCESS_TOKEN or not IG_USER_ID:
        raise RuntimeError("IG_ACCESS_TOKEN and IG_USER_ID must be set in environment.")

    base = f"https://graph.instagram.com/v18.0/{IG_USER_ID}"

    # Step A: Create media container
    print("  Creating Instagram media container...")
    r = requests.post(f"{base}/media", data={
        "media_type":   "REELS",
        "video_url":    video_url,
        "caption":      caption,
        "share_to_feed": "true",
        "access_token": IG_ACCESS_TOKEN,
    }, timeout=30)
    data = r.json()
    if "id" not in data:
        raise RuntimeError(f"Container creation failed: {data}")
    container_id = data["id"]
    print(f"  Container ID: {container_id}")

    # Step B: Poll until video is processed (usually 10-60s)
    print("  Waiting for Instagram to process video", end="", flush=True)
    for _ in range(30):
        time.sleep(5)
        status_r = requests.get(
            f"https://graph.instagram.com/v18.0/{container_id}",
            params={"fields": "status_code,status", "access_token": IG_ACCESS_TOKEN},
            timeout=15
        )
        status = status_r.json()
        code = status.get("status_code", "")
        print(".", end="", flush=True)
        if code == "FINISHED":
            print(" ✓")
            break
        if code == "ERROR":
            raise RuntimeError(f"Instagram processing error: {status}")
    else:
        raise RuntimeError("Instagram video processing timed out.")

    # Step C: Publish
    print("  Publishing Reel...")
    pub_r = requests.post(f"{base}/media_publish", data={
        "creation_id":  container_id,
        "access_token": IG_ACCESS_TOKEN,
    }, timeout=30)
    result = pub_r.json()
    if "id" not in result:
        raise RuntimeError(f"Publish failed: {result}")
    print(f"  ✅ Published! Post ID: {result['id']}")
    return result


# ── Full pipeline ──────────────────────────────────────────────────────────────

def short_to_reel(url: str, caption: str) -> str:
    """
    Full pipeline: URL → download → strip metadata → upload → Instagram Reel.
    Returns the Instagram post ID.
    """
    work_dir = WORK_DIR / str(int(time.time()))
    try:
        print(f"\n1/3  Downloading & stripping metadata...")
        video_path = download_short(url, work_dir)
        size_mb = video_path.stat().st_size / 1_000_000
        print(f"     Clean video: {size_mb:.1f} MB")

        print(f"\n2/3  Uploading to temp host for Instagram to pull...")
        video_url = upload_to_tmphost(video_path)
        print(f"     URL: {video_url}")

        print(f"\n3/3  Posting to Instagram as Reel...")
        result = post_reel(video_url, caption)

        return result["id"]
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ── Telegram /reel command ─────────────────────────────────────────────────────
# Add this to your bot.py:
#
# from short_to_reel import short_to_reel
#
# async def cmd_reel(update, ctx):
#     if not is_admin(update): return
#     args = ctx.args
#     if len(args) < 1:
#         await update.message.reply_text(
#             "Usage: /reel [youtube_shorts_url] [optional caption]\n\n"
#             "Example:\n/reel https://youtube.com/shorts/xxx Great goal by Mbappe! ⚽ #WorldCup"
#         )
#         return
#     url = args[0]
#     caption = " ".join(args[1:]) if len(args) > 1 else "⚽ #WorldCup #wcdaily"
#     msg = await update.message.reply_text("⬇️ Downloading Short...")
#     try:
#         await msg.edit_text("🧹 Stripping metadata...")
#         post_id = short_to_reel(url, caption)
#         await msg.edit_text(f"✅ Posted to Instagram as Reel!\nPost ID: {post_id}")
#     except Exception as e:
#         await msg.edit_text(f"❌ Error: {e}")
#
# # Register it:
# app.add_handler(CommandHandler("reel", cmd_reel))


# ── Standalone usage ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python short_to_reel.py [url] [caption]")
        sys.exit(1)
    url = sys.argv[1]
    caption = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "⚽ #WorldCup #wcdaily"
    post_id = short_to_reel(url, caption)
    print(f"\n🎉 Done! Instagram post ID: {post_id}")
