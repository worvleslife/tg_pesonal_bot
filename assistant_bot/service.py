"""Reminder delivery and daily plans, independent from Telegram handlers."""

from __future__ import annotations

import html
import logging
import math
import re
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, RetryAfter, TelegramError

from .parsing import next_occurrence
from .storage import Store


logger = logging.getLogger(__name__)


def _escaped(text: str, limit: int) -> str:
    """Clip before escaping, keeping complete HTML entities and valid markup."""
    encoded = html.escape(text)
    if len(encoded) <= limit:
        return encoded
    lo, hi = 0, min(len(text), limit)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(html.escape(text[:mid])) + 1 <= limit:
            lo = mid
        else:
            hi = mid - 1
    return html.escape(text[:lo]) + "…"


def _backoff(exc: TelegramError) -> int:
    if isinstance(exc, RetryAfter):
        delay = exc.retry_after
        seconds = delay.total_seconds() if isinstance(delay, timedelta) else delay
        return max(1, math.ceil(seconds))
    if isinstance(exc, Forbidden):
        return 3600
    return 60


def _buttons(reminder: dict[str, Any]) -> InlineKeyboardMarkup:
    rid = reminder["id"]
    recurring = reminder["repeat"] in {"daily", "weekly"}
    rows = [[InlineKeyboardButton("Готово сегодня" if recurring else "Готово",
                                  callback_data=f"ra:{rid}" if recurring else f"rd:{rid}")],
            [InlineKeyboardButton("Через 10 мин", callback_data=f"rs:{rid}:10"),
             InlineKeyboardButton("Через час", callback_data=f"rs:{rid}:60")]]
    if recurring:
        rows.append([InlineKeyboardButton("Остановить повторы", callback_data=f"rc:{rid}")])
    return InlineKeyboardMarkup(rows)


async def dispatch_due(store: Store, bot: Any, owner_id: int | None = None,
                       now: int | None = None, *, panel_manager=None) -> None:
    """Deliver a bounded due batch to each reminder's own private chat.

    By default all owners are scheduled. The optional owner filter is an
    internal convenience and is applied in SQL before the batch limit.

    The caller holds a shared asyncio.Lock around this and handler mutations.
    Delivery is at-least-once: an abrupt crash after Telegram accepts a message
    and before the SQLite commit can repeat that message after restart.
    """
    current = int(time.time()) if now is None else int(now)
    for reminder in store.due_reminders(current, limit=100, owner_id=owner_id):
        recipient = reminder["owner_id"]
        rid = reminder["id"]
        repeat = reminder["repeat"]
        try:
            next_due = (next_occurrence(reminder["due_at"], repeat, reminder["timezone"], current)
                        if repeat in {"daily", "weekly"} else None)
        except (ValueError, OverflowError) as exc:
            # A damaged record must not block delivery for other users.
            store.retry_reminder(recipient, rid, current + 3600)
            logger.warning("reminder_id=%s error_type=%s", rid, type(exc).__name__)
            continue
        header = "<b>⏰ Напоминание</b>\n\n"
        footer = "\n\n🔁 Каждый день" if repeat == "daily" else "\n\n🔁 Каждую неделю" if repeat == "weekly" else ""
        message = header + _escaped(reminder["text"], 2000 - len(header) - len(footer)) + footer
        task = store.task_for_reminder(recipient, rid)
        buttons = _buttons(reminder)
        if task and task['status'] == 'active':
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton('✓ Дело сделано', callback_data=f"plan:done:{task['id']}")],
                [InlineKeyboardButton('Продолжить / открыть дело', callback_data=f"plan:task:{task['id']}")],
            ])
        try:
            if panel_manager:
                await panel_manager.notify(recipient,message,buttons)
            else:
                await bot.send_message(chat_id=recipient, text=message, parse_mode="HTML",
                                       disable_web_page_preview=True, reply_markup=buttons)
        except TelegramError as exc:
            store.retry_reminder(recipient, rid, current + _backoff(exc))
            # Exception strings can contain request URLs/tokens or private text.
            logger.warning("reminder_id=%s error_type=%s", rid, type(exc).__name__)
            continue
        store.mark_delivered(recipient, rid, next_due, current)


def today_text(store: Store, owner_id: int, timezone: str,
               now: int | None = None) -> str:
    """Russian HTML plan of today's pending and all overdue reminders, <=3500 chars."""
    current = int(time.time()) if now is None else int(now)
    zone = ZoneInfo(timezone)
    local_now = datetime.fromtimestamp(current, zone)
    tomorrow = local_now.date() + timedelta(days=1)
    pending = store.list_reminders(owner_id, limit=1000)
    relevant = [item for item in pending if datetime.fromtimestamp(item["due_at"], zone).date() < tomorrow]
    header = f"<b>📅 Сегодня — {local_now:%d.%m.%Y}</b>\n<i>{html.escape(timezone)}</i>"
    tasks = store.list_tasks(owner_id, limit=3)
    if tasks:
        header += '\n\n<b>Твои дела</b>'
        for task in tasks:
            header += f"\n{'⭐' if task['priority'] else '•'} {_escaped(task['title'], 160)} · {task['minutes']} мин"
        header += '\nОткрыть дела: /plan'
    study = store.study_counts(owner_id,current)
    if study['due']:
        header += f"\n\n📖 Готово к повторению: {study['due']} · /study"
    if not relevant:
        return header + "\n\nНа сегодня нет ожидающих или просроченных напоминаний. ✨"
    chunks = [header, f"\n\nСегодня и ранее: {len(relevant)}"]
    shown = 0
    # Leave room for a closing notice so clipping never breaks an HTML tag.
    available = 3500 - 180
    for item in relevant:
        due = datetime.fromtimestamp(item["due_at"], zone)
        stamp = due.strftime("%H:%M" if due.date() == local_now.date() else "%d.%m %H:%M")
        overdue = " · просрочено" if item["due_at"] < current else ""
        repeat = " 🔁" if item["repeat"] else ""
        line = f"\n• <b>{stamp}</b>{overdue}{repeat} — {_escaped(item['text'], 160)}"
        if sum(map(len, chunks)) + len(line) > available:
            break
        chunks.append(line)
        shown += 1
    if shown < len(relevant):
        chunks.append(f"\n\nЕщё {len(relevant) - shown} — откройте «Напоминания».")
    if len(pending) == 1000 and store.stats(owner_id)["pending"] > 1000:
        chunks.append("\nУчтены ближайшие 1000 напоминаний.")
    return "".join(chunks)


async def send_digest(store: Store, bot: Any, owner_id: int, timezone: str,
                      now: int | None = None, *, panel_manager=None) -> None:
    """Send an enabled daily plan once per local day after its configured time.

    Successful delivery is recorded only afterward. Error backoff survives a
    restart. The caller serializes this function with its other service jobs.
    """
    if store.get_setting(owner_id, "started", "0") != "1":
        return
    configured = store.get_setting(owner_id, "digest_time", "off")
    if configured == "off":
        return
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", configured or "")
    if match is None:
        logger.warning("owner_id=%s error_type=InvalidDigestTime", owner_id)
        return
    current = int(time.time()) if now is None else int(now)
    local_now = datetime.fromtimestamp(current, ZoneInfo(timezone))
    today = local_now.date().isoformat()
    if store.get_setting(owner_id, "digest_last") == today:
        return
    if (local_now.hour, local_now.minute) < (int(match[1]), int(match[2])):
        return
    try:
        retry_at = int(store.get_setting(owner_id, "digest_retry_at", "0") or "0")
    except ValueError:
        retry_at = 0
    if current < retry_at:
        return
    message = today_text(store, owner_id, timezone, current)
    try:
        if panel_manager:
            await panel_manager.notify(owner_id,message)
        else:
            await bot.send_message(chat_id=owner_id, text=message, parse_mode="HTML",
                                   disable_web_page_preview=True)
    except TelegramError as exc:
        store.set_setting(owner_id, "digest_retry_at", str(current + _backoff(exc)))
        logger.warning("owner_id=%s error_type=%s", owner_id, type(exc).__name__)
        return
    store.set_setting(owner_id, "digest_last", today)
    store.set_setting(owner_id, "digest_retry_at", "0")


async def send_digests(store: Store, bot: Any, default_timezone: str,
                       now: int | None = None, *, panel_manager=None) -> None:
    """Run each subscribed user's private daily plan in their own timezone.

    Keep one user's bad settings or delivery failure from blocking the next.
    Existing per-owner settings, completion dates and retries remain intact.
    """
    current = int(time.time()) if now is None else int(now)
    for subscriber in store.digest_subscribers():
        owner_id = subscriber["owner_id"]
        try:
            retry_at = int(store.get_setting(owner_id, "digest_retry_at", "0") or "0")
        except ValueError:
            retry_at = 0
        if current < retry_at:
            continue
        try:
            await send_digest(store, bot, owner_id,
                              subscriber["timezone"] or default_timezone, current,panel_manager=panel_manager)
        except Exception as exc:
            # This per-user job boundary intentionally isolates unexpected
            # settings errors too. Do not log exception messages or tracebacks.
            store.set_setting(owner_id, "digest_retry_at", str(current + 3600))
            logger.warning("owner_id=%s error_type=%s", owner_id, type(exc).__name__)
