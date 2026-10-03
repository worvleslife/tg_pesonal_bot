"""Bounded Telegram downloads and Yandex OCR/STT. Never fetch arbitrary links."""
import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

import httpx

MAX_BYTES = 8_000_000
OCR_ENDPOINT = 'https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText'
STT_ENDPOINT = 'https://stt.api.cloud.yandex.net/speech/v1/stt:recognize'


class ExtractionError(Exception):
    """Only safe, locally authored messages may be shown to users."""


def mode_for(source):
    if not source or not source.get('file_id'):
        return None
    kind = source['kind']
    suffix = Path(source.get('file_name') or '').suffix.lower()
    if kind == 'photo' or suffix in ('.jpg', '.jpeg', '.png'):
        return 'image'
    if kind == 'voice' or suffix in ('.ogg', '.opus'):
        return 'voice'
    return 'pdf' if suffix == '.pdf' else 'text' if suffix in ('.txt', '.md') else None


async def download(bot, cfg, source):
    remote = await bot.get_file(source['file_id'])
    if remote.file_size and remote.file_size > MAX_BYTES:
        raise ExtractionError('Файл больше 8 МБ. Оригинал сохранён; пришли меньшую копию для распознавания.')
    address = str(remote.file_path or '')
    parsed = urlsplit(address)
    if (parsed.scheme != 'https' or parsed.netloc != 'api.telegram.org'
            or not parsed.path.startswith('/file/bot' + cfg.token + '/') or parsed.query or parsed.fragment):
        raise ExtractionError('Telegram не предоставил безопасную ссылку на файл. Попробуй позже.')
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=True) as client:
        async with client.stream('GET', address) as response:
            if response.status_code != 200:
                raise ExtractionError('Не удалось скачать оригинал из Telegram. Попробуй позже.')
            content = bytearray()
            async for chunk in response.aiter_bytes(65536):
                content.extend(chunk)
                if len(content) > MAX_BYTES:
                    raise ExtractionError('Файл больше 8 МБ. Пришли меньшую копию.')
    return bytes(content)


async def parse_local(content, mode):
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    # Separate process can be killed on timeout; parser work never blocks asyncio.
    proc = await asyncio.create_subprocess_exec(sys.executable, '-m', 'assistant_bot.extract_worker', mode,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        cwd=str(Path(__file__).resolve().parent.parent),
        env={key: value for key, value in os.environ.items() if key.upper() in
             ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH', 'LANG', 'LC_ALL')}, **kwargs)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(content), timeout=25)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode or len(out) > 150000:
        raise ExtractionError('Не удалось обработать файл. Оригинал сохранён.')
    try:
        result = json.loads(out)
    except (UnicodeError, ValueError):
        raise ExtractionError('Не удалось прочитать файл. Оригинал сохранён.') from None
    if 'error' in result:
        raise ExtractionError(result['error'])
    return result


async def recognize_cloud(cfg, content, mode, mime):
    if not cfg.yandex_api_key:
        raise ExtractionError('Владелец бота ещё не настроил Yandex API-ключ для распознавания.')
    headers = {'Authorization': 'Api-Key ' + cfg.yandex_api_key,
               'x-folder-id': cfg.yandex_folder_id, 'x-data-logging-enabled': 'false'}
    async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=True) as client:
        if mode == 'image':
            response = await client.post(OCR_ENDPOINT, headers=headers, json={
                'content': base64.b64encode(content).decode('ascii'), 'mimeType': mime,
                'languageCodes': ['ru', 'en'], 'model': 'page'})
        else:
            response = await client.post(STT_ENDPOINT, headers=headers,
                params={'format': 'oggopus', 'lang': 'ru-RU'}, content=content)
    if response.status_code in (401, 403):
        service = 'Vision OCR (yc.ai.vision.execute)' if mode == 'image' else 'SpeechKit (yc.ai.speechkitStt.execute)'
        raise ExtractionError(f'Yandex не разрешил распознавание. Владелец бота должен проверить ключ, права {service} и доступ к каталогу. Оригинал сохранён.')
    if response.status_code == 429:
        raise ExtractionError('Достигнут лимит сервиса распознавания Yandex. Попробуй позже.')
    if not response.is_success:
        raise ExtractionError('Yandex не смог распознать файл. Проверь формат и размер или попробуй позже.')
    try:
        result = response.json()
        if mode == 'image':
            # Live REST responses can wrap the documented annotation in result.
            annotation = result.get('result', result)
            text = annotation.get('textAnnotation', {}).get('fullText', '')
        else:
            text = result.get('result', '')
        if not isinstance(text, str) or not text.strip():
            raise ValueError()
    except (ValueError, AttributeError, UnicodeError):
        raise ExtractionError('Разборчивый текст не найден. Попробуй более чёткий снимок или запись.') from None
    notice = 'В распознавании возможны ошибки. Проверь имена, числа и смысл перед сохранением.'
    if len(text) > 12000:
        notice += ' Текст ограничен 12 000 символами.'
    return dict(text=text[:12000], method='Yandex OCR' if mode == 'image' else 'Yandex SpeechKit', notice=notice)


async def extract(bot, cfg, source):
    mode = mode_for(source)
    if not mode:
        raise ExtractionError('Пока поддерживаются PDF с текстом, TXT/MD, JPEG/PNG и голосовые Ogg Opus до 30 секунд.')
    try:
        content = await download(bot, cfg, source)
        result = await parse_local(content, mode)
        if mode in ('image', 'voice'):
            result = await recognize_cloud(cfg, content, mode, result['mime'])
        if not result.get('text', '').strip():
            raise ExtractionError('В файле не найден текст. Оригинал сохранён.')
        return result
    except httpx.HTTPError:
        raise ExtractionError('Не удалось связаться с Telegram или Yandex. Проверь интернет/VPN и повтори позже.') from None
