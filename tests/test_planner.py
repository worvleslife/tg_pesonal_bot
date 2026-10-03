"""Assistant workflows use temporary storage and mocked Telegram/AI only."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from html import unescape

import test_work_bot as fixtures
from assistant_bot import bot, planner
from assistant_bot.storage import Store
from assistant_bot.service import dispatch_due, today_text


class PlannerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.WorkBotTests.asyncSetUp
    asyncTearDown = fixtures.WorkBotTests.asyncTearDown
    update = fixtures.WorkBotTests.update
    texts = staticmethod(fixtures.WorkBotTests.texts)
    buttons = staticmethod(fixtures.WorkBotTests.buttons)
    callbacks = fixtures.WorkBotTests.callbacks
    callback_with_prefix = fixtures.WorkBotTests.callback_with_prefix
    click = fixtures.WorkBotTests.click
    say = fixtures.WorkBotTests.say

    async def confirm(self, update, **kwargs):
        return await self.click(self.callback_with_prefix(update,'plan:confirm:'), **kwargs)

    def task(self, title='Позвонить клиенту', owner=None):
        return self.db.add_tasks(owner or self.owner,[title])[0]

    async def test_menu_is_six_buttons_and_secondary_actions_remain_accessible(self):
        self.assertEqual(len(bot.MAIN.inline_keyboard),3)
        self.assertEqual(sum(map(len,bot.MAIN.inline_keyboard)),6)
        for text, marker in [('🧭 Сегодня','plan:capture'),('☰ Ещё','settings'),
                             ('📚 Мои материалы','favorites')]:
            view = await self.say(text)
            self.assertIn(marker,self.callbacks(view))

    async def test_capture_is_confirmed_atomic_owner_scoped_and_replay_safe(self):
        await self.say('📥 Разгрузить голову')
        view = await self.say('1. Позвонить\n- Подготовить договор\n• Проверить <отчёт>')
        token = self.callback_with_prefix(view,'plan:confirm:')
        self.assertEqual(self.db.task_counts(self.owner)['active'],0)
        await self.click(token,user=self.other,ctx=self.other_ctx)
        self.assertEqual(self.db.task_counts(self.other)['active'],0)
        await self.click(token)
        await self.click(token)
        self.assertEqual([t['title'] for t in self.db.list_tasks(self.owner)],
                         ['Позвонить','Подготовить договор','Проверить <отчёт>'])
        self.assertEqual(self.db.count_items(self.owner),0)

    async def test_new_capture_and_navigation_invalidate_old_preview(self):
        await self.click('plan:capture')
        old = await self.say('Первое')
        token = self.callback_with_prefix(old,'plan:confirm:')
        await self.click('plan:capture')
        current = await self.say('Второе')
        await self.click(token)
        await self.confirm(current)
        self.assertEqual(self.db.list_tasks(self.owner)[0]['title'],'Второе')
        await self.click('plan:capture')
        view=await self.say('Отменённое')
        await self.say('Работа рядом')
        await self.confirm(view)
        self.assertEqual(self.db.task_counts(self.owner)['active'],1)

    async def test_cancel_and_browse_do_not_save_accidental_notes(self):
        await self.click('plan:capture')
        await bot.command(self.update('/cancel'),self.ctx)
        await self.say('Не сохранять этот текст')
        self.assertEqual(self.db.count_items(self.owner),0)
        self.assertEqual(self.db.task_counts(self.owner)['active'],0)

    async def test_batch_limits_emoji_and_html_preview(self):
        await self.click('plan:capture')
        for text in ['', 'x'*301, '\n'.join(['Дело']*13)]:
            view=await self.say(text)
            self.assertNotIn('plan:confirm',str(self.callbacks(view)))
        view=await self.say('\n'.join(['😀<&>'*60]*12))
        visible=unescape(self.texts(view))
        self.assertLess(len(visible.encode('utf-16-le'))//2,4096)
        self.assertIn('&lt;',self.texts(view))
        await self.confirm(view)
        self.assertEqual(self.db.task_counts(self.owner)['active'],12)

    async def test_foreign_task_all_actions_and_sources_are_private(self):
        task=self.task('PRIVATE_TASK')
        own=self.task('Моё',owner=self.other)
        for suffix in [f'task:{task["id"]}',f'done:{task["id"]}',f'restore:{task["id"]}',
                       f'pin:{task["id"]}:1',f'rename:{task["id"]}',f'estimate:{task["id"]}',
                       f'time:{task["id"]}:5',f'focus:{task["id"]}',f'start:{task["id"]}:5',f'stop:{task["id"]}']:
            view=await self.click('plan:'+suffix,user=self.other,ctx=self.other_ctx)
            self.assertNotIn('PRIVATE_TASK',self.texts(view))
            self.assertEqual(self.db.get_task(self.owner,task['id']),task)
        item=self.db.add_item(self.owner,'PRIVATE_TEXT','PRIVATE_TITLE','Личное',[])
        for action in ('from','analyze'):
            view=await self.click(f'plan:{action}:library:{item["id"]}',user=self.other,ctx=self.other_ctx)
            self.assertNotIn('PRIVATE',self.texts(view))
        self.assertEqual(self.db.get_task(self.other,own['id'])['status'],'active')

    async def test_source_link_survives_restart_and_handles_deleted_material(self):
        section=self.db.create_work_section(self.owner,'Работа')
        item=self.db.add_work_material(self.owner,section['id'],title='Гайд',text='Текст')
        await self.click(f'plan:from:work:{item["id"]}')
        await self.confirm(await self.say('Изучить гайд'))
        task=self.db.list_tasks(self.owner)[0]
        view=await self.click(f'plan:task:{task["id"]}')
        self.assertIn(f'work:card:{item["id"]}:0',self.callbacks(view))
        self.db.delete_work_section(self.owner,section['id'])
        view=await self.click(f'plan:task:{task["id"]}')
        self.assertIn('удалён',self.texts(view))
        self.assertIsNotNone(self.db.get_task(self.owner,task['id']))

    async def test_priority_and_time_choose_real_owned_task(self):
        slow=self.task('Долгое')
        quick=self.task('Короткое')
        foreign=self.task('PRIVATE_OTHER',owner=self.other)
        await self.click(f'plan:time:{quick["id"]}:5')
        await self.click(f'plan:pin:{slow["id"]}:1')
        view=await self.click('plan:choose:5')
        self.assertIn('Короткое',self.texts(view))
        self.assertNotIn('Долгое',self.texts(view))
        await self.click(f'plan:pin:{quick["id"]}:1')
        self.assertEqual(self.db.get_task(self.owner,slow['id'])['priority'],0)
        self.assertEqual(self.db.get_task(self.other,foreign['id'])['priority'],0)
        await self.click(f'plan:done:{quick["id"]}')
        view=await self.click('plan:choose:5')
        self.assertIn('Нет открытых дел',self.texts(view))

    async def test_focus_confirm_repeat_stop_and_done(self):
        task=self.task()
        preview=await self.click(f'plan:start:{task["id"]}:5')
        self.assertEqual(self.db.stats(self.owner)['pending'],0)
        token=self.callback_with_prefix(preview,'plan:confirm:')
        await self.click(token)
        await self.click(token)
        self.assertEqual(self.db.stats(self.owner)['pending'],1)
        first=self.db.get_task(self.owner,task['id'])['focus_reminder_id']
        other=self.task('Другое')
        busy=await self.confirm(await self.click(f'plan:start:{other["id"]}:5'))
        self.assertIn('уже идёт',self.texts(busy))
        self.assertEqual(self.db.stats(self.owner)['pending'],1)
        await self.click(f'plan:done:{task["id"]}')
        self.assertEqual(self.db.get_reminder(self.owner,first)['status'],'cancelled')
        await self.click(f'plan:restore:{task["id"]}')
        await self.confirm(await self.click(f'plan:start:{task["id"]}:15'))
        second=self.db.get_task(self.owner,task['id'])['focus_reminder_id']
        await self.click(f'plan:stop:{task["id"]}')
        self.assertEqual(self.db.get_reminder(self.owner,second)['status'],'cancelled')
        self.assertEqual(self.db.get_task(self.owner,task['id'])['status'],'active')

    async def test_focus_delivery_has_task_buttons_and_preserves_other_user(self):
        task=self.task()
        self.db.start_task_focus(self.owner,task['id'],5,'Europe/Moscow')
        reminder=self.db.get_task(self.owner,task['id'])['focus_reminder_id']
        due=self.db.get_reminder(self.owner,reminder)['due_at']
        await dispatch_due(self.db,self.telegram,now=due+1)
        call=self.telegram.send_message.await_args
        self.assertEqual(call.kwargs['chat_id'],self.owner)
        buttons=[b.callback_data for row in call.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn(f'plan:done:{task["id"]}',buttons)
        await self.click(buttons[0],user=self.other,ctx=self.other_ctx)
        self.assertEqual(self.db.get_task(self.owner,task['id'])['status'],'active')

    async def test_ai_material_context_requires_confirmation_uses_existing_quota_flow(self):
        item=self.db.add_item(self.owner,'OWN_TEXT','Гайд','Работа',[])
        self.db.add_item(self.other,'FOREIGN_SECRET','Секрет','Работа',[])
        self.app.bot_data['config']=replace(self.app.bot_data['config'],yandex_api_key='fake-key',yandex_folder_id='b1testfolder')
        with patch('assistant_bot.planner.handle_chat_message',new_callable=AsyncMock) as request:
            preview=await self.click(f'plan:analyze:library:{item["id"]}')
            request.assert_not_awaited()
            self.assertIn('Yandex AI Studio',self.texts(preview))
            token=self.callback_with_prefix(preview,'plan:confirm:')
            await self.click(token)
            request.assert_awaited_once()
            prompt=request.await_args.kwargs['prompt']
            self.assertIn('OWN_TEXT',prompt)
            self.assertNotIn('FOREIGN_SECRET',prompt)
            self.assertEqual(self.ctx.user_data['state'],'ai')
            await self.click(token)
            request.assert_awaited_once()

    async def test_invalid_callbacks_and_group_guard(self):
        for data in ('plan:confirm:é','plan:task:-1','plan:task:999999999999999999999999',
                     'plan:choose:999','plan:list:all:0','plan:unknown'):
            await self.click(data)
        for value,handler in [('/plan',bot.command),('🧭 Сегодня',bot.message),('plan:capture',bot.callback)]:
            update=self.update(callback=value,chat_type='group') if handler is bot.callback else self.update(value,chat_type='group')
            await handler(update,self.ctx)
            update.effective_message.reply_text.assert_not_awaited()

    async def test_ai_context_reaches_provider_through_normal_history_and_quota(self):
        item=self.db.add_item(self.owner,'SOURCE_ONLY','Гайд','Работа',[])
        self.app.bot_data['config']=replace(self.app.bot_data['config'],yandex_api_key='fake-key',yandex_folder_id='b1testfolder')
        jobs=[]
        def schedule(coroutine):
            task=asyncio.create_task(coroutine)
            jobs.append(task)
            return task
        self.app.create_task=schedule
        with patch('assistant_bot.chat.generate_reply',new_callable=AsyncMock,return_value='Суть и три шага') as provider:
            await self.confirm(await self.click(f'plan:analyze:library:{item["id"]}'))
            await asyncio.wait_for(asyncio.gather(*jobs),timeout=3)
            provider.assert_awaited_once()
            self.assertIn('SOURCE_ONLY',provider.await_args.kwargs['text'])
            self.assertEqual(self.db.chat_history(self.owner)[-1]['content'],'Суть и три шага')
            self.assertEqual(self.db.chat_history(self.other),[])

    async def test_material_caption_matching_menu_still_enters_work_draft(self):
        from types import SimpleNamespace
        section=self.db.create_work_section(self.owner,'Материалы')
        await self.click(f'work:target:{section["id"]}')
        await self.say(caption='Работа рядом',document=SimpleNamespace(file_id='doc',file_name='guide.pdf'))
        self.assertEqual(self.ctx.user_data['state'],'work:material_title')
        self.assertEqual(self.ctx.user_data['work_draft']['material']['text'],'Работа рядом')

    async def test_daily_digest_includes_only_own_tasks(self):
        self.task('OWN_DAILY_TASK')
        self.task('FOREIGN_DAILY_TASK',owner=self.other)
        digest=today_text(self.db,self.owner,'Europe/Moscow')
        self.assertIn('OWN_DAILY_TASK',digest)
        self.assertNotIn('FOREIGN_DAILY_TASK',digest)

    async def test_expired_undelivered_focus_is_cancelled_when_new_session_starts(self):
        task=self.task()
        with patch('assistant_bot.storage.time.time',return_value=1000):
            self.db.start_task_focus(self.owner,task['id'],5,'Europe/Moscow')
        first=self.db.get_task(self.owner,task['id'])['focus_reminder_id']
        with patch('assistant_bot.storage.time.time',return_value=2000):
            self.db.start_task_focus(self.owner,task['id'],5,'Europe/Moscow')
        self.assertEqual(self.db.get_reminder(self.owner,first)['status'],'cancelled')
        self.assertEqual(self.db.stats(self.owner)['pending'],1)

    async def test_pagination_and_export_include_only_own_tasks(self):
        self.db.add_tasks(self.owner,[f'Дело {i}' for i in range(12)])
        self.task('FOREIGN_SECRET',owner=self.other)
        view=await self.click('plan:list:active:0')
        self.assertIn('plan:list:active:8',self.callbacks(view))
        view=await self.click('plan:list:active:8')
        self.assertEqual(len([c for c in self.callbacks(view) if c.startswith('plan:task:')]),4)
        exported=self.db.export_data(self.owner)
        self.assertEqual(len(exported['tasks']),12)
        self.assertNotIn('FOREIGN_SECRET',str(exported))


class TaskPersistenceTests(unittest.TestCase):
    def test_tasks_focus_and_completed_status_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'bot.sqlite3'
            db=Store(path)
            tasks=db.add_tasks(101,['Работа','Отдых'])
            db.edit_task(101,tasks[0]['id'],priority=True,minutes=15)
            db.edit_task(101,tasks[1]['id'],done=True)
            db.start_task_focus(101,tasks[0]['id'],15,'Europe/Moscow')
            db.close()
            db=Store(path)
            try:
                self.assertEqual(db.task_counts(101),{'active':1,'done':1})
                self.assertIsNotNone(db.active_focus(101))
                self.assertIsNone(db.active_focus(202))
                self.assertIsNone(db.get_task(202,tasks[0]['id']))
                self.assertEqual(db.get_task(101,tasks[0]['id'])['minutes'],15)
            finally:
                db.close()

    def test_invalid_batch_rolls_back_and_foreign_sources_rejected(self):
        db=Store(':memory:')
        try:
            item=db.add_item(101,'PRIVATE','Title','Work',[])
            with self.assertRaises(ValueError):
                db.add_tasks(202,['Valid'],source_kind='library',source_id=item['id'])
            with self.assertRaises(ValueError):
                db.add_tasks(101,['Valid','x'*301])
            self.assertEqual(db.task_counts(101)['active'],0)
            self.assertEqual(db.task_counts(202)['active'],0)
        finally:
            db.close()


if __name__=='__main__':
    unittest.main()
