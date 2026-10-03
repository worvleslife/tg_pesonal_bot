"""Bounded file parsing in a disposable process; stdin bytes, stdout JSON only."""
from io import BytesIO
import json
import logging
import sys
import warnings

MAX_BYTES = 8_000_000
MAX_TEXT = 12000


def limit_memory():
    """Hard process-memory limit before importing parsers, including on Windows."""
    limit = 384 * 1024 * 1024
    if sys.platform != 'win32':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        return None
    import ctypes as c
    from ctypes import wintypes as w

    class Basic(c.Structure):
        _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64), ('flags', w.DWORD),
                    ('min_ws', c.c_size_t), ('max_ws', c.c_size_t), ('active', w.DWORD),
                    ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]

    class IO(c.Structure):
        _fields_ = [(name, c.c_uint64) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]

    class Extended(c.Structure):
        _fields_ = [('basic', Basic), ('io', IO), ('process_memory', c.c_size_t),
                    ('job_memory', c.c_size_t), ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]

    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.GetCurrentProcess.restype = w.HANDLE
    job = kernel.CreateJobObjectW(None, None)
    info = Extended()
    info.basic.flags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    info.process_memory = limit
    if not job or not kernel.SetInformationJobObject(job, 9, c.byref(info), c.sizeof(info)) or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        raise ValueError('Не удалось включить ограничение памяти для обработки файла.')
    return job  # Keep handle open until this short-lived process exits.


def parse(data, mode):
    if not data or len(data) > MAX_BYTES:
        raise ValueError('Файл пустой или больше 8 МБ.')
    if mode == 'pdf':
        from pypdf import PdfReader
        if not data.startswith(b'%PDF-'):
            raise ValueError('Это не PDF-файл.')
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ValueError('PDF защищён паролем. Пришли незашифрованную копию.')
        total = len(reader.pages)
        parts, missing = [], []
        for index in range(min(total, 20)):
            page = reader.pages[index]
            value = (page.extract_text() or '').strip()
            if not value:
                missing.append(index + 1)
            parts.append(f'[Страница {index + 1}]\n{value}' if value else '')
            if sum(map(len, parts)) > MAX_TEXT:
                break
        raw = '\n\n'.join(p for p in parts if p)
        notices = ['Извлечён текстовый слой; текст внутри изображений не распознан.']
        if total > len(parts):
            notices.append(f'Обработаны первые {len(parts)} из {total} страниц.')
        if missing:
            notices.append('Нет текста на страницах: ' + ', '.join(map(str, missing)) + '.')
        if len(raw) > MAX_TEXT:
            notices.append('Текст ограничен 12 000 символами.')
        if not raw:
            raise ValueError('В PDF нет доступного текстового слоя. Пришли нужные страницы как фото для OCR.')
        return dict(text=raw[:MAX_TEXT], method='PDF: текстовый слой', notice=' '.join(notices))
    if mode == 'text':
        try:
            raw = data.decode('utf-8-sig')
        except UnicodeError:
            raise ValueError('Текстовый файл должен быть в UTF-8.') from None
        if any(ord(c) < 32 and c not in '\n\r\t' for c in raw):
            raise ValueError('Файл содержит двоичные данные вместо текста.')
        return dict(text=raw[:MAX_TEXT], method='Текст UTF-8', notice='Текст ограничен 12 000 символами.' if len(raw) > MAX_TEXT else '')
    if mode == 'image':
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 20_000_000
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as picture:
            if picture.format not in ('JPEG', 'PNG') or picture.width * picture.height > 20_000_000:
                raise ValueError('Нужен JPEG или PNG до 20 мегапикселей.')
            mime = 'image/jpeg' if picture.format == 'JPEG' else 'image/png'
            picture.verify()
        if len(data) > 4_000_000:
            raise ValueError('Для OCR пришли изображение до 4 МБ.')
        return dict(mime=mime)
    if mode == 'voice':
        # Validate a complete single-stream mono Opus container, including length.
        if len(data) > 1_000_000:
            raise ValueError('Голосовое должно быть не больше 1 МБ и 30 секунд.')
        offset, serial, seq, granule, first, ended = 0, None, 0, 0, True, False
        while offset < len(data):
            h = data[offset:offset + 27]
            if len(h) < 27 or h[:4] != b'OggS' or h[4] != 0 or ended:
                raise ValueError('Нужна целая голосовая запись Ogg Opus.')
            stream = h[14:18]
            if serial is not None and serial != stream or int.from_bytes(h[18:22], 'little') != seq:
                raise ValueError('Многопоточная или повреждённая запись не поддерживается.')
            serial, seq = stream, seq + 1
            segments = data[offset + 27:offset + 27 + h[26]]
            end = offset + 27 + h[26] + sum(segments)
            if len(segments) != h[26] or end > len(data):
                raise ValueError('Голосовая запись обрывается.')
            body = data[offset + 27 + h[26]:end]
            if first:
                if not body.startswith(b'OpusHead') or len(body) < 19 or body[9] != 1:
                    raise ValueError('Поддерживаются голосовые Ogg Opus с одним каналом.')
                pre_skip = int.from_bytes(body[10:12], 'little')
                first = False
            value = int.from_bytes(h[6:14], 'little')
            if value != 2**64 - 1:
                granule = max(granule, value)
            ended = bool(h[5] & 4)
            offset = end
        if first or not ended or (granule - pre_skip) / 48000 > 30:
            raise ValueError('Сейчас поддерживаются целые голосовые до 30 секунд. Раздели длинную запись.')
        return dict(mime='audio/ogg')
    raise ValueError('Этот формат пока не поддерживается.')


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        memory_job = limit_memory()
        result = parse(sys.stdin.buffer.read(MAX_BYTES + 1), sys.argv[1])
    except ValueError as exc:
        result = {'error': str(exc)}
    except Exception:
        result = {'error': 'Файл повреждён или не поддерживается. Оригинал сохранён.'}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
