"""
Image Pipeline — @levi.smokes
Generates post images for approved content_briefs that have no image_url.
Uploads each image to Supabase Storage and writes the public URL back to the brief.

How it works:
  1. Query content_briefs WHERE status='approved' AND image_url IS NULL
  2. Generate a PIL image using pillar brand colours + hook text
  3. Upload to Supabase Storage bucket 'post-images'
  4. Update content_briefs.image_url with the public CDN URL

Run manually:    python image_pipeline.py
Run on schedule: python image_pipeline.py --daemon  (every 30 min)

Deploy as a Railway service pointing to this file.
Env vars needed: SUPABASE_URL, SUPABASE_SERVICE_KEY
"""

import os
import io
import time
import logging
import argparse
import schedule
import textwrap
import requests
from datetime import datetime, timezone
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BUCKET = "post-images"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

W, H = 1080, 1080

# ── Brand colours per content pillar ──────────────────────────────────────────
PILLAR_STYLES = {
    "lifestyle":       {"bg": "#1A1A1A", "accent": "#D4AF37", "tag": "LIFESTYLE"},
    "education":       {"bg": "#0D1B2A", "accent": "#4FC3F7", "tag": "EDUCATION"},
    "product":         {"bg": "#1C1C1C", "accent": "#FF6B35", "tag": "PRODUCT"},
    "behind-the-scenes": {"bg": "#12121F", "accent": "#A78BFA", "tag": "BEHIND THE SCENES"},
    "engagement":      {"bg": "#1A0A0A", "accent": "#EF4444", "tag": "ENGAGEMENT"},
}
DEFAULT_STYLE = {"bg": "#111111", "accent": "#D4AF37", "tag": "POST"}


# ─────────────────────────────────────────────
# CLIENTS
# ─────────────────────────────────────────────

def init_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("Set SUPABASE_URL and SUPABASE_SERVICE_KEY in env")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def ensure_bucket_exists():
    """Create the storage bucket if it doesn't already exist."""
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    r = requests.get(f"{SUPABASE_URL}/storage/v1/bucket/{BUCKET}", headers=headers, timeout=10)
    if r.status_code == 200:
        log.info(f"Bucket '{BUCKET}' already exists.")
        return
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/bucket",
        headers=headers,
        json={"id": BUCKET, "name": BUCKET, "public": True},
        timeout=10,
    )
    if r.status_code in (200, 201):
        log.info(f"Created bucket '{BUCKET}'.")
    else:
        log.warning(f"Could not create bucket '{BUCKET}': {r.status_code} {r.text[:200]}")


# ─────────────────────────────────────────────
# IMAGE GENERATION
# ─────────────────────────────────────────────

def load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def generate_image(brief: dict) -> bytes:
    """Generate a 1080x1080 PNG for the given content brief. Returns raw PNG bytes."""
    pillar = (brief.get("pillar") or "lifestyle").lower()
    style = PILLAR_STYLES.get(pillar, DEFAULT_STYLE)

    bg = style["bg"]
    accent = style["accent"]
    tag_text = style["tag"]

    hook = brief.get("hook_idea") or brief.get("caption_starter") or "Coming soon..."
    hook = hook[:120]

    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)

    # Border lines
    draw.rectangle([0, 0, W, 10], fill=accent)
    draw.rectangle([0, H - 10, W, H], fill=accent)
    draw.rectangle([60, 60, 66, H - 60], fill=accent)
    draw.rectangle([W - 66, 60, W - 60, H - 60], fill=accent)

    # Pillar tag badge
    font_tag = load_font(22, bold=True)
    tag_x, tag_y = 100, 120
    tag_w = len(tag_text) * 14 + 40
    draw.rounded_rectangle([tag_x, tag_y, tag_x + tag_w, tag_y + 46], radius=23, fill=accent)
    draw.text((tag_x + 20, tag_y + 11), tag_text, font=font_tag, fill=bg)

    # Hook headline (word-wrapped)
    font_big = load_font(72, bold=True)
    font_sub = load_font(34)
    font_handle = load_font(26)

    words = hook.split()
    lines = []
    line = ""
    for w in words:
        test = (line + " " + w).strip()
        bbox = draw.textbbox((0, 0), test, font=font_big)
        if bbox[2] - bbox[0] < W - 200:
            line = test
        else:
            lines.append(line)
            line = w
    lines.append(line)

    y = 230
    for ln in lines:
        draw.text((100, y), ln, font=font_big, fill="#FFFFFF")
        bbox = draw.textbbox((0, 0), ln, font=font_big)
        y += (bbox[3] - bbox[1]) + 14

    # Divider
    y += 24
    draw.rectangle([100, y, W - 100, y + 3], fill=accent)
    y += 30

    # Sub-text snippet
    outline = brief.get("content_outline") or brief.get("angle") or ""
    if outline:
        snippet = outline[:160].replace("\n", " ")
        for ln in textwrap.fill(snippet, width=36).split("\n")[:4]:
            draw.text((100, y), ln, font=font_sub, fill="#CCCCCC")
            bbox = draw.textbbox((0, 0), ln, font=font_sub)
            y += (bbox[3] - bbox[1]) + 10

    # Handle watermark
    draw.text((100, H - 80), "@levi.smokes", font=font_handle, fill=accent)

    # Week label
    week = brief.get("week_of", "")
    if week:
        draw.text((W - 260, H - 80), str(week)[:10], font=font_handle, fill="#555555")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ─────────────────────────────────────────────
# SUPABASE STORAGE UPLOAD
# ─────────────────────────────────────────────

def upload_to_storage(brief_id: str, image_bytes: bytes) -> Optional[str]:
    filename = f"{brief_id}.png"
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{filename}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "image/png",
        "x-upsert": "true",
    }
    r = requests.post(url, headers=headers, data=image_bytes, timeout=30)
    if r.status_code in (200, 201):
        public_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{filename}"
        log.info(f"Uploaded image for brief {brief_id}: {public_url}")
        return public_url
    else:
        log.error(f"Upload failed for brief {brief_id}: {r.status_code} {r.text[:200]}")
        return None


# ─────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────

def run_image_pipeline():
    log.info("=" * 50)
    log.info(f"Image pipeline running at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 50)

    db = init_supabase()
    ensure_bucket_exists()

    result = (
        db.table("content_briefs")
        .select("id, pillar, hook_idea, caption_starter, content_outline, angle, format, week_of")
        .eq("status", "approved")
        .is_("image_url", "null")
        .order("created_at", desc=False)
        .limit(20)
        .execute()
    )
    briefs = result.data or []

    if not briefs:
        log.info("No approved briefs missing image_url. Nothing to do.")
        return

    log.info(f"Found {len(briefs)} brief(s) needing images.")
    success = 0

    for brief in briefs:
        brief_id = brief["id"]
        pillar = brief.get("pillar", "lifestyle")
        log.info(f"Generating image for brief {brief_id} [{pillar}]...")

        try:
            image_bytes = generate_image(brief)
        except Exception as e:
            log.error(f"Image generation failed for {brief_id}: {e}")
            continue

        public_url = upload_to_storage(brief_id, image_bytes)
        if not public_url:
            continue

        try:
            db.table("content_briefs").update({"image_url": public_url}).eq("id", brief_id).execute()
            log.info(f"\u2713 Updated brief {brief_id} with image_url")
            success += 1
        except Exception as e:
            log.error(f"Failed to update brief {brief_id}: {e}")

        time.sleep(0.5)

    log.info(f"Done. {success}/{len(briefs)} briefs updated with image URLs.")
    log.info("=" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Image Pipeline Agent")
    parser.add_argument("--daemon", action="store_true", help="Run every 30 minutes")
    args = parser.parse_args()

    if args.daemon:
        log.info("Daemon mode: running image pipeline every 30 minutes.")
        try:
            run_image_pipeline()
        except Exception as e:
            log.warning(f"Startup run skipped: {e}")
        schedule.every(30).minutes.do(run_image_pipeline)
        while True:
            try:
                schedule.run_pending()
            except Exception as e:
                log.error(f"Pipeline job failed: {e}")
            time.sleep(60)
    else:
        run_image_pipeline()
