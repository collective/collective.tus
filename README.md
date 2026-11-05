# collective.tus

TUS resumable uploads for [plone.restapi](https://github.com/plone/plone.restapi), with two changes to its own TUS support, and optional direct-to-S3 uploads:

- **The finished upload is moved into the blob instead of copied.** plone.restapi reads the uploaded file and writes it into a new blob, so every large upload is written to disk twice and the request holds the copy. Here the blob is created with a one-byte placeholder and the uploaded file is renamed into it after validation. If the upload directory and the blob directory are on different file systems, the file is copied once instead.
- **Termination.** `DELETE @tus-upload/<uid>` cancels an upload and removes what was uploaded so far ([TUS termination extension](https://tus.io/protocols/resumable-upload#termination)). `OPTIONS` advertises `creation,expiration,termination`.

It also cleans up expired uploads safely when several worker processes share the upload directory.

The endpoints, headers and request flow are the same as plone.restapi's, so clients don't change. See [plone.restapi's TUS documentation](https://6.docs.plone.org/plone.restapi/docs/source/endpoints/tusupload.html).

## Installation

Add `collective.tus` to your dependencies and install its profile (**collective.tus** in the add-ons control panel, or the GenericSetup profile `collective.tus:default`) on each site.

The add-on registers its services and its file field deserializer for its own browser layer. They take over from plone.restapi's once the profile is installed on a site; without it, plone.restapi's own TUS support is used. No monkeypatching.

## Direct-to-S3 uploads

On a site whose ZODB storage is [zodb-s3blobs](https://github.com/bluedynamics/zodb-s3blobs)' `S3BlobStorage`, install `collective.tus[s3]`. Uploads then go to an S3 multipart upload under `tus-staging/<uid>` instead of local disk:

- Each PATCH is uploaded as one part. Every chunk but the last must be the same size and at least 5 MiB (the S3 minimum), so configure the client's chunk size accordingly.
- The upload's state is kept in an annotation on its container, so any worker process can take any chunk: no sticky sessions or shared disk.
- When the last chunk arrives, the multipart upload is completed and the new file's blob gets zodb-s3blobs' staged-upload marker instead of the bytes. At commit, the storage copies the staging object to the blob's own key within S3.
- Abandoned multipart uploads are aborted when they expire, and a bucket lifecycle rule (`tus-staging-abort`, 7 days) is added on the first upload as a backstop.

This needs a zodb-s3blobs with `write_staged_marker()` and staged-key commits, which isn't released yet.

### Sending chunks straight to S3

Optionally, the chunks can bypass Zope altogether. With `TUS_S3_OFFLOAD_ENABLED=true`, nginx asks `GET @tus-authorize/<uid>` (with nginx's `auth_request`) how to route each PATCH:

- `X-Route: s3` with `X-S3-Url`, a presigned UploadPart URL (valid 5 minutes), and `X-Tus-New-Offset`: nginx PUTs the body to S3 and replies with that offset.
- `X-Route: plone`: nginx passes the PATCH to Plone. This is always the case for the last chunk, so Plone can complete the upload and save the content in one transaction.

`@tus-authorize` checks the same permissions as `@tus-upload`. Without the environment variable it always answers `X-Route: plone`, so it can be deployed before the nginx configuration and works as a kill switch.

A sketch of the nginx configuration (nginx 1.27.1 or later, for a variable `proxy_method`):

```nginx
# http scope: choose the upstream from the authorize response.
map $tus_route $tus_upstream { "s3" $tus_s3_url; default http://plone; }
map $tus_route $tus_method   { "s3" PUT;         default $request_method; }
map $tus_route $tus_authz    { "s3" "";          default $http_authorization; }
map $tus_route $tus_cookie   { "s3" "";          default $http_cookie; }
map $tus_route $tus_host     { "s3" $proxy_host; default $host; }

# server scope
location ~ /@tus-upload/[a-f0-9]+$ {
    error_page 418 = @tus_patch;
    if ($request_method = PATCH) { return 418; }
    proxy_pass http://plone;
}

location @tus_patch {
    client_max_body_size 0;
    proxy_request_buffering off;
    resolver 1.1.1.1;  # S3 is reached by name from a variable

    auth_request /_tus_authorize;
    auth_request_set $tus_route $upstream_http_x_route;
    auth_request_set $tus_s3_url $upstream_http_x_s3_url;
    auth_request_set $tus_new_offset $upstream_http_x_tus_new_offset;

    proxy_method $tus_method;
    proxy_pass $tus_upstream;
    proxy_set_header Host $tus_host;
    proxy_set_header Authorization $tus_authz;
    proxy_set_header Cookie $tus_cookie;

    proxy_hide_header Upload-Offset;
    proxy_hide_header Tus-Resumable;
    add_header Upload-Offset $tus_new_offset always;
    add_header Tus-Resumable "1.0.0" always;
}

location = /_tus_authorize {
    internal;
    client_max_body_size 0;
    set $authorize_uri $request_uri;
    if ($request_uri ~ "^(.*)/@tus-upload/([a-f0-9]+)(\?.*)?$") {
        set $authorize_uri "$1/@tus-authorize/$2$3";
    }
    proxy_pass http://plone$authorize_uri;
    proxy_method GET;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header X-Original-Upload-Offset $http_upload_offset;
    proxy_set_header X-Original-Content-Length $http_content_length;
}
```

Use `if` only to dispatch PATCH as above: the routing must come from the `map`s, because `if` runs before the authorize subrequest has set `$tus_route`.

## Settings

The same as plone.restapi's:

| Environment variable | Default | Meaning |
|---|---|---|
| `TUS_TMP_FILE_DIR` | `$CLIENT_HOME/tus-uploads` | where uploads are kept until they finish. Put it on the same file system as the blob storage so the move is a rename |
| `TUS_S3_OFFLOAD_ENABLED` | off | S3 mode: let `@tus-authorize` send chunks straight to S3 (`true`, `1`, `yes` or `on`) |

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
