"""Real handler integration with a mocked Telegram transport and persistent panel."""
import asyncio
from dataclasses import replace
from html import unescape
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telegram import InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import BadRequest, RetryAfter, TimedOut

import test_work_bot as fixtures
from assistant_bot import bot, panel, chat


class PanelTests(unittest.IsolatedAsyncioTestCase):
    update=fixtures.WorkBotTests.update
    asyncTearDown=fixtures.WorkBotTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.WorkBotTests.asyncSetUp(self)
        self.next_id=1000
        async def send(**kwargs):
            self.next_id+=1
            return SimpleNamespace(message_id=self.next_id)
        self.telegram.send_message.side_effect=send
        self.telegram.edit_message_text=AsyncMock(return_value=True)
        self.telegram.delete_message=AsyncMock(return_value=True)
        self.manager=panel.Panel(self.telegram,self.db)
        self.app.bot_data['panel']=self.manager

    def latest(self):
        return self.telegram.edit_message_text.await_args.kwargs

    def callbacks(self):
        return [b.callback_data for row in self.latest()['reply_markup'].inline_keyboard for b in row]

    async def click(self,data,user=None,ctx=None):
        update=self.update(callback=data,user=user)
        update.effective_message.message_id=self.manager.message_id(user or self.owner) or 100
        await bot.callback(update,ctx or self.ctx)
        return update

    async def test_navigation_across_features_creates_one_message(self):
        await bot.command(self.update('/start'),self.ctx)
        original=self.manager.message_id(self.owner)
        self.assertIsInstance(self.telegram.send_message.await_args.kwargs['reply_markup'],ReplyKeyboardRemove)
        self.assertIsInstance(self.latest()['reply_markup'],InlineKeyboardMarkup)
        for target in ('more','mem:home:0','library','work:root:0','plan:today','learn:home','settings','help','home'):
            await self.click(target)
            self.assertEqual(self.latest()['message_id'],original)
        self.assertEqual(self.telegram.send_message.await_count,1)
        self.telegram.delete_message.assert_not_awaited()

    async def test_project_wizard_text_inputs_share_panel(self):
        await self.click('mem:new')
        await bot.message(self.update('Выставка'),self.ctx)
        await bot.message(self.update('Подготовить стенд'),self.ctx)
        confirm=next(c for c in self.callbacks() if c.startswith('mem:confirm:'))
        await self.click(confirm)
        self.assertEqual(self.db.project_count(self.owner),1)
        self.assertIn('Выставка',self.latest()['text'])
        self.assertEqual(self.telegram.send_message.await_count,1)

    async def test_start_moves_only_tracked_panel_to_bottom(self):
        await self.click('home')
        previous=self.manager.message_id(self.owner)
        await bot.command(self.update('/start'),self.ctx)
        self.assertNotEqual(self.manager.message_id(self.owner),previous)
        self.telegram.delete_message.assert_awaited_once_with(chat_id=self.owner,message_id=previous)
        self.assertEqual(self.telegram.send_message.await_count,2)

    async def test_restart_reuses_persisted_id(self):
        await self.click('home')
        previous=self.manager.message_id(self.owner)
        self.app.bot_data['panel']=panel.Panel(self.telegram,self.db)
        await self.click('library')
        self.assertEqual(self.latest()['message_id'],previous)
        self.assertEqual(self.telegram.send_message.await_count,1)

    async def test_deleted_panel_recovers_but_network_errors_do_not_duplicate(self):
        await self.click('home')
        previous=self.manager.message_id(self.owner)
        self.telegram.edit_message_text.side_effect=BadRequest('Message to edit not found')
        await self.click('library')
        self.assertNotEqual(previous,self.manager.message_id(self.owner))
        self.assertEqual(self.telegram.send_message.await_count,2)
        for error in (TimedOut(),RetryAfter(1),BadRequest("Can't parse entities")):
            self.telegram.edit_message_text.side_effect=error
            with self.assertRaises(type(error)):
                await self.click('more')
        self.assertEqual(self.telegram.send_message.await_count,2)
        self.telegram.edit_message_text.side_effect=BadRequest('Message is not modified')
        await self.click('more')
        self.assertEqual(self.telegram.send_message.await_count,2)

    async def test_different_users_cannot_reuse_each_others_panel(self):
        await self.click('home')
        first=self.manager.message_id(self.owner)
        await self.click('home',self.other,self.other_ctx)
        second=self.manager.message_id(self.other)
        self.assertNotEqual(first,second)
        self.assertEqual(self.latest()['chat_id'],self.other)
        await self.click('more')
        self.assertEqual(self.latest()['chat_id'],self.owner)
        self.assertEqual(self.latest()['message_id'],first)

    async def test_long_material_is_losslessly_paged_in_place(self):
        content='😀 & текст <данные>\n'*700
        item=self.db.add_item(self.owner,content,'Длинный материал','Учёба',[])
        await self.click(f'open:{item["id"]}')
        cached=self.manager.page_cache[self.owner]
        self.assertEqual(''.join(cached['pages']),content.strip())
        for index in range(len(cached['pages'])):
            await self.click(f'panel:page:{cached["token"]}:{index}')
            text=self.latest()['text']
            self.assertLessEqual(len(unescape(text).encode('utf-16-le'))//2,4096)
        self.assertEqual(self.telegram.send_message.await_count,1)
        # Paging doesn't reset a draft/input state belonging to the current view.
        self.ctx.user_data['state']='mem:edit'
        await self.click(f'panel:page:{cached["token"]}:0')
        self.assertEqual(self.ctx.user_data['state'],'mem:edit')

    async def test_stale_or_foreign_page_buttons_cannot_replace_current_screen(self):
        await self.manager.render(self.owner,'x'*8000,bot.HOME)
        token=self.manager.page_cache[self.owner]['token']
        previous=self.telegram.edit_message_text.await_count
        foreign=await self.click(f'panel:page:{token}:1',self.other,self.other_ctx)
        self.assertTrue(foreign.callback_query.answer.await_args.kwargs['show_alert'])
        self.assertEqual(self.telegram.edit_message_text.await_count,previous)
        await self.click('home')
        previous=self.telegram.edit_message_text.await_count
        stale=await self.click(f'panel:page:{token}:1')
        self.assertTrue(stale.callback_query.answer.await_args.kwargs['show_alert'])
        self.assertEqual(self.telegram.edit_message_text.await_count,previous)

    async def test_background_result_cannot_overwrite_navigation(self):
        pending=await self.manager.render(self.owner,'Распознаю…',bot.HOME)
        await self.click('library')
        self.assertFalse(await pending.edit_text('Старый результат',reply_markup=bot.HOME))
        self.assertNotIn('Старый результат',self.latest()['text'])
        active=await self.manager.render(self.owner,'Распознаю другой файл',bot.HOME)
        self.assertTrue(await active.edit_text('Готово',reply_markup=bot.HOME))
        self.assertEqual(self.latest()['text'],'Готово')
        self.assertEqual(self.telegram.send_message.await_count,1)

    async def test_concurrent_background_completion_and_navigation_last_view_wins(self):
        pending=await self.manager.render(self.owner,'Распознаю…',bot.HOME)
        entered,release=asyncio.Event(),asyncio.Event()
        async def slow_edit(**kwargs):
            if kwargs['text']=='Результат':
                entered.set()
                await release.wait()
        self.telegram.edit_message_text.side_effect=slow_edit
        completing=asyncio.create_task(pending.edit_text('Результат',reply_markup=bot.HOME))
        await entered.wait()
        navigation=asyncio.create_task(self.manager.render(self.owner,'Новая страница',bot.MAIN))
        release.set()
        await asyncio.gather(completing,navigation)
        self.assertEqual(self.latest()['text'],'Новая страница')

    async def test_files_are_separate_but_ai_reply_uses_panel(self):
        source=self.db.add_item(self.owner,'','Документ','Учёба',[],kind='document',file_id='file')
        await self.click('home')
        await self.click(f'open:{source["id"]}')
        self.telegram.send_document.assert_awaited_once_with(self.owner,'file')
        update=self.update('Вопрос')
        scope=panel.bind(self.ctx,update)
        try:
            await chat.reply(update,'Ожидание ответа')
        finally:
            panel.unbind(scope)
        update.effective_message.reply_text.assert_not_awaited()
        self.assertEqual(self.telegram.send_message.await_count,1)

    async def test_ai_busy_then_notification_preserves_answer_and_page_buttons(self):
        self.app.bot_data['config']=replace(self.app.bot_data['config'],yandex_api_key='offline',yandex_folder_id='testfolder')
        self.app.create_task=asyncio.create_task
        ready,release=asyncio.Event(),asyncio.Event()
        async def answer(**kwargs):
            ready.set()
            await release.wait()
            return 'Подробный ответ. '*500
        with patch('assistant_bot.chat.generate_reply',answer):
            await self.click('ai:chat')
            await bot.message(self.update('Первый вопрос'),self.ctx)
            await ready.wait()
            task=self.app.bot_data['ai_tasks'][self.owner]
            await bot.message(self.update('Второй вопрос'),self.ctx)
            await self.manager.notify(self.owner,'Напоминание')
            release.set()
            await task
        self.assertIn('Подробный ответ',self.latest()['text'])
        self.assertIn('Напоминание',self.latest()['text'])
        self.assertEqual(len(self.db.chat_history(self.owner)),2)
        token=next(iter(self.app.bot_data['ai_answers']))
        await self.click(f'ai:page:{token}:1')
        self.assertEqual(self.app.bot_data['ai_answers'][token]['page'],1)
        self.assertEqual(self.telegram.send_message.await_count,2)
        group=self.update('/start',chat_type='group')
        await bot.command(group,self.ctx)
        self.assertEqual(self.telegram.send_message.await_count,2)
