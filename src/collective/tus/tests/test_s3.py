"""Tests for direct-to-S3 uploads.

S3 is replaced by an in-memory fake; the marker test needs zodb-s3blobs.
"""

from collective.tus.testing import COLLECTIVE_TUS_FUNCTIONAL_TESTING
from io import BytesIO
from plone import api
from plone.app.testing import login
from plone.app.testing import SITE_OWNER_NAME
from zope.annotation.interfaces import IAnnotations

import os
import sys
import time
import transaction
import unittest


class FakeS3Client:
    """In-memory stand-in for ``zodb_s3blobs.s3client.S3Client``.

    Records the parts uploaded for each (key, upload_id) and tracks the
    multipart upload lifecycle (created / completed / aborted). Just enough
    surface to exercise ``S3TUSUpload``.
    """

    def __init__(self, bucket_name="test-bucket"):
        self.bucket_name = bucket_name
        # {key: upload_id}
        self._uploads = {}
        # {(key, upload_id): {part_number: {'ETag': ..., 'Size': ...}}}
        self._parts = {}
        self._completed = set()
        self._aborted = set()
        self._next_upload_id = 1
        # Lifecycle state: in-memory rules list + counters for assertions.
        self._lifecycle_rules = []
        self._lifecycle_get_calls = 0
        self._lifecycle_put_calls = 0
        # Configurable error injection for tests.
        self._lifecycle_get_raises = None
        self._lifecycle_put_raises = None

    def generate_upload_part_presigned_url(
        self, s3_key, upload_id, part_number, expires_in=300
    ):
        # Deterministic stub URL so tests can assert routing/output.
        return (
            f"https://fake-s3.example/{s3_key}"
            f"?uploadId={upload_id}"
            f"&partNumber={part_number}"
            f"&X-Amz-Signature=stub-signature"
        )

    def ensure_abort_multipart_lifecycle_rule(self, rule_id, prefix, days=7):
        self._lifecycle_get_calls += 1
        if self._lifecycle_get_raises is not None:
            raise self._lifecycle_get_raises
        if any(r.get("ID") == rule_id for r in self._lifecycle_rules):
            return True
        self._lifecycle_put_calls += 1
        if self._lifecycle_put_raises is not None:
            raise self._lifecycle_put_raises
        self._lifecycle_rules.append(
            {
                "ID": rule_id,
                "Status": "Enabled",
                "Filter": {"Prefix": prefix},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": days},
                "Expiration": {"Days": days},
            }
        )
        return True

    def create_multipart_upload(self, s3_key):
        upload_id = f"mpu-{self._next_upload_id}"
        self._next_upload_id += 1
        self._uploads[s3_key] = upload_id
        self._parts[(s3_key, upload_id)] = {}
        return upload_id

    def upload_part(self, s3_key, upload_id, part_number, body):
        # Drain the body to simulate boto3 reading it.
        data = body.read() if hasattr(body, "read") else body
        size = len(data)
        etag = f'"etag-{s3_key}-{part_number}"'
        self._parts[(s3_key, upload_id)][part_number] = {
            "ETag": etag,
            "Size": size,
            "Data": data,
        }
        return etag

    def list_parts(self, s3_key, upload_id):
        parts = self._parts.get((s3_key, upload_id), {})
        return [
            {"PartNumber": n, "ETag": p["ETag"], "Size": p["Size"]}
            for n, p in sorted(parts.items())
        ]

    def complete_multipart_upload(self, s3_key, upload_id, parts):
        self._completed.add((s3_key, upload_id))

    def abort_multipart_upload(self, s3_key, upload_id):
        self._aborted.add((s3_key, upload_id))


class FakeS3Storage:
    """Stand-in for S3BlobStorage: the handoff only needs it to be set."""


class TestS3TUSUpload(unittest.TestCase):
    """Unit tests for the annotation-backed S3 TUS path."""

    layer = COLLECTIVE_TUS_FUNCTIONAL_TESTING

    def setUp(self):
        from plone import api as plone_api

        self.app = self.layer["app"]
        self.portal = self.layer["portal"]
        login(self.portal, SITE_OWNER_NAME)
        self.folder = plone_api.content.create(
            container=self.portal,
            type="Folder",
            id="s3tustestfolder",
            title="S3 TUS Test Folder",
        )
        transaction.commit()
        self.s3 = FakeS3Client()
        self.storage = FakeS3Storage()

    def _make_upload(self, uid="abc123", length=30 * 1024 * 1024, **extra):
        from collective.tus.s3 import S3TUSUpload

        meta = {
            "length": length,
            "filename": "test.bin",
            "content-type": "application/octet-stream",
            "@type": "File",
            "mode": "create",
        }
        meta.update(extra)
        return S3TUSUpload(
            uid,
            container=self.folder,
            metadata=meta,
            s3_storage=self.storage,
            s3_client=self.s3,
        )

    def test_initialization_creates_multipart_and_writes_annotation(self):
        from collective.tus.s3 import _container_uploads
        from collective.tus.s3 import ANNOTATION_KEY

        self._make_upload(uid="uid-1")
        # Multipart created with deterministic staging key
        self.assertEqual(self.s3._uploads, {"tus-staging/uid-1": "mpu-1"})
        # Annotation written
        uploads = _container_uploads(self.folder)
        self.assertIsNotNone(uploads)
        self.assertIn("uid-1", uploads)
        entry = uploads["uid-1"]
        self.assertEqual(entry["staging_key"], "tus-staging/uid-1")
        self.assertEqual(entry["multipart_upload_id"], "mpu-1")
        self.assertEqual(entry["length"], 30 * 1024 * 1024)
        self.assertIsNone(entry["chunk_size"])
        # Sanity: ANNOTATION_KEY is the right place
        self.assertIn(ANNOTATION_KEY, IAnnotations(self.folder))

    @unittest.skipIf(
        sys.version_info < (3, 12), "zodb-s3blobs needs Python 3.12 or later"
    )
    def test_process_blob_writes_marker_the_storage_accepts(self):
        from ZODB.blob import Blob
        from zodb_s3blobs.storage import _parse_staged_marker

        tus = self._make_upload(uid="0123abcd", length=12345)
        blob = Blob()
        with blob.open("w") as f:
            f.write(b"placeholder")
        tus.process_blob(blob)

        self.assertEqual(
            _parse_staged_marker(blob._p_blob_uncommitted), "tus-staging/0123abcd"
        )
        self.assertTrue(tus._handoff_done)

    def test_metadata_and_length_read_from_annotation(self):
        tus = self._make_upload(filename="report.pdf", length=12345)
        self.assertEqual(tus.length(), 12345)
        self.assertEqual(tus.metadata()["filename"], "report.pdf")

    def test_offset_zero_until_first_chunk_uploaded(self):
        tus = self._make_upload()
        self.assertEqual(tus.offset(), 0)

    def test_offset_uses_contiguous_prefix_of_parts(self):
        # 30 MiB total, 10 MiB chunks. Upload parts 1, 2, and 4 — gap at 3.
        # Contiguous offset = 2 * chunk_size.
        chunk = 10 * 1024 * 1024
        tus = self._make_upload(length=4 * chunk)
        # Manually inject parts since we don't have a real request body here.
        key = "tus-staging/abc123"
        upload_id = "mpu-1"
        for part_number in (1, 2, 4):
            self.s3._parts[(key, upload_id)][part_number] = {
                "ETag": f'"e-{part_number}"',
                "Size": chunk,
                "Data": b"",
            }
        # Set chunk_size in the annotation so offset() uses it.
        from collective.tus.s3 import _container_uploads

        entry = _container_uploads(self.folder)["abc123"]
        entry["chunk_size"] = chunk
        _container_uploads(self.folder)["abc123"] = entry
        self.assertEqual(tus.offset(), 2 * chunk)

    def test_write_uses_deterministic_part_number(self):
        chunk = 10 * 1024 * 1024
        length = 3 * chunk
        tus = self._make_upload(uid="dpn", length=length)
        # First chunk at offset 0.
        tus.write(BytesIO(b"x" * chunk), offset=0, content_length=chunk)
        # Second chunk at offset chunk.
        tus.write(BytesIO(b"y" * chunk), offset=chunk, content_length=chunk)
        parts = self.s3._parts[("tus-staging/dpn", "mpu-1")]
        self.assertEqual(sorted(parts.keys()), [1, 2])
        self.assertEqual(parts[1]["Size"], chunk)
        self.assertEqual(parts[2]["Size"], chunk)

    def test_write_first_patch_learns_chunk_size(self):
        from collective.tus.s3 import _container_uploads

        chunk = 10 * 1024 * 1024
        tus = self._make_upload(uid="learn", length=3 * chunk)
        tus.write(BytesIO(b"x" * chunk), offset=0, content_length=chunk)
        entry = _container_uploads(self.folder)["learn"]
        self.assertEqual(entry["chunk_size"], chunk)

    def test_write_rejects_non_final_chunk_below_5mib(self):
        from collective.tus.upload import TUSUploadError

        tus = self._make_upload(uid="small", length=100 * 1024 * 1024)
        small = 4 * 1024 * 1024  # below 5 MiB
        with self.assertRaises(TUSUploadError) as ctx:
            tus.write(BytesIO(b"x" * small), offset=0, content_length=small)
        self.assertIn("5 MiB", str(ctx.exception))

    def test_write_rejects_offset_not_multiple_of_chunk_size(self):
        from collective.tus.upload import TUSUploadError

        chunk = 10 * 1024 * 1024
        tus = self._make_upload(uid="badoff", length=3 * chunk)
        tus.write(BytesIO(b"x" * chunk), offset=0, content_length=chunk)
        # Now send at a misaligned offset.
        with self.assertRaises(TUSUploadError) as ctx:
            tus.write(
                BytesIO(b"y" * chunk),
                offset=chunk + 1,
                content_length=chunk,
            )
        self.assertIn("not a multiple", str(ctx.exception))

    def test_write_rejects_size_mismatch_for_non_final_chunk(self):
        from collective.tus.upload import TUSUploadError

        chunk = 10 * 1024 * 1024
        tus = self._make_upload(uid="badsize", length=3 * chunk)
        tus.write(BytesIO(b"x" * chunk), offset=0, content_length=chunk)
        # Subsequent non-final PATCH must equal chunk_size.
        bad = chunk - 100
        with self.assertRaises(TUSUploadError) as ctx:
            tus.write(BytesIO(b"y" * bad), offset=chunk, content_length=bad)
        self.assertIn("does not match", str(ctx.exception))

    def test_final_chunk_completes_multipart(self):
        chunk = 10 * 1024 * 1024
        length = 2 * chunk + 100  # final part is small (100 bytes)
        tus = self._make_upload(uid="finish", length=length)
        tus.write(BytesIO(b"a" * chunk), offset=0, content_length=chunk)
        tus.write(BytesIO(b"b" * chunk), offset=chunk, content_length=chunk)
        # Final small chunk
        tus.write(BytesIO(b"c" * 100), offset=2 * chunk, content_length=100)
        self.assertTrue(tus.finished)
        self.assertIn(("tus-staging/finish", "mpu-1"), self.s3._completed)

    def test_complete_with_missing_parts_raises_409(self):
        from collective.tus.upload import TUSUploadError

        chunk = 10 * 1024 * 1024
        length = 3 * chunk
        tus = self._make_upload(uid="gap", length=length)
        tus.write(BytesIO(b"a" * chunk), offset=0, content_length=chunk)
        # Skip middle chunk; pretend client thinks last chunk completes
        # (length boundary). The write at offset=2*chunk would set is_final.
        with self.assertRaises(TUSUploadError) as ctx:
            tus.write(
                BytesIO(b"c" * chunk),
                offset=2 * chunk,
                content_length=chunk,
            )
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("missing parts", str(ctx.exception))

    def test_cleanup_unfinished_aborts_multipart_and_drops_entry(self):
        from collective.tus.s3 import _container_uploads

        tus = self._make_upload(uid="abort")
        tus.cleanup()
        self.assertIn(("tus-staging/abort", "mpu-1"), self.s3._aborted)
        self.assertNotIn("abort", _container_uploads(self.folder))

    def test_cleanup_finished_does_not_abort(self):
        chunk = 10 * 1024 * 1024
        length = chunk
        tus = self._make_upload(uid="ok", length=length)
        tus.write(BytesIO(b"x" * chunk), offset=0, content_length=chunk)
        self.assertTrue(tus.finished)
        # Simulate handoff completing before cleanup
        tus._handoff_done = True
        tus.cleanup()
        self.assertNotIn(("tus-staging/ok", "mpu-1"), self.s3._aborted)

    def test_cleanup_expired_removes_old_entries(self):
        from collective.tus.s3 import _container_uploads

        tus_old = self._make_upload(uid="old")
        tus_new = self._make_upload(uid="new")
        # Backdate "old" past the expiration period.
        uploads = _container_uploads(self.folder)
        uploads["old"]["last_active"] = time.time() - tus_old.expiration_period - 1
        # Triggering cleanup_expired through any S3TUSUpload instance.
        tus_new.cleanup_expired()
        self.assertNotIn("old", _container_uploads(self.folder))
        self.assertIn("new", _container_uploads(self.folder))
        self.assertIn(("tus-staging/old", "mpu-1"), self.s3._aborted)

    def tearDown(self):
        from collective.tus.s3 import ANNOTATION_KEY

        ann = IAnnotations(self.folder)
        if ANNOTATION_KEY in ann:
            del ann[ANNOTATION_KEY]
        # Tear the folder down so the next test starts clean.
        api.content.delete(obj=self.folder)
        transaction.commit()


class _FakeResponse:
    def __init__(self):
        self.headers = {}
        self.status = 200

    def setHeader(self, name, value):
        self.headers[name] = value

    def getHeader(self, name, default=None):
        return self.headers.get(name, default)

    def setStatus(self, status, lock=0):
        self.status = status

    def setBody(self, body, is_error=0):
        self.body = body


class _FakeRequest:
    """Minimal stand-in for a Zope/Plone request object.

    Exposes the surface UploadAuthorize touches: getHeader, response, plus
    the dict-like ``get`` for environment-style lookups (REQUEST_METHOD).
    """

    def __init__(self, headers=None, method="GET"):
        self._headers = dict(headers or {})
        self.response = _FakeResponse()
        self._env = {"REQUEST_METHOD": method}
        self._rest_cors_preflight = False

    def getHeader(self, name, default=None):
        for k, v in self._headers.items():
            if k.lower() == name.lower():
                return v
        return default

    def get(self, key, default=None):
        return self._env.get(key, default)


class TestUploadAuthorize(unittest.TestCase):
    """Unit tests for the @tus-authorize endpoint's routing logic."""

    layer = COLLECTIVE_TUS_FUNCTIONAL_TESTING

    def setUp(self):
        from plone import api as plone_api

        self.app = self.layer["app"]
        self.portal = self.layer["portal"]
        login(self.portal, SITE_OWNER_NAME)
        self.folder = plone_api.content.create(
            container=self.portal,
            type="Folder",
            id="authorizetest",
            title="Authorize Test",
        )
        transaction.commit()
        self.s3 = FakeS3Client(bucket_name="authorize-bucket")
        self.storage = FakeS3Storage()
        # Force the offload on for this class. Tests that need it off can
        # delete the env var locally.
        os.environ["TUS_S3_OFFLOAD_ENABLED"] = "1"

    def tearDown(self):
        from collective.tus.s3 import ANNOTATION_KEY

        os.environ.pop("TUS_S3_OFFLOAD_ENABLED", None)
        ann = IAnnotations(self.folder)
        if ANNOTATION_KEY in ann:
            del ann[ANNOTATION_KEY]
        api.content.delete(obj=self.folder)
        transaction.commit()

    def _make_s3_upload(self, uid, length, chunk_size=None):
        from collective.tus.s3 import S3TUSUpload

        meta = {
            "length": length,
            "filename": "x.bin",
            "content-type": "application/octet-stream",
            "@type": "File",
            "mode": "create",
        }
        tus = S3TUSUpload(
            uid,
            container=self.folder,
            metadata=meta,
            s3_storage=self.storage,
            s3_client=self.s3,
        )
        if chunk_size is not None:
            from collective.tus.s3 import _container_uploads

            _container_uploads(self.folder)[uid]["chunk_size"] = chunk_size
        return tus

    def _authorize(self, uid, headers):
        """Invoke UploadAuthorize.reply() with patched S3 resolution.

        Bypasses ``__init__`` (the ``Service`` base inherits ``object``'s
        zero-arg init and relies on the publisher to set ``context`` /
        ``request``); we set them by hand for the unit test.
        """
        from collective.tus import s3 as tus_module
        from collective.tus.s3 import UploadAuthorize

        request = _FakeRequest(headers=headers)
        service = UploadAuthorize.__new__(UploadAuthorize)
        service.context = self.folder
        service.request = request
        service.uid = uid
        service.__name__ = "@tus-authorize"
        original_resolve = tus_module._resolve_s3_blob_storage
        tus_module._resolve_s3_blob_storage = lambda ctx: (
            self.storage,
            self.s3,
        )
        try:
            body = service.reply()
        finally:
            tus_module._resolve_s3_blob_storage = original_resolve
        return request.response, body

    def test_offload_disabled_routes_to_plone(self):
        os.environ.pop("TUS_S3_OFFLOAD_ENABLED", None)
        self._make_s3_upload("uid1", length=20 * 1024 * 1024)
        response, _ = self._authorize(
            "uid1",
            {
                "X-Original-Upload-Offset": "0",
                "X-Original-Content-Length": str(10 * 1024 * 1024),
            },
        )
        self.assertEqual(response.headers.get("X-Route"), "plone")
        self.assertEqual(response.status, 200)
        self.assertNotIn("X-S3-Url", response.headers)

    def test_unknown_uid_returns_404(self):
        # No upload created — annotation lookup will miss.
        response, _ = self._authorize(
            "missing-uid",
            {
                "X-Original-Upload-Offset": "0",
                "X-Original-Content-Length": "10485760",
            },
        )
        self.assertEqual(response.status, 404)

    def test_first_chunk_returns_presigned_url(self):
        chunk = 10 * 1024 * 1024
        self._make_s3_upload("uid2", length=3 * chunk)
        response, _ = self._authorize(
            "uid2",
            {
                "X-Original-Upload-Offset": "0",
                "X-Original-Content-Length": str(chunk),
            },
        )
        self.assertEqual(response.headers.get("X-Route"), "s3")
        self.assertIn("tus-staging/uid2", response.headers.get("X-S3-Url", ""))
        self.assertIn("partNumber=1", response.headers.get("X-S3-Url", ""))
        self.assertEqual(response.headers.get("X-Tus-New-Offset"), str(chunk))
        self.assertEqual(response.headers.get("X-Tus-Part-Number"), "1")

    def test_subsequent_chunk_uses_correct_part_number(self):
        chunk = 10 * 1024 * 1024
        self._make_s3_upload("uid3", length=3 * chunk, chunk_size=chunk)
        response, _ = self._authorize(
            "uid3",
            {
                "X-Original-Upload-Offset": str(chunk),
                "X-Original-Content-Length": str(chunk),
            },
        )
        self.assertEqual(response.headers.get("X-Tus-Part-Number"), "2")
        self.assertIn("partNumber=2", response.headers.get("X-S3-Url", ""))
        self.assertEqual(response.headers.get("X-Tus-New-Offset"), str(2 * chunk))

    def test_final_chunk_routes_to_plone(self):
        chunk = 10 * 1024 * 1024
        # Length = 2 chunks + small tail; the third PATCH would complete it.
        length = 2 * chunk + 100
        self._make_s3_upload("uid4", length=length, chunk_size=chunk)
        response, _ = self._authorize(
            "uid4",
            {
                "X-Original-Upload-Offset": str(2 * chunk),
                "X-Original-Content-Length": "100",
            },
        )
        self.assertEqual(response.headers.get("X-Route"), "plone")
        self.assertEqual(response.headers.get("X-Tus-Authorize-Reason"), "final-chunk")

    def test_misaligned_offset_rejected(self):
        chunk = 10 * 1024 * 1024
        self._make_s3_upload("uid5", length=3 * chunk, chunk_size=chunk)
        response, _ = self._authorize(
            "uid5",
            {
                "X-Original-Upload-Offset": str(chunk + 1),
                "X-Original-Content-Length": str(chunk),
            },
        )
        self.assertEqual(response.status, 400)


class TestTUSLifecycleAutoApply(unittest.TestCase):
    """Tests for the once-per-process lifecycle rule auto-apply."""

    def setUp(self):
        # Reset the process-local set between tests.
        from collective.tus.s3 import _LIFECYCLE_APPLIED

        _LIFECYCLE_APPLIED.clear()
        self.s3 = FakeS3Client(bucket_name="bucket-a")

    def test_first_call_applies_and_marks_bucket(self):
        from collective.tus.s3 import _ensure_lifecycle_rule_once
        from collective.tus.s3 import _LIFECYCLE_APPLIED

        _ensure_lifecycle_rule_once(self.s3)
        self.assertEqual(self.s3._lifecycle_get_calls, 1)
        self.assertEqual(self.s3._lifecycle_put_calls, 1)
        self.assertIn("bucket-a", _LIFECYCLE_APPLIED)

    def test_second_call_is_a_noop(self):
        from collective.tus.s3 import _ensure_lifecycle_rule_once

        _ensure_lifecycle_rule_once(self.s3)
        _ensure_lifecycle_rule_once(self.s3)
        # Still only one round trip — the second call is short-circuited
        # by the process-local set.
        self.assertEqual(self.s3._lifecycle_get_calls, 1)
        self.assertEqual(self.s3._lifecycle_put_calls, 1)

    def test_failure_is_swallowed_and_bucket_still_marked(self):
        from collective.tus.s3 import _ensure_lifecycle_rule_once
        from collective.tus.s3 import _LIFECYCLE_APPLIED

        self.s3._lifecycle_get_raises = RuntimeError("boom")
        # Should not raise; should still record the bucket so subsequent
        # POSTs don't keep retrying.
        _ensure_lifecycle_rule_once(self.s3)
        self.assertIn("bucket-a", _LIFECYCLE_APPLIED)

    def test_separate_buckets_are_each_handled_once(self):
        from collective.tus.s3 import _ensure_lifecycle_rule_once

        s3_b = FakeS3Client(bucket_name="bucket-b")
        _ensure_lifecycle_rule_once(self.s3)
        _ensure_lifecycle_rule_once(s3_b)
        _ensure_lifecycle_rule_once(self.s3)
        _ensure_lifecycle_rule_once(s3_b)
        self.assertEqual(self.s3._lifecycle_get_calls, 1)
        self.assertEqual(s3_b._lifecycle_get_calls, 1)


class TestS3Services(unittest.TestCase):
    """POST, PATCH, HEAD and DELETE through the services, with S3 faked."""

    layer = COLLECTIVE_TUS_FUNCTIONAL_TESTING

    def setUp(self):
        from collective.tus import s3
        from plone.app.testing import SITE_OWNER_PASSWORD
        from plone.restapi.testing import RelativeSession

        self.portal = self.layer["portal"]
        login(self.portal, SITE_OWNER_NAME)
        self.folder = api.content.create(
            container=self.portal, type="Folder", id="s3services", title="S3"
        )
        transaction.commit()
        self.s3 = FakeS3Client()
        self.storage = FakeS3Storage()
        self._original_resolve = s3._resolve_s3_blob_storage
        s3._resolve_s3_blob_storage = lambda ctx: (self.storage, self.s3)
        s3._LIFECYCLE_APPLIED.clear()

        self.upload_url = f"{self.folder.absolute_url()}/@tus-upload"
        self.api_session = RelativeSession(self.portal.absolute_url(), test=self)
        self.api_session.headers.update({"Accept": "application/json"})
        self.api_session.auth = (SITE_OWNER_NAME, SITE_OWNER_PASSWORD)

    def tearDown(self):
        from collective.tus import s3

        s3._resolve_s3_blob_storage = self._original_resolve
        self.api_session.close()
        transaction.abort()  # see what the server thread committed
        api.content.delete(obj=self.portal["s3services"])
        transaction.commit()

    def post(self, length):
        from base64 import b64encode

        metadata = ",".join(
            f"{k} {b64encode(v.encode()).decode()}"
            for k, v in (
                ("filename", "big.bin"),
                ("content-type", "application/octet-stream"),
                ("@type", "File"),
            )
        )
        response = self.api_session.post(
            self.upload_url,
            headers={
                "Tus-Resumable": "1.0.0",
                "Upload-Length": str(length),
                "Upload-Metadata": metadata,
            },
        )
        self.assertEqual(response.status_code, 201)
        return response.headers["Location"]

    def patch(self, location, data, offset):
        return self.api_session.patch(
            location,
            headers={
                "Content-Type": "application/offset+octet-stream",
                "Upload-Offset": str(offset),
                "Tus-Resumable": "1.0.0",
            },
            data=data,
        )

    def entries(self):
        from collective.tus.s3 import _container_uploads

        self.folder._p_jar.sync()
        return dict(_container_uploads(self.folder) or {})

    @unittest.skipIf(
        sys.version_info < (3, 12), "zodb-s3blobs needs Python 3.12 or later"
    )
    def test_upload_in_two_chunks_creates_file_with_marker(self):
        from collective.tus.s3 import _LIFECYCLE_APPLIED
        from zodb_s3blobs.storage import _parse_staged_marker

        chunk = 5 * 1024 * 1024
        length = chunk + 100
        location = self.post(length)
        uid = location.rsplit("/", 1)[-1]
        self.assertIn(uid, self.entries())
        self.assertIn(self.s3.bucket_name, _LIFECYCLE_APPLIED)

        response = self.patch(location, b"a" * chunk, 0)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["Upload-Offset"], str(chunk))

        response = self.api_session.head(location, headers={"Tus-Resumable": "1.0.0"})
        self.assertEqual(response.headers["Upload-Offset"], str(chunk))
        self.assertEqual(response.headers["Upload-Length"], str(length))

        response = self.patch(location, b"b" * 100, chunk)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["Upload-Offset"], str(length))

        self.assertIn((f"tus-staging/{uid}", "mpu-1"), self.s3._completed)
        self.assertNotIn(uid, self.entries())
        obj = self.folder["big.bin"]
        self.assertEqual(obj.file.size, length)
        with obj.file._blob.open() as f:
            path = f.name
        self.assertEqual(_parse_staged_marker(path), f"tus-staging/{uid}")

    def test_small_non_final_chunk_is_rejected(self):
        location = self.post(20 * 1024 * 1024)
        response = self.patch(location, b"a" * 1024, 0)
        self.assertEqual(response.status_code, 400)
        self.assertIn("5 MiB", response.json()["error"]["message"])

    def test_delete_aborts_the_multipart_upload(self):
        location = self.post(20 * 1024 * 1024)
        uid = location.rsplit("/", 1)[-1]
        response = self.api_session.delete(location, headers={"Tus-Resumable": "1.0.0"})
        self.assertEqual(response.status_code, 204)
        self.assertIn((f"tus-staging/{uid}", "mpu-1"), self.s3._aborted)
        self.assertNotIn(uid, self.entries())

    def test_authorize_over_http(self):
        os.environ["TUS_S3_OFFLOAD_ENABLED"] = "1"
        try:
            chunk = 5 * 1024 * 1024
            location = self.post(3 * chunk)
            response = self.api_session.get(
                location.replace("@tus-upload", "@tus-authorize"),
                headers={
                    "X-Original-Upload-Offset": "0",
                    "X-Original-Content-Length": str(chunk),
                },
            )
        finally:
            os.environ.pop("TUS_S3_OFFLOAD_ENABLED", None)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Route"], "s3")
        self.assertIn("partNumber=1", response.headers["X-S3-Url"])
        self.assertEqual(
            self.entries()[location.rsplit("/", 1)[-1]]["chunk_size"], chunk
        )
