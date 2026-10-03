"""Run with: python -m assistant_bot."""

import logging
import sys

from telegram.error import TelegramError

from .bot import build_application
from .config import Config, InstanceLock
from .storage import Store


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # HTTP request URLs contain the Telegram token: never print transport logs.
    logging.getLogger("httpx").disabled = True
    logging.getLogger("httpcore").disabled = True
    store = lock = None
    try:
        config = Config.load()
        lock = InstanceLock(config.database.with_suffix(".lock"))
        store = Store(config.database)
        store.recover_extractions()
        store.recover_archives()
        app = build_application(config, store)
        print("Ассистент запущен для всех пользователей. Для остановки нажми Ctrl+C.")
        print("Каждый пользователь открывает личный чат с ботом и отправляет /start.")
        app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)
        return 0
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except TelegramError as exc:
        print(f"Telegram недоступен ({type(exc).__name__}). Проверь токен, сеть и второй экземпляр бота.", file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()
        if lock:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
