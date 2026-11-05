"""Tests for what this add-on changes in plone.restapi's TUS support."""

from base64 import b64encode
from collective.tus.deserializer import NamedFieldDeserializer
from collective.tus.interfaces import ICollectiveTusLayer
from collective.tus.testing import COLLECTIVE_TUS_FUNCTIONAL_TESTING
from collective.tus.testing import COLLECTIVE_TUS_INTEGRATION_TESTING
from collective.tus.upload import TUSUpload
from io import BytesIO
from plone import api
from plone.app.testing import login
from plone.app.testing import setRoles
from plone.app.testing import SITE_OWNER_NAME
from plone.app.testing import SITE_OWNER_PASSWORD
from plone.app.testing import TEST_USER_ID
from plone.browserlayer.utils import registered_layers
from plone.namedfile.field import NamedBlobFile as NamedBlobFileField
from plone.namedfile.field import NamedFile as NamedFileField
from plone.restapi.testing import RelativeSession
from unittest import mock
from ZODB.blob import rename_or_copy_blob
from zope.interface import alsoProvides

import os
import plone.restapi.tests
import shutil
import time
import transaction
import unittest

RESTAPI_TESTS_DIR = os.path.dirname(plone.restapi.tests.__file__)


def _metadata(filename, content_type, type_="File"):
    return ",".join(
        f"{key} {b64encode(value.encode()).decode()}"
        for key, value in (
            ("filename", filename),
            ("content-type", content_type),
            ("@type", type_),
        )
    )


def _clear_tmp_dir():
    tmp_dir = os.path.join(os.environ.get("CLIENT_HOME"), "tus-uploads")
    if os.path.isdir(tmp_dir):
        shutil.rmtree(tmp_dir)


class TestServices(unittest.TestCase):
    layer = COLLECTIVE_TUS_FUNCTIONAL_TESTING

    def setUp(self):
        self.portal = self.layer["portal"]
        login(self.portal, SITE_OWNER_NAME)
        self.folder = api.content.create(
            container=self.portal, type="Folder", id="folder", title="Folder"
        )
        self.upload_url = f"{self.folder.absolute_url()}/@tus-upload"
        transaction.commit()

        self.api_session = RelativeSession(self.portal.absolute_url(), test=self)
        self.api_session.headers.update({"Accept": "application/json"})
        self.api_session.auth = (SITE_OWNER_NAME, SITE_OWNER_PASSWORD)

    def tearDown(self):
        self.api_session.close()
        _clear_tmp_dir()

    def post(self, length, filename="test.txt", content_type="text/plain"):
        response = self.api_session.post(
            self.upload_url,
            headers={
                "Tus-Resumable": "1.0.0",
                "Upload-Length": str(length),
                "Upload-Metadata": _metadata(filename, content_type),
            },
        )
        self.assertEqual(response.status_code, 201)
        return response.headers["Location"]

    def patch(self, location, data, offset=0):
        return self.api_session.patch(
            location,
            headers={
                "Content-Type": "application/offset+octet-stream",
                "Upload-Offset": str(offset),
                "Tus-Resumable": "1.0.0",
            },
            data=data,
        )

    def upload_of(self, location):
        return TUSUpload(location.rsplit("/", 1)[-1])

    def test_options_advertises_termination(self):
        response = self.api_session.options(self.upload_url)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            response.headers["Tus-Extension"], "creation,expiration,termination"
        )

    def test_delete_removes_the_upload(self):
        location = self.post(10)
        self.assertEqual(self.patch(location, b"abcd").status_code, 204)
        upload = self.upload_of(location)
        self.assertTrue(os.path.exists(upload.filepath))

        response = self.api_session.delete(location, headers={"Tus-Resumable": "1.0.0"})

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["Tus-Resumable"], "1.0.0")
        self.assertFalse(os.path.exists(upload.filepath))
        self.assertFalse(os.path.exists(upload.metadata_path))
        response = self.api_session.head(location, headers={"Tus-Resumable": "1.0.0"})
        self.assertEqual(response.status_code, 404)

    def test_delete_unknown_upload_returns_404(self):
        response = self.api_session.delete(
            f"{self.upload_url}/{'0' * 32}", headers={"Tus-Resumable": "1.0.0"}
        )
        self.assertEqual(response.status_code, 404)

    def test_delete_with_unsupported_version_returns_412(self):
        location = self.post(10)
        response = self.api_session.delete(location, headers={"Tus-Resumable": "0.2.2"})
        self.assertEqual(response.status_code, 412)
        self.assertTrue(os.path.exists(self.upload_of(location).metadata_path))

    def test_delete_without_add_permission_returns_401(self):
        location = self.post(10)
        self.folder.manage_permission("Add portal content", [], 0)
        transaction.commit()

        response = self.api_session.delete(location, headers={"Tus-Resumable": "1.0.0"})

        self.assertEqual(response.status_code, 401)
        self.assertTrue(os.path.exists(self.upload_of(location).metadata_path))

    def test_delete_on_site_root(self):
        self.upload_url = f"{self.portal.absolute_url()}/@tus-upload"
        location = self.post(10)
        response = self.api_session.delete(location, headers={"Tus-Resumable": "1.0.0"})
        self.assertEqual(response.status_code, 204)
        self.assertFalse(os.path.exists(self.upload_of(location).metadata_path))

    def test_finished_upload_is_moved_into_the_blob(self):
        data = b"0123456789" * 1000
        location = self.post(len(data))
        upload = self.upload_of(location)

        with mock.patch(
            "collective.tus.upload.rename_or_copy_blob",
            wraps=rename_or_copy_blob,
        ) as rename:
            response = self.patch(location, data)

        self.assertEqual(response.status_code, 204)
        rename.assert_called_once()
        self.assertEqual(rename.call_args[0][0], upload.filepath)
        self.assertFalse(os.path.exists(upload.filepath))
        self.assertFalse(os.path.exists(upload.metadata_path))

        transaction.commit()
        obj = self.folder["test.txt"]
        self.assertEqual(obj.file.data, data)
        self.assertEqual(obj.file.size, len(data))
        self.assertEqual(obj.file.filename, "test.txt")
        self.assertEqual(obj.file.contentType, "text/plain")

    def test_upload_in_two_chunks(self):
        location = self.post(8)
        response = self.patch(location, b"abcd")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["Upload-Offset"], "4")
        response = self.patch(location, b"efgh", offset=4)
        self.assertEqual(response.status_code, 204)
        transaction.commit()
        self.assertEqual(self.folder["test.txt"].file.data, b"abcdefgh")

    def test_image_upload_has_its_dimensions(self):
        path = os.path.join(RESTAPI_TESTS_DIR, "image.png")
        with open(path, "rb") as f:
            data = f.read()
        self.upload_url = f"{self.folder.absolute_url()}/@tus-upload"
        response = self.api_session.post(
            self.upload_url,
            headers={
                "Tus-Resumable": "1.0.0",
                "Upload-Length": str(len(data)),
                "Upload-Metadata": _metadata("image.png", "image/png", "Image"),
            },
        )
        location = response.headers["Location"]
        self.assertEqual(self.patch(location, data).status_code, 204)

        transaction.commit()
        image = self.folder["image.png"].image
        self.assertEqual(image.data, data)
        self.assertEqual(image.contentType, "image/png")
        self.assertEqual(image.getImageSize(), (215, 56))


class TestDeserializer(unittest.TestCase):
    layer = COLLECTIVE_TUS_INTEGRATION_TESTING

    def setUp(self):
        self.portal = self.layer["portal"]
        self.request = self.layer["request"]
        alsoProvides(self.request, ICollectiveTusLayer)
        setRoles(self.portal, TEST_USER_ID, ["Manager"])
        self.doc = api.content.create(container=self.portal, type="Document", id="d")

    def tearDown(self):
        _clear_tmp_dir()

    def upload(self, data, cls=TUSUpload):
        tus = cls(
            "a" * 32,
            {"length": len(data), "filename": "f.txt", "content-type": "text/plain"},
        )
        tus.write(BytesIO(data))
        return tus

    def deserialize(self, field, value):
        field.__name__ = "file"
        return NamedFieldDeserializer(field, self.doc, self.request)(value)

    def test_blob_field_gets_the_upload_moved_in(self):
        tus = self.upload(b"hello world")
        value = self.deserialize(NamedBlobFileField(), tus)
        self.assertFalse(os.path.exists(tus.filepath))
        self.assertEqual(value.data, b"hello world")
        self.assertEqual(value.size, 11)
        self.assertEqual(value.filename, "f.txt")
        self.assertEqual(value.contentType, "text/plain")

    def test_size_is_the_upload_length(self):
        # An upload class can put something other than the uploaded bytes in
        # the blob (a reference to them, say). The size must still be right.
        class Marker(TUSUpload):
            def process_blob(self, blob):
                with open(blob._p_blob_uncommitted, "wb") as f:
                    f.write(b"marker")

        tus = self.upload(b"x" * 100, cls=Marker)
        value = self.deserialize(NamedBlobFileField(), tus)
        self.assertEqual(value.size, 100)

    def test_non_blob_field_is_copied_as_plone_restapi_does(self):
        tus = self.upload(b"hello")
        value = self.deserialize(NamedFileField(), tus)
        tus.close()
        self.assertEqual(value.data, b"hello")
        self.assertTrue(os.path.exists(tus.filepath))

    def test_other_values_are_left_to_plone_restapi(self):
        value = self.deserialize(
            NamedBlobFileField(),
            {"data": "aGk=", "encoding": "base64", "filename": "a.txt"},
        )
        self.assertEqual(value.data, b"hi")

    def test_required_field_is_validated(self):
        from zope.schema.interfaces import RequiredMissing

        with self.assertRaises(RequiredMissing):
            self.deserialize(NamedBlobFileField(required=True), "")


class TestUpload(unittest.TestCase):
    layer = COLLECTIVE_TUS_INTEGRATION_TESTING

    def tearDown(self):
        _clear_tmp_dir()

    def test_metadata_available_after_initialize(self):
        tus = TUSUpload("myuid", {"length": 3})
        os.remove(tus.metadata_path)
        self.assertEqual(tus.metadata(), {"length": 3})

    def test_write_accepts_content_length(self):
        tus = TUSUpload("myuid", {"length": 3})
        tus.write(BytesIO(b"abc"), 0, content_length=3)
        self.assertTrue(tus.finished)
        self.assertEqual(tus.offset(), 3)

    def test_cleanup_expired_removes_old_uploads(self):
        tus = TUSUpload("myuid")
        old = time.time() - tus.expiration_period - 10
        paths = {}
        for uid in ("old1", "old2", "new"):
            paths[uid] = os.path.join(tus.tmp_dir, f"tus_upload_{uid}")
        for uid in ("old1", "new"):
            with open(paths[uid], "wb") as f:
                f.write(b"x")
        for uid in paths:
            with open(paths[uid] + ".json", "w") as f:
                f.write("{}")
        for path in (paths["old1"], paths["old1"] + ".json", paths["old2"] + ".json"):
            os.utime(path, (old, old))

        tus.cleanup_expired()

        self.assertFalse(os.path.exists(paths["old1"]))
        self.assertFalse(os.path.exists(paths["old1"] + ".json"))
        self.assertFalse(os.path.exists(paths["old2"] + ".json"))
        self.assertTrue(os.path.exists(paths["new"]))
        self.assertTrue(os.path.exists(paths["new"] + ".json"))

    def test_cleanup_expired_tolerates_files_removed_by_another_process(self):
        tus = TUSUpload("myuid")
        path = os.path.join(tus.tmp_dir, "tus_upload_gone")
        with open(path + ".json", "w") as f:
            f.write("{}")
        old = time.time() - tus.expiration_period - 10
        os.utime(path + ".json", (old, old))

        real_remove = os.remove

        def remove_twice(p):
            real_remove(p)
            if p == path + ".json":
                raise FileNotFoundError(p)

        with mock.patch("collective.tus.upload.os.remove", side_effect=remove_twice):
            tus.cleanup_expired()
        self.assertFalse(os.path.exists(path + ".json"))


class TestInstall(unittest.TestCase):
    layer = COLLECTIVE_TUS_INTEGRATION_TESTING

    def test_browser_layer_registered(self):
        self.assertIn(ICollectiveTusLayer, registered_layers())

    def test_uninstall_removes_browser_layer(self):
        setup_tool = api.portal.get_tool("portal_setup")
        setup_tool.runAllImportStepsFromProfile("profile-collective.tus:uninstall")
        self.assertNotIn(ICollectiveTusLayer, registered_layers())
