# Authentication & Authorization

Two ways to sign in, both ending in the **same session JWT**:

1. **Social OAuth2** — Google, GitHub, Yandex, VK.
2. **Email + password** — bcrypt-hashed, stored on the user row.

## The session token

- HS256 JWT signed with `JWT_SECRET`. Claims: `sub` (user id), `email`, `is_admin`,
  `iat`, `exp`, `typ:"session"`. TTL = `JWT_TTL_DAYS` (default 30).
- Sent by the client as `Authorization: Bearer <jwt>` on every API call.
- `auth/deps.py::get_current_user` validates it and loads the `User`; `require_admin`
  additionally checks `is_admin`. A bad/expired token → `401`.
- There is **no refresh token** in v1 — when it expires the user logs in again.

## Social login

Endpoints:

- `GET /api/auth/providers` → `{"providers": [...]}` — only providers whose credentials
  are configured (so the client renders just those buttons).
- `GET /api/auth/{provider}/login` → 302 to the provider, carrying a signed, 10-minute
  `state` token (CSRF protection — it can't be forged without `JWT_SECRET`).
- `GET /api/auth/{provider}/callback?code&state` → verifies state, exchanges the code for
  the provider profile, upserts the user, issues our JWT, and 302s to
  `{FRONTEND_URL}/#/auth/callback?token=<jwt>` (or `?error=<kind>`).

Provider specifics (`auth/oauth.py`), all normalised to `Profile(provider, subject,
email, email_verified, name, avatar)`:

| Provider | Notes |
|----------|-------|
| Google | OpenID Connect; `email_verified` honoured. |
| GitHub | Primary **verified** email fetched from `/user/emails`. |
| Yandex | `login.yandex.ru/info`; email treated as verified. |
| VK | Email only if the user granted it; often absent → the account can't email-link. |

### Registering the OAuth apps

For each provider you want, create an app and set its redirect/callback URL to:

```
{PUBLIC_BACKEND_URL}/api/auth/{provider}/callback
```

| Provider | Console | Scopes |
|----------|---------|--------|
| Google | console.cloud.google.com/apis/credentials (Web OAuth client) | `openid email profile` |
| GitHub | github.com/settings/developers (OAuth App) | `read:user user:email` |
| Yandex | oauth.yandex.com/client/new | `login:email login:info` |
| VK | dev.vk.com (Website app, enable Email) | `email` |

Then set `<PROVIDER>_CLIENT_ID` / `<PROVIDER>_CLIENT_SECRET` (see
[deployment.md](deployment.md)). A provider with either value missing is simply hidden.

## Email / password

- `POST /api/auth/register` `{email, password, name?}` → `201 {token, user}`.
  Validates email shape and `password ≥ 8` chars. `409` if the email already exists.
- `POST /api/auth/login` `{email, password}` → `200 {token, user}`. `401` on mismatch.
- Passwords are bcrypt-hashed (`auth/passwords.py`); only the hash is stored, in
  `users.password_hash`.

## Email canonicalization

Emails are normalised (`gitbook/config.py::canonical_email`) before matching or storing, so
aliases of one mailbox are treated as the same account:

- lowercased + trimmed; `+tag` suffix dropped;
- for Gmail (`gmail.com`/`googlemail.com`) dots in the local part are removed and the domain
  unified — Gmail ignores both, so `e.didar.2001@gmail.com` = `e.didar2001@gmail.com`.

This is applied to `ADMIN_EMAILS` matching, password registration/login, and link-by-email.
The canonical form is what's stored in `users.email` (and shown in the UI).

**One-time cleanup on boot:** `UserRepository.dedupe_by_email()` (run at startup from
`main.py`, under a Postgres advisory lock) normalises any pre-canonicalization emails and
**merges** accounts that resolve to the same mailbox — the admin (else earliest) account
absorbs the others' cards/progress/reviews/weights/oauth links, taking their password/
avatar/name if it lacked them. Idempotent. This is what heals duplicate accounts created
before canonicalization existed (e.g. `e.didar.2001@` and `e.didar2001@` registered
separately).

## Account identity & linking

- A `(provider, subject)` pair is the stable key for a social login (`oauth_accounts`).
- A **new** social login carrying a *verified* email is linked to an existing account
  with the same email — so Google/GitHub/Yandex/**email-password** for one person converge
  on a single account and card set.
- VK without email (or any unverified email) becomes its own account.
- Password registration refuses an email that already exists (it doesn't prove ownership
  of the address). Verified social login on that email later links to it.

## Admin

- `ADMIN_EMAILS` (comma-separated) lists admin addresses. `is_admin` is re-evaluated on
  **every** login, so you can promote/demote by editing the env var.
- Admin-only routes use `require_admin` (`403` otherwise). Currently:
  `POST /api/admin/claim-orphans`.
- **Auto-claim:** whenever an admin logs in, `main._session_for` calls
  `users.claim_orphans`, assigning any pre-multi-user rows (empty `user_id`) to them. This
  is how the original single-user deck lands in the admin account, with no manual step.
  It's idempotent — once claimed there's nothing left to claim. See
  [data-model.md](data-model.md#orphan-rows--claiming).

## Security notes / limitations (v1)

- No login rate-limiting / lockout yet — consider a reverse-proxy or app-level throttle.
- No email verification for password signup; ownership is only proven via social login.
- No password reset flow yet.
- CORS is controlled by `CORS_ORIGINS`. The `/asset` image proxy is intentionally public
  (an `<img>` can't send a Bearer header) and serves non-sensitive imported images only.
