"""Offline work-organizer integration tests, including private callback boundaries."""

import asyncio
from html import unescape
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from telegram import MessageEntity

from assistant_bot import bot
from assistant_bot.config import Config
from assistant_bot.storage import Store


class WorkBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = 123456
        self.other = 999
        self.db = Store(":memory:")
        self.db.set_setting(self.owner, 'auto_inbox', 'off')
        self.db.set_setting(self.other, 'auto_inbox', 'off')
        self.telegram = SimpleNamespace(**{
            name: AsyncMock() for name in (
                "send_message", "send_document", "send_photo", "send_voice", "send_audio",
                "send_video", "send_animation", "send_video_note",
            )
        })
        self.app = SimpleNamespace(bot_data={
            "config": Config("123456:fake_token_for_offline_tests_only"),
            "store": self.db, "lock": asyncio.Lock(),
        })
        self.ctx = SimpleNamespace(application=self.app, user_data={}, bot=self.telegram)
        self.other_ctx = SimpleNamespace(application=self.app, user_data={}, bot=self.telegram)

    async def asyncTearDown(self):
        self.db.close()

    def update(self, text=None, *, user=None, callback=None, caption=None, document=None,
               entities=None, caption_entities=None, chat_type="private", chat_id=None,
               is_bot=False, **media):
        owner = self.owner if user is None else user
        recipient = owner if chat_id is None else chat_id
        message = SimpleNamespace(
            text=text, caption=caption, document=document, photo=None, voice=None,
            audio=None, video=None, animation=None, video_note=None,
            chat_id=recipient, message_id=100, forward_origin=None,
            reply_text=AsyncMock(), edit_text=AsyncMock(), reply_document=AsyncMock(),
            parse_entities=lambda: entities or {},
            parse_caption_entities=lambda: caption_entities or {},
        )
        for name, value in media.items():
            setattr(message, name, value)
        query = (SimpleNamespace(data=callback, answer=AsyncMock(), message=message)
                 if callback is not None else None)
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=owner, is_bot=is_bot, username="tester",
                                           full_name="Тестовый пользователь"),
            effective_chat=SimpleNamespace(type=chat_type, id=recipient),
            effective_message=message, callback_query=query,
        )

    @staticmethod
    def texts(update):
        return "\n".join(call.args[0] for call in update.effective_message.reply_text.await_args_list)

    @staticmethod
    def buttons(update):
        rows = []
        for call in update.effective_message.reply_text.await_args_list:
            markup = call.kwargs.get("reply_markup")
            rows.extend(getattr(markup, "inline_keyboard", ()))
        return [button for row in rows for button in row]

    def callbacks(self, update):
        return [button.callback_data for button in self.buttons(update)]

    def callback_with_prefix(self, update, prefix):
        matches = [value for value in self.callbacks(update) if value and value.startswith(prefix)]
        self.assertTrue(matches, (prefix, self.callbacks(update), self.texts(update)))
        return matches[0]

    async def click(self, data, *, user=None, ctx=None):
        update = self.update(callback=data, user=user)
        await bot.callback(update, ctx or self.ctx)
        return update

    async def say(self, text=None, *, user=None, ctx=None, **kwargs):
        update = self.update(text, user=user, **kwargs)
        await bot.message(update, ctx or self.ctx)
        return update

    async def confirm(self, update, *, user=None, ctx=None):
        return await self.click(self.callback_with_prefix(update, "work:confirm:"), user=user, ctx=ctx)

    async def preview(self, section, *, text="Рабочая инструкция", title="Инструкция",
                      user=None, ctx=None, **kwargs):
        context = ctx or self.ctx
        await self.click(f"work:target:{section['id']}", user=user, ctx=context)
        await self.say(text, user=user, ctx=context, **kwargs)
        return await self.say(title, user=user, ctx=context)

    async def test_main_button_command_and_root_callback_enter_work_mode(self):
        for update, handler in ((self.update("Работа рядом"), bot.message),
                                (self.update("/work"), bot.command),
                                (self.update(callback="work:root:0"), bot.callback)):
            with self.subTest(handler=handler.__name__):
                self.ctx.user_data.clear()
                await handler(update, self.ctx)
                self.assertEqual(self.ctx.user_data.get("state"), "work:browse")
                self.assertIn("Работа рядом", self.texts(update))
                self.assertIn("work:add", self.callbacks(update))
                self.assertTrue(any("Добавить что-то нужное" in button.text
                                    for button in self.buttons(update)))
        self.assertEqual(self.db.count_items(self.owner), 0)

    async def test_work_handlers_obey_private_chat_guard(self):
        for invalid in ({"chat_type": "group"}, {"chat_id": self.other}, {"is_bot": True}):
            for update, handler in ((self.update("/work", **invalid), bot.command),
                                    (self.update("Работа рядом", **invalid), bot.message),
                                    (self.update(callback="work:new:0", **invalid), bot.callback)):
                with self.subTest(invalid=invalid, handler=handler.__name__):
                    await handler(update, self.ctx)
                    update.effective_message.reply_text.assert_not_awaited()
        self.assertEqual(self.db.count_users(), 0)
        self.assertEqual(self.db.count_work_sections(self.owner), 0)

    async def test_add_menu_creates_personal_section_button(self):
        menu = await self.click("work:add")
        self.assertIn("work:new:0", self.callbacks(menu))
        self.assertIn("work:pick:0", self.callbacks(menu))
        await self.click("work:new:0")
        preview = await self.say("Клиенты <важное>")
        self.assertEqual(self.db.count_work_sections(self.owner), 0)
        await self.confirm(preview)
        sections = self.db.list_work_sections(self.owner)
        self.assertEqual([section["title"] for section in sections], ["Клиенты <важное>"])
        root = await self.click("work:root:0")
        self.assertIn(f"work:section:{sections[0]['id']}:0", self.callbacks(root))
        other_root = await self.click("work:root:0", user=self.other, ctx=self.other_ctx)
        self.assertNotIn("Клиенты", self.texts(other_root) + str(self.callbacks(other_root)))
        self.assertEqual(self.db.count_items(self.owner), 0)

    async def test_invalid_and_duplicate_section_names_keep_creation_active(self):
        self.db.create_work_section(self.owner, "Ссылки")
        for invalid in ("ссылки", "x" * 61):
            with self.subTest(invalid=invalid):
                await self.click("work:new:0")
                preview = await self.say(invalid)
                if any(value.startswith("work:confirm:") for value in self.callbacks(preview)):
                    await self.confirm(preview)
                self.assertEqual(self.db.count_work_sections(self.owner), 1)
                self.assertTrue(self.ctx.user_data.get("state", "").startswith("work:"))
                self.assertEqual(self.db.count_items(self.owner), 0)
        await self.confirm(await self.say("Файлы"))
        self.assertEqual(self.db.count_work_sections(self.owner), 2)

    async def test_text_is_saved_only_after_preview_and_replay_is_harmless(self):
        section = self.db.create_work_section(self.owner, "Инструкции")
        update = await self.preview(section, text="Текст <b>не HTML</b>", title="Как <работать>")
        self.assertEqual(self.db.count_work_materials(self.owner), 0)
        self.assertEqual(self.db.count_items(self.owner), 0)
        self.assertIn("&lt;работать&gt;", self.texts(update))
        confirm = self.callback_with_prefix(update, "work:confirm:")
        result = await self.click(confirm)
        items = self.db.list_work_materials(self.owner, section["id"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Как <работать>")
        self.assertEqual(items[0]["text"], "Текст <b>не HTML</b>")
        self.assertEqual(items[0]["source_chat_id"], self.owner)
        self.assertEqual(items[0]["source_message_id"], 100)
        self.assertIn(f"work:target:{section['id']}", self.callbacks(result))
        await self.click(confirm)
        self.assertEqual(self.db.count_work_materials(self.owner), 1)

    async def test_replacing_preview_invalidates_old_confirmation(self):
        section = self.db.create_work_section(self.owner, "Работа")
        first = await self.preview(section, text="Первый", title="Первый")
        old = self.callback_with_prefix(first, "work:confirm:")
        second = await self.preview(section, text="Второй", title="Второй")
        current = self.callback_with_prefix(second, "work:confirm:")
        self.assertNotEqual(old, current)
        await self.click(old)
        self.assertEqual(self.db.count_work_materials(self.owner), 0)
        await self.click(current)
        self.assertEqual(self.db.list_work_materials(self.owner, section["id"])[0]["text"], "Второй")

    async def test_other_user_cannot_confirm_preview_nonce(self):
        first_section = self.db.create_work_section(self.owner, "Личное")
        second_section = self.db.create_work_section(self.other, "Личное")
        first = await self.preview(first_section, text="ПЕРВЫЙ_СЕКРЕТ")
        second = await self.preview(second_section, user=self.other, ctx=self.other_ctx,
                                    text="ВТОРОЙ_СЕКРЕТ")
        first_token = self.callback_with_prefix(first, "work:confirm:")
        second_token = self.callback_with_prefix(second, "work:confirm:")
        hostile = await self.click(first_token, user=self.other, ctx=self.other_ctx)
        self.assertNotIn("ПЕРВЫЙ_СЕКРЕТ", self.texts(hostile))
        self.assertEqual(self.db.count_work_materials(self.owner), 0)
        self.assertEqual(self.db.count_work_materials(self.other), 0)
        await self.click(second_token, user=self.other, ctx=self.other_ctx)
        await self.click(first_token)
        self.assertEqual(self.db.list_work_materials(self.owner, first_section["id"])[0]["text"],
                         "ПЕРВЫЙ_СЕКРЕТ")
        self.assertEqual(self.db.list_work_materials(self.other, second_section["id"])[0]["text"],
                         "ВТОРОЙ_СЕКРЕТ")

    async def test_document_caption_hidden_link_and_forward_are_preserved(self):
        section = self.db.create_work_section(self.owner, "Документы")
        entity = MessageEntity("text_link", 0, 4, url="https://example.com/private-guide")
        update = await self.preview(
            section, text=None, title="Шаблон", caption="Гайд и шаблон",
            document=SimpleNamespace(file_id="saved-document-id", file_name="template.pdf"),
            caption_entities={entity: "Гайд"}, forward_origin=SimpleNamespace(type="user"),
        )
        await self.click(self.callback_with_prefix(update, "work:confirm:"))
        material = self.db.list_work_materials(self.owner, section["id"])[0]
        self.assertEqual(material["kind"], "document")
        self.assertEqual(material["file_id"], "saved-document-id")
        self.assertEqual(material["file_name"], "template.pdf")
        self.assertEqual(material["text"], "Гайд и шаблон")
        self.assertIn("https://example.com/private-guide", material["urls"])
        await self.click(f"work:open:{material['id']}")
        self.telegram.send_document.assert_awaited_once()
        args = self.telegram.send_document.await_args
        self.assertIn("saved-document-id", str(args))
        self.assertIn(str(self.owner), str(args))

    async def test_supported_media_are_saved_and_reopened_without_download(self):
        section = self.db.create_work_section(self.owner, "Вложения")
        for kind in ("photo", "voice", "audio", "video", "animation", "video_note"):
            with self.subTest(kind=kind):
                file = SimpleNamespace(file_id=f"saved-{kind}", file_name=f"file-{kind}")
                update = await self.preview(section, text=None, title=kind,
                                            **{kind: [file] if kind == "photo" else file})
                await self.click(self.callback_with_prefix(update, "work:confirm:"))
                material = self.db.list_work_materials(self.owner, section["id"])[0]
                self.assertEqual(material["kind"], kind)
                self.assertEqual(material["file_id"], f"saved-{kind}")
                await self.click(f"work:open:{material['id']}")
                getattr(self.telegram, f"send_{kind}").assert_awaited_once()

    async def test_create_first_section_resumes_material_input(self):
        picker = await self.click("work:pick:0")
        self.assertEqual(self.db.count_work_sections(self.owner), 0)
        create = self.callback_with_prefix(picker, "work:new:1")
        await self.click(create)
        await self.confirm(await self.say("Первый раздел"))
        await self.say("Содержимое первого материала")
        preview = await self.say("Первый материал")
        self.assertEqual(self.db.count_work_materials(self.owner), 0)
        await self.click(self.callback_with_prefix(preview, "work:confirm:"))
        section = self.db.list_work_sections(self.owner)[0]
        item = self.db.list_work_materials(self.owner, section["id"])[0]
        self.assertEqual(item["text"], "Содержимое первого материала")
        self.assertEqual(self.db.count_items(self.owner), 0)

    async def test_cancel_discards_preview_and_work_browse_never_saves_to_library(self):
        section = self.db.create_work_section(self.owner, "Черновики")
        for value, handler in (("/cancel", bot.command), ("work:cancel", bot.callback)):
            with self.subTest(cancel=value):
                preview = await self.preview(section)
                confirm = self.callback_with_prefix(preview, "work:confirm:")
                update = self.update(value) if handler is bot.command else self.update(callback=value)
                await handler(update, self.ctx)
                await self.click(confirm)
                self.assertEqual(self.db.count_work_materials(self.owner), 0)
                self.assertNotIn("work_draft", self.ctx.user_data)
                self.assertNotIn("work_pending", self.ctx.user_data)
                await self.say("Случайный текст после отмены")
                self.assertEqual(self.db.count_items(self.owner), 0)

    async def test_leaving_work_via_commands_or_menu_invalidates_pending_save(self):
        section = self.db.create_work_section(self.owner, "Работа")
        for text, handler in (("/menu", bot.command), ("/library", bot.command),
                              ("📚 База знаний", bot.message), ("/id", bot.identity)):
            with self.subTest(text=text):
                preview = await self.preview(section)
                confirm = self.callback_with_prefix(preview, "work:confirm:")
                await handler(self.update(text), self.ctx)
                self.assertNotIn("work_draft", self.ctx.user_data)
                self.assertNotIn("work_pending", self.ctx.user_data)
                await self.click(confirm)
                self.assertEqual(self.db.count_work_materials(self.owner), 0)

    async def test_foreign_section_and_material_callbacks_never_disclose_or_mutate(self):
        section = self.db.create_work_section(self.owner, "СЕКРЕТНЫЙ_РАЗДЕЛ")
        item = self.db.add_work_material(self.owner, section["id"], title="СЕКРЕТНЫЙ_ЗАГОЛОВОК",
                                         text="СЕКРЕТНЫЙ_ТЕКСТ", kind="document",
                                         file_id="PRIVATE_FILE_ID")
        own = self.db.create_work_section(self.other, "Доступный")
        payloads = [f"work:{action}:{section['id']}{suffix}"
                    for action, suffix in (("section", ":0"), ("manage", ""), ("target", ""),
                                           ("rename_s", ""), ("shift", ":1"), ("delete_s", ""))]
        payloads += [f"work:{action}:{item['id']}{suffix}"
                     for action, suffix in (("card", ":0"), ("open", ""), ("rename_m", ""),
                                            ("move", ":0"), ("delete_m", ""))]
        payloads.append(f"work:move_to:{item['id']}:{own['id']}")
        for payload in payloads:
            with self.subTest(payload=payload):
                result = await self.click(payload, user=self.other, ctx=self.other_ctx)
                response = self.texts(result) + str(self.buttons(result))
                for secret in ("СЕКРЕТНЫЙ_РАЗДЕЛ", "СЕКРЕТНЫЙ_ЗАГОЛОВОК", "СЕКРЕТНЫЙ_ТЕКСТ",
                               "PRIVATE_FILE_ID"):
                    self.assertNotIn(secret, response)
                self.assertEqual(self.db.get_work_material(self.owner, item["id"]), item)
                self.assertIsNotNone(self.db.get_work_section(self.owner, section["id"]))
                self.telegram.send_document.assert_not_awaited()
        self.assertEqual(self.db.count_work_materials(self.other), 0)

    async def test_move_rename_and_reorder_owned_content(self):
        first = self.db.create_work_section(self.owner, "Первый")
        second = self.db.create_work_section(self.owner, "Второй")
        item = self.db.add_work_material(self.owner, first["id"], title="До", text="Материал")
        await self.click(f"work:rename_m:{item['id']}")
        await self.confirm(await self.say("После"))
        self.assertEqual(self.db.get_work_material(self.owner, item["id"])["title"], "После")
        await self.click(f"work:rename_s:{first['id']}")
        await self.confirm(await self.say("Новый первый"))
        self.assertEqual(self.db.get_work_section(self.owner, first["id"])["title"], "Новый первый")
        picker = await self.click(f"work:move:{item['id']}:0")
        self.assertIn(f"work:move_to:{item['id']}:{second['id']}", self.callbacks(picker))
        await self.confirm(await self.click(f"work:move_to:{item['id']}:{second['id']}"))
        self.assertEqual(self.db.get_work_material(self.owner, item["id"])["section_id"], second["id"])
        await self.confirm(await self.click(f"work:shift:{second['id']}:-1"))
        self.assertEqual([value["id"] for value in self.db.list_work_sections(self.owner)],
                         [second["id"], first["id"]])

    async def test_own_material_cannot_be_moved_to_foreign_section(self):
        own = self.db.create_work_section(self.owner, "Моё")
        foreign = self.db.create_work_section(self.other, "СЕКРЕТНЫЙ_РАЗДЕЛ")
        item = self.db.add_work_material(self.owner, own["id"], title="Материал", text="Мой текст")
        result = await self.click(f"work:move_to:{item['id']}:{foreign['id']}")
        self.assertNotIn("СЕКРЕТНЫЙ_РАЗДЕЛ", self.texts(result))
        self.assertEqual(self.db.get_work_material(self.owner, item["id"])["section_id"], own["id"])

    async def test_material_delete_requires_confirmation_and_is_replay_safe(self):
        section = self.db.create_work_section(self.owner, "Раздел")
        item = self.db.add_work_material(self.owner, section["id"], title="Удаляемый", text="Текст")
        request = await self.click(f"work:delete_m:{item['id']}")
        confirm = self.callback_with_prefix(request, "work:confirm:")
        self.assertIsNotNone(self.db.get_work_material(self.owner, item["id"]))
        await self.click(confirm, user=self.other, ctx=self.other_ctx)
        self.assertIsNotNone(self.db.get_work_material(self.owner, item["id"]))
        await self.click(confirm)
        self.assertIsNone(self.db.get_work_material(self.owner, item["id"]))
        await self.click(confirm)
        self.assertIsNotNone(self.db.get_work_section(self.owner, section["id"]))

    async def test_section_delete_requires_confirmation_and_removes_its_contents_only(self):
        section = self.db.create_work_section(self.owner, "Удаляемый")
        retained = self.db.create_work_section(self.owner, "Остаётся")
        deleted_item = self.db.add_work_material(self.owner, section["id"], title="Удалить", text="Текст")
        kept_item = self.db.add_work_material(self.owner, retained["id"], title="Оставить", text="Текст")
        request = await self.click(f"work:delete_s:{section['id']}")
        self.assertIsNotNone(self.db.get_work_section(self.owner, section["id"]))
        self.assertIsNotNone(self.db.get_work_material(self.owner, deleted_item["id"]))
        await self.click(self.callback_with_prefix(request, "work:confirm:"))
        self.assertIsNone(self.db.get_work_section(self.owner, section["id"]))
        self.assertIsNone(self.db.get_work_material(self.owner, deleted_item["id"]))
        self.assertIsNotNone(self.db.get_work_material(self.owner, kept_item["id"]))

    async def test_section_and_material_lists_paginate_and_keep_owners_separate(self):
        sections = [self.db.create_work_section(self.owner, f"Раздел {index}") for index in range(10)]
        self.db.create_work_section(self.other, "СЕКРЕТНЫЙ_РАЗДЕЛ")
        first = await self.click("work:root:0")
        self.assertEqual(sum(value.startswith("work:section:") for value in self.callbacks(first)), 8)
        self.assertIn("work:root:8", self.callbacks(first))
        second = await self.click("work:root:8")
        self.assertEqual(sum(value.startswith("work:section:") for value in self.callbacks(second)), 2)
        self.assertNotIn("СЕКРЕТНЫЙ_РАЗДЕЛ", str(self.buttons(first)) + str(self.buttons(second)))
        section = sections[0]
        for index in range(10):
            self.db.add_work_material(self.owner, section["id"], title=f"Материал {index}", text="Текст")
        material_first = await self.click(f"work:section:{section['id']}:0")
        self.assertEqual(sum(value.startswith("work:card:") for value in self.callbacks(material_first)), 8)
        self.assertIn(f"work:section:{section['id']}:8", self.callbacks(material_first))
        material_second = await self.click(f"work:section:{section['id']}:8")
        self.assertEqual(sum(value.startswith("work:card:") for value in self.callbacks(material_second)), 2)

    async def test_long_material_card_escapes_html_and_offers_next_page(self):
        section = self.db.create_work_section(self.owner, "Длинные")
        text = "Пример <b> и & символы 🐱\n" * 300
        item = self.db.add_work_material(self.owner, section["id"], title="Текст <пример>", text=text)
        result = await self.click(f"work:card:{item['id']}:0")
        self.assertIn("&lt;пример&gt;", self.texts(result))
        self.assertIn("&lt;b&gt;", self.texts(result))
        self.assertIn(f"work:card:{item['id']}:1", self.callbacks(result))
        for call in result.effective_message.reply_text.await_args_list:
            rendered = unescape(call.args[0])
            self.assertLessEqual(len(rendered.encode("utf-16-le")) // 2, 4096)


if __name__ == "__main__":
    unittest.main()
