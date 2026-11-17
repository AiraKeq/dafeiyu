import importlib.util
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch
import sys
sys.dont_write_bytecode=True
spec=importlib.util.spec_from_file_location('standalone_vision',Path(__file__).resolve().parents[1]/'vision_inbox.py')
v=importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)

class VisionInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.inbox=v.VisionInbox(self.root/'cache')
        self.image=self.root/'source.png'
        self.image.write_bytes(b'\x89PNG\r\n\x1a\n'+b'fixture')
    def tearDown(self):
        self.tmp.cleanup()
    def test_copy_is_owned_and_content_exact(self):
        path=Path(self.inbox.stash(self.image))
        self.assertEqual(path.parent,self.inbox.path)
        self.assertEqual(path.read_bytes(),self.image.read_bytes())
        self.assertEqual(self.inbox.validate(path),str(path))
    def test_same_content_is_deduplicated(self):
        first=self.inbox.stash(self.image)
        self.assertEqual(first,self.inbox.stash(self.image))
        self.assertEqual(len(list(self.inbox.path.iterdir())),1)
    def test_concurrent_same_image_is_safe(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            paths=list(pool.map(lambda _:self.inbox.stash(self.image),range(20)))
        self.assertEqual(len(set(paths)),1)
    def test_bad_content_is_not_an_image(self):
        self.image.write_text('not image',encoding='utf-8')
        with self.assertRaises(ValueError): self.inbox.stash(self.image)
    def test_empty_image_is_rejected(self):
        self.image.write_bytes(b'')
        with self.assertRaises(ValueError): self.inbox.stash(self.image)
    def test_oversize_is_rejected_without_copy(self):
        with patch.object(v,'MAX_IMAGE_BYTES',4):
            with self.assertRaises(ValueError): self.inbox.stash(self.image)
        self.assertEqual(list(self.inbox.path.iterdir()),[])
    def test_capacity_does_not_delete_old_images(self):
        path=Path(self.inbox.stash(self.image))
        self.image.write_bytes(b'\x89PNG\r\n\x1a\n'+b'different')
        with patch.object(v,'MAX_CACHE_BYTES',path.stat().st_size):
            with self.assertRaises(ValueError): self.inbox.stash(self.image)
        self.assertTrue(path.exists())
    def test_external_reference_cannot_be_validated(self):
        with self.assertRaises(ValueError): self.inbox.validate(self.image)
    def test_changed_owned_content_cannot_be_validated(self):
        path=Path(self.inbox.stash(self.image))
        path.write_bytes(b'\x89PNG\r\n\x1a\nchanged')
        with self.assertRaises(ValueError): self.inbox.validate(path)
    def test_four_supported_magics(self):
        cases=((b'\x89PNG\r\n\x1a\n','.png'),(b'\xff\xd8\xfffixture','.jpg'),
               (b'GIF89afixture','.gif'),(b'RIFF1234WEBPfixture','.webp'))
        for data,suffix in cases:
            with self.subTest(suffix=suffix): self.assertEqual(v.image_suffix(data),suffix)

if __name__=='__main__': unittest.main(verbosity=2)
