from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pyotp
import structlog

from algomcx.broker.credentials import FlattradeConfig

logger = structlog.get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
AUTH_URL = "https://auth.flattrade.in/"
TOKEN_API_URL = "https://authapi.flattrade.in/trade/apitoken"
SESSION_API_URL = "https://authapi.flattrade.in/auth/session"
FT_AUTH_URL = "https://authapi.flattrade.in/ftauth"

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://auth.flattrade.in/",
}


@dataclass(frozen=True)
class FlattradeSession:
    user_id: str
    access_token: str
    obtained_at: datetime
    expires_at: datetime
    source: str

    @property
    def is_valid(self) -> bool:
        return datetime.now(tz=IST) < self.expires_at.astimezone(IST)

    def to_dict(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "access_token": self.access_token,
            "obtained_at": self.obtained_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FlattradeSession:
        return cls(
            user_id=str(data["user_id"]),
            access_token=str(data["access_token"]),
            obtained_at=datetime.fromisoformat(str(data["obtained_at"])),
            expires_at=datetime.fromisoformat(str(data["expires_at"])),
            source=str(data.get("source", "file")),
        )


def generate_api_secret_hash(api_key: str, request_code: str, api_secret: str) -> str:
    """SHA256(api_key + request_code + api_secret) per Flattrade OAuth docs."""
    payload = f"{api_key}{request_code}{api_secret}"
    return hashlib.sha256(payload.encode()).hexdigest()


def build_authorization_url(api_key: str) -> str:
    return f"{AUTH_URL}?app_key={api_key}"


def parse_redirect_url(redirect_url: str) -> tuple[str, int, str]:
    parsed = urlparse(redirect_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/callback"
    return host, port, path


def next_token_expiry(now: datetime | None = None) -> datetime:
    """Flattrade tokens expire daily at 05:00 IST."""
    current = (now or datetime.now(tz=IST)).astimezone(IST)
    expiry = current.replace(hour=5, minute=0, second=0, microsecond=0)
    if current >= expiry:
        expiry += timedelta(days=1)
    return expiry


async def exchange_request_code(
    api_key: str,
    request_code: str,
    api_secret: str,
) -> FlattradeSession:
    hashed = generate_api_secret_hash(api_key, request_code, api_secret)
    payload = {
        "api_key": api_key,
        "request_code": request_code,
        "api_secret": hashed,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(TOKEN_API_URL, json=payload)
        response.raise_for_status()
        data = response.json()

    if data.get("stat") != "Ok" or not data.get("token"):
        emsg = str(data.get("emsg", "Flattrade token exchange failed"))
        if "INVALID_IP" in emsg.upper():
            raise ConnectionError(
                f"{emsg} — register your public IP in Flattrade Wall → Pi → API Key → Primary IP"
            )
        raise ConnectionError(emsg)

    now = datetime.now(tz=IST)
    user_id = str(data.get("client") or "")
    return FlattradeSession(
        user_id=user_id,
        access_token=str(data["token"]),
        obtained_at=now,
        expires_at=next_token_expiry(now),
        source="oauth",
    )


class FlattradeSessionStore:
    def __init__(self, token_file: Path) -> None:
        self._token_file = token_file

    def load(self) -> FlattradeSession | None:
        if not self._token_file.exists():
            return None
        try:
            data = json.loads(self._token_file.read_text(encoding="utf-8"))
            session = FlattradeSession.from_dict(data)
            if session.is_valid:
                return session
            logger.warning("flattrade_token_expired", expires_at=session.expires_at.isoformat())
        except Exception:
            logger.exception("flattrade_token_load_failed", path=str(self._token_file))
        return None

    def save(self, session: FlattradeSession) -> None:
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        self._token_file.write_text(
            json.dumps(session.to_dict(), indent=2),
            encoding="utf-8",
        )
        try:
            self._token_file.chmod(0o600)
        except OSError:
            pass
        logger.info(
            "flattrade_token_saved",
            path=str(self._token_file),
            expires_at=session.expires_at.isoformat(),
        )

    async def persist_to_db(self, pool: Any, session: FlattradeSession) -> None:
        await pool.execute(
            """
            INSERT INTO broker_sessions (user_id, access_token, expires_at, api_version)
            VALUES ($1, $2, $3, 'v2')
            """,
            session.user_id,
            session.access_token,
            session.expires_at,
        )


def resolve_session(
    cfg: FlattradeConfig,
    token_file: Path | None = None,
) -> FlattradeSession | None:
    """Resolve active session from env var or token file cache."""
    if cfg.access_token:
        user_id = cfg.user_id or ""
        now = datetime.now(tz=IST)
        return FlattradeSession(
            user_id=user_id,
            access_token=cfg.access_token,
            obtained_at=now,
            expires_at=next_token_expiry(now),
            source="env",
        )

    path = token_file or Path(cfg.token_file or ".flattrade/session.json")
    return FlattradeSessionStore(path).load()


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def generate_totp_code(secret: str) -> str:
    return pyotp.TOTP(secret.replace(" ", "")).now()


def extract_request_code(redirect_url: str) -> str:
    parsed = urlparse(redirect_url)
    params = parse_qs(parsed.query)
    code = params.get("code", [None])[0]
    if not code:
        raise ValueError(f"No request code in redirect URL: {redirect_url}")
    return str(code)


async def automated_login_with_credentials(
    user_id: str,
    password: str,
    api_key: str,
    api_secret: str,
    totp_secret: str,
) -> FlattradeSession:
    """Headless login via ftauth (password SHA256 + TOTP). See Flattrade auth API flow."""
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        sid_response = await client.post(SESSION_API_URL, headers=_BROWSER_HEADERS)
        sid_response.raise_for_status()
        sid = sid_response.text.strip().strip('"')
        if not sid:
            raise ConnectionError("Failed to obtain Flattrade auth session id")

        await client.get(
            f"{AUTH_URL}",
            params={"app_key": api_key, "sid": sid},
            headers=_BROWSER_HEADERS,
        )

        totp = generate_totp_code(totp_secret)
        auth_payload = {
            "UserName": user_id,
            "Password": sha256_hex(password),
            "PAN_DOB": totp,
            "App": "",
            "ClientID": "",
            "Key": "",
            "APIKey": api_key,
            "Sid": sid,
            "Override": "",
            "Source": "AUTHPAGE",
        }
        auth_response = await client.post(
            FT_AUTH_URL,
            json=auth_payload,
            headers=_BROWSER_HEADERS,
        )
        auth_response.raise_for_status()
        auth_data = auth_response.json()

        redirect_url = auth_data.get("RedirectURL") or auth_data.get("redirectURL")
        if not redirect_url:
            raise ConnectionError(auth_data.get("emsg", "ftauth did not return RedirectURL"))

        request_code = extract_request_code(redirect_url)

    session = await exchange_request_code(api_key, request_code, api_secret)
    user_id_final = session.user_id or user_id
    return FlattradeSession(
        user_id=user_id_final,
        access_token=session.access_token,
        obtained_at=session.obtained_at,
        expires_at=session.expires_at,
        source="auto_oauth",
    )


async def login_and_save(cfg: FlattradeConfig, *, force: bool = False) -> FlattradeSession:
    """Try automated login if password+TOTP configured; otherwise raise with browser hint."""
    if not cfg.api_key or not cfg.api_secret:
        raise ValueError("FLATTRADE_API_KEY and FLATTRADE_API_SECRET are required")
    if not cfg.user_id:
        raise ValueError("FLATTRADE_USER_ID is required")

    store = FlattradeSessionStore(Path(cfg.token_file))
    if not force:
        existing = store.load()
        if existing and existing.is_valid:
            return existing

    if cfg.password and cfg.totp_secret:
        logger.info("flattrade_auto_login_start", user_id=cfg.user_id)
        session = await automated_login_with_credentials(
            user_id=cfg.user_id,
            password=cfg.password,
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
            totp_secret=cfg.totp_secret,
        )
        store.save(session)
        return session

    raise RuntimeError(
        "No valid token. Either set FLATTRADE_PASSWORD + FLATTRADE_TOTP_SECRET for auto login, "
        "or run: python scripts/flattrade_login.py (browser OAuth)"
    )


async def ensure_session(cfg: FlattradeConfig) -> FlattradeSession:
    session = resolve_session(cfg)
    if session and session.is_valid:
        return session

    if cfg.password and cfg.totp_secret and cfg.api_key:
        return await login_and_save(cfg)

    raise RuntimeError(
        "No valid Flattrade session. Run: python scripts/flattrade_login.py "
        "(token expires daily at 05:00 IST)"
    )
