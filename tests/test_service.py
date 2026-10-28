import copy

import pytest

from app.db import DB, User
from app.jellyfin import JellyfinError
from app.service import Service


class FakeJellyfin:
    def __init__(self):
        self.users_by_name: dict[str, dict] = {}
        self.passwords: dict[str, str] = {}
        self.policies: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.libs: dict[str, str] = {}
        self.fail_library = False
        self.fail_delete = False
        self.deleted: list[str] = []
        self._n = 0

    def add_user(self, name, password="pw12345678", policy=None, uid=None):
        self._n += 1
        uid = uid or f"uid{self._n}"
        self.users_by_name[name.lower()] = {"Id": uid, "Name": name, "Policy": dict(policy or {})}
        self.passwords[name.lower()] = password
        self.policies[uid] = dict(policy or {})
        return uid

    async def user_by_name(self, name):
        self.calls.append(("user_by_name", name))
        u = self.users_by_name.get(name.lower())
        return copy.deepcopy(u) if u else None

    async def check_password(self, name, pw):
        self.calls.append(("check_password", name))
        return {"ok": True} if self.passwords.get(name.lower()) == pw else None

    async def create_user(self, name, pw):
        self.calls.append(("create_user", name))
        uid = self.add_user(name, pw)
        return copy.deepcopy(self.users_by_name[name.lower()]) | {"Id": uid}

    async def set_policy(self, uid, **changes):
        self.calls.append(("set_policy", uid, changes))
        self.policies.setdefault(uid, {}).update(changes)
        for u in self.users_by_name.values():
            if u["Id"] == uid:
                u["Policy"] = dict(self.policies[uid])

    async def create_library(self, name, path, *a, **k):
        self.calls.append(("create_library", name, path))
        if self.fail_library:
            raise JellyfinError(500, "boom")
        lib_id = f"lib-{len(self.libs) + 1}"
        self.libs[name] = lib_id
        return {"ItemId": lib_id}

    async def users(self):
        return [copy.deepcopy(u) for u in self.users_by_name.values()]

    async def delete_user(self, uid):
        self.calls.append(("delete_user", uid))
        if self.fail_delete:
            raise JellyfinError(500, "nope")
        self.deleted.append(uid)
        self.users_by_name = {k: v for k, v in self.users_by_name.items() if v["Id"] != uid}

    def policy_calls(self, uid):
        return [c[2] for c in self.calls if c[0] == "set_policy" and c[1] == uid]


@pytest.fixture
def jf():
    return FakeJellyfin()


@pytest.fixture
def svc(db, jf):
    return Service(db, jf)


INVITE = "secret-invite"


class TestRegisterValidation:
    async def test_wrong_invite(self, svc, jf):
        ok, msg = await svc.register(1, "nope", "bob", "pw12345678")
        assert not ok and "инвайт" in msg.lower()
        assert jf.calls == []

    async def test_non_ascii_invite_does_not_crash(self, svc):
        for bad in ("пароль", "😀", "\u0000", "é" * 100, ""):
            ok, _ = await svc.register(1, bad, "bob", "pw12345678")
            assert ok is False

    async def test_invite_whitespace_trimmed(self, svc):
        ok, login = await svc.register(1, f"  {INVITE}\n", "bob", "pw12345678")
        assert ok and login == "bob"

    async def test_empty_server_invite_disables_registration(self, svc, monkeypatch):
        from conftest import patch_cfg
        patch_cfg(monkeypatch, invite_code="")
        ok, _ = await svc.register(1, "", "bob", "pw12345678")
        assert ok is False

    async def test_rate_limit_after_five_failures(self, svc):
        for _ in range(5):
            ok, msg = await svc.register(7, "bad", "bob", "pw12345678")
            assert not ok and "инвайт" in msg.lower()
        ok, msg = await svc.register(7, INVITE, "bob", "pw12345678")  # even a correct invite is now blocked
        assert not ok and "много" in msg.lower()
        # another telegram user is unaffected
        ok, _ = await svc.register(8, INVITE, "bob", "pw12345678")
        assert ok

    async def test_rate_limit_window_expires(self, svc, monkeypatch):
        import app.service as s
        for _ in range(5):
            await svc.register(7, "bad", "bob", "pw12345678")
        svc._fails[7] = [t - 1000 for t in svc._fails[7]]
        ok, _ = await svc.register(7, INVITE, "bob", "pw12345678")
        assert ok

    @pytest.mark.parametrize("login", ["a", "ab", "bad name", "../x", "-abc", "a" * 33, "имя123", "x/y", ""])
    async def test_invalid_login(self, svc, jf, login):
        ok, msg = await svc.register(1, INVITE, login, "pw12345678")
        assert not ok and "Логин" in msg
        assert jf.calls == []

    async def test_short_password(self, svc):
        for pw in ("abc", "123456789"):
            ok, msg = await svc.register(1, INVITE, "bob", pw)
            assert not ok and "Пароль" in msg and "10" in msg

    @pytest.mark.parametrize("login", ["admin", "root", "Library", "radio", "jellyfin", "slskd", "bot", "users"])
    async def test_reserved_logins(self, svc, jf, login):
        ok, msg = await svc.register(1, INVITE, login, "pw12345678")
        assert not ok and "зарезерв" in msg
        assert jf.calls == []

    async def test_telegram_bound_to_other_login(self, svc, db):
        ok, _ = await svc.register(1, INVITE, "bob", "pw12345678")
        assert ok
        ok, msg = await svc.register(1, INVITE, "alice", "pw12345678")
        assert not ok and "bob" in msg

    async def test_existing_jf_account_is_not_bindable_by_strangers(self, svc, jf):
        jf.add_user("bob", password="right-password")
        ok, msg = await svc.register(1, INVITE, "bob", "right-password")
        assert not ok and "занят" in msg and "администратора" in msg
        assert not [c for c in jf.calls if c[0] == "check_password"]  # пароль Jellyfin не проверяется — не оракул

    async def test_per_login_limit_is_persistent_and_shared_between_telegrams(self, svc, jf, db):
        jf.add_user("bob", password="right-password")
        for tg in range(10, 15):  # пять разных Telegram-аккаунтов
            ok, msg = await svc.register(tg, INVITE, "bob", "x" * 12)
            assert not ok and "занят" in msg
        ok, msg = await svc.register(99, INVITE, "bob", "x" * 12)
        assert not ok and "много" in msg
        svc2 = Service(db, jf)  # «перезапуск» — лимит в базе
        ok, msg = await svc2.register(100, INVITE, "bob", "x" * 12)
        assert not ok and "много" in msg
        ok, _ = await svc2.register(101, INVITE, "alice", "pw12345678")  # другой логин — можно
        assert ok

    async def test_global_limit(self, svc, monkeypatch):
        monkeypatch.setattr(Service, "REG_GLOBAL", 3)
        for tg in range(20, 23):
            await svc.register(tg, "bad", f"user{tg}", "pw12345678")
        ok, msg = await svc.register(30, INVITE, "fresh", "pw12345678")
        assert not ok and "много" in msg

    async def test_login_is_case_insensitive_against_jellyfin(self, svc, jf):
        jf.add_user("Bob", password="right")
        ok, msg = await svc.register(1, INVITE, "bob", "wrong1wrong")
        assert not ok and "занят" in msg
        assert ("create_user", "bob") not in jf.calls


class TestRegisterSuccess:
    async def test_creates_library_and_locked_policy(self, svc, jf, db, cfg):
        ok, login = await svc.register(42, INVITE, "bob", "pw12345678")
        assert ok and login == "bob"
        uid = jf.users_by_name["bob"]["Id"]
        # access is closed immediately after creation, before the library exists
        first = jf.policy_calls(uid)[0]
        assert first["EnableAllFolders"] is False and first["EnabledFolders"] == []
        names = [c[0] for c in jf.calls]
        assert names.index("create_library") > names.index("set_policy")
        lib_call = next(c for c in jf.calls if c[0] == "create_library")
        assert lib_call[1] == "Музыка · bob" and lib_call[2] == str(cfg.users_dir / "bob")
        assert (cfg.users_dir / "bob").is_dir()
        pol = jf.policies[uid]
        assert pol["EnableAllFolders"] is False
        assert pol["EnabledFolders"] == [jf.libs["Музыка · bob"]]
        assert pol["IsAdministrator"] is False and pol["EnableContentDeletion"] is False
        u = db.user_by_tg(42)
        assert u.login == "bob" and u.library_id == jf.libs["Музыка · bob"] and u.jf_user_id == uid
        assert not u.is_admin and len(u.stream_token) >= 16
        assert (cfg.radio_dir / "strm" / "bob").exists()

    async def test_admin_telegram_flagged(self, svc, db):
        ok, _ = await svc.register(999, INVITE, "boss", "pw12345678")
        assert ok and db.user_by_tg(999).is_admin

    async def test_admin_adopts_existing_account_with_right_password(self, svc, jf, db):
        uid = jf.add_user("Bob", password="short")  # пароль существующего аккаунта — любой длины
        ok, msg = await svc.register(999, INVITE, "bob", "wrong")
        assert not ok and "не подходит" in msg
        ok, login = await svc.register(999, INVITE, "bob", "short")
        assert ok and login == "Bob"
        assert ("create_user", "bob") not in jf.calls
        assert db.user_by_tg(999).jf_user_id == uid

    async def test_existing_admin_policy_not_downgraded(self, svc, jf):
        jf.add_user("rootadmin", password="pw12345678", policy={"IsAdministrator": True})
        ok, _ = await svc.register(999, INVITE, "rootadmin", "pw12345678")
        assert ok
        uid = jf.users_by_name["rootadmin"]["Id"]
        assert jf.policy_calls(uid) == []  # no restrictive policy applied to the admin

    async def test_reregistration_keeps_stream_token(self, svc, db, jf):
        await svc.register(5, INVITE, "bob", "pw12345678")
        tok = db.user_by_login("bob").stream_token
        ok, _ = await svc.register(5, INVITE, "bob", "pw12345678")
        assert ok and db.user_by_login("bob").stream_token == tok


class TestRegisterFailureCleanup:
    async def test_deletes_new_jellyfin_user_when_library_creation_fails(self, svc, jf, db):
        jf.fail_library = True
        with pytest.raises(JellyfinError):
            await svc.register(1, INVITE, "bob", "pw12345678")
        assert len(jf.deleted) == 1
        assert "bob" not in jf.users_by_name
        assert db.user_by_login("bob") is None

    async def test_cleanup_error_does_not_mask_original(self, svc, jf):
        jf.fail_library = True
        jf.fail_delete = True
        with pytest.raises(JellyfinError, match="boom"):
            await svc.register(1, INVITE, "bob", "pw12345678")

    async def test_does_not_delete_preexisting_user(self, svc, jf):
        jf.add_user("bob", password="pw12345678")
        jf.fail_library = True
        with pytest.raises(JellyfinError):
            await svc.register(999, INVITE, "bob", "pw12345678")
        assert jf.deleted == []
        assert "bob" in jf.users_by_name

    async def test_non_jellyfin_error_also_cleans_up(self, svc, jf, monkeypatch):
        def boom(*a, **k):
            raise ValueError("disk full")
        monkeypatch.setattr(svc.db, "upsert_user", boom)
        with pytest.raises(ValueError):
            await svc.register(1, INVITE, "bob", "pw12345678")
        assert len(jf.deleted) == 1


class TestIsolateOthers:
    def mk(self, db, jf, login, lib, policy):
        uid = jf.add_user(login, policy=policy)
        db.upsert_user(User(login, hash(login) % 99999, uid, lib, "t", False))
        return uid

    async def test_fixes_all_folders_and_foreign_folders(self, svc, jf, db):
        a = self.mk(db, jf, "alice", "libA", {"EnableAllFolders": True, "EnabledFolders": []})
        b = self.mk(db, jf, "bobby", "libB", {"EnableAllFolders": False, "EnabledFolders": ["libB", "libA"]})
        c = self.mk(db, jf, "carol", "libC", {"EnableAllFolders": False, "EnabledFolders": ["libC"]})
        await svc._isolate_others("libC")
        assert jf.policies[a]["EnabledFolders"] == ["libA"] and jf.policies[a]["EnableAllFolders"] is False
        assert jf.policies[b]["EnabledFolders"] == ["libB"]
        assert jf.policy_calls(c) == []  # already compliant -> untouched

    async def test_admins_and_manual_accounts_untouched(self, svc, jf, db):
        adm = jf.add_user("rootadmin", policy={"IsAdministrator": True, "EnableAllFolders": True})
        db.upsert_user(User("rootadmin", None, adm, "libX", "t", True))
        manual = jf.add_user("manual", policy={"EnableAllFolders": True})
        await svc._isolate_others("libX")
        assert jf.policy_calls(adm) == [] and jf.policy_calls(manual) == []

    async def test_user_without_library_in_db_untouched(self, svc, jf, db):
        uid = jf.add_user("nolib", policy={"EnableAllFolders": True})
        db.upsert_user(User("nolib", 5, uid, None, "t", False))
        await svc._isolate_others("x")
        assert jf.policy_calls(uid) == []

    async def test_jellyfin_error_is_swallowed(self, svc, jf):
        async def boom():
            raise JellyfinError(500, "x")
        jf.users = boom
        await svc._isolate_others("x")  # must not raise

    async def test_registration_isolates_existing_users(self, svc, jf, db):
        a = self.mk(db, jf, "alice", "libA", {"EnableAllFolders": True})
        ok, _ = await svc.register(77, INVITE, "newbie", "pw12345678")
        assert ok
        assert jf.policies[a]["EnableAllFolders"] is False and jf.policies[a]["EnabledFolders"] == ["libA"]
