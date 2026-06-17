"""
Caption Writer Agent — @levi.smokes Personal Finance
Reads approved/draft content briefs from Supabase.
Generates full Instagram captions for each brief using Claude Sonnet.
Updates the brief row with the generated caption.

Run manually:    python caption_writer.py
Run on schedule: python caption_writer.py --daemon  (runs every Tuesday 9am, after Content Strategist)
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

# ─────────────────────────────────────────────
# ACCOUNT VOICE
# ─────────────────────────────────────────────
ACCOUNT_VOICE = """
Account: @levi.smokes (personal finance for young Singaporeans / Southeast Asians)
Tone: Casual, direct, slightly sarcastic — like a smart friend who learned money the hard way
Audience: 20-35 year olds in Singapore/SEA who want to build wealth but find finance intimidating
Style: Hook-first. Short punchy sentences. Real numbers. Singapore context (CPF, HDB, SGD, Grab).
Avoid: Boring intros. Textbook language. "In conclusion". Generic advice. Emojis overload.
"""

CAPTION_PROMPT = """You are a viral Instagram caption writer for a personal finance account targeting young Singaporeans.

Account voice:
{account_voice}

Here is the content brief for this post:
Pillar: {pillar}
Format: {format}
Hook idea: {hook_idea}
Angle: {angle}
Content outline: {content_outline}
Caption starter: {caption_starter}
Why it will work: {why_it_will_work}

Write a complete Instagram caption following this EXACT structure:

LINE 1-2: The hook (must match or improve on the caption_starter — this is what shows before "more")
[blank line]
BODY (3-5 short paragraphs, each 1-3 sentences):
- Use the content outline as your guide but write conversationally
- Include real Singapore numbers/context where possible (CPF, SGD amounts, local examples)
- Each paragraph punchy. No fluff. No "in this post I will..."
- Tell the reader what to think/do/feel differently after reading
[blank line]
CTA (1-2 sentences): Ask a question or give a clear action.
[blank line]
HASHTAGS (exactly 20, mix of niche + broad):
Start with 3-4 Singapore-specific: #sgfinance #singaporemoney #sgpersonalfinance etc
Then 6-8 pillar-specific: #{pillar_tags}
Then 6-8 broad reach: #personalfinance #moneytips #financialfreedom etc
Put all hashtags on ONE line separated by spaces.

Return ONLY the caption text. No JSON. No explanations. Just the raw caption.
"""

PILLAR_HASHTAGS = {
    "investing basics": "investingforbeginners investing101 stockmarket etf indexfund passiveincome",
    "budgeting hacks": "budgeting budgetingtips savemoney frugalliving moneysaving budgetlife",
    "money mindset": "moneymindset wealthmindset financialmindset growthmindset richhabits",
    "side hustle": "sidehustle sideincome passiveincome extraincome makemoneyonline freelance",
    "debt payoff": "debtfree debtpayoff getoutofdebt creditcarddebt studentloan financialgoals",
}


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


def fetch_briefs_needing_captions(db: Client) -> list[dict]:
    """Pull briefs that have no caption yet (draft status, caption is null)."""
    try:
        result = (
            db.table("content_briefs")
            .select("id, pillar, hook_idea, format, angle, content_outline, caption_starter, why_it_will_work, estimated_reach_tier, week_of")
            .eq("status", "draft")
            .is_("caption", "null")
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
        briefs = result.data or []
        log.info(f"Found {len(briefs)} briefs needing captions.")
        return briefs
    except Exception as e:
        log.error(f"Failed to fetch briefs: {e}")
        return []


def generate_caption(brief: dict, claude: anthropic.Anthropic) -> Optional[str]:
    """Generate a full Instagram caption for a brief."""
    pillar = brief.get("pillar", "")
    content_outline = brief.get("content_outline", "[]")
    if isinstance(content_outline, str):
        try:
            outline_list = json.loads(content_outline)
            content_outline_str = "\n".join(f"  - {s}" for s in outline_list)
        except Exception:
            content_outline_str = content_outline
    else:
        content_outline_str = "\n".join(f"  - {s}" for s in content_outline)

    prompt = CAPTION_PROMPT.format(
        account_voice=ACCOUNT_VOICE,
        pillar=pillar,
        format=brief.get("format", "carousel"),
        hook_idea=brief.get("hook_idea", ""),
        angle=brief.get("angle", ""),
        content_outline=content_outline_str,
        caption_starter=brief.get("caption_starter", ""),
        why_it_will_work=brief.get("why_it_will_work", ""),
        pillar_tags=PILLAR_HASHTAGS.get(pillar, "personalfinance moneytips"),
    )

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        caption = response.content[0].text.strip()
        log.info(f"Generated caption for [{pillar}] ({len(caption)} chars)")
        return caption
    except Exception as e:
        log.error(f"Failed to generate caption for [{pillar}]: {e}")
        return None


def save_caption(brief_id: str, caption: str, db: Client) -> bool:
    """Write caption back to the content_briefs row."""
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
    log.info("Caption Writer starting...")
    log.info("=" * 50)

    db = init_supabase()
    claude = init_anthropic()

    briefs = fetch_briefs_needing_captions(db)
    if not briefs:
        log.info("No briefs need captions. All done.")
        return

    saved = 0
    for brief in briefs:
        pillar = brief.get("pillar", "unknown")
        caption = generate_caption(brief, claude)
        if caption:
            ok = save_caption(brief["id"], caption, db)
            if ok:
                saved += 1
                log.info(f"  ✓ [{pillar}] caption saved ({len(caption)} chars)")
                first_line = caption.split("\n")[0][:100]
                log.info(f"    Hook: {first_line}")
            else:
                log.warning(f"  ✗ [{pillar}] caption generated but failed to save")
        time.sleep(2)

    log.info("=" * 50)
    log.info(f"Caption Writer complete. {saved}/{len(briefs)} captions written.")
    log.info("=" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Caption Writer Agent")
    parser.add_argument("--daemon", action="store_true", help="Run weekly on Tuesdays at 9am")
    args = parser.parse_args()

    if args.daemon:
        log.info("Daemon mode: running every Tuesday at 9:00 AM.")
        run_caption_writer()
        schedule.every().tuesday.at("09:00").do(run_caption_writer)
        while True:
            schedule.run_pending()
            time.sleep(60)
    else:
        run_caption_writer()
