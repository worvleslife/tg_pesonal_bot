"""Public mode no longer depends on a deployment-wide Telegram owner."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_bot.config import Config


class PublicConfigTests(unittest.TestCase):
    def test_ai_config_is_optional_and_never_prints_key(self):
        env = {"BOT_TOKEN": "123456789:" + "x" * 35,
               "YANDEX_API_KEY": "fake-secret-for-offline-test", "YANDEX_FOLDER_ID": "b1gtestfolder12345678",
               "YANDEX_MODEL": "deepseek-v4-flash", "AI_DAILY_LIMIT": "7", "AI_GLOBAL_DAILY_LIMIT": "30"}
        with patch.dict(os.environ, env, clear=True), patch("assistant_bot.config.load_dotenv"):
            cfg = Config.load()
        self.assertEqual(cfg.yandex_api_key, env["YANDEX_API_KEY"])
        self.assertNotIn(env["YANDEX_API_KEY"], repr(cfg))
        self.assertEqual(cfg.ai_model_uri, "gpt://b1gtestfolder12345678/deepseek-v4-flash")
        self.assertEqual((cfg.ai_daily_limit, cfg.ai_global_daily_limit), (7, 30))
        with patch.dict(os.environ, {"BOT_TOKEN": env["BOT_TOKEN"]}, clear=True), patch("assistant_bot.config.load_dotenv"):
            cfg = Config.load()
        self.assertEqual(cfg.yandex_api_key, "")
        self.assertEqual(cfg.ai_model_uri, "")
        self.assertEqual((cfg.ai_daily_limit, cfg.ai_global_daily_limit), (20, 100))

    def test_invalid_ai_limits_and_model_are_rejected(self):
        for name in ("AI_DAILY_LIMIT", "AI_GLOBAL_DAILY_LIMIT"):
            for value in ("0", "-1", "10001", "all", "1.5", ""):
                with self.subTest(name=name, value=value), patch.dict(os.environ, {
                    "BOT_TOKEN": "123456789:" + "x" * 35, name: value,
                }, clear=True), patch("assistant_bot.config.load_dotenv"):
                    with self.assertRaisesRegex(ValueError, name):
                        Config.load()
        with patch.dict(os.environ, {"BOT_TOKEN": "123456789:" + "x" * 35,
                                    "YANDEX_MODEL": "https://some-other-host"}, clear=True), patch("assistant_bot.config.load_dotenv"):
            with self.assertRaisesRegex(ValueError, "YANDEX_MODEL"):
                Config.load()

    def test_yandex_folder_validation_and_no_openai_key_fallback(self):
        env = {"BOT_TOKEN": "123456789:" + "x" * 35,
               "OPENAI_API_KEY": "unrelated-secret", "OPENAI_MODEL": "ignored-model"}
        with patch.dict(os.environ, env, clear=True), patch("assistant_bot.config.load_dotenv"):
            self.assertEqual(Config.load().yandex_api_key, "")
        for folder in ("https://not-a-folder", "b1g../other", "bad\nheader"):
            with self.subTest(folder=folder), patch.dict(os.environ, {
                **env, "YANDEX_FOLDER_ID": folder}, clear=True), patch("assistant_bot.config.load_dotenv"):
                with self.assertRaisesRegex(ValueError, "YANDEX_FOLDER_ID"):
                    Config.load()

    def test_admin_id_is_explicit_optional_and_supports_large_telegram_ids(self):
        for raw, expected in (("", None), ("7267009888", 7267009888)):
            with self.subTest(raw=raw), patch.dict(os.environ, {
                "BOT_TOKEN": "123456789:" + "x" * 35, "ADMIN_ID": raw,
                "TIMEZONE": "Europe/Moscow",
            }, clear=True), patch("assistant_bot.config.load_dotenv"):
                self.assertEqual(Config.load().admin_id, expected)

    def test_invalid_admin_id_fails_instead_of_granting_anyone_access(self):
        for raw in ("0", "-1", "someone", "123,456", str(2**63)):
            with self.subTest(raw=raw), patch.dict(os.environ, {
                "BOT_TOKEN": "123456789:" + "x" * 35, "ADMIN_ID": raw,
                "TIMEZONE": "Europe/Moscow",
            }, clear=True), patch("assistant_bot.config.load_dotenv"):
                with self.assertRaisesRegex(ValueError, "ADMIN_ID"):
                    Config.load()

    def test_legacy_owner_id_does_not_restrict_public_mode(self):
        for owner in ("", "123456", "invalid legacy value"):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as folder:
                env = {"BOT_TOKEN": "123456789:" + "x" * 35, "OWNER_ID": owner,
                       "TIMEZONE": "Europe/Moscow", "DATABASE_PATH": "data/library.sqlite3"}
                with patch.dict(os.environ, env, clear=True), patch(
                    "assistant_bot.config.load_dotenv"
                ), patch("assistant_bot.config.ROOT", Path(folder)):
                    cfg = Config.load()
                self.assertEqual(cfg.timezone, "Europe/Moscow")
                self.assertEqual(cfg.database, Path(folder) / "data/library.sqlite3")
                self.assertFalse(hasattr(cfg, "owner_id"))
                self.assertNotIn(env["BOT_TOKEN"], repr(cfg))


if __name__ == "__main__":
    unittest.main()
