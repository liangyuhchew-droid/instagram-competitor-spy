"""
Caption Writer Agent — @levi.cashflow
Reads approved content briefs from Supabase (status='approved', caption is null).
Generates full Instagram captions for each brief using Claude.
Updates the brief row with the generated caption.

Run manually:    python caption_writer.py
Run on schedule: python caption_writer.py --daemon
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
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

ACCOUNT_VOICE = """
Account: @levi.cashflow (personal finance for ambitious 20-35 year olds worldwide)
Tone: Casual, direct, slightly contrarian — like a smart friend who learned money the hard way
Audience: Young professionals globally who want to build wealth but find traditional finance boring
Style: Hook-first. Short punchy sentences. Universal examples — no country-specific taxes, schemes, or platforms.
Avoid: Boring intros. Textbook language. "In conclusion". Any country-specific references (CPF, 401k, ISA, etc.).
"""

CAPTION_PROMPT = """You are a viral Instagram caption writer for @levi.cashflow, a global personal finance account.

Account voice:
{account_voice}

Content brief:
Pillar: {pillar}
Format: {format}
Hook idea: {hook_idea}
Angle: {angle}
Content outline: {content_outline}
Caption starter: {caption_starter}

Write a complete Instagram caption following this EXACT structure:

LINE 1-2: The hook (must match or improve on caption_starter — this is what shows before "more")
[blank line]
BODY (3-5 short paragraphs, 1-3 sentences each):
- Use the content outline as a guide but write conversationally
- Use universal money concepts: income, expenses, savings rate, compound growth, net worth, debt
- Every sentence earns its place. Cut anything that doesn't move the reader
- Each paragraph should make the reader nod or feel called out
[blank line]
CTA: One punchy question or direct action. Not "what do you think?" — make it specific.
[blank line]
HASHTAGS (exactly 20, on ONE line separated by spaces):
4 pillar-specific: #{pillar_tags}
6 broad personal finance: #personalfinance #moneytips #financialfreedom #wealthbuilding #moneyhabits #financetips
5 growth hashtags: #investing #passiveincome #buildwealth #financialindependence #richhabits
5 lifestyle: #millennialmoney #genz #adulting #debtfree #frugalliving

Return ONLY the caption text. No JSON. No explanations.
"""

PILLAR_HASHTAGS = {
    "wealth-building":  "wealthbuilding buildwealth generationalwealth networth",
    "investing":        "investing investingforbeginners stockmarket indexfund etf",
    "money-mindset":    "moneymindset wealthmindset financialmindset richhabits",
    "income-streams":   "multipleincome sidehustle passiveincome incomeonline sideincome",
    "budgeting":        "budgeting savemoney frugalliving moneysaving budgetlife",
}
DEFAULT_PILLAR_TAGS = "personalfinance moneytips financialfreedom wealthbuilding"


def init_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise ValueError("Set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env")
    return create_client(url, key)


def fetch_briefs_needing_captions(db: Client) -> list:
    try:
        result = (
            db.table("content_briefs")
            .select("id, pillar, hook_idea, format, angle, content_outline, caption_starter, week_of")
            .eq("status", "approved")
            .is_("caption", "null")
            .order("created_at", desc=False)
            .limit(10)
            .execute()
        )
        briefs = result.data or []
        log.info(f"Found {len(briefs)} brief(s) needing captions.")
        return briefs
    except Exception as e:
        log.error(f"Failed to fetch briefs: {e}")
        return []


def generate_caption(brief: dict, claude: anthropic.Anthropic):
    pillar = (brief.get("pillar") or "").lower().strip()
    pillar_tags = PILLAR_HASHTAGS.get(pillar, DEFAULT_PILLAR_TAGS)
    raw_outline = brief.get("content_outline", "[]")
    if isinstance(raw_outline, str):
        try:
            outline_list = json.loads(raw_outline)
            content_outline_str = "\n".join(f"  - {s}" for s in outline_list)
        except Exception:
            content_outline_str = raw_outline
    else:
        content_outline_str = "\n".join(f"  - {s}" for s in raw_outline)
    prompt = CAPTION_PROMPT.format(
        account_voice=ACCOUNT_VOICE.strip(),
        pillar=pillar,
        format=brief.get("format", "carousel"),
        hook_idea=brief.get("hook_idea", ""),
        angle=brief.get("angle", ""),
        content_outline=content_outline_str,
        caption_starter=brief.get("caption_starter", ""),
        pillar_tags=pillar_tags,
    )
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        caption = response.content[0].text.strip()
        log.info(f"Generated caption for [{pillar}] ({len(caption)} chars)")
        return caption
    except Exception as e:
        log.error(f"Failed to generate caption: {e}")
        return None


def save_caption(brief_id: str, caption: str, db: Client) -> bool:
    try:
        db.table("content_briefs").update({
            "caption": caption,
            "caption_generated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", brief_id).execute()
        return True
    except Exception as e:
        log.error(f"Failed to save caption for {brief_id}: {e}")
        return False


def run_caption_writer():
    log.info("=" * 50)
    log.info(f"Caption Writer running at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    db = init_supabase()
    claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    briefs = fetch_briefs_needing_captions(db)
    if not briefs:
        log.info("No briefs need captions.")
        return
    saved = 0
    for brief in briefs:
        pillar = brief.get("pillar", "unknown")
        caption = generate_caption(brief, claude)
        if caption:
            ok = save_caption(brief["id"], caption, db)
            if ok:
                saved += 1
                log.info(f"  ✓ [{pillar}] caption saved")
                log.info(f"    Hook: {caption.split(chr(10))[0][:100]}")
        time.sleep(2)
    log.info(f"Done. {saved}/{len(briefs)} captions written.")
    log.info("=" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Caption Writer Agent")
    parser.add_argument("--daemon", action="store_true", help="Run every 6 hours")
    args = parser.parse_args()
    if args.daemon:
        log.info("Daemon mode: running caption writer every 6 hours.")
        try:
            run_caption_writer()
        except Exception as e:
            log.warning(f"Startup run failed: {e}")
        schedule.every(6).hours.do(run_caption_writer)
        while True:
            schedule.run_pending()
            time.sleep(60)
    else:
        run_caption_writer()
