# Auto Posting · SPORTS NEWS

Production Telegram sports-news platform for Railway.

One codebase powers two control surfaces:

- **Telegram admin bot** — fast moderation, editing, scheduling and publishing.
- **Telegram Mini App** — mobile-first workspace for queue, channels, routing, quotas, AI and system status.

## Production flow

```text
Telegram sources ─┐
                  ├─> filtering / dedupe / ranking ─> AI rewrite ─> media pipeline ─> moderation queue
Web sport feeds ──┘                                                        │
                                                                            ├─> manual publish
Daily fixtures -------------------------------------------------------------└─> automatic publish
```

Final publication prefers the configured **Premium Telegram user account** so custom emoji are preserved. If that account cannot write to a specific target, the system can fall back to the Bot API instead of losing the post.

## Runtime architecture

The Railway container starts only:

```bash
python -m app.safe_entrypoint
```

`safe_entrypoint.py` is intentionally tiny. Production startup lives in `app/runtime_bootstrap.py` and follows one deterministic sequence:

1. persistent storage + backups
2. SOCKS5 preflight
3. workspace-aware OpenAI routing
4. editorial / Ukrainian / Premium emoji policies
5. album + media runtime
6. source and channel routing
7. Premium publisher initialization
8. Mini App API/server
9. match + web discovery workers
10. Telegram reader + admin bot

This keeps startup easy to audit without changing the proven feature modules.

## Important modules

| Area | Module |
| --- | --- |
| Production bootstrap | `app/runtime_bootstrap.py` |
| Telegram reader | `app/main.py` |
| Admin bot | `app/admin_bot.py` |
| AI text + image processing | `app/ai_editor.py` |
| Persistent data | `app/database.py`, `app/persistence_runtime.py` |
| Multi-user workspaces | `app/tenant.py`, `app/multiuser_ui.py` |
| Channel/source routing | `app/channel_workspace.py` |
| Premium publishing | `app/user_publisher.py` |
| Premium emoji | `app/premium_emoji_support.py`, `app/premium_emoji_registry.py` |
| Telegram source catalog + policy | `app/source_whitelist.py` |
| Web discovery | `app/web_news_ingest.py` |
| Daily top matches | `app/match_schedule.py` |
| Editorial guardrails | `app/editorial_policy_runtime.py`, `app/ukrainian_output_runtime.py` |
| Mini App backend | `app/miniapp_server.py` |
| Mini App frontend | `miniapp/index.html`, `miniapp/app.css`, `miniapp/app.js` |

## Mini App

Frontend production assets are intentionally consolidated into three files:

```text
miniapp/
├── index.html
├── app.css
└── app.js
```

The former UI fragments were bundled in their original order so behavior stays the same while deployment, caching and maintenance become much simpler.

## Persistent storage

Railway filesystem is ephemeral. Attach a Railway Volume with mount path:

```text
/data
```

When `RAILWAY_VOLUME_MOUNT_PATH` is present the app automatically stores SQLite at:

```text
/data/sports_news.db
```

The persistence worker also keeps rolling database snapshots on durable storage.

## OpenAI

`OPENAI_API_KEY` is the shared Railway fallback.

Every authorized workspace can also save its own encrypted API key from the Telegram bot or Mini App. AI requests use the workspace key first and fall back to the Railway key when appropriate.

## Editorial baseline

- final post text is Ukrainian, including translated quotes
- foreign time zones are converted to Europe/Kyiv and the zone label is removed
- advertising / promo links are filtered
- Russia-related sports content is hard-blocked
- web candidates are ranked for freshness and relevance before AI spend
- generated web creatives are photography-first, typography-free and branded by code
- existing source photos are preserved as closely as possible during cleanup
- multi-photo albums are retained and processed together

## Railway checklist

Required in normal production:

- `TELEGRAM_BOT_TOKEN`
- `ADMIN_USER_ID`
- `TARGET_CHANNEL`
- `OPENAI_API_KEY`
- authorized reader session
- authorized Premium publisher session
- SOCKS5 values when proxy mode is enabled
- Railway Volume mounted at `/data`

Do not commit real API keys, Telegram sessions or proxy credentials.

## Quality gates

Every push to `main` runs `.github/workflows/quality.yml`. The structural check verifies:

- Python syntax for every application module
- internal `app.*` imports point to real modules
- Mini App ships only the three production assets
- Docker/Railway entrypoint contract stays intact
- session/database/.env runtime artifacts are not committed
- the approved Telegram source catalog has one source of truth

## Development rule

Prefer small additive changes and verify every production commit on Railway. Do not rewrite working publication, session, persistence or routing logic merely for style. Remove obsolete compatibility code only after the active replacement is wired and deployed.
