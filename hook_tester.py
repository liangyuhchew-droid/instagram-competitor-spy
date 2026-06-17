"""
Hook Tester Agent -- @levi.smokes Personal Finance
Scores all 5 hooks from the weekly content briefs.
Ranks them by predicted engagement (ROI-weighted: saves > shares > comments > likes).
Tags top 2 as 'approved', rest stay 'draft'.
Saves scores to content_briefs.hook_score column.

Run manually:    python hook_tester.py
Run on schedule: python hook_tester.py --daemon  (runs every Tuesday 10am, after Caption Writer)
"""

import os
import json
import logging
import argparse
import schedule
import time
from datetime import datetime, timezone
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

SCORING_PROMPT = """You are an Instagram growth expert scoring hooks for a personal finance account (@levi.smokes) targeting young Singaporeans aged 20-35.

Monetisation priority: Saves > Shares > Comments > Likes > Reach

Score each hook from 0-100 based on specificity, pattern interrupt, self-relevance, save potential, curiosity gap.

Hooks to score:
{hooks_list}

Return JSON array only:
[
  {{
    \"brief_id\": \"<id>\",
    \"pillar\": \"<pillar>\",
    \"hook\": \"<first line>\",
    \"score\": <0-100>,
    \"reasoning\": \"<2 sentences>\",
    \"predicted_best_metric\": \"<saves|shares|comments>\"
  }}
]

Order by score descending. Return ONLY the JSON array.
"""


def init_supabase():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise ValueError("Set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env")
    return create_client(url, key)


def init_anthropic():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("Set ANTHROPIC_API_KEY in .env")
    return anthropic.Anthropic(api_key=api_key)


def fetch_briefs_with_captions(db):
    try:
        result = (
            db.table("content_briefs")
            .select("id, pillar, hook_idea, caption, caption_starter, week_of")
            .eq("status", "draft")
            .not_.is_("caption", "null")
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
        briefs = result.data or []
        log.info(f"Found {len(briefs)} briefs with captions to score.")
        return briefs
    except Exception as e:
        log.error(f"Failed to fetch briefs: {e}")
        return []


def score_hooks(briefs, claude):
    hooks_list = []
    for b in briefs:
        caption = b.get("caption", "")
        first_line = caption.split("\n")[0][:150] if caption else b.get("caption_starter", "")
        hooks_list.append(f'Brief ID: {b["id"]}\nPillar: {b["pillar"]}\nHook: {first_line}')

    hooks_text = "\n\n---\n\n".join(hooks_list)
    prompt = SCORING_PROMPT.format(hooks_list=hooks_text)

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        scores = json.loads(raw.strip())
        log.info(f"Scored {len(scores)} hooks.")
        return scores
    except Exception as e:
        log.error(f"Failed to score hooks: {e}")
        return None


def save_scores_and_tag(scores, db):
    saved = 0
    for i, item in enumerate(scores):
        brief_id = item.get("brief_id")
        score = item.get("score", 0)
        reasoning = item.get("reasoning", "")
        predicted_metric = item.get("predicted_best_metric", "")
        new_status = "approved" if i < 2 else "draft"

        try:
            db.table("content_briefs").update({
                "hook_score": score,
                "hook_score_reasoning": reasoning,
                "predicted_best_metric": predicted_metric,
                "status": new_status,
            }).eq("id", brief_id).execute()
            saved += 1
            tag = "APPROVED" if new_status == "approved" else "draft"
            log.info(f"  {tag} [{item.get('pillar')}] score={score}")
        except Exception as e:
            log.error(f"Failed to save score for {brief_id}: {e}")

    return saved


def run_hook_tester():
    log.info("=" * 50)
    log.info("Hook Tester starting...")
    log.info("=" * 50)

    db = init_supabase()
    claude = init_anthropic()

    briefs = fetch_briefs_with_captions(db)
    if not briefs:
        log.info("No briefs to score. All done.")
        return

    scores = score_hooks(briefs, claude)
    if not scores:
        log.error("Scoring failed. Exiting.")
        return

    log.info("\nHook Rankings:")
    for i, s in enumerate(scores):
        log.info(f"  #{i+1} [{s.get('pillar')}] score={s.get('score')}")

    saved = save_scores_and_tag(scores, db)

    log.info("=" * 50)
    log.info(f"Hook Tester complete. {saved} scores saved. Top 2 tagged 'approved'.")
    log.info("=" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hook Tester Agent")
    parser.add_argument("--daemon", action="store_true", help="Run weekly on Tuesdays at 10am")
    args = parser.parse_args()

    if args.daemon:
        log.info("Daemon mode: running every Tuesday at 10:00 AM.")
        run_hook_tester()
        schedule.every().tuesday.at("10:00").do(run_hook_tester)
        while True:
            schedule.run_pending()
            time.sleep(60)
    else:
        run_hook_tester()
