"""Direct-to-S3 TUS uploads for sites whose ZODB storage is zodb-s3blobs'
S3BlobStorage.

Chunks go to an S3 multipart upload under a staging key. The upload's state is
kept in an annotation on its container, so any worker process can handle any
chunk. When the content is saved, the blob gets zodb-s3blobs' staged-upload
marker instead of the bytes, and at commit the storage copies the staging key
to the blob's own key within S3.

With ``TUS_S3_OFFLOAD_ENABLED`` set, the ``@tus-authorize`` endpoint lets a
front-end proxy (nginx ``auth_request``) send every chunk but the last straight
to S3 with a presigned URL, so they never pass through Zope.
"""

from Acquisition import aq_base
from BTrees.OOBTree import OOBTree
from collective.tus.services import UploadFileMixin
from collective.tus.upload import TUSUpload
from collective.tus.upload import TUSUploadError
from email.utils import formatdate
from io import BytesIO
from persistent.mapping import PersistentMapping
from plone.restapi.services.content import tus as restapi_tus
from tempfile import mkstemp
from ZODB.blob import rename_or_copy_blob
from zope.annotation.interfaces import IAnnotations
from zope.interface import implementer
from zope.publisher.interfaces import IPublishTraverse

import logging
import os
import time

logger = logging.getLogger(__name__)


# Annotation key on the upload's container (folder/site root). Holds an
# OOBTree[uid → PersistentMapping(descriptor)] for in-progress S3 uploads.
# The name predates this package and is kept so uploads in progress carry
# over.
ANNOTATION_KEY = "plone.restapi.tus_uploads"

# S3 staging key prefix and lifecycle rule ID for auto-aborting abandoned
# multipart uploads. zodb-s3blobs only accepts staged markers for keys under
# this prefix.
TUS_STAGING_PREFIX = "tus-staging/"
TUS_LIFECYCLE_RULE_ID = "tus-staging-abort"
TUS_LIFECYCLE_DAYS = 7

# Feature flag controlling the nginx-mediated direct-to-S3 offload.
# When unset/false, the @tus-authorize endpoint always returns
# X-Route: plone, which keeps PATCH bodies flowing through the Zope worker
# (the legacy/safe path). When true, @tus-authorize hands nginx a
# presigned UploadPart URL for non-final chunks; only the final chunk goes
# back through Plone for the transactional CompleteMultipartUpload +
# content-creation step. See the README for the nginx configuration.
TUS_S3_OFFLOAD_ENV = "TUS_S3_OFFLOAD_ENABLED"
TUS_PRESIGNED_URL_EXPIRES = 300  # seconds — generous for slow uploads


def _s3_offload_enabled():
    return os.environ.get(TUS_S3_OFFLOAD_ENV, "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# Process-local set of bucket names already checked / had the rule applied
# during this process's lifetime. Auto-apply runs at most once per bucket
# per process, regardless of success — never block uploads on lifecycle.
_LIFECYCLE_APPLIED: set = set()


def _ensure_lifecycle_rule_once(s3_client):
    """Apply the abort-multipart lifecycle rule on first POST per process.

    Idempotent within a process; tolerant of failure (logs a warning and
    moves on). The S3Client method itself is idempotent against the bucket
    too, so even repeated calls would be safe — this just avoids the round
    trip after the first call.
    """
    bucket = getattr(s3_client, "bucket_name", None)
    if not bucket or bucket in _LIFECYCLE_APPLIED:
        return
    try:
        s3_client.ensure_abort_multipart_lifecycle_rule(
            rule_id=TUS_LIFECYCLE_RULE_ID,
            prefix=TUS_STAGING_PREFIX,
            days=TUS_LIFECYCLE_DAYS,
        )
    except Exception:
        logger.warning(
            "TUS/S3: lifecycle setup raised unexpectedly for bucket=%s; "
            "continuing without it.",
            bucket,
            exc_info=True,
        )
    # Mark applied either way so we don't retry on every POST.
    _LIFECYCLE_APPLIED.add(bucket)


def _resolve_s3_blob_storage(context):
    """Return (storage, s3_client) if the context's ZODB storage is an
    S3BlobStorage, else None. Import-guarded so environments without
    zodb-s3blobs installed continue to work.

    Returns the **Connection's** MVCC instance of the storage (``jar._storage``),
    not ``db.storage``. ZODB's ``DB.open()`` calls ``new_instance()`` on the
    storage for each connection, and commits happen through that per-connection
    instance. The S3BlobStorage keeps pending/staged state on the instance, so
    registrations made on ``db.storage`` (the primary) would be invisible at
    commit time on the per-connection instance.
    """
    try:
        from zodb_s3blobs.storage import S3BlobStorage
    except ImportError:
        logger.debug("TUS/S3: zodb_s3blobs not installed; staying in local mode")
        return None
    jar = getattr(aq_base(context), "_p_jar", None)
    if jar is None:
        logger.debug("TUS/S3: context has no _p_jar; staying in local mode")
        return None
    storage = getattr(jar, "_storage", None)
    if storage is None:
        db = jar.db()
        storage = db.storage if db is not None else None
    if not isinstance(storage, S3BlobStorage):
        logger.debug(
            "TUS/S3: storage %r is not S3BlobStorage; staying in local mode",
            type(storage).__name__,
        )
        return None
    logger.debug("TUS/S3: resolved per-connection S3BlobStorage id=%s", id(storage))
    return storage, storage._s3_client


def _container_uploads(container, create=False):
    """Return the OOBTree of in-progress S3 uploads for this container.

    If ``create`` is False and no annotation exists, returns None.
    """
    annotations = IAnnotations(container)
    uploads = annotations.get(ANNOTATION_KEY)
    if uploads is None:
        if not create:
            return None
        uploads = OOBTree()
        annotations[ANNOTATION_KEY] = uploads
    return uploads


@implementer(IPublishTraverse)
class UploadAuthorize(UploadFileMixin, restapi_tus.UploadFileBase):
    """Authorise a single TUS PATCH chunk for direct-to-S3 forwarding.

    This is *not* part of the TUS protocol — it's an internal endpoint used
    by nginx's ``auth_request`` directive in the data-plane offload setup.
    nginx makes a body-less GET subrequest here for every PATCH the client
    sends; the response headers tell nginx whether to:

    * stream the body straight to S3 via a presigned ``UploadPart`` URL
      (``X-Route: s3`` + ``X-S3-Url``), or
    * forward the request to Plone unchanged (``X-Route: plone``),
      which is the path the final chunk takes so Plone can run
      ``CompleteMultipartUpload`` + content creation transactionally.

    Behaviour is controlled by the ``TUS_S3_OFFLOAD_ENABLED`` env var. When
    disabled (default), every chunk routes back to Plone — equivalent to
    not having the offload at all, used as a kill switch.

    The endpoint is auth-checked exactly like the other TUS handlers
    (``Add portal content`` / ``Modify portal content``), so a leaked
    ``@tus-authorize`` URL conveys no more privilege than a leaked
    ``@tus-upload`` URL.
    """

    def _route_to_plone(self, *, reason=None):
        """Build a 'send the body to Plone' authorize response."""
        self.request.response.setHeader("X-Route", "plone")
        if reason:
            self.request.response.setHeader("X-Tus-Authorize-Reason", reason)
        self.request.response.setStatus(200, lock=1)
        logger.debug(
            "TUS/S3: authorize routed to Plone uid=%s reason=%s",
            getattr(self, "uid", None),
            reason,
        )
        return ""

    def _original_int_header(self, *names):
        """Return the first parseable header value from ``names``, or None.

        nginx auth_request sub-requests have no original body, so
        ``Upload-Offset`` / ``Content-Length`` are forwarded under
        ``X-Original-*`` aliases by the nginx config. Accept either name
        so the endpoint is callable for ad-hoc testing too.
        """
        for name in names:
            raw = self.request.getHeader(name, "")
            if raw == "" or raw is None:
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
        return None

    def reply(self):
        # If offload is disabled, route everything back to Plone — this
        # makes the endpoint safe to deploy ahead of the nginx config and
        # acts as a runtime kill switch.
        if not _s3_offload_enabled():
            return self._route_to_plone(reason="offload-disabled")

        tus_upload = self.tus_upload()
        if tus_upload is None:
            return self.error("Not Found", "", 404)

        metadata = tus_upload.metadata()
        self.check_add_modify_permission(metadata.get("mode", "create"))

        # Local-disk uploads can't be offloaded — there's no S3 multipart
        # to PUT into. Route to Plone.
        if not isinstance(tus_upload, S3TUSUpload):
            return self._route_to_plone(reason="local-mode")

        offset = self._original_int_header("X-Original-Upload-Offset", "Upload-Offset")
        content_length = self._original_int_header(
            "X-Original-Content-Length", "Content-Length"
        )
        if offset is None or content_length is None:
            return self.error(
                "Bad Request",
                "Missing or invalid Upload-Offset / Content-Length",
            )

        # ``metadata()`` returns a copy of the persisted entry — it
        # contains staging_key, multipart_upload_id, length, chunk_size,
        # and the descriptor fields. We don't mutate it here.
        entry = tus_upload.metadata()
        if not entry:
            return self.error("Not Found", "", 404)

        length = int(entry["length"])
        chunk_size = entry.get("chunk_size")
        is_final = (offset + content_length) >= length

        # Validate alignment / size against the established chunk_size.
        # First PATCH learns chunk_size — we don't reject it here, but we
        # do need to know the size to compute a part_number, so we accept
        # the client's Content-Length as the chunk size for this request.
        if chunk_size is None:
            if not is_final and content_length < 5 * 1024 * 1024:
                return self.error(
                    "Bad Request",
                    "Non-final chunk must be at least 5 MiB",
                )
            effective_chunk_size = content_length
        else:
            if offset % chunk_size != 0:
                return self.error(
                    "Bad Request",
                    f"Upload-Offset {offset} is not a multiple of "
                    f"chunk_size {chunk_size}",
                )
            if not is_final and content_length != chunk_size:
                return self.error(
                    "Bad Request",
                    f"Non-final chunk size {content_length} does not match "
                    f"chunk_size {chunk_size}",
                )
            effective_chunk_size = chunk_size

        # Persist chunk_size on the first chunk's authorize, so that when
        # the final chunk arrives at Plone (offload-bypassed up to this
        # point) it has the correct value for part_number calculation.
        # One write per upload — see S3TUSUpload.remember_chunk_size.
        # Only this branch writes, so CSRF only needs disabling here
        # (nginx's auth_request subrequest carries no CSRF token; the
        # endpoint is already gated by the upload's Add/Modify perm).
        if chunk_size is None:
            self.disable_csrf_protection()
            tus_upload.remember_chunk_size(effective_chunk_size)

        # Final chunk goes through Plone — that's where the transactional
        # CompleteMultipartUpload + content-creation work happens.
        if is_final:
            return self._route_to_plone(reason="final-chunk")

        part_number = (offset // effective_chunk_size) + 1
        try:
            url = tus_upload._s3_client.generate_upload_part_presigned_url(
                entry["staging_key"],
                entry["multipart_upload_id"],
                part_number,
                expires_in=TUS_PRESIGNED_URL_EXPIRES,
            )
        except Exception:
            logger.exception(
                "TUS/S3: presigning failed for uid=%s part=%d; falling "
                "back to Plone-routed path for this chunk.",
                self.uid,
                part_number,
            )
            return self._route_to_plone(reason="presign-failed")

        new_offset = offset + content_length
        self.request.response.setHeader("X-Route", "s3")
        self.request.response.setHeader("X-S3-Url", url)
        self.request.response.setHeader("X-Tus-New-Offset", str(new_offset))
        self.request.response.setHeader("X-Tus-Part-Number", str(part_number))
        # Surface whether this was the chunk_size-learning PATCH so an
        # operator inspecting headers can tell. nginx doesn't act on it.
        if chunk_size is None:
            self.request.response.setHeader(
                "X-Tus-Chunk-Size-Learned", str(effective_chunk_size)
            )
        self.request.response.setStatus(200, lock=1)
        logger.debug(
            "TUS/S3: authorize routed to S3 uid=%s part=%d offset=%d size=%d "
            "presigned_expires_in=%d",
            self.uid,
            part_number,
            offset,
            content_length,
            TUS_PRESIGNED_URL_EXPIRES,
        )
        return ""


class S3TUSUpload(TUSUpload):
    """Multi-instance-safe TUS upload backed by S3 multipart + ZODB annotation.

    State lives in two places:
    - **Container annotation** (``IAnnotations(container)[ANNOTATION_KEY]``):
      a small descriptor (length, filename, content-type, fieldname, mode,
      multipart_upload_id, staging_key, chunk_size, created, last_active).
      Shared across workers via ZODB; no local disk involvement.
    - **S3 multipart upload**: parts themselves. Source of truth for what's
      been uploaded; recovered via ``list_parts`` for HEAD/Complete.

    Per-PATCH flow is intentionally tiny: the request body streams straight
    into ``upload_part`` with ``part_number = offset / chunk_size + 1``.
    There's no local buffer, no flush threshold, no double-pass over data.
    Sticky routing is unnecessary because any worker can resolve uid →
    annotation → multipart_upload_id and proceed.
    """

    # Subclass init must NOT touch tmp_dir/filepath like the parent does.
    file_prefix = TUSUpload.file_prefix
    expiration_period = TUSUpload.expiration_period

    def __init__(self, uid, container, metadata=None, s3_storage=None, s3_client=None):
        # Deliberately bypass TUSUpload.__init__ — it creates a tmp dir we
        # don't need. Set the minimum attributes the parent class methods
        # might still touch.
        self.uid = uid
        self.container = container
        self.tmp_dir = None
        self.filepath = None
        self.metadata_path = None
        self._metadata = None
        self._file = None
        self.finished = False
        self._s3_storage = s3_storage
        self._s3_client = s3_client
        # Set after process_blob hands the staging key to the storage so
        # cleanup() knows whether to delete the object on abort.
        self._handoff_done = False

        if metadata is not None:
            self._initialize(metadata)

    @property
    def s3_mode(self):
        return True

    @property
    def staging_key(self):
        return self._entry().get("staging_key")

    @property
    def multipart_upload_id(self):
        return self._entry().get("multipart_upload_id")

    def _entry(self):
        """Return the OOBTree entry for this upload, or empty mapping."""
        uploads = _container_uploads(self.container)
        if uploads is None:
            return {}
        return uploads.get(self.uid) or {}

    def _save_entry(self, entry):
        uploads = _container_uploads(self.container, create=True)
        uploads[self.uid] = entry

    def remember_chunk_size(self, chunk_size):
        """Persist chunk_size on the upload entry if it isn't set yet.

        Called from the authorize endpoint so that when the data-plane
        offload is on, the first chunk's size is recorded even though
        the PATCH body bypasses Plone. Without this, the first PATCH
        that ever reaches Plone (typically the final chunk, which is
        smaller) would "learn" the wrong chunk_size from its own
        content_length and compute a bogus part_number — leading to
        S3 InvalidArgument on UploadPart.

        One write per upload: gated on chunk_size being None, so every
        chunk after the first finds it already set and does nothing.
        """
        entry = self._entry()
        if not entry:
            return
        if entry.get("chunk_size") is None:
            entry["chunk_size"] = int(chunk_size)
            entry["last_active"] = time.time()
            self._save_entry(entry)
            self._metadata = entry

    def _initialize(self, metadata):
        """Create a fresh multipart upload and persist the descriptor."""
        self.cleanup_expired()
        staging_key = f"{TUS_STAGING_PREFIX}{self.uid}"
        upload_id = self._s3_client.create_multipart_upload(staging_key)
        now = time.time()
        entry = PersistentMapping(
            {
                # Upload descriptor (immutable once written, except chunk_size
                # which is learned from the first PATCH).
                "length": int(metadata.get("length", 0)),
                "filename": metadata.get("filename", ""),
                "content-type": metadata.get(
                    "content-type", "application/octet-stream"
                ),
                "@type": metadata.get("@type"),
                "fieldname": metadata.get("fieldname"),
                "mode": metadata.get("mode", "create"),
                "chunk_size": None,
                # S3 state.
                "staging_key": staging_key,
                "multipart_upload_id": upload_id,
                # Liveness.
                "created": now,
                "last_active": now,
            }
        )
        self._save_entry(entry)
        self._metadata = entry
        logger.info(
            "TUS/S3: created upload uid=%s staging_key=%s upload_id=%s "
            "container=%s length=%d",
            self.uid,
            staging_key,
            upload_id,
            "/".join(self.container.getPhysicalPath()),
            entry["length"],
        )

    def metadata(self):
        """Return the descriptor as a dict (compatible with TUSUpload.metadata)."""
        if self._metadata is None:
            self._metadata = self._entry()
        # The deserializer reads keys like 'content-type', 'filename',
        # '@type', 'mode' off this — they're stored under the same names.
        return dict(self._metadata) if self._metadata else {}

    def length(self):
        return int(self._entry().get("length", 0))

    def offset(self):
        """TUS offset = highest contiguous prefix of part numbers × chunk_size.

        Sparse parts beyond the first gap are ignored — the client will
        re-PATCH from there and S3 will overwrite by part number.

        If the upload has been completed in this request, the multipart
        upload no longer exists in S3 and ``list_parts`` would raise
        ``NoSuchUpload``. Short-circuit to ``length`` in that case — by
        definition a completed upload's offset equals its length.
        """
        if self.finished:
            return self.length()
        entry = self._entry()
        chunk_size = entry.get("chunk_size")
        if not chunk_size:
            return 0
        parts = self._s3_client.list_parts(
            entry["staging_key"], entry["multipart_upload_id"]
        )
        # Highest contiguous run starting from PartNumber=1.
        contiguous = 0
        total = 0
        for part in parts:
            if part["PartNumber"] != contiguous + 1:
                break
            contiguous += 1
            total += part["Size"]
        return total

    def expires(self):
        last_active = self._entry().get("last_active") or time.time()
        return formatdate(last_active + self.expiration_period, False, True)

    def write(self, infile, offset=0, content_length=None):
        """Stream one chunk straight to S3 as a single multipart part."""
        entry = self._entry()
        if not entry:
            raise TUSUploadError(
                f"Upload {self.uid} not found in container annotation",
                error_type="Not Found",
                status=404,
            )

        length = int(entry["length"])
        chunk_size = entry.get("chunk_size")

        if content_length is None:
            raise TUSUploadError("Missing or invalid Content-Length header for PATCH")

        is_final = (offset + content_length) >= length
        if chunk_size is None:
            # Learn chunk_size from the first PATCH.
            if not is_final and content_length < 5 * 1024 * 1024:
                raise TUSUploadError(
                    "Non-final chunk must be at least 5 MiB (S3 multipart "
                    "minimum). Configure the client to use a larger chunkSize."
                )
            chunk_size = content_length
        else:
            if offset % chunk_size != 0:
                raise TUSUploadError(
                    f"Upload-Offset {offset} is not a multiple of "
                    f"chunk_size {chunk_size}"
                )
            if not is_final and content_length != chunk_size:
                raise TUSUploadError(
                    f"Non-final chunk size {content_length} does not match "
                    f"established chunk_size {chunk_size}"
                )

        part_number = (offset // chunk_size) + 1
        # Read exactly content_length bytes into a seekable buffer. boto3's
        # SigV4 signer needs to either rewind the body (to compute SHA256
        # then re-read for upload) or hash a bytes-like payload directly;
        # a streaming reader without seek/tell falls between those paths
        # and triggers `TypeError: object supporting the buffer API required`.
        # The request body is already buffered in Zope's tempfile so this
        # doesn't add real memory pressure beyond the chunk itself.
        data = infile.read(content_length)
        if len(data) != content_length:
            raise TUSUploadError(
                f"Short read on PATCH body: expected {content_length} bytes, "
                f"got {len(data)}"
            )
        body = BytesIO(data)
        etag = self._s3_client.upload_part(
            entry["staging_key"],
            entry["multipart_upload_id"],
            part_number,
            body,
        )
        logger.info(
            "TUS/S3: uploaded part uid=%s part=%d size=%d offset=%d etag=%s",
            self.uid,
            part_number,
            content_length,
            offset,
            etag,
        )

        # Update annotation: chunk_size on first PATCH; last_active always.
        entry["last_active"] = time.time()
        if entry.get("chunk_size") is None:
            entry["chunk_size"] = chunk_size
        self._save_entry(entry)
        self._metadata = entry

        if is_final:
            self._complete()

    def _complete(self):
        """Verify all parts are present, call CompleteMultipartUpload."""
        entry = self._entry()
        parts = self._s3_client.list_parts(
            entry["staging_key"], entry["multipart_upload_id"]
        )
        chunk_size = entry["chunk_size"]
        length = entry["length"]
        # Expected part count: ceil(length / chunk_size).
        expected = (length + chunk_size - 1) // chunk_size
        actual_numbers = {p["PartNumber"] for p in parts}
        expected_numbers = set(range(1, expected + 1))
        missing = sorted(expected_numbers - actual_numbers)
        if missing:
            logger.error(
                "TUS/S3: cannot complete upload uid=%s — missing parts %s "
                "(expected=%d, got=%d)",
                self.uid,
                missing,
                expected,
                len(actual_numbers),
            )
            raise TUSUploadError(
                f"Cannot complete upload: missing parts {missing}",
                error_type="Conflict",
                status=409,
            )
        try:
            self._s3_client.complete_multipart_upload(
                entry["staging_key"],
                entry["multipart_upload_id"],
                parts,
            )
        except Exception:
            logger.exception(
                "TUS/S3: complete_multipart_upload failed uid=%s "
                "staging_key=%s parts=%d",
                self.uid,
                entry["staging_key"],
                len(parts),
            )
            raise
        logger.info(
            "TUS/S3: completed multipart upload uid=%s parts=%d total_bytes=%d "
            "staging_key=%s",
            self.uid,
            len(parts),
            length,
            entry["staging_key"],
        )
        self.finished = True

    def open(self):
        """Not used in S3 mode (deserializer reads via process_blob marker)."""
        raise NotImplementedError(
            "S3TUSUpload does not expose the uploaded bytes locally"
        )

    def close(self):
        """No-op — there is no local file handle in S3 mode."""

    def cleanup(self):
        """Drop annotation entry; abort multipart if upload was abandoned."""
        entry = self._entry()
        if entry:
            if (
                not self.finished
                and entry.get("multipart_upload_id")
                and self._s3_client is not None
            ):
                try:
                    self._s3_client.abort_multipart_upload(
                        entry["staging_key"], entry["multipart_upload_id"]
                    )
                except Exception:
                    logger.warning(
                        "TUS/S3: failed to abort multipart for upload %s",
                        self.uid,
                        exc_info=True,
                    )
            if self.finished and not self._handoff_done and self._s3_client is not None:
                # Completed S3 upload but handoff to ZODB blob never ran.
                # Leave the orphan for the lifecycle rule to clean up rather
                # than risk deleting a key the storage might end up
                # referencing.
                logger.warning(
                    "TUS/S3: upload uid=%s completed but handoff was not "
                    "performed; staging_key=%s left for lifecycle cleanup",
                    self.uid,
                    entry.get("staging_key"),
                )
            uploads = _container_uploads(self.container)
            if uploads is not None and self.uid in uploads:
                del uploads[self.uid]

    def cleanup_expired(self):
        """Remove annotation entries (and S3 multiparts) older than the period.

        Scoped to this container's annotation only — cheap and targeted, runs
        at upload-create time.
        """
        uploads = _container_uploads(self.container)
        if uploads is None:
            return
        cutoff = time.time() - self.expiration_period
        expired = [
            uid
            for uid, entry in uploads.items()
            if (entry.get("last_active") or entry.get("created") or 0) < cutoff
        ]
        for uid in expired:
            entry = uploads[uid]
            if self._s3_client is not None and entry.get("multipart_upload_id"):
                try:
                    self._s3_client.abort_multipart_upload(
                        entry["staging_key"], entry["multipart_upload_id"]
                    )
                except Exception:
                    logger.debug(
                        "TUS/S3: expired-upload abort failed (key=%s)",
                        entry.get("staging_key"),
                        exc_info=True,
                    )
            del uploads[uid]

    def process_blob(self, blob):
        """Hand the staging key to the S3 storage as the blob's content.

        Writes zodb-s3blobs' staged-upload marker into
        ``_p_blob_uncommitted``. At commit the storage recognises the marker
        by its content and copies the staging key to the blob's key
        server-side, instead of uploading the file.
        """
        from zodb_s3blobs.storage import write_staged_marker

        if self._s3_storage is None:
            raise RuntimeError("S3TUSUpload missing storage reference at handoff")
        size = self.length()
        entry = self._entry()
        staging_key = entry.get("staging_key")
        # Use a uniquely-named tempfile so two concurrent uploads in the same
        # process never race on the marker source path.
        fd, marker_source = mkstemp(prefix="tus-marker-", suffix=".bin")
        os.close(fd)
        try:
            write_staged_marker(marker_source, staging_key, size)
            target_path = blob._p_blob_uncommitted
            rename_or_copy_blob(marker_source, target_path)
        finally:
            if os.path.exists(marker_source):
                os.remove(marker_source)
        self._handoff_done = True
        logger.info(
            "TUS/S3: handoff complete uid=%s staging_key=%s size=%d "
            "marker_path=%s storage_id=%s",
            self.uid,
            staging_key,
            size,
            target_path,
            id(self._s3_storage),
        )
