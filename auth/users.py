"""User lookup / provisioning from a normalised OAuth profile.

Identity rule (chosen for this app):
- A (provider, subject) pair is the stable key for a login.
- New logins that carry a *verified* email are linked to an existing account with the
  same email, so Google/GitHub/Yandex for one person converge on one account.
- VK without email (or any unverified email) becomes its own account.
Admin status is re-evaluated from ADMIN_EMAILS on every login.
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy import text
from sqlmodel import Session, select

from gitbook.config import canonical_email, get_settings
from gitbook.models import Card, FsrsParams, OAuthAccount, Progress, Review, User

from .oauth import Profile
from .passwords import hash_password, verify_password


class EmailTaken(Exception):
    """Registration attempted with an email that already has an account."""


class BadCredentials(Exception):
    """Email/password login failed (unknown email, no password set, or wrong password)."""


class UserRepository:
    def __init__(self, engine) -> None:
        self.engine = engine

    def get(self, user_id: str) -> User | None:
        with Session(self.engine) as session:
            return session.get(User, user_id)

    # --------------------------------------------------- one-time email dedupe

    def dedupe_by_email(self) -> dict[str, int]:
        """Normalise stored emails to canonical form and merge accounts that turn out to
        be the same mailbox (e.g. Gmail dot/alias variants registered before
        canonicalization existed). Idempotent; safe to run on every boot.

        Guarded by a Postgres advisory lock so concurrent workers don't race. For each
        canonical-email group the primary is the admin, else the earliest account; the
        others' cards/progress/reviews/weights/oauth links move to it and they're deleted.
        """
        merged = normalized = 0
        with Session(self.engine) as session:
            session.execute(text("SELECT pg_advisory_xact_lock(4242424242)"))
            groups: dict[str, list[User]] = {}
            for user in session.exec(select(User)).all():
                canon = canonical_email(user.email)
                if canon:
                    groups.setdefault(canon, []).append(user)

            for canon, members in groups.items():
                if len(members) == 1:
                    if members[0].email != canon:
                        members[0].email = canon
                        session.add(members[0]); normalized += 1
                    continue
                # admin first, then earliest created — that account absorbs the rest.
                members.sort(key=lambda u: (not u.is_admin, u.created_at or 0.0))
                primary, dups = members[0], members[1:]
                for dup in dups:
                    for model in (Card, Progress, Review, FsrsParams, OAuthAccount):
                        for row in session.exec(select(model).where(model.user_id == dup.id)).all():
                            row.user_id = primary.id
                            session.add(row)
                    primary.password_hash = primary.password_hash or dup.password_hash
                    primary.avatar_url = primary.avatar_url or dup.avatar_url
                    primary.name = primary.name or dup.name
                    session.delete(dup); merged += 1
                primary.email = canon
                primary.is_admin = get_settings().is_admin_email(canon)
                session.add(primary)

            session.commit()
        if merged or normalized:
            print(f"[users] email dedupe: merged {merged} duplicate account(s), "
                  f"normalized {normalized} email(s)")
        return {"merged": merged, "normalized": normalized}

    # --------------------------------------------------------- email / password

    def register_password(self, email: str, password: str, name: str = "") -> User:
        """Create a new email/password account. Fails if the email is already in use."""
        settings = get_settings()
        email = canonical_email(email) or email.strip().lower()
        with Session(self.engine) as session:
            existing = session.exec(select(User).where(User.email == email)).first()
            if existing is not None:
                # An email may belong to a social account (no password) — we still refuse,
                # because password registration doesn't prove ownership of the address.
                raise EmailTaken(email)
            user = User(
                id=uuid.uuid4().hex, email=email, name=name or email.split("@")[0],
                provider="password", password_hash=hash_password(password),
                is_admin=settings.is_admin_email(email), created_at=time.time(),
                last_login=time.time(),
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            return user

    def authenticate_password(self, email: str, password: str) -> User:
        email = canonical_email(email) or email.strip().lower()
        with Session(self.engine) as session:
            user = session.exec(select(User).where(User.email == email)).first()
            if user is None or not verify_password(password, user.password_hash):
                raise BadCredentials()
            # Keep admin status and last_login fresh on each login.
            user.is_admin = get_settings().is_admin_email(user.email)
            user.last_login = time.time()
            session.add(user)
            session.commit()
            session.refresh(user)
            return user

    def upsert_from_profile(self, profile: Profile) -> User:
        settings = get_settings()
        now = time.time()
        with Session(self.engine) as session:
            link = session.exec(
                select(OAuthAccount).where(
                    OAuthAccount.provider == profile.provider,
                    OAuthAccount.subject == profile.subject,
                )
            ).first()

            user: User | None = session.get(User, link.user_id) if link else None

            canon = canonical_email(profile.email)
            # Link by verified email to an existing account when this identity is new.
            if user is None and canon and profile.email_verified:
                user = session.exec(select(User).where(User.email == canon)).first()

            if user is None:
                user = User(id=uuid.uuid4().hex, created_at=now)
                session.add(user)

            if link is None:
                session.add(OAuthAccount(
                    provider=profile.provider, subject=profile.subject,
                    user_id=user.id, created_at=now,
                ))

            # Refresh mutable profile fields + admin flag on each login.
            user.provider = profile.provider
            user.name = profile.name or user.name
            user.avatar_url = profile.avatar or user.avatar_url
            if canon and (profile.email_verified or not user.email):
                user.email = canon
            user.is_admin = settings.is_admin_email(user.email)
            user.last_login = now

            session.add(user)
            session.commit()
            session.refresh(user)
            return user

    def claim_orphans(self, user_id: str) -> dict[str, int]:
        """Assign all pre-multi-user rows (empty user_id) to this user. Admin one-shot."""
        counts: dict[str, int] = {}
        with Session(self.engine) as session:
            for name, model in (("cards", Card), ("progress", Progress),
                                ("reviews", Review), ("fsrs_params", FsrsParams)):
                rows = session.exec(select(model).where(model.user_id == "")).all()
                for row in rows:
                    row.user_id = user_id
                    session.add(row)
                counts[name] = len(rows)
            session.commit()
        return counts
