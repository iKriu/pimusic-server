"""Общие утилиты: нормализация строк, безопасные имена файлов, запуск процессов, ffprobe."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma", ".alac", ".aiff", ".aif", ".webm"}

_FEAT_RE = re.compile(r"\s*[\(\[]?\s*\b(?:feat\.?|ft\.|ft(?=\s)|featuring|при участии)\s+[^\)\]]*[\)\]]?", re.I)
_JUNK_RE = re.compile(
    r"[\(\[]\s*(?:official\s*(?:music\s*)?(?:video|audio|lyric[s]?\s*video|visualizer)|lyrics?|audio|hd|hq|"
    r"4k|explicit|clean|music video|mv|clip|клип|премьера[^\)\]]*)\s*[\)\]]",
    re.I,
)


def strip_feat(s: str) -> str:
    return _FEAT_RE.sub("", s).strip()


def clean_title(s: str) -> str:
    """Убирает мусор из названий YouTube-роликов: (Official Video), [HD] и т.п."""
    s = _JUNK_RE.sub("", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" -–—_|")


def norm(s: str | None) -> str:
    """Агрессивная нормализация для сравнения строк."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ё", "е")
    s = s.replace("&", " and ")
    s = strip_feat(s)
    s = re.sub(r"[\(\[].*?(remaster(ed)?|version|edit|mono|stereo|deluxe|bonus)[^\)\]]*[\)\]]", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\bthe\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def primary_artist(artist: str) -> str:
    """'A feat. B', 'A, B', 'A & B' -> 'A' (для нормализации ключа кэша)."""
    a = strip_feat(artist)
    for sep in (" feat", " ft.", ", ", " & ", " x ", " × ", "; ", " / ", " vs. ", " vs "):
        idx = a.lower().find(sep)
        if idx > 0:
            a = a[:idx]
    return a.strip()


def match_key(artist: str, title: str) -> str:
    return f"{norm(primary_artist(artist))}|{norm(title)}"


def similarity(a: str, b: str) -> float:
    """Jaccard по словам, 0..1."""
    sa, sb = set(norm(a).split()), set(norm(b).split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def contains_all(hay: str, needle: str) -> bool:
    h = set(norm(hay).split())
    return all(w in h for w in norm(needle).split())


# Изменённые версии трека (ускоренные/замедленные/«для TikTok»): по целым словам, после plain_words()
SPEED_VARIANTS = ("sped up", "speed up", "speedup", "spedup", "ускоренная", "ускоренный", "ускорено", "slowed",
                  "super slowed", "slowed reverb", "reverb", "tiktok version", "tik tok version", "nightcore",
                  "daycore", "8d")


def plain_words(s: str) -> str:
    """« слова через пробел » в нижнем регистре: для поиска стоп-слов по целым словам (norm() режет скобки)."""
    return " " + re.sub(r"[\W_]+", " ", (s or "").lower()).strip() + " "


def unwanted_variant(candidate: str, wanted: str, words: tuple[str, ...] = SPEED_VARIANTS) -> str | None:
    """Стоп-слово из words, которое есть в candidate, но которого нет в запросе wanted (None — всё в порядке)."""
    c, w = plain_words(candidate), plain_words(wanted)
    for b in words:
        if f" {b} " in c and f" {b} " not in w:
            return b
    return None


_BAD_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(s: str, maxlen: int = 120) -> str:
    s = unicodedata.normalize("NFC", s or "").strip()
    s = _BAD_FS.sub("_", s)
    s = s.strip(". ")
    if not s:
        s = "_"
    if len(s.encode("utf-8")) > maxlen:
        b = s.encode("utf-8")[:maxlen]
        s = b.decode("utf-8", "ignore").rstrip(". ")
    return s


_INTERNAL_HOSTS = {"localhost", "jellyfin", "slskd", "bot", "pimusic-jellyfin", "pimusic-slskd", "pimusic-bot"}


def is_public_url(url: str) -> bool:
    """Только публичные http(s)-адреса: бот не должен ходить во внутреннюю сеть (SSRF)."""
    import ipaddress
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not u.hostname or any(ord(c) < 33 for c in url):
        return False
    host = u.hostname.lower().rstrip(".")  # "localhost." == "localhost"
    if not host or host in _INTERNAL_HOSTS or host.endswith((".local", ".internal", ".lan", ".localhost")):
        return False
    ip = None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # сокращённые и восьмерично/шестнадцатеричные формы (127.1, 0x7f.0.0.1, 2130706433) — как их поймёт резолвер
        # неканонические числовые формы (127.1, 0x7f.0.0.1, 010.0.0.1, 2130706433) резолвер трактует как IP — отклоняем
        if re.fullmatch(r"(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+))*", host):
            return False
    if ip is not None:
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                    or ip.is_unspecified)
    return "." in host  # одиночные имена (redis, db) — это внутренняя сеть Docker


def _public_ip(ip) -> bool:  # ipaddress.IPv4Address | IPv6Address
    """Адрес из интернета: не частный/loopback/link-local/CGNAT/multicast/зарезервированный (в т.ч. IPv4 в IPv6)."""
    import ipaddress
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved \
            or ip.is_unspecified or not ip.is_global:
        return False
    return not (isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"))


async def resolve_host(host: str) -> set[str]:
    """Все IP-адреса имени (getaddrinfo в пуле потоков asyncio)."""
    import socket
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return {info[4][0].split("%")[0] for info in infos}


async def is_public_url_resolved(url: str) -> bool:
    """is_public_url + DNS: ВСЕ адреса имени должны быть публичными (иначе имя, указывающее на 192.168.x или шлюз
    контейнеров, проходит текстовую проверку — SSRF). Не резолвится — отказ."""
    import ipaddress
    from urllib.parse import urlparse
    if not is_public_url(url):
        return False
    host = (urlparse(url).hostname or "").rstrip(".")
    try:
        return _public_ip(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        addrs = await resolve_host(host)
    except (OSError, UnicodeError):
        return False
    try:
        return bool(addrs) and all(_public_ip(ipaddress.ip_address(a)) for a in addrs)
    except ValueError:
        return False


def split_artist_title(q: str) -> tuple[str, str]:
    """'Artist - Title' -> (artist, title). Если разделителя нет — ('', q)."""
    q = q.strip()
    for sep in (" - ", " – ", " — ", " -- ", " | "):
        if sep in q:
            a, t = q.split(sep, 1)
            return a.strip(), t.strip()
    return "", q


_SECRET_ENV = ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "INVITE", "SLSK_", "SLDL_")


def _child_env() -> dict[str, str]:
    """Дочерним процессам (yt-dlp, spotdl, ffmpeg) секреты из .env не нужны."""
    return {k: v for k, v in os.environ.items() if not any(m in k.upper() for m in _SECRET_ENV)}


@dataclass
class ProcResult:
    rc: int
    out: str
    err: str


MAX_STDOUT = 64 * 1024 * 1024   # больше не держим в памяти (yt-dlp -J большого плейлиста — единицы МБ)
MAX_STDERR = 4 * 1024 * 1024    # от stderr хранится хвост: итог ffmpeg (ebur128 Summary) — в конце
# Ограничения для ffmpeg/ffprobe на недоверенных файлах: CPU-секунды и адресное пространство. AS — с большим
# запасом: это виртуальная память, а jellyfin-ffmpeg на многоядерном x86 резервирует по стеку и арене malloc
# на поток (при 2–3 ГБ ffprobe падал на обычных загрузках: «файл не читается ffprobe»). Ловим только «бомбы».
FFMPEG_LIMITS = (1800, 16 * 1024 ** 3)
PROBE_LIMITS = (120, 16 * 1024 ** 3)


def _rlimits(limits: tuple[int, int] | None):
    """preexec_fn для POSIX: RLIMIT_CPU и RLIMIT_AS дочернего процесса (на Windows — без ограничений)."""
    if not limits or os.name != "posix":
        return None

    def apply() -> None:
        import resource
        cpu, mem = limits
        for res, val in ((resource.RLIMIT_CPU, cpu), (resource.RLIMIT_AS, mem)):
            try:
                resource.setrlimit(res, (val, val))
            except (ValueError, OSError):
                pass
    return apply


async def _drain(stream: asyncio.StreamReader | None, cap: int, tail: bool) -> bytes:
    """Читает поток до конца, храня не больше cap байт (начало или хвост)."""
    if stream is None:
        return b""
    buf = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(buf)
        if tail:
            buf += chunk
            if len(buf) > cap:
                del buf[:len(buf) - cap]
        elif len(buf) < cap:
            buf += chunk[:cap - len(buf)]


async def run(cmd: list[str], *, timeout: float | None = None, cwd: str | Path | None = None,
              env: dict[str, str] | None = None, limits: tuple[int, int] | None = None,
              max_stdout: int = MAX_STDOUT) -> ProcResult:
    log.debug("run: %s", cmd[0])
    extra = {"preexec_fn": _rlimits(limits)} if _rlimits(limits) else {}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None, env={**_child_env(), **(env or {})}, **extra,
        )
    except (FileNotFoundError, PermissionError) as e:
        return ProcResult(127, "", f"{cmd[0]}: {e}")

    async def collect() -> tuple[bytes, bytes]:
        out_, err_ = await asyncio.gather(_drain(proc.stdout, max_stdout, False),
                                          _drain(proc.stderr, MAX_STDERR, True))
        await proc.wait()
        return out_, err_
    try:
        out, err = await asyncio.wait_for(collect(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ProcResult(-9, "", f"timeout after {timeout}s")
    except BaseException:  # отмена задачи — не оставляем осиротевший ffmpeg/yt-dlp
        if proc.returncode is None:
            proc.kill()
        raise
    return ProcResult(proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


@dataclass
class AudioInfo:
    codec: str
    bitrate: int  # бит/с
    duration: float
    sample_rate: int
    tags: dict[str, str]
    is_vbr: bool = False
    format_name: str = ""  # контейнер по ffprobe: «mp3», «flac», «mov,mp4,m4a,3gp,3g2,mj2»…


async def probe(path: Path) -> AudioInfo | None:
    # только локальные файлы: плейлисты/ссылки внутри файла (hls, concat, http) ffprobe не откроет
    r = await run(["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-print_format", "json",
                   "-show_format", "-show_streams", "-select_streams", "a:0", str(path)], timeout=60,
                  limits=PROBE_LIMITS, max_stdout=32 * 1024 * 1024)
    if r.rc != 0:
        log.warning("ffprobe failed for %s (rc=%s): %s", path.name, r.rc, r.err[-300:])
        return None
    try:
        data = json.loads(r.out)
        st = data["streams"][0]
        fmt = data.get("format", {})
    except (ValueError, KeyError, IndexError) as e:
        log.warning("ffprobe: нет аудиопотока или ответ не разобрать для %s: %s", path.name, e)
        return None
    tags = {k.lower(): v for k, v in {**fmt.get("tags", {}), **st.get("tags", {})}.items()}
    br = int(st.get("bit_rate") or fmt.get("bit_rate") or 0)
    return AudioInfo(
        codec=st.get("codec_name", ""),
        bitrate=br,
        duration=float(fmt.get("duration") or st.get("duration") or 0),
        sample_rate=int(st.get("sample_rate") or 0),
        tags=tags,
        format_name=str(fmt.get("format_name") or ""),
    )


async def mp3_is_cbr320(path: Path) -> bool:
    """Проверка по заголовкам MPEG-кадров через mutagen: CBR 320 kbps."""
    def _check() -> bool:
        try:
            from mutagen.mp3 import MP3, BitrateMode
            m = MP3(str(path))
            return m.info.bitrate >= 319000 and m.info.bitrate_mode in (BitrateMode.CBR, BitrateMode.UNKNOWN)
        except Exception:  # noqa: BLE001
            return False
    return await asyncio.to_thread(_check)


async def transcode_mp3_320(src: Path, dst: Path) -> bool:
    """Любой аудиофайл -> MP3 CBR 320 kbps, 44.1/48 кГц, без тегов (теги ставит tagger)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".part.mp3")
    r = await run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                   "-protocol_whitelist", "file", "-i", str(src),
                   "-map", "0:a:0", "-vn", "-map_metadata", "-1",
                   "-c:a", "libmp3lame", "-b:a", "320k", "-compression_level", "0",
                   "-ar", "44100", "-id3v2_version", "0", str(tmp)], timeout=1800, limits=FFMPEG_LIMITS)
    if r.rc != 0 or not tmp.exists() or tmp.stat().st_size < 10_000:
        log.warning("ffmpeg transcode failed %s: %s", src, r.err[-500:])
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dst)
    return True


def find_audio_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in AUDIO_EXTS else []
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS
                  and not p.name.endswith((".part", ".part.mp3", ".incomplete")))


def rmtree_quiet(p: Path) -> None:
    shutil.rmtree(p, ignore_errors=True)


def human_size(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ПБ"


# ------------------------------------------------------------------ жанры, год, сортировка медиатеки
_GENRE_SPLIT = re.compile(r"\s*(?:;|/|\||,|\x00)\s*")
_ACRONYMS = {"edm": "EDM", "r&b": "R&B", "rnb": "R&B", "uk": "UK", "us": "US", "dj": "DJ", "idm": "IDM",
             "ebm": "EBM", "lo-fi": "Lo-Fi", "k-pop": "K-Pop", "j-pop": "J-Pop", "hip-hop": "Hip-Hop",
             "hip hop": "Hip-Hop", "drum & bass": "Drum & Bass", "drum and bass": "Drum & Bass", "dnb": "Drum & Bass"}


def _genre_case(g: str) -> str:
    low = g.casefold()
    if low in _ACRONYMS:
        return _ACRONYMS[low]
    if re.search(r"[а-яё]", low):  # «русский рок»: по-русски заглавная только первая буква
        return g[:1].upper() + g[1:].lower()
    words = []
    for w in g.split(" "):
        parts = w.split("-")
        words.append("-".join(_ACRONYMS.get(p.casefold(), p[:1].upper() + p[1:].lower()) for p in parts))
    return " ".join(words)


def normalize_genres(values: "str | list[str] | tuple[str, ...] | None") -> list[str]:
    """«rock; Pop / hip hop» -> ["Rock", "Pop", "Hip-Hop"]: разделители ; / | , и NUL, регистр как в заголовке,
    аббревиатуры (EDM, R&B) — как принято, без повторов. Первый — основной жанр."""
    if not values:
        return []
    items = [values] if isinstance(values, str) else list(values)
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        for g in _GENRE_SPLIT.split(str(item or "")):
            g = re.sub(r"\s+", " ", g).strip(" .-")
            if not g or len(g) > 60 or g.isdigit():
                continue
            g = _genre_case(g)
            if g.casefold() not in seen:
                seen.add(g.casefold())
                out.append(g)
    return out[:10]


def year_from(value: "str | int | None") -> int | None:
    """Год из «2019-05-03», «2019», 2019; вне 1900…2100 — None."""
    m = re.search(r"\b(19\d\d|20\d\d|2100)\b", str(value or ""))
    return int(m.group(1)) if m else None


def collate(s: str | None) -> str:
    """Ключ сравнения по-русски: без регистра, ё = е, лишние пробелы прочь."""
    return re.sub(r"\s+", " ", (s or "").casefold().replace("ё", "е")).strip()


def artist_sort_key(s: str | None) -> str:
    """Исполнитель для сортировки: без «The » в начале."""
    return re.sub(r"^the\s+", "", collate(s))
