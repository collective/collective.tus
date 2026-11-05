# collective.tus

TUS resumable uploads for [plone.restapi](https://github.com/plone/plone.restapi), with two changes to its own TUS support:

- **The finished upload is moved into the blob instead of copied.** plone.restapi reads the uploaded file and writes it into a new blob, so every large upload is written to disk twice and the request holds the copy. Here the blob is created with a one-byte placeholder and the uploaded file is renamed into it after validation. If the upload directory and the blob directory are on different file systems, the file is copied once instead.
- **Termination.** `DELETE @tus-upload/<uid>` cancels an upload and removes what was uploaded so far ([TUS termination extension](https://tus.io/protocols/resumable-upload#termination)). `OPTIONS` advertises `creation,expiration,termination`.

It also cleans up expired uploads safely when several worker processes share the upload directory.

The endpoints, headers and request flow are the same as plone.restapi's, so clients don't change. See [plone.restapi's TUS documentation](https://6.docs.plone.org/plone.restapi/docs/source/endpoints/tusupload.html).

## Installation

Add `collective.tus` to your dependencies and install its profile (**collective.tus** in the add-ons control panel, or the GenericSetup profile `collective.tus:default`) on each site.

The add-on registers its services and its file field deserializer for its own browser layer. They take over from plone.restapi's once the profile is installed on a site; without it, plone.restapi's own TUS support is used. No monkeypatching.

## Settings

The same as plone.restapi's:

| Environment variable | Default | Meaning |
|---|---|---|
| `TUS_TMP_FILE_DIR` | `$CLIENT_HOME/tus-uploads` | where uploads are kept until they finish. Put it on the same file system as the blob storage so the move is a rename |

Unfinished uploads expire after an hour of inactivity and are removed when a later upload starts.

## Differences from plone.restapi

- Only blob fields (`NamedBlobFile`, `NamedBlobImage`) get the upload moved in. Other file fields are handled by plone.restapi.
- An image uploaded with TUS isn't rotated according to its EXIF orientation, because its data isn't read when it's stored. Its width and height are read from the stored file when first needed.

## Compatibility

Plone 6.0 and 6.1, with plone.restapi 9.15 or later, including 10.x. CI tests plone.restapi 9.15.7 on Plone 6.0, and 9.15.3 and 10.2.0 on Plone 6.1. The add-on subclasses plone.restapi's TUS classes, which are internal and could change in a minor release, so CI runs plone.restapi's own TUS tests against it for each supported version.

## Development

```sh
make install
make test
```

## License

GPL-2.0-or-later. The tests in `tests/test_restapi_tus.py` are adapted from plone.restapi's.
