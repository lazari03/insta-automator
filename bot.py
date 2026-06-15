#!/usr/bin/env python3
import os, subprocess, zipfile, shutil, tempfile, logging
from pathlib import Path
from telegram import Update
from telegram.ext import (
    Application, CommandHandler,
    CallbackQueryHandler, ContextTypes
)
from telegram.error import Conflict

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_ADMIN_ID = int(os.environ["TELEGRAM_ADMIN_ID"])
IG_ACCESS_TOKEN   = os.environ.get("IG_ACCESS_TOKEN", "")
IG_USER_ID        = os.environ.get("IG_USER_ID", "")

def is_admin(update: Update) -> bool:
    return update.effective_user.id == TELEGRAM_ADMIN_ID

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    await update.message.reply_text(
        "🏆 *WCDAILY Bot*\n\n"
        "`/clip [url]` — cut YouTube video into clips\n"
        "`/reel [url] [caption]` — Short → post to IG\n"
        "`/status` — check integrations\n"
        "`/pause` / `/resume` — toggle auto-posting",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    await update.message.reply_text(
        f"{'✅' if IG_ACCESS_TOKEN else '❌'} Instagram token\n"
        f"{'✅' if IG_USER_ID else '❌'} Instagram user ID"
    )

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    Path("paused.flag").write_text("1")
    await update.message.reply_text("⏸ Paused.")

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    Path("paused.flag").unlink(missing_ok=True)
    await update.message.reply_text("▶️ Resumed.")

async def cmd_clip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    if not ctx.args:
        await update.message.reply_text("Usage: /clip [YouTube URL]")
        return
    url = ctx.args[0]
    msg = await update.message.reply_text("⬇️ Downloading...")
    work_dir = Path(tempfile.mkdtemp())
    try:
        import yt_dlp
        source = work_dir / "source.mp4"
        with yt_dlp.YoutubeDL({
            "outtmpl": str(source),
            "format": "best[ext=mp4]/best",
            "quiet": True
        }) as ydl:
            info = ydl.extract_info(url, download=True)
            duration = int(info.get("duration", 0))

        await msg.edit_text("✂️ Cutting clips...")
        clips = []
        start, i = 5, 1
        while start < duration - 5:
            end = min(start + 45, duration - 5)
            if end - start < 20: break
            out = work_dir / f"clip_{i:02d}.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-i", str(source),
                "-ss", str(start), "-t", str(end - start),
                "-vf", "crop=ih*9/16:ih",
                "-an", "-c:v", "libx264", "-crf", "26", "-preset", "fast",
                str(out)
            ], capture_output=True)
            if out.exists(): clips.append(out)
            start = end + 1
            i += 1

        if not clips:
            await msg.edit_text("❌ No clips generated.")
            return

        zip_path = work_dir / "clips.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for c in clips: zf.write(c, c.name)

        with open(zip_path, "rb") as f:
            await update.message.reply_document(f, filename="clips.zip",
                caption=f"🎬 {len(clips)} clips — 9:16, no audio.")
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ {e}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

async def cmd_reel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    if not ctx.args:
        await update.message.reply_text("Usage: /reel [url] [caption]")
        return
    url = ctx.args[0]
    caption = " ".join(ctx.args[1:]) or "⚽ #WorldCup #wcdaily #Shorts"
    msg = await update.message.reply_text("⬇️ Downloading Short...")
    work_dir = Path(tempfile.mkdtemp())
    try:
        from short_to_reel import download_short, upload_to_tmphost, post_reel
        await msg.edit_text("🧹 Stripping metadata...")
        video_path = download_short(url, work_dir)
        await msg.edit_text("☁️ Uploading...")
        video_url = upload_to_tmphost(video_path)
        await msg.edit_text("📲 Posting to Instagram...")
        result = post_reel(video_url, caption)
        await msg.edit_text(f"✅ Posted! ID: {result.get('id')}")
    except Exception as e:
        await msg.edit_text(f"❌ {e}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    if isinstance(ctx.error, Conflict):
        logging.warning("Conflict detected — another instance running, shutting down.")
        raise SystemExit(1)
    logging.error(f"Error: {ctx.error}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pause",  cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("clip",   cmd_clip))
    app.add_handler(CommandHandler("reel",   cmd_reel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(on_error)
    print("🤖 WCDAILY Bot running...")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()