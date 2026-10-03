"""Project context and evidence-first recall. All writes are explicit and scoped."""
from datetime import datetime
from difflib import unified_diff
from html import escape
from io import BytesIO
import secrets
from zoneinfo import ZoneInfo
from pathlib import Path

from .planner import db,zone,button,say,number
from .memory_store import KINDS,query_terms,normalized
from .answer_pages import paginate_answer
from .knowledge import organize
from . import recognition, chat_cleanup

HOME=[[button('← Проекты','mem:home:0'),button('🏠 Меню','home')]]
CANCEL=[[button('Отмена','mem:home:0')]]
CASE_FIELDS=[('object','Какой объект или ситуация? Например: камера у входа. До 200 символов.'),
             ('symptoms','Что наблюдалось? Симптомы и условия — до 800 символов.'),
             ('checks','Что проверили и что показали проверки? До 800 символов.'),
             ('actions','Что попробовали сделать? До 800 символов.'),
             ('outcome','Какой результат? Если ещё неизвестен, так и напиши. До 800 символов.')]
CASE_LABELS={'object':'Объект / ситуация','symptoms':'Наблюдения','checks':'Проверки','actions':'Действия','outcome':'Результат со слов пользователя'}


def clear(ctx):
    for key in tuple(ctx.user_data):
        if key.startswith('mem_'):
            ctx.user_data.pop(key,None)
    if str(ctx.user_data.get('state','')).startswith('mem:'):
        ctx.user_data.pop('state',None)


def reset(ctx):
    clear(ctx)
    ctx.user_data['state']='mem:browse'


def dated(ctx,owner,timestamp):
    return datetime.fromtimestamp(timestamp,ZoneInfo(zone(ctx,owner))).strftime('%d.%m.%Y %H:%M')


def case_body(payload):
    return '\n\n'.join(f'{label}: {payload.get(key, "Не указано")}' for key,label in CASE_LABELS.items())


async def missing(update):
    await say(update,'Проект или запись недоступны.',HOME)


async def home(update,ctx,offset=0):
    reset(ctx)
    owner=update.effective_user.id
    total=db(ctx).project_count(owner)
    offset=min(offset,max(0,(total-1)//8*8))
    projects=db(ctx).list_projects(owner,offset=offset)
    rows=[[button(p['title'][:55],f"mem:project:{p['id']}")] for p in projects]
    nav=[]
    if offset:
        nav.append(button('← Назад',f'mem:home:{offset-8}'))
    if offset+8<total:
        nav.append(button('Далее →',f'mem:home:{offset+8}'))
    if nav:
        rows.append(nav)
    rows += [[button('➕ Проект','mem:new'),button('🔎 Поиск по памяти','mem:search')],[button('🏠 Меню','home')]]
    await say(update,'<b>Мои проекты и опыт</b>\n\nЦель, материалы, решения и открытые вопросы — в одном месте. Проектом может быть поездка, учёба, ремонт или любая личная задача.',rows)


async def confirm(update,ctx,op,payload,text,label='✅ Сохранить'):
    token=secrets.token_hex(8)
    ctx.user_data.update(state='mem:confirm',mem_pending=dict(owner=update.effective_user.id,token=token,op=op,payload=payload))
    await say(update,text,[[button(label,f'mem:confirm:{token}')],*CANCEL])


async def project(update,ctx,ident):
    reset(ctx)
    owner=update.effective_user.id
    p=db(ctx).get_project(owner,ident)
    if not p:
        await missing(update)
        return
    ctx.user_data['mem_active_project']=ident
    # Summary is source excerpts, never an invented causal explanation.
    text=f"<b>{escape(p['title'])}</b>\n\n<b>Цель</b>\n{escape(p['goal'])}\n"
    entries=db(ctx).memory_entries(owner,ident,limit=1000)
    for kind,label in [('decision','Последние решения'),('question','Открытые вопросы'),('case','Последние случаи'),('idea','Идеи')]:
        values=[e for e in entries if e['kind']==kind][:2]
        if values:
            text+='\n<b>'+label+'</b>\n'+'\n'.join(f"• #{e['id']} {escape(e['body'][:100])}" for e in values)+'\n'
    count=db(ctx).memory_count(owner,ident)
    text+=f'\nЗаписей: {count}. Ниже — доступ к полным записям и их источникам.'
    rows=[]
    tasks=db(ctx).project_tasks(owner,ident,limit=5)
    active=next((t for t in tasks if t['status']=='active'),None)
    if active:
        checkpoint=db(ctx).get_checkpoint(owner,active['id'])
        if checkpoint:
            text+='\n\n<b>Сохранённый следующий шаг</b>\n'+escape(checkpoint['next_step'][:160])
    for t in tasks:
        rows.append([button(('✓ ' if t['status']=='done' else '○ ')+t['title'][:48],f"plan:task:{t['id']}")])
    for e in entries[:3]:
        rows.append([button(f"#{e['id']} · {e['title'][:42]}",f"mem:entry:{e['id']}:0")])
    rows += [[button('Все записи',f'mem:entries:{ident}:0'),button('➕ Запись',f'mem:add:{ident}')],
             [button('📎 Добавить файл',f'mem:file:{ident}')],
             [button('🧩 Новый случай',f'mem:case:{ident}'),button('Связать дело',f'mem:tasks:{ident}:0')],
             [button('📄 Памятка',f'mem:brief:{ident}'),button('Удалить проект',f'mem:deleteproject:{ident}')],*HOME]
    await say(update,text,rows)


async def entries(update,ctx,ident,offset):
    reset(ctx)
    owner=update.effective_user.id
    if not db(ctx).get_project(owner,ident):
        await missing(update)
        return
    ctx.user_data['mem_active_project']=ident
    total=db(ctx).memory_count(owner,ident)
    offset=min(offset,max(0,(total-1)//8*8))
    values=db(ctx).memory_entries(owner,ident,offset=offset)
    rows=[[button(f"{KINDS[e['kind']]} · {e['title'][:42]}",f"mem:entry:{e['id']}:0")] for e in values]
    nav=[]
    if offset:
        nav.append(button('← Назад',f'mem:entries:{ident}:{offset-8}'))
    if offset+8<total:
        nav.append(button('Далее →',f'mem:entries:{ident}:{offset+8}'))
    await say(update,f'<b>Записи проекта</b> · {total}',rows+([nav] if nav else [])+[[button('← Проект',f'mem:project:{ident}')]])


async def entry(update,ctx,ident,page=0,revision=None):
    reset(ctx)
    owner=update.effective_user.id
    current=db(ctx).get_memory_entry(owner,ident)
    e=db(ctx).memory_version(owner,ident,revision) if revision else current
    if not e or not current:
        await missing(update)
        return
    ctx.user_data['mem_active_project']=e['project_id']
    pages=paginate_answer(e['body'],limit=2300)
    page=min(page,len(pages)-1)
    text=f"<b>{escape(e['title'])}</b>\n{KINDS[e['kind']]} · {dated(ctx,owner,e['updated_at'])}\nВерсия {e['revision']} · запись #{ident}\n\n{escape(pages[page])}"
    if e['kind']=='case':
        text+='\n\n<i>Результат после действия не доказывает, что именно это действие устранило причину.</i>'
    rows=[]
    prefix=f'mem:version:{ident}:{revision}' if revision else f'mem:entry:{ident}'
    nav=[]
    if page:
        nav.append(button('← Страница',f'{prefix}:{page-1}'))
    if page+1<len(pages):
        nav.append(button('Страница →',f'{prefix}:{page+1}'))
    if nav:
        rows.append(nav)
    attachment=db(ctx).memory_attachment(owner,ident)
    if attachment:
        text+='\n\n📎 '+escape((attachment.get('file_name') or 'Исходный файл')[:150])
        rows.append([button('📎 Скачать / открыть файл',f'mem:original:{ident}')])
    if e['source_kind']:
        material=db(ctx).memory_source(owner,e['source_kind'],e['source_id'])
        if material:
            target=f"item:{material['id']}" if e['source_kind']=='library' else f"work:card:{material['id']}:0"
            rows.append([button('Открыть оригинал' if not attachment else 'Карточка материала',target)])
            recognize=recognition.button_for(db(ctx),owner,e['source_kind'],material)
            if recognize:
                rows.append([recognize])
            extracted=db(ctx).extraction(owner,e['source_kind'],e['source_id'])
            if extracted and extracted['accepted']:
                rows.append([button('Обновить запись по разбору',f'mem:refresh:{ident}')])
        else:
            text+='\nМатериал удалён из библиотеки; файл проекта сохранён.' if attachment else '\nОригинал недоступен; сохранённый текст записи остался.'
    if revision:
        history=[]
        if revision>1:
            history.append(button('← Предыдущая версия',f'mem:version:{ident}:{revision-1}:0'))
        if revision<current['revision']:
            history.append(button('Следующая версия →',f'mem:version:{ident}:{revision+1}:0'))
        rows += ([history] if history else [])+[[button('Текущая запись',f'mem:entry:{ident}:0')]]
    else:
        rows += [[button('✏️ Исправить',f'mem:edit:{ident}'),button('История',f'mem:version:{ident}:{e["revision"]}:0')],
                 [button('✏️ Название',f'mem:rename:{ident}')],
                 [button('Сделать делом',f'plan:from:memory:{ident}'),button('⏰ Напомнить',f'mem:remind:{ident}')]]
        if e['kind']=='case':
            rows.append([button('Что в итоге помогло?',f'mem:outcome:{ident}'),button('Похожие случаи',f'mem:similar:{ident}')])
        rows.append([button('Удалить запись',f'mem:deleteentry:{ident}')])
    rows.append([button('← Проект',f'mem:project:{e["project_id"]}')])
    await say(update,text,rows)


async def search(update,ctx,query=None,*,case_id=None):
    reset(ctx)
    if not query:
        ctx.user_data['state']='mem:search'
        await say(update,'<b>Поиск по личной памяти</b>\n\nОпиши, что ищешь: «Что пробовали, когда пропадала камера?»\n\nСейчас ищу по словам в текстах, подписях и проектных записях. Даты событий из вопроса автоматически не определяются; нераспознанные вложения не читаются.',HOME)
        return
    if len(query)>500:
        ctx.user_data['state']='mem:search'
        await say(update,'Сократи запрос до 500 символов.',HOME)
        return
    values=db(ctx).memory_search(update.effective_user.id,query,only_cases=case_id is not None,exclude_id=case_id or 0)
    text='<b>Найдено в твоих записях</b>\nСовпадения по словам; это выдержки из источников, не вывод ИИ.\n'
    if case_id:
        text+='Для похожих случаев сравни условия и проверки: совпадения слов не означают одинаковую причину.\n'
    rows=[]
    terms=query_terms(query)
    for index,e in enumerate(values,1):
        body=e['body']
        positions=[normalized(body).find(t) for t in terms if t in normalized(body)]
        start=max(0,min(positions)-50) if positions else 0
        snippet=body[start:start+120]
        kind=KINDS.get(e['kind'],'Исходный материал')
        text+=f"\n<b>{index}. {escape(e['title'][:50])}</b>\n{kind} · {dated(ctx,update.effective_user.id,e['created_at'])}\n{escape(snippet)}…\n"
        target=f"item:{e['id']}" if e['source']=='library' else f"work:card:{e['id']}:0" if e['source']=='work' else f"mem:entry:{e['id']}:0"
        rows.append([button(f'{index}. Открыть источник',target)])
    if not values:
        text+='\nПодходящих текстов не нашлось. Попробуй название, предмет или симптом. Это не означает, что сведений нет внутри сохранённых файлов.'
    await say(update,text,rows+[[button('Другой запрос','mem:search')],*HOME])


async def resume(update,ctx,name):
    reset(ctx)
    terms=query_terms(name)
    projects=[p for p in db(ctx).list_projects(update.effective_user.id,limit=1000)
              if terms and all(term in normalized(p['title']) for term in terms)]
    if len(projects)==1:
        await project(update,ctx,projects[0]['id'])
    else:
        rows=[[button(p['title'][:55],f"mem:project:{p['id']}")] for p in projects[:8]]
        await say(update,'Уточни проект:' if projects else 'Не нашёл проект по этому названию. Выбери его из списка.',rows+HOME)


async def pick_project(update,ctx,kind,ident,offset=0):
    reset(ctx)
    owner=update.effective_user.id
    if not db(ctx).memory_source(owner,kind,ident):
        await missing(update)
        return
    total=db(ctx).project_count(owner)
    rows=[[button(p['title'][:48],f"mem:attach:{p['id']}:{kind}:{ident}")] for p in db(ctx).list_projects(owner,offset=offset)]
    if offset:
        rows.append([button('← Назад',f'mem:link:{kind}:{ident}:{max(0,offset-8)}')])
    if offset+8<total:
        rows.append([button('Далее →',f'mem:link:{kind}:{ident}:{offset+8}')])
    if not total:
        rows.append([button('Создать проект','mem:new')])
    await say(update,'С каким проектом связать материал? Текст останется доступен вместе с оригиналом.',rows+HOME)


async def kind_picker(update,ctx,project_id,source_kind=None,source_id=None):
    reset(ctx)
    owner=update.effective_user.id
    if not db(ctx).get_project(owner,project_id) or (source_kind and not db(ctx).memory_source(owner,source_kind,source_id)):
        await missing(update)
        return
    ctx.user_data['mem_record']=dict(project_id=project_id,source_kind=source_kind,source_id=source_id)
    token=secrets.token_hex(8)
    ctx.user_data['mem_choice']=token
    rows=[[button(label,f'mem:kind:{token}:{kind}')] for kind,label in KINDS.items() if kind!='case']
    await say(update,'<b>Какой это тип сведений?</b>\n\nРазделим то, что наблюдали, сведения из инструкции и предположения. Метку выбираешь ты; бот не проверяет достоверность источника.',rows+CANCEL)


async def message(update,ctx):
    text=(update.effective_message.text or '').strip()
    state=ctx.user_data.get('state')
    try:
        msg=update.effective_message
        media=None
        for kind in ('document','photo','voice','audio','video','animation','video_note'):
            value=getattr(msg,kind,None)
            if value:
                media=(kind,value[-1] if kind=='photo' else value)
                break
        if state in ('mem:browse','mem:attachment') and media:
            owner=update.effective_user.id
            project_id=ctx.user_data.get('mem_active_project')
            if not db(ctx).get_project(owner,project_id or 0):
                await say(update,'Сначала открой нужный проект и нажми «Добавить файл».',HOME)
                return
            kind,media_object=media
            name=getattr(media_object,'file_name',None)
            caption=msg.caption or ''
            meta=organize(caption,kind=kind,file_name=name)
            if not caption and name:
                meta['title']=Path(name).stem.replace('_',' ').replace('-',' ')[:100] or name[:100]
            item=db(ctx).add_item(owner,text=caption,kind=kind,file_id=media_object.file_id,file_name=name,
                source_chat_id=msg.chat_id,source_message_id=msg.message_id,**meta)
            record=db(ctx).add_memory_entry(owner,project_id,'material',item['title'],
                caption or 'Файл сохранён в проекте. Открой оригинал или создай разбор кнопкой ниже.',
                source_kind='library',source_id=item['id'],payload={'attachment_placeholder':True})
            await say(update,'📎 Сохранил сам файл и его карточку в проект. Название можно изменить; разбор появится отдельно после обработки.',[])
            await entry(update,ctx,record['id'])
            return
        if state=='mem:search':
            await search(update,ctx,text)
        elif state=='mem:project_title':
            if not text or len(text)>80:
                raise ValueError('Название — от 1 до 80 символов.')
            ctx.user_data.update(state='mem:project_goal',mem_title=text)
            await say(update,'Какой результат хочешь получить? Цель проекта — до 500 символов.',CANCEL)
        elif state=='mem:project_goal':
            if not text or len(text)>500:
                raise ValueError('Опиши цель до 500 символов.')
            await confirm(update,ctx,'project',dict(title=ctx.user_data['mem_title'],goal=text),
                          f"Создать проект <b>{escape(ctx.user_data['mem_title'])}</b>?\n\n{escape(text)}")
        elif state=='mem:record_title':
            if not text or len(text)>100:
                raise ValueError('Название записи — до 100 символов.')
            ctx.user_data['mem_record']['title']=text
            ctx.user_data['state']='mem:record_body'
            await say(update,'Напиши сведения, решение с причиной или открытый вопрос. До 3500 символов.',CANCEL)
        elif state=='mem:record_body':
            if not text or len(text)>3500:
                raise ValueError('Текст — от 1 до 3500 символов.')
            payload=dict(ctx.user_data['mem_record'],body=text)
            await confirm(update,ctx,'record',payload,f"<b>{escape(payload['title'])}</b>\n{KINDS[payload['kind']]}\n\n{escape(text[:1300])}")
        elif state=='mem:case':
            draft=ctx.user_data['mem_case']
            index=draft['index']
            key,_=CASE_FIELDS[index]
            limit=200 if index==0 else 800
            if not text or len(text)>limit:
                raise ValueError(f'Напиши от 1 до {limit} символов.')
            draft['payload'][key]=text
            draft['index']+=1
            if draft['index']<len(CASE_FIELDS):
                await say(update,CASE_FIELDS[draft['index']][1],CANCEL)
            else:
                payload=dict(project_id=draft['project_id'],kind='case',title=draft['payload']['object'][:100],body=case_body(draft['payload']),payload=draft['payload'])
                await confirm(update,ctx,'record',payload,'<b>Сохранить случай?</b>\n\n'+escape(payload['body'][:1300])+'\n\nСвязь результата и действия не считается доказанной причиной.')
        elif state=='mem:rename':
            if not text or len(text)>100:
                raise ValueError('Название — от 1 до 100 символов.')
            current=ctx.user_data['mem_edit']
            await confirm(update,ctx,'revise',dict(ident=current['id'],revision=current['revision'],body=current['body'],title=text),
                          f'Название: <b>{escape(text)}</b>\nФайл и текст записи сохранятся.')
        elif state in ('mem:edit','mem:outcome'):
            if not text or len(text)>3500:
                raise ValueError('Напиши текст до 3500 символов.')
            current=ctx.user_data['mem_edit']
            payload=dict(ident=current['id'],revision=current['revision'],body=text)
            if state=='mem:outcome':
                details=dict(current['payload'],outcome=text)
                payload.update(body=case_body(details),payload=details)
            elif current['kind']=='case':
                # Generic edits preserve the prose, but cannot silently leave stale structured fields.
                payload['payload']={}
            diff='\n'.join(unified_diff(current['body'].splitlines(),payload['body'].splitlines(),fromfile='Было',tofile='Станет',lineterm=''))
            await confirm(update,ctx,'revise',payload,'<b>Сохранить новую версию?</b>\nПредыдущий текст останется в истории.\n\n'+escape(diff[:1400]))
        else:
            await say(update,'Выбери проект или поиск. Этот текст не сохранён автоматически.',HOME)
    except ValueError as exc:
        await say(update,escape(str(exc)),CANCEL)


async def callback(update,ctx,data):
    owner=update.effective_user.id
    if not str(ctx.user_data.get('state','')).startswith('mem:'):
        reset(ctx)
    parts=data.split(':')
    try:
        action=parts[1]
        if action=='home' and len(parts)==3:
            await home(update,ctx,number(parts[2]))
        elif action=='new' and len(parts)==2:
            reset(ctx)
            ctx.user_data['state']='mem:project_title'
            await say(update,'Как называется проект? Например, «Выставка», «Поездка» или «Изучаю Python». До 80 символов.',CANCEL)
        elif action=='search' and len(parts)==2:
            await search(update,ctx)
        elif action=='similar' and len(parts)==3:
            e=db(ctx).get_memory_entry(owner,number(parts[2]))
            if not e or e['kind']!='case':
                await missing(update)
                return
            await search(update,ctx,(e['payload'].get('object',e['title'])+' '+e['payload'].get('symptoms',''))[:500],case_id=e['id'])
        elif action=='project' and len(parts)==3:
            await project(update,ctx,number(parts[2]))
        elif action=='file' and len(parts)==3:
            ident=number(parts[2])
            if not db(ctx).get_project(owner,ident):
                await missing(update)
                return
            reset(ctx)
            ctx.user_data.update(state='mem:attachment',mem_active_project=ident)
            await say(update,'Пришли файл или фото с подписью. Сохраню оригинал в этот проект и создам карточку с названием. Подпись поможет понять, что для тебя важно.',[[button('← Проект',f'mem:project:{ident}')]])
        elif action=='original' and len(parts)==3:
            attachment=db(ctx).memory_attachment(owner,number(parts[2]))
            if not attachment:
                await missing(update)
                return
            methods={'document':'send_document','photo':'send_photo','voice':'send_voice','audio':'send_audio','video':'send_video','animation':'send_animation','video_note':'send_video_note'}
            method=methods.get(attachment['kind'])
            if method:
                await chat_cleanup.send_original(ctx,owner,attachment['kind'],attachment['file_id'],attachment.get('file_name'))
        elif action=='refresh' and len(parts)==3:
            current=db(ctx).get_memory_entry(owner,number(parts[2]))
            extracted=db(ctx).extraction(owner,current['source_kind'],current['source_id']) if current else None
            if not current or not extracted or not extracted['accepted']:
                await missing(update)
                return
            await confirm(update,ctx,'revise',dict(ident=current['id'],revision=current['revision'],body=extracted['accepted'],title=extracted['accepted_title'] or current['title']),
                          '<b>Обновить запись проекта по сохранённому разбору?</b>\nФайл и предыдущая версия останутся.\n\n'+escape(extracted['accepted'][:1300]))
        elif action=='entries' and len(parts)==4:
            await entries(update,ctx,number(parts[2]),number(parts[3]))
        elif action=='entry' and len(parts)==4:
            await entry(update,ctx,number(parts[2]),number(parts[3]))
        elif action=='version' and len(parts)==5:
            await entry(update,ctx,number(parts[2]),number(parts[4]),revision=number(parts[3]))
        elif action=='add' and len(parts)==3:
            await kind_picker(update,ctx,number(parts[2]))
        elif action=='link' and len(parts)==5:
            await pick_project(update,ctx,parts[2],number(parts[3]),number(parts[4]))
        elif action=='attach' and len(parts)==5:
            await kind_picker(update,ctx,number(parts[2]),parts[3],number(parts[4]))
        elif action=='kind' and len(parts)==4:
            draft=ctx.user_data.get('mem_record')
            if not draft or ctx.user_data.get('mem_choice')!=parts[2] or parts[3] not in KINDS or parts[3]=='case':
                raise ValueError('Выбор устарел. Открой действие заново.')
            ctx.user_data.pop('mem_choice',None)
            draft['kind']=parts[3]
            if draft['source_kind']:
                original=db(ctx).memory_source(owner,draft['source_kind'],draft['source_id'])
                if not original:
                    await missing(update)
                    return
                payload=dict(draft,title=original['title'][:100],body=(original['text'] or f"Вложение: {original.get('file_name') or original['kind']}. Содержание не распознано.")[:12000])
                await confirm(update,ctx,'record',payload,f"Связать <b>{escape(payload['title'])}</b> с проектом?\nТип: {KINDS[payload['kind']]}\nОригинал сохранится. В память попадёт текст или подпись, до 12 000 символов.")
            else:
                ctx.user_data['state']='mem:record_title'
                await say(update,'Короткое название записи — до 100 символов.',CANCEL)
        elif action=='case' and len(parts)==3:
            ident=number(parts[2])
            if not db(ctx).get_project(owner,ident):
                await missing(update)
                return
            reset(ctx)
            ctx.user_data.update(state='mem:case',mem_case=dict(project_id=ident,index=0,payload={}))
            await say(update,CASE_FIELDS[0][1],CANCEL)
        elif action in ('edit','outcome','remind','rename') and len(parts)==3:
            current=db(ctx).get_memory_entry(owner,number(parts[2]))
            if not current:
                await missing(update)
                return
            reset(ctx)
            if action=='remind':
                ctx.user_data.update(state='reminder_time',reminder_text=f"Вернуться к записи #{current['id']}: {current['title']} — /projects")
                await say(update,'Когда вернуться к записи? Напиши «завтра в 09:00» или «через час».',[[button('Отмена','home')]])
            else:
                if action=='outcome' and current['kind']!='case':
                    raise ValueError('Это не карточка случая.')
                if action=='edit' and current['kind']=='case':
                    raise ValueError('Для случая используй «Что в итоге помогло?». Новые проверки и действия можно сохранить отдельным случаем.')
                ctx.user_data.update(state='mem:'+action,mem_edit=current)
                await say(update,'Напиши новое название до 100 символов.' if action=='rename' else 'Что в итоге помогло и что изменилось? Неизвестную причину можно оставить неизвестной.' if action=='outcome' else 'Пришли исправленный текст. Предыдущая версия сохранится.',CANCEL)
        elif action=='tasks' and len(parts)==4:
            ident,offset=number(parts[2]),number(parts[3])
            if not db(ctx).get_project(owner,ident):
                await missing(update)
                return
            reset(ctx)
            tasks=db(ctx).list_tasks(owner,offset=offset,limit=9)
            rows=[[button(t['title'][:50],f"mem:tasklink:{ident}:{t['id']}")] for t in tasks[:8]]
            if offset:
                rows.append([button('← Назад',f'mem:tasks:{ident}:{max(0,offset-8)}')])
            if len(tasks)>8:
                rows.append([button('Далее →',f'mem:tasks:{ident}:{offset+8}')])
            await say(update,'Выбери своё открытое дело для связи с проектом.',rows+HOME)
        elif action=='tasklink' and len(parts)==4:
            if not db(ctx).link_project_task(owner,number(parts[2]),number(parts[3])):
                await missing(update)
                return
            await project(update,ctx,number(parts[2]))
        elif action in ('brief','download') and len(parts)==3:
            ident=number(parts[2])
            p=db(ctx).get_project(owner,ident)
            if not p:
                await missing(update)
                return
            if action=='brief':
                reset(ctx)
                await say(update,f"<b>Памятка: {escape(p['title'])}</b>\n\nВ файл войдут цель и до 1000 текущих записей этого проекта с типами, датами и номерами версий. История версий не включается.\n\nФайл придёт только тебе: проверь и отредактируй его перед передачей другому человеку.",
                          [[button('Скачать Markdown',f'mem:download:{ident}')],*HOME])
            else:
                values=db(ctx).memory_entries(owner,ident,limit=1000)
                chunks=[f"# {p['title']}\n\nЦель: {p['goal']}\n"]
                for e in values:
                    chunks.append(f"\n## {e['title']}\n{KINDS[e['kind']]} · {dated(ctx,owner,e['updated_at'])} · версия {e['revision']}\n\n{e['body']}\n")
                    if e['kind']=='case':
                        chunks.append('Результат после действия не доказывает причинную связь.\n')
                    if e['source_kind']:
                        chunks.append(f"Источник в боте: {e['source_kind']} #{e['source_id']}\n")
                await chat_cleanup.send_document(update,ctx,BytesIO('\n'.join(chunks).encode('utf-8')),filename=f'project-{ident}.md',caption='Личная памятка. Проверь содержание перед передачей.')
        elif action in ('deleteproject','deleteentry') and len(parts)==3:
            ident=number(parts[2])
            obj=db(ctx).get_project(owner,ident) if action=='deleteproject' else db(ctx).get_memory_entry(owner,ident)
            if not obj:
                await missing(update)
                return
            reset(ctx)
            await confirm(update,ctx,action,dict(ident=ident),f"Удалить «{escape(obj['title'])}» вместе с историей{' и записями проекта' if action=='deleteproject' else ''}?\n\nОригинальные материалы и дела останутся. Восстановить удалённое через бота нельзя.",label='Да, удалить')
        elif action=='confirm' and len(parts)==3:
            pending=ctx.user_data.get('mem_pending',{})
            if ctx.user_data.get('state')!='mem:confirm' or pending.get('owner')!=owner or pending.get('token')!=parts[2]:
                raise ValueError('Подтверждение уже использовано или устарело.')
            payload,op=pending['payload'],pending['op']
            reset(ctx)
            if op=='project':
                p=db(ctx).create_project(owner,**payload)
                await project(update,ctx,p['id'])
            elif op=='record':
                e=db(ctx).add_memory_entry(owner,**payload)
                await entry(update,ctx,e['id']) if e else await missing(update)
            elif op=='revise':
                e=db(ctx).revise_memory(owner,**payload)
                if e:
                    await entry(update,ctx,e['id'])
                else:
                    raise ValueError('Запись изменилась. Открой её заново перед исправлением.')
            elif op in ('deleteproject','deleteentry'):
                db(ctx).delete_memory(owner,payload['ident'],project=op=='deleteproject')
                await home(update,ctx)
        else:
            raise ValueError('Кнопка недоступна.')
    except (ValueError,IndexError,OverflowError) as exc:
        await say(update,escape(str(exc)),HOME)
