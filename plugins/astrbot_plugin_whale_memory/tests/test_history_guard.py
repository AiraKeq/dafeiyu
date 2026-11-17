import copy
import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('guard',Path(__file__).resolve().parents[1]/'history_guard.py')
guard=importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

class HistoryGuardTests(unittest.TestCase):
    def test_leading_assistant_is_removed(self):
        history=[{'role':'assistant','content':'old orphan'},{'role':'user','content':'hello'},{'role':'assistant','content':'answer'}]
        self.assertEqual([x['role'] for x in guard.normalize_history(history)],['user','assistant'])
    def test_consecutive_assistants_are_merged(self):
        history=[{'role':'user','content':'hi'},{'role':'assistant','content':'first'},{'role':'assistant','content':'second'}]
        self.assertEqual(guard.normalize_history(history)[-1]['content'],'first\nsecond')
    def test_consecutive_users_keep_both_messages(self):
        history=[{'role':'user','content':'one'},{'role':'user','content':'two'}]
        self.assertEqual(guard.normalize_history(history)[0]['content'],'one\ntwo')
    def test_user_images_are_kept(self):
        history=[{'role':'user','content':[{'type':'text','text':'look'},{'type':'image_url','image_url':{'url':'data:image/png;base64,fixture'}}]}]
        self.assertEqual(guard.normalize_history(history),history)
    def test_input_is_not_mutated(self):
        history=[{'role':'user','content':'hello'},{'role':'assistant','content':'one'},{'role':'assistant','content':'two'}]
        old=copy.deepcopy(history)
        guard.normalize_history(history)
        self.assertEqual(history,old)
    def test_assistant_images_are_not_sent_as_user_images(self):
        history=[{'role':'user','content':'hi'},{'role':'assistant','content':[{'type':'image_url','image_url':{'url':'fixture'}},{'type':'text','text':'answer'}]}]
        self.assertEqual(guard.normalize_history(history)[-1]['content'],[{'type':'text','text':'answer'}])
    def test_invalid_and_tool_rows_are_ignored(self):
        history=[None,{}, {'role':'tool','content':'x'},{'role':'assistant','content':'orphan'},{'role':'user','content':'ok'}]
        self.assertEqual(guard.normalize_history(history),[{'role':'user','content':'ok'}])
    def test_all_assistant_returns_empty(self):
        self.assertEqual(guard.normalize_history([{'role':'assistant','content':'orphan'}]),[])
    def test_mixed_string_and_parts_merge_losslessly(self):
        history=[{'role':'user','content':'one'},{'role':'user','content':[{'type':'text','text':'two'}]}]
        self.assertEqual(guard.normalize_history(history)[0]['content'],[{'type':'text','text':'one'},{'type':'text','text':'\n'},{'type':'text','text':'two'}])
    def test_idempotent(self):
        history=[{'role':'assistant','content':'bad'},{'role':'user','content':'u'},{'role':'assistant','content':'a'},{'role':'assistant','content':'b'}]
        once=guard.normalize_history(history)
        self.assertEqual(guard.normalize_history(once),once)

if __name__=='__main__': unittest.main(verbosity=2)
