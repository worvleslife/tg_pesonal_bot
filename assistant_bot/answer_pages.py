"""Split plain answer text into lossless Telegram-sized pages."""

from bisect import bisect_right


def paginate_answer(text: str, limit: int = 4096) -> list[str]:
    """Return pages of at most ``limit`` UTF-16 units, preserving all text.

    Split after paragraphs, lines, or spaces where the page is over 60% full;
    otherwise split at a Unicode code point. Astral characters count as two
    units and are never divided. Empty text returns an empty list. A limit
    unable to hold even one character raises ValueError. Callers that use
    Telegram HTML must escape each resulting page *after* pagination.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("The page limit must be a positive integer.")

    units = [0]
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if width > limit:
            raise ValueError("The page limit cannot hold a character in the answer.")
        units.append(units[-1] + width)

    pages = []
    start = 0
    while start < len(text):
        end = bisect_right(units, units[start] + limit, lo=start + 1) - 1
        if end < len(text):
            for separators in (("\n\n", "\r\n\r\n"), ("\n",), (" ", "\t")):
                boundary = max(
                    (index + len(separator)
                     for separator in separators
                     if (index := text.rfind(separator, start, end)) >= start),
                    default=start,
                )
                if (units[boundary] - units[start]) * 5 > limit * 3:
                    end = boundary
                    break
        pages.append(text[start:end])
        start = end
    return pages
