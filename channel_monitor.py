#!/usr/bin/env python3
"""
channel_monitor.py
Monitors YouTube channels for new Shorts every 30 min.
When a new Short is found → downloads → strips metadata → posts to Instagram.
No AI API needed — captions built from video title + description + keywords.
"""

import os, json, asyncio, time, subprocess, shutil, tempfile, requests
from pathlib import Path
from datetime import datetime
from short_to_reel import download_short, upload_to_tmphost, post_reel

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_ADMIN_ID = int(os.environ["TELEGRAM_ADMIN_ID"])

WATCH_CHANNELS = [
    ("FIFA",       "https://www.youtube.com/@FIFA"),
    ("Goal",       "https://www.youtube.com/@goal"),
    ("CBS Sports", "https://www.youtube.com/@CBSSports"),
]

CHECK_INTERVAL_MINUTES = 30
SEEN_FILE = Path("seen_videos.json")

# ── Seen tracking ─────────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()

def mark_seen(video_id: str):
    seen = load_seen()
    seen.add(video_id)
    SEEN_FILE.write_text(json.dumps(list(seen)))

# ── Fetch latest Shorts ───────────────────────────────────────────────────────

def get_latest_shorts(channel_url: str, max_videos: int = 5) -> list:
    cmd = [
        "yt-dlp",
        f"{channel_url}/shorts",
        "--flat-playlist",
        "--playlist-items", f"1-{max_videos}",
        "--print", '{"id":"%(id)s","title":"%(title)s","duration":%(duration)s,"description":"%(description)s"}',
        "--no-warnings",
        "--quiet",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    videos = []
    for line in result.stdout.strip().splitlines():
        try:
            v = json.loads(line)
            if v.get("duration") and int(v["duration"]) <= 60:
                v["url"] = f"https://www.youtube.com/shorts/{v['id']}"
                videos.append(v)
        except Exception:
            continue
    return videos

# ── Caption builder — no AI ───────────────────────────────────────────────────

BASE_TAGS = "#WorldCup #Football #wcdaily #Shorts #FIFA2026"

KEYWORD_TAGS = {
    "goal":       "#Goal #Gol #GoalAlert",
    "mbappe":     "#Mbappe #KylianMbappe",
    "ronaldo":    "#Ronaldo #CR7",
    "messi":      "#Messi #LeoMessi",
    "neymar":     "#Neymar",
    "haaland":    "#Haaland",
    "final":      "#WorldCupFinal #Final",
    "penalty":    "#Penalty",
    "red card":   "#RedCard",
    "free kick":  "#FreeKick",
    "freekick":   "#FreeKick",
    "assist":     "#Assist",
    "hat trick":  "#HatTrick",
    "save":       "#Save #Goalkeeper",
    "highlights": "#Highlights",
    "brazil":     "#Brazil #Selecao",
    "argentina":  "#Argentina",
    "france":     "#France #LesBleues",
    "england":    "#England #ThreeLions",
    "germany":    "#Germany",
    "spain":      "#Spain #LaRoja",
    "portugal":   "#Portugal",
    "morocco":    "#Morocco #AtlasLions",
    "japan":      "#Japan #Samurai",
}

def build_caption(title: str, description: str) -> str:
    # First 2 non-empty lines of description as body
    desc_lines = [l.strip() for l in description.splitlines() if l.strip()]
    desc_body = " ".join(desc_lines[:2])[:200] if desc_lines else ""

    # Avoid repeating title if description starts with it
    if desc_body and desc_body.lower().startswith(title.lower()[:30]):
        caption_body = desc_body
    elif desc_body:
        caption_body = f"{title}\n{desc_body}"
    else:
        caption_body = title

    # Match keywords from title + description
    combined = (title + " " + description).lower()
    extra_tags = []
    for keyword, tags in KEYWORD_TAGS.items():
        if keyword in combined:
            extra_tags.append(tags)
            if len(extra_tags) >= 4:
                break

    all_tags = BASE_TAGS
    if extra_tags:
        all_tags += " " + " ".join(extra_tags)

    return f"{caption_body}\n\n⚽ {all_tags}"[:2200]

# ── Telegram notify ───────────────────────────────────────────────────────────

def notify(message: str):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data={"chat_id": TELEGRAM_ADMIN_ID, "text": message, "parse_mode": "Markdown"},
        timeout=10
    )

# ── Process one Short ─────────────────────────────────────────────────────────

def process_and_post(video: dict, channel_name: str):
    title       = video["title"]
    url         = video["url"]
    video_id    = video["id"]
    description = video.get("description", "")

    print(f"\n[{datetime.utcnow().strftime('%H:%M:%S')}] New Short: {title}")
    notify(f"🔔 New Short from *{channel_name}*\n`{title}`\nProcessing...")

    work_dir = Path(tempfile.mkdtemp(prefix="wcdaily_"))
    try:
        print("  1/3 Downloading + stripping metadata...")
        video_path = download_short(url, work_dir)
        size_mb = video_path.stat().st_size / 1_000_000
        print(f"      {size_mb:.1f} MB — clean")

        print("  2/3 Building caption...")
        caption = build_caption(title, description)
        print(f"      {caption[:80]}...")

        print("  3/3 Uploading + posting to Instagram...")
        video_url = upload_to_tmphost(video_path)
        result = post_reel(video_url, caption)

        mark_seen(video_id)
        notify(
            f"✅ *Posted to Instagram!*\n"
            f"📹 {title}\n"
            f"📝 {caption[:120]}...\n"
            f"🆔 {result.get('id', '')}"
        )
        print(f"  ✅ Done! Post ID: {result.get('id')}")

    except Exception as e:
        print(f"  ❌ Error: {e}")
        notify(f"❌ *Failed*\n{title}\n`{str(e)[:200]}`")

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

# ── Main loop ─────────────────────────────────────────────────────────────────

async def monitor_loop():
    print(f"🔍 Monitor started — checking every {CHECK_INTERVAL_MINUTES} min")
    notify(
        f"🤖 *WCDAILY Monitor started*\n"
        f"Channels: {', '.join(n for n, _ in WATCH_CHANNELS)}\n"
        f"Interval: every {CHECK_INTERVAL_MINUTES} min"
    )

    while True:
        seen = load_seen()
        print(f"\n[{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC] Checking...")

        for channel_name, channel_url in WATCH_CHANNELS:
            try:
                print(f"  {channel_name}...")
                shorts = get_latest_shorts(channel_url)
                new = [v for v in shorts if v["id"] not in seen]
                print(f"  {len(shorts)} Shorts found, {len(new)} new")
                for video in new:
                    process_and_post(video, channel_name)
                    await asyncio.sleep(10)
            except Exception as e:
                print(f"  ❌ {channel_name}: {e}")

        print(f"  Sleeping {CHECK_INTERVAL_MINUTES} min...")
        await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    asyncio.run(monitor_loop())
