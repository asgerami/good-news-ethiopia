"""
Good News Ethiopia scraper bot.

Scrapes public posts from the Tikvah Ethiopia Telegram channels (via the
no-login t.me/s/<channel> preview page), asks Gemini to judge whether each
post is genuinely good news, translates + summarizes it into English, and
posts it to the Good News Ethiopia Telegram channel with:
  - a short English summary
  - a link to the full English translation (hosted on Telegraph)
  - a link back to the original Tikvah post

Run repeatedly (e.g. via a systemd timer or cron); already-processed posts
are tracked in state.json so reruns don't double-post.
"""

import html
import json
import logging
import os
import re
import time
from collections import Counter
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

SOURCE_CHANNELS = [
    c.strip()
    for c in os.environ.get("SOURCE_CHANNELS", "tikvahethiopia,tikvahethmagazine").split(",")
    if c.strip()
]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TARGET_CHAT = os.environ["TARGET_CHAT"]
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
MAX_MESSAGES_PER_RUN = int(os.environ.get("MAX_MESSAGES_PER_RUN", "20"))
MAX_SEEN_IDS = 5000
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "5"))

STATE_FILE = Path(__file__).parent / "state.json"
TELEGRAPH_API = "https://api.telegra.ph"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("good-news-bot")

# Signals distilled from real ad posts observed on these channels: classified-ad
# contact info (phone number + a sell/order call-to-action), spammed repeated links,
# and Tikvah's own paid-partnership disclosure text. Combining signals (rather than
# matching on phone numbers or links alone) keeps false positives on real news low.
AD_PHONE_PATTERN = re.compile(r"(?:\+251|0)(?:9|7)\d{8}")
AD_CTA_WORDS = ("call me", "ለሽያጭ", "ለመሽጥ", "ለማዘዝ", "ይደውሉ", "ለመግዛት")
AD_MARKER_PHRASES = ("partnership with tikvah", "በማስታወቂያ ዋጋ", "sponsored")


def looks_like_ad(text: str) -> bool:
    lower = text.lower()
    if any(phrase in lower for phrase in AD_MARKER_PHRASES):
        return True
    if AD_PHONE_PATTERN.search(text) and any(word in lower for word in AD_CTA_WORDS):
        return True
    urls = re.findall(r"https?://\S+", text)
    if urls and max(Counter(urls).values()) >= 3:
        return True  # same link spammed 3+ times
    return False


BATCH_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "results": {
            "type": "ARRAY",
            "description": "One result per input post, in any order — matched back by id.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {
                        "type": "STRING",
                        "description": "Must exactly match the id given for this post in the input.",
                    },
                    "is_good_news": {
                        "type": "BOOLEAN",
                        "description": (
                            "True only if the post reports genuinely positive, uplifting news: "
                            "achievements, development projects, humanitarian relief, sports wins, "
                            "cultural milestones, economic good news, acts of kindness, etc. "
                            "False for accidents, deaths, disasters, crime, conflict, or purely "
                            "political content."
                        ),
                    },
                    "is_authentic_and_postable": {
                        "type": "BOOLEAN",
                        "description": (
                            "False if this looks like an advertisement/sponsored post, an "
                            "unverified rumor, pure speculation, or content that cannot be "
                            "responsibly reposted as news."
                        ),
                    },
                    "reason": {
                        "type": "STRING",
                        "description": "One short sentence explaining the classification, for internal logging only.",
                    },
                    "summary_en": {
                        "type": "STRING",
                        "description": "A 1-3 sentence English summary of the news, suitable for a short social media post.",
                    },
                    "full_translation_en": {
                        "type": "STRING",
                        "description": "A complete, faithful English translation of the original post.",
                    },
                },
                "required": [
                    "id",
                    "is_good_news",
                    "is_authentic_and_postable",
                    "reason",
                    "summary_en",
                    "full_translation_en",
                ],
            },
        },
    },
    "required": ["results"],
}


def load_state() -> dict:
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
    else:
        state = {"seen_ids": [], "telegraph_access_token": None}
    # Migrate pre-multi-channel ids (bare numeric strings) to "channel/id" form.
    state["seen_ids"] = [
        sid if "/" in sid else f"tikvahethiopia/{sid}" for sid in state.get("seen_ids", [])
    ]
    return state


def save_state(state: dict) -> None:
    state["seen_ids"] = state.get("seen_ids", [])[-MAX_SEEN_IDS:]
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def fetch_source_messages(channel: str) -> list[dict]:
    url = f"https://t.me/s/{channel}"
    resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    messages = []
    for block in soup.select("div.tgme_widget_message"):
        post_id = block.get("data-post")  # e.g. "tikvahethiopia/107416" — already unique across channels
        if not post_id:
            continue

        text_div = block.select_one(".tgme_widget_message_text")
        text = text_div.get_text("\n", strip=True) if text_div else ""
        if not text:
            continue  # skip photo/video-only posts with no caption

        link_tag = block.select_one("a.tgme_widget_message_date")
        link = link_tag["href"] if link_tag else f"https://t.me/{post_id}"

        messages.append({"id": post_id, "text": text, "link": link})
    return messages


def classify_and_translate_batch(messages: list[dict]) -> dict[str, dict]:
    """Classify, translate, and summarize a batch of posts in a single Gemini call.

    Returns a dict mapping each message's id to its result. A post whose id is
    missing from the response (e.g. the model dropped it) simply won't appear
    in the returned dict — callers should treat that as "retry next run".
    """
    parts = [
        "You help run a 'Good News Ethiopia' Telegram channel that reposts "
        "only genuinely positive news, translated into English, sourced from "
        "Tikvah's Telegram channels (a trusted Ethiopian news source). Below "
        "are several posts (likely Amharic, possibly mixed with English), each "
        "labeled with its id. For EVERY post, decide whether it is good news, "
        "whether it is safe and authentic to repost as-is (not an ad, not an "
        "unverified rumor, not speculation), translate it completely and "
        "accurately into English, and write a short English summary. Return "
        "exactly one result per post in the 'results' array, each carrying its "
        "matching id."
    ]
    for msg in messages:
        parts.append(f"\n--- id: {msg['id']} ---\n{msg['text']}")
    prompt = "\n".join(parts)

    url = f"{GEMINI_API}/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": BATCH_SCHEMA,
            "maxOutputTokens": 8192,
        },
    }

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        resp = requests.post(url, json=payload, timeout=90)
        if resp.status_code == 429 and attempt < max_attempts:
            wait = 15 * attempt
            log.warning("Gemini rate-limited; retrying in %ds (attempt %d/%d)", wait, attempt, max_attempts)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        text_block = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text_block)
        return {item["id"]: item for item in parsed["results"]}


def get_telegraph_token(state: dict) -> str:
    token = state.get("telegraph_access_token")
    if token:
        return token
    resp = requests.post(
        f"{TELEGRAPH_API}/createAccount",
        data={"short_name": "GoodNewsEthiopia", "author_name": "Good News Ethiopia"},
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json()["result"]["access_token"]
    state["telegraph_access_token"] = token
    save_state(state)
    return token


def publish_full_translation(state: dict, title: str, full_text: str, original_link: str) -> str:
    token = get_telegraph_token(state)
    paragraphs = [p for p in full_text.split("\n") if p.strip()]
    content = [{"tag": "p", "children": [p]} for p in paragraphs]
    content.append(
        {
            "tag": "p",
            "children": [
                "Original post (Amharic): ",
                {"tag": "a", "attrs": {"href": original_link}, "children": [original_link]},
            ],
        }
    )
    resp = requests.post(
        f"{TELEGRAPH_API}/createPage",
        json={
            "access_token": token,
            "title": (title or "Good News from Ethiopia")[:250],
            "content": content,
            "author_name": "Good News Ethiopia",
            "author_url": original_link,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegraph error: {data}")
    return data["result"]["url"]


def send_to_telegram_channel(summary_en: str, original_link: str, telegraph_url: str) -> None:
    message = (
        f"{html.escape(summary_en)}\n\n"
        f'\U0001f517 <a href="{original_link}">Original post (Amharic)</a> — via Tikvah\n'
        f'\U0001f4d6 <a href="{telegraph_url}">Read the full English translation</a>'
    )
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data={
            "chat_id": TARGET_CHAT,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {data}")


def notify_admin_of_failure(error: BaseException) -> None:
    """Best-effort DM to the admin when a run crashes. Never raises itself —
    a broken alert must not mask the original failure."""
    if not ADMIN_CHAT_ID:
        return

    run_url = ""
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        run_url = f"\n\n{server}/{repo}/actions/runs/{run_id}"

    message = (
        "⚠️ <b>Good News Ethiopia bot crashed</b>\n\n"
        f"<code>{html.escape(f'{type(error).__name__}: {error}')}</code>"
        f"{html.escape(run_url) if run_url else ''}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": ADMIN_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception:
        log.exception("Failed to send admin failure notification")


def main() -> None:
    state = load_state()
    seen = set(state.get("seen_ids", []))

    new_messages = []
    for channel in SOURCE_CHANNELS:
        try:
            channel_messages = fetch_source_messages(channel)
        except Exception:
            log.exception("Failed to fetch messages from %s; skipping this run", channel)
            continue
        channel_new = [m for m in channel_messages if m["id"] not in seen]
        new_messages.extend(channel_new[-MAX_MESSAGES_PER_RUN:])

    if not new_messages:
        log.info("No new messages.")
        return

    candidates = []
    for msg in new_messages:
        if looks_like_ad(msg["text"]):
            log.info("Pre-filter: skipping %s (looks like an ad)", msg["id"])
            state.setdefault("seen_ids", []).append(msg["id"])
            save_state(state)
        else:
            candidates.append(msg)

    posted = 0
    for i in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[i : i + BATCH_SIZE]
        time.sleep(3)  # stay comfortably under Gemini's free-tier rate limit
        try:
            results = classify_and_translate_batch(batch)
        except Exception:
            log.exception(
                "Batch classification failed for %s; will retry next run",
                [m["id"] for m in batch],
            )
            continue

        for msg in batch:
            result = results.get(msg["id"])
            if result is None:
                log.warning("No result returned for %s; will retry next run", msg["id"])
                continue

            if not (result["is_good_news"] and result["is_authentic_and_postable"]):
                log.info("Skipping %s: %s", msg["id"], result["reason"])
                state.setdefault("seen_ids", []).append(msg["id"])
                save_state(state)
                continue

            try:
                title = result["summary_en"].split(".")[0]
                telegraph_url = publish_full_translation(
                    state, title, result["full_translation_en"], msg["link"]
                )
                send_to_telegram_channel(result["summary_en"], msg["link"], telegraph_url)
            except Exception:
                log.exception("Failed to post message %s; will retry next run", msg["id"])
                continue

            log.info("Posted %s -> %s", msg["id"], telegraph_url)
            posted += 1
            state.setdefault("seen_ids", []).append(msg["id"])
            save_state(state)

    log.info("Done. Posted %d new good-news item(s).", posted)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("Unhandled error; run failed")
        notify_admin_of_failure(e)
        raise
