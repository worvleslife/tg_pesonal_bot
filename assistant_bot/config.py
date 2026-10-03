"""Local configuration; credentials never belong in source code."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    timezone: str = "Europe/Moscow"
    database: Path = ROOT / "data" / "assistant.sqlite3"
    admin_id: int | None = None
    yandex_api_key: str = field(default="", repr=False)
    yandex_folder_id: str = ""
    yandex_model: str = "deepseek-v4-flash"
    ai_daily_limit: int = 20
    ai_global_daily_limit: int = 100

    @property
    def ai_model_uri(self) -> str:
        return f"gpt://{self.yandex_folder_id}/{self.yandex_model}" if self.yandex_folder_id else ""

    @classmethod
    def load(cls) -> "Config":
        load_dotenv(ROOT / ".env")
        token = os.getenv("BOT_TOKEN", "").strip()
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Укажи BOT_TOKEN из @BotFather в файле .env рядом с README.md.")
        raw_admin = os.getenv("ADMIN_ID", "").strip()
        if raw_admin and (not re.fullmatch(r"[1-9][0-9]{0,18}", raw_admin)
                          or int(raw_admin) > 2**63 - 1):
            raise ValueError("ADMIN_ID должен быть положительным числовым Telegram ID.")
        timezone = os.getenv("TIMEZONE", "Europe/Moscow").strip()
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Неверный TIMEZONE. Пример: Europe/Moscow. Проверь установку tzdata.") from exc
        path = Path(os.getenv("DATABASE_PATH", "data/assistant.sqlite3")).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        def limit(name: str, default: int) -> int:
            value = os.getenv(name, str(default)).strip()
            if not value.isdigit() or not 1 <= int(value) <= 10000:
                raise ValueError(f"{name} должен быть числом от 1 до 10000.")
            return int(value)
        folder = os.getenv("YANDEX_FOLDER_ID", "").strip()
        if folder and not re.fullmatch(r"[a-z0-9]{6,64}", folder):
            raise ValueError("Укажи идентификатор каталога Yandex AI Studio в YANDEX_FOLDER_ID.")
        model = os.getenv("YANDEX_MODEL", "deepseek-v4-flash").strip()
        if not re.fullmatch(r"deepseek-[a-z0-9.-]{1,70}(?:/latest)?", model):
            raise ValueError("Укажи имя модели DeepSeek в YANDEX_MODEL, например deepseek-v4-flash.")
        # Legacy OWNER_ID is intentionally ignored. Ownership comes only from
        # the authenticated Telegram sender, never from this deployment config.
        return cls(token=token, timezone=timezone, database=path,
                   admin_id=int(raw_admin) if raw_admin else None,
                   yandex_api_key=os.getenv("YANDEX_API_KEY", "").strip(),
                   yandex_folder_id=folder, yandex_model=model,
                   ai_daily_limit=limit("AI_DAILY_LIMIT", 20),
                   ai_global_daily_limit=limit("AI_GLOBAL_DAILY_LIMIT", 100))


class InstanceLock:
    """OS file lock released automatically on crash; prevents two schedulers."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("Бот уже запущен с этой базой данных. Закрой второй экземпляр.") from exc

    def close(self):
        self.file.close()
