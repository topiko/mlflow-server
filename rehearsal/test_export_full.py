"""Exercise full-export resume, synchronization, and integrity checks locally."""

import hashlib
import io
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from common import STATE, prepare_workspace
from export_full import (
    copy_pending,
    inventory,
    main,
    open_state,
    remove_deleted,
    verify_files,
)


class FakeS3:
    def __init__(self):
        self.objects = {"one/model.txt": b"model-one", "two/model.txt": b"model-two"}
        self.reads = 0

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return self

    def paginate(self, **_):
        yield {"Contents": [
            {"Key": key, "Size": len(value), "ETag": hashlib.sha256(value).hexdigest()}
            for key, value in self.objects.items()
        ]}

    def get_object(self, **request):
        value = self.objects[request["Key"]]
        if request["IfMatch"] != hashlib.sha256(value).hexdigest():
            raise ValueError("Source changed")
        self.reads += 1
        return {"ContentLength": len(value), "Body": io.BytesIO(value)}


class FullExportTests(unittest.TestCase):
    def setUp(self):
        prepare_workspace()
        self.directory = TemporaryDirectory(dir=STATE)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.destination = self.root / "artifacts"
        self.destination.mkdir()
        self.state = open_state(self.root / "export.sqlite3", "http://source", "bucket")
        self.addCleanup(self.state.close)
        self.s3 = FakeS3()

    def sync(self):
        objects = inventory(self.s3, "bucket")
        copied = copy_pending(
            self.s3, "bucket", self.destination, self.state, objects, 2
        )
        verify_files(self.destination, self.state, objects, 2)
        return copied

    def test_resume_and_update(self):
        self.assertEqual(self.sync(), 2)
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.s3.reads, 2)
        self.s3.objects["one/model.txt"] = b"model-new"
        self.assertEqual(self.sync(), 1)
        self.assertEqual(
            (self.destination / "one/model.txt").read_bytes(), b"model-new"
        )
        self.assertEqual(self.s3.reads, 3)

    def test_detect_corruption_with_unchanged_size_and_timestamp(self):
        self.sync()
        path = self.destination / "one/model.txt"
        original = path.stat()
        path.write_bytes(b"X" * original.st_size)
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
            verify_files(self.destination, self.state, inventory(self.s3, "bucket"), 2)

    def test_source_change_during_copy(self):
        objects = inventory(self.s3, "bucket")
        self.s3.objects["one/model.txt"] = b"new-content"
        with self.assertRaisesRegex(ValueError, "Source changed"):
            copy_pending(self.s3, "bucket", self.destination, self.state, objects, 2)
        self.assertFalse((self.destination / "one/model.txt").exists())

    def test_remove_only_exported_keys(self):
        self.sync()
        unrelated = self.destination / "unrelated.txt"
        unrelated.write_text("keep")
        del self.s3.objects["two/model.txt"]
        removed = remove_deleted(
            self.destination, self.state, inventory(self.s3, "bucket")
        )
        self.assertEqual(removed, 1)
        self.assertFalse((self.destination / "two/model.txt").exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(self.sync(), 0)

    def test_reject_unsafe_key(self):
        self.s3.objects["../outside"] = b"bad"
        with self.assertRaisesRegex(ValueError, "safe filesystem path"):
            inventory(self.s3, "bucket")

    def test_refuse_export_after_cutover(self):
        (self.root / "cutover-complete.json").write_text("{}")
        command = [
            "export_full.py", "--destination", str(self.destination),
            "--state", str(self.root),
        ]
        with patch("sys.argv", command):
            with self.assertRaisesRegex(SystemExit, "Cutover is complete"):
                main()


if __name__ == "__main__":
    unittest.main()
