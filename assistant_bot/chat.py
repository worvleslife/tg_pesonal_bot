"""Private AI conversations; network work never occupies the scheduler lock."""
import asyncio
from datetime import datetime, timezone
from html import escape
import logging
import secrets

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard, LinkPreviewOptions
from telegram.error import TelegramError

from .ai import AIError, generate_reply
from .answer_pages import paginate_answer
from . import panel

LOG = logging.getLogger(__name__)
CHAT_LABEL = "💬 Диалог с ИИ"
CHAT_KEYS = Keyboard([
    [Button("🆕 Новый диалог", callback_data="ai:new")],
    [Button("ℹ️ О диалоге", callback_data="ai:info")],
    [Button("🏠 Выйти в меню", callback_data="home")],
])
MAX_CACHED_ANSWERS = 128


async def reply(update, text, keyboard=CHAT_KEYS, *, separate=False):
    if not separate:
        return await panel.say(update,text,keyboard)
    return await update.effective_message.reply_text(
        text, parse_mode="HTML", reply_markup=keyboard,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def edit_reply(message, text, keyboard=CHAT_KEYS):
    return await message.edit_text(
        text, parse_mode="HTML", reply_markup=keyboard,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


def page_keys(token, index, count):
    nav = []
    if index:
        nav.append(Button("← Назад", callback_data=f"ai:page:{token}:{index - 1}"))
    nav.append(Button(f"{index + 1} / {count}", callback_data="ai:noop"))
    if index + 1 < count:
        nav.append(Button("Далее →", callback_data=f"ai:page:{token}:{index + 1}"))
    return Keyboard([nav, *CHAT_KEYS.inline_keyboard])


async def show_answer_page(update, ctx, token, index):
    entry = ctx.application.bot_data.get("ai_answers", {}).get(token)
    owner = update.effective_user.id
    handle=entry.get('panel_handle') if entry else None
    if (not entry or entry["owner"] != owner
            or (handle.message_id if handle else entry["message_id"]) != update.effective_message.message_id
            or (handle is not None and handle.manager.revisions.get(owner)!=handle.revision)
            or not 0 <= index < len(entry["pages"])):
        await update.callback_query.answer("Ответ устарел или недоступен. Задай вопрос заново.", show_alert=True)
        return
    if index == entry["page"]:
        await update.callback_query.answer()
        return
    await update.callback_query.answer()
    await edit_reply(handle or update.effective_message, escape(entry["pages"][index]),
                     page_keys(token, index, len(entry["pages"])))
    entry["page"] = index


def leave_chat(ctx, owner):
    """Cancel only this user's pending request; keep completed history."""
    task = ctx.application.bot_data.get("ai_tasks", {}).pop(owner, None)
    if task is not None:
        task.cancel()
    if ctx.user_data.get("state") == "ai":
        ctx.user_data.pop("state", None)


async def enter_chat(update, ctx):
    cfg = ctx.application.bot_data["config"]
    if not cfg.yandex_api_key or not cfg.ai_model_uri:
        ctx.user_data.pop("state", None)
        await reply(update, "💬 ИИ-диалог пока не подключён. Владелец бота должен настроить доступ к Yandex AI Studio. "
                    "Библиотека и напоминания уже работают.",
                    Keyboard([[Button("🏠 Меню", callback_data="home")]]))
        return
    ctx.user_data["state"] = "ai"
    await reply(update, "💬 <b>GPT 6 Astra</b>\nЗадай вопрос.")


async def chat_info(update, ctx):
    cfg = ctx.application.bot_data["config"]
    await reply(update, "<b>О диалоге</b>\n\n"
                "GPT 6 Astra — название ассистента в этом боте. Отвечает модель DeepSeek через Yandex AI Studio.\n\n"
                "Твой вопрос и недавняя история передаются Yandex AI Studio. У каждого пользователя "
                "своя история; материалы библиотеки автоматически не передаются. "
                "В памяти остаются до 6 последних пар вопросов и ответов. «Новый диалог» очищает память.\n\n"
                "Ответы обновляют общую панель. Длинные ответы листаются кнопками; после перехода "
                "в другой раздел или перезапуска страницы нужно открыть заново.\n\n"
                f"До {cfg.ai_daily_limit} запросов в день на человека; также действует общий лимит бота. "
                "/cancel — вернуться к библиотеке и напоминаниям.")


async def new_chat(update, ctx, *, confirmed=False):
    if not confirmed:
        await reply(update, "Очистить память твоего ИИ-диалога и начать заново? "
                    "Сообщения в Telegram и материалы библиотеки сохранятся.", Keyboard([
                        [Button("Да, начать заново", callback_data="ai:clear")],
                        [Button("Продолжить диалог", callback_data="ai:chat")],
                    ]))
        return
    owner = update.effective_user.id
    leave_chat(ctx, owner)
    ctx.application.bot_data["store"].clear_chat(owner)
    cache = ctx.application.bot_data.get("ai_answers", {})
    for token in [token for token, entry in cache.items() if entry["owner"] == owner]:
        cache.pop(token, None)
    await enter_chat(update, ctx)


async def handle_chat_message(update, ctx, *, prompt=None):
    owner = update.effective_user.id
    cfg = ctx.application.bot_data["config"]
    if not cfg.yandex_api_key or not cfg.ai_model_uri:
        await enter_chat(update, ctx)
        return
    text = prompt if prompt is not None else update.effective_message.text
    if not text or not text.strip():
        await reply(update, "В ИИ-диалоге пока доступны текстовые сообщения. "
                    "Для сохранения файлов выйди в меню.")
        return
    text = text.strip()
    if len(text) > 4000:
        await reply(update, "Сообщение слишком длинное. Отправь до 4000 символов за раз.")
        return
    tasks = ctx.application.bot_data.setdefault("ai_tasks", {})
    if owner in tasks:
        pending = ctx.application.bot_data.get('ai_pending', {}).get(owner)
        if pending:
            await edit_reply(pending, "Ещё готовлю ответ на твой предыдущий вопрос. Подожди немного.")
        return
    db = ctx.application.bot_data["store"]
    day = datetime.now(timezone.utc).date().isoformat()
    if not db.reserve_ai_request(owner, day, cfg.ai_daily_limit, cfg.ai_global_daily_limit):
        await reply(update, "На сегодня достигнут личный или общий лимит ИИ-запросов. "
                    "Он обновится в 03:00 по Москве. Библиотека и напоминания доступны.")
        return
    history = db.chat_history(owner)
    pending = await reply(update, "💭 Готовлю ответ…")
    ctx.application.bot_data.setdefault('ai_pending', {})[owner] = pending
    tasks[owner] = ctx.application.create_task(_answer(update, ctx, owner, text, history, pending))


async def _answer(update, ctx, owner, text, history, pending):
    data = ctx.application.bot_data
    cfg, db = data["config"], data["store"]
    current = asyncio.current_task()
    token = None
    delivered = False
    try:
        answer = await asyncio.wait_for(generate_reply(
            api_key=cfg.yandex_api_key, model=cfg.ai_model_uri, history=history, text=text), timeout=50)
        # No Telegram or model API network awaits hold the shared scheduler lock.
        # Cancellation remains responsive even when Telegram delivery is slow.
        pages = paginate_answer(answer)
        keyboard = CHAT_KEYS
        async with data["lock"]:
            if data["ai_tasks"].get(owner) is not current or ctx.user_data.get("state") != "ai":
                return
            if len(pages) > 1:
                token = secrets.token_hex(8)
                cache = data.setdefault("ai_answers", {})
                cache[token] = {"owner": owner, "pages": pages,
                                "message_id": pending.message_id, "page": 0}
                if isinstance(pending,panel.PanelHandle):
                    cache[token]['panel_handle']=pending
                while len(cache) > MAX_CACHED_ANSWERS:
                    cache.pop(next(iter(cache)))
                keyboard = page_keys(token, 0, len(pages))
        # Replace the progress bubble. Only one message is created per answer.
        edited=await edit_reply(pending, escape(pages[0]), keyboard)
        async with data["lock"]:
            if edited is not False and data["ai_tasks"].get(owner) is current and ctx.user_data.get("state") == "ai":
                db.append_chat_turn(owner, text, answer)
                delivered = True
    except asyncio.CancelledError:
        try:
            await asyncio.wait_for(edit_reply(pending, "Запрос отменён.",
                Keyboard([[Button("🏠 Меню", callback_data="home")]])), timeout=3)
        except (TelegramError, TimeoutError):
            pass
        raise
    except (AIError, TimeoutError) as exc:
        message = str(exc) if isinstance(exc, AIError) else "ИИ не успел ответить. Попробуй ещё раз позже."
        if data["ai_tasks"].get(owner) is current and ctx.user_data.get("state") == "ai":
            await edit_reply(pending, escape(message))
    except TelegramError as exc:
        LOG.warning("AI reply delivery failed: %s", type(exc).__name__)
    except Exception as exc:
        LOG.error("AI request failed: %s", type(exc).__name__)
        if data["ai_tasks"].get(owner) is current and ctx.user_data.get("state") == "ai":
            await edit_reply(pending, "Не удалось получить ответ ИИ. Попробуй позже.")
    finally:
        if token is not None and not delivered:
            data.get("ai_answers", {}).pop(token, None)
        if data.get("ai_tasks", {}).get(owner) is current:
            data["ai_tasks"].pop(owner, None)
        if data.get('ai_pending', {}).get(owner) is pending:
            data['ai_pending'].pop(owner, None)
