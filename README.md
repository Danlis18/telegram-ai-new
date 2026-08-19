# Telegram AI News

Automated Ukrainian sports-news pipeline for Telegram, designed for Railway.

## Current pipeline

Telegram source channels -> Telethon reader -> duplicate guard -> AI editor/scoring -> queue -> Telegram Bot publisher.

`AUTO_PUBLISH=false` is the safe default. News is processed and stored but is not posted until the pipeline is reviewed.

## Railway variables

Copy the names from `.env.example` into Railway Variables. Never commit real tokens or Telegram sessions.

Required: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`, `TELEGRAM_BOT_TOKEN`, `TARGET_CHANNEL`, `OPENAI_API_KEY`.

## Telegram session

Install dependencies locally and run:

```bash
python scripts/create_session.py
```

Log in to the dedicated Telegram reader account. Copy the generated string directly into Railway as `TELEGRAM_SESSION`. Treat it like a password.

## Run

```bash
python -m app.main
```

Railway can deploy directly from this repository using the included `Dockerfile`.

## Safety / editorial rules

- No source advertising or promo codes are copied.
- AI is instructed not to invent facts, quotes, scores, dates or transfer fees.
- Exact duplicate source posts are suppressed.
- Low-confidence news is rejected.
- Automatic publishing remains disabled until manually enabled.

## Next milestones

1. Semantic cross-source duplicate clustering.
2. Media download + branded image pipeline.
3. Admin review bot with Publish / Regenerate / Skip buttons.
4. Source reliability weighting and multi-source verification.
5. Daily AI presenter/avatar module (later phase).
