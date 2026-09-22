"""Extract slide text from a PPTX for protocol-document inspection."""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    pptx = Path(sys.argv[1])
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    end = int(sys.argv[3]) if len(sys.argv) > 3 else 10_000
    with zipfile.ZipFile(pptx) as archive:
        slides = [
            name
            for name in archive.namelist()
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
        ]
        slides.sort(key=lambda name: int(re.search(r"\d+", name).group()))
        for name in slides:
            number = int(re.search(r"\d+", name).group())
            if not start <= number <= end:
                continue
            root = ElementTree.fromstring(archive.read(name))
            texts = [node.text or "" for node in root.iter() if node.tag.endswith("}t")]
            print(f"--- slide {number} ---")
            print(" | ".join(texts))


if __name__ == "__main__":
    main()
