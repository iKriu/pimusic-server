"""Адрес Jellyfin в сообщениях бота: домен за reverse-proxy, а не локальный IP."""
from app.config import Config


def test_jellyfin_public_prefers_explicit(monkeypatch):
    monkeypatch.setenv("JELLYFIN_PUBLIC_URL", "https://jf.example.com/")
    monkeypatch.setenv("STREAM_PUBLIC_URL", "https://music.example.com")
    assert Config().jellyfin_public_base == "https://jf.example.com"


def test_jellyfin_public_uses_https_domain(monkeypatch):
    monkeypatch.delenv("JELLYFIN_PUBLIC_URL", raising=False)
    monkeypatch.setenv("STREAM_PUBLIC_URL", "https://music.example.com/")
    monkeypatch.setenv("PUBLIC_HOST", "192.168.1.20")
    assert Config().jellyfin_public_base == "https://music.example.com"


def test_jellyfin_public_falls_back_to_lan(monkeypatch):
    monkeypatch.delenv("JELLYFIN_PUBLIC_URL", raising=False)
    monkeypatch.delenv("STREAM_PUBLIC_URL", raising=False)
    monkeypatch.setenv("PUBLIC_HOST", "192.168.1.20")
    assert Config().jellyfin_public_base == "http://192.168.1.20:8096"
