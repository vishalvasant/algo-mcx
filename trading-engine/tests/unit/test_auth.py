from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from algomcx.broker.auth import (
    generate_api_secret_hash,
    next_token_expiry,
    parse_redirect_url,
)


def test_generate_api_secret_hash():
    # Deterministic per Flattrade docs: sha256(api_key + request_code + api_secret)
    result = generate_api_secret_hash("key", "code", "secret")
    assert len(result) == 64
    assert result == generate_api_secret_hash("key", "code", "secret")


def test_next_token_expiry_before_5am():
    ist = ZoneInfo("Asia/Kolkata")
    now = datetime(2026, 7, 13, 4, 0, tzinfo=ist)
    expiry = next_token_expiry(now)
    assert expiry.day == 13
    assert expiry.hour == 5


def test_next_token_expiry_after_5am():
    ist = ZoneInfo("Asia/Kolkata")
    now = datetime(2026, 7, 13, 10, 0, tzinfo=ist)
    expiry = next_token_expiry(now)
    assert expiry.day == 14
    assert expiry.hour == 5


def test_parse_redirect_url():
    host, port, path = parse_redirect_url("http://127.0.0.1:8000/callback")
    assert host == "127.0.0.1"
    assert port == 8000
    assert path == "/callback"
