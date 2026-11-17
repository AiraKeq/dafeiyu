"""Network-disabled native delivery, pagination, polish and image-input rehearsal."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

if os.environ.get('WHALE_REHEARSAL')!='1':
    raise RuntimeError('Run only in the isolated rehearsal container.')
import rehearsal_runtime as rt
from astrbot.core.agent.message import Message, TextPart
from astrbot.api.message_components import Image


class EnhancementRehearsal(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def fixture(self, **values):
        test=rt.RuntimeRehearsal()
        await test.asyncSetUp()
        test.config.update(delivery_guard_enabled=True, reply_polish_enabled=False,
                           vision_enabled=False, segment_score_micro_random=False)
        test.config.update(values)
        try:
            yield test
        finally:
            await test.asyncTearDown()

    async def response(self,test,event,text):
        await test.prepare_reply(event)
        response=rt.LLMResponse(role='assistant',completion_text=text)
        context=SimpleNamespace(messages=[Message(role='assistant',content=[TextPart(text=text)])])
        await test.plugin.remember_reply(event,response)
        await test.plugin.enhance_response(event,context,response)
        event.set_result(rt.MessageEventResult().message(response.completion_text))
        return response,context

    async def send(self,test,event):
        output=[]
        async def collect(message,*args,**kwargs):
            output.append(message.get_plain_text())
        await test.native_segmented_delivery(event,collect)
        page=event.get_extra('whale_delivery_page')
        self.assertLessEqual(len(output),3)
        self.assertTrue(all(len(text)<=page['char_limit'] for text in output))
        self.assertEqual(''.join(output),page['display_text'])
        return output

    async def test_dynamic_short_and_long_limits(self):
        for length in (12,61,120,180):
            async with self.fixture() as test:
                text='春'*length
                event=test.event('请解释清楚',str(length),direct=True)
                response,context=await self.response(test,event,text)
                output=await self.send(test,event)
                self.assertEqual(''.join(output),text)
                self.assertEqual(response.completion_text,text)
                self.assertEqual(context.messages[-1].content[-1].text,text)
                self.assertLessEqual(len(output),3)

    async def test_pagination_history_and_owned_continue(self):
        async with self.fixture() as test:
            text=('春风拂过花海。'*100)[:600]
            event=test.event('请把内容完整说完','pages',direct=True)
            response,context=await self.response(test,event,text)
            self.assertLess(len(response.completion_text),len(text))
            self.assertEqual(context.messages[-1].content[-1].text,response.completion_text)
            first=event.get_extra('whale_delivery_page')
            self.assertTrue(first['continuation'])
            contents=[first['content']]
            await self.send(test,event)
            other=test.event('继续','wrong-owner',sender='someone-else')
            await test.plugin.observe(other)
            self.assertFalse(other.get_extra('whale_cached_page'))
            pages=await test.plugin._page_store()
            while pages.peek_metadata(rt.SCOPE_A,'fixture-user-a')['remaining']:
                continuation=test.event('鲸鲸，继续','continue-'+str(len(contents)),direct=True)
                await test.plugin.observe(continuation)
                self.assertTrue(continuation.get_extra('whale_cached_page'))
                results=[item async for item in test.plugin.deliver_cached_page(continuation)]
                self.assertEqual(len(results),1)
                self.assertTrue(continuation.call_llm)
                continuation.set_result(results[0])
                page=continuation.get_extra('whale_delivery_page')
                contents.append(page['content'])
                await self.send(test,continuation)
            self.assertEqual(''.join(contents),text)
            rows=[row for row in test.rows() if row['role']=='assistant']
            self.assertEqual(len(rows),len(contents))
            self.assertNotIn(text, [row['text'] for row in rows])
            self.assertEqual(test.context.model_calls,[])

    async def test_authorized_private_chat_is_bounded_and_pageable(self):
        async with self.fixture() as test:
            text='私聊分页内容。'*70
            event=test.event('详细说', 'private-pages',group='',sender=rt.ADMIN,direct=True)
            response,context=await self.response(test,event,text)
            self.assertTrue(event.get_extra('whale_reply_token'))
            self.assertLess(len(response.completion_text),len(text))
            await self.send(test,event)
            pages=await test.plugin._page_store()
            scope='aiocqhttp:FriendMessage:'+rt.ADMIN
            self.assertGreater(pages.peek_metadata(scope,rt.ADMIN)['remaining'],0)
            continuation=test.event('继续','private-continue',group='',sender=rt.ADMIN,direct=True)
            await test.plugin.observe(continuation)
            self.assertTrue(continuation.get_extra('whale_cached_page'))
            results=[item async for item in test.plugin.deliver_cached_page(continuation)]
            continuation.set_result(results[0])
            await self.send(test,continuation)
            stranger=test.event('详细说','private-stranger',group='',sender='not-admin',direct=True)
            await test.plugin.observe(stranger)
            self.assertFalse(stranger.get_extra('whale_reply_token'))

    async def test_trimmed_history_cannot_start_with_assistant(self):
        async with self.fixture() as test:
            history=[{'role':'user','content':'u'*20},{'role':'assistant','content':'old answer'},
                     {'role':'user','content':'new question'},{'role':'assistant','content':'new answer'}]
            result=test.plugin._trim_contexts(history,char_budget=110)
            self.assertTrue(not result or result[0]['role']=='user')
            roles=[item['role'] for item in result]
            self.assertFalse(any(a==b=='assistant' for a,b in zip(roles,roles[1:])))

    async def test_failed_page_does_not_advance(self):
        async with self.fixture() as test:
            event=test.event('详细说','failure',direct=True)
            await self.response(test,event,'花'*500)
            pages=await test.plugin._page_store()
            async def broken(message,*args,**kwargs):
                raise OSError('offline failed protocol call')
            await test.native_segmented_delivery(event,broken)
            if test.plugin._cleanup_tasks:
                await asyncio.gather(*list(test.plugin._cleanup_tasks))
            meta=pages.peek_metadata(rt.SCOPE_A,'fixture-user-a')
            self.assertEqual(meta['index'],0)
            self.assertFalse(meta['busy'])
            self.assertTrue(event.get_extra('whale_reply_blocked'))
            self.assertFalse(any(row['role']=='assistant' for row in test.rows()))

    async def test_night_prevents_page_progress(self):
        async with self.fixture() as test:
            test.clock[0]=rt.instant(23,minute=59,second=59)
            event=test.event('详细说','night-page',direct=True)
            await self.response(test,event,'风'*500)
            test.clock[0]=rt.instant(0,day=14)
            await test.decorating_hooks(event)
            if test.plugin._cleanup_tasks:
                await asyncio.gather(*list(test.plugin._cleanup_tasks))
            pages=await test.plugin._page_store()
            self.assertEqual(pages.peek_metadata(rt.SCOPE_A,'fixture-user-a')['index'],0)
            self.assertTrue(event.is_stopped())

    async def test_new_response_replaces_old_pages(self):
        async with self.fixture() as test:
            first=test.event('详细说','old-pages',direct=True)
            await self.response(test,first,'旧'*500)
            await self.send(test,first)
            pages=await test.plugin._page_store()
            self.assertGreater(pages.peek_metadata(rt.SCOPE_A,'fixture-user-a')['remaining'],0)
            new=test.event('问一个新问题','new-short',direct=True)
            await self.response(test,new,'新问题我听见啦。')
            await self.send(test,new)
            self.assertIsNone(pages.peek_metadata(rt.SCOPE_A,'fixture-user-a'))

    async def test_polish_preserves_native_response_and_history(self):
        async with self.fixture(reply_polish_enabled=True) as test:
            original='很高兴为您服务。辛苦了，慢慢来。'
            proposed='辛苦啦，慢慢来呀。'
            test.context.get_current_chat_provider_id=AsyncMock(return_value='fixture-model')
            test.context.llm_generate=AsyncMock(return_value=rt.LLMResponse(role='assistant',completion_text=proposed))
            event=test.event('今天有点累','polish',direct=True)
            response,context=await self.response(test,event,original)
            self.assertEqual(response.completion_text,proposed)
            self.assertEqual(context.messages[-1].content[-1].text,proposed)
            self.assertEqual(test.context.llm_generate.await_count,1)
            await self.send(test,event)

    async def test_polish_bad_candidate_and_technical_skip(self):
        async with self.fixture(reply_polish_enabled=True) as test:
            test.context.get_current_chat_provider_id=AsyncMock(return_value='fixture-model')
            test.context.llm_generate=AsyncMock(return_value=rt.LLMResponse(role='assistant',completion_text='我叫夏目安安。'))
            original='很高兴为您服务。辛苦了，慢慢来。'
            event=test.event('今天有点累','bad-polish',direct=True)
            response,context=await self.response(test,event,original)
            self.assertEqual(response.completion_text,original)
            technical=test.event('请解释SQLite分页','skip-polish',direct=True)
            await self.response(test,technical,'数据库事务需要保持一致。')
            self.assertEqual(test.context.llm_generate.await_count,1)

    async def test_picture_refs_survive_cooldown_replay(self):
        async with self.fixture(vision_enabled=True) as test:
            fixture=Path(test.temporary.name)/'input.png'
            fixture.write_bytes(b'\x89PNG\r\n\x1a\n'+b'fixture only')
            seed=test.event('先打个招呼','seed',direct=True)
            await self.response(test,seed,'你好呀。')
            await self.send(test,seed)
            image=Image.fromFileSystem(fixture)
            event=test.event('看看图片','image',components=[image])
            await test.plugin.observe(event)
            self.assertTrue(event.is_stopped())
            refs=event.get_extra('whale_image_refs')
            self.assertEqual(len(refs),1)
            self.assertNotEqual(Path(refs[0]),fixture)
            self.assertTrue(fixture.resolve().is_relative_to(Path(test.temporary.name).resolve()))
            fixture.unlink()
            test.clock[0]+=10
            await test.plugin.replies.pump()
            # Owned-image verification crosses a to_thread boundary; wait for the
            # already-started dispatch task instead of assuming a synchronous put.
            if test.plugin.replies._dispatch_tasks:
                await asyncio.gather(*list(test.plugin.replies._dispatch_tasks.values()))
            replay=test.context.event_queue.get_nowait()
            await test.plugin.observe(replay)
            request=rt.ProviderRequest(prompt=replay.message_str,system_prompt='',contexts=[])
            await test.plugin.inject(replay,request)
            self.assertEqual(request.image_urls,refs)
            self.assertTrue(Path(refs[0]).is_file())

    async def test_image_only_message_gets_model_request(self):
        async with self.fixture(vision_enabled=True) as test:
            fixture=Path(test.temporary.name)/'only.png'
            fixture.write_bytes(b'\x89PNG\r\n\x1a\n'+b'fixture only')
            event=test.event('', 'image-only',components=[Image.fromFileSystem(fixture)])
            await test.plugin.observe(event)
            self.assertTrue(event.get_extra('whale_reply_token'))
            request=rt.ProviderRequest(prompt=event.message_str,system_prompt='',contexts=[])
            await test.plugin.inject(event,request)
            self.assertEqual(len(request.image_urls),1)
            self.assertIn('图片',request.prompt)

    async def test_unsupported_or_too_many_images_fail_closed(self):
        async with self.fixture(vision_enabled=True) as test:
            fixture=Path(test.temporary.name)/'bad.txt'
            fixture.write_text('not an image',encoding='utf-8')
            event=test.event('看图','bad-image',components=[Image.fromFileSystem(fixture)])
            await test.plugin.observe(event)
            self.assertTrue(event.is_stopped())
            self.assertFalse(event.get_extra('whale_reply_token'))
            many=test.event('看图','four-images',components=[Image.fromFileSystem(fixture)]*4)
            await test.plugin.observe(many)
            self.assertTrue(many.is_stopped())


if __name__=='__main__':
    unittest.main(verbosity=2)
