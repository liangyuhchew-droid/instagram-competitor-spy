# redeploy trigger
"""
Competitor Spy Agent — Instagram Finance Niche
Tracks mid-tier finance accounts (10k–100k followers)
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
import logging
import argparse
import schedule
from datetime import datetime, timedelta
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
    # Mid-tier finance accounts (10k–100k). Replace with real handles.
    "humphreytalks",         # personal finance, casual tone
    "lyfewithless",          # budgeting / frugality
    "wealthwithsasha",       # investing for beginners
    "andrewtateadvice",      # placeholder — swap for a real one
    "yourmoneycoach",        # placeholder — swap for a real one
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
    """Login to Instagram with session caching to avoid repeated logins."""
    if not INSTAGRAPI_AVAILABLE:
        return None

    ig_user = os.environ.get("IG_USERNAME")
    ig_pass = os.environ.get("IG_PASSWORD")
    if not ig_user or not ig_pass:
        log.warning("IG_USERNAME / IG_PASSWORD not set. Scraping will be skipped.")
        return None

    cl = IGClient()
    cl.delay_range = [2, 5]  # Random delay between requests (be polite)

    if os.path.exists(SESSION_FILE):
        try:
            cl.load_settings(SESSION_FILE)
            cl.login(ig_user, ig_pass)
            log.info("Loaded existing Instagram session.")
            return cl
        except Exception:
            log.warning("Cached session expired. Logging in fresh.")

    try:
        cl.login(ig_user, ig_pass)
        cl.dump_settings(SESSION_FILE)
        log.info("Instagram login successful.")
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

def scrape_account(cl, handle: str) -> list:
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
                "scraped_at": datetime.utcnow().isoformat(),
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


def classify_posts(posts: list, claude: anthropic.Anthropic) -> list:
    """Use Claude to batch-classify post topics."""
    if not posts:
        return posts

    # Build input list
    captions = [
        {"index": i, "caption": p.get("caption_preview", "")[:200]}
        for i, p in enumerate(posts)
    ]

    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",  # Use Haiku for cheap batch classification
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": CLASSIFIER_PROMPT + json.dumps(captions)
            }]
        )

        raw = response.content[0].text.strip()
        # Strip markdown code blocks if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        classifications = json.loads(raw)

        # Map back to posts
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
  "trending_topics": [string],         // top 5 topics getting most engagement right now
  "trending_formats": [string],        // which formats (carousel/reel/etc) are winning
  "content_gaps": [string],            // topics competitors aren't covering but audience wants
  "top_posts_analysis": [             // analyse the top 3 posts
    {{
      "handle": string,
      "topic": string,
      "why_it_worked": string,
      "angle_to_steal": string         // how YOU could do a better version
    }}
  ],
  "avoid": [string],                   // what's getting low engagement
  "weekly_insight": string             // one paragraph summary for the Content Strategist
}}
"""


def analyse_trends(posts: list, claude: anthropic.Anthropic):
    """Run trend analysis on this week's scraped posts."""
    if not posts:
        return None

    # Filter to last 7 days, sort by likes
    cutoff = datetime.utcnow() - timedelta(days=7)
    recent = [
        p for p in posts
        if p.get("posted_at") and datetime.fromisoformat(p["posted_at"].replace("Z", "")) > cutoff
    ]
    recent.sort(key=lambda x: x.get("likes", 0), reverse=True)

    # Pass top 30 to Claude (cost control)
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

def save_posts_to_supabase(posts: list, db: Client) -> int:
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

    today = datetime.utcnow().date().isoformat()
    try:
        # Get existing memory for today
        existing = db.table("agent_memory").select("*").eq("date", today).execute()

        if existing.data:
            # Merge trends into existing memory
            db.table("agent_memory").update({
                "patterns_detected": json.dumps(trends.get("trending_topics", [])),
                "audience_insight": trends.get("weekly_insight", ""),
                "updated_at": datetime.utcnow().isoformat()
            }).eq("date", today).execute()
        else:
            # Create new row for today
            db.table("agent_memory").insert({
                "date": today,
                "patterns_detected": json.dumps(trends.get("trending_topics", [])),
                "audience_insight": trends.get("weekly_insight", ""),
                "updated_at": datetime.utcnow().isoformat()
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
            time.sleep(5)  # Polite delay between accounts
    else:
        log.warning("Instagram client not available. Using mock data for testing.")
        all_posts = _mock_posts()

    if all_posts:
        # Classify topics with Claude
        all_posts = classify_posts(all_posts, claude)

        # Save raw posts to Supabase
        save_posts_to_supabase(all_posts, db)

        # Analyse trends
        trends = analyse_trends(all_posts, claude)
        if trends:
            save_trends_to_supabase(trends, db)
            log.info(f"Trending topics: {trends.get('trending_topics', [])}")
            log.info(f"Weekly insight: {trends.get('weekly_insight', '')[:200]}")

    log.info("Competitor Spy cycle complete.")
    log.info(f"Total posts processed: {len(all_posts)}")


def _mock_posts() -> list:
    """Mock data for testing without Instagram credentials."""
    return [
        {
            "handle": "test_account_1",
            "post_url": "https://www.instagram.com/p/mock1/",
            "caption_preview": "The S&P 500 has made millionaires out of people who just... didn't touch it. Here's exactly how compound interest works in your favour.",
            "likes": 2847,
            "comments": 134,
            "post_type": "carousel",
            "posted_at": (datetime.utcnow() - timedelta(days=1)).isoformat(),
            "scraped_at": datetime.utcnow().isoformat(),
            "topic_detected": None,
        },
        {
            "handle": "test_account_2",
            "post_url": "https://www.instagram.com/p/mock2/",
            "caption_preview": "I paid off $34,000 in debt in 18 months on a $52k salary. Here's the exact budget I used.",
            "likes": 5201,
            "comments": 389,
            "post_type": "carousel",
            "posted_at": (datetime.utcnow() - timedelta(days=2)).isoformat(),
            "scraped_at": datetime.utcnow().isoformat(),
            "topic_detected": None,
        },
        {
            "handle": "test_account_1",
            "post_url": "https://www.instagram.com/p/mock3/",
            "caption_preview": "Hot take: your emergency fund is costing you money if it's sitting in a regular savings account.",
            "likes": 1923,
            "comments": 201,
            "post_type": "reel",
            "posted_at": (datetime.utcnow() - timedelta(days=3)).isoformat(),
            "scraped_at": datetime.utcnow().isoformat(),
            "topic_detected": None,
        },
    ]


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Instagram Competitor Spy Agent")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously on a schedule (every 6 hours)"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run with mock data (no Instagram login needed)"
    )
    args = parser.parse_args()

    if args.test:
        # Override Instagram init to use mock data
        def init_instagram():
            return None
        log.info("Running in TEST mode with mock data.")

    if args.daemon:
        log.info(f"Daemon mode: running every {SCRAPE_INTERVAL_HOURS} hours.")
        run_spy()  # Run immediately on start
        schedule.every(SCRAPE_INTERVAL_HOURS).hours.do(run_spy)
        while True:
            schedule.run_pending()
            time.sleep(60)
    else:
        run_spy()
