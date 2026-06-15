#!/usr/bin/env python3
import os, json, asyncio, re, requests, subprocess, zipfile, shutil, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

TELEGRAM_TOKEN      = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_ADMIN_ID   = int(os.environ["TELEGRAM_ADMIN_ID"])
IG_ACCESS_TOKEN     = os.environ.get("IG_ACCESS_TOKEN", "")
IG_USER_ID          = os.environ.get("IG_USER_ID", "")
SCHEDULE_FILE       = Path("schedule.json")

def is_admin(update: Update) -> bool:
    return update.effective_user.id == TELEGRAM_ADMIN_ID

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    menu = (
        "🏆 *WCDAILY Bot* — World Cup Social Manager\n\n"
        "*Video*\n"
        "`/clip [url]` — cut YouTube video into Reels\n"
        "`/reel [url]` — Short → strip metadata → post to IG\n\n"
        "*Posting*\n"
        "`/post [caption]` — post text update\n"
        "`/pause` — pause all auto-posting\n"
        "`/resume` — resume auto-posting\n\n"
        "`/status` — check all integrations\n"
    )
    await update.message.reply_text(menu, parse_mode="Markdown")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    lines = ["🔌 *Integration Status*\n"]
    lines.append(f"{'✅' if IG_ACCESS_TOKEN else '❌'} Instagram Graph API")
    lines.append(f"{'✅' if IG_USER_ID else '❌'} Instagram User ID")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    Path("paused.flag").write_text("1")
    await update.message.reply_text("⏸ Auto-posting paused.")

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    Path("paused.flag").unlink(missing_ok=True)
    await update.message.reply_text("▶️ Auto-posting resumed.")

async def cmd_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    if not ctx.args:
        await update.message.reply_text("Usage: /post [caption]")
        return
    caption = " ".join(ctx.args)
    await update.message.reply_text(f"📝 Caption:\n\n{caption}\n\nPosting to Instagram...")

async def cmd_clip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /clip [YouTube URL]")
        return
    url = args[0]
    msg = await update.message.reply_text("⬇️ Downloading video...")
    work_dir = Path(tempfile.mkdtemp(prefix="wcdaily_"))
    try:
        import yt_dlp
        source = work_dir / "source.mp4"
        opts = {
            "outtmpl": str(source),
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "quiet": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            duration = int(info.get("duration", 0))

        await msg.edit_text("✂️ Cutting clips...")
        clips = []
        clip_len = 45
        start = 5
        end_limit = duration - 5
        i = 1
        while start < end_limit:
            end = min(start + clip_len, end_limit)
            if end - start < 20:
                break
            out_file = work_dir / f"clip_{i:02d}.mp4"
            cmd = [
                "ffmpeg", "-y",
                "-i", str(source),
                "-ss", str(start), "-t", str(end - start),
                "-vf", "crop=ih*9/16:ih",
                "-an", "-c:v", "libx264", "-crf", "26", "-preset", "fast",
                str(out_file)
            ]
            result = subprocess.run(cmd, capture_output=True)
            if result.returncode == 0 and out_file.exists():
                clips.append(out_file)
            start = end + 1
            i += 1

        if not clips:
            await msg.edit_text("❌ No clips generated.")
            return

        await msg.edit_text(f"📦 Zipping {len(clips)} clips...")
        zip_path = work_dir / "clips.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for c in clips:
                zf.write(c, c.name)

        with open(zip_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="clips.zip",
                caption=f"🎬 {len(clips)} clips ready — 9:16, audio stripped."
            )
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

async def cmd_reel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    if not ctx.args:
        await update.message.reply_text("Usage: /reel [YouTube Shorts URL] [optional caption]")
        return
    url = ctx.args[0]
    caption = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else "⚽ #WorldCup #wcdaily #Shorts"
    msg = await update.message.reply_text("⬇️ Downloading Short...")
    work_dir = Path(tempfile.mkdtemp(prefix="wcdaily_reel_"))
    try:
        from short_to_reel import download_short, upload_to_tmphost, post_reel
        await msg.edit_text("🧹 Stripping metadata...")
        video_path = download_short(url, work_dir)
        await msg.edit_text("☁️ Uploading...")
        video_url = upload_to_tmphost(video_path)
        await msg.edit_text("📲 Posting to Instagram...")
        result = post_reel(video_url, caption)
        await msg.edit_text(f"✅ Posted to Instagram!\nPost ID: {result.get('id')}")
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("pause",   cmd_pause))
    app.add_handler(CommandHandler("resume",  cmd_resume))
    app.add_handler(CommandHandler("post",    cmd_post))
    app.add_handler(CommandHandler("clip",    cmd_clip))
    app.add_handler(CommandHandler("reel",    cmd_reel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    print("🤖 WCDAILY Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()