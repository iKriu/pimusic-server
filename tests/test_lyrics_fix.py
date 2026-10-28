"""Тексты песен: очистка названия для поиска, проверка синхронного текста против длительности и сбитых меток,
подбор (LRCLIB get/search → syncedlyrics), перезапрос у скачанного трека и разовая проверка всего кэша."""
import os
from pathlib import Path

import pytest
from mutagen.id3 import ID3, SYLT, USLT, Encoding

from app import http, lyrics, lyrics_fix, tagger, webapp

import test_webapp
from test_webapp import add_track, hdr

alice, client = test_webapp.alice, test_webapp.client  # фикстуры


def lrc(stamps: list[float], word: str = "строка") -> str:
    return "\n".join(f"[{int(t // 60):02d}:{t % 60:05.2f}]{word} {i}" for i, t in enumerate(stamps))


def even(start: float, end: float, n: int) -> list[float]:
    return [start + (end - start) * i / (n - 1) for i in range(n)]


GOOD = lrc(even(10, 170, 40))  # нормальная песня ~3 мин: строка каждые ~4 с


# ------------------------------------------------------------------ название для поиска
@pytest.mark.parametrize("src,want", [
    ("TOY (Eurovision 2018 - Israel)", "TOY"),
    ("Song - 2011 Remaster", "Song"),
    ("Song (Live) [Remastered 2009]", "Song"),
    ("Track - Radio Edit", "Track"),
    ("Love Me (Ремикс)", "Love Me"),
    ("Hello (feat. Someone)", "Hello"),
    ("Молодость моя", "Молодость моя"),
    ("Part 2 - Intro", "Part 2 - Intro"),          # дефис без слова версии — часть названия
    ("(I Can't Get No) Satisfaction", "(I Can't Get No) Satisfaction"),
    ("Mix", "Mix"),                                # пустой результат — исходное название
])
def test_search_title(src, want):
    assert lyrics.search_title(src) == want


def test_clean_lrc_drops_netease_credits():
    src = "[00:00.00] 作词 : Кто-то\n[00:00.00] 作曲 : Кто-то\n[00:05.00]Первая строка"
    assert lyrics.clean_lrc(src) == "[00:05.00]Первая строка"


# ------------------------------------------------------------------ проверка синхронного текста
def test_good_lyrics_pass():
    assert lyrics.sync_problem(GOOD, 180) == ""
    assert lyrics.sync_problem(GOOD, 0) == ""  # длительность неизвестна — проверяется только синхронизация


def test_longer_than_track_is_other_song():
    # «Молодость моя»: трек 2:49, а последняя строка на 3:55 — текст другой песни (NetEase через syncedlyrics)
    text = lrc(even(9, 236, 50))
    assert "другой песни" in lyrics.sync_problem(text, 169)
    assert lyrics.length_problem(text, 169)
    assert lyrics.sync_problem(lrc(even(9, 180, 50)), 169) == ""  # +11 с — в допуске (затухание, тишина)


def test_much_shorter_than_track_is_other_song():
    assert "другой песни" in lyrics.sync_problem(lrc(even(5, 80, 20)), 240)


def test_garbled_sync_detected():
    # «TOY»: строки сбиты в кучи по 0.3–0.7 с, между кучами — по 30–40 с тишины (метки как в файле с сервера)
    stamps = [0.01, 7.44, 9.22, 12.77, 14.41, 14.75, 29.78, 70.53, 72.09, 72.85, 73.56, 76.91, 79.88, 83.45,
              86.37, 86.99, 87.31, 116.3, 118.52, 120.09, 121.06, 121.82, 122.33, 124.05, 124.6, 132.31, 132.64,
              134.3, 134.65, 135.22, 135.77, 136.51, 138.03, 138.82, 141.63, 141.91, 142.04, 149.79, 150.2,
              150.58, 151.58, 153.46, 153.94, 158.8, 161.65, 162.11, 167.79, 170.15, 175.42, 177.72, 178.74]
    assert "сбитая синхронизация" in lyrics.sync_problem(lrc(stamps), 181)


def test_too_few_lines_not_judged():
    assert lyrics.sync_problem("[00:01.00]a\n[00:02.00]b", 180) == ""


def test_crammed_tail_is_not_garbled():
    # частый дефект LRCLIB: верная синхронизация, а последние строки «пачкой» за пару секунд до конца
    stamps = even(5, 150, 40) + [150 + 0.25 * i for i in range(1, 30)] + [158, 158.8, 159.6]
    assert lyrics.sync_problem(lrc(stamps), 215) == ""


def test_short_text_is_weak_problem():
    text = lrc(even(5, 80, 20))
    assert lyrics.sync_problem(text, 240) and lyrics.sync_problem(text, 240, strict=False) == ""


# ------------------------------------------------------------------ подбор текста
@pytest.fixture
def fake_lrclib(monkeypatch):
    """LRCLIB без сети: get — по (название, альбом), search — список записей."""
    state = {"get": {}, "search": [], "calls": [], "sl": None, "sl_calls": []}

    async def get_json(url, *, params=None, **kw):
        state["calls"].append((url.rsplit("/", 1)[1], dict(params or {})))
        if url.endswith("/get"):
            return state["get"].get((params["track_name"], params.get("album_name", "")))
        return state["search"]

    def fake_sl(query):
        state["sl_calls"].append(query)
        return state["sl"]

    monkeypatch.setattr(http, "get_json", get_json)
    monkeypatch.setattr(lyrics, "_syncedlyrics", fake_sl)
    return state


async def test_exact_get_with_original_title_first(fake_lrclib):
    fake_lrclib["get"][("TOY (Eurovision 2018 - Israel)", "Album")] = {"syncedLyrics": GOOD, "plainLyrics": "p"}
    synced, plain = await lyrics.fetch("Netta", "TOY (Eurovision 2018 - Israel)", "Album", 180)
    assert synced == GOOD and plain == "p"
    assert len(fake_lrclib["calls"]) == 1


async def test_garbled_exact_record_falls_back_to_clean_title(fake_lrclib):
    bad = lrc([0, 7, 9, 12, 14, 14.3, 14.6, 30, 70, 70.4, 70.8, 71.2, 71.6, 72, 120, 120.3, 120.6, 175])
    fake_lrclib["get"][("TOY (Eurovision 2018 - Israel)", "Album")] = {"syncedLyrics": bad, "plainLyrics": "plain"}
    fake_lrclib["get"][("TOY", "")] = {"syncedLyrics": GOOD, "plainLyrics": "plain2"}
    synced, plain = await lyrics.fetch("Netta", "TOY (Eurovision 2018 - Israel)", "Album", 180)
    assert synced == GOOD
    assert ("get", {"artist_name": "Netta", "track_name": "TOY", "duration": "180"}) in fake_lrclib["calls"]


async def test_garbled_exact_record_keeps_its_plain_text(fake_lrclib):
    bad = lrc([0, 7, 9, 12, 14, 14.3, 14.6, 30, 70, 70.4, 70.8, 71.2, 71.6, 72, 120, 120.3, 120.6, 175])
    fake_lrclib["get"][("Song", "")] = {"syncedLyrics": bad, "plainLyrics": "верный текст"}
    assert await lyrics.fetch("A", "Song", "", 180) == ("", "верный текст")


async def test_syncedlyrics_other_song_rejected(fake_lrclib):
    # LRCLIB ничего не дал, syncedlyrics вернул текст другой (более длинной) песни — лучше без текста
    fake_lrclib["sl"] = "[00:00.00] 作词 : X\n" + lrc(even(9, 236, 50))
    assert await lyrics.fetch("Три дня дождя", "Молодость моя", "У ЧАСТЬ ПЕРВАЯ", 169) == ("", "")
    assert fake_lrclib["sl_calls"] == ["Молодость моя Три дня дождя"]


async def test_syncedlyrics_plausible_accepted_and_cleaned(fake_lrclib):
    fake_lrclib["sl"] = "[00:00.00] 作词 : X\n" + GOOD
    synced, plain = await lyrics.fetch("A", "Song (Live)", "", 180)
    assert synced == GOOD and "作词" not in plain
    assert fake_lrclib["sl_calls"] == ["Song A"]  # уточнение версии в запросе не участвует


async def test_syncedlyrics_needs_duration(fake_lrclib):
    fake_lrclib["sl"] = GOOD
    assert await lyrics.fetch("A", "Song", "", 0) == ("", "")
    assert fake_lrclib["sl_calls"] == []


async def test_search_filters_other_artist_title_and_length(fake_lrclib):
    fake_lrclib["search"] = [
        {"artistName": "Other", "trackName": "Song", "duration": 180, "syncedLyrics": GOOD},
        {"artistName": "A", "trackName": "Completely Different", "duration": 180, "syncedLyrics": GOOD},
        {"artistName": "A", "trackName": "Song", "duration": 240, "syncedLyrics": GOOD},
        {"artistName": "A", "trackName": "Song", "duration": 181, "syncedLyrics": lrc(even(9, 236, 50))},
    ]
    assert await lyrics.fetch("A", "Song", "", 180) == ("", "")
    fake_lrclib["search"].append({"artistName": "A feat. B", "trackName": "Song (Remastered)", "duration": 181,
                                  "syncedLyrics": GOOD, "plainLyrics": "p"})
    assert await lyrics.fetch("A", "Song", "", 180) == (GOOD, "p")


# ------------------------------------------------------------------ запись текста у скачанного трека
def mp3_with_lyrics(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    tags = ID3()
    tags.add(USLT(encoding=Encoding.UTF8, lang="XXX", desc="", text=text))
    tags.add(SYLT(encoding=Encoding.UTF8, lang="XXX", format=2, type=1, desc="", text=[("x", 1000)]))
    tags.save(str(path), v2_version=4, v1=0)


def test_write_lyrics_tags_replaces_only_lyrics(tmp_path):
    p = tmp_path / "a.mp3"
    mp3_with_lyrics(p, "старый")
    assert tagger.write_lyrics_tags(p, GOOD, "")
    tags = ID3(str(p))
    assert [f.text for f in tags.getall("USLT")] == [GOOD] and len(tags.getall("SYLT")) == 1
    assert not tagger.write_lyrics_tags(p, GOOD, "")  # то же самое — файл не трогаем
    assert tagger.write_lyrics_tags(p, "", "")
    assert not ID3(str(p)).getall("USLT") and not ID3(str(p)).getall("SYLT")
    assert not tagger.write_lyrics_tags(tmp_path / "a.flac", GOOD, "")


def test_write_lyrics_updates_user_hardlinks(tmp_path):
    cache, link = tmp_path / "lib" / "a.mp3", tmp_path / "users" / "u" / "a.mp3"
    mp3_with_lyrics(cache, "old")
    cache.with_suffix(".lrc").write_text("старый\n", encoding="utf-8")
    link.parent.mkdir(parents=True)
    os.link(cache, link)
    os.link(cache.with_suffix(".lrc"), link.with_suffix(".lrc"))
    lyrics_fix.write_lyrics(cache, [link], GOOD, "")
    assert link.with_suffix(".lrc").read_text(encoding="utf-8").strip() == GOOD
    assert [f.text for f in ID3(str(link)).getall("USLT")] == [GOOD]  # mutagen пишет в тот же inode
    # нового синхронного нет — .lrc удаляется везде, обычный текст ложится в .txt (и ссылкой к пользователю)
    lyrics_fix.write_lyrics(cache, [link], "", "обычный")
    assert not cache.with_suffix(".lrc").exists() and not link.with_suffix(".lrc").exists()
    assert link.with_suffix(".txt").read_text(encoding="utf-8") == "обычный\n"
    assert os.path.samefile(cache.with_suffix(".txt"), link.with_suffix(".txt"))
    lyrics_fix.write_lyrics(cache, [link], "", "")
    assert not any(p.suffix in (".lrc", ".txt") for p in [*cache.parent.iterdir(), *link.parent.iterdir()])


def bad_track(db, cfg, title="Song", duration=169):
    tid = add_track(db, cfg, "alice", "Artist", title, cover=False)
    t = db.get_track(tid)
    db._x("UPDATE tracks SET duration=? WHERE id=?", (duration, tid))
    Path(t.path).with_suffix(".lrc").write_text(lrc(even(9, 236, 50)), encoding="utf-8")
    return db.get_track(tid)


async def test_refetch_replaces_and_clears(db, cfg, alice, monkeypatch):
    t = bad_track(db, cfg)

    async def found(*a, **kw):
        return GOOD, ""
    monkeypatch.setattr(lyrics, "fetch", found)
    res = await lyrics_fix.refetch(db, t, dry_run=True)
    assert res["status"] == "updated" and "другой песни" in res["problem"]
    lrc_path = Path(t.path).with_suffix(".lrc")
    assert "03:5" in lrc_path.read_text(encoding="utf-8")  # сухой прогон ничего не пишет
    assert (await lyrics_fix.refetch(db, t))["status"] == "updated"
    assert lrc_path.read_text(encoding="utf-8").strip() == GOOD
    assert (await lyrics_fix.refetch(db, t))["status"] == "same"

    async def nothing(*a, **kw):
        return "", ""
    monkeypatch.setattr(lyrics, "fetch", nothing)
    assert (await lyrics_fix.refetch(db, t))["status"] == "not_found"  # хороший старый текст не трогаем
    assert lrc_path.exists()
    assert (await lyrics_fix.refetch(db, t, clear=True))["status"] == "cleared"
    assert not lrc_path.exists()
    # текст «кончается до середины» (бывает у extended-версий) без замены не удаляется
    lrc_path.write_text(lrc(even(5, 60, 20)), encoding="utf-8")
    res = await lyrics_fix.refetch(db, t)
    assert res["status"] == "not_found" and res["problem"] and lrc_path.exists()


async def test_audit_dry_run_reports_and_apply_fixes(db, cfg, alice, monkeypatch):
    bad = bad_track(db, cfg, "Bad")
    ok = add_track(db, cfg, "alice", "Artist", "Good", cover=False)
    Path(db.get_track(ok).path).with_suffix(".lrc").write_text(lrc(even(10, 190, 40)), encoding="utf-8")
    calls = []

    async def nothing(*a, **kw):
        calls.append(a)
        return "", ""
    monkeypatch.setattr(lyrics, "fetch", nothing)
    lines = []
    stats = await lyrics_fix.audit(db, out=lines.append)
    assert stats["tracks"] == 2 and stats["synced"] == 2 and stats["suspicious"] == 1
    assert calls == [] and any("Bad" in x and "другой песни" in x for x in lines)
    assert "сухой прогон" in lines[-1]
    assert Path(bad.path).with_suffix(".lrc").exists()
    stats = await lyrics_fix.audit(db, apply=True, out=lines.append)
    assert stats["cleared"] == 1 and len(calls) == 1
    assert not Path(bad.path).with_suffix(".lrc").exists()
    assert Path(db.get_track(ok).path).with_suffix(".lrc").exists()


# ------------------------------------------------------------------ POST /api/tracks/{id}/lyrics/refetch
async def test_refetch_endpoint(client, db, cfg, monkeypatch):
    t = bad_track(db, cfg)

    async def found(*a, **kw):
        return GOOD, ""
    monkeypatch.setattr(lyrics, "fetch", found)
    r = await client.post(f"/api/tracks/{t.id}/lyrics/refetch", headers=hdr())
    assert r.status == 200
    data = await r.json()
    assert data["status"] == "updated" and data["synced"][0]["text"] == "строка 0"
    assert (await client.post("/api/tracks/99999/lyrics/refetch", headers=hdr())).status == 404
    assert (await client.post(f"/api/tracks/{t.id}/lyrics/refetch")).status == 401


async def test_refetch_endpoint_clear_and_limits(client, db, cfg, monkeypatch):
    t = bad_track(db, cfg)

    async def nothing(*a, **kw):
        return "", ""
    monkeypatch.setattr(lyrics, "fetch", nothing)
    r = await client.post(f"/api/tracks/{t.id}/lyrics/refetch", json={"clear": True}, headers=hdr())
    data = await r.json()
    assert data["status"] == "cleared" and data["synced"] is None and data["plain"] is None
    client.w._lyrics_busy.add(t.id)
    assert (await client.post(f"/api/tracks/{t.id}/lyrics/refetch", headers=hdr())).status == 409
    client.w._lyrics_busy.clear()
    monkeypatch.setattr(webapp, "LYRICS_REFETCH_PER_HOUR", 1)
    assert (await client.post(f"/api/tracks/{t.id}/lyrics/refetch", headers=hdr())).status == 429


# ------------------------------------------------------------------ по ревью: сбои сети, понижение, чужой clear
@pytest.mark.parametrize("src", ["Song (Original Sin)", "Hello (From The Sky)", "Hello - The Album Leaf",
                                 "Radio Gaga", "Single Ladies"])
def test_search_title_keeps_real_title_parts(src):
    assert lyrics.search_title(src) == src


async def test_fetch_raise_errors_on_network_failure(monkeypatch):
    async def down(url, **kw):
        if kw.get("strict"):
            raise http.FetchError("LRCLIB недоступен")
        return None
    monkeypatch.setattr(http, "get_json", down)
    monkeypatch.setattr(lyrics, "_syncedlyrics", lambda q: None)
    assert await lyrics.fetch("A", "Song", "", 180) == ("", "")  # обычный режим: «текста нет»
    with pytest.raises(http.FetchError):
        await lyrics.fetch("A", "Song", "", 180, raise_errors=True)


async def test_refetch_network_error_keeps_old_text(db, cfg, alice, monkeypatch):
    t = bad_track(db, cfg)

    async def down(*a, **kw):
        raise http.FetchError("сеть")
    monkeypatch.setattr(lyrics, "fetch", down)
    res = await lyrics_fix.refetch(db, t, clear=True)
    assert res["status"] == "error" and Path(t.path).with_suffix(".lrc").exists()
    stats = await lyrics_fix.audit(db, apply=True, out=lambda *_: None)
    assert stats["error"] == 1 and stats["cleared"] == 0 and Path(t.path).with_suffix(".lrc").exists()


async def test_refetch_does_not_downgrade_good_synced_to_plain(db, cfg, alice, monkeypatch):
    tid = add_track(db, cfg, "alice", "Artist", "Good", cover=False)
    t = db.get_track(tid)
    lrc_path = Path(t.path).with_suffix(".lrc")
    lrc_path.write_text(lrc(even(10, 190, 40)), encoding="utf-8")

    async def plain_only(*a, **kw):
        return "", "обычный текст"
    monkeypatch.setattr(lyrics, "fetch", plain_only)
    assert (await lyrics_fix.refetch(db, t))["status"] == "not_found"
    assert lyrics.is_synced(lrc_path.read_text(encoding="utf-8"))


async def test_refetch_endpoint_error_and_foreign_clear(client, db, cfg, monkeypatch):
    t = bad_track(db, cfg, "Mine")

    async def down(*a, **kw):
        raise http.FetchError("сеть")
    monkeypatch.setattr(lyrics, "fetch", down)
    r = await client.post(f"/api/tracks/{t.id}/lyrics/refetch", headers=hdr())
    assert r.status == 504 and Path(t.path).with_suffix(".lrc").exists()
    # трек виден только через чужой плейлист: clear игнорируется (текст общий у всех держателей)
    calls = []

    async def refetch(db_, track, **kw):
        calls.append(kw)
        return {"status": "not_found"}
    monkeypatch.setattr(lyrics_fix, "refetch", refetch)
    monkeypatch.setattr(client.db, "track_access", lambda login, tid: (db.get_track(tid), None))
    r = await client.post(f"/api/tracks/{t.id}/lyrics/refetch", json={"clear": True}, headers=hdr())
    assert r.status == 200 and calls[-1]["clear"] is False
