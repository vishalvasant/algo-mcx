"""Flattrade credentials: DB storage with .env fallback."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import structlog

from algomcx.config import EnvSettings, get_env
from algomcx.db.connection import get_pool

logger = structlog.get_logger(__name__)

_CREDENTIALS_ROW_ID = 1
_cached: FlattradeConfig | None = None


@dataclass(frozen=True)
class FlattradeConfig:
    user_id: str | None
    api_key: str | None
    api_secret: str | None
    password: str | None
    totp_secret: str | None
    access_token: str | None
    redirect_url: str
    token_file: str

    def has_api_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def has_auto_login(self) -> bool:
        return bool(
            self.user_id
            and self.api_key
            and self.api_secret
            and self.password
            and self.totp_secret
        )

    @classmethod
    def from_env(cls, env: EnvSettings | None = None) -> FlattradeConfig:
        env = env or get_env()
        return cls(
            user_id=_clean(env.flattrade_user_id),
            api_key=_clean(env.flattrade_api_key),
            api_secret=_clean(env.flattrade_api_secret),
            password=_clean(env.flattrade_password),
            totp_secret=_clean(env.flattrade_totp_secret),
            access_token=_clean(env.flattrade_access_token),
            redirect_url=env.flattrade_redirect_url or "http://127.0.0.1:8000/callback",
            token_file=env.flattrade_token_file or ".flattrade/session.json",
        )

    @classmethod
    def from_db_row(cls, row: dict[str, Any], *, env: FlattradeConfig | None = None) -> FlattradeConfig:
        """Build config from the saved database row (Settings → Flattrade)."""
        env = env or cls.from_env()
        return cls(
            user_id=_clean(row.get("user_id")),
            api_key=_clean(row.get("api_key")),
            api_secret=_clean(row.get("api_secret")),
            password=_clean(row.get("password")),
            totp_secret=_clean(row.get("totp_secret")),
            access_token=env.access_token,
            redirect_url=_clean(row.get("redirect_url")) or env.redirect_url,
            token_file=env.token_file,
        )

    def merge_db(self, row: dict[str, Any] | None) -> FlattradeConfig:
        if not row:
            return self
        merged = self
        for field in (
            "user_id",
            "api_key",
            "api_secret",
            "password",
            "totp_secret",
        ):
            value = _clean(row.get(field))
            if value:
                merged = replace(merged, **{field: value})
        redirect = _clean(row.get("redirect_url"))
        if redirect:
            merged = replace(merged, redirect_url=redirect)
        return merged


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def mask_secret(value: str | None, *, head: int = 4, tail: int = 4) -> str | None:
    if not value:
        return None
    if len(value) <= head + tail:
        return "•" * len(value)
    return f"{value[:head]}{'•' * (len(value) - head - tail)}{value[-tail:]}"


def credentials_status(cfg: FlattradeConfig, *, source: str) -> dict[str, Any]:
    return {
        "source": source,
        "user_id": cfg.user_id,
        "api_key_masked": mask_secret(cfg.api_key),
        "api_secret_set": bool(cfg.api_secret),
        "password_set": bool(cfg.password),
        "totp_secret_set": bool(cfg.totp_secret),
        "redirect_url": cfg.redirect_url,
        "has_api_credentials": cfg.has_api_credentials(),
        "has_auto_login": cfg.has_auto_login(),
    }


async def _load_db_row() -> dict[str, Any] | None:
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT user_id, api_key, api_secret, password, totp_secret, redirect_url
                FROM flattrade_credentials
                WHERE id = $1
                """,
                _CREDENTIALS_ROW_ID,
            )
        return dict(row) if row else None
    except Exception:
        logger.exception("flattrade_credentials_load_failed")
        return None


def _detect_source(base: FlattradeConfig, merged: FlattradeConfig) -> str:
    db_fields = ("user_id", "api_key", "api_secret", "password", "totp_secret")
    from_db = any(getattr(merged, f) != getattr(base, f) for f in db_fields)
    from_env = any(getattr(base, f) for f in db_fields)
    if from_db and from_env:
        return "mixed"
    if from_db:
        return "database"
    if from_env:
        return "environment"
    return "none"


async def load_flattrade_config(*, force: bool = False) -> FlattradeConfig:
    global _cached
    if force:
        _cached = None
    elif _cached is not None:
        return _cached
    base = FlattradeConfig.from_env()
    row = await _load_db_row()
    if row and _clean(row.get("api_key")) and _clean(row.get("api_secret")):
        merged = FlattradeConfig.from_db_row(row, env=base)
    else:
        merged = base.merge_db(row)
    _cached = merged
    return merged


async def load_flattrade_credentials_status() -> dict[str, Any]:
    base = FlattradeConfig.from_env()
    row = await _load_db_row()
    if row and _clean(row.get("api_key")) and _clean(row.get("api_secret")):
        merged = FlattradeConfig.from_db_row(row, env=base)
    else:
        merged = base.merge_db(row)
    return credentials_status(merged, source=_detect_source(base, merged))


def invalidate_flattrade_config_cache() -> None:
    global _cached
    _cached = None


async def save_flattrade_credentials(updates: dict[str, Any]) -> dict[str, Any]:
    """Persist credentials; empty secret fields keep existing values."""
    current = await load_flattrade_config(force=True)
    pool = get_pool()

    user_id = _clean(updates.get("user_id")) or current.user_id
    api_key = _clean(updates.get("api_key")) or current.api_key
    api_secret = _clean(updates.get("api_secret")) or current.api_secret
    password = _clean(updates.get("password")) or current.password
    totp_secret = _clean(updates.get("totp_secret")) or current.totp_secret
    redirect_url = _clean(updates.get("redirect_url")) or current.redirect_url

    if not user_id or not api_key or not api_secret:
        raise ValueError("user_id, api_key, and api_secret are required")

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO flattrade_credentials (
                id, user_id, api_key, api_secret, password, totp_secret, redirect_url, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, now())
            ON CONFLICT (id) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                api_key = EXCLUDED.api_key,
                api_secret = EXCLUDED.api_secret,
                password = EXCLUDED.password,
                totp_secret = EXCLUDED.totp_secret,
                redirect_url = EXCLUDED.redirect_url,
                updated_at = now()
            """,
            _CREDENTIALS_ROW_ID,
            user_id,
            api_key,
            api_secret,
            password,
            totp_secret,
            redirect_url,
        )

    invalidate_flattrade_config_cache()
    merged = await load_flattrade_config(force=True)
    base = FlattradeConfig.from_env()
    status = credentials_status(merged, source=_detect_source(base, merged))
    logger.info("flattrade_credentials_saved", user_id=user_id, source=status["source"])
    return status
