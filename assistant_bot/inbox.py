"""Automatic inbox processing outside the update/scheduler lock."""
import asyncio
from datetime import datetime, timezone
from html import escape
import re

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as K
from telegram.error import TelegramError

from . import panel
from .ai import AIError
from .extraction import extract, mode_for, ExtractionError
from .inbox_ai import classify


def enabled(data, owner):
    return bool(data['config'].yandex_api_key and data['config'].ai_model_uri) and data['store'].get_setting(owner, 'auto_inbox', 'on') == 'on'


def content(store, owner, ident):
    job, item = store.inbox_job(owner, ident), store.get_item(owner, ident)
    rows = [[B('📎 Исходный материал', callback_data=f'item:{ident}')], [B('🏠 Меню', callback_data='home')]]
    if not job or not item:
        return 'Материал недоступен.', [[B('🏠 Меню', callback_data='home')]]
    if job['status'] in ('pending', 'running'):
        rows.insert(0, [B('Обновить статус', callback_data=f'inbox:view:{ident}')])
        return '⏳ Разбираю материал: библиотека или дела. Исходник сохранён. Можно пользоваться другими разделами.', rows
    if job['status'] == 'failed':
        rows.insert(0, [B('Повторить разбор', callback_data=f'inbox:retry:{ident}')])
        return escape(job['error']), rows
    target = {'library': '📚 Библиотека', 'tasks': '✅ Мои дела', 'mixed': '📚 Библиотека + ✅ Мои дела'}[job['destination']]
    text = '<b>'+escape(item['title'])+'</b>\n'+target+'\n\n'+escape(job['summary'])
    for link in store.inbox_tasks(owner, ident):
        task = store.get_task(owner, link['task_id'])
        if task:
            text += f"\n\n• {escape(task['title'])} — ≈ {task['minutes']} мин"
            rows.insert(-2, [B('✏️ '+task['title'][:45], callback_data=f"plan:task:{task['id']}")])
    text += '\n\n<i>Разбор ИИ: проверь смысл. Время — приблизительная оценка активной работы.</i>'
    if job['status'] == 'done' and job['destination'] != 'library':
        rows.insert(-2, [B('↩ Вернуть в библиотеку', callback_data=f'inbox:undo:{ident}')])
    if job['raw_text']:
        rows.insert(-2, [B('Текст исходника', callback_data=f'inbox:raw:{ident}')])
    return text, rows


async def enqueue(update, ctx, item):
    data = ctx.application.bot_data
    owner, ident = update.effective_user.id, item['id']
    if not enabled(data, owner):
        return False
    data['store'].queue_inbox(owner, ident)
    text, rows = content(data['store'], owner, ident)
    handle = await panel.say(update, text, K(rows))
    data.setdefault('inbox_handles', {})[(owner, ident)] = handle
    await tick(ctx)
    return True


def quota(data, owner):
    cfg = data['config']
    if not enabled(data, owner):
        raise AIError('Авторазбор выключен. Исходник сохранён в библиотеке.')
    if not data['store'].reserve_ai_request(owner, datetime.now(timezone.utc).date().isoformat(),
                                          cfg.ai_daily_limit, cfg.ai_global_daily_limit):
        raise AIError('На сегодня исчерпан личный или общий лимит ИИ. Исходник сохранён; повтори разбор позже.')


async def tick(ctx):
    data = ctx.application.bot_data
    jobs = data.setdefault('inbox_tasks', {})
    for job in data['store'].pending_inbox():
        owner, ident = job['owner_id'], job['item_id']
        if len(jobs) >= 2:
            break
        if owner in jobs:
            continue
        data['store'].inbox_state(owner, ident, 'running')
        jobs[owner] = asyncio.create_task(run(ctx, owner, ident))


async def run(ctx, owner, ident):
    data, handle = ctx.application.bot_data, None
    store, cfg = data['store'], data['config']
    try:
        source = store.get_item(owner, ident)
        if not source:
            return
        text = source['text']
        if source.get('file_id'):
            mode = mode_for(source)
            if not mode:
                raise ExtractionError('Этот формат пока не разбираю автоматически. Оригинал сохранён в библиотеке.')
            if mode in ('image', 'voice'):
                quota(data, owner)
            extracted = await asyncio.wait_for(extract(ctx.bot, cfg, source), timeout=100)
            text = extracted['text']
        store.inbox_state(owner, ident, 'running', raw_text=text[:12000])
        if not store.get_item(owner, ident):
            return
        quota(data, owner)
        result = await asyncio.wait_for(classify(cfg, text, source['text'] if source.get('file_id') else ''), timeout=60)
        async with data['lock']:
            if enabled(data, owner):
                store.finish_inbox(owner, ident, result)
            else:
                store.inbox_state(owner, ident, 'failed', error='Авторазбор выключен. Исходник сохранён; дела не созданы.')
    except asyncio.CancelledError:
        store.inbox_state(owner, ident, 'failed', error='Разбор прерван. Исходник сохранён; можно повторить.')
        raise
    except (AIError, ExtractionError) as exc:
        store.inbox_state(owner, ident, 'failed', error=str(exc))
    except Exception:
        # Provider/transport exceptions can contain credential-bearing URLs.
        store.inbox_state(owner, ident, 'failed', error='Не удалось завершить разбор. Исходник сохранён; можно повторить.')
    finally:
        data.get('inbox_tasks', {}).pop(owner, None)
        handle = data.get('inbox_handles', {}).pop((owner, ident), None)
    if handle and store.get_item(owner, ident):
        text, rows = content(store, owner, ident)
        try:
            await handle.edit_text(text, parse_mode='HTML', reply_markup=K(rows))
        except TelegramError:
            pass  # Persistent result can always be opened from its source.


async def callback(update, ctx, value):
    store, owner = ctx.application.bot_data['store'], update.effective_user.id
    match = re.fullmatch(r'inbox:(view|retry|undo|raw):(\d{1,19})', value)
    if not match:
        return
    action, ident = match[1], int(match[2])
    job = store.inbox_job(owner, ident)
    if job and action == 'retry' and job['status'] == 'failed':
        if enabled(ctx.application.bot_data, owner):
            store.inbox_state(owner, ident, 'pending')
            await enqueue(update, ctx, store.get_item(owner, ident))
            return
    if job and action == 'undo':
        result = store.undo_inbox(owner, ident)
        if result:
            removed, kept = result
            await panel.say(update, f'Материал возвращён в библиотеку. Удалено созданных ИИ дел: {removed}. '
                f'Сохранено изменённых или начатых тобой дел: {kept}.', K([[B('Открыть материал', callback_data=f'item:{ident}')]]))
            return
    if job and action == 'raw':
        await panel.say(update, '<b>Извлечённый текст · возможны ошибки</b>\n\n'+escape(job['raw_text']),
                        K([[B('← Разбор', callback_data=f'inbox:view:{ident}')]]))
        return
    text, rows = content(store, owner, ident)
    await panel.say(update, text, K(rows))
