"""MadCap Flare TOC (Table of Contents) parsing logic."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import pyjson5  # type: ignore

from .utils import topic_id_for

# define( ... );   or   define( ... )
DEFINE_RE = re.compile(r"^\s*define\s*\(\s*(.*?)\s*\)\s*;?\s*$", re.DOTALL)

@dataclass
class TopicEntry:
    """One TOC entry. ``path`` is empty for header-only group nodes."""
    depth: int
    title: str
    path: str  # relative to manual_root, e.g. "Sources/AAD/AADIntro.html"
    topic_id: str  # stable slug, or "" for header-only

def parse_define_js(text: str) -> Any:
    """Parse a MadCap AMD-style ``define({...})`` file."""
    m = DEFINE_RE.match(text)
    if not m:
        body = text.strip().rstrip(";").strip()
    else:
        body = m.group(1)
    return pyjson5.loads(body)

def find_toc_main(manual_root: Path) -> Path | None:
    """The main TOC is the *.js under Data/Tocs/ that has no _Chunk suffix."""
    tocs_dir = manual_root / "Data" / "Tocs"
    if not tocs_dir.is_dir():
        return None
    candidates = [
        p for p in tocs_dir.glob("*.js")
        if "_Chunk" not in p.name
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda p: len(p.name))

def load_chunks(main_toc_path: Path, prefix: str) -> list[dict]:
    """Load Chunk0.js, Chunk1.js, ... into a list of dicts."""
    tocs_dir = main_toc_path.parent
    chunks: list[dict] = []
    n = 0
    while True:
        chunk_path = tocs_dir / f"{prefix}{n}.js"
        if not chunk_path.is_file():
            break
        chunks.append(parse_define_js(chunk_path.read_text(encoding="utf-8")))
        n += 1
    return chunks

def build_index_to_entry(chunks: list[dict]) -> dict[int, dict]:
    """Walk every chunk, read the per-entry i[0], and build a flat map."""
    out: dict[int, dict] = {}
    for chunk_no, chunk in enumerate(chunks):
        for path, entry in chunk.items():
            idx_arr = entry.get("i") or []
            title_arr = entry.get("t") or []
            bookmark_arr = entry.get("b") or []
            if not idx_arr or not title_arr:
                continue
            idx = idx_arr[0]
            out[idx] = {
                "path": path.lstrip("/"),
                "title": title_arr[0],
                "bookmark": (bookmark_arr[0] if bookmark_arr else "") or "",
                "chunk_no": chunk_no,
            }
    return out

def _looks_like_topic_path(path: str) -> bool:
    """Filter out MadCap placeholder paths like ``___`` from real topic refs."""
    if not path:
        return False
    p = Path(path)
    return "/" in path and "." in p.name

def _walk_with_groups(
    nodes: list[dict],
    index_to_entry: dict[int, dict],
    depth: int,
    accum: list[TopicEntry],
    manual_root: Path,
) -> None:
    """Walk TOC nodes and build flat TopicEntry objects including group nodes."""
    for node in nodes:
        idx = node.get("i")
        entry = index_to_entry.get(idx) if idx is not None else None
        if entry is None:
            # Group node — try to extract title from 't' or 'l'
            title = (node.get("t") or node.get("l") or "").strip()
            if title:
                accum.append(TopicEntry(depth=depth, title=title, path="", topic_id=""))
                next_depth = depth + 1
            else:
                next_depth = depth
            for child in node.get("n", []) or []:
                _walk_with_groups([child], index_to_entry, next_depth, accum, manual_root)
            continue
        rel_path = entry["path"]
        topic_id = topic_id_for(manual_root / rel_path, manual_root)
        accum.append(TopicEntry(
            depth=depth, title=entry["title"], path=rel_path, topic_id=topic_id
        ))
        for child in node.get("n", []) or []:
            _walk_with_groups([child], index_to_entry, depth + 1, accum, manual_root)

def load_toc(manual_root: Path) -> tuple[list[TopicEntry], str | None]:
    """Parse Flare TOC. Returns (entries, wrapper_title)."""
    main_toc = find_toc_main(manual_root)
    if main_toc is None:
        return [], None
    main = parse_define_js(main_toc.read_text(encoding="utf-8"))
    if not isinstance(main, dict):
        return [], None
    prefix = main.get("prefix")
    tree = main.get("tree") or {}
    nodes = tree.get("n") or []
    if not prefix or not nodes:
        return [], None
    chunks = load_chunks(main_toc, prefix)
    index_to_entry = build_index_to_entry(chunks)

    raw: list[TopicEntry] = []
    _walk_with_groups(nodes, index_to_entry, depth=0, accum=raw, manual_root=manual_root)

    # Normalize placeholder paths to empty
    for e in raw:
        if e.path and not _looks_like_topic_path(e.path):
            e.path = ""
            e.topic_id = ""

    # Detect single-wrapper TOC
    top = [e for e in raw if e.depth == 0]
    wrapper_title: str | None = None
    if len(top) == 1 and not top[0].path and any(e.depth > 0 for e in raw):
        wrapper_title = top[0].title
        shifted = [
            TopicEntry(depth=e.depth - 1, title=e.title, path=e.path, topic_id=e.topic_id)
            for e in raw if e is not top[0]
        ]
        return shifted, wrapper_title
    return raw, wrapper_title

def find_topic_file(manual_root: Path, rel_path: str) -> Path | None:
    """Resolve TOC ``rel_path`` to the actual .htm or .html file on disk."""
    candidate = manual_root / rel_path
    if candidate.is_file():
        return candidate
    if candidate.suffix.lower() == ".html":
        alt = candidate.with_suffix(".htm")
        if alt.is_file():
            return alt
    elif candidate.suffix.lower() == ".htm":
        alt = candidate.with_suffix(".html")
        if alt.is_file():
            return alt
    return None

