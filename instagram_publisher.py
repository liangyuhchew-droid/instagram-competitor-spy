"""
Instagram Publisher Agent — @levi.cashflow Personal Finance
Publishes approved content_briefs to Instagram twice daily: 6am and 12pm SGT.
Uses Instagram Graph API to create and publish carousel/image posts.
Updates content_briefs status to 'published' after posting.

NOTE: Instagram Graph API requires image URLs hosted publicly.
Images are uploaded to a staging bucket (or use a Canva/S3 URL stored in brief).

Run manually: python instagram_publisher.py
Run on schedule: python instagram_publisher.py --daemon
"""

import os
import json
import logging
import argparse
import schedule
import time
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v21.0"

# Posts per day slots (SGT = UTC+8, so 6am SGT = 22:00 UTC prev day, 12pm SGT = 04:00 UTC)
PUBLISH_SLOTS_UTC = ["22:00", "04:00"]  # corresponds to 6am and 12pm SGT

# ─────────────────────────────────────────────
# CLIENTS
# ─────────────────────────────────────────────

def init_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise ValueError("Set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env")
    return create_client(url, key)

def get_ig_credentials() -> tuple[str, str]:
    ig_account_id = os.environ.get("INSTAGRAM_ACCOUNT_ID")
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    if not ig_account_id or not access_token:
        raise ValueError("Set INSTAGRAM_ACCOUNT_ID and INSTAGRAM_ACCESS_TOKEN in env")
    return ig_account_id, access_token

# ─────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────

def fetch_next_approved_brief(db: Client) -> Optional[dict]:
    """
    Get the next approved brief that hasn't been published yet.
    Priority: highest hook_score first.
    """
    try:
        result = (
            db.table("content_briefs")
            .select("id, pillar, caption, hook_idea, format, image_url, hook_score, week_of")
            .eq("status", "approved")
            .is_("published_url", "null")
            .order("hook_score", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if rows:
            log.info(f"Found brief to publish: [{rows[0]['pillar']}] score={rows[0].get('hook_score')}")
        else:
            log.info("No approved briefs ready to publish.")
        return rows[0] if rows else None
    except Exception as e:
        log.error(f"Failed to fetch brief: {e}")
        return None

# ─────────────────────────────────────────────
# CATCH-UP SCHEDULING LOGIC
# ─────────────────────────────────────────────

def get_last_published_time(db: Client) -> Optional[datetime]:
    """Get the most recent published_at timestamp from content_briefs."""
    try:
        result = (
            db.table("content_briefs")
            .select("published_at")
            .eq("status", "published")
            .order("published_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if rows and rows[0].get("published_at"):
            ts = rows[0]["published_at"].replace("Z", "+00:00")
            return datetime.fromisoformat(ts)
        return None
    except Exception as e:
        log.error(f"Failed to fetch last published time: {e}")
        return None

def should_catch_up(last_published: Optional[datetime]) -> bool:
    """
    Check if a scheduled window was missed since the last publish.
    Scheduled slots: 22:00 UTC (6am SGT) and 04:00 UTC (12pm SGT).
    Looks back up to 3 days to catch any missed window.
    """
    now_utc = datetime.now(timezone.utc)

    if last_published is None:
        log.info("No prior publish found — will publish now.")
        return True

    slot_hours = [22, 4]  # UTC hours for each daily slot

    for days_back in range(3):
        for hour in slot_hours:
            slot = (now_utc - timedelta(days=days_back)).replace(
                hour=hour, minute=0, second=0, microsecond=0
            )
            if last_published < slot <= now_utc:
                log.info(
                    f"Missed scheduled slot: {slot.strftime('%Y-%m-%d %H:%M UTC')} "
                    f"(last published: {last_published.strftime('%Y-%m-%d %H:%M UTC')})"
                )
                return True

    log.info(
        f"No missed windows. Last published: {last_published.strftime('%Y-%m-%d %H:%M UTC')}. "
        f"Waiting for next scheduled slot."
    )
    return False

# ─────────────────────────────────────────────
# INSTAGRAM GRAPH API PUBLISHING
# ─────────────────────────────────────────────

def create_single_image_container(ig_account_id: str, access_token: str, image_url: str, caption: str) -> Optional[str]:
    """Step 1: Create a media container for a single image post."""
    url = f"{GRAPH_BASE}/{ig_account_id}/media"
    payload = {
        "image_url": image_url,
        "caption": caption,
        "access_token": access_token,
    }
    resp = requests.post(url, data=payload, timeout=30)
    resp.raise_for_status()
    container_id = resp.json().get("id")
    log.info(f"Created media container: {container_id}")
    return container_id

def create_carousel_item_container(ig_account_id: str, access_token: str, image_url: str) -> Optional[str]:
    """Create a carousel item container (no caption on individual slides)."""
    url = f"{GRAPH_BASE}/{ig_account_id}/media"
    payload = {
        "image_url": image_url,
        "is_carousel_item": True,
        "access_token": access_token,
    }
    resp = requests.post(url, data=payload, timeout=30)
    resp.raise_for_status()
    return resp.json().get("id")

def create_carousel_container(ig_account_id: str, access_token: str, item_ids: list[str], caption: str) -> Optional[str]:
    """Create the parent carousel container."""
    url = f"{GRAPH_BASE}/{ig_account_id}/media"
    payload = {
        "media_type": "CAROUSEL",
        "children": ",".join(item_ids),
        "caption": caption,
        "access_token": access_token,
    }
    resp = requests.post(url, data=payload, timeout=30)
    resp.raise_for_status()
    container_id = resp.json().get("id")
    log.info(f"Created carousel container: {container_id}")
    return container_id

def publish_container(ig_account_id: str, access_token: str, container_id: str) -> Optional[str]:
    """Step 2: Publish the container. Returns the media ID of the live post."""
    url = f"{GRAPH_BASE}/{ig_account_id}/media_publish"
    payload = {
        "creation_id": container_id,
        "access_token": access_token,
    }
    time.sleep(5)
    resp = requests.post(url, data=payload, timeout=30)
    resp.raise_for_status()
    media_id = resp.json().get("id")
    log.info(f"Published! Media ID: {media_id}")
    return media_id

def get_post_permalink(media_id: str, access_token: str) -> Optional[str]:
    """Get the permalink of a published post."""
    url = f"{GRAPH_BASE}/{media_id}"
    params = {"fields": "permalink", "access_token": access_token}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("permalink")

def publish_brief(brief: dict, ig_account_id: str, access_token: str) -> Optional[str]:
    """Publish a content brief to Instagram."""
    caption = brief.get("caption", "")
    image_url = brief.get("image_url")
    post_format = brief.get("format", "carousel")

    if not image_url:
        log.warning(f"No image_url for brief {brief['id']} — skipping publish.")
        return None

    image_urls = []
    if image_url.startswith("["):
        try:
            image_urls = json.loads(image_url)
        except Exception:
            image_urls = [image_url]
    else:
        image_urls = [image_url]

    if post_format == "carousel" and len(image_urls) > 1:
        log.info(f"Publishing carousel with {len(image_urls)} slides...")
        item_ids = []
        for url in image_urls[:10]:
            item_id = create_carousel_item_container(ig_account_id, access_token, url)
            if item_id:
                item_ids.append(item_id)
            time.sleep(1)
        if not item_ids:
            log.error("No carousel items created.")
            return None
        container_id = create_carousel_container(ig_account_id, access_token, item_ids, caption)
    else:
        log.info("Publishing single image post...")
        container_id = create_single_image_container(ig_account_id, access_token, image_urls[0], caption)

    if not container_id:
        return None

    media_id = publish_container(ig_account_id, access_token, container_id)
    if not media_id:
        return None

    permalink = get_post_permalink(media_id, access_token)
    return permalink

# ─────────────────────────────────────────────
# SUPABASE UPDATE
# ─────────────────────────────────────────────

def mark_as_published(brief_id: str, permalink: str, db: Client):
    """Update the brief row with published status and URL."""
    db.table("content_briefs").update({
        "status": "published",
        "published_url": permalink,
        "published_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", brief_id).execute()
    log.info(f"Marked brief {brief_id} as published: {permalink}")

# ─────────────────────────────────────────────
# MAIN PUBLISH CYCLE
# ─────────────────────────────────────────────

def run_publisher():
    log.info("=" * 50)
    log.info(f"Publisher running at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 50)

    db = init_supabase()
    try:
        ig_account_id, access_token = get_ig_credentials()
    except ValueError as e:
        log.warning(f"Instagram credentials not configured yet — skipping cycle: {e}")
        return

    brief = fetch_next_approved_brief(db)
    if not brief:
        log.info("Nothing to publish right now.")
        return

    permalink = publish_brief(brief, ig_account_id, access_token)
    if permalink:
        mark_as_published(brief["id"], permalink, db)
        log.info(f"✓ Published [{brief['pillar']}]: {permalink}")
    else:
        log.warning(f"✗ Publish failed for [{brief.get('pillar')}] — check image_url and token.")

    log.info("=" * 50)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Instagram Publisher Agent")
    parser.add_argument("--daemon", action="store_true", help="Publish at 6am and 12pm SGT daily")
    args = parser.parse_args()

    if args.daemon:
        log.info("Daemon mode: publishing at 22:00 UTC (6am SGT) and 04:00 UTC (12pm SGT) daily.")

        # Smart catch-up: publish immediately if a scheduled window was missed since last post
        try:
            db = init_supabase()
            last_published = get_last_published_time(db)
            if should_catch_up(last_published):
                log.info("Catch-up publish triggered (missed window or first run).")
                run_publisher()
            else:
                log.info("Skipping startup publish — already posted within current window.")
        except Exception as e:
            log.warning(f"Catch-up check failed: {e}")

        # Schedule twice daily (UTC)
        schedule.every().day.at("22:00").do(run_publisher)  # 6am SGT
        schedule.every().day.at("04:00").do(run_publisher)  # 12pm SGT

        while True:
            try:
                schedule.run_pending()
            except Exception as e:
                log.error(f"Scheduled publish job failed: {e}")
            time.sleep(60)
    else:
        run_publisher()
