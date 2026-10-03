"""Private, user-managed work sections and their Telegram materials.

The public bot router supplies authentication and the shared database lock.
Every object lookup still includes the authenticated owner. All writes require
a single-use confirmation stored in that owner's transient conversation data.
"""

from __future__ import annotations

from html import escape
import re
import secrets

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard, LinkPreviewOptions
from telegram.error import TelegramError

from .knowledge import organize
from . import recognition, panel, chat_cleanup


PAGE_SIZE = 8
_MEDIA = {
    "document": "send_document", "photo": "send_photo", "voice": "send_voice",
    "audio": "send_audio", "video": "send_video", "animation": "send_animation",
    "video_note": "send_video_note",
}
_KINDS = {
    "document": "Документ", "photo": "Фото", "voice": "Голосовое сообщение",
    "audio": "Аудио", "video": "Видео", "animation": "Анимация",
    "video_note": "Видеосообщение", "text": "Текст",
}


def _db(ctx):
    return ctx.application.bot_data["store"]


def _owner(update):
    return update.effective_user.id


def clear_draft(ctx):
    """Discard only this feature's transient state, preserving other features."""
    for key in tuple(ctx.user_data):
        if key.startswith("work_"):
            ctx.user_data.pop(key, None)
    if str(ctx.user_data.get("state", "")).startswith("work:"):
        ctx.user_data.pop("state", None)


def _reset(ctx):
    clear_draft(ctx)
    ctx.user_data["state"] = "work:browse"


async def _say(update, text, rows=None):
    return await panel.say(update,text,Keyboard(rows) if rows else None)


def _button(label, callback):
    return Button(label, callback_data=callback)


def _input_rows(extra=None):
    return (extra or []) + [[_button("⬅️ Назад", "work:back"), _button("Отмена", "work:cancel")]]


def _input(ctx, state, back):
    ctx.user_data.pop("work_pending", None)
    ctx.user_data["state"] = state
    ctx.user_data["work_back"] = back


def _offset(total, offset):
    return min(max(0, offset), max(0, (total - 1) // PAGE_SIZE * PAGE_SIZE))


def _navigation(total, offset, prefix):
    row = []
    if offset:
        row.append(_button("← Назад", f"{prefix}:{max(0, offset - PAGE_SIZE)}"))
    if offset + PAGE_SIZE < total:
        row.append(_button("Дальше →", f"{prefix}:{offset + PAGE_SIZE}"))
    return [row] if row else []


def _title(text, limit):
    title = " ".join(text.split())
    if not title or len(title) > limit:
        raise ValueError(f"Напиши название от 1 до {limit} символов.")
    return title


def _id(value, *, zero=False):
    if not re.fullmatch(r"[0-9]{1,19}", value):
        raise ValueError("Некорректная кнопка.")
    number = int(value)
    if number > 2**63 - 1 or (not zero and number == 0):
        raise ValueError("Некорректная кнопка.")
    return number


def _chunks(text, limit=2800):
    """Bound visible text by UTF-16 units, including non-BMP emoji."""
    result, start, units = [], 0, 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit:
            result.append(text[start:index])
            start, units = index, 0
        units += width
    result.append(text[start:])
    return result


async def _missing(update, ctx):
    _reset(ctx)
    await _say(update, "Раздел или материал недоступен. Возможно, он уже удалён.",
               [[_button("⬅️ Работа рядом", "work:root:0")]])


async def _expired(update):
    await _say(update, "Эта кнопка уже устарела. Открой нужное действие заново.",
               [[_button("⬅️ Работа рядом", "work:root:0")]])


async def show_root(update, ctx, offset=0):
    _reset(ctx)
    owner, db = _owner(update), _db(ctx)
    total = db.count_work_sections(owner)
    offset = _offset(total, offset)
    sections = db.list_work_sections(owner, limit=PAGE_SIZE, offset=offset)
    rows = [[_button(f"📁 {section['title']} · {section['material_count']}",
                     f"work:section:{section['id']}:0")] for section in sections]
    rows += _navigation(total, offset, "work:root")
    rows += [[_button("➕ Добавить что-то нужное", "work:add")],
             [_button("🏠 Главное меню", "home")]]
    text = "<b>Работа рядом</b>\n\nТвои рабочие разделы и материалы видны только тебе."
    text += "\nВыбери раздел:" if total else "\n\nПока разделов нет. Добавь первый — например, «Инструкции»."
    await _say(update, text, rows)


async def _add_menu(update, ctx):
    _reset(ctx)
    await _say(update, "<b>Добавить что-то нужное</b>\n\nЧто добавляем?", [
        [_button("📁 Добавить раздел", "work:new:0")],
        [_button("📝 Добавить информацию в раздел", "work:pick:0")],
        [_button("⬅️ Назад", "work:root:0"), _button("Отмена", "work:cancel")],
    ])


async def _new_section(update, ctx, resume=False, restore=False):
    if not restore:
        _reset(ctx)
        ctx.user_data["work_draft"] = {"owner": _owner(update), "resume": resume}
    draft = ctx.user_data.get("work_draft", {})
    if draft.get("owner") != _owner(update):
        await _expired(update)
        return
    _input(ctx, "work:section_name", "work:pick:0" if draft.get("resume") else "work:add")
    await _say(update, "<b>Новый раздел</b>\n\nКак назовём раздел? Напиши название до 60 символов.", _input_rows())


async def _pick_section(update, ctx, offset=0):
    _reset(ctx)
    owner, db = _owner(update), _db(ctx)
    total = db.count_work_sections(owner)
    if not total:
        await _say(update, "Сначала создадим раздел, затем добавим в него материал.", [
            [_button("📁 Создать первый раздел", "work:new:1")],
            [_button("⬅️ Назад", "work:add"), _button("Отмена", "work:cancel")],
        ])
        return
    offset = _offset(total, offset)
    sections = db.list_work_sections(owner, limit=PAGE_SIZE, offset=offset)
    rows = [[_button(f"📁 {s['title']}", f"work:target:{s['id']}")] for s in sections]
    rows += _navigation(total, offset, "work:pick")
    rows += [[_button("📁 Добавить раздел", "work:new:1")],
             [_button("⬅️ Назад", "work:add"), _button("Отмена", "work:cancel")]]
    await _say(update, "<b>Добавить информацию</b>\n\nВыбери раздел:", rows)


async def _content(update, ctx, section_id=None):
    owner = _owner(update)
    if section_id is not None:
        _reset(ctx)
        ctx.user_data["work_draft"] = {"owner": owner, "section_id": section_id}
    draft = ctx.user_data.get("work_draft", {})
    section = _db(ctx).get_work_section(owner, draft.get("section_id", 0)) if draft.get("owner") == owner else None
    if not section:
        await _missing(update, ctx)
        return
    _input(ctx, "work:material_content", "work:pick:0")
    await _say(update, f"<b>📁 {escape(section['title'])}</b>\n\n"
               "Пришли текст, ссылку или один файл с подписью. Можно переслать сообщение.\n"
               "Поддерживаются документы, фото, видео, аудио, голосовые сообщения и анимации.", _input_rows())


async def _ask_material_title(update, ctx):
    draft = ctx.user_data.get("work_draft", {})
    if draft.get("owner") != _owner(update) or not draft.get("material"):
        await _expired(update)
        return
    if not _db(ctx).get_work_section(_owner(update), draft["section_id"]):
        await _missing(update, ctx)
        return
    _input(ctx, "work:material_title", "work:content")
    nonce = secrets.token_hex(8)
    ctx.user_data["work_suggestion"] = nonce
    suggestion = draft["material"]["title"]
    await _say(update, "<b>Как назвать материал?</b>\n\n"
               "Напиши короткое название для кнопки — до 100 символов.\n"
               f"Предлагаю: <b>{escape(suggestion)}</b>",
               _input_rows([[_button("Использовать предложенное название", f"work:suggest:{nonce}")]]))


async def _confirmation(update, ctx, *, op, text, back, label="✅ Сохранить", **payload):
    nonce = secrets.token_hex(8)
    ctx.user_data["work_pending"] = {"owner": _owner(update), "nonce": nonce, "op": op, **payload}
    ctx.user_data["state"] = "work:confirm"
    ctx.user_data["work_back"] = back
    await _say(update, text, _input_rows([[_button(label, f"work:confirm:{nonce}")]]))


def _body(material):
    text = material.get("text") or ""
    extra = [url for url in (material.get("urls") or []) if url not in text]
    return "\n\n".join([value for value in [text, *extra] if value])


async def _preview_material(update, ctx, title):
    draft = ctx.user_data.get("work_draft", {})
    owner = _owner(update)
    section = _db(ctx).get_work_section(owner, draft.get("section_id", 0)) if draft.get("owner") == owner else None
    if not section or not draft.get("material"):
        await _missing(update, ctx)
        return
    material = draft["material"]
    material["title"] = title
    preview = _chunks(_body(material), 2100)[0]
    if len(preview) < len(_body(material)):
        preview += "\n…\nПолный текст сохранится целиком."
    attachment = ""
    if material.get("file_id"):
        attachment = f"\n📎 {escape((material.get('file_name') or _KINDS[material['kind']])[:150])}"
    await _confirmation(update, ctx, op="add_material",
        text=f"<b>Сохранить материал?</b>\n\n📁 {escape(section['title'])}\n"
             f"<b>{escape(title)}</b>{attachment}\n\n{escape(preview)}",
        back="work:title")


async def _show_section(update, ctx, section_id, offset=0):
    _reset(ctx)
    owner, db = _owner(update), _db(ctx)
    section = db.get_work_section(owner, section_id)
    if not section:
        await _missing(update, ctx)
        return
    total = db.count_work_materials(owner, section_id)
    offset = _offset(total, offset)
    materials = db.list_work_materials(owner, section_id, limit=PAGE_SIZE, offset=offset)
    rows = [[_button(f"{'📎' if m.get('file_id') else '📝'} {m['title']}", f"work:card:{m['id']}:0")] for m in materials]
    rows += _navigation(total, offset, f"work:section:{section_id}")
    rows += [[_button("➕ Добавить информацию", f"work:target:{section_id}")],
             [_button("⚙️ Управление разделом", f"work:manage:{section_id}")],
             [_button("⬅️ Работа рядом", "work:root:0")]]
    text = f"<b>📁 {escape(section['title'])}</b>\n\nМатериалов: {total}"
    text += "\nВыбери материал:" if total else "\n\nРаздел пока пустой. Добавь первый материал."
    await _say(update, text, rows)


async def _card(update, ctx, material_id, page=0, saved=False):
    _reset(ctx)
    material = _db(ctx).get_work_material(_owner(update), material_id)
    if not material:
        await _missing(update, ctx)
        return
    body = _body(material)
    pages = _chunks(body)
    page = min(max(0, page), len(pages) - 1)
    text = ("✅ Материал сохранён.\n\n" if saved else "") + f"<b>{escape(material['title'])}</b>"
    if material.get("file_id"):
        text += f"\n📎 {escape((material.get('file_name') or _KINDS.get(material['kind'], 'Файл'))[:150])}"
    if pages[page]:
        text += f"\n\n{escape(pages[page])}"
    rows = []
    if len(pages) > 1:
        text += f"\n\n<i>Страница {page + 1} из {len(pages)}</i>"
        nav = []
        if page:
            nav.append(_button("← Назад", f"work:card:{material_id}:{page - 1}"))
        if page + 1 < len(pages):
            nav.append(_button("Дальше →", f"work:card:{material_id}:{page + 1}"))
        rows.append(nav)
    if material.get("file_id"):
        rows.append([_button("📎 Открыть файл", f"work:open:{material_id}")])
    recognize = recognition.button_for(_db(ctx), _owner(update), 'work', material)
    if recognize:
        rows.append([recognize])
    rows += [[_button('🗂 Связать с проектом',f'mem:link:work:{material_id}:0')],
             [_button('🧠 Создать учебную карточку', f'learn:from:work:{material_id}')],
             [_button('✅ Сделать делом', f'plan:from:work:{material_id}'),
              _button('💬 Разобрать с ИИ', f'plan:analyze:work:{material_id}')],
             [_button("✏️ Переименовать", f"work:rename_m:{material_id}"),
              _button("📁 Переместить", f"work:move:{material_id}:0")],
             [_button("🗑 Удалить", f"work:delete_m:{material_id}")],
             [_button("➕ Добавить ещё сюда", f"work:target:{material['section_id']}")],
             [_button("⬅️ В раздел", f"work:section:{material['section_id']}:0")]]
    await _say(update, text, rows)


async def _open_file(update, ctx, material_id):
    _reset(ctx)
    material = _db(ctx).get_work_material(_owner(update), material_id)
    if not material:
        await _missing(update, ctx)
        return
    if not material.get("file_id") or material["kind"] not in _MEDIA:
        await _card(update, ctx, material_id)
        return
    try:
        await chat_cleanup.send_original(ctx,_owner(update),material['kind'],material['file_id'],material.get('file_name'))
    except TelegramError:
        await _say(update, "Telegram не смог вернуть файл. Попробуй ещё раз позже.",
                   [[_button("⬅️ К материалу", f"work:card:{material_id}:0")]])


async def _manage(update, ctx, section_id):
    _reset(ctx)
    section = _db(ctx).get_work_section(_owner(update), section_id)
    if not section:
        await _missing(update, ctx)
        return
    await _say(update, f"<b>⚙️ {escape(section['title'])}</b>\n\nУправление разделом:", [
        [_button("✏️ Переименовать", f"work:rename_s:{section_id}")],
        [_button("⬆️ Выше", f"work:shift:{section_id}:-1"), _button("⬇️ Ниже", f"work:shift:{section_id}:1")],
        [_button("🗑 Удалить раздел", f"work:delete_s:{section_id}")],
        [_button("⬅️ В раздел", f"work:section:{section_id}:0")],
    ])


async def _rename(update, ctx, kind, ident):
    _reset(ctx)
    db, owner = _db(ctx), _owner(update)
    obj = db.get_work_section(owner, ident) if kind == "s" else db.get_work_material(owner, ident)
    if not obj:
        await _missing(update, ctx)
        return
    ctx.user_data["work_input"] = {"owner": owner, "kind": kind, "id": ident}
    _input(ctx, "work:rename", f"work:manage:{ident}" if kind == "s" else f"work:card:{ident}:0")
    await _say(update, f"Сейчас: <b>{escape(obj['title'])}</b>\n\n"
               f"Напиши новое название до {60 if kind == 's' else 100} символов.", _input_rows())


async def _move_picker(update, ctx, material_id, offset=0):
    _reset(ctx)
    db, owner = _db(ctx), _owner(update)
    material = db.get_work_material(owner, material_id)
    if not material:
        await _missing(update, ctx)
        return
    total = db.count_work_sections(owner)
    offset = _offset(total, offset)
    sections = db.list_work_sections(owner, limit=PAGE_SIZE, offset=offset)
    rows = [[_button(f"{'✓ ' if s['id'] == material['section_id'] else '📁 '}{s['title']}",
                     f"work:move_to:{material_id}:{s['id']}")] for s in sections]
    rows += _navigation(total, offset, f"work:move:{material_id}")
    rows += [[_button("⬅️ Назад", f"work:card:{material_id}:0"), _button("Отмена", "work:cancel")]]
    await _say(update, f"<b>Переместить «{escape(material['title'])}»</b>\n\nВыбери раздел:", rows)


async def _confirm_action(update, ctx, op, ident, target=None):
    _reset(ctx)
    owner, db = _owner(update), _db(ctx)
    is_section = op in {"delete_section", "shift_section"}
    obj = db.get_work_section(owner, ident) if is_section else db.get_work_material(owner, ident)
    if not obj:
        await _missing(update, ctx)
        return
    title = escape(obj["title"])
    if op == "delete_section":
        text = f"<b>Удалить раздел «{title}»?</b>\n\nТакже будут удалены все его материалы: {obj['material_count']}. Восстановить их через бота не получится."
        back, label = f"work:manage:{ident}", "🗑 Да, удалить раздел"
    elif op == "delete_material":
        text, back, label = f"Удалить материал <b>«{title}»</b>?", f"work:card:{ident}:0", "🗑 Да, удалить материал"
    elif op == "move_material":
        section = db.get_work_section(owner, target)
        if not section:
            await _missing(update, ctx)
            return
        if obj["section_id"] == target:
            await _say(update, "Материал уже в этом разделе.", [[_button("⬅️ К материалу", f"work:card:{ident}:0")]])
            return
        text = f"Переместить <b>«{title}»</b> в раздел <b>«{escape(section['title'])}»</b>?"
        back, label = f"work:move:{ident}:0", "✅ Переместить"
    else:
        text = f"Передвинуть раздел <b>«{title}»</b> на одну позицию {'выше' if target == -1 else 'ниже'}?"
        back, label = f"work:manage:{ident}", "✅ Передвинуть"
    await _confirmation(update, ctx, op=op, ident=ident, target=target,
                        text=text, back=back, label=label)


async def _apply_confirmation(update, ctx, nonce):
    pending = ctx.user_data.get("work_pending", {})
    owner, db = _owner(update), _db(ctx)
    if (ctx.user_data.get("state") != "work:confirm" or pending.get("owner") != owner
            or not secrets.compare_digest(str(pending.get("nonce", "")), nonce)):
        await _expired(update)
        return
    # Consume before the write, under the router's shared lock. A retry cannot
    # apply the same mutation twice, even when Telegram delivery later fails.
    ctx.user_data.pop("work_pending", None)
    ctx.user_data["state"] = "work:browse"
    op, ident = pending["op"], pending.get("ident")
    try:
        if op == "add_section":
            section = db.create_work_section(owner, pending["title"])
            if pending.get("resume"):
                await _content(update, ctx, section["id"])
            else:
                await _show_section(update, ctx, section["id"])
        elif op == "add_material":
            draft = ctx.user_data.get("work_draft", {})
            if draft.get("owner") != owner or not draft.get("material"):
                await _expired(update)
                return
            material = db.add_work_material(owner, draft["section_id"], **draft["material"])
            if material:
                await _card(update, ctx, material["id"], saved=True)
            else:
                await _missing(update, ctx)
        elif op in {"rename_section", "rename_material"}:
            if op == "rename_section":
                result = db.rename_work_section(owner, ident, pending["title"])
                if result:
                    await _manage(update, ctx, ident)
                else:
                    await _missing(update, ctx)
            else:
                result = db.rename_work_material(owner, ident, pending["title"])
                if result:
                    await _card(update, ctx, ident)
                else:
                    await _missing(update, ctx)
        elif op == "delete_section":
            if db.delete_work_section(owner, ident):
                await show_root(update, ctx)
            else:
                await _missing(update, ctx)
        elif op == "delete_material":
            material = db.get_work_material(owner, ident)
            if material and db.delete_work_material(owner, ident):
                await _show_section(update, ctx, material["section_id"])
            else:
                await _missing(update, ctx)
        elif op == "move_material":
            if db.move_work_material(owner, ident, pending["target"]):
                await _card(update, ctx, ident)
            else:
                await _missing(update, ctx)
        elif op == "shift_section":
            if not db.get_work_section(owner, ident):
                await _missing(update, ctx)
                return
            if not db.shift_work_section(owner, ident, pending["target"]):
                await _say(update, "Раздел уже находится с этого края списка.")
            await _manage(update, ctx, ident)
        else:
            await _expired(update)
    except ValueError as exc:
        # A duplicate section name or a concurrent edit can invalidate a draft.
        # Keep the input context so the user can go back and correct it.
        await _say(update, escape(str(exc)), _input_rows())


async def cancel(update, ctx):
    await show_root(update, ctx)


async def handle_callback(update, ctx, data):
    """Route only work callbacks; the parent has already answered the query."""
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != "work":
        return
    # Old inline buttons remain clickable after leaving this feature. Enter
    # its browsing mode even for an expired button, so the next user message
    # cannot accidentally become an ordinary library entry or a search query.
    # Keep an existing work wizard intact on stale confirmation attempts.
    if not str(ctx.user_data.get("state", "")).startswith("work:"):
        _reset(ctx)
    action = parts[1]
    try:
        if action == "root" and len(parts) == 3:
            await show_root(update, ctx, _id(parts[2], zero=True))
        elif action == "add" and len(parts) == 2:
            await _add_menu(update, ctx)
        elif action == "new" and len(parts) == 3 and parts[2] in {"0", "1"}:
            await _new_section(update, ctx, resume=parts[2] == "1")
        elif action == "name" and len(parts) == 2:
            await _new_section(update, ctx, restore=True)
        elif action == "pick" and len(parts) == 3:
            await _pick_section(update, ctx, _id(parts[2], zero=True))
        elif action == "target" and len(parts) == 3:
            await _content(update, ctx, _id(parts[2]))
        elif action == "content" and len(parts) == 2:
            await _content(update, ctx)
        elif action == "title" and len(parts) == 2:
            await _ask_material_title(update, ctx)
        elif action == "section" and len(parts) == 4:
            await _show_section(update, ctx, _id(parts[2]), _id(parts[3], zero=True))
        elif action == "manage" and len(parts) == 3:
            await _manage(update, ctx, _id(parts[2]))
        elif action == "card" and len(parts) == 4:
            await _card(update, ctx, _id(parts[2]), _id(parts[3], zero=True))
        elif action == "open" and len(parts) == 3:
            await _open_file(update, ctx, _id(parts[2]))
        elif action in {"rename_s", "rename_m"} and len(parts) == 3:
            await _rename(update, ctx, action[-1], _id(parts[2]))
        elif action == "move" and len(parts) == 4:
            await _move_picker(update, ctx, _id(parts[2]), _id(parts[3], zero=True))
        elif action == "move_to" and len(parts) == 4:
            await _confirm_action(update, ctx, "move_material", _id(parts[2]), _id(parts[3]))
        elif action in {"delete_s", "delete_m"} and len(parts) == 3:
            await _confirm_action(update, ctx, "delete_section" if action[-1] == "s" else "delete_material", _id(parts[2]))
        elif action == "shift" and len(parts) == 4 and parts[3] in {"-1", "1"}:
            await _confirm_action(update, ctx, "shift_section", _id(parts[2]), int(parts[3]))
        elif action == "confirm" and len(parts) == 3 and re.fullmatch(r"[0-9a-f]{16}", parts[2]):
            await _apply_confirmation(update, ctx, parts[2])
        elif action == "suggest" and len(parts) == 3 and re.fullmatch(r"[0-9a-f]{16}", parts[2]):
            draft = ctx.user_data.get("work_draft", {})
            nonce = str(ctx.user_data.get("work_suggestion", ""))
            if (ctx.user_data.get("state") == "work:material_title" and draft.get("owner") == _owner(update)
                    and nonce and secrets.compare_digest(nonce, parts[2]) and draft.get("material")):
                ctx.user_data.pop("work_suggestion", None)
                await _preview_material(update, ctx, draft["material"]["title"])
            else:
                await _expired(update)
        elif action == "back" and len(parts) == 2:
            back = ctx.user_data.pop("work_back", "work:root:0")
            ctx.user_data.pop("work_pending", None)
            await handle_callback(update, ctx, back)
        elif action == "cancel" and len(parts) == 2:
            await cancel(update, ctx)
        else:
            await _expired(update)
    except (ValueError, OverflowError):
        await _expired(update)


def _extract_material(msg):
    text = msg.text or msg.caption or ""
    kind, file_id, file_name = "text", None, None
    # Telegram animations can also populate document; prefer the real type.
    for candidate in ("animation", "document", "photo", "voice", "audio", "video", "video_note"):
        obj = getattr(msg, candidate, None)
        if obj:
            if candidate == "photo":
                obj = obj[-1]
            kind, file_id = candidate, obj.file_id
            file_name = getattr(obj, "file_name", None)
            break
    if not text and not file_id:
        raise ValueError("Пришли текст, ссылку или файл с подписью.")
    if getattr(msg, "media_group_id", None):
        raise ValueError("Пришли один файл отдельным сообщением: альбомы пока не поддерживаются.")
    if len(text) > 16000:
        raise ValueError("Текст слишком длинный. Пришли его документом или сократи до 16 000 символов.")
    urls = []
    for entity, value in {**msg.parse_entities(), **msg.parse_caption_entities()}.items():
        if entity.type == "text_link" and entity.url:
            urls.append(entity.url)
        elif entity.type == "url":
            urls.append(value)
    organized = organize(text, kind=kind, file_name=file_name, urls=urls)
    return {"text": text, "kind": kind, "file_id": file_id, "file_name": file_name,
            "title": organized["title"], "urls": organized["urls"],
            "source_chat_id": msg.chat_id, "source_message_id": msg.message_id}


async def handle_message(update, ctx):
    state = ctx.user_data.get("state", "")
    msg, owner, db = update.effective_message, _owner(update), _db(ctx)
    text = msg.text or ""
    try:
        if state == "work:section_name":
            draft = ctx.user_data.get("work_draft", {})
            if draft.get("owner") != owner:
                await _expired(update)
                return
            title = _title(text, 60)
            await _confirmation(update, ctx, op="add_section", title=title,
                resume=bool(draft.get("resume")), back="work:name",
                text=f"Создать раздел <b>«{escape(title)}»</b>?", label="✅ Создать раздел")
        elif state == "work:material_content":
            draft = ctx.user_data.get("work_draft", {})
            if draft.get("owner") != owner or not db.get_work_section(owner, draft.get("section_id", 0)):
                await _missing(update, ctx)
                return
            draft["material"] = _extract_material(msg)
            await _ask_material_title(update, ctx)
        elif state == "work:material_title":
            await _preview_material(update, ctx, _title(text, 100))
        elif state == "work:rename":
            data = ctx.user_data.get("work_input", {})
            if data.get("owner") != owner:
                await _expired(update)
                return
            is_section = data["kind"] == "s"
            obj = db.get_work_section(owner, data["id"]) if is_section else db.get_work_material(owner, data["id"])
            if not obj:
                await _missing(update, ctx)
                return
            title = _title(text, 60 if is_section else 100)
            await _confirmation(update, ctx, op="rename_section" if is_section else "rename_material",
                ident=data["id"], title=title, back=f"work:rename_{data['kind']}:{data['id']}",
                text=f"Переименовать <b>«{escape(obj['title'])}»</b> в <b>«{escape(title)}»</b>?")
        elif state == "work:confirm":
            await _say(update, "Проверь карточку выше и нажми «Сохранить», либо вернись назад для изменения.", _input_rows())
        else:
            await _say(update, "Чтобы сохранить материал здесь, выбери раздел или нажми «Добавить что-то нужное».", [
                [_button("➕ Добавить что-то нужное", "work:add")],
                [_button("⬅️ Работа рядом", "work:root:0"), _button("🏠 Главное меню", "home")],
            ])
    except ValueError as exc:
        await _say(update, escape(str(exc)), _input_rows())
