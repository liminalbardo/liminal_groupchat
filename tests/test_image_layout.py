import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gchat import images, settings
from gchat.engine import Engine


class ImageLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.media = self.root / "media"
        self.media.mkdir()
        for key, value in (("DATA_DIR", str(self.root)), ("MEDIA_DIR", str(self.media))):
            patcher = patch.object(settings, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_ratios_match_served_thumbnails(self):
        for name, size in (("wide.png", (1600, 901)), ("tall.png", (451, 900)), ("small.gif", (90, 160))):
            Image.new("RGB", size).save(self.media / name)
            with Image.open(images.thumbnail(name)) as thumbnail:
                self.assertEqual(images.dimensions(name), thumbnail.size)

    def test_missing_and_invalid_images_have_no_dimensions(self):
        (self.media / "broken.png").write_text("invalid")
        for name in ("missing.png", "broken.png", "../outside.png"):
            self.assertIsNone(images.dimensions(name))

    def test_snapshot_and_live_message_include_dimensions(self):
        Image.new("RGB", (600, 300)).save(self.media / "wide.png")
        msg = {"id": "image", "kind": "image", "image": "wide.png", "status": "done"}
        eng = Engine.__new__(Engine)
        eng.chat = {"messages": [msg]}
        eng.running = False
        eng.typing = set()
        queue = asyncio.Queue()
        eng.listeners = {queue}
        with patch("gchat.engine.settings.public", return_value={}), patch("gchat.engine.store.list_chats", return_value=[]):
            snapshot = eng.snapshot()["chat"]["messages"][0]
        eng._emit_message(msg)
        live = queue.get_nowait()["message"]
        self.assertEqual((snapshot["image_width"], snapshot["image_height"]), (600, 300))
        self.assertEqual(live, snapshot)
        self.assertNotIn("image_width", msg)


if __name__ == "__main__":
    unittest.main()
