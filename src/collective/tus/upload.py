from fnmatch import fnmatch
from plone.restapi.services.content import tus as restapi_tus
from ZODB.blob import rename_or_copy_blob

import os
import time


class TUSUploadError(Exception):
    """Raised by an upload's ``write()`` when a chunk can't be accepted.

    The PATCH service turns it into a TUS error response.
    """

    def __init__(self, message, error_type="Bad Request", status=400):
        super().__init__(message)
        self.error_type = error_type
        self.status = status


class TUSUpload(restapi_tus.TUSUpload):
    """An upload kept on local disk, in ``TUS_TMP_FILE_DIR`` or
    ``CLIENT_HOME/tus-uploads``.

    The finished file is moved into the content's blob by ``process_blob``
    instead of being copied, so a large upload isn't written to disk twice.
    """

    def initialize(self, metadata):
        super().initialize(metadata)
        self._metadata = metadata

    def write(self, infile, offset=0, content_length=None):
        """Write to the uploaded file at the given offset.

        ``content_length`` is the PATCH request's Content-Length. Local-disk
        uploads read the body until EOF and don't need it, but the argument
        lets other upload classes use it.
        """
        super().write(infile, offset)

    def cleanup_expired(self):
        """Remove unfinished uploads that have expired.

        Several worker processes can share the upload directory and clean it
        up at the same time, so a file may disappear between the listing and
        the removal.
        """
        now = time.time()
        for filename in os.listdir(self.tmp_dir):
            if not fnmatch(filename, f"{self.file_prefix}*.json"):
                continue
            metadata_path = os.path.join(self.tmp_dir, filename)
            filepath = metadata_path[:-5]
            mtime = None
            for candidate in (filepath, metadata_path):
                try:
                    mtime = os.stat(candidate).st_mtime
                    break
                except FileNotFoundError:
                    continue
            if mtime is None or (now - mtime) <= self.expiration_period:
                continue
            for path in (metadata_path, filepath):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

    def process_blob(self, blob):
        """Move the uploaded file into the blob's uncommitted file.

        If both are on the same file system this is a rename. Otherwise the
        file is copied and the upload removed.
        """
        rename_or_copy_blob(self.filepath, blob._p_blob_uncommitted)
