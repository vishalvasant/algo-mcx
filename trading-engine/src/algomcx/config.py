from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_yaml(name: str) -> dict[str, Any]:
    config_dir = Path(os.environ.get("CONFIG_DIR", "config"))
    path = config_dir / name
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data or {}


def _resolve_env_file() -> str | None:
    candidates = [
        Path(os.environ.get("ALGOFLAT_ENV_FILE", "")),
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[3] / ".env",  # repo root from src/algomcx/config.py
        Path(__file__).resolve().parents[2] / ".env",
    ]
    for path in candidates:
        if str(path) and path.is_file():
            return str(path)
    return None


_env_file = _resolve_env_file()


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_file,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    trading_mode: str = Field(default="paper", alias="TRADING_MODE")
    database_url: str = Field(
        default="postgresql://algomcx:algomcx@localhost:5432/algomcx",
        alias="DATABASE_URL",
    )
    config_dir: str = Field(default="./config", alias="CONFIG_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")
    telegram_alerts_enabled: str | None = Field(
        default=None, alias="TELEGRAM_ALERTS_ENABLED"
    )

    flattrade_user_id: str | None = Field(default=None, alias="FLATTRADE_USER_ID")
    flattrade_api_key: str | None = Field(default=None, alias="FLATTRADE_API_KEY")
    flattrade_api_secret: str | None = Field(default=None, alias="FLATTRADE_API_SECRET")
    flattrade_password: str | None = Field(default=None, alias="FLATTRADE_PASSWORD")
    flattrade_totp_secret: str | None = Field(default=None, alias="FLATTRADE_TOTP_SECRET")
    flattrade_access_token: str | None = Field(default=None, alias="FLATTRADE_ACCESS_TOKEN")
    flattrade_redirect_url: str = Field(
        default="http://127.0.0.1:8000/callback",
        alias="FLATTRADE_REDIRECT_URL",
    )
    flattrade_token_file: str = Field(
        default=".flattrade/session.json",
        alias="FLATTRADE_TOKEN_FILE",
    )


@lru_cache
def get_env() -> EnvSettings:
    return EnvSettings()


class AppConfig:
    def __init__(self) -> None:
        self.env = get_env()
        self.broker = _load_yaml("broker_config.yaml")
        self.symbols = _load_yaml("symbols_config.yaml")
        self.strategy = _load_yaml("strategy_config.yaml")
        self.validator = _load_yaml("validator_config.yaml")
        self.risk = _load_yaml("risk_config.yaml")
        self.execution = _load_yaml("execution_config.yaml")
        self.position_exit = _load_yaml("position_exit_config.yaml")
        self.paper_trading = _load_yaml("paper_trading_config.yaml")
        self.market_session = _load_yaml("market_session_config.yaml")
        self.runtime = _load_yaml("runtime_config.yaml")
        self.logging = _load_yaml("logging_config.yaml")
        self.ml = _load_yaml("ml_config.yaml")
        self.data_availability = _load_yaml("data_availability_config.yaml")

    @property
    def is_paper(self) -> bool:
        return self.env.trading_mode.lower() == "paper"

    @property
    def is_live(self) -> bool:
        return self.env.trading_mode.lower() == "live"


@lru_cache
def get_config() -> AppConfig:
    return AppConfig()
