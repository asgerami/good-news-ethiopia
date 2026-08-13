"""
Good News Ethiopia scraper bot.

Scrapes public posts from the Tikvah Ethiopia Telegram channel (via the
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
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

SOURCE_CHANNEL = os.environ.get("SOURCE_CHANNEL", "tikvahethiopia")
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TARGET_CHAT = os.environ["TARGET_CHAT"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
MAX_MESSAGES_PER_RUN = int(os.environ.get("MAX_MESSAGES_PER_RUN", "20"))
MAX_SEEN_IDS = 5000

STATE_FILE = Path(__file__).parent / "state.json"
TELEGRAPH_API = "https://api.telegra.ph"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("good-news-bot")

CLASSIFY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
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
        "is_good_news",
        "is_authentic_and_postable",
        "reason",
        "summary_en",
        "full_translation_en",
    ],
}


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_ids": [], "telegraph_access_token": None}


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
        post_id = block.get("data-post")
        if not post_id:
            continue
        msg_id = post_id.split("/")[-1]

        text_div = block.select_one(".tgme_widget_message_text")
        text = text_div.get_text("\n", strip=True) if text_div else ""
        if not text:
            continue  # skip photo/video-only posts with no caption

        link_tag = block.select_one("a.tgme_widget_message_date")
        link = link_tag["href"] if link_tag else f"https://t.me/{channel}/{msg_id}"

        messages.append({"id": msg_id, "text": text, "link": link})
    return messages


def classify_and_translate(source_text: str) -> dict:
    prompt = (
        "You help run a 'Good News Ethiopia' Telegram channel that reposts "
        "only genuinely positive news, translated into English, sourced from "
        "the Tikvah Ethiopia Telegram channel (a trusted Ethiopian news source). "
        "Given the post below (likely Amharic, possibly mixed with English), "
        "decide whether it is good news, whether it is safe and authentic to "
        "repost as-is (not an ad, not an unverified rumor, not speculation), "
        "translate it completely and accurately into English, and write a "
        "short English summary.\n\n---\n" + source_text
    )
    url = f"{GEMINI_API}/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    resp = requests.post(
        url,
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": CLASSIFY_SCHEMA,
            },
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    text_block = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text_block)


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
        "\U0001f4f0 <b>Good News from Ethiopia</b>\n\n"
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


def main() -> None:
    state = load_state()
    seen = set(state.get("seen_ids", []))

    all_messages = fetch_source_messages(SOURCE_CHANNEL)
    new_messages = [m for m in all_messages if m["id"] not in seen]
    new_messages = new_messages[-MAX_MESSAGES_PER_RUN:]

    if not new_messages:
        log.info("No new messages.")
        return

    posted = 0
    for msg in new_messages:
        try:
            result = classify_and_translate(msg["text"])
        except Exception:
            log.exception("Classification failed for message %s; will retry next run", msg["id"])
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
    main()
