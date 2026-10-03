"""Review-before-index attachment workflow; jobs do not hold the scheduler lock."""
import asyncio
from datetime import datetime, timezone
from html import escape
from io import BytesIO
import json
import re
import secrets

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from telegram.error import TelegramError

from .extraction import ExtractionError, extract, mode_for
from .memory_store import normalized, query_terms
from .material_ai import summarize
from .ai import AIError
from . import panel, chat_cleanup


def clear(ctx):
    for key in tuple(ctx.user_data):
        if key.startswith('rec_'):
            ctx.user_data.pop(key, None)
    if str(ctx.user_data.get('state', '')).startswith('rec:'):
        ctx.user_data.pop('state', None)


def db(ctx):
    return ctx.application.bot_data['store']


def back(kind, ident):
    return 'item:' + str(ident) if kind == 'library' else f'work:card:{ident}:0'


def button_for(store, owner, kind, source):
    if not mode_for(source):
        return None
    row = store.extraction(owner, kind, source['id'])
    label = {'running': '⏳ Статус обработки', 'draft': '👀 Проверить распознавание',
             'accepted': '📝 Текст вложения'}.get(row['status'] if row else '', '🔎 Распознать содержимое')
    return Button(label, callback_data=f'rec:view:{kind}:{source["id"]}:0')


async def say(update, text, rows):
    return await panel.say(update,text,Keyboard(rows))


def pages(text):
    return [text[i:i + 1500] for i in range(0, len(text), 1500)] or ['']


def suggested_projects(store, owner, text):
    """Transparent keyword hints, using only this owner's project titles/goals."""
    content = normalized(text)
    scored = []
    for project in store.list_projects(owner, limit=1000):
        terms = query_terms(project['title'] + ' ' + project['goal'])
        score = sum(term in content for term in terms)
        if score >= 2 or (len(normalized(project['title'])) >= 4 and normalized(project['title']) in content):
            scored.append((score, project))
    return [project for _, project in sorted(scored, key=lambda pair: (-pair[0], -pair[1]['id']))[:3]]


def view_content(row, kind, ident, page=0):
    body = row['candidate'] if row['status'] == 'draft' else row['accepted']
    parts = pages(body)
    page = min(max(page, 0), len(parts) - 1)
    base = f'{kind}:{ident}'
    revision = row['revision']
    title = 'Проверь распознавание' if row['status'] == 'draft' else 'Текст вложения · сохранён'
    proposed_title=row.get('candidate_title','') if row['status']=='draft' else row.get('accepted_title','')
    text = (f'<b>{title}</b>\n{escape(proposed_title)}\n{escape(row["method"])} · {page + 1}/{len(parts)}\n\n'
            + escape(parts[page]) + '\n\n<i>' + escape(row['notice']) + '</i>')
    rows = []
    nav = []
    if page:
        nav.append(Button('←', callback_data=f'rec:view:{base}:{page - 1}'))
    if page + 1 < len(parts):
        nav.append(Button('→', callback_data=f'rec:view:{base}:{page + 1}'))
    if nav:
        rows.append(nav)
    if row['status'] == 'draft':
        text += '\n\nНовый текст ещё не участвует в поиске.'
        text += '\nСохраню также название; обновлю неизменённую карточку файла, добавленного прямо в проект.'
        rows.append([Button('✅ Верно, сохранить', callback_data=f'rec:accept:{base}:{revision}'),
                     Button('Не сохранять', callback_data=f'rec:discard:{base}:{revision}')])
    rows.append([Button('✏️ Исправить страницу', callback_data=f'rec:edit:{base}:{revision}:{page}')])
    rows.append([Button('✏️ Название', callback_data=f'rec:title:{base}:{revision}')])
    if row.get('source_text'):
        rows.append([Button('Полный текст распознавания', callback_data=f'rec:raw:{base}:0')])
    if row['accepted']:
        rows.extend([[Button('🗂 Связать с проектом', callback_data=f'mem:link:{base}:0')],
                     [Button('✅ Сделать делом', callback_data=f'plan:from:{base}'),
                      Button('⏰ Напомнить', callback_data=f'rec:remind:{base}')],
                     [Button('💬 Разобрать с ИИ', callback_data=f'plan:analyze:{base}')]])
    rows.extend([[Button('⬇️ Текст файлом', callback_data=f'rec:download:{base}:{revision}')],
                 [Button('История', callback_data=f'rec:history:{base}')],
                 [Button('← Оригинал', callback_data=back(kind, ident))]])
    return text, rows


async def view(update, ctx, kind, ident, page=0):
    owner = update.effective_user.id
    source = db(ctx).extraction_source(owner, kind, ident)
    if not source:
        await say(update, 'Материал недоступен.', [[Button('Меню', callback_data='home')]])
        return
    row = db(ctx).extraction(owner, kind, ident)
    if row and row['status'] in ('draft', 'accepted'):
        text, rows = view_content(row, kind, ident, page)
        if row['status']=='accepted':
            suggestions=suggested_projects(db(ctx),owner,row['accepted'])
            if suggestions:
                text+='\n\nВозможные проекты по совпадению слов:'
                rows += [[Button(p['title'][:50],callback_data=f'mem:attach:{p["id"]}:{kind}:{ident}')] for p in suggestions]
        await say(update, text, rows)
        return
    if row and row['status'] == 'running':
        await say(update, '⏳ Обрабатываю файл в фоне. Можно пользоваться другими разделами.',
                  [[Button('Обновить', callback_data=f'rec:view:{kind}:{ident}:0')]])
        return
    mode = mode_for(source)
    if not mode:
        await say(update, 'Этот формат пока не поддерживается.', [[Button('← Оригинал', callback_data=back(kind, ident))]])
        return
    descriptions = {
        'pdf': 'Прочитаю текстовый слой PDF локально: до 8 МБ, первые 20 страниц и 12 000 символов. Сканированные страницы нужно прислать как фото.',
        'text': 'Прочитаю TXT/MD в UTF-8 локально: до 8 МБ и 12 000 символов.',
        'image': 'Передам изображение в Yandex OCR, затем текст и подпись — модели DeepSeek для краткой выжимки и названия. До двух платных API-запросов из лимита бота. JPEG/PNG до 4 МБ и 20 мегапикселей. Оригинал и полный текст останутся отдельно; результат ИИ нужно проверить.',
        'voice': 'Передам эту запись в Yandex SpeechKit: голосовое Ogg Opus, один канал, до 30 секунд и 1 МБ. Это платный API-запрос из лимита бота.',
    }
    token = secrets.token_hex(8)
    ctx.user_data['rec_pending'] = dict(owner=owner, kind=kind, ident=ident, token=token)
    text = '<b>Извлечь содержание?</b>\n\n' + descriptions[mode] + '\n\nОригинал сохранится. Текст можно проверить и исправить перед добавлением в поиск.'
    if row and row['notice']:
        text += '\n\nПоследний результат: ' + escape(row['notice'])
    await say(update, text, [[Button('Начать распознавание', callback_data='rec:start:' + token)],
                            [Button('← Оригинал', callback_data=back(kind, ident))]])


async def start(update, ctx, token):
    owner = update.effective_user.id
    draft = ctx.user_data.get('rec_pending', {})
    if draft.get('owner') != owner or draft.get('token') != token:
        await say(update, 'Кнопка устарела. Открой материал заново.', [[Button('Меню', callback_data='home')]])
        return
    data = ctx.application.bot_data
    jobs = data.setdefault('extraction_tasks', {})
    if owner in jobs or len(jobs) >= 2:
        await say(update, 'Сейчас обрабатываются другие файлы. Повтори через минуту.', [[Button('← Материал', callback_data=back(draft['kind'], draft['ident']))]])
        return
    kind, ident = draft['kind'], draft['ident']
    source = db(ctx).extraction_source(owner, kind, ident)
    current = db(ctx).extraction(owner, kind, ident)
    if not source or not mode_for(source) or current and current['status'] in ('running', 'draft'):
        await view(update, ctx, kind, ident)
        return
    cfg = data['config']
    if mode_for(source) in ('image', 'voice'):
        if not cfg.yandex_api_key:
            await say(update, 'Yandex API-ключ для распознавания пока не настроен.', [[Button('← Оригинал', callback_data=back(kind, ident))]])
            return
        day = datetime.now(timezone.utc).date().isoformat()
        if not db(ctx).reserve_ai_request(owner, day, cfg.ai_daily_limit, cfg.ai_global_daily_limit):
            await say(update, 'На сегодня исчерпан личный или общий лимит ИИ-запросов.', [[Button('← Оригинал', callback_data=back(kind, ident))]])
            return
    pending = await say(update, '⏳ Обрабатываю файл. Оригинал сохранён, другие разделы доступны.',
                        [[Button('← Материал', callback_data=back(kind, ident))]])
    row = db(ctx).start_extraction(owner, kind, ident)
    if not row:
        return
    ctx.user_data.pop('rec_pending', None)
    jobs[owner] = ctx.application.create_task(run(ctx, owner, kind, ident, source, row['revision'], pending))


async def run(ctx, owner, kind, ident, source, revision, pending):
    data = ctx.application.bot_data
    try:
        try:
            result = await asyncio.wait_for(extract(ctx.bot, data['config'], source), timeout=100)
            result['source_text']=result['text']
            result['title']=source['title']
            if mode_for(source) in ('pdf','text') and not source.get('text','').strip():
                heading=next((line.strip() for line in result['text'].splitlines()
                              if len(line.strip())>=5 and not line.startswith('[Страница ')), '')
                if heading:
                    result['title']=heading[:80]
            if mode_for(source)=='image':
                async with data['lock']:
                    still_exists=db(ctx).extraction_source(owner,kind,ident)
                    cfg=data['config']
                    allowed=bool(still_exists) and db(ctx).reserve_ai_request(owner,datetime.now(timezone.utc).date().isoformat(),cfg.ai_daily_limit,cfg.ai_global_daily_limit)
                if not still_exists:
                    return
                if allowed:
                    try:
                        brief=await asyncio.wait_for(summarize(cfg,result['text'],source.get('text','')),timeout=55)
                        result.update(brief)
                        result['method']='OCR → выжимка ИИ'
                        result['notice']='Интерпретация ИИ: проверь по цитатам и оригиналу. Имена, цифры и выводы могут быть ошибочными. '+result.get('notice','')
                    except (AIError,TimeoutError):
                        result['notice']='ИИ-разбор не удался. Показан полный текст OCR, без выжимки. '+result.get('notice','')
                else:
                    result['notice']='Для ИИ-разбора не осталось дневной квоты. Показан полный текст OCR. '+result.get('notice','')
        except ExtractionError as exc:
            result = dict(failed=True, notice=str(exc))
        except asyncio.CancelledError:
            db(ctx).finish_extraction(owner, kind, ident, revision, failed=True, notice='Обработка прервана. Можно повторить.')
            raise
        except (TimeoutError, TelegramError):
            result = dict(failed=True, notice='Не удалось получить результат вовремя. Оригинал сохранён; попробуй позже.')
        except Exception:
            result = dict(failed=True, notice='Не удалось обработать файл. Оригинал сохранён; попробуй позже.')
        async with data['lock']:
            row = db(ctx).finish_extraction(owner, kind, ident, revision, **result)
        if not row:
            return  # Deleted while processing: never resurrect or deliver its content.
        if row['status'] == 'draft':
            text, rows = view_content(row, kind, ident)
        else:
            text, rows = escape(row['notice']), [[Button('Повторить / статус', callback_data=f'rec:view:{kind}:{ident}:0')]]
        try:
            await pending.edit_text(text, parse_mode='HTML', reply_markup=Keyboard(rows))
        except TelegramError:
            pass  # Persistent result remains available in the source card.
    finally:
        if data.get('extraction_tasks', {}).get(owner) is asyncio.current_task():
            data['extraction_tasks'].pop(owner, None)


async def callback(update, ctx, data):
    owner = update.effective_user.id
    if re.fullmatch(r'rec:start:[a-f0-9]{16}', data):
        await start(update, ctx, data.split(':')[2])
        return
    match = re.fullmatch(r'rec:(view|accept|discard|edit|title|raw|download|history|version|remind):(library|work):([1-9][0-9]{0,15})(?::([0-9]{1,8}))?(?::([0-9]{1,3}))?', data)
    if not match:
        return
    action, kind, number, arg, page = match.groups()
    ident = int(number)
    if not db(ctx).extraction_source(owner, kind, ident):
        await view(update, ctx, kind, ident)
        return
    if action == 'view':
        clear(ctx)
        await view(update, ctx, kind, ident, int(arg or 0))
        return
    row = db(ctx).extraction(owner, kind, ident)
    if not row:
        await view(update, ctx, kind, ident)
        return
    if action=='raw':
        parts=pages(row.get('source_text') or row['candidate'] or row['accepted'])
        index=min(int(arg or 0),len(parts)-1)
        await say(update,f'<b>Полный текст OCR / извлечения · {index+1}/{len(parts)}</b>\nЭто автоматическое извлечение, возможны ошибки.\n\n'+escape(parts[index]),
                  [[Button('←',callback_data=f'rec:raw:{kind}:{ident}:{max(0,index-1)}'),Button('→',callback_data=f'rec:raw:{kind}:{ident}:{min(len(parts)-1,index+1)}')],
                   [Button('← Выжимка / запись',callback_data=f'rec:view:{kind}:{ident}:0')]])
        return
    if action == 'history':
        with db(ctx)._lock:
            versions = db(ctx)._conn.execute('''SELECT revision FROM extraction_versions
                WHERE owner_id=? AND source_kind=? AND source_id=? ORDER BY revision DESC LIMIT 20''', (owner, kind, ident)).fetchall()
        await say(update, '<b>Последние сохранённые версии</b>\nИсходный файл остаётся в карточке материала.',
                  [[Button(f'Версия {v[0]}', callback_data=f'rec:version:{kind}:{ident}:{v[0]}:0')] for v in versions]
                  + [[Button('← Текущий текст', callback_data=f'rec:view:{kind}:{ident}:0')]])
        return
    if action == 'version' and arg:
        with db(ctx)._lock:
            old = db(ctx)._conn.execute('''SELECT snapshot FROM extraction_versions WHERE owner_id=? AND source_kind=? AND source_id=? AND revision=?''', (owner, kind, ident, int(arg))).fetchone()
        if old:
            body = pages(json.loads(old[0])['accepted'])
            index = min(int(page or 0), len(body) - 1)
            await say(update, f'<b>История · версия {arg} · {index + 1}/{len(body)}</b>\n\n' + escape(body[index]),
                      [[Button('←', callback_data=f'rec:version:{kind}:{ident}:{arg}:{max(0,index-1)}'),
                        Button('→', callback_data=f'rec:version:{kind}:{ident}:{arg}:{min(len(body)-1,index+1)}')],
                       [Button('Текущий текст', callback_data=f'rec:view:{kind}:{ident}:0')]])
        return
    if action == 'remind' and row['accepted']:
        from .bot import choose_time
        source = db(ctx).extraction_source(owner, kind, ident)
        await choose_time(update, ctx, 'Вернуться к материалу: ' + source['title'])
        return
    if arg is None or row['revision'] != int(arg):
        await say(update, 'Текст изменился. Открой актуальную версию.', [[Button('Открыть', callback_data=f'rec:view:{kind}:{ident}:0')]])
        return
    if action == 'accept':
        accepted = db(ctx).accept_extraction(owner, kind, ident, int(arg))
        if accepted:
            await say(update, '✅ Сохранил название и текст для поиска. Исходный файл остался. Ранее отредактированные записи проектов не перезаписывались.',
                      [[Button('Поиск по памяти', callback_data='mem:search')]])
        await view(update, ctx, kind, ident)
    elif action == 'discard':
        db(ctx).discard_extraction(owner, kind, ident, int(arg))
        await say(update, 'Черновик закрыт. Оригинал и ранее сохранённый текст остались.', [[Button('← Оригинал', callback_data=back(kind, ident))]])
    elif action=='title' and row['status'] in ('draft','accepted'):
        clear(ctx)
        ctx.user_data.update(state='rec:title',rec_edit=dict(owner=owner,kind=kind,ident=ident,revision=int(arg),page=0))
        await say(update,'Пришли понятное название до 100 символов. Оно применится после подтверждения.',[[Button('Отмена',callback_data=f'rec:view:{kind}:{ident}:0')]])
    elif action == 'edit' and row['status'] in ('draft', 'accepted'):
        clear(ctx)
        body = row['candidate'] if row['status'] == 'draft' else row['accepted']
        index = min(int(page or 0), len(pages(body)) - 1)
        ctx.user_data.update(state='rec:edit', rec_edit=dict(owner=owner, kind=kind, ident=ident, revision=int(arg), page=index))
        await say(update, f'Пришли исправленный текст страницы {index+1} одним сообщением (до 3500 символов). Остальные страницы сохранятся. Затем покажу результат перед сохранением.\n\n' + escape(pages(body)[index]),
                  [[Button('Отмена', callback_data=f'rec:view:{kind}:{ident}:{index}')]])
    elif action == 'download' and row['status'] in ('draft', 'accepted'):
        body = row['candidate'] if row['status'] == 'draft' else row['accepted']
        await chat_cleanup.send_document(update,ctx,BytesIO(body.encode('utf-8')), filename=f'material-{ident}-text.txt')


async def message(update, ctx):
    draft = ctx.user_data.get('rec_edit', {})
    owner = update.effective_user.id
    if draft.get('owner') != owner:
        clear(ctx)
        return
    text = update.effective_message.text or ''
    if not text.strip() or len(text) > 3500:
        await say(update, 'Пришли текст страницы от 1 до 3500 символов. /cancel — отмена.', [])
        return
    kind, ident, revision = draft['kind'], draft['ident'], draft['revision']
    row = db(ctx).extraction(owner, kind, ident)
    if not row or row['revision'] != revision:
        clear(ctx)
        await view(update, ctx, kind, ident)
        return
    if ctx.user_data.get('state')=='rec:title':
        try:
            db(ctx).title_extraction(owner,kind,ident,revision,text)
        except ValueError as exc:
            await say(update,escape(str(exc)),[])
            return
        clear(ctx)
        await view(update,ctx,kind,ident)
        return
    parts = pages(row['candidate'] if row['status'] == 'draft' else row['accepted'])
    parts[draft['page']] = text
    if len(''.join(parts)) > 12000:
        await say(update, 'Общий текст превышает 12 000 символов. Сократи исправленную страницу.', [])
        return
    db(ctx).edit_extraction(owner, kind, ident, revision, ''.join(parts))
    clear(ctx)
    await view(update, ctx, kind, ident, draft['page'])
