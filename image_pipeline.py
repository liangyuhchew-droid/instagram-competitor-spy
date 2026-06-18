"""
Image Pipeline — @levi.cashflow
Generates post images for approved content_briefs that have no image_url.
Uploads each image to Supabase Storage and writes the public URL back to the brief.

Design: clean 1080x1080 quote-card style — big bold hook, pillar accent colour,
handle watermark. No raw JSON rendered.

Run manually: python image_pipeline.py
Run on schedule: python image_pipeline.py --daemon (every 30 min)
"""

import os
import io
import json
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
# Keys must match content_strategist.py CONTENT_PILLARS exactly
PILLAR_STYLES = {
    "wealth-building":  {"bg": "#0A1628", "accent": "#22C55E",  "tag": "WEALTH BUILDING"},
    "investing":        {"bg": "#0D1B2A", "accent": "#3B82F6",  "tag": "INVESTING"},
    "money-mindset":    {"bg": "#1A1A0D", "accent": "#EAB308",  "tag": "MONEY MINDSET"},
    "income-streams":   {"bg": "#1A0D1A", "accent": "#A855F7",  "tag": "INCOME STREAMS"},
    "budgeting":        {"bg": "#0F1A0F", "accent": "#F97316",  "tag": "SMART BUDGETING"},
}
DEFAULT_STYLE = {"bg": "#0D0D0D", "accent": "#D4AF37", "tag": "MONEY TIPS"}

# Fuzzy pillar matching — maps common pillar names to style keys
PILLAR_ALIASES = {
    "wealth": "wealth-building",
    "wealth building": "wealth-building",
    "wealth-building": "wealth-building",
    "invest": "investing",
    "investing": "investing",
    "investing basics": "investing",
    "mindset": "money-mindset",
    "money mindset": "money-mindset",
    "money-mindset": "money-mindset",
    "income": "income-streams",
    "income streams": "income-streams",
    "income-streams": "income-streams",
    "side hustle": "income-streams",
    "side-hustle": "income-streams",
    "budget": "budgeting",
    "budgeting": "budgeting",
    "budgeting hacks": "budgeting",
    "savings": "budgeting",
    "save": "budgeting",
    "debt": "budgeting",
    "debt payoff": "budgeting",
}


def resolve_style(pillar: str) -> dict:
    """Return PILLAR_STYLES entry for a pillar name, with fuzzy matching."""
    key = (pillar or "").lower().strip()
    resolved = PILLAR_ALIASES.get(key)
    if resolved:
        return PILLAR_STYLES[resolved]
    # Partial match fallback
    for alias, style_key in PILLAR_ALIASES.items():
        if alias in key or key in alias:
            return PILLAR_STYLES[style_key]
    return DEFAULT_STYLE


def init_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("Set SUPABASE_URL and SUPABASE_SERVICE_KEY in env")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def ensure_bucket_exists():
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    r = requests.get(f"{SUPABASE_URL}/storage/v1/bucket/{BUCKET}", headers=headers, timeout=10)
    if r.status_code == 200:
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
        log.warning(f"Could not create bucket: {r.status_code} {r.text[:200]}")


def clean_hook(raw: str) -> str:
    """Strip JSON artifacts, brackets, and control chars from hook text."""
    if not raw:
        return ""
    text = raw.strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
            if isinstance(items, list) and items:
                text = str(items[0])
        except Exception:
            text = text.lstrip("[").split(",")[0].strip('"\'[] ')
    for prefix in ["Slide 1:", "Hook slide", "bold text:", "sub-text:", "Sub-text:"]:
        idx = text.find(prefix)
        if idx != -1:
            text = text[idx + len(prefix):].strip(" :—-\"'")
    return text[:160]


def load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf" if bold else "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def wrap_text(draw, text: str, font, max_width: int, max_lines: int = 5) -> list:
    words = text.split()
    lines = []
    line = ""
    for word in words:
        test = (line + " " + word).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            line = test
        else:
            if line:
                lines.append(line)
            line = word
        if len(lines) >= max_lines:
            break
    if line and len(lines) < max_lines:
        lines.append(line)
    return lines


def choose_font_size(text: str, max_width: int, draw):
    for size in [96, 84, 72, 60, 52, 44]:
        font = load_font(size, bold=True)
        lines = wrap_text(draw, text, font, max_width, max_lines=5)
        if len(lines) <= 4:
            return size, font
    return 44, load_font(44, bold=True)


def generate_image(brief: dict) -> bytes:
    pillar = (brief.get("pillar") or "").lower().strip()
    style = resolve_style(pillar)
    bg = style["bg"]
    accent = style["accent"]
    tag_txt = style["tag"]
    raw_hook = brief.get("hook_idea") or brief.get("caption_starter") or "The money rules they never taught you."
    hook = clean_hook(raw_hook)
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    MARGIN = 80
    INNER_W = W - 2 * MARGIN
    BAR_H = 12
    draw.rectangle([0, 0, W, BAR_H], fill=accent)
    font_tag = load_font(22, bold=True)
    TAG_X, TAG_Y = MARGIN, 80
    tag_bbox = draw.textbbox((0, 0), tag_txt, font=font_tag)
    tag_pad_x, tag_pad_y = 22, 10
    tag_w = (tag_bbox[2] - tag_bbox[0]) + tag_pad_x * 2
    tag_h = (tag_bbox[3] - tag_bbox[1]) + tag_pad_y * 2
    draw.rounded_rectangle([TAG_X, TAG_Y, TAG_X + tag_w, TAG_Y + tag_h], radius=tag_h // 2, fill=accent)
    draw.text((TAG_X + tag_pad_x, TAG_Y + tag_pad_y), tag_txt, font=font_tag, fill=bg)
    hook_y_start = TAG_Y + tag_h + 70
    _, font_hook = choose_font_size(hook, INNER_W, draw)
    hook_lines = wrap_text(draw, hook, font_hook, INNER_W, max_lines=5)
    y = hook_y_start
    line_gap = 18
    for ln in hook_lines:
        draw.text((MARGIN, y), ln, font=font_hook, fill="#FFFFFF")
        bbox = draw.textbbox((0, 0), ln, font=font_hook)
        y += (bbox[3] - bbox[1]) + line_gap
    divider_y = max(y + 60, H - 200)
    draw.rectangle([MARGIN, divider_y, W - MARGIN, divider_y + 3], fill=accent)
    font_handle = load_font(30, bold=True)
    handle_y = divider_y + 36
    draw.text((MARGIN, handle_y), "@levi.cashflow", font=font_handle, fill=accent)
    font_cta = load_font(24)
    cta_y = handle_y + 44
    draw.text((MARGIN, cta_y), "Follow for more money moves ↑", font=font_cta, fill="#888888")
    draw.rectangle([0, H - BAR_H, W, H], fill=accent)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


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
    log.error(f"Upload failed for brief {brief_id}: {r.status_code} {r.text[:200]}")
    return None


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
        pillar = brief.get("pillar", "general")
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
            log.info(f"✓ Updated brief {brief_id} with image_url")
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
