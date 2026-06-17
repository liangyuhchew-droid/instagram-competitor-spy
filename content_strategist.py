"""
Content Strategist Agent — @levi.smokes Personal Finance
Reads competitor post data + trend memory from Supabase.
Generates 5 weekly content briefs (one per pillar) using Claude Sonnet.
Writes briefs to Supabase `content_briefs` table.

Run manually:    python content_strategist.py
Run on schedule: python content_strategist.py --daemon  (runs every Monday 9am)
Deploy alongside competitor_spy.py on Railway.
"""

import os
import json
import logging
import argparse
import schedule
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

CONTENT_PILLARS = [
    "investing basics",
    "budgeting hacks",
    "money mindset",
    "side hustle",
    "debt payoff",
]

ACCOUNT_VOICE = """
Account: @levi.smokes (personal finance for young Singaporeans / Southeast Asians)
Tone: Casual, direct, slightly sarcastic — like a smart friend who learned money the hard way
Audience: 20-35 year olds in Singapore/SEA who want to build wealth but find finance intimidating
Style: Carousels and Reels. Hook-first. Real examples over theory. Singapore context when relevant.
Avoid: Boring "top 5 tips" energy. Generic advice. Anything that sounds like a textbook.
"""

BRIEF_PROMPT = """You are a content strategist for a personal finance Instagram account.

Account voice:
{account_voice}

Here are the top-performing competitor posts from the last 7 days:
{competitor_posts}

Here is the weekly trend insight from competitor analysis:
{weekly_insight}

Generate a detailed content brief for the pillar: **{pillar}**

Return a JSON object with exactly these fields:
{{
  "pillar": "{pillar}",
  "hook_idea": "The opening line / hook (scroll-stopping — question, hot take, or surprising stat)",
  "format": "carousel",
  "angle": "The unique angle that makes this different from generic content",
  "content_outline": [
    "Slide 1: ...",
    "Slide 2: ...",
    "Slide 3: ...",
    "Slide 4: ...",
    "Slide 5: ...",
    "Slide 6: CTA"
  ],
  "caption_starter": "First 2 lines of caption (hook before the more cutoff)",
  "why_it_will_work": "1-2 sentences: what competitor data shows this will perform",
  "estimated_reach_tier": "high"
}}

Make hook_idea and angle specific to Singapore/SEA context where possible.
Return ONLY the JSON object, no other text.
"""


def init_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise ValueError("Set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env")
    return create_client(url, key)


def init_anthropic() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("Set ANTHROPIC_API_KEY in .env")
    return anthropic.Anthropic(api_key=api_key)


def fetch_top_competitor_posts(db: Client, days: int = 7, limit: int = 20) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        result = (
            db.table("competitor_posts")
            .select("handle, caption_preview, likes, comments, post_type, posted_at, topic_detected, post_url")
            .gte("posted_at", cutoff)
            .order("likes", desc=True)
            .limit(limit)
            .execute()
        )
        posts = result.data or []
        log.info(f"Fetched {len(posts)} top competitor posts from last {days} days.")
        return posts
    except Exception as e:
        log.error(f"Failed to fetch competitor posts: {e}")
        return []


def fetch_weekly_insight(db: Client) -> str:
    try:
        result = (
            db.table("agent_memory")
            .select("audience_insight, patterns_detected, date")
            .order("date", desc=True)
            .limit(1)
            .execute()
        )
        if result.data:
            row = result.data[0]
            insight = row.get("audience_insight", "")
            patterns = row.get("patterns_detected", "[]")
            if isinstance(patterns, str):
                patterns = json.loads(patterns)
            return f"Trending topics: {', '.join(patterns)}\n\nWeekly insight: {insight}"
        return "No trend data available yet."
    except Exception as e:
        log.error(f"Failed to fetch weekly insight: {e}")
        return "No trend data available."


def generate_brief(
    pillar: str,
    competitor_posts: list[dict],
    weekly_insight: str,
    claude: anthropic.Anthropic
) -> Optional[dict]:
    relevant = [p for p in competitor_posts if p.get("topic_detected") == pillar]
    posts_to_use = relevant[:10] if len(relevant) >= 3 else competitor_posts[:10]

    posts_summary = [
        {
            "handle": p.get("handle"),
            "topic": p.get("topic_detected"),
            "format": p.get("post_type"),
            "likes": p.get("likes"),
            "caption": p.get("caption_preview", "")[:150],
        }
        for p in posts_to_use
    ]

    prompt = BRIEF_PROMPT.format(
        account_voice=ACCOUNT_VOICE,
        competitor_posts=json.dumps(posts_summary, indent=2),
        weekly_insight=weekly_insight,
        pillar=pillar,
    )

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0]
        brief = json.loads(raw)
        log.info(f"Generated brief for pillar: {pillar}")
        return brief
    except Exception as e:
        log.error(f"Failed to generate brief for {pillar}: {e}")
        return None


def save_briefs_to_supabase(briefs: list[dict], db: Client) -> int:
    if not briefs:
        return 0

    week_of = datetime.now(timezone.utc).date().isoformat()
    saved = 0
    for brief in briefs:
        try:
            row = {
                "week_of": week_of,
                "pillar": brief.get("pillar"),
                "hook_idea": brief.get("hook_idea"),
                "format": brief.get("format"),
                "angle": brief.get("angle"),
                "content_outline": json.dumps(brief.get("content_outline", [])),
                "caption_starter": brief.get("caption_starter"),
                "why_it_will_work": brief.get("why_it_will_work"),
                "estimated_reach_tier": brief.get("estimated_reach_tier"),
                "status": "draft",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            db.table("content_briefs").insert(row).execute()
            saved += 1
        except Exception as e:
            log.warning(f"Failed to save brief for {brief.get('pillar')}: {e}")

    log.info(f"Saved {saved}/{len(briefs)} content briefs to Supabase.")
    return saved


def run_strategist():
    log.info("=" * 50)
    log.info("Content Strategist starting...")
    log.info("=" * 50)

    db = init_supabase()
    claude = init_anthropic()

    competitor_posts = fetch_top_competitor_posts(db, days=7, limit=20)
    weekly_insight = fetch_weekly_insight(db)

    if not competitor_posts:
        log.warning("No competitor post data found. Run the Competitor Spy first.")
        return

    log.info(f"Weekly insight preview: {weekly_insight[:200]}")

    briefs = []
    for pillar in CONTENT_PILLARS:
        brief = generate_brief(pillar, competitor_posts, weekly_insight, claude)
        if brief:
            briefs.append(brief)
        time.sleep(2)

    saved = save_briefs_to_supabase(briefs, db)

    log.info("=" * 50)
    log.info(f"Content Strategist complete. {saved}/{len(CONTENT_PILLARS)} briefs saved.")
    for brief in briefs:
        log.info(f"  [{brief.get('pillar')}] {brief.get('format')} — {brief.get('hook_idea', '')[:80]}")
    log.info("=" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Content Strategist Agent")
    parser.add_argument("--daemon", action="store_true", help="Run weekly on Mondays at 9am")
    args = parser.parse_args()

    if args.daemon:
        log.info("Daemon mode: running every Monday at 9:00 AM.")
        run_strategist()
        schedule.every().monday.at("09:00").do(run_strategist)
        while True:
            schedule.run_pending()
            time.sleep(60)
    else:
        run_strategist()
