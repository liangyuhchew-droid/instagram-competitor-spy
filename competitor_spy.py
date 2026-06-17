"""  # v2 — timezone-aware datetimes + posted_at column
Competitor Spy Agent — Instagram Smoking / Lifestyle Niche
Tracks mid-tier smoking, cigar, and hookah lifestyle accounts (10k–100k followers)
Stores data to Supabase for the Content Strategist to consume

Stack: Python 3.11+ · instagrapi · anthropic · supabase-py · schedule

Install dependencies:
    pip install instagrapi anthropic supabase python-dotenv schedule

Run manually:    python competitor_spy.py
Run on schedule: python competitor_spy.py --daemon  (checks every 6 hours)
Or deploy as a Railway cron job (recommended — add to your existing Railway project)
"""

import os
import time
import json
import base64
import logging
import argparse
import schedule
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
from supabase import create_client, Client
from dotenv import load_dotenv

# ─────────────────────────────────────────────
# Try to import instagrapi; fall back to mock
# ─────────────────────────────────────────────
try:
    from instagrapi import Client as IGClient
    from instagrapi.exceptions import LoginRequired, RateLimitError, ChallengeRequired
    INSTAGRAPI_AVAILABLE = True
except ImportError:
    INSTAGRAPI_AVAILABLE = False
    print("⚠️  instagrapi not installed. Run: pip install instagrapi")

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG — edit these
# ─────────────────────────────────────────────
COMPETITOR_HANDLES = [
    # Mid-tier smoking / cigar / lifestyle accounts (10k–100k). Verify follower counts periodically.
    "cigarsandleisure",      # cigar lifestyle, aesthetic content
    "stogieguys",            # cigar reviews and culture
    "premiumcigarlife",      # premium cigar lifestyle
    "hookahsocial",          # hookah / shisha lifestyle
    "smokelifestyle",        # general smoking lifestyle content
]

POSTS_PER_ACCOUNT = 12          # How many recent posts to scrape per account
MIN_LIKES_TO_STORE = 100        # Ignore posts below this threshold
SCRAPE_INTERVAL_HOURS = 6       # How often to run in daemon mode
SESSION_FILE = ".ig_session.json"  # Cached login session

# ─────────────────────────────────────────────
# INITIALISE CLIENTS
# ─────────────────────────────────────────────

def init_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise ValueError("Set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env")
    return create_client(url, key)


def init_instagram() -> Optional["IGClient"]:
    """Login to Instagram using a trusted session from env var or local cache."""
    if not INSTAGRAPI_AVAILABLE:
        return None

    ig_user = os.environ.get("IG_USERNAME")
    ig_pass = os.environ.get("IG_PASSWORD")
    if not ig_user or not ig_pass:
        log.warning("IG_USERNAME / IG_PASSWORD not set. Scraping will be skipped.")
        return None

    cl = IGClient()
    cl.delay_range = [2, 5]  # Random delay between requests (be polite)

    # ── Priority 1: Load trusted session from IG_SESSION_B64 env var ──
    ig_session_b64 = os.environ.get("IG_SESSION_B64")
    if ig_session_b64:
        try:
            session_json = base64.b64decode(ig_session_b64).decode("utf-8")
            session_data = json.loads(session_json)
            cl.set_settings(session_data)
            cl.login(ig_user, ig_pass)
            log.info("Loaded trusted Instagram session from IG_SESSION_B64.")
            return cl
        except Exception as e:
            log.warning(f"Failed to load session from IG_SESSION_B64: {e}. Trying fresh login.")

    # ── Priority 2: Load from local session file ──
    if os.path.exists(SESSION_FILE):
        try:
            cl.load_settings(SESSION_FILE)
            cl.login(ig_user, ig_pass)
            log.info("Loaded existing Instagram session from file.")
            return cl
        except Exception:
            log.warning("Cached session expired. Logging in fresh.")

    # ── Priority 3: Fresh login (may trigger ChallengeRequired on new IPs) ──
    try:
        cl.login(ig_user, ig_pass)
        cl.dump_settings(SESSION_FILE)
        log.info("Instagram fresh login successful.")
        return cl
    except ChallengeRequired:
        log.warning(
            "Instagram requires identity verification (ChallengeRequired). "
            "This happens on new IPs. Open the Instagram app and approve the login, "
            "then Railway will retry on the next 6-hour cycle. Skipping scrape this cycle."
        )
        return None
    except LoginRequired:
        log.error("Instagram credentials rejected. Check IG_USERNAME / IG_PASSWORD.")
        return None
    except Exception as e:
        log.error(f"Instagram login failed: {e}. Skipping scrape this cycle.")
        return None


def init_anthropic() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("Set ANTHROPIC_API_KEY in .env")
    return anthropic.Anthropic(api_key=api_key)


# ─────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────

def scrape_account(cl: "IGClient", handle: str) -> list[dict]:
    """Scrape recent posts from one account."""
    posts = []
    try:
        user_id = cl.user_id_from_username(handle)
        medias = cl.user_medias(user_id, amount=POSTS_PER_ACCOUNT)
        time.sleep(3)  # Polite delay between accounts

        for media in medias:
            likes = media.like_count or 0
            comments = media.comment_count or 0

            if likes < MIN_LIKES_TO_STORE:
                continue

            caption = ""
            if media.caption_text:
                caption = media.caption_text[:500]  # Truncate long captions

            post = {
                "handle": handle,
                "post_url": f"https://www.instagram.com/p/{media.code}/",
                "caption_preview": caption[:200],
                "likes": likes,
                "comments": comments,
                "post_type": _map_media_type(media.media_type),
                "posted_at": media.taken_at.isoformat() if media.taken_at else None,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "topic_detected": None,  # Filled in by classify_posts()
            }
            posts.append(post)

        log.info(f"@{handle}: scraped {len(posts)} posts above threshold.")
    except RateLimitError:
        log.error(f"Rate limited on @{handle}. Sleeping 10 minutes.")
        time.sleep(600)
    except LoginRequired:
        log.error("Instagram session expired. Delete .ig_session.json and restart.")
        raise
    except Exception as e:
        log.error(f"Failed to scrape @{handle}: {e}")

    return posts


def _map_media_type(media_type: int) -> str:
    mapping = {1: "single_image", 2: "carousel", 8: "carousel"}
    return mapping.get(media_type, "reel")


# ─────────────────────────────────────────────
# TOPIC CLASSIFIER (Claude)
# ─────────────────────────────────────────────

CLASSIFIER_PROMPT = """You are a content classifier for personal finance Instagram posts.

Given a list of post captions, classify each one into a topic from this list:
- investing basics
- stock market
- crypto
- budgeting hacks
- debt payoff
- side hustle
- money mindset
- real estate
- retirement / FIRE
- tax tips
- market news
- financial mistake / story
- other

Return ONLY a JSON array with one object per post, in the same order as input:
[{"index": 0, "topic": "string", "confidence": "high"|"medium"|"low"}, ...]

Posts to classify:
"""


def classify_posts(posts: list[dict], claude: anthropic.Anthropic) -> list[dict]:
    """Use Claude to batch-classify post topics."""
    if not posts:
        return posts

    captions = [
        {"index": i, "caption": p.get("caption_preview", "")[:200]}
        for i, p in enumerate(posts)
    ]

    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": CLASSIFIER_PROMPT + json.dumps(captions)
            }]
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        classifications = json.loads(raw)

        for item in classifications:
            idx = item.get("index", -1)
            if 0 <= idx < len(posts):
                posts[idx]["topic_detected"] = item.get("topic", "other")

        log.info(f"Classified {len(classifications)} posts via Claude Haiku.")

    except Exception as e:
        log.error(f"Classification failed: {e}. Topics left as None.")

    return posts


# ─────────────────────────────────────────────
# TREND ANALYSER (Claude)
# ─────────────────────────────────────────────

TREND_PROMPT = """You are a trend analyst for a personal finance Instagram account.

Here is scraped data from competitor finance accounts (last 7 days, sorted by likes):

{posts_json}

Analyse this data and return a JSON object:
{{
  "trending_topics": [string],
  "trending_formats": [string],
  "content_gaps": [string],
  "top_posts_analysis": [
    {{
      "handle": string,
      "topic": string,
      "why_it_worked": string,
      "angle_to_steal": string
    }}
  ],
  "avoid": [string],
  "weekly_insight": string
}}
"""


def analyse_trends(posts: list[dict], claude: anthropic.Anthropic) -> Optional[dict]:
    """Run trend analysis on this week's scraped posts."""
    if not posts:
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    recent = [
        p for p in posts
        if p.get("posted_at") and datetime.fromisoformat(p["posted_at"]) > cutoff
    ]
    recent.sort(key=lambda x: x.get("likes", 0), reverse=True)

    top_posts = recent[:30]
    if not top_posts:
        log.info("No recent posts to analyse for trends.")
        return None

    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": TREND_PROMPT.format(
                    posts_json=json.dumps(top_posts, default=str, indent=2)
                )
            }]
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        trends = json.loads(raw)
        log.info("Trend analysis complete.")
        return trends

    except Exception as e:
        log.error(f"Trend analysis failed: {e}")
        return None


# ─────────────────────────────────────────────
# SUPABASE STORAGE
# ─────────────────────────────────────────────

def save_posts_to_supabase(posts: list[dict], db: Client) -> int:
    """Upsert posts into competitor_posts table. Returns count saved."""
    if not posts:
        return 0

    saved = 0
    for post in posts:
        try:
            db.table("competitor_posts").upsert(
                post,
                on_conflict="post_url"
            ).execute()
            saved += 1
        except Exception as e:
            log.warning(f"Failed to save post {post.get('post_url')}: {e}")

    log.info(f"Saved {saved}/{len(posts)} posts to Supabase.")
    return saved


def save_trends_to_supabase(trends: dict, db: Client):
    """Save trend analysis to agent_memory table (merged with existing)."""
    if not trends:
        return

    today = datetime.now(timezone.utc).date().isoformat()
    try:
        existing = db.table("agent_memory").select("*").eq("date", today).execute()

        if existing.data:
            db.table("agent_memory").update({
                "patterns_detected": json.dumps(trends.get("trending_topics", [])),
                "audience_insight": trends.get("weekly_insight", ""),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("date", today).execute()
        else:
            db.table("agent_memory").insert({
                "date": today,
                "patterns_detected": json.dumps(trends.get("trending_topics", [])),
                "audience_insight": trends.get("weekly_insight", ""),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).execute()

        log.info("Trend data saved to agent_memory.")
    except Exception as e:
        log.error(f"Failed to save trends: {e}")


# ─────────────────────────────────────────────
# MAIN RUN LOOP
# ─────────────────────────────────────────────

def run_spy():
    """One full scrape + classify + analyse cycle."""
    log.info("=" * 50)
    log.info("Competitor Spy starting...")
    log.info("=" * 50)

    db = init_supabase()
    claude = init_anthropic()
    ig = init_instagram()

    all_posts = []

    if ig:
        for handle in COMPETITOR_HANDLES:
            log.info(f"Scraping @{handle}...")
            posts = scrape_account(ig, handle)
            all_posts.extend(posts)
            time.sleep(5)
    else:
        log.warning("Instagram client not available. Using mock data for testing.")
        all_posts = _mock_posts()

    if all_posts:
        all_posts = classify_posts(all_posts, claude)
        save_posts_to_supabase(all_posts, db)
        trends = analyse_trends(all_posts, claude)
        if trends:
            save_trends_to_supabase(trends, db)
            log.info(f"Trending topics: {trends.get('trending_topics', [])}")
            log.info(f"Weekly insight: {trends.get('weekly_insight', '')[:200]}")

    log.info("Competitor Spy cycle complete.")
    log.info(f"Total posts processed: {len(all_posts)}")


def _mock_posts() -> list[dict]:
    """Mock data for testing without Instagram credentials."""
    return [
        {
            "handle": "test_account_1",
            "post_url": "https://www.instagram.com/p/mock1/",
            "caption_preview": "The S&P 500 has made millionaires out of people who just... didn't touch it.",
            "likes": 2847,
            "comments": 134,
            "post_type": "carousel",
            "posted_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "topic_detected": None,
        },
        {
            "handle": "test_account_2",
            "post_url": "https://www.instagram.com/p/mock2/",
            "caption_preview": "I paid off $34,000 in debt in 18 months on a $52k salary.",
            "likes": 5201,
            "comments": 389,
            "post_type": "carousel",
            "posted_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "topic_detected": None,
        },
        {
            "handle": "test_account_1",
            "post_url": "https://www.instagram.com/p/mock3/",
            "caption_preview": "Hot take: your emergency fund is costing you money in a regular savings account.",
            "likes": 1923,
            "comments": 201,
            "post_type": "reel",
            "posted_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "topic_detected": None,
        },
    ]


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Instagram Competitor Spy Agent")
    parser.add_argument("--daemon", action="store_true", help="Run continuously on a schedule")
    parser.add_argument("--test", action="store_true", help="Run with mock data")
    args = parser.parse_args()

    if args.test:
        def init_instagram():
            return None
        log.info("Running in TEST mode with mock data.")

    if args.daemon:
        log.info(f"Daemon mode: running every {SCRAPE_INTERVAL_HOURS} hours.")
        run_spy()
        schedule.every(SCRAPE_INTERVAL_HOURS).hours.do(run_spy)
        while True:
            schedule.run_pending()
            time.sleep(60)
    else:
        run_spy()
