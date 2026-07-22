from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_env_file() -> str | None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    for path in (Path.cwd() / ".env", root / ".env"):
        if path.is_file():
            return str(path)
    return None


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_resolve_env_file(), extra="ignore")

    web_username: str = "admin"
    web_password: str = "algoflat"
    jwt_secret: str = "change-me-in-production"
    jwt_expire_hours: int = 24


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    expires_at: str


_bearer = HTTPBearer(auto_error=False)
_settings: AuthSettings | None = None


def auth_settings() -> AuthSettings:
    global _settings
    if _settings is None:
        _settings = AuthSettings()
    return _settings


def create_access_token(username: str) -> tuple[str, datetime]:
    settings = auth_settings()
    expires = datetime.now(tz=timezone.utc) + timedelta(hours=settings.jwt_expire_hours)
    payload = {"sub": username, "exp": expires, "iat": datetime.now(tz=timezone.utc)}
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return token, expires


def verify_credentials(username: str, password: str) -> bool:
    settings = auth_settings()
    user_ok = secrets.compare_digest(username, settings.web_username)
    pass_ok = secrets.compare_digest(password, settings.web_password)
    return user_ok and pass_ok


def decode_token(token: str) -> dict[str, Any]:
    settings = auth_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session",
        ) from exc


async def require_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    token: str | None = None
    if credentials and credentials.scheme.lower() == "bearer":
        token = credentials.credentials
    if not token:
        token = request.cookies.get("access_token")
    if not token:
        token = request.query_params.get("access_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    payload = decode_token(token)
    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return str(username)
