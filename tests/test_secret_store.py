import logging
import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

import secret_store

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_store(monkeypatch):
    monkeypatch.setattr(secret_store, "_secrets", {})
    for key in secret_store.SECRET_KEYS + ("LOG_LEVEL",):
        monkeypatch.delenv(key, raising=False)
    yield


def write_env(tmp_path, text):
    path = tmp_path / "bot.env"
    path.write_text(text, encoding="utf-8")
    return path


def test_secrets_stay_in_memory_and_out_of_the_environment(tmp_path, monkeypatch):
    path = write_env(tmp_path, "DISCORD_TOKEN=tok-123456\nSPOTIFY_CLIENT_SECRET=sec-123456\nLOG_LEVEL=DEBUG\n")
    monkeypatch.setenv("BOT_ENV_FILE", str(path))
    secret_store.load()
    assert secret_store.get("DISCORD_TOKEN") == "tok-123456"
    assert secret_store.get("SPOTIFY_CLIENT_SECRET") == "sec-123456"
    assert "DISCORD_TOKEN" not in os.environ and "SPOTIFY_CLIENT_SECRET" not in os.environ
    assert os.environ["LOG_LEVEL"] == "DEBUG"  # lo que no es secreto sí va al entorno


def test_secret_passed_as_env_var_is_removed_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_ENV_FILE", str(write_env(tmp_path, "")))
    monkeypatch.setenv("DISCORD_TOKEN", "from-env-123")
    secret_store.load()
    assert secret_store.get("DISCORD_TOKEN") == "from-env-123"
    assert "DISCORD_TOKEN" not in os.environ


def test_empty_secret_counts_as_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_ENV_FILE", str(write_env(tmp_path, "SPOTIFY_CLIENT_ID=\n")))
    secret_store.load()
    assert secret_store.get("SPOTIFY_CLIENT_ID") is None


def test_secrets_are_redacted_from_logs(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("BOT_ENV_FILE", str(write_env(tmp_path, "DISCORD_TOKEN=super-secret-token\n")))
    secret_store.load()
    logger = logging.getLogger("test-redact")
    logger.addFilter(secret_store.RedactSecrets())
    with caplog.at_level(logging.INFO, logger="test-redact"):
        logger.info("conectando con %s", "super-secret-token")
        try:
            raise RuntimeError("falló con super-secret-token")
        except RuntimeError:
            logger.exception("error")
    text = caplog.text + "".join(r.exc_text or "" for r in caplog.records)
    assert "super-secret-token" not in text and "***" in text


def test_env_example_has_no_real_secrets():
    """El ejemplo se sube a GitHub: sus secretos tienen que estar vacíos."""
    values = dotenv_values(ROOT / ".env.example")
    filled = [k for k in secret_store.SECRET_KEYS if values.get(k)]
    assert filled == [], f"Estos valores no pueden ir en .env.example (se publican): {filled}"
