"""Stateless Yandex AI Studio client; never reads the library or sends user IDs."""

from __future__ import annotations

import re

import httpx


ENDPOINT = "https://ai.api.cloud.yandex.net/v1/responses"
MODEL_URI = re.compile(
    r"gpt://(?P<folder>[A-Za-z0-9_-]{1,64})/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,39})?"
)
MAX_TEXT_CHARS = 4_000
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CHARS = 20_000
MAX_REPLY_CHARS = 10_000
TIMEOUT_SECONDS = 45.0
TRUNCATION_NOTE = "\n\nОтвет сокращён. Попроси продолжить или уточни вопрос."

INSTRUCTIONS = (
    "Ты внимательный личный ИИ-ассистент в Telegram. Отвечай по-русски, "
    "ясно, доброжелательно и по существу; по просьбе пользователя используй другой язык. "
    "Помогай с идеями, текстами, объяснениями и планированием. "
    "Не выдавай предположения за факты. У тебя есть только сообщения этого диалога. "
    "У тебя нет доступа к базе знаний, файлам, напоминаниям, другим пользователям, "
    "предыдущим чатам в ChatGPT, компьютеру, интернету или инструментам. "
    "Не утверждай, что создал напоминание, сохранил материал, отправил сообщение "
    "или выполнил иное действие. Для создания напоминания предложи выйти из "
    "ИИ-диалога и воспользоваться кнопкой напоминаний в меню бота. "
    "Для сохранения материала предложи выйти из ИИ-диалога и отправить его боту. "
    "Пиши обычным текстом, без HTML-разметки."
)


class AIError(Exception):
    """An error whose message can safely be shown to a Telegram user."""


def _bounded_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Copy recent text messages; ignore metadata and untrusted role names."""
    result = []
    remaining = MAX_HISTORY_CHARS
    for message in reversed(history[-MAX_HISTORY_MESSAGES:]):
        if not isinstance(message, dict):
            continue
        role, content = message.get("role"), message.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        if not content.strip():
            continue
        # Keep the newest part of the conversation if the oldest message does
        # not fit. Inputs are always copied, never modified in the caller.
        content = content[-remaining:]
        result.append({"role": role, "content": content})
        remaining -= len(content)
        if remaining == 0:
            break
    result.reverse()
    return result


def _status_error(response: httpx.Response) -> AIError:
    """Use status and known error codes only, never the service's raw text."""
    status = response.status_code
    if status in (401, 403):
        return AIError("Не удалось подключить ИИ. Владелец бота должен проверить API-ключ и доступ к каталогу Yandex AI Studio.")
    if status == 429:
        try:
            data = response.json()
            error = data.get("error") if isinstance(data, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
        except (ValueError, UnicodeError):
            code = None
        if code in ("insufficient_quota", "billing_hard_limit_reached"):
            return AIError("Закончился доступный лимит Yandex AI Studio. Владелец бота должен проверить баланс и ограничения.")
        return AIError("ИИ пока получает слишком много запросов. Попробуй немного позже.")
    if status in (400, 404, 422):
        return AIError("ИИ не смог обработать запрос. Попробуй новый диалог; если ошибка повторится, владельцу бота нужно проверить модель и настройки API Yandex AI Studio.")
    return AIError("Сервис Yandex AI Studio временно недоступен. Попробуй позже.")


def _reply_text(data: object) -> str:
    if not isinstance(data, dict) or data.get("status") not in ("completed", "incomplete"):
        raise AIError("ИИ не смог закончить ответ. Попробуй ещё раз.")
    output = data.get("output")
    if not isinstance(output, list):
        raise AIError("ИИ прислал ответ в неизвестном формате. Попробуй ещё раз.")
    parts = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
            continue
        for content in item["content"]:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_text":
                value = content.get("text")
            elif content.get("type") == "refusal":
                value = content.get("refusal")
            else:
                continue
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    reply = "\n\n".join(parts)
    if not reply:
        raise AIError("ИИ не успел сформировать текст ответа. Попробуй задать вопрос короче.")
    if data.get("status") == "incomplete" or len(reply) > MAX_REPLY_CHARS:
        return reply[:MAX_REPLY_CHARS - len(TRUNCATION_NOTE)].rstrip() + TRUNCATION_NOTE
    return reply


async def generate_reply(*, api_key: str, model: str,
                         history: list[dict[str, str]], text: str) -> str:
    """Generate text without tools, remote conversation storage or retries."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
        raise AIError("Отправь текстовый вопрос длиной от 1 до 4000 символов.")
    if not isinstance(api_key, str) or not api_key.strip() or any(
        char.isspace() or ord(char) > 127 or ord(char) < 32 for char in api_key
    ):
        raise AIError("ИИ пока не настроен: владелец бота должен добавить API-ключ Yandex AI Studio.")
    model_match = MODEL_URI.fullmatch(model) if isinstance(model, str) else None
    if model_match is None:
        raise AIError("Владелец бота должен проверить URI модели Yandex AI Studio: gpt://идентификатор_каталога/модель.")
    payload = {
        "model": model,
        "instructions": INSTRUCTIONS,
        "input": [*_bounded_history(history), {"role": "user", "content": text}],
        "max_output_tokens": 1500,
        "store": False,
    }
    try:
        # Respect HTTPS_PROXY for installations using a local VPN proxy. Never
        # follow redirects: the API key belongs only to Yandex AI Studio.
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, trust_env=True,
                                     follow_redirects=False) as client:
            response = await client.post(
                ENDPOINT, headers={
                    "Authorization": f"Api-Key {api_key}",
                    "x-folder-id": model_match["folder"],
                    "x-data-logging-enabled": "false",
                }, json=payload
            )
    except httpx.TimeoutException:
        raise AIError("ИИ не ответил вовремя. Попробуй ещё раз чуть позже.") from None
    except (httpx.RequestError, ValueError):
        raise AIError("Не удалось связаться с ИИ. Проверь подключение и VPN или попробуй позже.") from None
    if not response.is_success:
        raise _status_error(response)
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        raise AIError("ИИ прислал ответ в неизвестном формате. Попробуй ещё раз.") from None
    return _reply_text(data)
