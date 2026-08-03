# API Reference

Base URL is the deployment origin. **Auth** column: 🔓 public · 🔐 Bearer JWT required ·
👑 admin only. JSON everywhere unless noted.

## Auth

| Method | Path | Auth | Body / Query | Returns |
|--------|------|:---:|--------------|---------|
| GET | `/api/auth/providers` | 🔓 | — | `{providers: string[]}` (configured socials) |
| GET | `/api/auth/{provider}/login` | 🔓 | — | 302 → provider |
| GET | `/api/auth/{provider}/callback` | 🔓 | `code, state` | 302 → `{FRONTEND_URL}/#/auth/callback?token=…` |
| POST | `/api/auth/register` | 🔓 | `{email, password, name?}` | `201 {token, user}` · `400` invalid · `409` taken |
| POST | `/api/auth/login` | 🔓 | `{email, password}` | `{token, user}` · `401` wrong creds |
| GET | `/api/auth/me` | 🔐 | — | `{id, email, name, avatar_url, provider, is_admin}` |

Logout is client-side (drop the token). There is no server session to revoke.

## Study

| Method | Path | Auth | Notes |
|--------|------|:---:|-------|
| GET | `/api/config` | 🔓 | Static: ratings, retention, algorithm |
| GET | `/api/index` | 🔐 | Theme/subtheme tree with counts for the filters |
| POST | `/api/questions/random` | 🔐 | Body `{theme?, subtheme?, answered_only, mode, exclude[]}`; picks the next card (FSRS order). `404` if the pool is empty |
| POST | `/api/reviews` | 🔐 | Body `{question_id, rating:1–4}`; grades a card, advances FSRS |
| GET | `/api/stats` | 🔐 | Per-card aggregates + `orphaned` flag |

## Cards

| Method | Path | Auth | Notes |
|--------|------|:---:|-------|
| GET | `/api/cards` | 🔐 | Query `theme?, subtheme?, search?, limit, offset` → `{cards, total}` |
| POST | `/api/cards` | 🔐 | Create. **`429`** if over `DAILY_CARD_LIMIT` in the last 24h |
| GET | `/api/cards/{id}` | 🔐 | Full card (raw ProseMirror docs). `404` if not yours |
| GET | `/api/cards/{id}/study` | 🔐 | Study-shaped view (rendered HTML + progress) for inline-edit refresh |
| PUT | `/api/cards/{id}` | 🔐 | Update (partial). `404` if not yours |
| DELETE | `/api/cards/{id}` | 🔐 | `204`. `404` if not yours |

`CardIn` = `{question, answer, theme, subtheme, tags[], position?}` where `question`/
`answer` are ProseMirror docs (`{type:"doc", content:[…]}`).

## Optimizer (per user)

| Method | Path | Auth | Notes |
|--------|------|:---:|-------|
| GET | `/api/optimizer/status` | 🔐 | Effective-review count, readiness, current weight source |
| POST | `/api/optimizer/run` | 🔐 | Train + apply this user's weights. `400` too few reviews · `501` optimizer extra not installed |

## Admin & assets

| Method | Path | Auth | Notes |
|--------|------|:---:|-------|
| POST | `/api/admin/claim-orphans` | 👑 | Assign unowned rows to the caller → `{claimed:{…}, user_id}` |
| POST | `/api/admin/purge-orphaned` | 👑 | **Delete** the caller's review history not linked to a real card (the "removed from source" rows). Cards untouched → `{deleted: {progress, reviews}}` |
| POST | `/api/admin/restore-orphaned` | 👑 | Rebuild cards from the caller's dangling study history (text only, no answers), reusing the old id so history reconnects; additive, skips existing → `{restored, skipped_duplicates}` |
| POST | `/api/admin/import-gitbook` | 👑 | Re-import the GitBook source into the caller's account. Adds missing questions; for questions already present it **fills an empty answer** from the source (never overwrites) and **reconnects dangling history** to that card → `{imported, filled_answers, reconnected, total_cards}` |
| GET | `/asset?path=…` | 🔓 | Image proxy for already-imported cards (public: `<img>` can't send Bearer) |
| GET | `/` | 🔓 | `{service, docs}` |

Interactive schema (Swagger UI) is served at **`/docs`** by FastAPI.
