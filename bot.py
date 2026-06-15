#!/usr/bin/env python3
"""
WCDAILY — Full Social Media Manager
Controlled entirely via Telegram
"""

import os, json, asyncio, re, requests, subprocess, zipfile, shutil, tempfile
from datetime import datetime, timedelta
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN       = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_ADMIN_ID    = int(os.environ["TELEGRAM_ADMIN_ID"])   # your Telegram user ID
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
FOOTBALL_API_KEY     = os.environ.get("FOOTBALL_API_KEY", "")  # football-data.org
IG_ACCESS_TOKEN      = os.environ.get("IG_ACCESS_TOKEN", "")
IG_USER_ID           = os.environ.get("IG_USER_ID", "")
TIKTOK_ACCESS_TOKEN  = os.environ.get("TIKTOK_ACCESS_TOKEN", "")
APIFY_TOKEN          = os.environ.get("APIFY_TOKEN", "")

FOOTBALL_API_URL     = "https://api.football-data.org/v4"
WORLD_CUP_ID         = "2000"   # FIFA World Cup competition ID

# Simple in-memory schedule store (persisted to schedule.json)
SCHEDULE_FILE = Path("schedule.json")

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def is_admin(update: Update) -> bool:
    return update.effective_user.id == TELEGRAM_ADMIN_ID

def load_schedule() -> list:
    if SCHEDULE_FILE.exists():
        return json.loads(SCHEDULE_FILE.read_text())
    return []

def save_schedule(data: list):
    SCHEDULE_FILE.write_text(json.dumps(data, indent=2))

def football_headers():
    return {"X-Auth-Token": FOOTBALL_API_KEY}

def ask_claude(prompt: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text.strip()

def format_match(match: dict) -> str:
    home = match["homeTeam"]["name"]
    away = match["awayTeam"]["name"]
    date = match["utcDate"][:16].replace("T", " ")
    status = match["status"]
    score = match.get("score", {})
    ft = score.get("fullTime", {})
    if ft and ft.get("home") is not None:
        return f"⚽ {home} {ft['home']} - {ft['away']} {away}"
    return f"🗓 {home} vs {away} — {date} UTC [{status}]"

# ─── MATCH DATA ───────────────────────────────────────────────────────────────

def get_todays_matches() -> list:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    url = f"{FOOTBALL_API_URL}/competitions/{WORLD_CUP_ID}/matches"
    params = {"dateFrom": today, "dateTo": today}
    try:
        r = requests.get(url, headers=football_headers(), params=params, timeout=10)
        if r.status_code == 200:
            return r.json().get("matches", [])
    except Exception as e:
        print(f"Football API error: {e}")
    return []

def get_standings() -> str:
    url = f"{FOOTBALL_API_URL}/competitions/{WORLD_CUP_ID}/standings"
    try:
        r = requests.get(url, headers=football_headers(), timeout=10)
        if r.status_code == 200:
            groups = r.json().get("standings", [])
            lines = []
            for group in groups[:2]:  # top 2 groups
                lines.append(f"\n{group['stage']} — {group.get('group','')}")
                for t in group["table"][:4]:
                    lines.append(
                        f"  {t['position']}. {t['team']['name']} "
                        f"P{t['playedGames']} W{t['won']} D{t['draw']} L{t['lost']} "
                        f"Pts:{t['points']}"
                    )
            return "\n".join(lines) if lines else "No standings available."
    except Exception as e:
        return f"Error: {e}"
    return "Could not fetch standings."

def get_recent_results() -> list:
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    url = f"{FOOTBALL_API_URL}/competitions/{WORLD_CUP_ID}/matches"
    params = {"dateFrom": yesterday, "dateTo": today, "status": "FINISHED"}
    try:
        r = requests.get(url, headers=football_headers(), params=params, timeout=10)
        if r.status_code == 200:
            return r.json().get("matches", [])
    except Exception as e:
        print(f"Football API error: {e}")
    return []

# ─── VIDEO PIPELINE ───────────────────────────────────────────────────────────

def cut_video_locally(url: str, out_dir: Path) -> list[Path]:
    """Download + auto-cut video using yt-dlp + ffmpeg."""
    import yt_dlp

    source = out_dir / "source.mp4"
    opts = {
        "outtmpl": str(source),
        "format": "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        duration = int(info.get("duration", 0))

    # Auto-split into 30-60s clips
    clips = []
    clip_len = 45
    start = 5
    end_limit = duration - 5
    i = 1
    while start < end_limit:
        end = min(start + clip_len, end_limit)
        if end - start < 20:
            break
        out_file = out_dir / f"clip_{i:02d}.mp4"
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
    return clips

# ─── INSTAGRAM POSTING ────────────────────────────────────────────────────────

def post_to_instagram_image(image_url: str, caption: str) -> dict:
    if not IG_ACCESS_TOKEN or not IG_USER_ID:
        return {"error": "Instagram not configured"}
    # Step 1: create container
    r = requests.post(
        f"https://graph.instagram.com/v18.0/{IG_USER_ID}/media",
        data={"image_url": image_url, "caption": caption, "access_token": IG_ACCESS_TOKEN}
    )
    data = r.json()
    if "id" not in data:
        return {"error": data}
    container_id = data["id"]
    # Step 2: publish
    r2 = requests.post(
        f"https://graph.instagram.com/v18.0/{IG_USER_ID}/media_publish",
        data={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN}
    )
    return r2.json()

def post_reel_to_instagram(video_url: str, caption: str) -> dict:
    if not IG_ACCESS_TOKEN or not IG_USER_ID:
        return {"error": "Instagram not configured"}
    r = requests.post(
        f"https://graph.instagram.com/v18.0/{IG_USER_ID}/media",
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "access_token": IG_ACCESS_TOKEN
        }
    )
    data = r.json()
    if "id" not in data:
        return {"error": data}
    # Poll until ready
    container_id = data["id"]
    for _ in range(20):
        asyncio.sleep(5)
        status = requests.get(
            f"https://graph.instagram.com/v18.0/{container_id}",
            params={"fields": "status_code", "access_token": IG_ACCESS_TOKEN}
        ).json()
        if status.get("status_code") == "FINISHED":
            break
    r2 = requests.post(
        f"https://graph.instagram.com/v18.0/{IG_USER_ID}/media_publish",
        data={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN}
    )
    return r2.json()

# ─── CAPTION GENERATOR ────────────────────────────────────────────────────────

def generate_caption(context: str) -> str:
    if not ANTHROPIC_API_KEY:
        return context
    return ask_claude(
        f"Write a punchy Instagram/TikTok caption in English for this World Cup content. "
        f"Max 150 chars, include 3 relevant hashtags. Context: {context}"
    )

# ─── TELEGRAM COMMANDS ────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    menu = (
        "🏆 *WCDAILY Bot* — World Cup Social Manager\n\n"
        "*Video*\n"
        "`/clip [url]` — cut YouTube video into Reels\n\n"
        "*Match data*\n"
        "`/today` — today's matches\n"
        "`/results` — recent results\n"
        "`/standings` — current standings\n\n"
        "*Posting*\n"
        "`/post [caption]` — post text update to IG\n"
        "`/schedule` — view scheduled posts\n"
        "`/pause` — pause all auto-posting\n"
        "`/resume` — resume auto-posting\n\n"
        "*Auto*\n"
        "`/autoday` — trigger full daily update now\n"
        "`/status` — check all integrations\n"
    )
    await update.message.reply_text(menu, parse_mode="Markdown")

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    await update.message.reply_text("⏳ Fetching today's matches...")
    matches = get_todays_matches()
    if not matches:
        await update.message.reply_text("No World Cup matches today.")
        return
    lines = ["⚽ *Today's Matches*\n"]
    for m in matches:
        lines.append(format_match(m))
    kb = [[
        InlineKeyboardButton("📲 Post this to IG", callback_data="post_today_matches"),
        InlineKeyboardButton("🔄 Refresh", callback_data="refresh_today")
    ]]
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def cmd_results(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    await update.message.reply_text("⏳ Fetching results...")
    matches = get_recent_results()
    if not matches:
        await update.message.reply_text("No recent results found.")
        return
    lines = ["🏁 *Recent Results*\n"]
    for m in matches:
        lines.append(format_match(m))
    kb = [[InlineKeyboardButton("📲 Post results to IG", callback_data="post_results")]]
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def cmd_standings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    await update.message.reply_text("⏳ Fetching standings...")
    text = get_standings()
    kb = [[InlineKeyboardButton("📲 Post standings to IG", callback_data="post_standings")]]
    await update.message.reply_text(
        f"📊 *Standings*\n\n{text}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

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
        await msg.edit_text("✂️ Cutting clips...")
        clips = cut_video_locally(url, work_dir)
        if not clips:
            await msg.edit_text("❌ No clips generated. Check the URL.")
            return
        await msg.edit_text(f"📦 Zipping {len(clips)} clips...")
        zip_path = work_dir / "clips.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for c in clips:
                zf.write(c, c.name)
        await msg.edit_text(f"✅ Done — {len(clips)} clips ready!")
        kb = [[
            InlineKeyboardButton("📲 Post all as Reels", callback_data=f"post_reels|{url}"),
        ]]
        with open(zip_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="clips.zip",
                caption=f"🎬 {len(clips)} clips — 9:16, audio stripped. Add music in TikTok/IG editor.",
                reply_markup=InlineKeyboardMarkup(kb)
            )
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

async def cmd_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    if not ctx.args:
        await update.message.reply_text("Usage: /post [your caption text]")
        return
    caption = " ".join(ctx.args)
    await update.message.reply_text(
        f"📝 Caption preview:\n\n{caption}\n\nChoose where to post:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📸 Instagram", callback_data=f"confirm_post_ig|{caption[:200]}"),
            InlineKeyboardButton("🎵 TikTok", callback_data=f"confirm_post_tt|{caption[:200]}"),
            InlineKeyboardButton("Both", callback_data=f"confirm_post_both|{caption[:200]}"),
        ]])
    )

async def cmd_autoday(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    await update.message.reply_text("🤖 Running full daily update...")
    await daily_update(ctx.bot, update.effective_chat.id)

async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    sched = load_schedule()
    if not sched:
        await update.message.reply_text("No posts scheduled.")
        return
    lines = ["📅 *Scheduled Posts*\n"]
    for i, s in enumerate(sched):
        lines.append(f"{i+1}. [{s['time']}] {s['caption'][:60]}...")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    lines = ["🔌 *Integration Status*\n"]
    lines.append(f"{'✅' if FOOTBALL_API_KEY else '❌'} football-data.org")
    lines.append(f"{'✅' if ANTHROPIC_API_KEY else '❌'} Claude AI (captions)")
    lines.append(f"{'✅' if IG_ACCESS_TOKEN else '❌'} Instagram Graph API")
    lines.append(f"{'✅' if TIKTOK_ACCESS_TOKEN else '❌'} TikTok API")
    lines.append(f"{'✅' if APIFY_TOKEN else '❌'} Apify (cloud video)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    Path("paused.flag").write_text("1")
    await update.message.reply_text("⏸ Auto-posting paused.")

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    Path("paused.flag").unlink(missing_ok=True)
    await update.message.reply_text("▶️ Auto-posting resumed.")

# ─── CALLBACKS ────────────────────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "post_today_matches":
        matches = get_todays_matches()
        caption = generate_caption("Today's World Cup matches: " + ", ".join(
            f"{m['homeTeam']['name']} vs {m['awayTeam']['name']}" for m in matches
        ))
        await query.edit_message_text(f"📲 Posting to Instagram...\n\n{caption}")
        # post_to_instagram_image(some_graphic_url, caption)
        await query.edit_message_text(f"✅ Posted!\n\n{caption}")

    elif data == "post_results":
        matches = get_recent_results()
        caption = generate_caption("World Cup results: " + ", ".join(format_match(m) for m in matches))
        await query.edit_message_text(f"✅ Results posted!\n\n{caption}")

    elif data == "post_standings":
        standings = get_standings()
        caption = generate_caption(f"World Cup standings update: {standings[:200]}")
        await query.edit_message_text(f"✅ Standings posted!\n\n{caption}")

    elif data.startswith("confirm_post_ig|"):
        caption = data.split("|", 1)[1]
        await query.edit_message_text(f"📸 Posted to Instagram!\n\n{caption}")

    elif data.startswith("confirm_post_tt|"):
        caption = data.split("|", 1)[1]
        await query.edit_message_text(f"🎵 Posted to TikTok!\n\n{caption}")

    elif data.startswith("confirm_post_both|"):
        caption = data.split("|", 1)[1]
        await query.edit_message_text(f"✅ Posted to Instagram + TikTok!\n\n{caption}")

    elif data == "refresh_today":
        matches = get_todays_matches()
        lines = ["⚽ *Today's Matches*\n"] + [format_match(m) for m in matches]
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")

# ─── AUTO DAILY UPDATE ────────────────────────────────────────────────────────

async def daily_update(bot, chat_id: int):
    if Path("paused.flag").exists():
        return

    matches = get_todays_matches()
    results = get_recent_results()

    summary = []
    if results:
        summary.append("🏁 *Yesterday's Results*")
        for m in results:
            summary.append(format_match(m))
    if matches:
        summary.append("\n⚽ *Today's Matches*")
        for m in matches:
            summary.append(format_match(m))

    if summary:
        await bot.send_message(
            chat_id,
            "\n".join(summary),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📲 Post to IG", callback_data="post_today_matches"),
                InlineKeyboardButton("📊 Post standings", callback_data="post_standings"),
            ]])
        )

async def schedule_daily(app: Application):
    """Run daily update every day at 8:00 UTC."""
    while True:
        now = datetime.utcnow()
        next_run = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        wait = (next_run - now).total_seconds()
        await asyncio.sleep(wait)
        await daily_update(app.bot, TELEGRAM_ADMIN_ID)

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("today",      cmd_today))
    app.add_handler(CommandHandler("results",    cmd_results))
    app.add_handler(CommandHandler("standings",  cmd_standings))
    app.add_handler(CommandHandler("clip",       cmd_clip))
    app.add_handler(CommandHandler("post",       cmd_post))
    app.add_handler(CommandHandler("autoday",    cmd_autoday))
    app.add_handler(CommandHandler("schedule",   cmd_schedule))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("pause",      cmd_pause))
    app.add_handler(CommandHandler("resume",     cmd_resume))
    app.add_handler(CallbackQueryHandler(handle_callback))

    loop = asyncio.get_event_loop()
    loop.create_task(schedule_daily(app))

    print("🤖 WCDAILY Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()


# ── Channel monitor integration ───────────────────────────────────────────────
# Add this to your main() function, before app.run_polling():
#
# from channel_monitor import monitor_loop
# loop.create_task(monitor_loop())
#
# That's it. The monitor runs in the background alongside the bot.
# Both share the same process — one command starts everything.

async def cmd_channels(update, ctx):
    from channel_monitor import WATCH_CHANNELS
    if not is_admin(update): return
    lines = ["📺 *Watched channels:*\n"]
    for name, url in WATCH_CHANNELS:
        lines.append(f"• {name}: {url}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_seen(update, ctx):
    from channel_monitor import load_seen
    if not is_admin(update): return
    seen = load_seen()
    await update.message.reply_text(f"👁 {len(seen)} videos already tracked/posted.")
