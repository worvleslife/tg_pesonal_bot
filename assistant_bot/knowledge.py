"""Local, transparent organization of supplied text and filenames.

No webpage fetching, document interpretation or external AI is performed here.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_HASHTAG = re.compile(r"(?<!\w)#([\w][\w-]{0,49})", re.UNICODE)
_CATEGORIES: dict[str, tuple[str, ...]] = {
    "Технологии": ("python", "javascript", "typescript", "программ", "разработ", "технолог", "нейросет", "код", "api", "docker", "linux", "github", "ai", "chatgpt", "бот"),
    "Работа": ("работ", "проект", "клиент", "дедлайн", "совещан", "карьер", "ваканс", "резюме", "бизнес", "задач", "коллег"),
    "Обучение": ("обучен", "учеб", "учёб", "курс", "лекци", "урок", "университет", "экзамен", "конспект", "образован", "книг", "tutorial"),
    "Финансы": ("финанс", "бюджет", "инвест", "деньг", "акци", "налог", "кредит", "расход", "доход", "оплат", "эконом", "банк"),
    "Здоровье": ("здоров", "трениров", "спорт", "питан", "врач", "лекарств", "медицин", "фитнес", "зарядк", "сон", "йог"),
    "Идеи": ("идея", "идеи", "идею", "задум", "вдохнов", "придум", "мозгов", "референс"),
    "Личное": ("семь", "семе", "личн", "дом", "рецепт", "путешеств", "отпуск", "покупк", "подар", "друз", "хобби"),
}
_DOMAINS: dict[str, tuple[str, ...]] = {
    "Технологии": ("github.com", "gitlab.com", "stackoverflow.com", "habr.com", "developer.mozilla.org", "pypi.org", "docs.python.org"),
    "Обучение": ("coursera.org", "stepik.org", "edx.org", "udemy.com", "khanacademy.org", "wikipedia.org"),
    "Работа": ("hh.ru", "linkedin.com", "trello.com", "notion.so", "notion.site"),
    "Финансы": ("finam.ru", "banki.ru", "investing.com", "cbr.ru"),
    "Здоровье": ("who.int", "pubmed.ncbi.nlm.nih.gov", "mayoclinic.org"),
    "Идеи": ("pinterest.com", "behance.net", "dribbble.com"),
}


def _normalize_url(value: str) -> str | None:
    value = value.strip().strip("<>").rstrip(".,!?:;]}")
    while value.endswith(")") and value.count(")") > value.count("("):
        value = value[:-1]
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        # Requiring a parsed port rejects malformed links while preserving paths.
        _ = parts.port
        user_info, separator, authority = parts.netloc.rpartition("@")
        netloc = user_info + separator + authority.lower() if separator else parts.netloc.lower()
        return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        return None


def _urls(text: str, supplied: list[str] | None) -> list[str]:
    result, seen = [], set()
    for raw in [*(supplied or []), *_URL.findall(text)]:
        normalized = _normalize_url(raw)
        if normalized is None:
            continue
        parts = urlsplit(normalized)
        key = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, parts.fragment))
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def organize(
    text: str, kind: str = "text", file_name: str | None = None, urls: list[str] | None = None
) -> dict:
    """Suggest a category and tags using only visible text, names and URL hosts."""
    links = _urls(text, urls)
    text_without_links = _URL.sub(" ", text)
    tags = list(dict.fromkeys(match.lower().replace("ё", "е") for match in _HASHTAG.findall(text_without_links)))[:20]
    content = _URL.sub(" ", f"{text} {file_name or ''}").lower().replace("ё", "е")
    words = re.findall(r"\w+", content, flags=re.UNICODE)
    scores = {}
    for category, keywords in _CATEGORIES.items():
        # Short terms must match entire words: "код" must not match "крокодил".
        scores[category] = sum(
            any(word == keyword if len(keyword) <= 3 else word.startswith(keyword.replace("ё", "е")) for word in words)
            for keyword in keywords
        )
    for link in links:
        host = (urlsplit(link).hostname or "").lower()
        for category, domains in _DOMAINS.items():
            if any(host == domain or host.endswith("." + domain) for domain in domains):
                scores[category] += 2
    category = max(scores, key=scores.get)
    if not scores[category]:
        category = "Входящие"

    plain = _HASHTAG.sub("", text_without_links).strip()
    lines = [line.strip(" -—•.,;:()[]{}") for line in plain.splitlines()]
    first_line = next((line for line in lines if line), "")
    if first_line:
        title = first_line
    elif file_name:
        title = file_name
    elif links:
        parts = urlsplit(links[0])
        title = (parts.hostname or "Ссылка") + (parts.path if parts.path != "/" else "")
    else:
        title = {"photo": "Фото", "document": "Документ", "voice": "Голосовая заметка", "video": "Видео", "audio": "Аудио"}.get(kind, "Заметка")
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) > 100:
        title = title[:99].rstrip() + "…"
    return {"title": title, "category": category, "tags": tags, "urls": links}
