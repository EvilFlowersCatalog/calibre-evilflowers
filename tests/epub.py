"""Tiny valid EPUBs for tests. Deterministic: the same text always gives the same bytes."""

from __future__ import annotations

import io
import zipfile
from functools import cache

_CONTAINER = b"""<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{text}</dc:title><dc:identifier id="id">{text}</dc:identifier><dc:language>en</dc:language>
  </metadata>
  <manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="c"/></spine>
</package>"""

_XHTML = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{text}</title></head><body><p>{text}</p></body></html>"""


@cache
def epub(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data, method in (
            ("mimetype", b"application/epub+zip", zipfile.ZIP_STORED),
            ("META-INF/container.xml", _CONTAINER, zipfile.ZIP_DEFLATED),
            ("content.opf", _OPF.format(text=text).encode(), zipfile.ZIP_DEFLATED),
            ("c.xhtml", _XHTML.format(text=text).encode(), zipfile.ZIP_DEFLATED),
        ):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = method
            archive.writestr(info, data)
    return buffer.getvalue()
