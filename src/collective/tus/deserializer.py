from collective.tus.interfaces import ICollectiveTusLayer
from collective.tus.upload import TUSUpload
from plone.dexterity.interfaces import IDexterityContent
from plone.namedfile.interfaces import INamedBlobFile
from plone.namedfile.interfaces import INamedField
from plone.restapi.deserializer.dxfields import (
    NamedFieldDeserializer as BaseNamedFieldDeserializer,
)
from plone.restapi.interfaces import IFieldDeserializer
from zope.component import adapter
from zope.interface import implementer

# A blob file can't be created empty, so it starts with this one byte and the
# upload replaces its file.
PLACEHOLDER = b"0"


@implementer(IFieldDeserializer)
@adapter(INamedField, IDexterityContent, ICollectiveTusLayer)
class NamedFieldDeserializer(BaseNamedFieldDeserializer):
    """File field deserializer that moves a finished TUS upload into the blob.

    plone.restapi reads the upload and writes it into a new blob, so a large
    file is copied in full. Here the blob is created with a placeholder and
    the upload's file is moved into it after validation.

    Anything else, including a non-blob named field, is handled by
    plone.restapi.
    """

    def __call__(self, value):
        if not isinstance(value, TUSUpload):
            return super().__call__(value)
        if not INamedBlobFile.implementedBy(self.field._type):
            return super().__call__(value)

        metadata = value.metadata()
        result = self.field._type(
            data=PLACEHOLDER,
            contentType=metadata.get("content-type", "application/octet-stream"),
            filename=metadata.get("filename"),
        )

        # Always validate to check for required fields.
        self.field.validate(result)

        value.process_blob(result._blob)
        # ``size`` may have been cached from the placeholder during
        # validation. Set it to the upload's length, which is also right when
        # the blob file is not the uploaded bytes themselves.
        result.__dict__["size"] = value.length()
        return result
