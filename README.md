# Good News Ethiopia

A bot that scrapes Tikvah Ethiopia's public Telegram channels, uses Gemini to
find posts that are genuinely positive news (and filters out ads, rumors, and
negative news), translates them into English, and posts them to the
[Good News Ethiopia](https://t.me/good_news_ethiopia) Telegram channel.

Every post includes:

- A short English summary
- A link to the full English translation (hosted free on [Telegra.ph](https://telegra.ph))
- A link back to the original Tikvah post

## How it works

1. **Scrape** — fetches recent posts from each channel in `SOURCE_CHANNELS`
   via Telegram's public `t.me/s/<channel>` preview page (no login required).
2. **Pre-filter** — skips obvious ads (classified-ad phone numbers, spammed
   links, Tikvah's own "Partnership with Tikvah-Ethiopia" sponsor tag) before
   spending an API call on them.
3. **Classify + translate** — sends the remaining posts to Gemini in batches,
   asking it to decide whether each one is genuinely good news, whether it's
   authentic and safe to repost, and to produce a full English translation
   plus a short summary.
4. **Publish** — for anything that passes, publishes the full translation to
   Telegra.ph and posts the summary + both links to the Telegram channel.
5. **Track state** — processed post IDs are saved in `state.json` so reruns
   never repost the same story.

It's designed to run unattended every 30 minutes (see [Deployment](#deployment)).

## Setup

### 1. Create the Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` →
   follow the prompts. Save the token it gives you.
2. Create (or use an existing) Telegram channel for the bot to post to.
3. Add the bot as an **admin** of that channel (Channel settings →
   Administrators → Add Admin), with permission to post messages.

### 2. Get a Gemini API key

Get a free key from [Google AI Studio](https://aistudio.google.com/apikey).

> **Free tier quota varies by model.** `gemini-flash-latest` has hit a
> 20-requests/day wall on some keys. This bot defaults to
> `gemini-flash-lite-latest`, which has a much more workable free quota — if
> you change `GEMINI_MODEL`, sanity-check its quota first.

### 3. (Optional) Get your admin chat ID for failure alerts

If a run crashes, the bot can DM you directly instead of failing silently.

1. Message the bot you created (anything, e.g. "hi") — bots can't message you
   first, so you have to message them once.
2. Run:
   ```bash
   curl -s "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates" | python3 -m json.tool
   ```
3. Your chat ID is the `message.from.id` field in the response.

### 4. Configure environment variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | From BotFather. |
| `TARGET_CHAT` | Yes | The channel to post to, e.g. `@good_news_ethiopia`. Bot must be admin there. |
| `GEMINI_API_KEY` | Yes | From Google AI Studio. |
| `ADMIN_CHAT_ID` | No | Your personal Telegram chat ID, for crash DMs. |
| `SOURCE_CHANNELS` | No | Comma-separated Tikvah handles to scrape. Default: `tikvahethiopia,tikvahethmagazine`. |
| `GEMINI_MODEL` | No | Default: `gemini-flash-lite-latest`. |
| `MAX_MESSAGES_PER_RUN` | No | Cap on new posts processed per channel per run. Default: `20`. |
| `BATCH_SIZE` | No | How many posts go into a single Gemini call. Default: `5`. |

## Running locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python3 scraper.py
```

Each run fetches new posts, classifies/translates/posts anything that
qualifies, and updates `state.json`. Safe to run repeatedly — already-seen
posts are skipped.

## Deployment

This runs for free on **GitHub Actions**, with a scheduled workflow
(`.github/workflows/scraper.yml`) that checks for new posts and commits the
updated `state.json` back to the repo.

### 1. Push to GitHub and add secrets

```bash
gh repo create good-news-ethiopia --private --source=. --push
```

(Already done for this project: https://github.com/asgerami/good-news-ethiopia)

Add the required secrets (values from your `.env`):

```bash
printf '%s' "$TELEGRAM_BOT_TOKEN" | gh secret set TELEGRAM_BOT_TOKEN
printf '%s' "$TARGET_CHAT" | gh secret set TARGET_CHAT
printf '%s' "$GEMINI_API_KEY" | gh secret set GEMINI_API_KEY
printf '%s' "$ADMIN_CHAT_ID" | gh secret set ADMIN_CHAT_ID   # optional
```

### 2. Set up a reliable trigger (important)

GitHub's native `on: schedule` cron is **best-effort** — it's documented to
delay or drop runs "during periods of high load," and in practice this
showed up as runs landing every ~1 hour instead of every 30 minutes, with
occasional multi-hour gaps. `workflow_dispatch` (API-triggered) runs don't
have this problem — they start within seconds.

The workflow keeps `on: schedule` as a free backup, but for reliable timing,
use a free external cron service to call the dispatch API every 30 minutes:

1. Create a **fine-grained GitHub token** at
   https://github.com/settings/personal-access-tokens/new, scoped to only
   this repo, with **Actions: Read and write** permission (nothing else).
2. Sign up at [cron-job.org](https://cron-job.org) (free) and create a job:
   - **URL:** `https://api.github.com/repos/asgerami/good-news-ethiopia/actions/workflows/scraper.yml/dispatches`
   - **Method:** POST
   - **Schedule:** every 30 minutes
   - **Headers:**
     - `Accept: application/vnd.github+json`
     - `Authorization: Bearer <your token>`
     - `X-GitHub-Api-Version: 2022-11-28`
     - `Content-Type: application/json`
   - **Body:** `{"ref":"main"}`
3. Use the "Test run" button — a `204 No Content` response means it worked.

## Maintenance

- **Check recent runs:** `gh run list --workflow=scraper.yml`
- **Watch a run live:** `gh run watch`
- **Trigger a run manually:** `gh workflow run scraper.yml`
- **Crashes** (as opposed to routine skipped/retried posts) DM `ADMIN_CHAT_ID`
  on Telegram automatically, with a link to the failed run.
- **Rotate the cron-job.org token** periodically, or if it's ever been pasted
  somewhere it shouldn't have (e.g. accidentally shared in a screenshot) —
  revoke it at https://github.com/settings/personal-access-tokens and issue
  a new one.

## Files

| File | Purpose |
|---|---|
| `scraper.py` | The whole bot — scrape, filter, classify, translate, publish, post. |
| `state.json` | Tracks processed post IDs and the Telegra.ph account token. Committed by the workflow after each run. |
| `.env.example` | Template for local configuration. |
| `.github/workflows/scraper.yml` | The scheduled GitHub Actions workflow. |
| `requirements.txt` | Python dependencies. |
