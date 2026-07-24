"""Utility functions for unzipping and path handling."""

from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path
from rich.console import Console
from .config import LANG_SUFFIX_RE

console = Console()

def strip_lang_suffix(basename: str) -> str:
    """Strip lang suffix, e.g. 'OneIM_en-us.html5' -> 'OneIM'."""
    return LANG_SUFFIX_RE.sub("", basename)

def manual_basename_from_zip(zip_path: Path) -> str:
    """Extract and normalize manual name from zip path."""
    stem = zip_path.name
    if stem.endswith(".zip"):
        stem = stem[:-4]
    
    # Strip lang and/or .html5 suffix if present
    stripped = strip_lang_suffix(stem)
    if stripped != stem:
        return stripped
    if stem.endswith(".html5"):
        return stem[:-6]
    return stem

def sanitize_filename(name: str) -> str:
    """Conservative filename sanitizer: keep alphanumeric, dot, dash, underscore."""
    out = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE)
    return out.strip("._") or "image"

def common_top_dir(names: list[str]) -> str | None:
    """Return single top-level directory if all files share one, else None."""
    tops: set[str] = set()
    for n in names:
        first = n.split("/", 1)[0]
        if not first:
            continue
        tops.add(first)
        if len(tops) > 1:
            return None
    return next(iter(tops)) if tops else None

def already_unzipped(target: Path) -> bool:
    """Return True if the target directory already contains a Flare Sources folder."""
    sources = target / "Sources"
    return sources.is_dir() and any(sources.iterdir())

def unzip_one(zip_path: Path, target: Path) -> int:
    """Unzip manual, flattening any common top-level wrapper folder."""
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        wrapper = common_top_dir(names)
        if wrapper is None:
            zf.extractall(target)
            return len(names)
        
        prefix = wrapper + "/"
        extracted_count = 0
        for info in zf.infolist():
            if not info.filename.startswith(prefix):
                continue
            stripped = info.filename[len(prefix):]
            if not stripped:
                continue
            dest = target / stripped
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                out.write(src.read())
            extracted_count += 1
        return extracted_count

def topic_id_for(html_path: Path, manual_root: Path) -> str:
    """Generate a stable 80-char alphanumeric slug and hash suffix for a topic path."""
    rel = html_path.relative_to(manual_root).as_posix()
    digest = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:8]
    slug = re.sub(r"\W+", "-", rel.lower()).strip("-")[:80]
    return f"{slug}-{digest}"

def is_shell_path(path: Path, manual_root: Path) -> bool:
    """Detect if path is a runtime frame launcher or shell helper."""
    name = path.name.lower()
    if name.endswith("_csh.html") or name.endswith("_csh.htm"):
        return True
    if path.parent.resolve() == manual_root.resolve():
        root_name = manual_root.name.lower()
        if path.stem.lower() == root_name:
            return True
    return False


