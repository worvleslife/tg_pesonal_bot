"""Project memory privacy, evidence, revisions, and workflow integration."""
import tempfile
from pathlib import Path
from html import unescape
import unittest

import test_work_bot as fixtures
from assistant_bot import bot
from assistant_bot.memory import case_body
from assistant_bot.storage import Store


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.WorkBotTests.asyncSetUp
    asyncTearDown=fixtures.WorkBotTests.asyncTearDown
    update=fixtures.WorkBotTests.update
    texts=staticmethod(fixtures.WorkBotTests.texts)
    buttons=staticmethod(fixtures.WorkBotTests.buttons)
    callbacks=fixtures.WorkBotTests.callbacks
    callback_with_prefix=fixtures.WorkBotTests.callback_with_prefix
    click=fixtures.WorkBotTests.click
    say=fixtures.WorkBotTests.say

    def project(self,owner=None,title='Выставка'):
        return self.db.create_project(owner or self.owner,title,'Подготовить экспозицию')

    def note(self,project=None,kind='observation',body='Камера пропадала с регистратора'):
        p=project or self.project()
        return self.db.add_memory_entry(p['owner_id'],p['id'],kind,'Камера',body)

    async def confirm(self,view,**kwargs):
        return await self.click(self.callback_with_prefix(view,'mem:confirm:'),**kwargs)

    async def test_project_creation_confirm_cancel_and_private_summary(self):
        await self.click('mem:new')
        await self.say('Мой проект')
        view=await self.say('Моя цель')
        self.assertEqual(self.db.project_count(self.owner),0)
        await self.confirm(view,user=self.other,ctx=self.other_ctx)
        await self.confirm(view)
        await self.confirm(view)
        self.assertEqual(self.db.project_count(self.owner),1)
        self.assertEqual(self.db.project_count(self.other),0)
        await self.click('mem:new')
        await self.say('Отмена')
        preview=await self.say('Цель')
        await bot.command(self.update('/cancel'),self.ctx)
        await self.confirm(preview)
        self.assertEqual(self.db.project_count(self.owner),1)

    async def test_typed_entry_source_link_and_replay(self):
        p=self.project()
        original=self.db.add_item(self.owner,'Инструкция камеры','Гайд','Учёба',[])
        await self.click(f'mem:attach:{p["id"]}:library:{original["id"]}')
        token=self.ctx.user_data['mem_choice']
        view=await self.click(f'mem:kind:{token}:instruction')
        self.assertEqual(self.db.memory_count(self.owner,p['id']),0)
        await self.confirm(view)
        e=self.db.memory_entries(self.owner,p['id'])[0]
        self.assertEqual(e['kind'],'instruction')
        card=await self.click(f'mem:entry:{e["id"]}:0')
        self.assertIn(f'item:{original["id"]}',self.callbacks(card))
        self.assertIn('Из инструкции',self.texts(card))
        await self.confirm(view)
        self.assertEqual(self.db.memory_count(self.owner,p['id']),1)
        duplicate=self.db.add_memory_entry(self.owner,p['id'],'observation','Другой','Текст',source_kind='library',source_id=original['id'])
        self.assertEqual(duplicate['id'],e['id'])

    async def test_manual_note_and_history_preserve_prior_body(self):
        p=self.project()
        view=await self.click(f'mem:add:{p["id"]}')
        choice=next(c for c in self.callbacks(view) if c.endswith(':decision'))
        await self.click(choice)
        await self.say('Срок')
        await self.confirm(await self.say('Решили 5 октября, потому что доступен зал'))
        e=self.db.memory_entries(self.owner,p['id'])[0]
        await self.click(f'mem:edit:{e["id"]}')
        preview=await self.say('Решили 7 октября: зал перенёс бронь')
        self.assertIn('Было',self.texts(preview))
        self.assertEqual(self.db.get_memory_entry(self.owner,e['id'])['revision'],1)
        await self.confirm(preview)
        self.assertEqual(self.db.get_memory_entry(self.owner,e['id'])['revision'],2)
        old=await self.click(f'mem:version:{e["id"]}:1:0')
        self.assertIn('5 октября',self.texts(old))
        self.assertNotIn('7 октября',self.texts(old))
        self.assertIsNone(self.db.revise_memory(self.owner,e['id'],1,'Старая правка'))

    async def test_case_walkthrough_result_and_causality_label(self):
        p=self.project()
        await self.click(f'mem:case:{p["id"]}')
        for text in ['Камера','Пропадает вечером','Проверили питание','Заменили кабель']:
            await self.say(text)
        preview=await self.say('Пока работает; причина неизвестна')
        await self.confirm(preview)
        e=self.db.memory_entries(self.owner,p['id'])[0]
        self.assertEqual(e['payload']['checks'],'Проверили питание')
        card=await self.click(f'mem:entry:{e["id"]}:0')
        self.assertIn('не доказывает',self.texts(card))
        await self.click(f'mem:outcome:{e["id"]}')
        await self.confirm(await self.say('Через сутки сбой вернулся'))
        changed=self.db.get_memory_entry(self.owner,e['id'])
        self.assertIn('сбой вернулся',changed['body'])
        self.assertEqual(changed['payload']['checks'],'Проверили питание')
        self.assertIn('Пока работает',self.db.memory_version(self.owner,e['id'],1)['body'])

    async def test_unified_search_provenance_all_sources_and_privacy(self):
        self.db.add_item(self.owner,'Инструкция камеры','Гайд','Работа',[])
        section=self.db.create_work_section(self.owner,'Устройства')
        self.db.add_work_material(self.owner,section['id'],title='Питание камеры',text='Проверить питание')
        e=self.note()
        other=self.project(self.other,'PRIVATE_PROJECT')
        self.db.add_memory_entry(self.other,other['id'],'observation','Камера','PRIVATE_SECRET камера')
        update=self.update('/memory Где камера пропадала?')
        await bot.command(update,self.ctx)
        text=self.texts(update)
        self.assertIn('Гайд',text)
        self.assertIn('Питание',text)
        self.assertNotIn('PRIVATE',text)
        self.assertIn('Совпадения по словам',text)
        self.assertIn(f'mem:entry:{e["id"]}:0',self.callbacks(update))
        results=self.db.memory_search(self.owner,'камера')
        self.assertEqual({r['source'] for r in results},{'library','work','memory'})

    async def test_foreign_objects_callbacks_cannot_read_mutate_download_or_link(self):
        p=self.project()
        e=self.note(p)
        task=self.db.add_tasks(self.owner,['PRIVATE_TASK'])[0]
        original=self.db.add_item(self.owner,'PRIVATE_SOURCE','Private','X',[])
        own=self.project(self.other,'Мой')
        callbacks=[f'mem:project:{p["id"]}',f'mem:entries:{p["id"]}:0',f'mem:entry:{e["id"]}:0',
                   f'mem:version:{e["id"]}:1:0',f'mem:edit:{e["id"]}',f'mem:outcome:{e["id"]}',
                   f'mem:download:{p["id"]}',f'mem:brief:{p["id"]}',f'mem:remind:{e["id"]}',
                   f'mem:deleteentry:{e["id"]}',f'mem:deleteproject:{p["id"]}',
                   f'mem:tasklink:{own["id"]}:{task["id"]}',f'mem:attach:{own["id"]}:library:{original["id"]}']
        for callback in callbacks:
            view=await self.click(callback,user=self.other,ctx=self.other_ctx)
            self.assertNotIn('PRIVATE',self.texts(view))
            view.effective_message.reply_document.assert_not_awaited()
        self.assertEqual(self.db.get_memory_entry(self.owner,e['id']),e)
        self.assertEqual(self.db.project_tasks(self.other,own['id']),[])
        self.assertIsNone(self.db.add_memory_entry(self.other,p['id'],'idea','x','y'))
        self.assertIsNone(self.db.add_memory_entry(self.other,own['id'],'idea','x','y',source_kind='library',source_id=original['id']))

    async def test_case_task_link_and_completion_asks_for_outcome(self):
        p=self.project()
        e=self.db.add_memory_entry(self.owner,p['id'],'case','Камера',case_body({'object':'Камера'}),payload={'object':'Камера'})
        await self.click(f'plan:from:memory:{e["id"]}')
        view=await self.say('Проверить кабель')
        await self.click(self.callback_with_prefix(view,'plan:confirm:'))
        task=self.db.list_tasks(self.owner)[0]
        self.assertEqual(self.db.project_tasks(self.owner,p['id'])[0]['id'],task['id'])
        card=await self.click(f'plan:task:{task["id"]}')
        self.assertIn(f'mem:entry:{e["id"]}:0',self.callbacks(card))
        done=await self.click(f'plan:done:{task["id"]}')
        self.assertIn('Что в итоге помогло',self.texts(done))
        repeat=await self.click(f'plan:done:{task["id"]}')
        self.assertNotIn('Что в итоге помогло',self.texts(repeat))

    async def test_resume_phrase_and_twenty_minutes_do_not_save_notes(self):
        p=self.project()
        e=self.note(p,'decision','Выбрали зал, потому что он близко')
        view=await self.say('Верни меня в проект выставки')
        self.assertIn('Выбрали зал',self.texts(view))
        task=self.db.add_tasks(self.owner,['Короткое дело'])[0]
        self.db.edit_task(self.owner,task['id'],minutes=15)
        view=await self.say('У меня есть 20 минут')
        self.assertIn('Короткое дело',self.texts(view))
        self.assertEqual(self.db.count_items(self.owner),0)

    async def test_private_brief_and_delete_cascade_keep_original_and_tasks(self):
        p=self.project()
        original=self.db.add_item(self.owner,'Original','Original','X',[])
        e=self.db.add_memory_entry(self.owner,p['id'],'observation','Entry','OLD',source_kind='library',source_id=original['id'])
        self.db.revise_memory(self.owner,e['id'],1,'CURRENT')
        task=self.db.add_tasks(self.owner,['Keep task'])[0]
        self.db.link_project_task(self.owner,p['id'],task['id'])
        view=await self.click(f'mem:download:{p["id"]}')
        text=view.effective_message.reply_document.await_args.args[0].getvalue().decode('utf-8')
        self.assertIn('CURRENT',text)
        self.assertNotIn('OLD',text)
        delete=await self.click(f'mem:deleteproject:{p["id"]}')
        self.assertIsNotNone(self.db.get_project(self.owner,p['id']))
        await self.confirm(delete)
        self.assertIsNone(self.db.get_project(self.owner,p['id']))
        self.assertIsNone(self.db.memory_version(self.owner,e['id'],1))
        self.assertIsNotNone(self.db.get_item(self.owner,original['id']))
        self.assertIsNotNone(self.db.get_task(self.owner,task['id']))

    async def test_pagination_message_bounds_and_navigation_cancel(self):
        for i in range(10):
            self.project(title=f'Проект{i}')
        view=await self.click('mem:home:0')
        self.assertIn('mem:home:8',self.callbacks(view))
        p=self.project()
        for i in range(10):
            self.note(p,body='😀<&>'*800)
        view=await self.click(f'mem:entries:{p["id"]}:0')
        self.assertIn(f'mem:entries:{p["id"]}:8',self.callbacks(view))
        e=self.db.memory_entries(self.owner,p['id'])[0]
        view=await self.click(f'mem:entry:{e["id"]}:0')
        self.assertLess(len(unescape(self.texts(view)).encode('utf-16-le'))//2,4096)
        await self.click(f'mem:edit:{e["id"]}')
        preview=await self.say('Не сохранять')
        await self.say('🧭 Сегодня')
        await self.confirm(preview)
        self.assertEqual(self.db.get_memory_entry(self.owner,e['id'])['revision'],1)

    async def test_invalid_buttons_and_group_guard(self):
        for data in ('mem:project:-1','mem:entry:999999999999999999999:0','mem:confirm:é','mem:kind:bad:decision','mem:foo'):
            await self.click(data)
        for update,handler in [(self.update('/projects',chat_type='group'),bot.command),
                               (self.update(callback='mem:home:0',chat_id=self.other),bot.callback)]:
            await handler(update,self.ctx)
            update.effective_message.reply_text.assert_not_awaited()


class MemoryPersistenceTests(unittest.TestCase):
    def test_migration_keeps_existing_data_and_export_is_private(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'bot.sqlite'
            db=Store(path)
            item=db.add_item(101,'Original','Original','X',[])
            db._conn.executescript('DROP TABLE memory_task_links; DROP TABLE memory_versions; DROP TABLE memory_entries; DROP TABLE memory_projects;')
            db.close()
            db=Store(path)
            p=db.create_project(101,'Project','Goal')
            e=db.add_memory_entry(101,p['id'],'decision','Decision','OLD')
            db.revise_memory(101,e['id'],1,'NEW')
            db.close()
            db=Store(path)
            try:
                self.assertEqual(db.get_item(101,item['id'])['text'],'Original')
                self.assertEqual(db.memory_version(101,e['id'],1)['body'],'OLD')
                self.assertEqual(db.get_memory_entry(101,e['id'])['body'],'NEW')
                self.assertEqual(db.export_data(202)['memory_versions'],[])
                self.assertEqual(len(db.export_data(101)['memory_versions']),2)
                self.assertEqual(db._conn.execute('PRAGMA foreign_key_check').fetchall(),[])
            finally:
                db.close()


if __name__=='__main__':
    unittest.main()
