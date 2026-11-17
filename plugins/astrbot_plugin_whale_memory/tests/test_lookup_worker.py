import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import lookup_worker

class LookupWorkerTests(unittest.TestCase):
    def _run(self, factory, queries):
        with tempfile.TemporaryDirectory() as directory:
            stdin=SimpleNamespace(buffer=io.BytesIO(json.dumps({'queries':queries},ensure_ascii=False).encode()))
            output=io.StringIO()
            with patch.object(sys,'argv',['lookup_worker','--deps-dir',directory]), \
                 patch.object(sys,'stdin',stdin),patch.object(sys,'stdout',output), \
                 patch.dict(sys.modules,{'ddgs':SimpleNamespace(DDGS=factory)}):
                lookup_worker.main()
            if directory in sys.path:sys.path.remove(directory)
            return json.loads(output.getvalue())

    def test_first_success_is_not_lost_to_a_redundant_query_failure(self):
        calls=[]
        class FakeSearch:
            def __init__(self,**kwargs):pass
            def text(self,query,**kwargs):
                calls.append(query)
                if len(calls)>1:raise RuntimeError('later backend unavailable')
                return [{'title':'公开词条','href':'https://example.org/meme','body':'这是有来源的公开摘要。'}]
        result=self._run(FakeSearch,['公开词条','无结果的第二个查询'])
        self.assertTrue(result['ok']);self.assertEqual(len(result['data']['results']),1)
        self.assertEqual(calls,['公开词条'])

    def test_failed_engine_does_not_discard_next_engine_success(self):
        calls=[]
        class FakeSearch:
            def __init__(self,**kwargs):pass
            def text(self,query,**kwargs):
                calls.append(kwargs['backend'])
                if kwargs['backend']=='yahoo':raise RuntimeError('synthetic transport failure')
                return [{'title':'词条','href':'https://example.org/meme','body':'有来源的摘要'}]
        result=self._run(FakeSearch,['公开词条'])
        self.assertTrue(result['ok']);self.assertEqual(result['data']['backend'],'wikipedia')
        self.assertEqual(calls,['yahoo','wikipedia'])

if __name__=='__main__':unittest.main()
