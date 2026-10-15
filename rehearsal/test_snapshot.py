"""Check rejection of damaged or unsafe artifact streams without production access."""

import hashlib
import io
import json
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from common import STATE, prepare_workspace
from snapshot import receive_artifacts


class ArtifactStreamTests(unittest.TestCase):
    def setUp(self):
        prepare_workspace()
        self.directory = TemporaryDirectory(dir=STATE)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.patch = patch("snapshot.ARTIFACTS", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.key = "1/run/artifacts/model.txt"
        self.payload = b"complete artifact"
        self.objects = {self.key: {"size": len(self.payload)}}

    def stream(self, *, key=None, content=None, manifest=True, symlink=False):
        key = key or self.key
        content = self.payload if content is None else content
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            info = tarfile.TarInfo(key)
            if symlink:
                info.type = tarfile.SYMTYPE
                info.linkname = "../../outside"
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            if manifest:
                payload = json.dumps([{
                    "key": key,
                    "sha256": hashlib.sha256(self.payload).hexdigest(),
                }]).encode()
                info = tarfile.TarInfo("__snapshot_manifest__.json")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        stream.seek(0)
        return stream

    def test_complete_artifact(self):
        manifest = receive_artifacts(self.stream(), self.objects)
        self.assertEqual(len(manifest), 1)
        self.assertEqual((self.root / self.key).read_bytes(), self.payload)

    def test_s3_exporter_round_trip(self):
        # No boto3 installation or network credentials are needed for this test.
        with patch.dict("sys.modules", {"boto3": MagicMock()}):
            from s3_reader import stream_objects

        s3 = MagicMock()
        s3.get_object.return_value = {
            "ContentLength": len(self.payload),
            "Body": io.BytesIO(self.payload),
        }
        objects = [{"key": self.key, "size": len(self.payload), "etag": '"etag"'}]
        output = io.TextIOWrapper(io.BytesIO())
        with patch("sys.stdout", output):
            stream_objects(s3, "bucket", objects)
        stream = io.BytesIO(output.buffer.getvalue())
        manifest = receive_artifacts(stream, self.objects)
        self.assertEqual((self.root / self.key).read_bytes(), self.payload)
        self.assertEqual(
            manifest[0]["sha256"], hashlib.sha256(self.payload).hexdigest()
        )
        s3.get_object.assert_called_once_with(
            Bucket="bucket", Key=self.key, IfMatch='"etag"'
        )

    def test_corrupted_bytes(self):
        stream = self.stream(content=b"X" * len(self.payload))
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            receive_artifacts(stream, self.objects)

    def test_missing_manifest(self):
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            receive_artifacts(self.stream(manifest=False), self.objects)

    def test_path_traversal(self):
        key = "../outside"
        with self.assertRaisesRegex(ValueError, "Unexpected artifact path"):
            receive_artifacts(self.stream(key=key), {key: self.objects[self.key]})

    def test_symlinks(self):
        with self.assertRaisesRegex(ValueError, "regular files"):
            receive_artifacts(self.stream(symlink=True), self.objects)

    def test_size_mismatch(self):
        with self.assertRaisesRegex(ValueError, "size mismatch"):
            receive_artifacts(self.stream(content=b"short"), self.objects)


if __name__ == "__main__":
    unittest.main()
