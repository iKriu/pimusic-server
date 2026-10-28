"""Конфигурация бота: всё берётся из переменных окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    # пустая строка в .env (KEY=) считается «не задано»
    return os.environ.get(name, "").strip() or default


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    v = _env(name, "1" if default else "0").lower()
    return v in ("1", "true", "yes", "on", "y")


@dataclass(frozen=True)
class Config:
    # Telegram
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    invite_code: str = field(default_factory=lambda: _env("INVITE_CODE"))
    admin_telegram_ids: tuple[int, ...] = field(
        default_factory=lambda: tuple(
            int(x) for x in _env("ADMIN_TELEGRAM_IDS").replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()
        )
    )

    # Jellyfin
    jellyfin_url: str = field(default_factory=lambda: _env("JELLYFIN_INTERNAL_URL", "http://jellyfin:8096").rstrip("/"))
    jellyfin_admin_user: str = field(default_factory=lambda: _env("JELLYFIN_ADMIN_USER", "admin"))
    jellyfin_admin_password: str = field(default_factory=lambda: _env("JELLYFIN_ADMIN_PASSWORD"))
    server_name: str = field(default_factory=lambda: _env("SERVER_NAME", "PiMusic"))

    # slskd / Soulseek
    slskd_url: str = field(default_factory=lambda: _env("SLSKD_INTERNAL_URL", "http://slskd:5030").rstrip("/"))
    slskd_api_key: str = field(default_factory=lambda: _env("SLSKD_API_KEY"))
    slsk_username: str = field(default_factory=lambda: _env("SLSK_USERNAME"))
    slsk_password: str = field(default_factory=lambda: _env("SLSK_PASSWORD"))
    sldl_username: str = field(default_factory=lambda: _env("SLDL_SLSK_USERNAME"))
    sldl_password: str = field(default_factory=lambda: _env("SLDL_SLSK_PASSWORD"))
    sldl_listen_port: int = field(default_factory=lambda: _int("SLDL_LISTEN_PORT", 50301))

    # Spotify (необязательно — для spotDL / sldl)
    spotify_client_id: str = field(default_factory=lambda: _env("SPOTIFY_CLIENT_ID"))
    spotify_client_secret: str = field(default_factory=lambda: _env("SPOTIFY_CLIENT_SECRET"))

    # Last.fm
    lastfm_api_key: str = field(default_factory=lambda: _env("LASTFM_API_KEY"))

    # Пути внутри контейнера
    media_root: Path = field(default_factory=lambda: Path(_env("MEDIA_ROOT", "/media")))
    state_dir: Path = field(default_factory=lambda: Path(_env("STATE_DIR", "/state")))

    # Радио
    public_host: str = field(default_factory=lambda: _env("PUBLIC_HOST", "127.0.0.1"))
    stream_port: int = field(default_factory=lambda: _int("STREAM_PORT", 8097))
    stream_bitrate: str = field(default_factory=lambda: _env("STREAM_BITRATE", "320k"))
    # Показывать личные «Волны» как каналы Live TV (их увидят ВСЕ пользователи с доступом к Live TV)
    wave_in_livetv: bool = field(default_factory=lambda: _bool("WAVE_IN_LIVETV", False))
    wave_playlist_size: int = field(default_factory=lambda: _int("WAVE_PLAYLIST_SIZE", 100))

    # Поведение
    download_workers: int = field(default_factory=lambda: _int("DOWNLOAD_WORKERS", 2))
    # Отдельный пул для импорта медиатеки из файла: обычные запросы не ждут импорт.
    # 4 — безопасно и для слабой машины, и для лимита поисков Soulseek (~9 поисков в минуту на аккаунт).
    import_workers: int = field(default_factory=lambda: _int("IMPORT_WORKERS", 4))
    max_playlist_tracks: int = field(default_factory=lambda: _int("MAX_PLAYLIST_TRACKS", 300))
    wave_size: int = field(default_factory=lambda: _int("WAVE_SIZE", 10))
    soulseek_search_timeout: int = field(default_factory=lambda: _int("SOULSEEK_SEARCH_TIMEOUT", 25))
    soulseek_download_timeout: int = field(default_factory=lambda: _int("SOULSEEK_DOWNLOAD_TIMEOUT", 600))
    allow_flac_transcode: bool = field(default_factory=lambda: _bool("ALLOW_FLAC_TRANSCODE", True))
    contact_email: str = field(default_factory=lambda: _env("CONTACT_EMAIL", "pimusic@example.com"))

    # Вход вне Telegram (PWA, приложения Windows/Android)
    # Telegram Login Widget: включать только после /setdomain у @BotFather (домен из STREAM_PUBLIC_URL)
    login_widget: bool = field(default_factory=lambda: _bool("LOGIN_WIDGET", False))
    # Доп. Origin для CORS через запятую (к встроенным tauri://localhost, capacitor://localhost …)
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(o.strip().rstrip("/") for o in _env("CORS_ORIGINS").split(",") if o.strip()))
    # Загрузка своих файлов (бот, Mini App)
    acoustid_api_key: str = field(default_factory=lambda: _env("ACOUSTID_API_KEY"))  # опознание по отпечатку
    upload_max_mb: int = field(default_factory=lambda: _int("UPLOAD_MAX_MB", 300))
    upload_min_free_gb: int = field(default_factory=lambda: _int("UPLOAD_MIN_FREE_GB", 5))
    upload_workers: int = field(default_factory=lambda: _int("UPLOAD_WORKERS", 1))
    # Reverse-proxy, которым верим в X-Forwarded-For (CIDR через запятую); пусто — только loopback
    trusted_proxies: tuple[str, ...] = field(
        default_factory=lambda: tuple(o.strip() for o in _env("TRUSTED_PROXIES").split(",") if o.strip()))
    upload_user_quota_gb: int = field(default_factory=lambda: _int("UPLOAD_USER_QUOTA_GB", 10))
    # Фоновый расчёт громкости (ReplayGain, EBU R128) для нормализации в плеере
    loudness_analysis: bool = field(default_factory=lambda: _bool("LOUDNESS_ANALYSIS", True))
    # Файлы автообновления приложений (latest.json Tauri, android.json, .exe/.apk/.sig); пусто — <MEDIA_ROOT>/updates
    updates_dir_env: str = field(default_factory=lambda: _env("UPDATES_DIR"))

    # ---- производные пути ----
    @property
    def library_dir(self) -> Path:  # общий кэш треков (один физический экземпляр файла)
        return self.media_root / "library"

    @property
    def share_dir(self) -> Path:  # раздача Soulseek: жёсткие ссылки на треки кэша, кроме личных загрузок
        return self.media_root / "share"

    @property
    def users_dir(self) -> Path:  # личные медиатеки (жёсткие ссылки на library/)
        return self.media_root / "users"

    @property
    def radio_dir(self) -> Path:  # общие интернет-радиостанции (.strm)
        return self.media_root / "radio"

    @property
    def incoming_dir(self) -> Path:  # временные загрузки
        return self.media_root / "incoming"

    @property
    def slskd_downloads_dir(self) -> Path:
        return self.incoming_dir / "slskd"

    @property
    def updates_dir(self) -> Path:  # обновления приложений (в контейнере — только чтение)
        return Path(self.updates_dir_env) if self.updates_dir_env else self.media_root / "updates"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "pimusic.db"

    @property
    def jellyfin_api_key_file(self) -> Path:
        return self.state_dir / "jellyfin_api_key"

    @property
    def stream_public_base(self) -> str:
        # За reverse-proxy (Nginx Proxy Manager, Caddy): STREAM_PUBLIC_URL=https://music.example.com
        url = _env("STREAM_PUBLIC_URL").rstrip("/")
        return url or f"http://{self.public_host}:{self.stream_port}"

    @property
    def jellyfin_public_base(self) -> str:
        """Адрес Jellyfin для людей и приложений: JELLYFIN_PUBLIC_URL, иначе домен из STREAM_PUBLIC_URL (https —
        reverse-proxy отдаёт на нём и Jellyfin, и Mini App), иначе локальный http://PUBLIC_HOST:8096."""
        url = _env("JELLYFIN_PUBLIC_URL").rstrip("/")
        if url:
            return url
        base = self.stream_public_base
        return base if base.startswith("https://") else f"http://{self.public_host}:8096"

    @property
    def user_agent(self) -> str:
        return f"PiMusic/1.0 ( {self.contact_email} )"

    # ключ Jellyfin из окружения — запоминается при старте (окружение потом очищается, см. scrub_secret_env)
    jellyfin_api_key_env: str = field(default_factory=lambda: _env("JELLYFIN_API_KEY"), repr=False)

    def jellyfin_api_key(self) -> str:
        if self.jellyfin_api_key_env:
            return self.jellyfin_api_key_env
        try:
            return self.jellyfin_api_key_file.read_text().strip()
        except FileNotFoundError:
            return ""


CFG = Config()

# переменные с секретами: после старта убираются из os.environ (их не увидят ни дочерние процессы, ни случайный
# дамп окружения); значения уже в CFG
SECRET_ENV = ("TELEGRAM_BOT_TOKEN", "INVITE_CODE", "JELLYFIN_ADMIN_PASSWORD", "JELLYFIN_API_KEY", "SLSKD_API_KEY",
              "SLSK_PASSWORD", "SLDL_SLSK_PASSWORD", "SPOTIFY_CLIENT_SECRET", "LASTFM_API_KEY", "ACOUSTID_API_KEY",
              "SLSKD_WEB_PASSWORD")


def scrub_secret_env() -> list[str]:
    """Вызывать один раз при старте, после того как CFG прочитан. Возвращает, что убрано."""
    removed = [k for k in SECRET_ENV if k in os.environ]
    for k in removed:
        os.environ.pop(k, None)
    return removed
