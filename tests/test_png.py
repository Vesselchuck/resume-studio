"""
Tests for build/_png.py's handling of an image with no pixels.

PNG cannot hold one: IHDR's width and height must be at least 1. The
writer used to emit an IHDR with a zero in it anyway, and Pillow could
not read the result back. It now refuses with a ValueError that says
why, instead of handing the preview bytes no browser decodes.
"""

import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

try:
    from PIL import Image
except ImportError:  # pragma: no cover — requirements.txt installs it
    Image = None

import _png  # noqa: E402


@unittest.skipIf(Image is None, "Pillow not installed")
class TestEmptyImage(unittest.TestCase):
    def test_an_image_with_no_pixels_is_refused(self):
        for size in ((0, 4), (4, 0), (0, 0)):
            for mode in ("RGB", "L", "RGBA"):
                with self.subTest(size=size, mode=mode):
                    with self.assertRaisesRegex(ValueError, "empty image"):
                        _png.encode(Image.new(mode, size))

    def test_one_pixel_still_encodes(self):
        img = Image.new("RGB", (1, 1), (10, 20, 30))
        back = Image.open(io.BytesIO(_png.encode(img)))
        back.load()
        self.assertEqual(back.size, (1, 1))
        self.assertEqual(back.convert("RGB").tobytes(), img.tobytes())


if __name__ == "__main__":
    unittest.main()
