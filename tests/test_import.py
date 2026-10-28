"""Импорт медиатеки из CSV/TXT (Tune My Music, Exportify) и фоновая очередь импорта."""
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from app import importer, library, service
from app.sources import TrackRequest

TMM = ("\ufeffTrack name,Artist name,Album,Playlist name,Type,ISRC,Spotify - id\n"
       '"Electric Love","BØRNS","Dopamine","Favorite Songs","Favorite","USUM71416307","2GiJYvgVaD2HtM8GqD9EgQ"\n'
       '"Hello","Adele","25","Favorite Songs","Favorite","GBBKS1500214","4sPmO7WMQUAf45kwMOtONw"\n'
       '"Hello","Adele","25","Favorite Songs","Favorite","GBBKS1500214","0ENSn4fwAbCGeFGVUbXEU3"\n'
       '"Levitating","Dua Lipa","Future Nostalgia","Favorite Songs","Favorite","GBAHT1901299","39LLxExYz6ewLAcYrzQQyP"\n'
       '"Levitating (feat. DaBaby)","Dua Lipa","Future Nostalgia","Favorite Songs","Favorite","GBAHT2000942",'
       '"5nujrmhLynf4yMoMtj8AQF"\n'
       '"Группа крови","Кино","Группа крови","Favorite Songs","Favorite","FR59R1744890","1eXiBTnU3qz0Ha3rnXvYTK"\n')


def test_parse_tunemymusic_csv_keeps_every_row():
    items, fmt = asyncio.run(importer.parse_file(TMM.encode("utf-8"), "My Spotify Library.csv"))
    assert len(items) == 6, "ни одна строка не должна пропасть: у всех разные Spotify id"
    assert fmt.startswith("CSV")
    first = items[0]
    assert (first["artist"], first["title"], first["album"], first["isrc"]) == \
        ("BØRNS", "Electric Love", "Dopamine", "USUM71416307")
    assert first["spotify_url"] == "https://open.spotify.com/track/2GiJYvgVaD2HtM8GqD9EgQ"
    assert items[5]["artist"] == "Кино"
    assert importer.same_recording_count(items) == 1  # два «Hello» с одним ISRC


def test_exact_duplicate_rows_dropped():
    dup = TMM + '"Hello","Adele","25","Favorite Songs","Favorite","GBBKS1500214","4sPmO7WMQUAf45kwMOtONw"\n'
    items, fmt = asyncio.run(importer.parse_file(dup.encode("utf-8"), "x.csv"))
    assert len(items) == 6 and "пропущено: 1" in fmt


def test_parse_exportify_style_csv():
    csv_text = ("Track URI;Track Name;Artist Name(s);Album Name;Duration (ms);ISRC\n"
                "spotify:track:2GiJYvgVaD2HtM8GqD9EgQ;Electric Love;BØRNS;Dopamine;218106;USUM71416307\n"
                "spotify:track:39LLxExYz6ewLAcYrzQQyP;Levitating;Dua Lipa, DaBaby;Future Nostalgia;203807;GBAHT1901299\n")
    items, _ = asyncio.run(importer.parse_file(csv_text.encode("utf-8"), "liked.csv"))
    assert len(items) == 2
    assert items[0]["duration"] == pytest.approx(218.106)
    assert items[0]["spotify_url"].endswith("2GiJYvgVaD2HtM8GqD9EgQ")
    assert items[1]["title"] == "Levitating"


def test_parse_txt_variants(monkeypatch):
    async def no_swap(items):
        return False
    monkeypatch.setattr(importer, "fix_txt_order", no_swap)
    txt = ("# мой плейлист\n1. Muse - Supermassive Black Hole\n02) Кино — Группа крови\n"
           "Radiohead\tCreep\n\nпросто строка без разделителя\n")
    items, fmt = asyncio.run(importer.parse_file(txt.encode("utf-8"), "list.txt"))
    assert [(i["artist"], i["title"]) for i in items] == [
        ("Muse", "Supermassive Black Hole"), ("Кино", "Группа крови"), ("Radiohead", "Creep")]


def test_txt_order_detection_swaps(monkeypatch):
    async def hits(artist, title):  # Deezer «знает» только порядок артист→песня
        return 1 if artist in ("Muse", "Adele", "Кино") else 0
    monkeypatch.setattr(importer, "_deezer_hits", hits)
    txt = "Supermassive Black Hole - Muse\nHello - Adele\nГруппа крови - Кино\n"
    items, fmt = asyncio.run(importer.parse_file(txt.encode("utf-8"), "list.txt"))
    assert "Песня - Артист" in fmt
    assert items[0]["artist"] == "Muse" and items[0]["title"] == "Supermassive Black Hole"


def test_cp1251_txt(monkeypatch):
    async def no_swap(items):
        return False
    monkeypatch.setattr(importer, "fix_txt_order", no_swap)
    items, _ = asyncio.run(importer.parse_file("Кино - Звезда по имени Солнце\n".encode("cp1251"), "a.txt"))
    assert items[0]["artist"] == "Кино"


def test_garbage_csv_raises():
    with pytest.raises(ValueError):
        asyncio.run(importer.parse_file(b"foo,bar,baz\n1,2,3\n", "x.csv"))


# ---------------------------------------------------------------- «одна запись или разные версии»
def _add(db, cfg, artist, title, isrc=None, mbid=None):
    p = cfg.library_dir / artist / f"{title}.mp3"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    from app.util import match_key
    db.add_track(match_key=match_key(artist, title), mbid=mbid, isrc=isrc, artist=artist, title=title, album=None,
                 duration=200, path=str(p), source="t")
    return db.track_by_path(str(p))


def test_versions_with_different_isrc_are_not_merged(db, cfg):
    _add(db, cfg, "Dua Lipa", "Levitating", isrc="GBAHT1901299")
    assert library.lookup_cached(db, "Dua Lipa", "Levitating (feat. DaBaby)", isrc="GBAHT2000942") is None
    assert library.lookup_cached(db, "Dua Lipa", "Levitating", isrc="GBAHT1901299") is not None


def test_same_isrc_other_release_reused(db, cfg):
    t = _add(db, cfg, "Adele", "Hello", isrc="GBBKS1500214")
    assert library.lookup_cached(db, "Adele", "Hello", isrc="GBBKS1500214").id == t.id


def test_same_mbid_different_isrc_reused(db, cfg):
    t = _add(db, cfg, "ZHU", "Superfriends", isrc="QMALR1400002", mbid="m-1")
    found = library.find_same_recording(db, artist="ZHU", title="Superfriends", mbid="m-1", isrc="USSM11405662")
    assert found and found.id == t.id


def test_key_only_match_reused_when_no_ids(db, cfg):
    t = _add(db, cfg, "Muse", "Uprising")
    assert library.lookup_cached(db, "Muse", "Uprising").id == t.id


# ---------------------------------------------------------------- фоновая очередь импорта
class FakeMessenger:
    def __init__(self):
        self.sent, self.edits, self.docs = [], [], []

    async def send(self, chat_id, text):
        self.sent.append(text)
        return len(self.sent)

    async def edit(self, chat_id, mid, text):
        self.edits.append(text)

    async def send_document(self, chat_id, data, filename, caption=""):
        self.docs.append((filename, data.decode("utf-8-sig")))


def test_import_queue_end_to_end(db, cfg, monkeypatch):
    from app.db import User
    import app.config
    monkeypatch.setattr(service, "CFG", replace(app.config.CFG, download_workers=4))
    u = User(login="alice", tg_id=1, jf_user_id="jf", library_id=None, stream_token="t", is_admin=False)
    db.upsert_user(u)
    processed = []

    async def fake_process(self, job, req):
        processed.append((job.origin, req.artist, req.title, req.isrc, req.spotify_url))
        ok = req.title != "Missing"
        return service.TrackOutcome(req, ok, "✅" if ok else "❌ не нашёл", reused=req.title == "Hello")

    monkeypatch.setattr(service.Service, "_process", fake_process)
    monkeypatch.setattr(service.Service, "_schedule_refresh", lambda self, *a, **k: None)

    async def run():
        svc = service.Service(db, None)
        svc.messenger = FakeMessenger()
        svc._workers = [asyncio.create_task(svc._worker(i)) for i in range(4)]
        items = [{"artist": "Muse", "title": "Uprising", "album": "", "isrc": "GBAHT0900320", "duration": 0,
                  "spotify_url": "https://open.spotify.com/track/x"},
                 {"artist": "Adele", "title": "Hello", "album": "25", "isrc": "GBBKS1500214", "duration": 0,
                  "spotify_url": ""},
                 {"artist": "Nobody", "title": "Missing", "album": "", "isrc": "", "duration": 0, "spotify_url": ""}]
        iid = svc.start_import(u, 123, "lib.csv", items)
        for _ in range(100):
            await asyncio.sleep(0.2)
            if db.get_import(iid)["state"] == "done":
                break
        for w in svc._workers:
            w.cancel()
        svc._import_task.cancel()
        return svc, iid

    svc, iid = asyncio.run(run())
    assert db.get_import(iid)["state"] == "done"
    assert {p[2] for p in processed} == {"Uprising", "Hello", "Missing"}
    assert all(p[0] == "import" for p in processed)
    assert processed[[p[2] for p in processed].index("Uprising")][3:] == ("GBAHT0900320", "https://open.spotify.com/track/x")
    c = db.import_counts(iid)
    assert (c["done"], c["cached"], c["failed"]) == (1, 1, 1)
    assert svc.messenger.docs and svc.messenger.docs[0][0] == "not_found.csv"
    assert "Missing" in svc.messenger.docs[0][1]
    assert any("завершён" in t for t in svc.messenger.sent)


def test_interrupted_items_resume_after_restart(db):
    from app.db import User
    db.upsert_user(User(login="bob", tg_id=2, jf_user_id="jf", library_id=None, stream_token="t", is_admin=False))
    iid = db.create_import("bob", 2, "f.csv", [{"artist": "A", "title": "B"}, {"artist": "C", "title": "D"}])
    first = db.next_import_item()
    db.set_import_item(first["id"], "active")  # бот упал посреди загрузки
    assert db.reset_active_import_items() == 1
    assert db.import_counts(iid)["pending"] == 2


def test_cancel_import_keeps_finished(db):
    iid = db.create_import("bob", 2, "f.csv", [{"artist": "A", "title": "B"}, {"artist": "C", "title": "D"}])
    first = db.next_import_item()
    db.set_import_item(first["id"], "done")
    db.cancel_import(iid)
    c = db.import_counts(iid)
    assert c["done"] == 1 and c["pending"] == 0 and db.get_import(iid)["state"] == "cancelled"


def test_fair_order_between_two_imports(db):
    a = db.create_import("u1", 1, "a.csv", [{"artist": "A", "title": str(i)} for i in range(5)])
    b = db.create_import("u2", 2, "b.csv", [{"artist": "B", "title": str(i)} for i in range(5)])
    for _ in range(2):
        it = db.next_import_item()
        db.set_import_item(it["id"], "done")
    nxt = db.next_import_item()
    # после двух позиций одного импорта следующая берётся у отстающего
    seen = {db.get_import(nxt["import_id"])["filename"]}
    assert seen <= {"a.csv", "b.csv"}
    counts_a, counts_b = db.import_counts(a)["done"], db.import_counts(b)["done"]
    assert abs(counts_a - counts_b) <= 2


def test_reconcile_after_delete_in_player(db, cfg):
    """Трек удалили в Jellyfin/Finamp: файл в папке пользователя пропал → запись и общий кэш чистятся,
    но только когда трек больше ни у кого не остался."""
    from app.db import User
    for login in ("alice", "bob"):
        db.upsert_user(User(login=login, tg_id=None, jf_user_id="jf-" + login, library_id=None,
                            stream_token="t" + login, is_admin=False))
    t = _add(db, cfg, "Muse", "Uprising", isrc="GBAHT0900320")
    la = library.link_to_user(t, "alice")
    db.link_track("alice", t.id, str(la))
    lb = library.link_to_user(t, "bob")
    db.link_track("bob", t.id, str(lb))
    svc = service.Service.__new__(service.Service)
    svc.db = db
    svc._refresh_pending = {}
    svc._refresh_first = 0.0
    svc.jf = None

    async def run():
        la.unlink()  # alice удалила в плеере
        assert await svc.reconcile_deleted() == 0  # первый проход только запоминает
        n1 = await svc.reconcile_deleted()
        exists_after_alice = Path(t.path).exists()
        lb.unlink()  # потом bob
        await svc.reconcile_deleted()
        n2 = await svc.reconcile_deleted()
        for task in svc._refresh_pending.values():
            task.cancel()
        return n1, exists_after_alice, n2

    n1, exists_after_alice, n2 = asyncio.run(run())
    assert n1 == 1 and exists_after_alice, "у bob трек ещё есть — кэш не трогаем"
    assert n2 == 1 and not Path(t.path).exists() and db.get_track(t.id) is None


def test_orphan_playlists_deleted_only_unknown_wave():
    from app.jellyfin import Jellyfin

    class FakeJF(Jellyfin):
        def __init__(self):
            self.deleted = []

        async def users(self):
            return [{"Id": "u1", "Name": "alice"}]

        async def user_playlists(self, uid):
            return [{"Id": "own-wave", "Name": "Моя Волна"}, {"Id": "orphan", "Name": "Моя Волна1"},
                    {"Id": "mine", "Name": "Летний вайб"}]

        async def request(self, method, path, **kw):
            if method == "DELETE":
                self.deleted.append(path)
            return None

    jf = FakeJF()
    assert asyncio.run(jf.delete_orphan_playlists({"own-wave"})) == 1
    assert jf.deleted == ["/Items/orphan"], "свой плейлист бота и ручные плейлисты пользователя не трогаем"


def test_real_export_file_if_present():
    p = Path(r"C:/Users/user/Downloads/My Spotify Library.csv")
    if not p.exists():
        pytest.skip("реального экспорта нет")
    items, fmt = asyncio.run(importer.parse_file(p.read_bytes(), p.name))
    assert len(items) == 1337


def test_cleanup_stale_incoming(cfg, monkeypatch):
    import os
    import time as _t
    monkeypatch.setattr(service, "CFG", cfg)
    old = cfg.incoming_dir / "slskd" / "bot" / "abc" / "11 Glory Box.mp3"
    new = cfg.incoming_dir / "slskd" / "bot" / "def" / "fresh.mp3"
    for p in (old, new):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    past = _t.time() - 5 * 3600
    os.utime(old, (past, past))
    os.utime(old.parent, (past, past))
    assert service.cleanup_stale_incoming() == 1
    assert not old.exists() and new.exists()


# ---------------------------------------------------------------- управление импортом (кнопки)
def _svc(db):
    svc = service.Service.__new__(service.Service)
    svc.db = db
    svc.active = {}
    svc._rate_marks = {}
    svc._last_progress_text = {}
    svc._ensure_import_feeder = lambda: None
    return svc


def _items(n):
    return [{"artist": "A", "title": f"T{i}", "album": "", "isrc": "", "duration": 0, "spotify_url": ""}
            for i in range(n)]


def test_pause_resume_stop_owner_only(db):
    svc = _svc(db)
    iid = db.create_import("alice", 1, "lib.csv", _items(3))
    assert not svc.pause_import(iid, "bob"), "чужой импорт ставить на паузу нельзя"
    assert svc.pause_import(iid, "alice") and db.get_import(iid)["state"] == "paused"
    assert db.next_import_item() is None, "на паузе очередь не выдаёт новых позиций"
    assert [r["id"] for r in db.running_imports("alice")] == [iid], "пауза видна пользователю в /import_status"
    assert db.running_imports() == []
    assert svc.resume_import(iid, "alice") and db.get_import(iid)["state"] == "running"
    assert db.next_import_item() is not None
    assert svc.stop_import(iid, "alice") and db.get_import(iid)["state"] == "cancelled"
    assert not svc.stop_import(iid, "alice")


def test_buttons_follow_state(db):
    svc = _svc(db)
    iid = db.create_import("alice", 1, "lib.csv", _items(2))
    datas = [d for row in svc._import_buttons(iid) for _, d in row]
    assert f"impp:{iid}" in datas and f"impx:{iid}" in datas and f"impf:{iid}" in datas
    svc.pause_import(iid, "alice")
    datas = [d for row in svc._import_buttons(iid) for _, d in row]
    assert f"impr:{iid}" in datas and f"impp:{iid}" not in datas


def test_failed_csv_and_live_text(db):
    svc = _svc(db)
    iid = db.create_import("alice", 1, "lib.csv", _items(3))
    it = db.next_import_item()
    db.set_import_item(it["id"], "failed", "❌ не нашёл")
    assert svc.failed_csv(iid, "bob") is None
    csv_text = svc.failed_csv(iid, "alice").decode("utf-8-sig")
    assert "Track name" in csv_text and "T0" in csv_text
    svc.active = {100: "alice: Muse — Uprising", 101: "bob: X — Y", 0: "alice: ручной запрос"}
    text = svc.import_status_text(iid, live=True)
    assert "Muse — Uprising" in text and "X — Y" not in text and "ручной запрос" not in text
    assert "1 из 3" in text and "❌ не нашлось: 1" in text


def test_progress_message_sent_with_buttons_and_not_repeated(db):
    svc = _svc(db)
    svc.messenger = FakeMessenger()
    sent = []

    async def edit(chat, mid, text, buttons=None):
        sent.append((text, buttons))
    svc.messenger.edit = edit
    iid = db.create_import("alice", 1, "lib.csv", _items(2))
    db.set_import_status_msg(iid, 77)
    asyncio.run(svc._import_progress(iid))
    asyncio.run(svc._import_progress(iid))  # тот же текст — повторная правка не нужна
    assert len(sent) == 1 and sent[0][1] and "📥 Импорт" in sent[0][0]


# ---------------------------------------------------------------- регрессии ревью
class ButtonsMessenger(FakeMessenger):
    async def edit(self, chat_id, mid, text, buttons=None):
        self.edits.append(text)

    async def send(self, chat_id, text, buttons=None):
        self.sent.append(text)
        return len(self.sent)


def test_start_import_stores_status_msg_before_feeder_wakes(db):
    from app.db import User
    svc = _svc(db)
    seen = []
    svc._ensure_import_feeder = lambda: seen.append(db.running_imports()[-1]["status_msg"])
    u = User(login="alice", tg_id=1, jf_user_id="jf", library_id=None, stream_token="t", is_admin=False)
    iid = svc.start_import(u, 1, "lib.csv", _items(1), status_msg=55)
    assert seen == [55] and db.get_import(iid)["status_msg"] == 55
    assert db.get_import(svc.start_import(u, 1, "b.csv", _items(1)))["status_msg"] is None


def test_idle_feeder_wakes_immediately_on_new_import(db, cfg, monkeypatch):
    from app.db import User
    u = User(login="alice", tg_id=1, jf_user_id="jf", library_id=None, stream_token="t", is_admin=False)
    db.upsert_user(u)
    processed = []

    async def fake_process(self, job, req):
        processed.append(req.title)
        return service.TrackOutcome(req, True, "✅")

    monkeypatch.setattr(service.Service, "_process", fake_process)
    monkeypatch.setattr(service.Service, "_schedule_refresh", lambda self, *a, **k: None)

    async def run():
        svc = service.Service(db, None)
        svc._ensure_import_feeder()
        await asyncio.sleep(0.2)  # очередь простаивает (раньше — sleep(30))
        svc.start_import(u, 1, "lib.csv", _items(1))
        for _ in range(30):
            await asyncio.sleep(0.05)
            if processed:
                break
        svc._import_task.cancel()
        for w in svc._import_workers:
            w.cancel()

    asyncio.run(run())
    assert processed == ["T0"], "новый импорт стартует сразу, а не через 30 с"


def test_paused_import_message_refreshed_when_last_inflight_done(db):
    svc = _svc(db)
    svc._tasks = set()
    svc.messenger = ButtonsMessenger()
    iid = db.create_import("alice", 1, "lib.csv", _items(2), status_msg=77)
    item = db.next_import_item()
    db.set_import_item(item["id"], "active")
    svc.pause_import(iid, "alice")

    async def run():
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(service.TrackOutcome(TrackRequest(artist="A", title="T0"), True, "✅"))
        svc._import_item_done(item["id"], fut, {item["id"]: iid})
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(run())
    assert svc.messenger.edits and "на паузе" in svc.messenger.edits[-1]
    assert "✅ скачано: 1" in svc.messenger.edits[-1]


def test_finish_import_edits_and_sends_short_notice(db):
    svc = _svc(db)
    svc._schedule_refresh = lambda *a, **k: None
    svc.messenger = ButtonsMessenger()
    iid = db.create_import("alice", 1, "lib.csv", _items(3), status_msg=77)
    for state in ("done", "cached", "failed"):
        db.set_import_item(db.next_import_item()["id"], state)
    asyncio.run(svc._finish_import(iid))
    assert len(svc.messenger.edits) == 1 and "🎉 Импорт завершён" in svc.messenger.edits[0]
    assert svc.messenger.sent == ["🎉 Импорт «lib.csv» завершён: ✅ 1, ♻️ 1, ❌ 1"], "без полной копии отчёта"



def test_wave_first_track_does_not_wait_for_slow_jellyfin(db, cfg):
    """Каталог Jellyfin на слабой машине грузится ~17 с — первый трек станции берётся из базы бота мгновенно."""
    import time as _t
    from app import radio
    from app.db import User
    u = User(login="alice", tg_id=1, jf_user_id="jf", library_id=None, stream_token="t", is_admin=False)
    db.upsert_user(u)
    t = _add(db, cfg, "Muse", "Uprising", isrc="X1")
    link = library.link_to_user(t, "alice")
    db.link_track("alice", t.id, str(link))

    class SlowJF:
        async def audio_items(self, uid):
            await asyncio.sleep(5)
            return []

    async def run():
        picker = radio.Picker(SlowJF(), u, db)
        t0 = _t.monotonic()
        item = await picker.next()
        took = _t.monotonic() - t0
        picker._loading.cancel()
        return item, took

    item, took = asyncio.run(run())
    assert item and item["Name"] == "Uprising" and Path(item["Path"]) == link
    assert took < 1.0


def test_import_draft_survives_restart_and_confirms_once(db, cfg):
    """Кнопка «Начать импорт» не должна протухать после перезапуска бота: черновик лежит в базе."""
    from app.db import DB, User
    svc = _svc(db)
    started = []
    svc._ensure_import_feeder = lambda: started.append(1)
    u = User(login="bob", tg_id=2, jf_user_id="jf", library_id=None, stream_token="t", is_admin=False)
    iid = svc.draft_import(u, 2, "lib.csv", _items(3), status_msg=7)
    assert db.get_import(iid)["state"] == "draft"
    assert db.running_imports() == [] and db.running_imports("bob") == []
    assert db.next_import_item() is None          # черновик очередь не трогает
    svc2 = _svc(DB(cfg.db_path))                  # «перезапуск»: новый сервис и соединение, та же база
    svc2._ensure_import_feeder = lambda: started.append(2)
    assert svc2.confirm_import(iid, status_msg=9)
    assert not svc2.confirm_import(iid)            # двойное нажатие не запускает второй раз
    row = svc2.db.get_import(iid)
    assert row["state"] == "running" and row["status_msg"] == 9 and len(started) == 1
    assert svc2.db.next_import_item()["import_id"] == iid


def test_import_draft_cancel_and_purge(db):
    from app.db import User
    svc = _svc(db)
    u = User(login="bob", tg_id=2, jf_user_id="jf", library_id=None, stream_token="t", is_admin=False)
    a = svc.draft_import(u, 2, "a.csv", _items(2))
    assert db.delete_import_draft(a) and db.get_import(a) is None
    assert db.import_counts(a)["pending"] == 0
    b = svc.draft_import(u, 2, "b.csv", _items(2))
    assert db.purge_import_drafts() == 0           # свежий черновик живёт
    db._x("UPDATE imports SET created_at=0 WHERE id=?", (b,))
    assert db.purge_import_drafts() == 1 and db.get_import(b) is None
