"""SQLite-хранилище: пользователи, общий кэш треков, радиостанции."""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .util import safe_name

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS users (
    login        TEXT PRIMARY KEY,
    tg_id        INTEGER UNIQUE,
    jf_user_id   TEXT NOT NULL,
    library_id   TEXT,
    stream_token TEXT NOT NULL,
    is_admin     INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tracks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    match_key  TEXT NOT NULL,           -- нормализованное "артист|название"
    mbid       TEXT,                    -- MusicBrainz recording id
    isrc       TEXT,
    artist     TEXT NOT NULL,
    title      TEXT NOT NULL,
    album      TEXT,
    duration   INTEGER,                 -- секунды
    path       TEXT NOT NULL UNIQUE,    -- путь в общем кэше /media/library/...
    source     TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tracks_key  ON tracks(match_key);
CREATE INDEX IF NOT EXISTS idx_tracks_mbid ON tracks(mbid);
CREATE INDEX IF NOT EXISTS idx_tracks_isrc ON tracks(isrc);

-- псевдонимы запросов: что пользователь искал -> какой трек получил
CREATE TABLE IF NOT EXISTS aliases (
    alias_key TEXT PRIMARY KEY,
    track_id  INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_aliases_track ON aliases(track_id);

CREATE TABLE IF NOT EXISTS user_tracks (
    login     TEXT NOT NULL REFERENCES users(login) ON DELETE CASCADE,
    track_id  INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    link_path TEXT NOT NULL,
    added_at  INTEGER NOT NULL,
    PRIMARY KEY (login, track_id)
);
CREATE INDEX IF NOT EXISTS idx_user_tracks_track ON user_tracks(track_id);

CREATE TABLE IF NOT EXISTS radio (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    owner  TEXT,                         -- NULL = общая станция
    name   TEXT NOT NULL,
    url    TEXT NOT NULL,
    path   TEXT NOT NULL UNIQUE
);

-- импорт медиатеки из файла (Tune My Music CSV/TXT): очередь переживает перезапуск бота
CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    login       TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    filename    TEXT NOT NULL,
    total       INTEGER NOT NULL,
    status_msg  INTEGER,
    state       TEXT NOT NULL DEFAULT 'running',   -- running / done / cancelled
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS import_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id   INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    artist      TEXT NOT NULL,
    title       TEXT NOT NULL,
    album       TEXT,
    isrc        TEXT,
    duration    REAL,
    spotify_url TEXT,
    state       TEXT NOT NULL DEFAULT 'pending',   -- pending / active / done / cached / failed
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_import_items ON import_items(import_id, state, idx);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- что уже предлагала "Моя волна", чтобы не качать повторно отвергнутое/неудачное
CREATE TABLE IF NOT EXISTS wave_seen (
    login     TEXT NOT NULL,
    match_key TEXT NOT NULL,
    ok        INTEGER NOT NULL,
    seen_at   INTEGER NOT NULL,
    PRIMARY KEY (login, match_key)
);

-- плейлисты бота (личные и общие); владелец в playlist_members не пишется
CREATE TABLE IF NOT EXISTS playlists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner       TEXT NOT NULL REFERENCES users(login) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    share_token TEXT UNIQUE,            -- NULL = личный; иначе токен приглашения
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_playlists_owner ON playlists(owner);
CREATE TABLE IF NOT EXISTS playlist_members (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    login       TEXT NOT NULL REFERENCES users(login) ON DELETE CASCADE,
    joined_at   INTEGER NOT NULL,
    PRIMARY KEY (playlist_id, login)
);
CREATE INDEX IF NOT EXISTS idx_playlist_members_login ON playlist_members(login);
CREATE TABLE IF NOT EXISTS playlist_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id    INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    added_by    TEXT,
    position    REAL NOT NULL,
    added_at    INTEGER NOT NULL,
    UNIQUE (playlist_id, track_id)
);
CREATE INDEX IF NOT EXISTS idx_playlist_items_pos ON playlist_items(playlist_id, position);
CREATE INDEX IF NOT EXISTS idx_playlist_items_track ON playlist_items(track_id);

-- настройки плеера пользователя по ключам (eq.presets, eq.shared, eq.device.<id>, player): JSON как есть;
-- по ключам — чтобы устройства не затирали друг друга. updated_at — миллисекунды, строго растёт по ключу
CREATE TABLE IF NOT EXISTS user_settings (
    login      TEXT NOT NULL REFERENCES users(login) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (login, key)
);

-- устройства вне Telegram (PWA, Windows, Android): в базе только SHA-256 токена
CREATE TABLE IF NOT EXISTS devices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    login      TEXT NOT NULL REFERENCES users(login) ON DELETE CASCADE,
    name       TEXT NOT NULL DEFAULT '',
    platform   TEXT NOT NULL DEFAULT 'web',
    token_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_devices_login ON devices(login);

-- загрузка своих файлов (бот, Mini App): очередь переживает перезапуск; path — имя, придуманное сервером
CREATE TABLE IF NOT EXISTS uploads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    login        TEXT NOT NULL REFERENCES users(login) ON DELETE CASCADE,
    orig_name    TEXT NOT NULL,
    path         TEXT NOT NULL,
    size         INTEGER NOT NULL DEFAULT 0,
    -- receiving / queued / analyzing / need_info / processing / done / duplicate / failed / cancelled
    state        TEXT NOT NULL DEFAULT 'queued',
    origin       TEXT NOT NULL DEFAULT 'app',     -- bot / app
    chat_id      INTEGER,                          -- куда писать о результате
    batch        TEXT NOT NULL DEFAULT '',         -- пачка файлов: одно итоговое сообщение
    manual       INTEGER NOT NULL DEFAULT 0,       -- пользователь указал/подтвердил данные — не опознаём
    guess_artist TEXT, guess_title TEXT, guess_album TEXT,
    duration     REAL,
    track_id     INTEGER,
    prompt_msg   INTEGER,                          -- сообщение бота «Пришлите Исполнитель — Название»
    note         TEXT,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_state ON uploads(state, id);
CREATE INDEX IF NOT EXISTS idx_uploads_login ON uploads(login, id);

-- вход по ссылке t.me/<бот>?start=login_<nonce>: в базе только SHA-256 nonce
CREATE TABLE IF NOT EXISTS auth_nonces (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    nonce_hash TEXT NOT NULL UNIQUE,
    device     TEXT NOT NULL DEFAULT '',
    platform   TEXT NOT NULL DEFAULT 'web',
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending / approved / denied / used
    login      TEXT,
    client_ip  TEXT NOT NULL DEFAULT '',          -- откуда начат вход (показывается в боте)
    code       TEXT NOT NULL DEFAULT '',          -- проверочный код на экране устройства
    opener_tg  INTEGER,                           -- кто первым открыл ссылку в боте
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
"""

# доступ к треку: он в медиатеке пользователя или в плейлисте, где пользователь владелец/участник
_PL_ACCESS = """EXISTS (SELECT 1 FROM playlist_items pi JOIN playlists p ON p.id=pi.playlist_id
                 WHERE pi.track_id=t.id AND (p.owner=? OR EXISTS (
                     SELECT 1 FROM playlist_members m WHERE m.playlist_id=p.id AND m.login=?)))"""


PL_MAX_PER_USER = 200
PL_MAX_TRACKS = 5000
PL_MAX_MEMBERS = 20


class DeviceLimit(Exception):
    """У пользователя уже максимум устройств."""


class PlaylistLimit(Exception):
    """Достигнут лимит: args[0] — 'playlists' или 'members'."""


@dataclass
class User:
    login: str
    tg_id: int | None
    jf_user_id: str
    library_id: str | None
    stream_token: str
    is_admin: bool


@dataclass
class Track:
    id: int
    match_key: str
    mbid: str | None
    isrc: str | None
    artist: str
    title: str
    album: str | None
    duration: int | None
    path: str
    source: str | None
    gain_db: float | None = None    # ReplayGain трека, дБ (цель −14 LUFS); None — ещё не посчитано
    peak: float | None = None       # пик (true peak), линейный
    genre: str | None = None        # основной жанр (нормализованный)
    genres: str | None = None       # все жанры, JSON-список
    year: int | None = None
    broken: bool = False            # пользователь пометил файл проблемным («Перекачать») — из кэша не выдаём


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Новые колонки в старых базах (CREATE TABLE IF NOT EXISTS их не добавит)."""
        def cols(table: str) -> set[str]:
            return {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
        if "track_id" not in cols("import_items"):  # какой трек получился (для плейлиста импорта)
            self._conn.execute("ALTER TABLE import_items ADD COLUMN track_id INTEGER")
        if "playlist_id" not in cols("imports"):  # плейлист бота, который наполняет импорт
            self._conn.execute("ALTER TABLE imports ADD COLUMN playlist_id INTEGER")
        ncols = cols("auth_nonces")
        for col in ("client_ip TEXT NOT NULL DEFAULT ''", "code TEXT NOT NULL DEFAULT ''", "opener_tg INTEGER"):
            if col.split()[0] not in ncols:
                self._conn.execute(f"ALTER TABLE auth_nonces ADD COLUMN {col}")
        tcols = cols("tracks")
        for col in ("gain_db REAL", "peak REAL", "loudness_at INTEGER",  # громкость: анализ в фоне
                    "genre TEXT", "genres TEXT", "year INTEGER",  # жанр (основной), все жанры JSON, год
                    "broken_at INTEGER"):  # файл помечен проблемным: при повторном добавлении — качать заново
            if col.split()[0] not in tcols:
                self._conn.execute(f"ALTER TABLE tracks ADD COLUMN {col}")
        if "source" not in cols("imports"):  # откуда импорт: tg (бот / Mini App в Telegram) или app (приложение)
            self._conn.execute("ALTER TABLE imports ADD COLUMN source TEXT NOT NULL DEFAULT 'tg'")

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Транзакция BEGIN IMMEDIATE под общим замком; вложенный вызов — часть внешней транзакции."""
        with self._lock:
            if self._conn.in_transaction:
                yield self._conn
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _q(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(args)))

    def _x(self, sql: str, args: Iterable = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(args))
            return cur.lastrowid or cur.rowcount

    # ---------- users ----------
    @staticmethod
    def _user(r: sqlite3.Row | None) -> User | None:
        if r is None:
            return None
        return User(r["login"], r["tg_id"], r["jf_user_id"], r["library_id"], r["stream_token"], bool(r["is_admin"]))

    def user_by_tg(self, tg_id: int) -> User | None:
        rows = self._q("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        return self._user(rows[0] if rows else None)

    def user_by_login(self, login: str) -> User | None:
        rows = self._q("SELECT * FROM users WHERE lower(login)=lower(?)", (login,))
        return self._user(rows[0] if rows else None)

    def user_by_token(self, login: str, token: str) -> User | None:
        rows = self._q("SELECT * FROM users WHERE lower(login)=lower(?) AND stream_token=?", (login, token))
        return self._user(rows[0] if rows else None)

    def all_users(self) -> list[User]:
        return [self._user(r) for r in self._q("SELECT * FROM users ORDER BY login")]  # type: ignore[misc]

    def upsert_user(self, u: User) -> None:
        with self._lock:
            # Telegram-аккаунт может быть привязан только к одному логину
            if u.tg_id is not None:
                self._conn.execute("UPDATE users SET tg_id=NULL WHERE tg_id=? AND login<>?", (u.tg_id, u.login))
            self._conn.execute(
                """INSERT INTO users(login,tg_id,jf_user_id,library_id,stream_token,is_admin,created_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(login) DO UPDATE SET tg_id=excluded.tg_id, jf_user_id=excluded.jf_user_id,
                     library_id=excluded.library_id, stream_token=excluded.stream_token, is_admin=excluded.is_admin""",
                (u.login, u.tg_id, u.jf_user_id, u.library_id, u.stream_token, int(u.is_admin), int(time.time())),
            )

    def delete_user(self, login: str) -> None:
        self._x("DELETE FROM users WHERE login=?", (login,))

    # ---------- tracks ----------
    @staticmethod
    def _track(r: sqlite3.Row | None) -> Track | None:
        if r is None:
            return None
        keys = r.keys()
        return Track(r["id"], r["match_key"], r["mbid"], r["isrc"], r["artist"], r["title"], r["album"],
                     r["duration"], r["path"], r["source"],
                     r["gain_db"] if "gain_db" in keys else None, r["peak"] if "peak" in keys else None,
                     r["genre"] if "genre" in keys else None, r["genres"] if "genres" in keys else None,
                     r["year"] if "year" in keys else None,
                     bool(r["broken_at"]) if "broken_at" in keys else False)

    def find_track(self, *, match_key: str | None = None, mbid: str | None = None, isrc: str | None = None,
                   alias_key: str | None = None) -> Track | None:
        if alias_key:
            rows = self._q("SELECT t.* FROM aliases a JOIN tracks t ON t.id=a.track_id WHERE a.alias_key=?", (alias_key,))
            if rows:
                return self._track(rows[0])
        if mbid:
            rows = self._q("SELECT * FROM tracks WHERE mbid=? LIMIT 1", (mbid,))
            if rows:
                return self._track(rows[0])
        if isrc:
            rows = self._q("SELECT * FROM tracks WHERE isrc=? LIMIT 1", (isrc,))
            if rows:
                return self._track(rows[0])
        if match_key:
            rows = self._q("SELECT * FROM tracks WHERE match_key=? LIMIT 1", (match_key,))
            if rows:
                return self._track(rows[0])
        return None

    def track_by_path(self, path: str) -> Track | None:
        rows = self._q("SELECT * FROM tracks WHERE path=?", (path,))
        return self._track(rows[0] if rows else None)

    def add_track(self, *, match_key: str, mbid: str | None, isrc: str | None, artist: str, title: str,
                  album: str | None, duration: int | None, path: str, source: str | None,
                  genres: list[str] | None = None, year: int | None = None) -> int:
        existing = self.track_by_path(path)
        if existing:
            return existing.id
        return self._x(
            """INSERT INTO tracks(match_key,mbid,isrc,artist,title,album,duration,path,source,created_at,
                                  genre,genres,year) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (match_key, mbid, isrc, artist, title, album, duration, path, source, int(time.time()),
             genres[0] if genres else None, json.dumps(genres, ensure_ascii=False) if genres else None, year),
        )

    def set_genre_year(self, track_id: int, genres: list[str] | None, year: int | None) -> None:
        """Жанры/год из тегов файла или каталога; пустое значение не затирает уже известное."""
        self._x("""UPDATE tracks SET genre=coalesce(?, genre), genres=coalesce(?, genres), year=coalesce(?, year)
                   WHERE id=?""",
                (genres[0] if genres else None, json.dumps(genres, ensure_ascii=False) if genres else None,
                 year, track_id))

    def tracks_after(self, last_id: int, limit: int, *, without_genre: bool = False) -> list[Track]:
        """Треки по id после last_id (для фоновых проходов по всему кэшу)."""
        extra = " AND genre IS NULL" if without_genre else ""
        rows = self._q(f"SELECT * FROM tracks WHERE id>?{extra} ORDER BY id LIMIT ?", (last_id, limit))
        return [self._track(r) for r in rows]  # type: ignore[misc]

    def add_alias(self, alias_key: str, track_id: int) -> None:
        if alias_key:
            self._x("INSERT OR REPLACE INTO aliases(alias_key,track_id) VALUES(?,?)", (alias_key, track_id))

    def link_track(self, login: str, track_id: int, link_path: str) -> None:
        self._x(
            "INSERT OR REPLACE INTO user_tracks(login,track_id,link_path,added_at) VALUES(?,?,?,?)",
            (login, track_id, link_path, int(time.time())),
        )

    def link_track_if_exists(self, login: str, track_id: int, link_path: str) -> tuple[bool, bool]:
        """Строка медиатеки — только если трек ещё есть (в одной транзакции с проверкой: очистка кэша его
        не удалит после этого). (записано, строка уже была)."""
        with self._tx() as c:
            if not c.execute("SELECT 1 FROM tracks WHERE id=?", (track_id,)).fetchone():
                return False, False
            existed = bool(c.execute("SELECT 1 FROM user_tracks WHERE login=? AND track_id=?",
                                     (login, track_id)).fetchone())
            c.execute("INSERT OR REPLACE INTO user_tracks(login,track_id,link_path,added_at) VALUES(?,?,?,?)",
                      (login, track_id, link_path, int(time.time())))
            return True, existed

    def user_has_track(self, login: str, track_id: int) -> str | None:
        rows = self._q("SELECT link_path FROM user_tracks WHERE login=? AND track_id=?", (login, track_id))
        return rows[0]["link_path"] if rows else None

    def user_track_keys(self, login: str) -> set[str]:
        rows = self._q("SELECT t.match_key FROM user_tracks u JOIN tracks t ON t.id=u.track_id WHERE u.login=?", (login,))
        return {r["match_key"] for r in rows}

    def user_track_by_path(self, login: str, path: str) -> tuple[Track, str] | None:
        """Трек медиатеки пользователя по пути файла (ссылка в /media/users/… или файл общего кэша)."""
        rows = self._q(
            """SELECT t.*, u.link_path FROM user_tracks u JOIN tracks t ON t.id=u.track_id
               WHERE u.login=? AND (u.link_path=? OR t.path=?) LIMIT 1""", (login, path, path))
        return (self._track(rows[0]), rows[0]["link_path"]) if rows else None  # type: ignore[return-value]

    def user_track_count(self, login: str) -> int:
        return self._q("SELECT count(*) c FROM user_tracks WHERE login=?", (login,))[0]["c"]

    def find_user_tracks(self, login: str, needle: str, limit: int = 10) -> list[tuple[Track, str]]:
        like = f"%{needle.lower()}%"
        rows = self._q(
            """SELECT t.*, u.link_path FROM user_tracks u JOIN tracks t ON t.id=u.track_id
               WHERE u.login=? AND (lower(t.artist) LIKE ? OR lower(t.title) LIKE ?)
               ORDER BY u.added_at DESC LIMIT ?""",
            (login, like, like, limit),
        )
        return [(self._track(r), r["link_path"]) for r in rows]  # type: ignore[misc]

    # ---------- группировка медиатеки (только чтение) ----------
    # Ссылка лежит в /media/users/<login>/<AlbumArtist>/<Album>/file.mp3 — исполнитель и альбом
    # медиатеки берутся из папок, как их видит Jellyfin.
    @staticmethod
    def library_folders(link_path: str) -> tuple[str, str]:
        p = Path(link_path)
        return p.parent.parent.name, p.parent.name

    def user_library_view(self, login: str) -> list[tuple[Track, str, int]]:
        """[(трек, ссылка, когда добавлен)] — для сортировок/фильтров медиатеки (их делает webapp)."""
        rows = self._q("""SELECT t.*, u.link_path, u.added_at AS link_added_at FROM user_tracks u
                          JOIN tracks t ON t.id=u.track_id WHERE u.login=? ORDER BY u.link_path""", (login,))
        return [(self._track(r), r["link_path"], r["link_added_at"]) for r in rows]  # type: ignore[misc]

    def user_library(self, login: str, *, recent: bool = False) -> list[tuple[Track, str]]:
        order = "u.added_at DESC, u.rowid DESC" if recent else "u.link_path"
        rows = self._q(
            f"""SELECT t.*, u.link_path FROM user_tracks u JOIN tracks t ON t.id=u.track_id
               WHERE u.login=? ORDER BY {order}""", (login,))
        return [(self._track(r), r["link_path"]) for r in rows]  # type: ignore[misc]

    def track_access(self, login: str, track_id: int) -> tuple[Track, str | None] | None:
        """(трек, ссылка пользователя или None), если трек в медиатеке или в доступном плейлисте."""
        hit = self.accessible_tracks(login, [track_id])
        return hit.get(track_id)

    def accessible_tracks(self, login: str, ids: Iterable[int]) -> dict[int, tuple[Track, str | None]]:
        """{id: (трек, ссылка или None)} — только доступные пользователю (медиатека или его плейлисты)."""
        ids = list(dict.fromkeys(int(i) for i in ids))
        out: dict[int, tuple[Track, str | None]] = {}
        for k in range(0, len(ids), 500):
            chunk = ids[k:k + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._q(
                f"""SELECT t.*, u.link_path FROM tracks t
                    LEFT JOIN user_tracks u ON u.track_id=t.id AND u.login=?
                    WHERE t.id IN ({marks}) AND (u.login IS NOT NULL OR {_PL_ACCESS})""",
                (login, *chunk, login, login))
            for r in rows:
                out[r["id"]] = (self._track(r), r["link_path"])  # type: ignore[assignment]
        return out

    @staticmethod
    def display_name(folder: str, names: list[str]) -> str:
        """Имя для показа: тег, из которого получилось имя папки, иначе самое частое значение тега."""
        names = [n for n in names if n]
        for n in names:
            if safe_name(n) == folder:
                return n
        return Counter(names).most_common(1)[0][0] if names and folder in ("", "_") else folder

    def user_artists(self, login: str) -> list[tuple[str, str, int]]:
        """[(папка исполнителя, имя, число треков)] по алфавиту."""
        groups: dict[str, list[Track]] = {}
        for t, link in self.user_library(login):
            groups.setdefault(self.library_folders(link)[0], []).append(t)
        out = [(k, self.display_name(k, [t.artist for t in ts]), len(ts)) for k, ts in groups.items()]
        return sorted(out, key=lambda x: x[1].casefold())

    def user_albums(self, login: str) -> list[tuple[str, str, str, str, int, list[tuple[Track, str]]]]:
        """[(папка исполнителя, папка альбома, альбом, исполнитель, число треков, треки)] по исполнителю/альбому."""
        groups: dict[tuple[str, str], list[tuple[Track, str]]] = {}
        for t, link in self.user_library(login):
            groups.setdefault(self.library_folders(link), []).append((t, link))
        out = []
        for (a, b), rows in groups.items():
            out.append((a, b, self.display_name(b, [t.album or "" for t, _ in rows]),
                        self.display_name(a, [t.artist for t, _ in rows]), len(rows), rows))
        return sorted(out, key=lambda x: (x[3].casefold(), x[2].casefold()))

    def user_tracks_by_artist(self, login: str, artist_folder: str) -> list[tuple[Track, str]]:
        """Все треки пользователя в папке исполнителя (по альбомам, внутри альбома — по имени файла)."""
        return [(t, link) for t, link in self.user_library(login) if self.library_folders(link)[0] == artist_folder]

    def set_track_source(self, track_id: int, source: str) -> None:
        """Источник файла сменился (свой файл пользователя заменён скачанным): громкость — пересчитать."""
        self._x("UPDATE tracks SET source=?, gain_db=NULL, peak=NULL, loudness_at=NULL WHERE id=?", (source, track_id))

    def set_track_broken(self, track_id: int, broken: bool = True) -> None:
        self._x("UPDATE tracks SET broken_at=? WHERE id=?", (int(time.time()) if broken else None, track_id))

    def track_file_replaced(self, track_id: int, source: str, duration: int | None) -> None:
        """Файл трека заменён новым: источник, длительность; громкость — пересчитать, пометка «проблемный» снята."""
        self._x("""UPDATE tracks SET source=?, duration=COALESCE(?, duration), gain_db=NULL, peak=NULL,
                   loudness_at=NULL, broken_at=NULL WHERE id=?""", (source, duration, track_id))

    def set_track_path(self, track_id: int, path: str) -> None:
        self._x("UPDATE tracks SET path=? WHERE id=?", (path, track_id))

    def links_for_track(self, track_id: int) -> list[sqlite3.Row]:
        return self._q("SELECT login, link_path FROM user_tracks WHERE track_id=?", (track_id,))

    def all_user_tracks(self) -> list[sqlite3.Row]:
        return self._q("SELECT login, track_id, link_path FROM user_tracks")

    def get_track(self, track_id: int) -> Track | None:
        rows = self._q("SELECT * FROM tracks WHERE id=?", (track_id,))
        return self._track(rows[0] if rows else None)

    def unlink_track(self, login: str, track_id: int) -> None:
        self._x("DELETE FROM user_tracks WHERE login=? AND track_id=?", (login, track_id))

    def track_refcount(self, track_id: int) -> int:
        return self._q("SELECT count(*) c FROM user_tracks WHERE track_id=?", (track_id,))[0]["c"]

    def delete_track(self, track_id: int) -> None:
        self._x("DELETE FROM tracks WHERE id=?", (track_id,))

    def stats(self) -> dict[str, int]:
        return {
            "users": self._q("SELECT count(*) c FROM users")[0]["c"],
            "tracks": self._q("SELECT count(*) c FROM tracks")[0]["c"],
            "links": self._q("SELECT count(*) c FROM user_tracks")[0]["c"],
        }

    # ---------- radio ----------
    def add_radio(self, owner: str | None, name: str, url: str, path: str) -> int:
        return self._x("INSERT OR REPLACE INTO radio(owner,name,url,path) VALUES(?,?,?,?)", (owner, name, url, path))

    def list_radio(self, owner: str | None) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM radio WHERE owner IS ? OR owner IS NULL ORDER BY owner IS NULL, name", (owner,))

    def get_radio(self, rid: int) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM radio WHERE id=?", (rid,))
        return rows[0] if rows else None

    def delete_radio(self, rid: int) -> None:
        self._x("DELETE FROM radio WHERE id=?", (rid,))

    # ---------- key-value ----------
    def kv_get(self, key: str) -> str | None:
        rows = self._q("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def kv_set(self, key: str, value: str) -> None:
        self._x("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, value))

    def kv_values(self, prefix: str) -> list[str]:
        return [r["value"] for r in self._q("SELECT value FROM kv WHERE key LIKE ?", (prefix + "%",))]

    def kv_items(self, prefix: str) -> dict[str, str]:
        """Точное совпадение префикса (LIKE считает «_» шаблоном)."""
        rows = self._q("SELECT key, value FROM kv WHERE substr(key, 1, ?)=?", (len(prefix), prefix))
        return {r["key"]: r["value"] for r in rows}

    def kv_delete(self, key: str) -> None:
        self._x("DELETE FROM kv WHERE key=?", (key,))

    # ---------- плейлисты ----------
    # Все изменения — в одной транзакции (_tx): проверка лимитов, вставка, updated_at.
    def track_in_playlists(self, track_id: int) -> bool:
        return bool(self._q("SELECT 1 FROM playlist_items WHERE track_id=? LIMIT 1", (track_id,)))

    def playlists_with_track(self, track_id: int) -> list[int]:
        return [r[0] for r in self._q("SELECT DISTINCT playlist_id FROM playlist_items WHERE track_id=?",
                                      (track_id,))]

    def delete_track_if_unused(self, track_id: int) -> bool:
        """Удаляет строку трека, если его нет ни в одной медиатеке (плейлисты файл не держат: строки плейлистов
        уходят каскадом). True — строка удалена."""
        with self._tx() as c:
            if c.execute("SELECT 1 FROM user_tracks WHERE track_id=? LIMIT 1", (track_id,)).fetchone():
                return False
            return c.execute("DELETE FROM tracks WHERE id=?", (track_id,)).rowcount == 1

    def create_playlist(self, owner: str, name: str, description: str = "", share_token: str | None = None,
                        *, track_ids: Iterable[int] = (), max_per_user: int | None = None,
                        max_tracks: int = 5000) -> int:
        """Новый плейлист (и сразу треки). PlaylistLimit — лимит плейлистов владельца."""
        now = int(time.time())
        with self._tx() as c:
            if max_per_user is not None and c.execute("SELECT count(*) FROM playlists WHERE owner=?",
                                                      (owner,)).fetchone()[0] >= max_per_user:
                raise PlaylistLimit("playlists")
            pid = c.execute("""INSERT INTO playlists(owner,name,description,share_token,created_at,updated_at)
                               VALUES(?,?,?,?,?,?)""", (owner, name, description, share_token, now, now)).lastrowid
            if track_ids:
                self.add_playlist_tracks(pid, track_ids, owner, max_tracks)  # type: ignore[arg-type]
            return pid  # type: ignore[return-value]

    def get_playlist(self, pid: int) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM playlists WHERE id=?", (pid,))
        return rows[0] if rows else None

    def playlist_by_token(self, token: str) -> sqlite3.Row | None:
        if not token:
            return None
        rows = self._q("SELECT * FROM playlists WHERE share_token=?", (token,))
        return rows[0] if rows else None

    def playlist_role(self, pid: int, login: str) -> str | None:
        """'owner' / 'member' / None (чужой или нет такого)."""
        pl = self.get_playlist(pid)
        if not pl:
            return None
        if pl["owner"] == login:
            return "owner"
        if self._q("SELECT 1 FROM playlist_members WHERE playlist_id=? AND login=?", (pid, login)):
            return "member"
        return None

    def user_playlists(self, login: str) -> list[sqlite3.Row]:
        """Свои и те, где участник — по updated_at, свежие первыми."""
        return self._q(
            """SELECT * FROM playlists WHERE owner=? OR id IN (SELECT playlist_id FROM playlist_members WHERE login=?)
               ORDER BY updated_at DESC, id DESC""", (login, login))

    def owned_playlist_count(self, login: str) -> int:
        return self._q("SELECT count(*) c FROM playlists WHERE owner=?", (login,))[0]["c"]

    def update_playlist(self, pid: int, *, name: str | None = None, description: str | None = None) -> None:
        with self._tx() as c:
            if name is not None:
                c.execute("UPDATE playlists SET name=? WHERE id=?", (name, pid))
            if description is not None:
                c.execute("UPDATE playlists SET description=? WHERE id=?", (description, pid))
            self.touch_playlist(pid)

    def touch_playlist(self, pid: int) -> None:
        self._x("UPDATE playlists SET updated_at=? WHERE id=?", (int(time.time()), pid))

    def set_share_token(self, pid: int, token: str | None) -> None:
        with self._tx():
            self._x("UPDATE playlists SET share_token=? WHERE id=?", (token, pid))
            self.touch_playlist(pid)

    def delete_playlist(self, pid: int) -> list[int]:
        """Удаляет плейлист; возвращает его track_id (кандидаты на очистку общего кэша)."""
        with self._tx() as c:
            ids = [r[0] for r in c.execute("SELECT track_id FROM playlist_items WHERE playlist_id=?", (pid,))]
            c.execute("DELETE FROM playlists WHERE id=?", (pid,))
            return ids

    def playlist_members(self, pid: int) -> list[str]:
        return [r["login"] for r in self._q(
            "SELECT login FROM playlist_members WHERE playlist_id=? ORDER BY joined_at, login", (pid,))]

    def add_playlist_member(self, pid: int, login: str, max_members: int | None = None) -> bool:
        """True — вступил (False — уже был). PlaylistLimit — участников уже максимум."""
        with self._tx() as c:
            if c.execute("SELECT 1 FROM playlist_members WHERE playlist_id=? AND login=?", (pid, login)).fetchone():
                return False
            if max_members is not None and c.execute("SELECT count(*) FROM playlist_members WHERE playlist_id=?",
                                                     (pid,)).fetchone()[0] >= max_members:
                raise PlaylistLimit("members")
            c.execute("INSERT INTO playlist_members(playlist_id,login,joined_at) VALUES(?,?,?)",
                      (pid, login, int(time.time())))
            self.touch_playlist(pid)
            return True

    def remove_playlist_member(self, pid: int, login: str, *, rotate: bool = False,
                               new_token: str | None = None) -> bool:
        """rotate=True — заодно сменить токен приглашения (исключённый не вернётся по старой ссылке)."""
        with self._tx() as c:
            n = c.execute("DELETE FROM playlist_members WHERE playlist_id=? AND login=?", (pid, login)).rowcount
            if n:
                if rotate:
                    c.execute("UPDATE playlists SET share_token=? WHERE id=?", (new_token, pid))
                self.touch_playlist(pid)
            return bool(n)

    def playlist_stats(self, pid: int) -> tuple[int, int]:
        """(число треков, суммарная длительность в секундах)."""
        r = self._q("""SELECT count(*) c, coalesce(sum(t.duration), 0) s FROM playlist_items pi
                       JOIN tracks t ON t.id=pi.track_id WHERE pi.playlist_id=?""", (pid,))[0]
        return r["c"], r["s"]

    def playlist_items(self, pid: int, login: str, limit: int | None = None) -> list[tuple[sqlite3.Row, Track, str | None]]:
        """[(строка playlist_items, трек, ссылка login или None)] по позиции."""
        rows = self._q(
            f"""SELECT t.*, pi.id AS item_id, pi.added_by, pi.position, pi.added_at AS item_added_at,
                       u.link_path FROM playlist_items pi JOIN tracks t ON t.id=pi.track_id
                LEFT JOIN user_tracks u ON u.track_id=t.id AND u.login=?
                WHERE pi.playlist_id=? ORDER BY pi.position, pi.id{' LIMIT ?' if limit else ''}""",
            (login, pid, *([limit] if limit else [])))
        return [(r, self._track(r), r["link_path"]) for r in rows]  # type: ignore[misc]

    def playlist_track_ids(self, pid: int) -> list[int]:
        return [r["track_id"] for r in self._q(
            "SELECT track_id FROM playlist_items WHERE playlist_id=? ORDER BY position, id", (pid,))]

    def add_playlist_tracks(self, pid: int, track_ids: Iterable[int], added_by: str, limit: int) -> tuple[int, int]:
        """Добавляет в конец (уже имеющиеся пропускает). Возвращает (добавлено, пропущено).
        Трек, исчезнувший из базы на ходу, просто пропускается."""
        ids = list(dict.fromkeys(track_ids))
        added = 0
        with self._tx() as c:
            if not c.execute("SELECT 1 FROM playlists WHERE id=?", (pid,)).fetchone():
                return 0, len(ids)
            have = {r[0] for r in c.execute("SELECT track_id FROM playlist_items WHERE playlist_id=?", (pid,))}
            pos = c.execute("SELECT coalesce(max(position), 0) FROM playlist_items WHERE playlist_id=?",
                            (pid,)).fetchone()[0]
            room = max(0, limit - len(have))
            now = int(time.time())
            for tid in ids:
                if tid in have or added >= room:
                    continue
                try:
                    c.execute("""INSERT INTO playlist_items(playlist_id,track_id,added_by,position,added_at)
                                 VALUES(?,?,?,?,?)""", (pid, tid, added_by, pos + 1024.0, now))
                except sqlite3.IntegrityError:  # трек удалили между проверкой доступа и вставкой
                    continue
                pos += 1024.0
                have.add(tid)
                added += 1
            if added:
                self.touch_playlist(pid)
        return added, len(ids) - added

    def remove_playlist_items(self, pid: int, item_ids: Iterable[int]) -> list[int]:
        """Удаляет позиции плейлиста; возвращает track_id удалённых."""
        removed: list[int] = []
        with self._tx() as c:
            for iid in dict.fromkeys(item_ids):
                row = c.execute("SELECT track_id FROM playlist_items WHERE id=? AND playlist_id=?",
                                (iid, pid)).fetchone()
                if row:
                    c.execute("DELETE FROM playlist_items WHERE id=?", (iid,))
                    removed.append(row[0])
            if removed:
                self.touch_playlist(pid)
        return removed

    def move_playlist_item(self, pid: int, item_id: int, to: int) -> bool:
        """Ставит позицию item_id на индекс to (в итоговом порядке). Позиции — дробные: двигается одна строка,
        а когда зазор между соседями становится слишком мал, весь плейлист перенумеровывается."""
        with self._tx() as c:
            rows = c.execute("SELECT id, position FROM playlist_items WHERE playlist_id=? ORDER BY position, id",
                             (pid,)).fetchall()
            if not any(r[0] == item_id for r in rows):
                return False
            rest = [(r[0], r[1]) for r in rows if r[0] != item_id]
            to = max(0, min(len(rest), to))
            prev = rest[to - 1][1] if to > 0 else None
            nxt = rest[to][1] if to < len(rest) else None
            if prev is None and nxt is None:
                new = 1024.0
            elif prev is None:
                new = nxt - 1024.0
            elif nxt is None:
                new = prev + 1024.0
            else:
                new = (prev + nxt) / 2
            if (prev is not None and new - prev < 1e-6) or (nxt is not None and nxt - new < 1e-6):
                order = [r[0] for r in rest]
                order.insert(to, item_id)
                c.executemany("UPDATE playlist_items SET position=? WHERE id=?",
                              [(1024.0 * (i + 1), iid) for i, iid in enumerate(order)])
            else:
                c.execute("UPDATE playlist_items SET position=? WHERE id=?", (new, item_id))
            self.touch_playlist(pid)
            return True

    def free_playlist_name(self, owner: str, base: str, maxlen: int = 100) -> str:
        """base, а если у владельца уже есть такой — «base (2)», «base (3)»…"""
        base = (base.strip() or "Плейлист")[:maxlen]
        names = {r["name"] for r in self._q("SELECT name FROM playlists WHERE owner=?", (owner,))}
        if base not in names:
            return base
        for i in range(2, 10000):
            suffix = f" ({i})"
            cand = base[:maxlen - len(suffix)] + suffix
            if cand not in names:
                return cand
        return base

    def put_playlist_track(self, pid: int, track_id: int, position: float, added_by: str,
                           limit: int = PL_MAX_TRACKS) -> bool:
        """Трек на заданную позицию (порядок источника, а не завершения загрузок). False — плейлиста нет,
        трек уже в нём, плейлист полон или трек исчез."""
        with self._tx() as c:
            if not c.execute("SELECT 1 FROM playlists WHERE id=?", (pid,)).fetchone():
                return False
            if c.execute("SELECT count(*) FROM playlist_items WHERE playlist_id=?", (pid,)).fetchone()[0] >= limit:
                return False
            try:
                c.execute("""INSERT INTO playlist_items(playlist_id,track_id,added_by,position,added_at)
                             VALUES(?,?,?,?,?)""", (pid, track_id, added_by, float(position), int(time.time())))
            except sqlite3.IntegrityError:  # уже в плейлисте (дубль в файле) или трек удалён
                return False
            self.touch_playlist(pid)
            return True

    def playlist_end_position(self, pid: int) -> float:
        """Позиция «после последнего» — для дописывания в конец."""
        r = self._q("SELECT coalesce(max(position), 0) p FROM playlist_items WHERE playlist_id=?", (pid,))
        return float(r[0]["p"]) + 1024.0

    def move_playlist_items(self, pid: int, item_ids: Iterable[int], to: int) -> int:
        """Выделение (в его текущем порядке) — блоком на индекс to среди остальных; плейлист перенумеровывается.
        Возвращает, сколько позиций перенесено (0 — ни одной из этого плейлиста)."""
        want = set(item_ids)
        with self._tx() as c:
            rows = [r[0] for r in c.execute("SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY position, id",
                                            (pid,))]
            block = [i for i in rows if i in want]
            if not block:
                return 0
            rest = [i for i in rows if i not in want]
            to = max(0, min(len(rest), to))
            order = rest[:to] + block + rest[to:]
            c.executemany("UPDATE playlist_items SET position=? WHERE id=?",
                          [(1024.0 * (n + 1), iid) for n, iid in enumerate(order)])
            self.touch_playlist(pid)
            return len(block)

    def playlists_for_track(self, login: str, track_id: int) -> list[tuple[sqlite3.Row, bool]]:
        rows = self._q(
            """SELECT p.*, EXISTS (SELECT 1 FROM playlist_items pi WHERE pi.playlist_id=p.id AND pi.track_id=?) has
               FROM playlists p WHERE p.owner=? OR p.id IN (SELECT playlist_id FROM playlist_members WHERE login=?)
               ORDER BY p.updated_at DESC, p.id DESC""", (track_id, login, login))
        return [(r, bool(r["has"])) for r in rows]

    # ---------- громкость (ReplayGain) ----------
    def next_loudness_track(self, skip: Iterable[int] = ()) -> Track | None:
        """Следующий трек без анализа громкости: свежие первыми (новые загрузки — сразу); skip — отложенные."""
        skip = list(skip)[:500]
        extra = f" AND id NOT IN ({','.join('?' * len(skip))})" if skip else ""
        rows = self._q(f"SELECT * FROM tracks WHERE loudness_at IS NULL{extra} ORDER BY id DESC LIMIT 1", skip)
        return self._track(rows[0] if rows else None)

    def set_loudness(self, track_id: int, gain_db: float | None, peak: float | None) -> None:
        """gain_db=None — анализ не удался (повторять не будем)."""
        self._x("UPDATE tracks SET gain_db=?, peak=?, loudness_at=? WHERE id=?",
                (gain_db, peak, int(time.time()), track_id))

    def loudness_pending(self) -> int:
        return self._q("SELECT count(*) c FROM tracks WHERE loudness_at IS NULL")[0]["c"]

    # ---------- настройки пользователя ----------
    def settings(self, login: str) -> dict[str, tuple[str, int]]:
        return {r["key"]: (r["value"], r["updated_at"]) for r in self._q(
            "SELECT key, value, updated_at FROM user_settings WHERE login=? ORDER BY key", (login,))}

    def put_setting(self, login: str, key: str, value: str, base: int | None,
                    max_keys: int) -> tuple[str, tuple[str, int] | None]:
        """('ok', (value, updated_at)) / ('conflict', текущее) — base устарел / ('limit', None)."""
        with self._tx() as c:
            row = c.execute("SELECT value, updated_at FROM user_settings WHERE login=? AND key=?",
                            (login, key)).fetchone()
            if base is not None and (row is None or row[1] != base):
                return "conflict", (row[0], row[1]) if row else None
            if row is None and c.execute("SELECT count(*) FROM user_settings WHERE login=?",
                                         (login,)).fetchone()[0] >= max_keys:
                return "limit", None
            now = int(time.time() * 1000)
            ts = max(now, row[1] + 1) if row else now
            c.execute("""INSERT INTO user_settings(login,key,value,updated_at) VALUES(?,?,?,?)
                         ON CONFLICT(login, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                      (login, key, value, ts))
            return "ok", (value, ts)

    def delete_setting(self, login: str, key: str) -> bool:
        with self._lock:
            return self._conn.execute("DELETE FROM user_settings WHERE login=? AND key=?",
                                      (login, key)).rowcount == 1

    # ---------- устройства и вход вне Telegram ----------
    def add_device(self, login: str, name: str, platform: str, token_hash: str, max_devices: int = 20,
                   idle_ttl: int | None = None) -> int:
        """Новое устройство. DeviceLimit — уже максимум (сначала выйти на старом). Давно не появлявшиеся
        (idle_ttl) удаляются и не считаются."""
        now = int(time.time())
        with self._tx() as c:
            if idle_ttl:
                c.execute("DELETE FROM devices WHERE login=? AND last_seen<?", (login, now - idle_ttl))
            if c.execute("SELECT count(*) FROM devices WHERE login=?", (login,)).fetchone()[0] >= max_devices:
                raise DeviceLimit(login)
            return c.execute("""INSERT INTO devices(login,name,platform,token_hash,created_at,last_seen)
                                VALUES(?,?,?,?,?,?)""", (login, name, platform, token_hash, now, now)).lastrowid

    def device_by_token_hash(self, token_hash: str) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM devices WHERE token_hash=?", (token_hash,))
        return rows[0] if rows else None

    def touch_device(self, device_id: int, now: int) -> None:
        self._x("UPDATE devices SET last_seen=? WHERE id=?", (now, device_id))

    def user_devices(self, login: str) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM devices WHERE login=? ORDER BY last_seen DESC, id DESC", (login,))

    def device_count(self, login: str) -> int:
        return self._q("SELECT count(*) c FROM devices WHERE login=?", (login,))[0]["c"]

    def revoke_all_devices(self, login: str) -> int:
        with self._lock:
            return self._conn.execute("DELETE FROM devices WHERE login=?", (login,)).rowcount

    def purge_idle_devices(self, idle_ttl: int) -> int:
        with self._lock:
            return self._conn.execute("DELETE FROM devices WHERE last_seen<?",
                                      (int(time.time()) - idle_ttl,)).rowcount

    def revoke_device(self, login: str, device_id: int) -> bool:
        with self._lock:
            return self._conn.execute("DELETE FROM devices WHERE id=? AND login=?", (device_id, login)).rowcount == 1

    def create_nonce(self, nonce_hash: str, device: str, platform: str, ttl: int, max_pending: int,
                     client_ip: str = "", code: str = "") -> bool:
        """False — слишком много незавершённых входов (защита базы от засорения)."""
        now = int(time.time())
        with self._tx() as c:
            c.execute("DELETE FROM auth_nonces WHERE expires_at<?", (now - 3600,))
            if c.execute("SELECT count(*) FROM auth_nonces WHERE status='pending' AND expires_at>=?",
                         (now,)).fetchone()[0] >= max_pending:
                return False
            c.execute("""INSERT INTO auth_nonces(nonce_hash,device,platform,created_at,expires_at,client_ip,code)
                         VALUES(?,?,?,?,?,?,?)""", (nonce_hash, device, platform, now, now + ttl, client_ip, code))
            return True

    def nonce_by_hash(self, nonce_hash: str) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM auth_nonces WHERE nonce_hash=?", (nonce_hash,))
        return rows[0] if rows else None

    def get_nonce(self, nonce_id: int) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM auth_nonces WHERE id=?", (nonce_id,))
        return rows[0] if rows else None

    def claim_nonce_opener(self, nonce_id: int, tg_id: int) -> bool:
        """Первый, кто открыл ссылку входа в боте, «владеет» ею; другой аккаунт — отказ."""
        with self._lock:
            return self._conn.execute(
                "UPDATE auth_nonces SET opener_tg=? WHERE id=? AND (opener_tg IS NULL OR opener_tg=?)",
                (tg_id, nonce_id, tg_id)).rowcount == 1

    def decide_nonce(self, nonce_id: int, login: str | None, approve: bool, tg_id: int | None = None) -> bool:
        """Разрешить/отклонить вход — только ожидающий, не истёкший и (если задан tg_id) открытый этим аккаунтом."""
        sql = "UPDATE auth_nonces SET status=?, login=? WHERE id=? AND status='pending' AND expires_at>=?"
        args: list = ["approved" if approve else "denied", login if approve else None, nonce_id, int(time.time())]
        if tg_id is not None:
            sql += " AND opener_tg=?"
            args.append(tg_id)
        with self._lock:
            return self._conn.execute(sql, args).rowcount == 1

    def use_nonce(self, nonce_id: int) -> bool:
        """Одноразовая выдача токена: True только для первого опроса после «Разрешить»."""
        with self._lock:
            return self._conn.execute(
                "UPDATE auth_nonces SET status='used' WHERE id=? AND status='approved' AND expires_at>=?",
                (nonce_id, int(time.time()))).rowcount == 1

    # ---------- загрузка своих файлов ----------
    UPLOAD_ACTIVE = ("receiving", "queued", "analyzing", "processing")
    UPLOAD_FIELDS = {"state", "manual", "guess_artist", "guess_title", "guess_album", "duration", "track_id",
                     "prompt_msg", "note", "path", "size", "chat_id"}

    def create_upload(self, login: str, orig_name: str, path: str, *, size: int = 0, state: str = "queued",
                      origin: str = "app", chat_id: int | None = None, batch: str = "") -> int:
        now = int(time.time())
        return self._x("""INSERT INTO uploads(login,orig_name,path,size,state,origin,chat_id,batch,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?)""",
                       (login, orig_name, path, size, state, origin, chat_id, batch, now, now))

    def get_upload(self, uid: int) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM uploads WHERE id=?", (uid,))
        return rows[0] if rows else None

    def set_upload(self, uid: int, *, expect: tuple[str, ...] | None = None, **fields) -> bool:
        """Обновить поля; expect — только если текущее состояние из списка (защита от гонок). True — обновили."""
        bad = set(fields) - self.UPLOAD_FIELDS
        if bad:
            raise ValueError(f"unknown upload fields: {bad}")
        sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
        args: list = [*fields.values(), int(time.time()), uid]
        sql = f"UPDATE uploads SET {sets} WHERE id=?"
        if expect:
            sql += f" AND state IN ({','.join('?' * len(expect))})"
            args += list(expect)
        with self._lock:
            return self._conn.execute(sql, args).rowcount == 1

    def claim_upload(self) -> sqlite3.Row | None:
        """Следующая загрузка в очереди -> analyzing (атомарно: два воркера не возьмут одну)."""
        with self._tx() as c:
            row = c.execute("SELECT id FROM uploads WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            c.execute("UPDATE uploads SET state='analyzing', updated_at=? WHERE id=?", (int(time.time()), row[0]))
        return self.get_upload(row[0])

    def reset_uploads(self) -> tuple[int, int]:
        """После перезапуска: начатые — обратно в очередь, недокачанные из Telegram — неудача."""
        now = int(time.time())
        with self._lock:
            requeued = self._conn.execute("UPDATE uploads SET state='queued', updated_at=? "
                                          "WHERE state IN ('analyzing','processing')", (now,)).rowcount
            failed = self._conn.execute("UPDATE uploads SET state='failed', note='загрузка прервана перезапуском', "
                                        "updated_at=? WHERE state='receiving'", (now,)).rowcount
        return requeued, failed

    def user_uploads(self, login: str, limit: int = 100) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM uploads WHERE login=? ORDER BY id DESC LIMIT ?", (login, limit))

    def queued_upload_bytes(self, login: str) -> int:
        marks = ",".join("?" * len(self.UPLOAD_ACTIVE))
        return self._q(f"SELECT coalesce(sum(size),0) s FROM uploads WHERE login=? AND state IN ({marks}, 'need_info')",
                       (login, *self.UPLOAD_ACTIVE))[0]["s"]

    def stale_receiving(self, older_than: int) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM uploads WHERE state='receiving' AND updated_at<?",
                       (int(time.time()) - older_than,))

    def active_upload_count(self, login: str) -> int:
        marks = ",".join("?" * len(self.UPLOAD_ACTIVE))
        return self._q(f"SELECT count(*) c FROM uploads WHERE login=? AND state IN ({marks}, 'need_info')",
                       (login, *self.UPLOAD_ACTIVE))[0]["c"]

    def batch_uploads(self, batch: str) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM uploads WHERE batch=? ORDER BY id", (batch,))

    def upload_by_prompt(self, chat_id: int, msg_id: int) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM uploads WHERE chat_id=? AND prompt_msg=? ORDER BY id DESC LIMIT 1",
                       (chat_id, msg_id))
        return rows[0] if rows else None

    def stale_uploads(self, older_than: int) -> list[sqlite3.Row]:
        """Брошенные «ждут названия» — для чистки."""
        return self._q("SELECT * FROM uploads WHERE state='need_info' AND updated_at<?",
                       (int(time.time()) - older_than,))

    def upload_paths(self) -> set[str]:
        marks = ",".join("?" * len(self.UPLOAD_ACTIVE))
        return {r["path"] for r in self._q(f"SELECT path FROM uploads WHERE state IN ({marks}, 'need_info')",
                                           self.UPLOAD_ACTIVE)}

    # ---------- import ----------
    def create_import(self, login: str, chat_id: int, filename: str, items: list[dict],
                      status_msg: int | None = None, state: str = "running", source: str = "tg") -> int:
        """state='draft' — файл разобран и ждёт кнопки «Начать импорт»: черновик в базе переживает перезапуск бота.
        Импорт и все позиции — одной транзакцией (без «полуимпортов» при сбое).
        source='app' — импорт из приложения/браузера: в Telegram о нём не пишем (chat_id = 0)."""
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO imports(login,chat_id,filename,total,created_at,status_msg,state,source)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (login, chat_id if source != "app" else 0, filename, len(items), int(time.time()), status_msg, state,
                 source))
            iid = cur.lastrowid
            c.executemany(
                """INSERT INTO import_items(import_id,idx,artist,title,album,isrc,duration,spotify_url)
                   VALUES(?,?,?,?,?,?,?,?)""",
                [(iid, i, it.get("artist", ""), it.get("title", ""), it.get("album") or None, it.get("isrc") or None,
                  it.get("duration") or None, it.get("spotify_url") or None) for i, it in enumerate(items)])
            return iid  # type: ignore[return-value]

    def get_import(self, iid: int) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM imports WHERE id=?", (iid,))
        return rows[0] if rows else None

    def running_imports(self, login: str | None = None) -> list[sqlite3.Row]:
        """Без login — только идущие (для очереди). С login — идущие и на паузе (для команд пользователя)."""
        if login:
            return self._q("SELECT * FROM imports WHERE state IN ('running','paused') AND login=? ORDER BY id",
                           (login,))
        return self._q("SELECT * FROM imports WHERE state='running' ORDER BY id")

    def activate_import_draft(self, iid: int) -> bool:
        """Черновик → в работу. False, если его уже запустили или отменили (двойное нажатие)."""
        with self._lock:
            return self._conn.execute("UPDATE imports SET state='running', created_at=? WHERE id=? AND state='draft'",
                                      (int(time.time()), iid)).rowcount == 1

    def delete_import_draft(self, iid: int) -> bool:
        with self._lock:
            if self._conn.execute("DELETE FROM imports WHERE id=? AND state='draft'", (iid,)).rowcount != 1:
                return False
            self._conn.execute("DELETE FROM import_items WHERE import_id=?", (iid,))
            return True

    def purge_import_drafts(self, older_than: float = 14 * 86400) -> int:
        """Забытые черновики (файл прислали, кнопку так и не нажали) — чистим через две недели."""
        with self._tx() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM imports WHERE state='draft' AND created_at<?",
                                           (int(time.time() - older_than),))]
            for iid in ids:
                c.execute("DELETE FROM import_items WHERE import_id=?", (iid,))
                c.execute("DELETE FROM imports WHERE id=?", (iid,))
            return len(ids)

    def set_import_state(self, iid: int, state: str) -> None:
        self._x("UPDATE imports SET state=? WHERE id=?", (state, iid))

    def set_import_status_msg(self, iid: int, msg_id: int) -> None:
        self._x("UPDATE imports SET status_msg=? WHERE id=?", (msg_id, iid))

    def reset_active_import_items(self) -> int:
        """После перезапуска бота «зависшие» в работе позиции возвращаются в очередь."""
        with self._lock:
            return self._conn.execute("UPDATE import_items SET state='pending' WHERE state='active'").rowcount

    def next_import_item(self, exclude_imports: set[int] | None = None) -> sqlite3.Row | None:
        """Следующая позиция: по очереди между импортами разных людей (самый «отстающий» первым).
        Прогресс считается один раз на импорт (а не на каждую позицию), позиция берётся по индексу."""
        imports = self._q(
            """SELECT im.id, im.total, (SELECT count(*) FROM import_items x WHERE x.import_id=im.id
                                         AND x.state IN ('done','cached','failed','active')) AS processed
               FROM imports im WHERE im.state='running'""")
        fallback = None
        for im in sorted(imports, key=lambda r: (r["processed"] / max(1, r["total"]), r["id"])):
            rows = self._q("SELECT * FROM import_items WHERE import_id=? AND state='pending' ORDER BY idx LIMIT 1",
                           (im["id"],))
            if not rows:
                continue
            if exclude_imports and im["id"] in exclude_imports:
                fallback = fallback or rows[0]
                continue
            return rows[0]
        return fallback

    def set_import_item(self, item_id: int, state: str, note: str | None = None,
                        track_id: int | None = None) -> None:
        self._x("UPDATE import_items SET state=?, note=?, track_id=coalesce(?, track_id) WHERE id=?",
                (state, note, track_id, item_id))

    def get_import_item(self, item_id: int) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM import_items WHERE id=?", (item_id,))
        return rows[0] if rows else None

    def import_items_ok(self, iid: int) -> list[sqlite3.Row]:
        """Удачные позиции импорта (скачано / уже было) по порядку файла."""
        return self._q("SELECT * FROM import_items WHERE import_id=? AND state IN ('done','cached') ORDER BY idx",
                       (iid,))

    def set_import_playlist(self, iid: int, pid: int | None) -> None:
        self._x("UPDATE imports SET playlist_id=? WHERE id=?", (pid, iid))

    def imports_without_playlist(self) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM imports WHERE playlist_id IS NULL AND state<>'draft' ORDER BY id")

    def import_counts(self, iid: int) -> dict[str, int]:
        counts = {"pending": 0, "active": 0, "done": 0, "cached": 0, "failed": 0}
        for r in self._q("SELECT state, count(*) c FROM import_items WHERE import_id=? GROUP BY state", (iid,)):
            counts[r["state"]] = r["c"]
        return counts

    def failed_import_items(self, iid: int) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM import_items WHERE import_id=? AND state='failed' ORDER BY idx", (iid,))

    def cancel_import(self, iid: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE imports SET state='cancelled' WHERE id=?", (iid,))
            c.execute("DELETE FROM import_items WHERE import_id=? AND state='pending'", (iid,))

    # ---------- wave ----------
    def wave_seen(self, login: str) -> set[str]:
        return {r["match_key"] for r in self._q("SELECT match_key FROM wave_seen WHERE login=?", (login,))}

    def mark_wave(self, login: str, match_key: str, ok: bool) -> None:
        self._x("INSERT OR REPLACE INTO wave_seen(login,match_key,ok,seen_at) VALUES(?,?,?,?)",
                (login, match_key, int(ok), int(time.time())))
