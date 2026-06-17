"""
Instagram Publisher Agent -- @levi.smokes Personal Finance
Publishes approved content_briefs to Instagram twice daily: 6am and 12pm SGT.
Uses Instagram Graph API to create and publish carousel/image posts.
Updates content_briefs status to 'published' after posting.

Run manually:    python instagram_publisher.py
Run on schedule: python instagram_publisher.py --daemon
"""

import os
import json
import logging
import argparse
import schedule
import time
import requests
from datetime import datetime, timezone
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

# 6am SGT = 22:00 UTC prev day, 12pm SGT = 04:00 UTC
PUBLISH_SLOTS_UTC = ["22:00", "04:00"]


def init_supabase():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise ValueError("Set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env")
    return create_client(url, key)


def get_ig_credentials():
    ig_account_id = os.environ.get("INSTAGRAM_ACCOUNT_ID")
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    if not ig_account_id or not access_token:
        raise ValueError("Set INSTAGRAM_ACCOUNT_ID and INSTAGRAM_ACCESS_TOKEN in env")
    return ig_account_id, access_token


def fetch_next_approved_brief(db):
    """Get the next approved brief that hasn't been published yet. Priority: highest hook_score."""
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


def create_single_image_container(ig_account_id, access_token, image_url, caption):
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


def create_carousel_item_container(ig_account_id, access_token, image_url):
    url = f"{GRAPH_BASE}/{ig_account_id}/media"
    payload = {
        "image_url": image_url,
        "is_carousel_item": True,
        "access_token": access_token,
    }
    resp = requests.post(url, data=payload, timeout=30)
    resp.raise_for_status()
    return resp.json().get("id")


def create_carousel_container(ig_account_id, access_token, item_ids, caption):
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


def publish_container(ig_account_id, access_token, container_id):
    url = f"{GRAPH_BASE}/{ig_account_id}/media_publish"
    payload = {
        "creation_id": container_id,
        "access_token": access_token,
    }
    time.sleep(5)  # Wait for container to be ready
    resp = requests.post(url, data=payload, timeout=30)
    resp.raise_for_status()
    media_id = resp.json().get("id")
    log.info(f"Published! Media ID: {media_id}")
    return media_id


def get_post_permalink(media_id, access_token):
    url = f"{GRAPH_BASE}/{media_id}"
    params = {"fields": "permalink", "access_token": access_token}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("permalink")


def publish_brief(brief, ig_account_id, access_token):
    caption = brief.get("caption", "")
    image_url = brief.get("image_url")
    post_format = brief.get("format", "carousel")

    if not image_url:
        log.warning(f"No image_url for brief {brief['id']} -- skipping. Add image URLs first.")
        return None

    # Parse image_url -- could be JSON array for carousel
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
        for url in image_urls[:10]:  # IG max 10 slides
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

    return get_post_permalink(media_id, access_token)


def mark_as_published(brief_id, permalink, db):
    db.table("content_briefs").update({
        "status": "published",
        "published_url": permalink,
        "published_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", brief_id).execute()
    log.info(f"Marked brief {brief_id} as published: {permalink}")


def run_publisher():
    log.info("=" * 50)
    log.info(f"Publisher running at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 50)

    db = init_supabase()
    ig_account_id, access_token = get_ig_credentials()

    brief = fetch_next_approved_brief(db)
    if not brief:
        log.info("Nothing to publish right now.")
        return

    permalink = publish_brief(brief, ig_account_id, access_token)
    if permalink:
        mark_as_published(brief["id"], permalink, db)
        log.info(f"Published [{brief['pillar']}]: {permalink}")
    else:
        log.warning(f"Publish failed for [{brief.get('pillar')}] -- check image_url and token.")

    log.info("=" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Instagram Publisher Agent")
    parser.add_argument("--daemon", action="store_true", help="Publish at 6am and 12pm SGT daily")
    args = parser.parse_args()

    if args.daemon:
        log.info("Daemon mode: publishing at 22:00 UTC (6am SGT) and 04:00 UTC (12pm SGT) daily.")
        run_publisher()
        schedule.every().day.at("22:00").do(run_publisher)  # 6am SGT
        schedule.every().day.at("04:00").do(run_publisher)  # 12pm SGT
        while True:
            schedule.run_pending()
            time.sleep(60)
    else:
        run_publisher()
