import tests  # noqa: F401  first, so HOME is a throwaway dir before anything reads it
import os
import tempfile
import unittest

from herdr_machine0 import paste

S, E = paste.START, paste.END


class FilterTest(unittest.TestCase):
    def test_passthrough_outside_paste(self):
        f = paste.PasteFilter(lambda b: b.upper())
        self.assertEqual(f.feed(b"hello"), b"hello")

    def test_transform_inside_paste(self):
        f = paste.PasteFilter(lambda b: b.upper())
        self.assertEqual(f.feed(b"a" + S + b"xy" + E + b"b"), b"a" + S + b"XY" + E + b"b")

    def test_markers_split_across_chunks(self):
        f = paste.PasteFilter(lambda b: b.upper())
        data = b"a" + S + b"hello world" + E + b"z"
        out = b"".join(f.feed(data[i:i + 3]) for i in range(0, len(data), 3))
        self.assertEqual(out, b"a" + S + b"HELLO WORLD" + E + b"z")

    def test_two_pastes(self):
        f = paste.PasteFilter(lambda b: b.upper())
        self.assertEqual(f.feed(S + b"a" + E + S + b"b" + E), S + b"A" + E + S + b"B" + E)


class RewriteTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.file = os.path.join(self.dir, "shot.png")
        with open(self.file, "wb") as f:
            f.write(b"png")
        self.uploads = []

    def upload(self, src, dest):
        self.uploads.append((src, dest))

    def test_allowed_file_is_uploaded_and_rewritten(self):
        out = paste.rewrite(("see %s." % self.file).encode(), [self.dir], "/home/u/paste", self.upload, 1000)
        self.assertEqual(len(self.uploads), 1)
        dest = self.uploads[0][1]
        self.assertTrue(dest.startswith("/home/u/paste/") and dest.endswith("-shot.png"))
        self.assertEqual(out, ("see %s." % dest).encode())

    def test_outside_allowed_dirs_untouched(self):
        out = paste.rewrite(self.file.encode(), ["/nonexistent"], "/d", self.upload, 1000)
        self.assertEqual(out, self.file.encode())
        self.assertEqual(self.uploads, [])

    def test_missing_and_large_files_untouched(self):
        text = ("%s %s" % (self.file + "x", self.file)).encode()
        out = paste.rewrite(text, [self.dir], "/d", self.upload, 1)
        self.assertEqual(out, text)

    def test_upload_failure_keeps_path(self):
        def fail(_s, _d):
            raise OSError("down")
        self.assertEqual(paste.rewrite(self.file.encode(), [self.dir], "/d", fail, 1000), self.file.encode())


if __name__ == "__main__":
    unittest.main()
