"""Private recall cards. No AI calls, automatic grading, or unsolicited pushes."""
from datetime import datetime
from html import escape
import secrets
from zoneinfo import ZoneInfo

from .planner import db, zone, source, button, say, number


HOME = [[button('← Учёба', 'learn:home'), button('🏠 Меню', 'home')]]
CANCEL = [[button('Отмена', 'learn:home')]]


def clear(ctx):
    for key in tuple(ctx.user_data):
        if key.startswith('learn_'):
            ctx.user_data.pop(key,None)
    if str(ctx.user_data.get('state','')).startswith('learn:'):
        ctx.user_data.pop('state',None)


def reset(ctx):
    clear(ctx)
    ctx.user_data['state']='learn:browse'


async def home(update,ctx):
    reset(ctx)
    counts=db(ctx).study_counts(update.effective_user.id)
    await say(update,f"<b>Учёба · вспомнить, а не перечитать</b>\n\nКарточек: {counts['total']}\nГотово к повторению: {counts['due']}\n\n"
              'Сначала попробуй ответить сам, затем открой ответ и оцени себя. После «Помню» интервал растёт: 1, 3, 7, 14, 30 дней; после «Трудно» — 10 минут.',
              [[button('🧠 Повторить', 'learn:review')],
               [button('➕ Создать карточку','learn:new'),button('Мои карточки','learn:list:0')],
               [button('← Сегодня','plan:today'),button('🏠 Меню','home')]])


async def new(update,ctx,kind=None,ident=None):
    reset(ctx)
    if kind and not source(ctx,update.effective_user.id,kind,ident):
        await say(update,'Материал недоступен.',HOME)
        return
    ctx.user_data.update(state='learn:question',learn_source=(kind,ident))
    await say(update,'<b>Что хочешь запомнить?</b>\n\nНапиши один вопрос до 300 символов. Например: «Чем отличается выручка от прибыли?»\n\nСледующим сообщением сохраним твой ответ. Текст остаётся в личной базе бота.',CANCEL)


async def confirmation(update,ctx,op,payload,text,label='✅ Сохранить'):
    token=secrets.token_hex(8)
    ctx.user_data.update(state='learn:confirm',learn_pending=dict(owner=update.effective_user.id,token=token,op=op,payload=payload))
    await say(update,text,[[button(label,f'learn:confirm:{token}')],*CANCEL])


async def listing(update,ctx,offset=0):
    reset(ctx)
    owner=update.effective_user.id
    count=db(ctx).study_counts(owner)['total']
    offset=min(offset,max(0,(count-1)//8*8))
    cards=db(ctx).list_study_cards(owner,offset=offset)
    rows=[[button(c['question'][:55],f"learn:card:{c['id']}")] for c in cards]
    nav=[]
    if offset:
        nav.append(button('← Назад',f'learn:list:{offset-8}'))
    if offset+8<count:
        nav.append(button('Далее →',f'learn:list:{offset+8}'))
    if nav:
        rows.append(nav)
    await say(update,f'<b>Мои учебные карточки</b> · {count}',rows+HOME)


async def card(update,ctx,ident):
    reset(ctx)
    owner=update.effective_user.id
    card=db(ctx).get_study_card(owner,ident)
    if not card:
        await say(update,'Карточка недоступна.',HOME)
        return
    due=datetime.fromtimestamp(card['due_at'],ZoneInfo(zone(ctx,owner))).strftime('%d.%m %H:%M')
    text=f"<b>{escape(card['question'])}</b>\n\n{escape(card['answer'])}\n\nПовторение: {due}"
    rows=[]
    if card['source_kind']:
        item=source(ctx,owner,card['source_kind'],card['source_id'])
        if item:
            target=f"item:{item['id']}" if card['source_kind']=='library' else f"work:card:{item['id']}:0"
            rows.append([button('Открыть исходный материал',target)])
        else:
            text+='\nИсточник удалён; вопрос и ответ сохранены.'
    rows += [[button('✏️ Исправить','learn:edit:'+str(ident)),button('🗑 Удалить','learn:delete:'+str(ident))],*HOME]
    await say(update,text,rows)


async def review(update,ctx):
    reset(ctx)
    owner=update.effective_user.id
    cards=db(ctx).list_study_cards(owner,due_only=True,limit=1)
    if not cards:
        await say(update,'На сейчас всё повторено. Карточки появятся здесь, когда подойдёт их время. Можно добавить новую.',
                  [[button('➕ Создать карточку','learn:new')],*HOME])
        return
    card=cards[0]
    token=secrets.token_hex(8)
    ctx.user_data.update(state='learn:review',learn_review=dict(owner=owner,id=card['id'],revision=card['revision'],token=token,revealed=False))
    await say(update,f"<b>Попробуй вспомнить</b>\n\n{escape(card['question'])}\n\nОтветь мысленно или напиши свой ответ сюда. Затем открой сохранённый ответ для сравнения.",
              [[button('Показать ответ',f'learn:reveal:{token}')],*HOME])


async def message(update,ctx):
    text=(update.effective_message.text or '').strip()
    state=ctx.user_data.get('state')
    if state=='learn:question':
        if not text or len(text)>300:
            await say(update,'Нужен один текстовый вопрос до 300 символов.',CANCEL)
            return
        ctx.user_data.update(state='learn:answer',learn_question=text)
        await say(update,'Теперь пришли правильный ответ своими словами — до 1000 символов. Он будет эталоном для самопроверки.',CANCEL)
    elif state=='learn:answer':
        if not text or len(text)>1000:
            await say(update,'Напиши текстовый ответ до 1000 символов.',CANCEL)
            return
        question=ctx.user_data.get('learn_question','')
        kind,ident=ctx.user_data.get('learn_source',(None,None))
        payload=dict(question=question,answer=text,source_kind=kind,source_id=ident)
        edit=ctx.user_data.get('learn_edit')
        if edit:
            payload=dict(ident=edit['id'],revision=edit['revision'],question=question,answer=text)
        await confirmation(update,ctx,'edit' if edit else 'create',payload,
                           f'<b>Сохранить карточку?</b>\n\n<b>{escape(question)}</b>\n\n{escape(text)}')
    elif state=='learn:review':
        token=ctx.user_data.get('learn_review',{}).get('token')
        await say(update,'Сравни свой ответ с сохранённым. Бот не выставляет оценку за тебя.',
                  [[button('Показать ответ',f'learn:reveal:{token}')],*HOME])
    else:
        await say(update,'Выбери повторение или создание карточки. Этот текст не сохранён.',HOME)


async def callback(update,ctx,data):
    owner=update.effective_user.id
    if not str(ctx.user_data.get('state','')).startswith('learn:'):
        reset(ctx)
    parts=data.split(':')
    try:
        action=parts[1]
        if action=='home' and len(parts)==2:
            await home(update,ctx)
        elif action=='new' and len(parts)==2:
            await new(update,ctx)
        elif action=='from' and len(parts)==4:
            await new(update,ctx,parts[2],number(parts[3]))
        elif action=='review' and len(parts)==2:
            await review(update,ctx)
        elif action=='list' and len(parts)==3:
            await listing(update,ctx,number(parts[2]))
        elif action=='card' and len(parts)==3:
            await card(update,ctx,number(parts[2]))
        elif action in ('delete','edit') and len(parts)==3:
            ident=number(parts[2])
            current=db(ctx).get_study_card(owner,ident)
            if not current:
                raise ValueError('Карточка недоступна.')
            reset(ctx)
            if action=='delete':
                await confirmation(update,ctx,'delete',dict(ident=ident),f"Удалить карточку «{escape(current['question'])}»?",label='Да, удалить')
            else:
                ctx.user_data.update(state='learn:question',learn_edit=dict(id=ident,revision=current['revision']))
                await say(update,'Пришли исправленный вопрос, затем ответ. После сохранения карточка вернётся к первому повторению.',CANCEL)
        elif action=='confirm' and len(parts)==3:
            pending=ctx.user_data.get('learn_pending',{})
            if ctx.user_data.get('state')!='learn:confirm' or pending.get('owner')!=owner or pending.get('token')!=parts[2]:
                raise ValueError('Подтверждение устарело.')
            reset(ctx)
            payload=pending['payload']
            if pending['op']=='create':
                saved=db(ctx).create_study_card(owner,**payload)
                await card(update,ctx,saved['id'])
            elif pending['op']=='edit':
                saved=db(ctx).edit_study_card(owner,**payload)
                if not saved:
                    raise ValueError('Карточка изменилась или удалена. Открой её заново.')
                await card(update,ctx,saved['id'])
            elif pending['op']=='delete':
                db(ctx).delete_study_card(owner,payload['ident'])
                await home(update,ctx)
        elif action in ('reveal','grade') and len(parts) in (3,4):
            entry=ctx.user_data.get('learn_review',{})
            if ctx.user_data.get('state')!='learn:review' or entry.get('owner')!=owner or entry.get('token')!=parts[2]:
                raise ValueError('Это повторение уже завершено или устарело.')
            current=db(ctx).get_study_card(owner,entry['id'])
            if not current or current['revision']!=entry['revision']:
                raise ValueError('Карточка изменилась. Начни повторение заново.')
            if action=='reveal' and len(parts)==3:
                entry['revealed']=True
                await say(update,f"<b>{escape(current['question'])}</b>\n\n{escape(current['answer'])}\n\nПолучилось вспомнить?",
                          [[button('Помню',f"learn:grade:{entry['token']}:yes"),button('Трудно',f"learn:grade:{entry['token']}:no")],*HOME])
            elif action=='grade' and len(parts)==4 and parts[3] in ('yes','no') and entry.get('revealed'):
                reset(ctx)
                saved=db(ctx).review_study_card(owner,entry['id'],entry['revision'],parts[3]=='yes')
                if not saved:
                    raise ValueError('Оценка уже учтена или карточка ещё не готова к повторению.')
                due=datetime.fromtimestamp(saved['due_at'],ZoneInfo(zone(ctx,owner))).strftime('%d.%m в %H:%M')
                await say(update,f'Следующее повторение — {due}.',[[button('Следующая карточка','learn:review')],*HOME])
            else:
                raise ValueError('Сначала открой ответ, затем оцени себя.')
        else:
            raise ValueError('Кнопка недоступна.')
    except (ValueError,IndexError,OverflowError) as exc:
        await say(update,escape(str(exc)),HOME)
