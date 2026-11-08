"""Create a small Calibre library for development. Run under calibre-debug:

    calibre-debug -e scripts/seed_library.py -- /path/to/library

Refuses to touch a directory that already holds a library. Books are generated
from a few lines of public-domain text with ebook-convert, so the result has
real EPUB and PDF files, covers and varied metadata.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from calibre.ebooks.metadata.book.base import Metadata
from calibre.library import db as open_library

BOOKS = [
    {
        "title": "Pride and Prejudice",
        "authors": ["Jane Austen"],
        "languages": ["eng"],
        "tags": ["Fiction", "Classics"],
        "publisher": "T. Egerton",
        "pubdate": datetime(1813, 1, 28, tzinfo=UTC),
        "identifiers": {"isbn": "9780141439518"},
        "comments": "<p>It is a truth universally acknowledged...</p>",
        "formats": ["EPUB", "PDF"],
    },
    {
        "title": "Marína",
        "authors": ["Andrej Sládkovič"],
        "languages": ["slk"],
        "tags": ["Poetry"],
        "pubdate": datetime(1846, 1, 1, tzinfo=UTC),
        "comments": "<p>Najdlhšia ľúbostná báseň.</p>",
        "formats": ["EPUB"],
    },
    {
        "title": "Frankenstein; or, The Modern Prometheus",
        "authors": ["Mary Shelley"],
        "languages": ["eng"],
        "tags": ["Fiction", "Horror"],
        "formats": ["EPUB"],
    },
    {
        "title": "A Book Without Language",
        "authors": ["Anonymous"],
        "tags": [],
        "formats": ["PDF"],
    },
    {
        "title": "Only an AZW3",
        "authors": ["Test Author", "Second Author"],
        "languages": ["eng"],
        "formats": ["AZW3"],
    },
]


def convert(text_path: Path, fmt: str, mi: Metadata, out_dir: Path) -> Path:
    target = out_dir / f"{text_path.stem}.{fmt.lower()}"
    ebook_convert = os.path.join(os.path.dirname(sys.executable), "ebook-convert")
    if not os.path.exists(ebook_convert):
        ebook_convert = "ebook-convert"
    subprocess.run(
        [ebook_convert, str(text_path), str(target), "--title", mi.title, "--authors", " & ".join(mi.authors)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return target


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    library = Path(sys.argv[-1]).resolve()
    if (library / "metadata.db").exists():
        print(f"{library} already has a library; run 'scripts/dev.sh reset' first to recreate it")
        return 0
    library.mkdir(parents=True, exist_ok=True)
    db = open_library(str(library)).new_api

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for index, spec in enumerate(BOOKS):
            mi = Metadata(spec["title"], spec["authors"])
            for key in ("languages", "tags", "publisher", "pubdate", "identifiers", "comments"):
                if key in spec:
                    setattr(mi, key, spec[key])
            source = work / f"book{index}.txt"
            source.write_text(f"{spec['title']}\n\nby {', '.join(spec['authors'])}\n\n" + "Lorem ipsum. " * 200)
            formats = {fmt: str(convert(source, fmt, mi, work)) for fmt in spec["formats"]}
            db.add_books([(mi, formats)])
            print(f"added {spec['title']} ({', '.join(formats)})")
    print(f"library ready: {library}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
