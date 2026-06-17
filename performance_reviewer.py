"""
Performance Reviewer Agent -- @levi.smokes Personal Finance
Runs every Sunday at 8am SGT.
Pulls last 7 days of Instagram post insights via Graph API.
Scores each pillar/format by monetisation ROI (saves > shares > comments > likes).
Writes a strategic memo to agent_memory so Content Strategist uses it next Monday.

Run manually:    python performance_reviewer.py
Run on schedule: python performance_reviewer.py --daemon
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

import anthropic
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v21.0"

ROI_WEIGHTS = {
    "saved": 4.0,
    "shares": 3.0,
    "comments": 2.0,
    "likes": 1.0,
    "reach": 0.5,
}

ANALYSIS_PROMPT = """You are a performance strategist for @levi.smokes, a personal finance Instagram account for young Singaporeans.

Last week post performance:
{performance_data}

Historical context:
{historical_context}

Analyse with focus on MONETISATION ROI (saves > shares > comments > likes).

Return a JSON object with these exact keys:
{{
  \"week_of\": \"<YYYY-MM-DD>\",
  \"top_pillar\": \"<best pillar>\",
  \"worst_pillar\": \"<worst pillar>\",
  \"top_format\": \"<carousel|reel|single>\",
  \"trending_topics\": [\"<topic1>\", \"<topic2>\", \"<topic3>\"],
  \"winning_hook_patterns\": [\"<pattern1>\", \"<pattern2>\"],
  \"avoid_next_week\": [\"<avoid1>\", \"<avoid2>\"],
  \"recommended_pillar_weights\": {{
    \"investing basics\": <0-100>,
    \"budgeting hacks\": <0-100>,
    \"money mindset\": <0-100>,
    \"side hustle\": <0-100>,
    \"debt payoff\": <0-100>
  }},
  \"optimal_post_frequency\": <integer>,
  \"insight\": \"<3-4 sentence strategic memo>\",
  \"singapore_trending\": [\"<local topic1>\", \"<local topic2>\"]
}}

Weights must sum to 500. Return ONLY the JSON object.
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


def get_ig_credentials():
    ig_account_id = os.environ.get("INSTAGRAM_ACCOUNT_ID")
    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    if not ig_account_id or not access_token:
        raise ValueError("Set INSTAGRAM_ACCOUNT_ID and INSTAGRAM_ACCESS_TOKEN in env")
    return ig_account_id, access_token


def fetch_recent_posts(ig_account_id, access_token, days=7):
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    url = f"{GRAPH_BASE}/{ig_account_id}/media"
    params = {
        "fields": "id,caption,timestamp,media_type,permalink",
        "since": since,
        "access_token": access_token,
        "limit": 50,
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    log.info(f"Found {len(data)} posts in the last {days} days.")
    return data


def fetch_post_insights(media_id, access_token):
    url = f"{GRAPH_BASE}/{media_id}/insights"
    params = {
        "metric": "impressions,reach,saved,shares,comments_count,like_count,total_interactions",
        "access_token": access_token,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        metrics = {}
        for item in resp.json().get("data", []):
            metrics[item["name"]] = item["values"][0]["value"] if item.get("values") else item.get("value", 0)
        return metrics
    except Exception as e:
        log.warning(f"Could not fetch insights for {media_id}: {e}")
        return {}


def calculate_roi_score(metrics):
    score = 0.0
    score += metrics.get("saved", 0) * ROI_WEIGHTS["saved"]
    score += metrics.get("shares", 0) * ROI_WEIGHTS["shares"]
    score += metrics.get("comments_count", 0) * ROI_WEIGHTS["comments"]
    score += metrics.get("like_count", 0) * ROI_WEIGHTS["likes"]
    score += metrics.get("reach", 0) * ROI_WEIGHTS["reach"]
    return round(score, 2)


def match_brief_to_post(caption, db):
    if not caption:
        return None
    snippet = caption[:60].strip()
    try:
        result = (
            db.table("content_briefs")
            .select("id, pillar, hook_idea, format")
            .like("caption", f"{snippet}%")
            .limit(1)
            .execute()
        )
        rows = result.data or []
        return rows[0] if rows else None
    except Exception:
        return None


def collect_performance_data(db):
    ig_account_id, access_token = get_ig_credentials()
    posts = fetch_recent_posts(ig_account_id, access_token, days=7)

    enriched = []
    for post in posts:
        media_id = post["id"]
        metrics = fetch_post_insights(media_id, access_token)
        roi_score = calculate_roi_score(metrics)
        brief = match_brief_to_post(post.get("caption", ""), db)

        row = {
            "media_id": media_id,
            "timestamp": post.get("timestamp"),
            "media_type": post.get("media_type"),
            "permalink": post.get("permalink"),
            "caption_preview": (post.get("caption") or "")[:120],
            "pillar": brief.get("pillar") if brief else "unknown",
            "format": brief.get("format") if brief else post.get("media_type", "unknown").lower(),
            "roi_score": roi_score,
            **metrics,
        }
        enriched.append(row)
        log.info(f"  [{row['pillar']}] ROI={roi_score} saves={metrics.get('saved',0)} shares={metrics.get('shares',0)}")

    enriched.sort(key=lambda x: x["roi_score"], reverse=True)
    return enriched


def fetch_historical_context(db):
    try:
        result = (
            db.table("agent_memory")
            .select("week_of, insight, top_pillar, trending_topics")
            .order("created_at", desc=True)
            .limit(4)
            .execute()
        )
        rows = result.data or []
        if not rows:
            return "No historical data yet."
        parts = []
        for r in rows:
            parts.append(
                f"Week of {r.get('week_of')}: Top pillar={r.get('top_pillar')} | "
                f"Insight: {r.get('insight', '')[:200]}"
            )
        return "\n".join(parts)
    except Exception as e:
        log.warning(f"Could not fetch historical context: {e}")
        return "No historical data available."


def analyse_performance(performance_data, historical_context, claude):
    perf_json = json.dumps(performance_data, indent=2, default=str)
    prompt = ANALYSIS_PROMPT.format(
        performance_data=perf_json,
        historical_context=historical_context,
    )
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
        return json.loads(raw.strip())
    except Exception as e:
        log.error(f"Analysis failed: {e}")
        return None


def save_to_agent_memory(analysis, db):
    try:
        week_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = {
            "week_of": analysis.get("week_of", week_of),
            "top_pillar": analysis.get("top_pillar"),
            "worst_pillar": analysis.get("worst_pillar"),
            "top_format": analysis.get("top_format"),
            "trending_topics": json.dumps(analysis.get("trending_topics", [])),
            "winning_hook_patterns": json.dumps(analysis.get("winning_hook_patterns", [])),
            "avoid_next_week": json.dumps(analysis.get("avoid_next_week", [])),
            "recommended_pillar_weights": json.dumps(analysis.get("recommended_pillar_weights", {})),
            "optimal_post_frequency": analysis.get("optimal_post_frequency", 10),
            "insight": analysis.get("insight", ""),
            "singapore_trending": json.dumps(analysis.get("singapore_trending", [])),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        db.table("agent_memory").insert(row).execute()
        log.info("Strategic memo saved to agent_memory.")
        return True
    except Exception as e:
        log.error(f"Failed to save to agent_memory: {e}")
        return False


def save_post_performance_records(performance_data, db):
    try:
        for post in performance_data:
            db.table("post_performance").upsert({
                "media_id": post["media_id"],
                "pillar": post.get("pillar", "unknown"),
                "format": post.get("format", "unknown"),
                "roi_score": post.get("roi_score", 0),
                "saved": post.get("saved", 0),
                "shares": post.get("shares", 0),
                "comments_count": post.get("comments_count", 0),
                "like_count": post.get("like_count", 0),
                "reach": post.get("reach", 0),
                "permalink": post.get("permalink", ""),
                "posted_at": post.get("timestamp"),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="media_id").execute()
    except Exception as e:
        log.warning(f"Could not save post performance records: {e}")


def run_performance_reviewer():
    log.info("=" * 50)
    log.info("Performance Reviewer starting...")
    log.info("=" * 50)

    db = init_supabase()
    claude = init_anthropic()

    log.info("Fetching last 7 days of post performance from Instagram...")
    try:
        performance_data = collect_performance_data(db)
    except Exception as e:
        log.error(f"Failed to collect performance data: {e}")
        return

    if not performance_data:
        log.info("No posts found in the last 7 days. Skipping.")
        return

    save_post_performance_records(performance_data, db)
    historical_context = fetch_historical_context(db)

    log.info("Analysing performance with Claude...")
    analysis = analyse_performance(performance_data, historical_context, claude)
    if not analysis:
        log.error("Analysis failed. Exiting.")
        return

    log.info(f"Top pillar: {analysis.get('top_pillar')}")
    log.info(f"Worst pillar: {analysis.get('worst_pillar')}")
    log.info(f"Optimal freq: {analysis.get('optimal_post_frequency')} posts/week")
    log.info(f"Insight: {analysis.get('insight', '')[:200]}")

    save_to_agent_memory(analysis, db)

    log.info("=" * 50)
    log.info("Performance Reviewer complete. Content Strategist picks this up Monday.")
    log.info("=" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Performance Reviewer Agent")
    parser.add_argument("--daemon", action="store_true", help="Run weekly on Sundays at 8am")
    args = parser.parse_args()

    if args.daemon:
        log.info("Daemon mode: running every Sunday at 00:00 UTC (8am SGT).")
        run_performance_reviewer()
        schedule.every().sunday.at("00:00").do(run_performance_reviewer)
        while True:
            schedule.run_pending()
            time.sleep(60)
    else:
        run_performance_reviewer()
