"""Uploaded files, handed to the browser by the app rather than by the bucket.

Every upload -- a school's logo, a parent's bank slip, an applicant's birth
certificate -- is reached through a view, never through the storage URL. The
bucket is private (``MEDIA_PRIVATE_BUCKET=true``), so a storage URL is either a
signed link that works for anyone holding it until it expires, or nothing at
all; a view is a link that only opens for whoever the view lets in, checked on
every request. It also means nothing depends on how a particular S3-compatible
host signs URLs: the file is read with the app's own credentials and streamed.

The view decides *who*; this module decides *how*, once, so the three of them
cannot drift apart on the headers that matter.
"""

from __future__ import annotations

import mimetypes
import posixpath

from django.http import FileResponse, Http404

#: What a browser may render in place. Anything else is sent as a download, so a
#: file that arrived with a misleading extension is never interpreted as a page.
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/heic"})
DOCUMENT_TYPES = IMAGE_TYPES | {"application/pdf"}

#: Even an inline type is rendered under a sandbox with nothing allowed to load.
#: It is what makes an SVG logo safe to serve from our own origin: inside an
#: ``<img>`` an SVG cannot run script anyway, but opened directly it is a document,
#: and without this its script would run as us.
_SANDBOX = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"


def stream_stored_file(field_file, *, inline_types, cache_control: str) -> FileResponse:
    """Stream ``field_file`` from whichever storage holds it, or 404.

    ``inline_types`` is the set of content types shown in the browser rather
    than downloaded; ``cache_control`` is the caller's call, because a logo and
    a bank slip are not cached the same way.
    """
    if not field_file:
        raise Http404("No file is attached.")
    try:
        handle = field_file.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404("The attached file could not be found.")

    filename = posixpath.basename(field_file.name)
    content_type, _ = mimetypes.guess_type(filename)
    if content_type not in inline_types:
        content_type = "application/octet-stream"
    response = FileResponse(
        handle,
        content_type=content_type,
        as_attachment=content_type == "application/octet-stream",
        filename=filename,
    )
    response["Cache-Control"] = cache_control
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = _SANDBOX
    return response
