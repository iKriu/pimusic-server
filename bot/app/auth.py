"""Вход вне Telegram (PWA, Windows, Android): токены устройств, вход по ссылке в бота, Telegram Login Widget.

Токен устройства — secrets.token_urlsafe(32); в базе хранится только SHA-256 (как и nonce входа),
поэтому утечка базы не даёт доступа. Сравнение подписей — hmac.compare_digest.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import ipaddress
import re
import secrets
import time
from typing import Any

NONCE_TTL = 300               # ссылка входа живёт 5 минут
MAX_PENDING_NONCES = 1000     # незавершённых входов на весь сервер
MAX_DEVICES = 20              # устройств на пользователя (сверх — отказ: сначала выйти на старом)
DEVICE_IDLE_TTL = 90 * 86400  # токен устройства, не появлявшегося 90 дней, перестаёт действовать
WIDGET_MAX_AGE = 300          # auth_date виджета — не старше 5 минут (и один раз: ReplayGuard)
CODE_DIGITS = 4               # проверочный код входа: виден на экране устройства, выбирается в боте
PLATFORMS = ("web", "pwa", "windows", "android", "ios", "linux", "macos")
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
LAST_SEEN_STEP = 60           # last_seen устройства пишем не чаще раза в минуту

# Origin нативных оболочек (Tauri, Capacitor) и локальной разработки
DEFAULT_CORS_ORIGINS = ("tauri://localhost", "http://tauri.localhost", "https://tauri.localhost",
                        "capacitor://localhost", "http://localhost", "https://localhost")


PLATFORM_LABELS = {"web": "Браузер", "pwa": "Приложение (PWA)", "windows": "Windows", "android": "Android",
                   "ios": "iPhone / iPad", "linux": "Linux", "macos": "macOS"}


def platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform, platform or "Устройство")


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def new_token() -> tuple[str, str]:
    """(токен для клиента, его хэш для базы)."""
    t = secrets.token_urlsafe(32)
    return t, sha256(t)


def new_code() -> str:
    return f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"


def code_choices(code: str, n: int = 3) -> list[str]:
    """Верный код и n−1 приманок вперемешку (для кнопок в боте)."""
    out = {code}
    while len(out) < n:
        out.add(new_code())
    items = list(out)
    secrets.SystemRandom().shuffle(items)
    return items


def new_nonce() -> tuple[str, str]:
    n = secrets.token_urlsafe(24)  # 32 символа: «login_» + nonce укладывается в 64 символа deep link
    return n, sha256(n)


# управляющие, а также невидимые и меняющие направление текста (подмена имени устройства в сообщении бота)
_BAD_CHARS = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]+")


def clean_device(name: Any, platform: Any) -> tuple[str, str]:
    """Название устройства (до 64 символов, без управляющих/невидимых/bidi) и платформа из белого списка."""
    n = re.sub(r"\s+", " ", _BAD_CHARS.sub(" ", name if isinstance(name, str) else "")).strip()[:64]
    p = platform if isinstance(platform, str) and platform in PLATFORMS else "web"
    return n, p


def check_widget(data: dict, bot_token: str, now: float | None = None,
                 max_age: int = WIDGET_MAX_AGE) -> dict | None:
    """Проверка данных Telegram Login Widget (https://core.telegram.org/widgets/login#checking-authorization):
    secret = SHA256(bot_token), подпись HMAC-SHA256 строки «ключ=значение» по алфавиту, кроме hash."""
    if not bot_token or not isinstance(data, dict):
        return None
    received = data.get("hash")
    if not isinstance(received, str) or not received:
        return None
    fields = {k: v for k, v in data.items() if k != "hash" and v is not None}
    if not all(isinstance(v, (str, int)) and not isinstance(v, bool) for v in fields.values()):
        return None
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(bot_token.encode()).digest()
    calc = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc.encode(), received.lower().encode("utf-8", "replace")):
        return None
    try:
        auth_date, uid = int(fields.get("auth_date", 0)), int(fields.get("id", 0))
    except (TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    if uid <= 0 or auth_date <= 0 or now - auth_date > max_age or auth_date - now > 300:
        return None
    return fields


# По умолчанию — только loopback: адрес reverse-proxy (NPM) задаётся в TRUSTED_PROXIES явно. Доверять всей
# локальной сети нельзя: любой контейнер/устройство в LAN подделал бы X-Forwarded-For и обошёл лимиты входа.
DEFAULT_TRUSTED_PROXIES = ("127.0.0.0/8", "::1/128")
PRIVATE_NETWORKS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
                    "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10")


def parse_networks(items: Any) -> tuple:
    """Список CIDR (TRUSTED_PROXIES); мусор пропускается, пустой список — только loopback."""
    nets = []
    for it in items or ():
        try:
            nets.append(ipaddress.ip_network(str(it).strip(), strict=False))
        except ValueError:
            continue
    return tuple(nets) or tuple(ipaddress.ip_network(n) for n in DEFAULT_TRUSTED_PROXIES)


_DEFAULT_NETS = parse_networks(())


def _addr(ip: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Адрес из строки; «::ffff:192.168.1.2» (dual-stack сокет) — как IPv4, иначе прокси не узнать."""
    try:
        a = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    mapped = getattr(a, "ipv4_mapped", None)
    return mapped or a


def _in(ip: str, nets: tuple) -> bool:
    a = _addr(ip)
    return a is not None and any(a in n for n in nets if a.version == n.version)


def client_ip(remote: str | None, forwarded_for: str | None, trusted: tuple | None = None) -> str:
    """IP клиента. X-Forwarded-For учитывается, только если соединение пришло от доверенного прокси
    (TRUSTED_PROXIES, по умолчанию только loopback): берём самый правый адрес цепочки, который не прокси
    (левые части клиент может подделать). Вся цепочка из прокси или мусор в ней — адрес соединения."""
    nets = trusted or _DEFAULT_NETS
    remote = (remote or "").strip()
    if not forwarded_for or not _in(remote, nets):
        return remote or "?"
    for ip in reversed([x.strip() for x in forwarded_for.split(",") if x.strip()]):
        if _in(ip, nets):
            continue
        a = _addr(ip)
        return str(a) if a is not None else remote
    return remote


def rate_key(ip: str) -> str:
    """Ключ лимита: IPv4 — адрес, IPv6 — сеть /64 (у одного клиента их миллиарды)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if a.version == 6:
        if a.ipv4_mapped:
            return str(a.ipv4_mapped)
        return str(ipaddress.ip_network(f"{a}/64", strict=False))
    return str(a)


class RateLimiter:
    """Скользящее окно: не больше limit событий за window секунд на ключ. Старые ключи чистятся раз в окно."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._hits: dict[str, collections.deque] = {}
        self._pruned = 0.0

    def _prune(self, now: float) -> None:
        if now - self._pruned < self.window:
            return
        self._pruned = now
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window]:
            del self._hits[k]

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self._prune(now)
        q = self._hits.setdefault(key, collections.deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


class ReplayGuard:
    """Однократность подписанных данных (виджет Telegram) в пределах их срока жизни."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._seen: dict[str, float] = {}

    def first(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if len(self._seen) > 1000:
            self._seen = {k: t for k, t in self._seen.items() if now - t < self.ttl}
        t = self._seen.get(key)
        if t is not None and now - t < self.ttl:
            return False
        self._seen[key] = now
        return True
