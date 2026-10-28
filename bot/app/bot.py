"""Telegram-бот PiMusic."""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hmac
import html
import inspect
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, MenuButtonCommands,
                      MenuButtonWebApp, Message, Update, WebAppInfo)
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, Defaults,
                          MessageHandler, filters)

from . import auth, downloaders, http, importer, library, radio, search, sources, util, views, wave
from .config import CFG
from .db import DB, User
from .jellyfin import Jellyfin
from .service import Job, Service, playlist_line
from . import uploads as uploads_mod
from .webapp import setup_webapp
from .util import human_size
from .views import LiveReport, Reactor, SingleTrackView, StatusMessage, blockquote, fit_lines

log = logging.getLogger(__name__)

HELP = """<b>🎧 PiMusic — домашний стриминг</b>

<b>Регистрация</b>
<code>/register ИНВАЙТ логин пароль</code> — создать аккаунт Jellyfin с личной медиатекой

<b>Музыка</b>
Просто пришлите:
• группу или песню: <code>Radiohead</code>, <code>Weird Fishes</code> — покажу варианты, вы выберете кнопкой
  (у группы — популярные треки и альбомы, альбом можно скачать целиком)
• список из нескольких строк <code>Артист - Песня</code> — скачаю всё сразу
• ссылку Spotify на трек, альбом или плейлист
• ссылку YouTube / YouTube Music / SoundCloud / Bandcamp
• <b>файл CSV или TXT</b> со списком — импорт всей медиатеки (например, «Любимые треки» Spotify через
  Tune My Music → Export to file). Идёт в фоне, /import_status — прогресс, /import_cancel — остановить

Источники по очереди: Soulseek → spotDL → yt-dlp. Всё сохраняется в MP3 320 kbps с тегами, обложкой и синхронным текстом.

<b>Команды</b>
/wave — «Моя Волна»: найти и скачать похожие треки (Last.fm)
/mywave — обновить плейлист «Моя Волна» и получить ссылку на живой поток
/radio — интернет-радиостанции
<code>/radio_add Название | URL</code> — добавить станцию
/find <i>текст</i> — поиск в своей медиатеке
/delete <i>id</i> — удалить трек из своей медиатеки
/queue — очередь загрузок
/me — мой аккаунт и ссылки
/devices — где выполнен вход вне Telegram (PWA, приложения) и выход

<b>Свои файлы</b>
Пришлите аудиофайл (mp3, flac, wav, m4a, ogg…; до 20 МБ — ограничение Telegram, большие — через Mini App).
Я опознаю трек, добавлю обложку и текст и положу в медиатеку. Не опознал — спрошу «Исполнитель — Название».

<b>Плейлисты</b>
/playlists — список; создавать и слушать — в Mini App («🎵 PiMusic» слева от поля ввода)
• общий плейлист: включите доступ и отправьте ссылку-приглашение — участники смогут добавлять и удалять треки
• «Сохранить в медиатеку» — недостающие треки появятся у вас без скачивания
• плейлисты видны и в Jellyfin / Finamp / Symfonium (из треков вашей медиатеки)
"""

ADMIN_HELP = """
<b>Администратору</b>
/status — диск, кэш, Soulseek
/radio_del <i>id</i> — удалить станцию
/users — пользователи
Отправьте файл <code>cookies.txt</code> с подписью <code>/cookies</code> — cookies YouTube для yt-dlp/spotDL
"""


def _human_len(seconds: int) -> str:
    m = int(seconds or 0) // 60
    return f"{m // 60} ч {m % 60} мин" if m >= 60 else f"{m} мин"


class Notifier:
    """Одно «живое» сообщение для прогресса одного трека; итоги — отдельными сообщениями."""

    def __init__(self, msg: Message):
        self.msg = msg
        self.status: Message | None = None
        self._lock = asyncio.Lock()

    async def __call__(self, text: str) -> None:
        text = text[:4000]
        async with self._lock:
            try:
                if text.startswith("⏳"):
                    if self.status is None:
                        self.status = await self.msg.reply_text(text)
                    else:
                        with contextlib.suppress(BadRequest):
                            await self.status.edit_text(text)
                    return
                if self.status is not None:
                    st, self.status = self.status, None
                    try:
                        await st.edit_text(text)
                        return
                    except BadRequest:
                        pass
                await self.msg.reply_text(text)
            except TelegramError as e:
                log.warning("telegram send failed: %s", e)


class Messenger:
    """Отправка сообщений из фоновых задач (прогресс импорта) без привязки к входящему сообщению."""

    def __init__(self, owner: "PiMusicBot"):
        self.owner = owner

    async def send(self, chat_id: int, text: str, buttons: views.Buttons | None = None, *,
                   html: bool = False) -> int | None:
        m = await self.owner.app.bot.send_message(chat_id, text[:4096], reply_markup=views.keyboard(buttons),
                                                  parse_mode=ParseMode.HTML if html else None)
        return getattr(m, "message_id", None)

    async def edit(self, chat_id: int, message_id: int, text: str, buttons: views.Buttons | None = None, *,
                   html: bool = False) -> None:
        try:
            await self.owner.app.bot.edit_message_text(
                text[:4096], chat_id=chat_id, message_id=message_id, reply_markup=views.keyboard(buttons),
                parse_mode=ParseMode.HTML if html else None)
        except BadRequest as e:
            if not views.is_not_modified(e):
                raise

    async def send_document(self, chat_id: int, data: bytes, filename: str, caption: str = "") -> None:
        await self.owner.app.bot.send_document(chat_id, document=data, filename=filename, caption=caption[:1000])


class PiMusicBot:
    def __init__(self) -> None:
        self.db = DB(CFG.db_path)
        self.jf = Jellyfin(CFG.jellyfin_url, CFG.jellyfin_api_key())
        self.svc = Service(self.db, self.jf)
        self.radio = radio.RadioServer(self.db, self.jf)
        # Telegram Mini App (/app/) и его API (/api/) живут на том же aiohttp-сервере, что и потоки «Моей Волны»
        self.webapp = setup_webapp(self.radio.app, db=self.db, svc=self.svc, jf=self.jf,
                                   bot_token=CFG.telegram_token, radio=self.radio)
        self._bg: list[asyncio.Task] = []
        self.svc.messenger = Messenger(self)
        self.app: Application = (
            ApplicationBuilder().token(CFG.telegram_token).concurrent_updates(True)
            .defaults(Defaults(link_preview_options=LinkPreviewOptions(is_disabled=True)))
            .post_init(self._post_init).post_shutdown(self._post_shutdown).build()
        )
        a = self.app
        a.add_handler(CommandHandler("start", self.cmd_start))
        a.add_handler(CommandHandler("help", self.cmd_help))
        a.add_handler(CommandHandler("playlists", self.cmd_playlists))
        a.add_handler(CommandHandler("devices", self.cmd_devices))
        a.add_handler(CommandHandler("register", self.cmd_register))
        a.add_handler(CommandHandler("me", self.cmd_me))
        a.add_handler(CommandHandler("wave", self.cmd_wave))
        a.add_handler(CommandHandler("mywave", self.cmd_mywave))
        a.add_handler(CommandHandler("radio", self.cmd_radio))
        a.add_handler(CommandHandler("radio_add", self.cmd_radio_add))
        a.add_handler(CommandHandler("radio_del", self.cmd_radio_del))
        a.add_handler(CommandHandler("find", self.cmd_find))
        a.add_handler(CommandHandler("delete", self.cmd_delete))
        a.add_handler(CommandHandler("queue", self.cmd_queue))
        a.add_handler(CommandHandler("import_status", self.cmd_import_status))
        a.add_handler(CommandHandler("import_cancel", self.cmd_import_cancel))
        a.add_handler(CommandHandler("status", self.cmd_status))
        a.add_handler(CommandHandler("users", self.cmd_users))
        a.add_handler(MessageHandler(filters.Document.ALL & filters.CaptionRegex(r"^/cookies"), self.cmd_cookies))
        a.add_handler(MessageHandler(filters.Document.FileExtension("csv") | filters.Document.FileExtension("txt")
                                     | filters.Document.FileExtension("tsv"), self.on_import_file))
        audio_ext = filters.Document.AUDIO
        for ext in ("mp3", "flac", "wav", "m4a", "aac", "ogg", "opus", "wma", "aiff", "aif", "alac", "webm"):
            audio_ext = audio_ext | filters.Document.FileExtension(ext)
        a.add_handler(MessageHandler(filters.AUDIO | audio_ext, self.on_audio_file))
        a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        a.add_handler(CallbackQueryHandler(self.on_import_choice, pattern=r"^imp:"))
        a.add_handler(CallbackQueryHandler(self.on_import_control, pattern=r"^imp[prxf]"))
        a.add_handler(CallbackQueryHandler(self.on_fav, pattern=r"^fav:"))
        a.add_handler(CallbackQueryHandler(self.on_auth, pattern=r"^auth:"))
        a.add_handler(CallbackQueryHandler(self.on_upload_choice, pattern=r"^upl:"))
        a.add_handler(CallbackQueryHandler(self.on_device, pattern=r"^dev:"))
        a.add_handler(CallbackQueryHandler(self.on_delete_choice, pattern=r"^del(ok|no)?:"))
        a.add_handler(CallbackQueryHandler(self.on_choice))
        a.add_error_handler(self.on_error)

    # ------------------------------------------------------------------ lifecycle
    async def _post_init(self, app: Application) -> None:
        await self.jf.wait_ready()
        await self.svc.start()
        await self.radio.start()
        self._bg.append(asyncio.create_task(self._periodic()))
        await self._setup_menu_button(app)
        try:  # имя бота — для ссылок-приглашений в общие плейлисты (t.me/<бот>?start=pl_…)
            self.webapp.bot_username = (await app.bot.get_me()).username or None
        except TelegramError as e:
            log.warning("getMe не удался — ссылки-приглашения будут только на Mini App: %s", e)
        with contextlib.suppress(TelegramError):
            await app.bot.set_my_commands([
                BotCommand("register", "Регистрация: /register инвайт логин пароль"),
                BotCommand("wave", "Моя Волна — скачать похожие треки"),
                BotCommand("mywave", "Плейлист и живой поток «Моя Волна»"),
                BotCommand("radio", "Интернет-радио"),
                BotCommand("radio_add", "Добавить станцию: Название | URL"),
                BotCommand("find", "Поиск в моей медиатеке"),
                BotCommand("playlists", "Мои плейлисты"),
                BotCommand("devices", "Устройства вне Telegram (вход и выход)"),
                BotCommand("queue", "Очередь загрузок"),
                BotCommand("import_status", "Прогресс импорта медиатеки из файла"),
                BotCommand("import_cancel", "Остановить импорт"),
                BotCommand("me", "Мой аккаунт"),
                BotCommand("help", "Справка"),
            ])
        log.info("бот запущен")

    async def _setup_menu_button(self, app: Application) -> None:
        """Кнопка «🎵 PiMusic» слева от поля ввода открывает Mini App. Telegram требует HTTPS-адрес,
        поэтому включаем её, только если задан STREAM_PUBLIC_URL=https://..."""
        base = CFG.stream_public_base
        try:
            if base.startswith("https://"):
                await app.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp("🎵 PiMusic", WebAppInfo(url=f"{base}/app/")))
                log.info("Mini App: %s/app/", base)
            else:
                await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except TelegramError as e:
            log.warning("не удалось настроить кнопку меню: %s", e)

    async def _post_shutdown(self, _: Application) -> None:
        for t in self._bg:
            t.cancel()
        await self.svc.stop()
        await self.radio.stop()
        await self.jf.close()
        await http.close()

    async def _periodic(self) -> None:
        """Раз в 6 часов пересобираем личные плейлисты «Моя Волна» по свежим лайкам и прослушиваниям."""
        await asyncio.sleep(120)
        while True:
            for u in self.db.all_users():
                try:
                    await radio.refresh_wave_playlist(self.jf, u, CFG.wave_playlist_size, self.db)
                except Exception as e:  # noqa: BLE001
                    log.warning("плейлист «Моя Волна» для %s не обновлён: %s", u.login, e)
            await asyncio.sleep(6 * 3600)

    def run(self) -> None:
        self.app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)

    # ------------------------------------------------------------------ helpers
    def _user(self, update: Update) -> User | None:
        return self.db.user_by_tg(update.effective_user.id) if update.effective_user else None

    def _is_admin(self, update: Update, user: User | None) -> bool:
        uid = update.effective_user.id if update.effective_user else 0
        return bool((user and user.is_admin) or uid in CFG.admin_telegram_ids)

    async def _need_user(self, update: Update) -> User | None:
        u = self._user(update)
        if not u and update.effective_message:
            await update.effective_message.reply_html(
                "Сначала зарегистрируйтесь: <code>/register ИНВАЙТ логин пароль</code>")
        return u

    @staticmethod
    def _jf_public() -> str:
        return CFG.jellyfin_public_base

    def _reactor(self, msg: Any) -> Reactor:
        """Реакции на сообщение пользователя (msg может быть None — тогда реакции просто не ставятся)."""
        if msg is None:
            return Reactor(None, None, None)
        return Reactor(self.app.bot, getattr(msg, "chat_id", None), getattr(msg, "message_id", None))

    def _make_view(self, job: Job, status: StatusMessage, reactor: Reactor, subtitle: str = "") -> Any:
        """Одиночный трек — статус + карточка; несколько — один живой отчёт."""
        if len(job.tracks) == 1:
            user = job.user

            async def is_favorite(track: Any) -> bool | None:
                return await self._track_is_favorite(user, track)

            return SingleTrackView(status, reactor, is_favorite=is_favorite)

        async def edit(text: str) -> None:
            await status.show(text, html_mode=True)

        return LiveReport(job.title or job.text, job.tracks, edit, subtitle=subtitle, reactor=reactor)

    async def _start_job(self, job: Job, status: StatusMessage, reactor: Reactor, subtitle: str = "") -> Any:
        job.view = self._make_view(job, status, reactor, subtitle)
        if isinstance(job.view, LiveReport):
            await job.view.start()
        return job.view

    # ------------------------------------------------------------------ commands
    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/start pl_<токен> — приглашение в общий плейлист (deep link), иначе справка."""
        args = getattr(ctx, "args", None) or []
        arg = args[0] if args else ""
        if arg.startswith("pl_"):
            await self._playlist_invite(update, arg[3:])
            return
        if arg.startswith("login_"):
            await self._login_request(update, arg[6:])
            return
        await self.cmd_help(update, ctx)

    # ------------------------------------------------------------------ свои файлы (аудио в чат)
    _AUDIO_MIME_EXT = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/flac": ".flac", "audio/x-flac": ".flac",
                       "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
                       "audio/aac": ".aac", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/x-ms-wma": ".wma",
                       "audio/aiff": ".aiff", "audio/x-aiff": ".aiff", "audio/webm": ".webm"}

    @classmethod
    def _audio_name(cls, att: Any) -> str:
        name = getattr(att, "file_name", None)
        if isinstance(name, str) and name.strip():
            return name
        ext = cls._AUDIO_MIME_EXT.get(str(getattr(att, "mime_type", "") or "").lower(), ".mp3")
        performer, title = getattr(att, "performer", None), getattr(att, "title", None)
        base = f"{performer} - {title}" if isinstance(performer, str) and isinstance(title, str) and title else "audio"
        return base + ext

    async def on_audio_file(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Аудиофайл (или несколько в одном альбоме сообщений) — в очередь загрузок."""
        u = await self._need_user(update)
        up = getattr(self.svc, "uploads", None)
        if not u or up is None:
            return
        msg = update.effective_message
        att = msg.audio or msg.document
        if att is None:
            return
        name = self._audio_name(att)
        group = getattr(msg, "media_group_id", None)
        reactor = self._reactor(msg)
        if (att.file_size or 0) > uploads_mod.BOT_MAX_BYTES:
            await msg.reply_html(
                f"Файл «{views.esc(name, 120)}» больше 20 МБ — Telegram не даёт ботам скачивать такие. "
                "Загрузите его через Mini App: «Загрузить файлы» (до "
                f"{CFG.upload_max_mb} МБ).", reply_markup=self._app_button("📤 Загрузить файлы", "#uploads"))
            return
        try:
            up.check_accept(u, att.file_size or 0)
            uid, path = up.begin(u, name, origin="bot", chat_id=msg.chat_id,
                                 batch=f"tg:{msg.chat_id}:{group or msg.message_id}")
        except uploads_mod.UploadError as e:
            await msg.reply_text(f"❌ «{name}»: {e.args[1]}")
            return
        await reactor.set(views.R_ACCEPTED)
        try:
            f = await att.get_file()
            await f.download_to_drive(custom_path=str(path))
            size = path.stat().st_size
        except Exception as e:  # noqa: BLE001
            log.warning("upload download failed: %s", e)
            up.abort(uid, "не удалось получить файл из Telegram")
            await msg.reply_text(f"❌ Не удалось получить «{name}» из Telegram, пришлите ещё раз.")
            return
        up.received(uid, size)
        if not group:
            await msg.reply_text(f"📥 Принял «{name}» — опознаю и добавлю в медиатеку.")

    async def on_upload_choice(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        u = self._user(update)
        up = getattr(self.svc, "uploads", None)
        parts = (q.data or "").split(":")
        if not u or up is None or len(parts) != 3 or parts[1] not in ("keep", "cancel") or not parts[2].isdigit():
            await q.answer()
            return
        uid = int(parts[2])
        ok = up.skip(u.login, uid) if parts[1] == "keep" else up.cancel(u.login, uid)
        if not ok:
            await q.answer("Уже обработано", show_alert=True)
            return
        await q.answer("Сохраняю" if parts[1] == "keep" else "Отменено")
        row = self.db.get_upload(uid)
        with contextlib.suppress(TelegramError):
            await q.edit_message_text(f"⏳ Сохраняю «{row['orig_name']}» как есть…" if parts[1] == "keep"
                                      else f"⏹ Загрузка «{row['orig_name']}» отменена.")

    async def _upload_reply(self, u: User, msg: Any) -> bool:
        """Ответ на вопрос «Исполнитель — Название». True — сообщение обработано здесь."""
        up = getattr(self.svc, "uploads", None)
        rt = getattr(msg, "reply_to_message", None)
        rid, chat = getattr(rt, "message_id", None), getattr(msg, "chat_id", None)
        if up is None or not isinstance(rid, int) or not isinstance(chat, int):
            return False
        row = self.db.upload_by_prompt(chat, rid)
        if not row or row["login"] != u.login:
            return False
        parts = [x.strip() for x in re.split(r"\s+[—–-]\s+", (msg.text or "").strip(), maxsplit=2)]
        if len(parts) < 2 or not all(parts[:2]):
            await msg.reply_text("Нужно в виде «Исполнитель — Название», например: Кино — Группа крови")
            return True
        if not up.provide_info(u.login, row["id"], parts[0], parts[1], parts[2] if len(parts) > 2 else ""):
            await msg.reply_text("Этот файл уже обработан.")
            return True
        await msg.reply_text(f"⏳ Ищу «{parts[0]} — {parts[1]}» и сохраняю…")
        return True

    # ------------------------------------------------------------------ вход на других устройствах
    async def _login_request(self, update: Update, nonce: str) -> None:
        """/start login_<nonce>: подтвердить вход в PWA/приложении. Нужно выбрать код, который виден на экране
        устройства (две кнопки — приманки): чужую ссылку, присланную «для входа», так просто не подтвердить."""
        msg = update.effective_message
        u = self._user(update)
        if not u:
            await msg.reply_html("Сначала зарегистрируйтесь: <code>/register ИНВАЙТ логин пароль</code>")
            return
        row = self.db.nonce_by_hash(auth.sha256(nonce)) if auth.NONCE_RE.match(nonce or "") else None
        if not row or row["status"] != "pending" or row["expires_at"] < time.time():
            await msg.reply_text("Ссылка для входа устарела или уже использована — начните вход заново.")
            return
        if not self.db.claim_nonce_opener(row["id"], update.effective_user.id):
            await msg.reply_text("Эту ссылку для входа уже открыл другой аккаунт Telegram — начните вход заново.")
            return
        device = html.escape(row["device"] or "Новое устройство")
        started = time.strftime("%H:%M", time.localtime(row["created_at"]))
        place = html.escape(row["client_ip"] or "неизвестно")
        codes = auth.code_choices(row["code"]) if row["code"] else []
        buttons = [InlineKeyboardButton(c, callback_data=f"auth:c:{row['id']}:{c}") for c in codes]
        if not buttons:  # старый вход без кода
            buttons = [InlineKeyboardButton("✅ Разрешить", callback_data=f"auth:ok:{row['id']}")]
        kb = InlineKeyboardMarkup([buttons, [InlineKeyboardButton("❌ Отклонить", callback_data=f"auth:no:{row['id']}")]])
        await msg.reply_html(
            f"🔐 <b>Войти в PiMusic на новом устройстве?</b>\n📱 {device} · "
            f"{html.escape(auth.platform_label(row['platform']))}\n🌐 IP: {place}\n🕒 Начат в {started}\n"
            f"👤 Аккаунт: <b>{html.escape(u.login)}</b>\n\n"
            + ("Нажмите код, который <b>виден на экране вашего устройства</b>. Разрешайте, только если этот код "
               "виден на экране вашего устройства. " if codes else "")
            + "Если вход начали не вы — нажмите «Отклонить».", reply_markup=kb)

    async def on_auth(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        u = self._user(update)
        parts = (q.data or "").split(":")
        valid = u and len(parts) in (3, 4) and parts[1] in ("ok", "no", "c") and parts[2].isdigit() \
            and (parts[1] != "c" or len(parts) == 4)
        if not valid:
            await q.answer("Сначала /register" if not u else "", show_alert=not u)
            return
        nid, tg = int(parts[2]), update.effective_user.id
        row = self.db.get_nonce(nid)
        if parts[1] == "c":
            # верный код — разрешить; неверный — сразу отклонить (угадывать нельзя)
            approve = bool(row and row["code"]) and hmac.compare_digest(parts[3].encode(), row["code"].encode())
        else:
            approve = parts[1] == "ok" and bool(row) and not row["code"]  # «Разрешить» без кода — только старым
        if not self.db.decide_nonce(nid, u.login, approve, tg_id=tg):
            await q.answer("Запрос устарел", show_alert=True)
            with contextlib.suppress(TelegramError):
                await q.edit_message_text("Ссылка для входа устарела — начните вход заново.")
            return
        wrong = parts[1] == "c" and not approve
        await q.answer("Разрешено" if approve else ("Код не совпал — вход отклонён" if wrong else "Отклонено"),
                       show_alert=wrong)
        with contextlib.suppress(TelegramError):
            await q.edit_message_text("✅ Вход разрешён — вернитесь в приложение." if approve else
                                      "❌ Код не совпал — вход отклонён. Начните вход заново." if wrong else
                                      "❌ Вход отклонён.")

    def _devices_view(self, u: User) -> tuple[str, InlineKeyboardMarkup | None]:
        rows = self.db.user_devices(u.login)
        if not rows:
            return ("📱 Устройств вне Telegram нет. Войти можно в приложении PiMusic или PWA кнопкой "
                    "«Войти через Telegram»."), None
        lines, btns = [], []
        for i, r in enumerate(rows, 1):
            seen = time.strftime("%d.%m.%Y %H:%M", time.localtime(r["last_seen"]))
            lines.append(f"{i}. {views.esc(r['name'] or 'Устройство', 60)} · "
                         f"{views.esc(auth.platform_label(r['platform']), 30)} — был {seen}")
            btns.append(InlineKeyboardButton(f"🗑 {i}", callback_data=f"dev:rv:{r['id']}"))
        text = (f"📱 <b>Устройства</b> ({len(rows)}):\n" + "\n".join(lines)
                + "\n\nНажмите 🗑 с номером, чтобы выйти на этом устройстве.")
        kb = [btns[i:i + 5] for i in range(0, len(btns), 5)]
        kb.append([InlineKeyboardButton("🚪 Выйти на всех устройствах", callback_data="dev:all:0")])
        return text, InlineKeyboardMarkup(kb)

    async def cmd_devices(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        u = await self._need_user(update)
        if not u:
            return
        text, kb = self._devices_view(u)
        await update.effective_message.reply_html(text, reply_markup=kb)

    async def on_device(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        u = self._user(update)
        parts = (q.data or "").split(":")
        if not u or len(parts) != 3 or parts[1] not in ("rv", "all") or not parts[2].isdigit():
            await q.answer()
            return
        if parts[1] == "all":
            n = self.db.revoke_all_devices(u.login)
            await q.answer(f"Выход выполнен на {n} устройствах" if n else "Устройств нет")
        else:
            ok = self.db.revoke_device(u.login, int(parts[2]))
            await q.answer("Устройство отключено" if ok else "Уже отключено")
        text, kb = self._devices_view(u)
        with contextlib.suppress(TelegramError):
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    @staticmethod
    def _app_button(text: str, fragment: str) -> InlineKeyboardMarkup | None:
        """Кнопка Mini App: Telegram принимает только https-адрес."""
        base = CFG.stream_public_base
        if not base.startswith("https://"):
            return None
        return InlineKeyboardMarkup([[InlineKeyboardButton(text, web_app=WebAppInfo(url=f"{base}/app/{fragment}"))]])

    async def _playlist_invite(self, update: Update, token: str) -> None:
        msg = update.effective_message
        pl = self.db.playlist_by_token(token[:64]) if token else None
        if not pl:
            await msg.reply_text("Приглашение недействительно: владелец выключил общий доступ или обновил ссылку.")
            return
        count, seconds = self.db.playlist_stats(pl["id"])
        u = self._user(update)
        role = self.db.playlist_role(pl["id"], u.login) if u else None
        text = (f"🎶 Плейлист <b>{html.escape(pl['name'])}</b>\n"
                f"👤 {html.escape(pl['owner'])} · {count} тр. · {_human_len(seconds)}\n")
        if pl["description"]:
            text += f"<i>{html.escape(pl['description'][:300])}</i>\n"
        if not u:
            text += "\nЧтобы присоединиться, сначала зарегистрируйтесь: <code>/register ИНВАЙТ логин пароль</code>"
            await msg.reply_html(text)
            return
        text += ("\nВы уже участник этого плейлиста." if role else
                 "\nОткройте плейлист и нажмите «Присоединиться» — сможете слушать, добавлять и удалять треки.")
        kb = self._app_button("Открыть плейлист", f"#join={token}")
        if kb is None:
            text += f"\n\nMini App: {html.escape(CFG.stream_public_base)}/app/#join={html.escape(token)}"
        await msg.reply_html(text, reply_markup=kb)

    async def cmd_playlists(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        u = await self._need_user(update)
        if not u:
            return
        lines = []
        for pl in self.db.user_playlists(u.login):
            count, _s = self.db.playlist_stats(pl["id"])
            if pl["owner"] == u.login:
                tags = "общий" if pl["share_token"] else ""
            else:
                tags = f"участник, владелец {pl['owner']}"
            lines.append(f"• {views.esc(pl['name'], 80)} — {count} тр."
                         + (f" <i>({views.esc(tags, 80)})</i>" if tags else ""))
        if lines:
            shown, more = fit_lines(lines, range(len(lines)), 3500)
            text = f"🎶 <b>Мои плейлисты</b> ({len(lines)}):\n" + blockquote(shown, more)
        else:
            text = "🎶 Плейлистов пока нет — создайте первый в Mini App."
        await update.effective_message.reply_html(text, reply_markup=self._app_button("Открыть плейлисты",
                                                                                       "#playlists"))

    async def cmd_help(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        u = self._user(update)
        text = HELP + (ADMIN_HELP if self._is_admin(update, u) else "")
        if u:
            text += f"\nВы вошли как <b>{html.escape(u.login)}</b>. Веб-плеер: {self._jf_public()}"
        await update.effective_message.reply_html(text)

    async def cmd_register(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        args = ctx.args or []
        # сообщение с паролем сразу удаляем из чата
        with contextlib.suppress(TelegramError):
            await msg.delete()
        chat = update.effective_chat
        if len(args) != 3:
            await chat.send_message("Формат: /register ИНВАЙТ логин пароль\n(сообщение с паролем я удалил)")
            return
        invite, login, password = args
        wait = await chat.send_message("⏳ Создаю аккаунт и личную медиатеку…")
        try:
            ok, res = await self.svc.register(update.effective_user.id, invite, login, password)
        except Exception as e:  # noqa: BLE001
            log.exception("register failed")
            ok, res = False, f"Ошибка Jellyfin: {e}"
        if not ok:
            await wait.edit_text("❌ " + res)
            return
        user = self.db.user_by_login(res)
        await wait.edit_text(
            f"🎉 Готово! Аккаунт <b>{html.escape(res)}</b> создан.\n\n"
            f"🌐 Веб-плеер: {self._jf_public()}\n"
            f"📱 Приложения: Finamp, Symfonium, Jellyfin — сервер <code>{self._jf_public()}</code>\n"
            f"👤 Логин: <code>{html.escape(res)}</code>, пароль — тот, что вы указали.\n\n"
            f"Ваша медиатека видна только вам. Пришлите название песни или ссылку — и я её скачаю.\n"
            f"📻 Живой поток «Моя Волна»: {html.escape(radio.wave_url(user)) if user else ''}",
            parse_mode=ParseMode.HTML)

    async def cmd_me(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        u = await self._need_user(update)
        if not u:
            return
        n = self.db.user_track_count(u.login)
        await update.effective_message.reply_html(
            f"👤 <b>{html.escape(u.login)}</b>{' (админ)' if u.is_admin else ''}\n"
            f"🎵 Треков в медиатеке: {n}\n"
            f"🌐 Jellyfin: {self._jf_public()}\n"
            f"📻 Живой поток «Моя Волна»: {html.escape(radio.wave_url(u))}\n"
            f"Сейчас в эфире: {html.escape(self.radio.now_playing(u.login) or '—')}")

    async def cmd_queue(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Свои загрузки (админ видит все): что качают другие — их личное дело."""
        u = self._user(update)
        mine = (lambda v: True) if self._is_admin(update, u) else \
            (lambda v: bool(u) and v.startswith(f"{u.login}: "))
        lines = [f"• {html.escape(v)}" for v in self.svc.active.values() if mine(v)]
        shown, more = fit_lines(lines, range(len(lines)), 3500)
        active = blockquote(shown, more) if lines else "—"
        await update.effective_message.reply_html(
            f"⏬ <b>Сейчас качается</b> ({len(lines)}):\n{active}\n\nВ очереди: {self.svc.queue.qsize()}")

    async def cmd_wave(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        u = await self._need_user(update)
        if not u:
            return
        size = CFG.wave_size
        if ctx.args and ctx.args[0].isdigit():
            size = max(1, min(50, int(ctx.args[0])))
        msg = update.effective_message
        reactor = self._reactor(msg)
        await reactor.set(views.R_ACCEPTED)
        status = await msg.reply_text("🌊 Слушаю ваши лайки и ищу похожее (Last.fm / Deezer)…")
        reqs, seeds = await wave.build(self.db, self.jf, u, size)
        if not seeds:
            await status.edit_text("Медиатека пока пуста — добавьте пару треков и поставьте им ❤️ в Jellyfin.")
            return
        if not reqs:
            await status.edit_text("Не нашёл новых похожих треков — всё похожее уже у вас 🙂")
            return
        notify = Notifier(msg)
        notify.status = status
        job = Job(user=u, text="/wave", notify=notify, origin="wave", tracks=reqs, title="🌊 Моя Волна")
        subtitle = "На основе: " + "; ".join(seeds[:5]) + ("…" if len(seeds) > 5 else "")
        st = StatusMessage(self.app.bot, msg.chat_id, status.message_id, reply_to=msg.message_id)
        await self._start_job(job, st, reactor, subtitle)

        async def go() -> None:
            await self.svc.run_job(job, announce=False)
            # запоминаем, что уже предлагали
            owned = self.db.user_track_keys(u.login)
            from .util import match_key
            for r in reqs:
                k = match_key(r.artist, r.title)
                self.db.mark_wave(u.login, k, k in owned)
            try:
                await self.svc.refresh_now()  # новые треки должны попасть в Jellyfin до сборки плейлиста
                await radio.refresh_wave_playlist(self.jf, u, CFG.wave_playlist_size, self.db)
            except Exception as e:  # noqa: BLE001
                log.warning("плейлист «Моя Волна» для %s после /wave не обновлён: %s", u.login, e)

        self.app.create_task(go())

    async def cmd_mywave(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        u = await self._need_user(update)
        if not u:
            return
        n = await radio.refresh_wave_playlist(self.jf, u, CFG.wave_playlist_size, self.db)
        await update.effective_message.reply_html(
            f"🌊 Плейлист «Моя Волна» обновлён: {n} треков (лайки, любимые жанры и артисты).\n"
            f"Он есть в Jellyfin, Finamp и Symfonium в разделе «Плейлисты».\n\n"
            f"📻 Бесконечный живой поток (VLC, Symfonium, любой плеер):\n<code>{html.escape(radio.wave_url(u))}</code>")

    async def cmd_radio(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        rows = self.db.list_radio(None)
        lines = [f"<code>{r['id']}</code> · {html.escape(r['name'])}" for r in rows if r["owner"] is None]
        shown, more = fit_lines(lines, range(len(lines)), 3500)
        await update.effective_message.reply_html(
            f"📻 <b>Интернет-радио</b> ({len(lines)}) — в Jellyfin: Live TV → Каналы\n"
            + (blockquote(shown, more) if lines else "—") +
            "\n\nДобавить: <code>/radio_add Название | URL</code>")

    async def cmd_radio_add(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        u = await self._need_user(update)
        if not u:
            return
        text = update.effective_message.text.split(maxsplit=1)
        if len(text) < 2 or "|" not in text[1]:
            await update.effective_message.reply_html("Формат: <code>/radio_add Название | http://поток</code>")
            return
        name, url = (x.strip() for x in text[1].split("|", 1))
        # текстовая проверка + DNS: имя, указывающее на адрес локальной сети или шлюза, не принимаем (SSRF через ffmpeg)
        if not radio.valid_station(name, url) or not await util.is_public_url_resolved(url):
            await update.effective_message.reply_text(
                "Нужны название (до 100 символов) и публичная http(s)-ссылка на поток.")
            return
        p = radio.station_path(None, name)
        radio.write_strm(p, url)
        self.db.add_radio(None, name[:100], url, str(p))
        try:
            await radio.sync_tuner(self.db, self.jf)
            note = "Появится в Jellyfin → Live TV через минуту."
        except Exception as e:  # noqa: BLE001
            note = f"Но Jellyfin не принял список станций: {e}"
        await update.effective_message.reply_text(f"📻 Станция «{name}» добавлена. {note}")

    async def cmd_radio_del(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        u = self._user(update)
        if not self._is_admin(update, u):
            await update.effective_message.reply_text("Только для администратора.")
            return
        if not ctx.args or not ctx.args[0].isdigit():
            await update.effective_message.reply_text("Формат: /radio_del id (см. /radio)")
            return
        r = self.db.get_radio(int(ctx.args[0]))
        if not r:
            await update.effective_message.reply_text("Нет такой станции.")
            return
        from pathlib import Path
        Path(r["path"]).unlink(missing_ok=True)
        self.db.delete_radio(r["id"])
        try:
            await radio.sync_tuner(self.db, self.jf)
        except Exception as e:  # noqa: BLE001
            log.warning("список радиостанций в Jellyfin не обновлён: %s", e)
        await update.effective_message.reply_text(f"Удалено: {r['name']}")

    async def cmd_find(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        u = await self._need_user(update)
        if not u:
            return
        needle = " ".join(ctx.args or [])
        rows = self.db.find_user_tracks(u.login, needle, 20)
        if not rows:
            await update.effective_message.reply_text("Ничего не найдено.")
            return
        lines = [f"{i + 1}. {views.esc(t.artist, 60)} — {views.esc(t.title, 80)}"
                 + (f" <i>({views.esc(t.album, 50)})</i>" if t.album else "") for i, (t, _) in enumerate(rows)]
        lines, more = fit_lines(lines, range(len(lines)), 3300)
        btns = [InlineKeyboardButton(f"🗑 {i + 1}", callback_data=f"del:{t.id}") for i, (t, _) in enumerate(rows)]
        kb = InlineKeyboardMarkup([btns[i:i + 5] for i in range(0, len(btns), 5)])
        head = f"🔎 <b>Нашлось в медиатеке: {len(rows)}</b>" + (f" по «{views.esc(needle, 100)}»" if needle else "")
        await update.effective_message.reply_html(
            head + "\n" + blockquote(lines, more) + "\nНажмите 🗑 с номером, чтобы удалить трек из своей медиатеки.\n"
                   "Удалять можно и прямо в Jellyfin / Finamp / Symfonium: «⋯» → «Удалить».", reply_markup=kb)

    async def on_delete_choice(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        u = self._user(update)
        if not u:
            await q.answer("Сначала /register", show_alert=True)
            return
        action, _, arg = (q.data or "").partition(":")
        if action == "delno":
            await q.answer("Оставил")
            with contextlib.suppress(TelegramError):
                await q.message.delete()
            return
        if not arg.isdigit():
            await q.answer()
            return
        tid = int(arg)
        track = self.db.get_track(tid)
        if not track or not self.db.user_has_track(u.login, tid):
            await q.answer("Этого трека уже нет в вашей медиатеке.", show_alert=True)
            return
        if action == "del":
            await q.answer()
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Да, удалить", callback_data=f"delok:{tid}"),
                                        InlineKeyboardButton("Отмена", callback_data="delno:")]])
            await q.message.reply_html(f"Удалить из вашей медиатеки <b>{html.escape(track.artist)} — "
                                       f"{html.escape(track.title)}</b>?", reply_markup=kb)
        elif action == "delok":
            text = await self.svc.delete_track(u, tid)
            await q.answer("Удалено")
            with contextlib.suppress(TelegramError):
                await q.edit_message_text(text)

    async def cmd_delete(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        u = await self._need_user(update)
        if not u:
            return
        if not ctx.args or not ctx.args[0].isdigit():
            await update.effective_message.reply_text("Формат: /delete id (id смотрите через /find)")
            return
        await update.effective_message.reply_text(await self.svc.delete_track(u, int(ctx.args[0])))

    async def cmd_status(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update, self._user(update)):
            await update.effective_message.reply_text("Только для администратора.")
            return
        total, used, free = library.disk_usage()
        st = self.db.stats()
        slsk = "🟢 онлайн" if await downloaders.SLSKD.online() else "🔴 нет соединения"
        await update.effective_message.reply_html(
            f"💾 Диск: занято {human_size(used)} из {human_size(total)}, свободно {human_size(free)}\n"
            f"👥 Пользователей: {st['users']}\n🎵 Уникальных треков в кэше: {st['tracks']}\n"
            f"🔗 Ссылок в медиатеках: {st['links']} (экономия ≈ {max(0, st['links'] - st['tracks'])} файлов)\n"
            f"🔎 Soulseek (slskd): {slsk}\n⏬ Очередь: {self.svc.queue.qsize()}\n"
            f"🌐 Jellyfin: {self._jf_public()} · slskd: http://{CFG.public_host}:5030")

    async def cmd_users(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update, self._user(update)):
            await update.effective_message.reply_text("Только для администратора.")
            return
        lines = [f"• {html.escape(u.login)} — {self.db.user_track_count(u.login)} треков" for u in self.db.all_users()]
        if not lines:
            await update.effective_message.reply_html("Пока никого.")
            return
        shown, more = fit_lines(lines, range(len(lines)), 3800)
        await update.effective_message.reply_html(f"👥 <b>Пользователи: {len(lines)}</b>\n" + blockquote(shown, more))

    async def cmd_cookies(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update, self._user(update)):
            await update.effective_message.reply_text("Только для администратора.")
            return
        doc = update.effective_message.document
        f = await doc.get_file()
        dst = CFG.state_dir / "youtube_cookies.txt"
        await f.download_to_drive(str(dst))
        await update.effective_message.reply_text("🍪 cookies.txt сохранён — yt-dlp и spotDL будут его использовать.")

    async def on_text(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        u = await self._need_user(update)
        if not u:
            return
        msg = update.effective_message
        text = (msg.text or "").strip()
        if len(text) < 2:
            return
        if await self._upload_reply(u, msg):  # ответ на «Не смог опознать файл…»
            return
        await self._reactor(msg).set(views.R_ACCEPTED)
        # Одна строка свободного текста — сначала показываем варианты на выбор.
        # Ссылки (Spotify/YouTube/SoundCloud) и списки из нескольких строк качаем сразу.
        if "\n" not in text and sources.classify(text) == "text":
            self.app.create_task(self._offer_choices(u, msg, text))
            return
        notify = Notifier(msg)
        await notify("⏳ Принял, разбираю запрос…")
        self.app.create_task(self._submit(u, text, notify))

    # ------------------------------------------------------------------ выбор из найденного
    async def _offer_choices(self, u: User, msg: Message, query: str) -> None:
        status = await msg.reply_text(f"🔎 Ищу «{query}»…")
        try:
            res = await search.search(query, u.tg_id or 0)
        except Exception as e:  # noqa: BLE001
            log.warning("search failed: %s", e)
            res = None
        if not res or (not res.tracks and not res.albums):
            with contextlib.suppress(TelegramError):
                await status.edit_text("В каталогах не нашёл — ищу напрямую в Soulseek и YouTube…")
            notify = Notifier(msg)
            notify.status = status  # дальше это же сообщение станет статусом загрузки
            await self._submit(u, query, notify)
            return
        token = search.store(res)
        text, kb = self._render_main(res, token)
        with contextlib.suppress(TelegramError):
            await status.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    @staticmethod
    def _num_rows(prefix: str, token: str, n: int, per_row: int = 5) -> list[list[InlineKeyboardButton]]:
        btns = [InlineKeyboardButton(str(i + 1), callback_data=f"{prefix}:{token}:{i}") for i in range(n)]
        return [btns[i:i + per_row] for i in range(0, len(btns), per_row)]

    def _render_main(self, res: search.Result, token: str) -> tuple[str, InlineKeyboardMarkup]:
        lines = []
        if res.artist:
            lines.append(f"🎤 <b>{html.escape(res.artist)}</b>\n\nПопулярные треки:")
            for i, t in enumerate(res.tracks):
                d = search.fmt_duration(t.duration)
                lines.append(f"{i + 1}. {html.escape(t.title)}" + (f" <i>{d}</i>" if d else ""))
        else:
            lines.append(f"🔎 Нашёл по запросу «{html.escape(res.query)}»:\n")
            for i, t in enumerate(res.tracks):
                d = search.fmt_duration(t.duration)
                album = f" · <i>{html.escape(t.album)}</i>" if t.album else ""
                lines.append(f"{i + 1}. {html.escape(t.artist)} — <b>{html.escape(t.title)}</b>{album}"
                             + (f" {d}" if d else ""))
        if res.albums:
            lines.append("\n💿 Альбомы — нажмите, чтобы открыть:")
        lines.append("\nВыберите номер трека:" if res.tracks else "")
        rows = self._num_rows("t", token, len(res.tracks))
        if len(res.tracks) > 1:
            rows.append([InlineKeyboardButton(f"⬇️ Скачать все {len(res.tracks)}", callback_data=f"T:{token}")])
        for j, al in enumerate(res.albums):
            label = f"💿 {al.title}" + (f" ({al.year})" if al.year else "") + (" · EP" if al.kind == "ep" else "")
            rows.append([InlineKeyboardButton(label[:60], callback_data=f"a:{token}:{j}")])
        rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data=f"x:{token}")])
        return "\n".join(lines).strip(), InlineKeyboardMarkup(rows)

    def _render_album(self, res: search.Result, token: str, j: int,
                      tracks: list) -> tuple[str, InlineKeyboardMarkup]:
        al = res.albums[j]
        lines = [f"💿 <b>{html.escape(al.title)}</b>" + (f" ({al.year})" if al.year else "")
                 + f"\n🎤 {html.escape(al.artist)}\n"]
        for i, t in enumerate(tracks):
            d = search.fmt_duration(t.duration)
            lines.append(f"{i + 1}. {html.escape(t.title)}" + (f" <i>{d}</i>" if d else ""))
        rows = [[InlineKeyboardButton(f"⬇️ Весь альбом ({len(tracks)})", callback_data=f"A:{token}:{j}")]]
        rows += self._num_rows(f"at{j}", token, len(tracks), per_row=6)
        rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"b:{token}")])
        return "\n".join(lines)[:4000], InlineKeyboardMarkup(rows)

    async def on_choice(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        parts = (q.data or "").split(":")
        if len(parts) < 2:
            await q.answer()
            return
        action, token = parts[0], parts[1]
        res = search.get(token)
        u = self._user(update)
        if not res or not u:
            await q.answer("Результаты поиска устарели — отправьте запрос ещё раз.", show_alert=True)
            return
        if res.owner and update.effective_user and res.owner != update.effective_user.id:
            await q.answer("Это не ваш поиск 🙂", show_alert=True)
            return
        try:
            if action == "x":
                await q.answer()
                with contextlib.suppress(TelegramError):
                    await q.message.delete()
            elif action == "b":
                await q.answer()
                text, kb = self._render_main(res, token)
                await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            elif action == "a":
                j = int(parts[2])
                await q.answer("Открываю альбом…")
                tracks = await search.album_tracks(res, j)
                if not tracks:
                    await q.answer("Не удалось получить треклист", show_alert=True)
                    return
                text, kb = self._render_album(res, token, j, tracks)
                await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            elif action == "t":
                req = res.tracks[int(parts[2])]
                await self._enqueue(q, u, [req], req.label)
            elif action == "T":
                await self._enqueue(q, u, list(res.tracks), res.artist or res.query)
            elif action == "A":
                j = int(parts[2])
                tracks = await search.album_tracks(res, j)
                await self._enqueue(q, u, tracks, f"{res.albums[j].artist} — {res.albums[j].title}")
            elif action.startswith("at"):
                j = int(action[2:])
                tracks = await search.album_tracks(res, j)
                req = tracks[int(parts[2])]
                await self._enqueue(q, u, [req], req.label)
            else:
                await q.answer()
        except (IndexError, ValueError):
            await q.answer("Неверная кнопка", show_alert=True)
        except BadRequest as e:  # «message is not modified» и т.п.
            log.debug("callback edit: %s", e)

    async def _enqueue(self, q, u: User, reqs: list, title: str) -> None:
        reqs = [dataclasses.replace(r) for r in reqs]  # копии: воркер дополняет длительность и т.п.
        await q.answer(f"⏬ В очередь: {title}"[:190])
        if not reqs:
            with contextlib.suppress(TelegramError):
                await q.message.reply_text("Не удалось получить список треков — попробуйте ещё раз.")
            return
        # реакции — на исходный запрос пользователя (сообщение с вариантами отвечает на него)
        reactor = self._reactor(getattr(q.message, "reply_to_message", None))
        await reactor.set(views.R_ACCEPTED)
        notify = Notifier(q.message)
        job = Job(user=u, text=title, notify=notify, tracks=reqs, title=title)
        status = StatusMessage(self.app.bot, q.message.chat_id, reply_to=q.message.message_id)
        if len(reqs) == 1:
            await status.show(f"⏳ {reqs[0].label}: в очереди…")
        await self._start_job(job, status, reactor)
        self.app.create_task(self.svc.run_job(job, announce=False))

    # ------------------------------------------------------------------ импорт медиатеки из файла
    async def on_import_file(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        u = await self._need_user(update)
        if not u:
            return
        msg = update.effective_message
        doc = msg.document
        await self._reactor(msg).set(views.R_ACCEPTED)
        if doc.file_size and doc.file_size > 15 * 1024 * 1024:
            await msg.reply_text("Файл больше 15 МБ — разбейте его на части.")
            return
        status = await msg.reply_text("📄 Читаю файл…")
        try:
            f = await doc.get_file()
            data = bytes(await f.download_as_bytearray())
            items, fmt = await importer.parse_file(data, doc.file_name or "import.txt")
        except ValueError as e:
            await status.edit_text(f"❌ Не понял формат файла: {e}\n\nПодходят CSV из Tune My Music / Exportify "
                                   "(колонки «Track name», «Artist name») или TXT со строками «Артист - Песня».")
            return
        except Exception as e:  # noqa: BLE001
            log.exception("import parse failed")
            await status.edit_text(f"❌ Не удалось прочитать файл: {e}")
            return
        if not items:
            await status.edit_text("В файле не нашлось ни одного трека.")
            return
        cached = await asyncio.to_thread(
            lambda: sum(1 for it in items if library.lookup_cached(self.db, it["artist"], it["title"], isrc=it["isrc"])))
        same = importer.same_recording_count(items)
        iid = await asyncio.to_thread(self.svc.draft_import, u, msg.chat_id, doc.file_name or "import", items,
                                      status.message_id)
        plines = [f"• {views.esc(it['artist'], 60)} — {views.esc(it['title'], 80)}" for it in items[:60]]
        plines, pmore = fit_lines(plines, range(len(plines)), 1500)
        preview = blockquote(plines, pmore + max(0, len(items) - 60))
        same_note = ""
        if same:
            by_isrc: dict[str, list[dict]] = {}
            for it in items:
                if it.get("isrc"):
                    by_isrc.setdefault(it["isrc"], []).append(it)
            ex = [g for g in by_isrc.values() if len(g) > 1][:2]
            examples = "; ".join(
                f"{views.esc(g[0]['artist'], 60)} — {views.esc(g[0]['title'], 80)} "
                f"({', '.join('«' + views.esc(x['album'] or '?', 60) + '»' for x in g[:4])})" for g in ex)
            same_note = (f"🔁 {same} строк — это те же записи, что уже есть в списке, но из другого издания "
                         f"(альбом / Deluxe / сингл, одинаковый ISRC = один и тот же звук), например: {examples}. "
                         f"Скачаются один раз и в медиатеке будут по одному разу.\n")
        remaining = len(items) - cached - same
        # замер на одноплатнике (ARM, 4 ядра): ~40 с на трек на воркер (поиск в Soulseek ~3 с + загрузка 10–40 с + теги/тексты)
        low = remaining * 40 / max(1, CFG.import_workers) / 3600
        eta = "меньше часа" if low < 1 else f"~{round(low)}–{round(low * 1.6) + 1} ч"
        text = (f"📄 <b>{html.escape(doc.file_name or 'файл')}</b> ({fmt})\n"
                f"🎵 Строк в файле: <b>{len(items)}</b>, к скачиванию: <b>{remaining}</b>\n"
                + (f"♻️ Уже есть в общем кэше: {cached} — добавятся мгновенно\n" if cached else "")
                + same_note
                + f"\n{preview}\n"
                f"⏱ Ориентировочно: {eta} ({CFG.import_workers} загрузки параллельно). "
                f"Треки появляются в медиатеке сразу по мере скачивания, а не в конце. Импорт идёт в фоне, "
                f"ваши обычные запросы и запросы друзей обслуживаются отдельно и не ждут, перезапуск сервера "
                f"импорт не прерывает.")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Начать импорт", callback_data=f"imp:{iid}:go"),
                                    InlineKeyboardButton("✖️ Отмена", callback_data=f"imp:{iid}:no")]])
        await status.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    async def on_import_choice(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        _, token, action = (q.data.split(":") + ["", ""])[:3]
        u = self._user(update)
        imp = self.db.get_import(int(token)) if token.isdigit() else None
        if not imp or not u:
            await q.answer("Устарело — отправьте файл ещё раз.", show_alert=True)
            return
        if imp["login"] != u.login:
            await q.answer("Это не ваш файл 🙂", show_alert=True)
            return
        iid = imp["id"]
        if imp["state"] != "draft":
            await q.answer("Этот импорт уже запущен — прогресс в /import_status." if imp["state"] != "cancelled"
                           else "Этот импорт отменён.", show_alert=True)
            return
        if action != "go":
            self.db.delete_import_draft(iid)
            await q.answer("Отменено")
            await q.edit_message_text("Импорт отменён.")
            return
        # id сообщения отдаём сразу: очередь не должна успеть создать второе сообщение прогресса
        if not self.svc.confirm_import(iid, status_msg=q.message.message_id):
            await q.answer("Этот импорт уже запущен.", show_alert=True)
            return
        await q.answer("Импорт запущен")
        await q.edit_message_text(self.svc.import_status_text(iid) + "\n\nПрогресс буду обновлять в этом сообщении.")

    async def cmd_import_status(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        u = await self._need_user(update)
        if not u:
            return
        running = self.db.running_imports(u.login)
        if not running:
            await update.effective_message.reply_text("Активных импортов нет. Отправьте боту CSV или TXT со списком "
                                                      "треков (например, экспорт из Tune My Music).")
            return
        for imp in running:
            await update.effective_message.reply_text(self.svc.import_status_text(imp["id"]))

    async def cmd_import_cancel(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        u = await self._need_user(update)
        if not u:
            return
        n = self.svc.cancel_imports(u.login)
        await update.effective_message.reply_text(
            f"Остановлено импортов: {n}. Уже скачанные треки остаются в медиатеке." if n else "Активных импортов нет.")

    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        err = ctx.error
        if isinstance(err, NetworkError):  # обрывы связи с Telegram — PTB сам переподключится
            log.warning("Telegram: сетевая ошибка: %s", err)
            return
        log.error("Ошибка обработчика: %s", err, exc_info=err)

    async def _submit(self, u: User, text: str, notify: Notifier) -> None:
        """Разобрать запрос (ссылка, список строк, свободный текст) и поставить задание с живым отображением.
        Повторяет Service.submit, но с view: одиночный трек — статус и карточка, несколько — живой отчёт."""
        msg = getattr(notify, "msg", None)
        reactor = self._reactor(msg)
        try:
            resolved = await sources.resolve(text)
            if resolved.error:
                await notify("⚠️ " + resolved.error)
                await reactor.set(views.R_FAIL)
                return
            if not resolved.tracks:
                await notify("Ничего не нашёл по этой ссылке.")
                await reactor.set(views.R_FAIL)
                return
            tracks = resolved.tracks
            title = resolved.title or (tracks[0].label if len(tracks) == 1 else f"{len(tracks)} треков")
            job = Job(user=u, text=text, notify=notify, tracks=tracks, title=title)
            if getattr(resolved, "note", ""):
                await notify(resolved.note)  # например: Spotify отдал только первые 100 треков
            link_playlist = getattr(self.svc, "link_playlist", None)
            if link_playlist is not None:
                link_playlist(job, resolved)  # ссылка на плейлист — плейлист бота в том же порядке
            subtitle = playlist_line(job.playlist_name) if isinstance(job.playlist_id, int) else ""
            if msg is not None:
                st_msg = getattr(notify, "status", None)
                status = StatusMessage(self.app.bot, msg.chat_id, getattr(st_msg, "message_id", None),
                                       reply_to=msg.message_id)
                await self._start_job(job, status, reactor, subtitle)
            await self.svc.run_job(job, announce=job.view is None and len(tracks) > 1)
        except Exception as e:  # noqa: BLE001
            log.exception("submit failed")
            await notify(f"❌ Ошибка: {e}")
            await reactor.set(views.R_FAIL)

    # ------------------------------------------------------------------ карточка трека: ❤️ избранное
    @staticmethod
    def _same_path(a: str | None, b: str | None) -> bool:
        if not a or not b:
            return False
        if a == b or os.path.normpath(a) == os.path.normpath(b) or Path(a).as_posix() == Path(b).as_posix():
            return True
        # у бота и Jellyfin медиатека может быть смонтирована по-разному — сравниваем часть после /media/
        ra, rb = PiMusicBot._media_rel(a), PiMusicBot._media_rel(b)
        return ra is not None and ra == rb

    @staticmethod
    def _media_rel(p: str) -> str | None:
        posix = p.replace("\\", "/")
        i = posix.find("/media/")
        return posix[i + len("/media/"):] if i >= 0 else None

    async def _jf_find_item(self, u: User, track: Any, link: str) -> dict | None:
        """Ищем трек в Jellyfin по названию (а не выгружаем всю медиатеку) и сверяем путь."""
        data = await self.jf.request("GET", "/Items", params={
            "userId": u.jf_user_id, "IncludeItemTypes": "Audio", "Recursive": True,
            "searchTerm": track.title, "Fields": "Path", "EnableUserData": True, "Limit": 50})
        items = (data or {}).get("Items") or []
        return next((it for it in items if self._same_path(it.get("Path"), link)), None)

    async def _track_is_favorite(self, u: User, track: Any) -> bool | None:
        link = self.db.user_has_track(u.login, track.id)
        if not link:
            return None
        item = await self._jf_find_item(u, track, link)
        return bool((item.get("UserData") or {}).get("IsFavorite")) if item else None

    async def on_fav(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        u = self._user(update)
        if not u:
            await q.answer("Сначала /register", show_alert=True)
            return
        arg = (q.data or "").partition(":")[2]
        if not arg.isdigit():
            await q.answer()
            return
        tid = int(arg)
        link = self.db.user_has_track(u.login, tid)
        track = self.db.get_track(tid)
        if not link or not track:
            await q.answer("Этого трека уже нет в вашей медиатеке.", show_alert=True)
            return
        try:
            item = await self._jf_find_item(u, track, link)
        except Exception as e:  # noqa: BLE001
            log.warning("favorite: jellyfin items failed: %s", e)
            await q.answer("Jellyfin сейчас недоступен — попробуйте позже.", show_alert=True)
            return
        if not item:
            await q.answer("Jellyfin ещё индексирует трек — попробуйте через минуту", show_alert=True)
            return
        was_fav = bool((item.get("UserData") or {}).get("IsFavorite"))
        try:
            await self.jf.request("DELETE" if was_fav else "POST", f"/UserFavoriteItems/{item['Id']}",
                                  params={"userId": u.jf_user_id})
        except Exception as e:  # noqa: BLE001
            log.warning("favorite toggle failed: %s", e)
            await q.answer("Не получилось — попробуйте ещё раз.", show_alert=True)
            return
        await q.answer("💔 Убрал из избранного" if was_fav else "❤️ Добавил в избранное")
        with contextlib.suppress(TelegramError):
            await q.edit_message_reply_markup(reply_markup=views.card_markup(tid, track.title, fav=not was_fav))

    # ------------------------------------------------------------------ кнопки прогресса импорта
    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    async def on_import_control(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        action, _sep, arg = (q.data or "").partition(":")
        u = self._user(update)
        if not u:
            await q.answer("Сначала /register", show_alert=True)
            return
        if not arg.isdigit():
            await q.answer()
            return
        iid = int(arg)
        imp = self.db.get_import(iid)
        if not imp or imp["login"] != u.login:
            await q.answer("Это не ваш импорт 🙂", show_alert=True)
            return
        svc = self.svc

        async def refresh() -> None:
            try:
                await self._maybe_await(svc.refresh_import_message(iid))
            except Exception as e:  # noqa: BLE001
                log.debug("refresh import message failed: %s", e)

        if action == "impp":
            ok = await self._maybe_await(svc.pause_import(iid, u.login))
            await q.answer("⏸ Импорт на паузе" if ok else "Импорт уже не идёт")
            await refresh()
        elif action == "impr":
            ok = await self._maybe_await(svc.resume_import(iid, u.login))
            await q.answer("▶️ Продолжаю импорт" if ok else "Импорт уже завершён")
            await refresh()
        elif action == "impx":
            await q.answer("Точно остановить?")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ Да, остановить", callback_data=f"impx!:{iid}"),
                                        InlineKeyboardButton("Продолжить", callback_data=f"impx-:{iid}")]])
            with contextlib.suppress(TelegramError):
                await q.message.reply_text(
                    f"Остановить импорт «{imp['filename']}»? Уже скачанные треки останутся в медиатеке.",
                    reply_markup=kb)
        elif action == "impx!":
            ok = await self._maybe_await(svc.stop_import(iid, u.login))
            await q.answer("⏹ Импорт остановлен" if ok else "Импорт уже не идёт")
            with contextlib.suppress(TelegramError):
                await q.edit_message_text("⏹ Импорт остановлен. Уже скачанные треки остались в медиатеке."
                                          if ok else "Импорт уже не идёт.")
            await refresh()
        elif action == "impx-":
            await q.answer("Продолжаю")
            with contextlib.suppress(TelegramError):
                await q.message.delete()
        elif action == "impf":
            data = await self._maybe_await(svc.failed_csv(iid, u.login))
            if not data:
                await q.answer("Пока всё нашлось 🙂")
                return
            await q.answer("Отправляю список")
            with contextlib.suppress(TelegramError):
                await q.message.reply_document(document=data, filename="not_found.csv",
                                               caption="Треки, которые пока не нашлись. Файл можно прислать боту "
                                                       "ещё раз позже — уже скачанное пропустится мгновенно.")
        else:
            await q.answer()
