"""
Content Strategist — @levi.cashflow
Generates content briefs for a personal finance Instagram account targeting a
global audience of 20-35 year olds who want to build wealth.

Pillar names MUST match image_pipeline.py PILLAR_STYLES keys:
  wealth-building | investing | money-mindset | income-streams | budgeting

Run manually: python content_strategist.py
Run on schedule: python content_strategist.py --daemon
"""

import os
import json
import time
import logging
import argparse
import schedule
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Account identity ───────────────────────────────────────────────────────────
ACCOUNT_VOICE = """
Account: @levi.cashflow (personal finance for ambitious 20-35 year olds worldwide)
Tone: Casual, direct, slightly contrarian — like a smart friend who learned money the hard way
Audience: Young professionals globally who want to build wealth but find traditional finance content dry or intimidating
Style: Carousels and Reels. Hook-first. Real examples over theory. Universally relatable money situations.
Avoid: Country-specific rules, platforms, or currency amounts. Boring "top 5 tips" energy. Generic "invest early" advice. Anything textbook.
Goal: Every post should make someone think "I never thought about it that way" or "I needed to hear this."
"""

# ── Content pillars (must match image_pipeline.py PILLAR_STYLES keys) ─────────
CONTENT_PILLARS = [
    "wealth-building",
    "investing",
    "money-mindset",
    "income-streams",
    "budgeting",
]

BRIEF_PROMPT = """
You are a content strategist for @levi.cashflow, a personal finance Instagram account with 10M-follower ambitions.

Account voice:
{account_voice}

Today's date: {today}
Content pillar to write for: {pillar}
Format: {format}

Generate ONE highly engaging content brief. Use this exact JSON format — no commentary, just the JSON object:

{{
  "pillar": "{pillar}",
  "format": "{format}",
  "week_of": "{week_of}",
  "hook_idea": "<Single punchy opening line. Max 12 words. Make it provocative, surprising or counter-intuitive. No country-specific references.>",
  "angle": "<1-2 sentences describing the specific insight or counter-narrative. What's the non-obvious angle?>",
  "caption_starter": "<First 2-3 sentences of the actual Instagram caption. Must match hook energy. Include a cliffhanger that makes them want to read more.>",
  "content_outline": [
    "Slide 1: Hook — <hook text>",
    "Slide 2: Problem — <the relatable pain point>",
    "Slide 3: Insight — <the non-obvious truth>",
    "Slide 4: Proof — <a concrete example or analogy>",
    "Slide 5: Action step — <one specific thing they can do today>",
    "Slide 6: CTA — Follow @levi.cashflow for more money moves"
  ],
  "hashtags": ["#personalfinance", "#moneytips", "#financialfreedom", "#investing", "#wealthbuilding"],
  "status": "approved"
}}

Requirements:
- hook_idea must be standalone — punchy enough to stop the scroll with zero context
- No dollar amounts, no country-specific tax terms, no local platform names (no "401k", no "CPF", no "ISA")
- Use universal money concepts: income, expenses, savings rate, compound growth, debt, net worth
- Real people can relate to this content regardless of where they live
- Make it feel personal, not like a finance textbook
"""

FORMAT_OPTIONS = ["carousel", "carousel", "carousel", "reel"]


def init_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("Set SUPABASE_URL and SUPABASE_SERVICE_KEY in env")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def get_recent_pillars(db: Client, days: int = 7) -> list:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    result = (
        db.table("content_briefs")
        .select("pillar")
        .gte("created_at", since)
        .execute()
    )
    return [row["pillar"] for row in (result.data or []) if row.get("pillar")]


def pick_pillar(db: Client) -> str:
    recent = get_recent_pillars(db)
    fresh = [p for p in CONTENT_PILLARS if p not in recent]
    pool = fresh if fresh else CONTENT_PILLARS
    return random.choice(pool)


def save_brief(db: Client, brief: dict):
    result = db.table("content_briefs").insert(brief).execute()
    rows = result.data or []
    if rows:
        return rows[0].get("id")
    return None


def generate_brief(pillar: str):
    if not ANTHROPIC_KEY:
        raise ValueError("Set ANTHROPIC_API_KEY in env")
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week_of = (datetime.now(timezone.utc) - timedelta(days=datetime.now(timezone.utc).weekday())).strftime("%Y-%m-%d")
    fmt = random.choice(FORMAT_OPTIONS)
    prompt = BRIEF_PROMPT.format(
        account_voice=ACCOUNT_VOICE.strip(),
        today=today,
        pillar=pillar,
        format=fmt,
        week_of=week_of,
    )
    log.info(f"Generating brief: pillar={pillar}, format={fmt}")
    message = client.messages.create(
        model="claude-opus-4-5" if os.environ.get("USE_OPUS") else "claude-sonnet-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        brief = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse brief JSON: {e}\nRaw:\n{raw[:300]}")
        return None
    if isinstance(brief.get("content_outline"), list):
        brief["content_outline"] = json.dumps(brief["content_outline"])
    if isinstance(brief.get("hashtags"), list):
        brief["hashtags"] = json.dumps(brief["hashtags"])
    brief["status"] = "approved"
    brief["created_at"] = datetime.now(timezone.utc).isoformat()
    return brief


def run_strategist():
    log.info("=" * 50)
    log.info(f"Content strategist running at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    db = init_supabase()
    pillar = pick_pillar(db)
    brief = generate_brief(pillar)
    if not brief:
        log.error("Brief generation failed — skipping this run.")
        return
    brief_id = save_brief(db, brief)
    if brief_id:
        log.info(f"✓ Saved brief [{pillar}] with id {brief_id}")
        log.info(f"  Hook: {brief.get('hook_idea', '')[:80]}")
    else:
        log.error("Failed to save brief to Supabase.")
    log.info("=" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Content Strategist Agent")
    parser.add_argument("--daemon", action="store_true", help="Run every 6 hours")
    args = parser.parse_args()
    if args.daemon:
        log.info("Daemon mode: generating content brief every 6 hours.")
        try:
            run_strategist()
        except Exception as e:
            log.warning(f"Startup run failed: {e}")
        schedule.every(6).hours.do(run_strategist)
        while True:
            try:
                schedule.run_pending()
            except Exception as e:
                log.error(f"Strategist job failed: {e}")
            time.sleep(60)
    else:
        run_strategist()
