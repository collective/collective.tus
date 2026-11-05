# Changelog

## 1.0.0a1 (unreleased)

- Initial release: plone.restapi's TUS services with the finished upload moved into the blob instead of copied, upload termination (`DELETE`), and expired-upload cleanup that is safe with several worker processes. [instification]
- Direct-to-S3 uploads for sites on zodb-s3blobs (`collective.tus[s3]`), with an optional `@tus-authorize` endpoint so nginx can send chunks straight to S3. [instification]
