"""Private study and resume workflows; no network or production database."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from html import unescape

import test_work_bot as fixtures
from assistant_bot import bot
from assistant_bot.storage import Store


class LearningTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.WorkBotTests.asyncSetUp
    asyncTearDown=fixtures.WorkBotTests.asyncTearDown
    update=fixtures.WorkBotTests.update
    texts=staticmethod(fixtures.WorkBotTests.texts)
    buttons=staticmethod(fixtures.WorkBotTests.buttons)
    callbacks=fixtures.WorkBotTests.callbacks
    callback_with_prefix=fixtures.WorkBotTests.callback_with_prefix
    click=fixtures.WorkBotTests.click
    say=fixtures.WorkBotTests.say

    async def confirm(self,update,**kwargs):
        return await self.click(self.callback_with_prefix(update,'learn:confirm:'),**kwargs)

    async def create_preview(self,question='Что такое функция?',answer='Именованный блок кода'):
        await self.click('learn:new')
        await self.say(question)
        return await self.say(answer)

    async def test_create_preview_replay_cancel_and_navigation(self):
        preview=await self.create_preview()
        self.assertEqual(self.db.study_counts(self.owner)['total'],0)
        await self.confirm(preview,user=self.other,ctx=self.other_ctx)
        self.assertEqual(self.db.study_counts(self.other)['total'],0)
        await self.confirm(preview)
        await self.confirm(preview)
        self.assertEqual(self.db.study_counts(self.owner)['total'],1)
        for label,handler in [('/cancel',bot.command),('/id',bot.identity),('🧭 Сегодня',bot.message),('Работа рядом',bot.message)]:
            preview=await self.create_preview('Отменённый вопрос')
            await handler(self.update(label),self.ctx)
            await self.confirm(preview)
            self.assertEqual(self.db.study_counts(self.owner)['total'],1)
        self.assertEqual(self.db.count_items(self.owner),0)

    async def test_foreign_cards_read_edit_delete_grade_do_not_leak(self):
        card=self.db.create_study_card(self.owner,'PRIVATE_QUESTION','PRIVATE_ANSWER')
        for action in ('card','edit','delete'):
            view=await self.click(f'learn:{action}:{card["id"]}',user=self.other,ctx=self.other_ctx)
            self.assertNotIn('PRIVATE',self.texts(view))
        view=await self.click('learn:review')
        reveal=self.callback_with_prefix(view,'learn:reveal:')
        await self.click(reveal,user=self.other,ctx=self.other_ctx)
        self.assertEqual(self.db.get_study_card(self.owner,card['id']),card)
        self.assertEqual(self.db.list_study_cards(self.other),[])
        self.assertNotIn('PRIVATE',str(self.db.export_data(self.other)))

    async def test_answer_hidden_until_reveal_and_rating_only_once(self):
        card=self.db.create_study_card(self.owner,'Question','SECRET_ANSWER')
        view=await self.click('learn:review')
        self.assertNotIn('SECRET_ANSWER',self.texts(view))
        token=self.ctx.user_data['learn_review']['token']
        await self.click(f'learn:grade:{token}:yes')
        self.assertEqual(self.db.get_study_card(self.owner,card['id'])['revision'],0)
        await self.say('Моя попытка ответа')
        self.assertEqual(self.db.count_items(self.owner),0)
        revealed=await self.click(f'learn:reveal:{token}')
        self.assertIn('SECRET_ANSWER',self.texts(revealed))
        await self.click(f'learn:grade:{token}:yes')
        rated=self.db.get_study_card(self.owner,card['id'])
        self.assertEqual(rated['revision'],1)
        await self.click(f'learn:grade:{token}:yes')
        self.assertEqual(self.db.get_study_card(self.owner,card['id']),rated)
        view=await self.click('learn:review')
        self.assertIn('всё повторено',self.texts(view))

    async def test_edit_and_delete_confirmation_are_owner_scoped(self):
        card=self.db.create_study_card(self.owner,'До','Ответ')
        await self.click(f'learn:edit:{card["id"]}')
        await self.say('После')
        preview=await self.say('Новый ответ')
        await self.confirm(preview)
        changed=self.db.get_study_card(self.owner,card['id'])
        self.assertEqual(changed['question'],'После')
        self.assertEqual(changed['revision'],1)
        preview=await self.click(f'learn:delete:{card["id"]}')
        self.assertIsNotNone(self.db.get_study_card(self.owner,card['id']))
        await self.confirm(preview,user=self.other,ctx=self.other_ctx)
        self.assertIsNotNone(self.db.get_study_card(self.owner,card['id']))
        await self.confirm(preview)
        await self.confirm(preview)
        self.assertIsNone(self.db.get_study_card(self.owner,card['id']))

    async def test_material_source_private_and_deletion_keeps_learning_card(self):
        item=self.db.add_item(self.owner,'Текст','Материал','Учёба',[])
        denied=await self.click(f'learn:from:library:{item["id"]}',user=self.other,ctx=self.other_ctx)
        self.assertNotIn('Материал</b>',self.texts(denied))
        await self.click(f'learn:from:library:{item["id"]}')
        await self.say('Вопрос')
        await self.confirm(await self.say('Ответ'))
        card=self.db.list_study_cards(self.owner)[0]
        view=await self.click(f'learn:card:{card["id"]}')
        self.assertIn(f'item:{item["id"]}',self.callbacks(view))
        self.db.delete_item(self.owner,item['id'])
        view=await self.click(f'learn:card:{card["id"]}')
        self.assertIn('Источник удалён',self.texts(view))
        self.assertEqual(self.db.get_study_card(self.owner,card['id'])['answer'],'Ответ')

    async def test_menu_keeps_six_buttons_and_today_shows_only_due_owned_cards(self):
        self.assertEqual(sum(map(len,bot.MAIN.inline_keyboard)),6)
        self.db.create_study_card(self.other,'PRIVATE','ANSWER')
        view=await self.click('plan:today')
        self.assertNotIn('learn:review',self.callbacks(view))
        self.db.create_study_card(self.owner,'Моё','Ответ')
        view=await self.click('plan:today')
        self.assertIn('learn:review',self.callbacks(view))
        command=self.update('/study')
        await bot.command(command,self.ctx)
        self.assertIn('learn:new',self.callbacks(command))

    async def test_pagination_and_unicode_message_bounds(self):
        for i in range(10):
            self.db.create_study_card(self.owner,f'Вопрос{i}','Ответ')
        view=await self.click('learn:list:0')
        self.assertIn('learn:list:8',self.callbacks(view))
        view=await self.click('learn:list:8')
        self.assertEqual(sum(c.startswith('learn:card:') for c in self.callbacks(view)),2)
        preview=await self.create_preview('😀'*300,'<&😀>'*250)
        self.assertLess(len(unescape(self.texts(preview)).encode('utf-16-le'))//2,4096)
        self.assertIn('&lt;',self.texts(preview))
        await self.confirm(preview)
        self.assertEqual(self.db.study_counts(self.owner)['total'],11)

    async def test_checkpoint_confirm_stops_only_own_timer_and_survives_navigation(self):
        task=self.db.add_tasks(self.owner,['Изучить тему'])[0]
        other=self.db.add_tasks(self.other,['PRIVATE_TASK'])[0]
        self.db.start_task_focus(self.owner,task['id'],25,'Europe/Moscow')
        self.db.start_task_focus(self.other,other['id'],25,'Europe/Moscow')
        await self.click(f'plan:checkpoint:{task["id"]}')
        await self.say('Прочитал первую главу')
        preview=await self.say('Решить пример 3')
        confirm=self.callback_with_prefix(preview,'plan:confirm:')
        self.assertIsNone(self.db.get_checkpoint(self.owner,task['id']))
        await self.click(confirm,user=self.other,ctx=self.other_ctx)
        await self.click(confirm)
        self.assertIsNone(self.db.active_focus(self.owner))
        self.assertIsNotNone(self.db.active_focus(self.other))
        checkpoint=self.db.get_checkpoint(self.owner,task['id'])
        await self.click(confirm)
        self.assertEqual(self.db.get_checkpoint(self.owner,task['id']),checkpoint)
        self.assertEqual(self.db.get_task(self.owner,task['id'])['title'],'Изучить тему')
        await self.say('📚 Мои материалы')
        view=await self.click('plan:today')
        self.assertIn('Решить пример 3',self.texts(view))
        self.assertNotIn('PRIVATE_TASK',self.texts(view))
        await self.click(f'plan:done:{task["id"]}')
        self.assertIsNone(self.db.recent_checkpoint(self.owner))

    async def test_foreign_checkpoint_and_completed_task_cannot_be_modified(self):
        task=self.db.add_tasks(self.owner,['PRIVATE_TASK'])[0]
        view=await self.click(f'plan:checkpoint:{task["id"]}',user=self.other,ctx=self.other_ctx)
        self.assertNotIn('PRIVATE_TASK',self.texts(view))
        self.assertIsNone(self.db.save_checkpoint(self.other,task['id'],'x','y'))
        self.db.edit_task(self.owner,task['id'],done=True)
        self.assertIsNone(self.db.save_checkpoint(self.owner,task['id'],'x','y'))

    async def test_invalid_buttons_and_private_chat_guard(self):
        for callback in ('learn:grade:é:yes','learn:reveal:bad','learn:card:-1','learn:list:999999999999999999999','learn:foo'):
            await self.click(callback)
        for update,handler in [(self.update('/study',chat_type='group'),bot.command),
                               (self.update(callback='learn:new',chat_id=self.other),bot.callback)]:
            await handler(update,self.ctx)
            update.effective_message.reply_text.assert_not_awaited()


class LearningStorageTests(unittest.TestCase):
    def test_schedule_backoff_revision_and_edit_reset(self):
        db=Store(':memory:')
        try:
            with patch('assistant_bot.storage.time.time',return_value=1000):
                card=db.create_study_card(101,'Question','Answer')
            now=1000
            for days in (1,3,7,14,30,30):
                previous=card
                card=db.review_study_card(101,card['id'],card['revision'],True,now=now)
                self.assertEqual(card['due_at'],now+days*86400)
                self.assertIsNone(db.review_study_card(101,card['id'],previous['revision'],True,now=now))
                self.assertIsNone(db.review_study_card(202,card['id'],card['revision'],True,now=now))
                self.assertIsNone(db.review_study_card(101,card['id'],card['revision'],True,now=now))
                now=card['due_at']
            hard=db.review_study_card(101,card['id'],card['revision'],False,now=now)
            self.assertEqual(hard['due_at'],now+600)
            self.assertEqual(hard['stage'],0)
            self.assertIsNone(db.edit_study_card(101,card['id'],card['revision'],'Q','A'))
            edited=db.edit_study_card(101,card['id'],hard['revision'],'Q','A')
            self.assertEqual(edited['stage'],0)
            self.assertIsNone(edited['reviewed_at'])
        finally:
            db.close()

    def test_additive_migration_persistence_and_owner_export(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'bot.db'
            db=Store(path)
            task=db.add_tasks(101,['Task'])[0]
            card=db.create_study_card(101,'PRIVATE_QUESTION','PRIVATE_ANSWER')
            db.save_checkpoint(101,task['id'],'PRIVATE_PROGRESS','PRIVATE_STEP')
            db.close()
            db=Store(path)
            try:
                self.assertEqual(db.get_study_card(101,card['id'])['answer'],'PRIVATE_ANSWER')
                self.assertEqual(db.recent_checkpoint(101)['next_step'],'PRIVATE_STEP')
                exported=db.export_data(101)
                self.assertEqual(len(exported['task_checkpoints']),1)
                self.assertEqual(len(exported['study_cards']),1)
                self.assertNotIn('PRIVATE',str(db.export_data(202)))
                self.assertEqual(db._conn.execute('PRAGMA foreign_key_check').fetchall(),[])
            finally:
                db.close()


if __name__=='__main__':
    unittest.main()
