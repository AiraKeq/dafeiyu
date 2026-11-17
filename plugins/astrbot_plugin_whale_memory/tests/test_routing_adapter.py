import asyncio
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from routing_adapter import classify

class RoutingAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_explicit_controls_reach_sdk_without_global_mutation(self):
        create=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))]))
        provider=SimpleNamespace(client=SimpleNamespace(base_url=SimpleNamespace(host='api.deepseek.com'),
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
            _prepare_chat_payload=AsyncMock(return_value=({'model':'deepseek-flash','messages':[{'role':'user','content':'data'}]},[])),
            provider_config={'custom_extra_body':{'thinking':{'type':'enabled'}}})
        original=copy.deepcopy(provider.provider_config)
        context=SimpleNamespace(get_provider_by_id=lambda _:provider)
        result=await classify(context,'p','公开词是什么意思',[],'JSON only')
        self.assertEqual(result,'{"ok":true}')
        args=create.await_args.kwargs
        self.assertEqual(args['max_tokens'],512)
        self.assertEqual(args['response_format'],{'type':'json_object'})
        self.assertEqual(args['extra_body'],{'thinking':{'type':'disabled'}})
        self.assertEqual(provider.provider_config,original)

    async def test_assembler_receives_only_current_images_and_no_history(self):
        create=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))]))
        prepare=AsyncMock(return_value=({'model':'deepseek-flash','messages':[]},[]))
        provider=SimpleNamespace(_prepare_chat_payload=prepare,
            client=SimpleNamespace(base_url=SimpleNamespace(host='api.deepseek.com'),chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
        await classify(SimpleNamespace(get_provider_by_id=lambda _:provider),'p','公开图片',['/current/test.png'],'route')
        self.assertEqual(prepare.await_args.kwargs['image_urls'],['/current/test.png'])
        self.assertEqual(prepare.await_args.kwargs['contexts'],[])

    async def test_fake_public_context_preserves_explicit_controls(self):
        generate=AsyncMock(return_value=SimpleNamespace(completion_text='{}'))
        await classify(SimpleNamespace(llm_generate=generate),'p','word',[],'route')
        self.assertEqual(generate.await_args.kwargs['thinking'],{'type':'disabled'})
        self.assertEqual(generate.await_args.kwargs['max_tokens'],512)

if __name__=='__main__':unittest.main()
