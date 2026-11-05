"""TUS services, registered for the add-on's browser layer.

They subclass plone.restapi's services and replace only what needs to
change: which upload class is used, the extensions advertised, the
Content-Length passed to ``write()``, and termination (DELETE).
"""

from base64 import b64decode
from collective.tus.upload import TUSUpload
from collective.tus.upload import TUSUploadError
from plone.protect.interfaces import IDisableCSRFProtection
from plone.rest.interfaces import ICORSPolicy
from plone.restapi.services import Service
from plone.restapi.services.content import tus as restapi_tus
from uuid import uuid4
from zope.component import queryMultiAdapter
from zope.interface import alsoProvides
from zope.interface import implementer
from zope.publisher.interfaces import IPublishTraverse

import logging

logger = logging.getLogger(__name__)


TUS_OPTIONS_RESPONSE_HEADERS = {
    "Tus-Resumable": "1.0.0",
    "Tus-Version": "1.0.0",
    "Tus-Extension": "creation,expiration,termination",
}


class UploadOptions(restapi_tus.UploadOptions):
    """OPTIONS: advertise the TUS version and extensions."""

    def reply(self):
        for name, value in TUS_OPTIONS_RESPONSE_HEADERS.items():
            self.request.response.setHeader(name, value)
        return Service.reply(self)


class TUSServiceMixin:
    """What every TUS service here shares."""

    def __call__(self):
        # As plone.restapi's TUSBaseService, but CORS preflight responses
        # advertise this add-on's extensions.
        policy = queryMultiAdapter((self.context, self.request), ICORSPolicy)
        if policy is not None:
            if self.request._rest_cors_preflight:
                policy.process_preflight_request()
                for name, value in TUS_OPTIONS_RESPONSE_HEADERS.items():
                    self.request.response.setHeader(name, value)
                return
            policy.process_simple_request()
        elif self.request._rest_cors_preflight:
            return
        return self.render()

    def disable_csrf_protection(self):
        """Exempt the request from plone.protect's automatic CSRF check.

        S3 uploads keep their state in the ZODB, so POST, PATCH and DELETE
        write to it. TUS has no way to send a CSRF token, and the services
        already require an authenticated user with the right permission.
        """
        alsoProvides(self.request, IDisableCSRFProtection)

    def create_upload(self, uid, metadata):
        """Create the upload for a POST: in S3 if the site's storage is
        zodb-s3blobs, otherwise on local disk."""
        from collective.tus import s3

        resolved = s3._resolve_s3_blob_storage(self.context)
        if resolved is not None:
            storage, s3_client = resolved
            s3._ensure_lifecycle_rule_once(s3_client)
            return s3.S3TUSUpload(
                uid,
                container=self.context,
                metadata=metadata,
                s3_storage=storage,
                s3_client=s3_client,
            )
        return TUSUpload(uid, metadata=metadata)

    def find_upload(self, uid):
        """Return the upload with this uid, or None."""
        from collective.tus import s3

        uploads = s3._container_uploads(self.context)
        if uploads is not None and uid in uploads:
            resolved = s3._resolve_s3_blob_storage(self.context)
            if resolved is None:
                # The upload was started in S3 but the storage no longer is
                # S3: a configuration problem, not a missing upload.
                logger.error(
                    "TUS: upload uid=%s is in S3 but the storage is not "
                    "S3BlobStorage. context=%s",
                    uid,
                    "/".join(self.context.getPhysicalPath()),
                )
                return None
            storage, s3_client = resolved
            return s3.S3TUSUpload(
                uid, container=self.context, s3_storage=storage, s3_client=s3_client
            )

        tus_upload = TUSUpload(uid)
        if tus_upload.length() == 0:
            logger.info(
                "TUS: upload not found. uid=%s method=%s context=%s",
                uid,
                self.request.get("REQUEST_METHOD", "?"),
                "/".join(self.context.getPhysicalPath()),
            )
            return None
        return tus_upload


class UploadPost(TUSServiceMixin, restapi_tus.UploadPost):
    """POST: create an upload."""

    def reply(self):
        if not self.check_tus_version():
            return self.unsupported_version()

        self.disable_csrf_protection()

        length = self.request.getHeader("Upload-Length", "")
        try:
            length = int(length)
        except ValueError:
            return self.error("Bad Request", "Missing or invalid Upload-Length header")

        metadata = {}
        for item in self.request.getHeader("Upload-Metadata", "").split(","):
            key_value = item.split()
            if len(key_value) == 2:
                key = key_value[0].lower()
                value = b64decode(key_value[1]).decode("utf-8")
                metadata[key] = value
        metadata["length"] = length
        if self.__name__.endswith("@tus-replace"):
            metadata["mode"] = "replace"
        else:
            metadata["mode"] = "create"

        tus_upload = self.create_upload(uuid4().hex, metadata)

        self.request.response.setStatus(201)
        url = self.request.getURL()
        # The Location is always @tus-upload, also for @tus-replace.
        if url.endswith("@tus-replace"):
            url = url.replace("@tus-replace", "@tus-upload")
        self.request.response.setHeader("Location", f"{url}/{tus_upload.uid}")
        self.request.response.setHeader("Upload-Expires", tus_upload.expires())
        self.request.response.setHeader("Tus-Resumable", "1.0.0")
        return Service.reply(self)


class UploadFileMixin(TUSServiceMixin):
    """For the services that act on one upload (``@tus-upload/<uid>``)."""

    def tus_upload(self):
        if self.uid is None:
            return None
        return self.find_upload(self.uid)


@implementer(IPublishTraverse)
class UploadHead(UploadFileMixin, restapi_tus.UploadHead):
    """HEAD: report an upload's length and offset."""


@implementer(IPublishTraverse)
class UploadPatch(UploadFileMixin, restapi_tus.UploadPatch):
    """PATCH: append a chunk; the last chunk creates or updates the content."""

    def reply(self):
        tus_upload = self.tus_upload()
        if tus_upload is None:
            return self.error("Not Found", "", 404)

        metadata = tus_upload.metadata()
        self.check_add_modify_permission(metadata.get("mode", "create"))

        if not self.check_tus_version():
            return self.unsupported_version()

        self.disable_csrf_protection()

        content_type = self.request.getHeader("Content-Type")
        if content_type != "application/offset+octet-stream":
            return self.error("Bad Request", "Missing or invalid Content-Type header")

        offset = self.request.getHeader("Upload-Offset", "")
        try:
            offset = int(offset)
        except ValueError:
            return self.error("Bad Request", "Missing or invalid Upload-Offset header")

        content_length = self.request.getHeader("Content-Length", "")
        try:
            content_length = int(content_length) if content_length else None
        except ValueError:
            content_length = None

        request_body = self.request._file
        if hasattr(request_body, "raw"):  # Unwrap io.BufferedRandom
            request_body = request_body.raw
        try:
            tus_upload.write(request_body, offset, content_length=content_length)
        except TUSUploadError as e:
            return self.error(e.error_type, str(e), e.status)
        offset = tus_upload.offset()

        if tus_upload.finished:
            self.create_or_modify_content(tus_upload)
        else:
            self.request.response.setHeader("Upload-Expires", tus_upload.expires())

        self.request.response.setHeader("Tus-Resumable", "1.0.0")
        self.request.response.setHeader("Upload-Offset", f"{offset}")
        return self.reply_no_content()


@implementer(IPublishTraverse)
class UploadDelete(UploadFileMixin, restapi_tus.UploadFileBase):
    """DELETE: terminate an upload and remove what was uploaded so far
    (the TUS termination extension)."""

    def reply(self):
        tus_upload = self.tus_upload()
        if tus_upload is None:
            return self.error("Not Found", "", 404)

        metadata = tus_upload.metadata()
        self.check_add_modify_permission(metadata.get("mode", "create"))

        if not self.check_tus_version():
            return self.unsupported_version()

        self.disable_csrf_protection()
        tus_upload.close()
        tus_upload.cleanup()
        self.request.response.setHeader("Tus-Resumable", "1.0.0")
        return self.reply_no_content()
