"""Russian Telegram interface. All writes share a lock with the scheduler."""

from __future__ import annotations

import asyncio
from datetime import datetime
from functools import wraps
from html import escape
from io import BytesIO
import json
import logging
import re
import secrets
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import (
    BotCommand, BotCommandScopeChat, InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard,
    LinkPreviewOptions, ReplyKeyboardMarkup, Update,
)
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Config
from .chat import (CHAT_LABEL, chat_info, enter_chat, handle_chat_message,
                   leave_chat, new_chat, show_answer_page)
from .knowledge import organize
from .parsing import parse_reminder
from .service import dispatch_due, send_digests, today_text
from .storage import Store
from . import workspace, planner, learning, memory, recognition, panel, chat_cleanup, inbox

LOG = logging.getLogger(__name__)
MAIN = Keyboard([
    [Button('🧭 Сегодня',callback_data='plan:today'),Button('📥 Разгрузить голову',callback_data='plan:capture')],
    [Button('📚 Мои материалы',callback_data='library'),Button('Работа рядом',callback_data='work:root:0')],
    [Button(CHAT_LABEL,callback_data='ai:chat'),Button('☰ Ещё',callback_data='more')],
])
HOME = Keyboard([[Button("🏠 Меню", callback_data="home")]])
HELP = (
    "<b>Твой личный ассистент</b>\n\n"
    "«Сегодня» собирает дела, фокус и ближайшие напоминания. «Разгрузить голову» "
    "принимает список дел: одна строка — одно дело. «Что сейчас?» подбирает дело по свободному времени.\n\n"
    "Отправь текст, ссылку или файл из главного меню. Авторазбор ИИ оформит справку в библиотеку, "
    "выделит явно указанные личные дела и предложит приблизительное время работы. Оценку можно исправить, сортировку — отменить. "
    "Авторазбор через Yandex можно выключить в настройках. Добавь #теги для поиска.\n\n"
    "<b>Напоминания</b>\n"
    "<code>/remind через 10 минут купить хлеб</code>\n"
    "<code>/remind завтра в 09:00 позвонить</code>\n"
    "<code>/remind 30.12.2026 18:30 подготовить подарки</code>\n"
    "<code>/remind каждый день в 09:00 выпить воды</code>\n"
    "<code>/remind каждую неделю в 10:00 план недели</code>\n\n"
    "Или нажми «Напоминания» → «Добавить»: помогу выбрать время.\n\n"
    "<b>Работа рядом</b>\nСоздавай свои разделы и добавляй в них рабочие материалы "
    "через кнопку «Добавить что-то нужное». Сохранение — после подтверждения.\n\n"
    "<b>Команды</b>\n/plan — дела и фокус\n/capture — добавить дела\n/library — библиотека\n/work — Работа рядом\n/search текст — поиск\n"
    "/today — план на сегодня\n/stats — статистика\n/export — выгрузка JSON\n"
    "/chat — диалог с ИИ\n/newchat — очистить память ИИ-диалога\n"
    "/study — учебные карточки и повторение\n"
    "/projects — проекты и опыт\n/memory запрос — поиск по личной памяти\n"
    "/settings — часовой пояс и ежедневная сводка\n/cancel — отменить текущий ввод\n\n"
    "Новые файлы сохраняются на диск перед удалением входящего сообщения. Выданные вложения очищаются из чата через час. "
    "Оригинал остаётся в материалах; при ошибке скачивания входящий файл не удаляется. "
    "Для файлов в выбранном проекте/разделе есть «Распознать содержимое»: "
    "PDF с текстом, TXT/MD, фото JPEG/PNG и голосовые Ogg Opus до 30 секунд. "
    "При включённом авторазборе входящие материалы передаются в Yandex автоматически; действуют лимиты ИИ-запросов. "
    "Автоматический текст помечен как непроверенный ИИ-разбор. Страницы сайтов пока не загружаются. "
    "Напоминания приходят, пока программа запущена и есть интернет."
)


def store(ctx) -> Store:
    return ctx.application.bot_data["store"]


def config(ctx) -> Config:
    return ctx.application.bot_data["config"]


def tz(ctx, owner: int) -> str:
    return store(ctx).get_setting(owner, "timezone", config(ctx).timezone)


def stamp(value: int, timezone: str) -> str:
    return datetime.fromtimestamp(value, ZoneInfo(timezone)).strftime("%d.%m.%Y в %H:%M")


async def say(update: Update, text: str, keyboard=None):
    return await panel.say(update,text,keyboard)


def protected(fn):
    """Accept users only in their own private chat; handlers scope every query.

    Telegram supplies effective_user.id. Callback IDs, message text and env
    values never select whose library to read. PTB keeps user_data per sender.
    """
    @wraps(fn)
    async def wrapper(update, ctx):
        user, chat = update.effective_user, update.effective_chat
        if not user or not chat or not update.effective_message:
            return
        if (chat.type != "private" or chat.id != user.id or user.id <= 0
                or getattr(user, "is_bot", False)
                or update.effective_message.chat_id != user.id):
            if update.callback_query:
                await update.callback_query.answer("Открой личный чат с ботом.", show_alert=True)
            return
        async with ctx.application.bot_data["lock"]:
            store(ctx).record_user(user.id, username=getattr(user, "username", None),
                                   display_name=getattr(user, "full_name", None)
                                   or getattr(user, "first_name", None))
            scope=panel.bind(ctx,update)
            try:
                await fn(update, ctx)
                if manager:=ctx.application.bot_data.get('cleanup'):
                    manager.received(update)
            finally:
                panel.unbind(scope)
    return wrapper


@protected
async def identity(update, ctx):
    recognition.clear(ctx)
    leave_chat(ctx, update.effective_user.id)
    workspace.clear_draft(ctx)
    planner.clear(ctx)
    learning.clear(ctx)
    memory.clear(ctx)
    await say(update, f"Твой Telegram ID: <code>{update.effective_user.id}</code>\n"
              "Твои материалы, напоминания и настройки привязаны к этому аккаунту.")


async def home(update, ctx):
    leave_chat(ctx, update.effective_user.id)
    ctx.user_data.clear()
    s = store(ctx).stats(update.effective_user.id)
    counts = store(ctx).task_counts(update.effective_user.id)
    await say(update, "<b>Что поможет тебе сейчас?</b>\n\n"
              f"Дел: {counts['active']} · Напоминаний: {s['pending']}\n\n"
              "🧭 Выбрать следующий шаг — «Сегодня».\n"
              "📥 Освободить голову — выгрузить список дел.\n"
              "Пришли материал сюда — ИИ распределит его в библиотеку или дела и оценит время. Авторазбор через Yandex можно выключить в настройках.\n\n"
              "Разделы открываются здесь, в одном сообщении. /start — вернуть панель вниз чата.\n\n"
              "Твои дела и материалы доступны только тебе.", MAIN)


async def more(update, ctx):
    await say(update, '<b>Ещё возможности</b>', Keyboard([
        [Button('🗂 Проекты и опыт', callback_data='mem:home:0'),Button('🔎 Память', callback_data='mem:search')],
        [Button('⏰ Напоминания', callback_data='reminders'), Button('⚙️ Настройки', callback_data='settings')],
        [Button('📖 Учёба', callback_data='learn:home'),Button('🎲 Вспомнить материал', callback_data='random')],
        [Button('📊 Статистика', callback_data='stats'), Button('📦 Выгрузить данные', callback_data='export')],
        [Button('Помощь', callback_data='help'), Button('🏠 Меню', callback_data='home')],
    ]))


async def library(update, ctx, offset=0):
    owner = update.effective_user.id
    opts = ctx.user_data.get("library_filter", {})
    total = store(ctx).count_items(owner, **opts)
    items = store(ctx).list_items(owner, **opts, offset=offset, limit=6)
    heading = "⭐ Избранное" if opts.get("favorites") else "📚 База знаний"
    scope = opts.get("query") or opts.get("category")
    if scope:
        heading += f" · {escape(str(scope)[:120])}"
    rows = [[Button(f"{'⭐ ' if i['favorite'] else ''}{i['title'][:48]}", callback_data=f"item:{i['id']}")] for i in items]
    nav = []
    if offset:
        nav.append(Button("← Назад", callback_data=f"page:{max(0, offset - 6)}"))
    if total > offset + 6:
        nav.append(Button("Дальше →", callback_data=f"page:{offset + 6}"))
    if nav:
        rows.append(nav)
    rows.extend([[Button("📂 Категории", callback_data="categories"), Button("🔎 Найти", callback_data="search")],
                 [Button('🗂 Проекты',callback_data='mem:home:0'),Button('Поиск по всей памяти',callback_data='mem:search')],
                 [Button("⭐ Избранное", callback_data="favorites"), Button("➕ Сохранить", callback_data="add")],
                 [Button('📖 Повторить знания', callback_data='learn:home'),Button("🏠 Меню", callback_data="home")]])
    empty = "\n\nПока пусто. Пришли ссылку, заметку или файл." if not total else "\nВыбери карточку:"
    await say(update, f"<b>{heading}</b>\nНайдено: {total}{empty}", Keyboard(rows))


async def item_card(update, ctx, item):
    if not item:
        await say(update, "Этот материал уже удалён.", HOME)
        return
    ident = item["id"]
    tags = " ".join("#" + t.lstrip("#") for t in item["tags"])
    body = escape(item["text"][:1600])
    if len(item["text"]) > 1600:
        body += "…\n<i>Полный текст — кнопка ниже.</i>"
    text = f"<b>{escape(item['title'])}</b>\n📂 {escape(item['category'])}\n{escape(tags[:300])}\n\n{body}"
    rows = [
        [Button('🗂 Связать с проектом',callback_data=f'mem:link:library:{ident}:0')],
        [Button('✅ Сделать делом', callback_data=f'plan:from:library:{ident}'),
         Button('💬 Разобрать с ИИ', callback_data=f'plan:analyze:library:{ident}')],
        [Button('🧠 Создать учебную карточку', callback_data=f'learn:from:library:{ident}')],
        [Button("⭐ Убрать звезду" if item["favorite"] else "☆ В избранное", callback_data=f"fav:{ident}"),
         Button("✏️ Изменить", callback_data=f"edit:{ident}")],
        [Button("📖 Открыть материал", callback_data=f"open:{ident}"), Button("⏰ Вернуться позже", callback_data=f"later:{ident}")],
        [Button("🗑 Удалить", callback_data=f"delete:{ident}"), Button("📚 Библиотека", callback_data="library")],
    ]
    recognize = recognition.button_for(store(ctx), update.effective_user.id, 'library', item)
    if recognize:
        rows.insert(0, [recognize])
    if store(ctx).inbox_job(update.effective_user.id, ident):
        rows.insert(0, [Button('✨ Авторазбор: результат / статус', callback_data=f'inbox:view:{ident}')])
    if item.get('file_id') and ctx.application.bot_data.get('cleanup'):
        local = store(ctx).local_file(update.effective_user.id, item['file_id'])
        if local and local['status'] == 'ready':
            text += '\n\n📎 Оригинал сохранён на диске. Выданная копия удалится из чата через час.'
        elif local and local['status'] == 'blocked':
            text += '\n\n⚠️ Локальная копия не создана: '+escape(local['error'])+' Входящее сообщение оставлено.'
        else:
            text += '\n\n⏳ Сохраняю оригинал на диск. Входящее вложение удалю только после проверки копии.'
    await say(update, text, Keyboard(rows))


async def reminder_list(update, ctx, offset=0):
    owner = update.effective_user.id
    reminders = store(ctx).list_reminders(owner, limit=7, offset=offset)
    rows = [[Button(f"{stamp(r['due_at'], tz(ctx,owner))} · {r['text'][:28]}", callback_data=f"rview:{r['id']}")] for r in reminders[:6]]
    nav = []
    if offset:
        nav.append(Button("← Назад", callback_data=f"rpage:{max(0,offset-6)}"))
    if len(reminders) > 6:
        nav.append(Button("Дальше →", callback_data=f"rpage:{offset+6}"))
    if nav:
        rows.append(nav)
    rows.extend([[Button("➕ Добавить напоминание", callback_data="rnew")], [Button("🏠 Меню", callback_data="home")]])
    await say(update, "<b>⏰ Напоминания</b>\n" + ("Выбери напоминание:" if reminders else "Новых напоминаний пока нет."), Keyboard(rows))


async def preview(update, ctx, raw):
    owner = update.effective_user.id
    try:
        parsed = parse_reminder(raw, tz(ctx, owner))
        if len(parsed.text) > 1000:
            raise ValueError("Сократи описание напоминания до 1000 символов.")
    except ValueError as exc:
        await say(update, escape(str(exc)) + "\n\nПример: <code>завтра в 09:00 позвонить</code>\n/cancel — отмена")
        return
    nonce = secrets.token_hex(4)
    ctx.user_data["draft"] = {"text": parsed.text, "due_at": parsed.due_at, "repeat": parsed.repeat,
                              "timezone": tz(ctx, owner), "nonce": nonce}
    ctx.user_data.pop("state", None)
    repeat = {"daily": "Каждый день", "weekly": "Каждую неделю", None: "Один раз"}[parsed.repeat]
    await say(update, f"<b>Создать напоминание?</b>\n\n{escape(parsed.text)}\n\n"
              f"🕒 {stamp(parsed.due_at, tz(ctx, owner))}\n{escape(tz(ctx,owner))} · {repeat}",
              Keyboard([[Button("✅ Создать", callback_data=f"confirm:{nonce}"), Button("Отмена", callback_data="cancel")]]))


async def settings(update, ctx):
    owner = update.effective_user.id
    digest = store(ctx).get_setting(owner, "digest_time", "off")
    auto = store(ctx).get_setting(owner, 'auto_inbox', 'on') == 'on'
    await say(update, f"<b>⚙️ Настройки</b>\n\nЧасовой пояс: {escape(tz(ctx, owner))}\n"
              f"Авторазбор ИИ: {'включён' if auto else 'выключен'}\n"
              f"Ежедневная сводка: {'выключена' if digest == 'off' else escape(digest)}\n\n"
              "Авторазбор передаёт новый входящий материал в Yandex AI: выделяет суть, создаёт явно указанные дела и оценивает время. Расходует дневной лимит. Отправка в выбранный проект/рабочий раздел сохраняет твой выбор.\n\n"
              "Сводка присылает план на день. Часовой пояс новых напоминаний можно изменить; "
              "уже созданные сохраняют своё расписание.", Keyboard([
                  [Button('Выключить авторазбор' if auto else 'Включить авторазбор', callback_data='auto:off' if auto else 'auto:on')],
                  [Button("🌍 Часовой пояс", callback_data="timezone")],
                  [Button("☀️ Сводка в 09:00", callback_data="digest:09:00"), Button("Выключить", callback_data="digest:off")],
                  [Button("🕒 Другое время сводки", callback_data="digesttime")],
                  [Button("📊 Статистика", callback_data="stats"), Button("📦 Выгрузить", callback_data="export")],
                  [Button("🏠 Меню", callback_data="home")],
              ]))


async def export(update, ctx):
    payload = json.dumps(store(ctx).export_data(update.effective_user.id), ensure_ascii=False, indent=2).encode("utf-8")
    await chat_cleanup.send_document(update,ctx,BytesIO(payload), filename="assistant-export.json",
        caption="Дела, база знаний, материалы «Работа рядом», напоминания и ссылки на файлы Telegram. Бинарные вложения не входят в выгрузку.")


async def stats(update, ctx):
    owner = update.effective_user.id
    db = store(ctx)
    s = db.stats(owner)
    tasks = db.task_counts(owner)
    await say(update, "<b>📊 Твоя статистика</b>\n\n"
              f"📚 Материалов: {s['items']}\n⭐ В избранном: {s['favorites']}\n"
              f"💼 Работа рядом: {db.count_work_sections(owner)} разделов, {db.count_work_materials(owner)} материалов\n"
              f"✅ Дел завершено: {tasks['done']} · Открыто: {tasks['active']}\n"
              f"⏰ Запланировано: {s['pending']}\n✅ Доставлено или завершено: {s['completed']}", HOME)


async def users(update, ctx, offset=0):
    """Read-only administrator directory; never fetch anyone's materials."""
    if config(ctx).admin_id is None or update.effective_user.id != config(ctx).admin_id:
        await say(update, "Команда доступна только администратору.", HOME)
        return
    db = store(ctx)
    total = db.count_users()
    offset = min(max(0, offset), max(0, (total - 1) // 8 * 8))
    entries = db.admin_users(limit=8, offset=offset)
    chunks = [f"<b>👥 Пользователи бота</b>\nВсего: {total}\n"]
    for index, entry in enumerate(entries, offset + 1):
        name = (entry["display_name"] or "Без имени")[:100]
        username = " @" + entry["username"][:40] if entry["username"] else ""
        chunks.append(f"\n<b>{index}. {escape(name)}{escape(username)}</b>\n"
                      f"ID: <code>{entry['user_id']}</code> · Материалов: {entry['item_count']}\n")
    if not entries:
        chunks.append("\nПользователей пока нет.\n")
    chunks.append("\n<i>Показаны профили и количество материалов. Содержимое чужих записей недоступно.</i>")
    nav = []
    if offset:
        nav.append(Button("← Назад", callback_data=f"users:{max(0, offset - 8)}"))
    if offset + 8 < total:
        nav.append(Button("Дальше →", callback_data=f"users:{offset + 8}"))
    rows = ([nav] if nav else []) + [[Button("🔄 Обновить", callback_data=f"users:{offset}"),
                                    Button("🏠 Меню", callback_data="home")]]
    await say(update, "".join(chunks), Keyboard(rows))


async def choose_time(update, ctx, description):
    ctx.user_data["reminder_text"] = description
    ctx.user_data["state"] = "reminder_time"
    await say(update, "<b>Когда напомнить?</b>\nВыбери кнопку или напиши: «завтра в 09:00», «через 2 часа», "
              "«каждый день в 09:00».\n/cancel — отмена", Keyboard([
                  [Button("Через 10 минут", callback_data="when:10"), Button("Через час", callback_data="when:60")],
                  [Button("Завтра в 09:00", callback_data="when:tomorrow")],
              ]))


@protected
async def command(update, ctx):
    recognition.clear(ctx)
    cmd, _, arg = (update.effective_message.text or "").partition(" ")
    cmd = cmd.split("@")[0].lower()
    leave_chat(ctx, update.effective_user.id)
    if cmd=='/cancel' and str(ctx.user_data.get('state','')).startswith('mem:'):
        await memory.home(update,ctx)
        return
    memory.clear(ctx)
    if cmd == '/cancel' and str(ctx.user_data.get('state','')).startswith('learn:'):
        await learning.home(update,ctx)
        return
    learning.clear(ctx)
    if cmd == "/cancel" and str(ctx.user_data.get("state", "")).startswith("work:"):
        await workspace.cancel(update, ctx)
        return
    if cmd == '/cancel' and str(ctx.user_data.get('state','')).startswith('plan:'):
        await planner.today(update,ctx)
        return
    workspace.clear_draft(ctx)
    planner.clear(ctx)
    # Commands exit pending input. Work drafts are invalidated on navigation.
    ctx.user_data.pop("state", None)
    owner = update.effective_user.id
    if cmd == "/start":
        store(ctx).set_setting(owner, "started", "1")
        await home(update, ctx)
    elif cmd in ("/menu", "/cancel"):
        await home(update, ctx)
    elif cmd == "/help":
        extra = "\n\n<b>Администратор</b>\n/users — пользователи бота" if config(ctx).admin_id == owner else ""
        await say(update, HELP + extra, MAIN)
    elif cmd == "/users":
        await users(update, ctx)
    elif cmd == "/chat":
        await enter_chat(update, ctx)
    elif cmd == "/newchat":
        await new_chat(update, ctx)
    elif cmd == "/library":
        ctx.user_data["library_filter"] = {}
        await library(update, ctx)
    elif cmd == "/work":
        await workspace.show_root(update, ctx)
    elif cmd in ('/plan', '/today'):
        await planner.today(update,ctx)
    elif cmd == '/capture':
        await planner.capture(update,ctx)
    elif cmd == '/study':
        await learning.home(update,ctx)
    elif cmd=='/projects':
        await memory.home(update,ctx)
    elif cmd=='/memory':
        await memory.search(update,ctx,arg.strip() or None)
    elif cmd == "/search":
        if arg.strip():
            ctx.user_data["library_filter"] = {"query": arg.strip().lstrip("#")[:200]}
            await library(update, ctx)
        else:
            ctx.user_data["state"] = "search"
            await say(update, "Что найти? Напиши слово, #тег или часть ссылки.\n/cancel — отмена")
    elif cmd == "/remind":
        if arg.strip():
            await preview(update, ctx, arg.strip())
        else:
            await reminder_list(update, ctx)
    elif cmd == "/settings":
        await settings(update, ctx)
    elif cmd == "/stats":
        await stats(update, ctx)
    elif cmd == "/export":
        await export(update, ctx)
    else:
        await say(update, "Не знаю эту команду. /help — список возможностей.", MAIN)


@protected
async def callback(update, ctx):
    q = update.callback_query
    data = q.data or ""
    if data.startswith('panel:'):
        manager=ctx.application.bot_data.get('panel')
        if manager:
            await manager.page(update)
        else:
            await q.answer('Открой материал заново.',show_alert=True)
        return
    if data.startswith("ai:page:"):
        match = re.fullmatch(r"ai:page:([a-f0-9]{16}):([0-9]{1,3})", data)
        if match:
            await show_answer_page(update, ctx, match[1], int(match[2]))
        else:
            await q.answer("Эта кнопка недоступна.", show_alert=True)
        return
    await q.answer()
    if data == "ai:noop":
        return
    owner = update.effective_user.id
    db = store(ctx)
    if data.startswith('inbox:') or data in ('auto:on', 'auto:off'):
        leave_chat(ctx, owner)
        recognition.clear(ctx)
        memory.clear(ctx)
        workspace.clear_draft(ctx)
        planner.clear(ctx)
        learning.clear(ctx)
        ctx.user_data.pop('state', None)
        if data.startswith('auto:'):
            db.set_setting(owner, 'auto_inbox', data.split(':')[1])
            await settings(update, ctx)
        else:
            await inbox.callback(update, ctx, data)
        return
    if data.startswith('rec:'):
        leave_chat(ctx, owner)
        memory.clear(ctx)
        workspace.clear_draft(ctx)
        planner.clear(ctx)
        learning.clear(ctx)
        await recognition.callback(update, ctx, data)
        return
    recognition.clear(ctx)
    if data in ("ai:chat", "ai:new", "ai:clear", "ai:info"):
        memory.clear(ctx)
        workspace.clear_draft(ctx)
        planner.clear(ctx)
        learning.clear(ctx)
        if data == "ai:info":
            await chat_info(update, ctx)
        elif data == "ai:chat":
            await enter_chat(update, ctx)
        else:
            await new_chat(update, ctx, confirmed=data == "ai:clear")
        return
    leave_chat(ctx, owner)
    if data.startswith('mem:'):
        workspace.clear_draft(ctx)
        planner.clear(ctx)
        learning.clear(ctx)
        await memory.callback(update,ctx,data)
        return
    memory.clear(ctx)
    if data.startswith('learn:'):
        workspace.clear_draft(ctx)
        planner.clear(ctx)
        await learning.callback(update,ctx,data)
        return
    learning.clear(ctx)
    if data.startswith('plan:'):
        workspace.clear_draft(ctx)
        await planner.callback(update,ctx,data)
        return
    planner.clear(ctx)
    if data.startswith("work:"):
        # Keep the active wizard until its own handler commits or cancels it.
        await workspace.handle_callback(update, ctx, data)
        return
    workspace.clear_draft(ctx)
    if data in ("home", "cancel"):
        await home(update, ctx)
    elif data == 'more':
        await more(update,ctx)
    elif data == 'reminders':
        ctx.user_data.pop('state',None)
        await reminder_list(update,ctx)
    elif data == 'settings':
        ctx.user_data.pop('state',None)
        await settings(update,ctx)
    elif data == 'help':
        await say(update,HELP,HOME)
    elif data == 'favorites':
        ctx.user_data['library_filter'] = {'favorites': True}
        await library(update,ctx)
    elif data == 'random':
        item = db.random_item(owner)
        await item_card(update,ctx,item) if item else await say(update,'Сначала сохрани материал в библиотеку.',HOME)
    elif data.startswith("users:"):
        # Authorize before parsing callback arguments or accessing the directory.
        if config(ctx).admin_id != owner:
            await users(update, ctx)
            return
        page = data.partition(":")[2]
        if not re.fullmatch(r"[0-9]{1,9}", page):
            await say(update, "Открой список заново командой /users.", HOME)
            return
        await users(update, ctx, int(page))
    elif data == "library":
        ctx.user_data.pop("state", None)
        ctx.user_data["library_filter"] = {}
        await library(update, ctx)
    elif data.startswith("page:"):
        await library(update, ctx, max(0, int(data.split(":")[1])))
    elif data == "categories":
        cats = db.categories(owner)
        ctx.user_data["categories"] = [c for c, _ in cats]
        await say(update, "<b>📂 Категории</b>" if cats else "Категории появятся после первого материала.",
                  Keyboard([[Button(f"{c[:40]} · {n}", callback_data=f"cat:{index}")] for index, (c,n) in enumerate(cats[:40])] + [[Button("Все материалы", callback_data="library")]]))
    elif data.startswith("cat:"):
        index = int(data.split(":")[1])
        cats = ctx.user_data.get("categories", [])
        if index >= len(cats):
            await say(update, "Открой категории заново.", HOME)
            return
        ctx.user_data["library_filter"] = {"category": cats[index]}
        await library(update, ctx)
    elif data in ("search", "add", "timezone", "digesttime", "rnew"):
        ctx.user_data["state"] = {"rnew": "reminder_text"}.get(data, data)
        messages = {
            "search": "Что найти? Напиши слово, #тег или часть ссылки.",
            "add": "Отправь ссылку, заметку, документ, фото, видео или голосовое. Подпись и #теги помогут с сортировкой.",
            "timezone": "Напиши название часового пояса, например Europe/Moscow или Asia/Yekaterinburg.",
            "digesttime": "Во сколько присылать сводку? Формат: 09:00.",
            "rnew": "О чём напомнить? Например: «позвонить врачу».",
        }
        await say(update, messages[data] + "\n/cancel — отмена")
    elif data.startswith("digest:"):
        db.set_setting(owner, "digest_time", data[len("digest:"):])
        await settings(update, ctx)
    elif data == "stats":
        await stats(update, ctx)
    elif data == "export":
        await export(update, ctx)
    elif data.startswith("confirm:"):
        draft = ctx.user_data.get("draft")
        if not draft or draft["nonce"] != data.split(":")[1]:
            await say(update, "Эта кнопка уже использована или устарела. Создай новое напоминание.", HOME)
            return
        if draft["due_at"] <= int(time.time()):
            ctx.user_data.pop("draft", None)
            await say(update, "Выбранное время уже прошло. Укажи новое.", HOME)
            return
        r = db.add_reminder(owner, draft["text"], draft["due_at"], draft["timezone"], draft["repeat"])
        ctx.user_data.pop("draft", None)
        await say(update, f"✅ Напоминание сохранено на {stamp(r['due_at'], r['timezone'])}.",
                  Keyboard([[Button("Открыть", callback_data=f"rview:{r['id']}"), Button("🏠 Меню", callback_data="home")]]))
    elif data.startswith("when:"):
        description = ctx.user_data.get("reminder_text")
        if not description:
            await say(update, "Сначала укажи, о чём напомнить.", HOME)
            return
        choice = data.split(":")[1]
        when = "завтра в 09:00" if choice == "tomorrow" else f"через {int(choice)} минут"
        await preview(update, ctx, f"{when} {description}")
    elif data.startswith("rpage:"):
        await reminder_list(update, ctx, max(0,int(data.split(":")[1])))
    elif data.startswith(("rview:", "rd:", "rs:", "rc:", "ra:")):
        parts = data.split(":")
        action, ident = parts[0], int(parts[1])
        r = db.get_reminder(owner, ident)
        if not r or r["status"] in ("done", "cancelled"):
            await say(update, "Это напоминание уже завершено.", HOME)
            return
        if action == "rview":
            repeat = {None: "Один раз", "daily": "Каждый день", "weekly": "Каждую неделю"}.get(r["repeat"], "")
            await say(update, f"<b>⏰ Напоминание</b>\n\n{escape(r['text'])}\n\n"
                      f"{stamp(r['due_at'], r['timezone'])} · {escape(r['timezone'])}\n{repeat}", Keyboard([
                          [Button("Остановить повторы" if r["repeat"] else "✅ Готово", callback_data=f"rd:{ident}")],
                          [Button("Отменить напоминание", callback_data=f"rc:{ident}"), Button("🏠 Меню", callback_data="home")],
                      ]))
        elif action == "rd":
            db.complete_reminder(owner, ident)
            await say(update, "✅ Готово. Повторы остановлены." if r["repeat"] else "✅ Отмечено выполненным.", HOME)
        elif action == "ra":
            await say(update, "✅ Отлично! Следующее напоминание — " + stamp(r["due_at"], r["timezone"]) + ".", HOME)
        elif action == "rc":
            db.cancel_reminder(owner, ident)
            await say(update, "Напоминание отменено.", HOME)
        elif action == "rs":
            minutes = int(parts[2])
            if minutes not in (10, 60):
                return
            # Do not shift the regular daily/weekly schedule when snoozing an occurrence.
            key = f"snooze:{q.message.message_id}"
            if db.get_setting(owner, key):
                await say(update, "Это уведомление уже отложено.", HOME)
                return
            due = int(time.time()) + minutes * 60
            if r["repeat"]:
                db.add_reminder(owner, r["text"], due, r["timezone"])
            else:
                db.snooze_reminder(owner, ident, due)
            db.set_setting(owner, key, "1")
            await say(update, f"⏳ Напомню через {minutes} минут.", HOME)
    elif data.startswith(("item:", "fav:", "edit:", "open:", "later:", "delete:", "erase:")):
        action, raw_id = data.split(":", 1)
        ident = int(raw_id)
        item = db.get_item(owner, ident)
        if not item:
            await item_card(update, ctx, None)
            return
        if action == "item":
            await item_card(update, ctx, item)
        elif action == "fav":
            await item_card(update, ctx, db.toggle_favorite(owner, ident))
        elif action == "edit":
            ctx.user_data["state"] = "edit"
            ctx.user_data["edit_id"] = ident
            await say(update, "Введи три части через вертикальную черту:\n"
                      "<code>Название | Категория | #тег #тег</code>\n"
                      "Например: <code>Курс Python | Обучение | #python #курсы</code>\n/cancel — отмена")
        elif action == "later":
            await choose_time(update, ctx, f"Вернуться к материалу #{ident}: {item['title']} (открыть: /library)")
        elif action == "delete":
            await say(update, f"Удалить «{escape(item['title'])}» из базы?", Keyboard([
                [Button("Да, удалить", callback_data=f"erase:{ident}"), Button("Оставить", callback_data=f"item:{ident}")]]))
        elif action == "erase":
            db.delete_item(owner, ident)
            await say(update, "Материал удалён из базы.", HOME)
        elif action == "open":
            if item["file_id"]:
                methods = {"document": "send_document", "photo": "send_photo", "voice": "send_voice",
                           "audio": "send_audio", "video": "send_video", "animation": "send_animation", "video_note": "send_video_note"}
                await chat_cleanup.send_original(ctx,owner,item['kind'],item['file_id'],item.get('file_name'))
            body='\n\n'.join([item['text'],*[url for url in item['urls'] if url not in item['text']]]).strip()
            if body:
                await say(update,escape(body),Keyboard([[Button('← Карточка',callback_data=f'item:{ident}')]]))


@protected
async def message(update, ctx):
    msg = update.effective_message
    text = msg.text or msg.caption or ""
    owner = update.effective_user.id
    actions = {
        "⏰ Напоминания": "reminders", "📚 База знаний": "library", "🔎 Поиск": "search",
        "⭐ Избранное": "favorites", "🗓 Мой день": "today", "🎯 Фокус": "focus",
        "🎲 Вспомнить": "random", "⚙️ Настройки": "settings",
        CHAT_LABEL: "ai",
        "Работа рядом": "work",
        "🧭 Сегодня": "today", "📥 Разгрузить голову": "capture",
        "📚 Мои материалы": "library", "☰ Ещё": "more",
    }
    action = actions.get(msg.text)
    if action:
        recognition.clear(ctx)
        leave_chat(ctx, owner)
        memory.clear(ctx)
        learning.clear(ctx)
        workspace.clear_draft(ctx)
        planner.clear(ctx)
        ctx.user_data.pop("state", None)
        if action == "ai":
            await enter_chat(update, ctx)
        elif action == "work":
            await workspace.show_root(update, ctx)
        elif action == "reminders":
            await reminder_list(update, ctx)
        elif action in ("library", "favorites"):
            ctx.user_data["library_filter"] = {"favorites": True} if action == "favorites" else {}
            await library(update, ctx)
        elif action == "search":
            ctx.user_data["state"] = "search"
            await say(update, "Что найти? Напиши слово, #тег или часть ссылки.\n/cancel — отмена")
        elif action == "today":
            await planner.today(update,ctx)
        elif action == 'capture':
            await planner.capture(update,ctx)
        elif action == 'more':
            await more(update,ctx)
        elif action == "focus":
            await preview(update, ctx, "через 25 минут Фокус завершён — пора сделать перерыв ☕")
        elif action == "random":
            item = store(ctx).random_item(owner)
            if item:
                await item_card(update, ctx, item)
            else:
                await say(update, "Сохрани первый материал — и я помогу вернуться к нему позже.", HOME)
        elif action == "settings":
            await settings(update, ctx)
        return

    state = ctx.user_data.get("state")
    if str(state or '').startswith('rec:'):
        await recognition.message(update, ctx)
        return
    if state in (None,'mem:browse','plan:browse') and msg.text:
        free_time=re.fullmatch(r'(?i)(?:у меня есть|есть)\s+(\d{1,3})\s+мин(?:ут|уты|ута)?[.!]?',msg.text.strip())
        if free_time:
            memory.clear(ctx)
            await planner.choose(update,ctx,int(free_time[1]))
            return
    if state in (None,'mem:browse','plan:browse') and msg.text and re.match(r'(?i)^верни\s+меня\s+в\s+проект\s+',msg.text):
        await memory.resume(update,ctx,re.sub(r'(?i)^верни\s+меня\s+в\s+проект\s+','',msg.text))
        return
    if str(state or '').startswith('mem:'):
        await memory.message(update,ctx)
        return
    if str(state or '').startswith('learn:'):
        await learning.message(update,ctx)
        return
    if str(state or '').startswith('plan:'):
        await planner.message(update,ctx)
        return
    if str(state or "").startswith("work:"):
        await workspace.handle_message(update, ctx)
        return
    if state == "ai":
        await handle_chat_message(update, ctx)
        return
    if state and state != "add" and not msg.text:
        await say(update, "Сейчас жду текст. /cancel — выйти из текущего действия и сохранить файл.")
        return
    if state == "search":
        ctx.user_data.pop("state",None)
        ctx.user_data["library_filter"] = {"query": text.strip().lstrip("#")[:200]}
        await library(update, ctx)
        return
    if state == "timezone":
        try:
            ZoneInfo(text.strip())
        except (ValueError, ZoneInfoNotFoundError):
            await say(update, "Не нашёл такой часовой пояс. Пример: Europe/Moscow.")
            return
        store(ctx).set_setting(owner,"timezone",text.strip())
        ctx.user_data.pop("state",None)
        await settings(update,ctx)
        return
    if state == "digesttime":
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text.strip()):
            await say(update, "Нужно время в формате ЧЧ:ММ, например 09:00.")
            return
        store(ctx).set_setting(owner,"digest_time",text.strip())
        ctx.user_data.pop("state",None)
        await settings(update,ctx)
        return
    if state == "reminder_text":
        if not text.strip() or len(text) > 900:
            await say(update, "Напиши описание от 1 до 900 символов.")
            return
        await choose_time(update,ctx,text.strip())
        return
    if state == "reminder_time":
        await preview(update,ctx,f"{text} {ctx.user_data['reminder_text']}")
        return
    if state == "edit":
        parts = [s.strip() for s in text.split("|",2)]
        if len(parts) != 3 or not parts[0] or not parts[1] or len(parts[0])>100 or len(parts[1])>50:
            await say(update,"Формат: <code>Название | Категория | #тег #тег</code>. Название — до 100, категория — до 50 символов.")
            return
        tags = [t.lstrip("#")[:40] for t in parts[2].split()[:12] if t.lstrip("#")]
        item = store(ctx).update_item(owner,ctx.user_data["edit_id"],parts[0],parts[1],tags)
        ctx.user_data.pop("state",None)
        await item_card(update,ctx,item)
        return
    if state != "add" and re.match(r"(?i)^напомни\b",text):
        await preview(update,ctx,text)
        return

    kind, file_id, file_name = "text", None, None
    for candidate in ("document", "photo", "voice", "audio", "video", "animation", "video_note"):
        obj = getattr(msg,candidate,None)
        if obj:
            if candidate == "photo":
                obj = obj[-1]
            kind, file_id = candidate, obj.file_id
            file_name = getattr(obj,"file_name",None)
            break
    if not text and not file_id:
        await say(update,"Пришли текст, ссылку, документ, фото, видео или голосовое сообщение.",MAIN)
        return
    urls = []
    for entity, value in {**msg.parse_entities(), **msg.parse_caption_entities()}.items():
        if entity.type == "text_link" and entity.url:
            urls.append(entity.url)
        elif entity.type == "url":
            urls.append(value)
    organized = organize(text,kind=kind,file_name=file_name,urls=urls)
    item = store(ctx).add_item(owner,text=text,kind=kind,file_id=file_id,file_name=file_name,
        source_chat_id=msg.chat_id,source_message_id=msg.message_id,**organized)
    ctx.user_data.pop("state",None)
    if await inbox.enqueue(update, ctx, item):
        return
    await say(update,"✅ Сохранил в базу знаний.")
    await item_card(update,ctx,item)


async def tick(ctx):
    cfg = config(ctx)
    async with ctx.application.bot_data["lock"]:
        await dispatch_due(store(ctx),ctx.bot,panel_manager=ctx.application.bot_data.get('panel'))
        await send_digests(store(ctx),ctx.bot,cfg.timezone,panel_manager=ctx.application.bot_data.get('panel'))


async def housekeeping(ctx):
    if manager:=ctx.application.bot_data.get('cleanup'):
        await manager.tick()
    await inbox.tick(ctx)


async def shutdown(app):
    tasks=list(app.bot_data.get('inbox_tasks', {}).values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks,return_exceptions=True)
    if manager:=app.bot_data.get('cleanup'):
        tasks=list(manager.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


async def post_init(app):
    commands = [BotCommand(name,description) for name,description in [
        ("start","Главное меню"),("remind","Создать напоминание"),("library","База знаний"),
        ("work","Работа рядом — мои материалы"),
        ("plan","Дела и фокус"),("capture","Разгрузить голову"),
        ('study','Учёба и повторение'),
        ('projects','Проекты и опыт'),('memory','Поиск по личной памяти'),
        ("search","Поиск материалов"),("today","План на сегодня"),("settings","Настройки"),
        ("stats","Статистика"),("export","Выгрузить данные"),("cancel","Отменить ввод"),
        ("id","Мой Telegram ID"),("help","Примеры и помощь"),
        ("chat","Диалог с ИИ"),("newchat","Новый ИИ-диалог"),
    ]]
    await app.bot.set_my_commands(commands)
    admin_id = app.bot_data["config"].admin_id
    if admin_id is not None:
        try:
            await app.bot.set_my_commands(commands + [BotCommand("users", "Пользователи бота")],
                                          scope=BotCommandScopeChat(admin_id))
        except TelegramError as exc:
            # A newly configured admin may not have opened the chat yet.
            # Keep the public bot running; typing /users still authenticates.
            LOG.warning("Admin menu unavailable: %s", type(exc).__name__)
    app.job_queue.run_repeating(tick, interval=5, first=1, job_kwargs={"max_instances":1,"coalesce":True})
    app.job_queue.run_repeating(housekeeping, interval=5, first=2, job_kwargs={"max_instances":1,"coalesce":True})


async def on_error(update, ctx):
    # Avoid logging Update objects, tokens, URLs or personal messages.
    LOG.error("Handler failed: %s",type(ctx.error).__name__)
    if isinstance(update,Update) and update.effective_chat and update.effective_chat.type=="private":
        try:
            if update.effective_user and update.effective_user.id==update.effective_chat.id:
                scope=panel.bind(ctx,update)
                try:
                    await say(update,"Не получилось завершить действие. Проверь результат в меню перед повтором. /cancel — выйти из ввода.",MAIN)
                finally:
                    panel.unbind(scope)
        except TelegramError:
            pass


def build_application(cfg:Config, db:Store)->Application:
    app = Application.builder().token(cfg.token).concurrent_updates(False).post_init(post_init).post_shutdown(shutdown).build()
    app.bot_data.update(config=cfg,store=db,lock=asyncio.Lock())
    app.bot_data['panel']=panel.Panel(app.bot,db)
    app.bot_data['cleanup']=chat_cleanup.Cleanup(app.bot,db,cfg)
    app.add_handler(CommandHandler("id",identity))
    app.add_handler(MessageHandler(filters.COMMAND,command))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.ALL,message))
    app.add_error_handler(on_error)
    return app
