"""Telegram Bot API delivery for trading alerts."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import structlog

from algomcx.config import get_env
from algomcx.db.connection import get_pool
from algomcx.notifications.policy import is_important_alert

logger = structlog.get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_CHAT_KEY = "telegram_chat_id"


def _parse_enabled(raw: str | None, *, has_token: bool) -> bool:
    if raw is None or raw.strip() == "":
        return has_token
    return raw.strip().lower() in ("1", "true", "yes", "on")


class TelegramNotifier:
    def __init__(
        self,
        *,
        token: str | None,
        chat_id: str | None,
        enabled: bool,
    ) -> None:
        self._token = (token or "").strip() or None
        self._chat_id = (chat_id or "").strip() or None
        self._enabled = enabled and bool(self._token)

    @property
    def configured(self) -> bool:
        return bool(self._token)

    @property
    def linked(self) -> bool:
        return bool(self._chat_id)

    @property
    def ready(self) -> bool:
        return self._enabled and self.configured and self.linked

    def status(self) -> dict[str, object]:
        return {
            "enabled": self._enabled,
            "configured": self.configured,
            "linked": self.linked,
            "ready": self.ready,
            "chat_id": self._chat_id,
        }

    def set_chat_id(self, chat_id: str | None) -> None:
        self._chat_id = (chat_id or "").strip() or None

    async def load_chat_id_from_db(self) -> str | None:
        if self._chat_id:
            return self._chat_id
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchval(
                    "SELECT value FROM engine_settings WHERE key = $1",
                    _CHAT_KEY,
                )
            if row:
                self._chat_id = str(row).strip()
            return self._chat_id
        except Exception:
            logger.exception("telegram_chat_id_load_failed")
            return None

    async def save_chat_id(self, chat_id: str) -> None:
        chat_id = str(chat_id).strip()
        self._chat_id = chat_id
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO engine_settings (key, value, updated_at)
                VALUES ($1, $2, now())
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    updated_at = now()
                """,
                _CHAT_KEY,
                chat_id,
            )

    async def get_bot_info(self) -> dict[str, object]:
        if not self._token:
            return {"ok": False, "error": "bot_token_missing"}
        return await self._api("getMe")

    async def discover_chat_from_updates(self) -> str | None:
        if not self._token:
            return None
        data = await self._api("getUpdates")
        if not data.get("ok"):
            return None
        for item in reversed(data.get("result") or []):
            message = item.get("message") or item.get("edited_message")
            if not message:
                continue
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            if chat_id is not None:
                chat_id_str = str(chat_id)
                await self.save_chat_id(chat_id_str)
                return chat_id_str
        return None

    async def send_alert(
        self,
        *,
        type_: str,
        severity: str,
        title: str,
        message: str,
    ) -> bool:
        if not self._enabled or not self._token:
            return False
        if not is_important_alert(type_, title):
            return False
        await self.load_chat_id_from_db()
        if not self._chat_id:
            logger.debug("telegram_skip_no_chat_id", title=title)
            return False
        text = _format_alert(
            title=title,
            message=message,
            severity=severity,
            type_=type_,
        )
        data = await self._api(
            "sendMessage",
            json={
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        if data.get("ok"):
            return True
        logger.warning(
            "telegram_send_failed",
            title=title,
            error=data.get("description"),
        )
        return False

    async def send_test(self) -> dict[str, object]:
        if not self._token:
            return {"ok": False, "error": "bot_token_missing"}
        await self.load_chat_id_from_db()
        if not self._chat_id:
            discovered = await self.discover_chat_from_updates()
            if not discovered:
                return {
                    "ok": False,
                    "error": "chat_not_linked",
                    "hint": (
                        "Open Telegram, search your bot, tap Start, "
                        "then call POST /control/telegram/link"
                    ),
                }
        now = datetime.now(IST).strftime("%H:%M:%S IST")
        text = (
            f"✅ <b>Algo-MCX connected</b>\n"
            f"Telegram alerts are working.\n"
            f"<i>{now}</i>"
        )
        data = await self._api(
            "sendMessage",
            json={
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
            },
        )
        ok = bool(data.get("ok"))
        if not ok:
            return {"ok": False, "error": data.get("description"), **self.status()}
        return {"ok": True, **self.status()}

    async def _api(self, method: str, *, json: dict | None = None) -> dict:
        if not self._token:
            return {"ok": False, "description": "bot_token_missing"}
        url = _API_BASE.format(token=self._token, method=method)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=json or {})
                return resp.json()
        except Exception as exc:
            logger.exception("telegram_api_error", method=method)
            return {"ok": False, "description": str(exc)}


def _format_alert(
    *,
    title: str,
    message: str,
    severity: str,
    type_: str | None = None,
) -> str:
    if type_ == "trade":
        title_lower = title.lower()
        if "sell" in title_lower or "exit" in title_lower:
            icon = "✅" if severity != "warning" else "🔻"
        else:
            icon = "🟢"
    else:
        icon = {"critical": "🔴", "warning": "🟠", "info": "🔵"}.get(severity, "⚪")
    ts = datetime.now(IST).strftime("%d %b %H:%M IST")
    return f"{icon} <b>{_escape_html(title)}</b>\n{_escape_html(message)}\n<i>{ts}</i>"


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


_notifier: TelegramNotifier | None = None


def get_telegram_notifier() -> TelegramNotifier:
    global _notifier
    if _notifier is None:
        env = get_env()
        token = getattr(env, "telegram_bot_token", None)
        chat_id = getattr(env, "telegram_chat_id", None)
        enabled = _parse_enabled(
            getattr(env, "telegram_alerts_enabled", None),
            has_token=bool(token),
        )
        _notifier = TelegramNotifier(
            token=token,
            chat_id=chat_id,
            enabled=enabled,
        )
    return _notifier


async def maybe_send_telegram_alert(
    *,
    type_: str,
    severity: str,
    title: str,
    message: str,
) -> None:
    try:
        await get_telegram_notifier().send_alert(
            type_=type_,
            severity=severity,
            title=title,
            message=message,
        )
    except Exception:
        logger.exception("telegram_alert_dispatch_failed", title=title)
