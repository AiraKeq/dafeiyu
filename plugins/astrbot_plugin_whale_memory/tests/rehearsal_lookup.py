"""Native fake-message verification of 25-second pacing and grounded lookup."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from rehearsal_runtime import RuntimeRehearsal, LLMResponse, SCOPE_A
from lookup_policy import fallback_text


class LookupRehearsal(RuntimeRehearsal):
    # Reuse fixtures only; avoid inheriting the legacy test inventory twice.
    def _plan(self, query='尊嘟假嘟 含义'):
        return SimpleNamespace(completion_text=json.dumps({'need_lookup':True,'kind':'text',
            'confidence':0.5,'public':True,'queries':[query]},ensure_ascii=False))

    async def test_25_second_boundary_and_alias_still_force(self):
        self.config['proactive_gap_seconds']=25
        self.assertEqual(25,self.plugin._reply_gap())
        self.plugin.replies.external_reply(SCOPE_A)
        self.clock[0]+=24.999
        ordinary=self.event('普通消息等待25秒','q25')
        await self.plugin.observe(ordinary)
        self.assertTrue(ordinary.is_stopped())
        direct=self.event('鲸鲸，回应一下','alias25')
        await self.plugin.observe(direct)
        self.assertFalse(direct.is_stopped())
        self.assertTrue(direct.get_extra('whale_reply_token'))
        self.plugin.replies.finish(SCOPE_A,direct.get_extra('whale_reply_token'),sent=True)
        self.clock[0]+=25
        ordinary2=self.event('普通消息到期','q25-ready')
        await self.plugin.observe(ordinary2)
        self.assertFalse(ordinary2.is_stopped())

    async def test_screenshot_normal_dialogue_never_calls_router_or_search(self):
        self.config['web_lookup_enabled']=True
        self.plugin.lookup.enabled=True
        for serial,text in enumerate(('？','你干嘛凶我','这不是梗')):
            with self.subTest(text=text), patch.object(self.context,'llm_generate',new=AsyncMock(side_effect=RuntimeError('must not call classifier'))) as router, \
                 patch.object(self.plugin.lookup,'search',new=AsyncMock(side_effect=RuntimeError('must not search'))) as search:
                event=self.event(text,'normal-dialogue-'+str(serial),direct=True)
                req=await self.prepare_reply(event)
                router.assert_not_awaited();search.assert_not_awaited()
                self.assertFalse(event.get_extra('whale_lookup_fallback'))
                self.assertIn('正常交流',req.system_prompt)
                self.plugin.replies.finish(SCOPE_A,event.get_extra('whale_reply_token'),sent=True)

    async def test_low_confidence_ordinary_chat_and_classifier_error_do_not_force_template(self):
        self.config['web_lookup_enabled']=True
        raw=json.dumps({'need_lookup':False,'kind':'text','confidence':0.4,'public':False,'queries':[]})
        with patch.object(self.context,'llm_generate',new=AsyncMock(return_value=SimpleNamespace(completion_text=raw))), \
             patch.object(self.plugin.lookup,'search',new=AsyncMock()) as search:
            event=self.event('今天工作好累','private-normal')
            req=await self.prepare_reply(event)
            self.assertFalse(event.get_extra('whale_lookup_fallback'));search.assert_not_awaited()
        self.plugin.replies.finish(SCOPE_A,event.get_extra('whale_reply_token'),sent=True)
        with patch.object(self.context,'llm_generate',new=AsyncMock(side_effect=RuntimeError('synthetic error'))), \
             patch.object(self.plugin.lookup,'search',new=AsyncMock()) as search:
            event=self.event('今天一起随便聊聊','ordinary-error',direct=True)
            await self.prepare_reply(event)
            self.assertFalse(event.get_extra('whale_lookup_fallback'));search.assert_not_awaited()

    async def test_lookup_evidence_is_injected_without_sending_sources(self):
        self.config['web_lookup_enabled']=True
        self.plugin.lookup.enabled=True
        evidence={'state':'ready','results':[{'title':'词条说明','url':'https://example.org/meme',
                                           'snippet':'公开资料解释尊嘟假嘟是“真的假的”的谐音表达。'}]}
        with patch.object(self.context,'llm_generate',new=AsyncMock(return_value=self._plan())), \
             patch.object(self.plugin.lookup,'search',new=AsyncMock(return_value=evidence)) as search:
            event=self.event('尊嘟假嘟什么意思','meaning')
            req=await self.prepare_reply(event)
            self.assertIn('不可信第三方搜索摘要',req.system_prompt)
            self.assertIn('https://example.org/meme',req.system_prompt)
            self.assertEqual(1,search.await_count)
            self.assertEqual(0,event.send_attempts)
            self.assertFalse(event.get_extra('whale_lookup_fallback'))

    async def test_no_evidence_replaces_model_and_decorated_result(self):
        self.config['web_lookup_enabled']=True
        self.config['delivery_guard_enabled']=True
        self.plugin.lookup.enabled=True
        with patch.object(self.context,'llm_generate',new=AsyncMock(return_value=self._plan())), \
             patch.object(self.plugin.lookup,'search',new=AsyncMock(return_value={'state':'no_evidence','results':[]})):
            event=self.event('尊嘟假嘟什么意思','no-evidence')
            await self.prepare_reply(event)
            response=LLMResponse(role='assistant',completion_text='这个说法源于完全虚构的朝代。')
            await self.plugin.remember_reply(event,response)
            self.assertEqual(fallback_text(),response.completion_text)
            event.set_result(event.plain_result('另一层装饰又编造了出处。'))
            sent=[]
            async def collector(chain,*args,**kwargs):sent.append(chain.get_plain_text())
            await self.native_segmented_delivery(event,collector)
            self.assertEqual(''.join(sent),fallback_text())
            self.assertLessEqual(len(sent),3)

    async def test_private_query_is_never_sent_to_search(self):
        self.config['web_lookup_enabled']=True
        self.plugin.lookup.enabled=True
        with patch.object(self.context,'llm_generate',new=AsyncMock(return_value=self._plan('手机号13800138000'))), \
             patch.object(self.plugin.lookup,'search',new=AsyncMock()) as search:
            event=self.event('这是什么意思','private')
            await self.prepare_reply(event)
            search.assert_not_awaited()
            self.assertEqual(fallback_text(),event.get_extra('whale_lookup_fallback'))

    async def test_router_failure_is_explicit_uncertainty(self):
        self.config['web_lookup_enabled']=True
        with patch.object(self.context,'llm_generate',new=AsyncMock(side_effect=RuntimeError('synthetic router failure'))), \
             patch.object(self.plugin.lookup,'search',new=AsyncMock()) as search:
            event=self.event('陌生词是什么意思','router-error')
            await self.prepare_reply(event)
            search.assert_not_awaited()
            self.assertEqual(fallback_text(),event.get_extra('whale_lookup_fallback'))


if __name__=='__main__':
    names=[name for name in LookupRehearsal.__dict__ if name.startswith('test_')]
    suite=unittest.TestSuite(LookupRehearsal(name) for name in names)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
