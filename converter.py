"""MadCap Flare HTML5 to Markdown converter logic."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import httpx
from rich.console import Console
from selectolax.parser import HTMLParser, Node
from markdownify import MarkdownConverter

from .config import (
    BODY_SELECTOR,
    STRIP_SELECTORS,
    IMAGE_EXTS,
    VLM_SKIP_EXTS,
    VLM_PROMPT,
    OPENROUTER_URL,
    DEFAULT_VLM_MODEL,
    XML_DECL_RE,
    LITERAL_PLACEHOLDER_RE,
)
from .utils import (
    manual_basename_from_zip,
    sanitize_filename,
    unzip_one,
    is_shell_path,
)
from .toc import TopicEntry, load_toc, find_topic_file

console = Console()

# Anchors with these name patterns are link-target stubs to drop from headings.
ANCHOR_NAME_RE = re.compile(r"^([0-9a-fA-F-]{36}|kanchor\d+)$")

# Table placeholder sentinels
TABLE_PLACEHOLDER_FMT = "MADCAPTABLEPLACEHOLDERXX{i}XX"
TABLE_PLACEHOLDER_RE = re.compile(r"MADCAPTABLEPLACEHOLDERXX(\d+)XX")

@dataclass
class ManualReport:
    version: str | None
    module: str | None
    manual_name: str
    output_path: Path
    topic_count: int = 0
    topic_failures: int = 0
    image_count: int = 0
    image_failures: int = 0
    approx_tokens: int = 0
    file_size: int = 0
    status: str = "ok"
    error: str | None = None

    @property
    def topic_failure_pct(self) -> float:
        total = self.topic_count + self.topic_failures
        return (self.topic_failures / total * 100.0) if total else 0.0

    @property
    def image_failure_pct(self) -> float:
        total = self.image_count + self.image_failures
        return (self.image_failures / total * 100.0) if total else 0.0

@dataclass
class ImageRef:
    """One image reference in a topic body."""
    original_abs: Path
    dest_rel: str  # e.g. "./<dirname>/img_001_foo.png"
    alt: str
    sha256: str = ""
    skip_vlm: bool = False

@dataclass
class ImageProcessingState:
    """Per-manual mutable state passed across topic conversions."""
    counter: int = 0
    dest_dir: Path = field(default_factory=Path)
    dest_dir_basename: str = ""
    refs: list[ImageRef] = field(default_factory=list)
    by_original: dict[Path, ImageRef] = field(default_factory=dict)
    used_dest_names: dict[str, Path] = field(default_factory=dict)
    failures: int = 0

class VLMClient:
    """Synchronous OpenRouter client for captioning screenshots."""
    def __init__(self, api_key: str, model: str, cache_dir: Path, concurrency: int, prompt: str | None = None):
        self.api_key = api_key
        self.model = model or DEFAULT_VLM_MODEL
        self.cache_dir = cache_dir
        self.concurrency = max(1, concurrency)
        self.prompt = prompt or VLM_PROMPT
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._http = httpx.Client(timeout=120.0)

    def close(self) -> None:
        self._http.close()

    def cache_path(self, sha: str) -> Path:
        return self.cache_dir / f"{sha}.txt"

    def get_cached(self, sha: str) -> str | None:
        p = self.cache_path(sha)
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8").strip() or None
            except OSError:
                return None
        return None

    def _call(self, image_path: Path, ext: str) -> str:
        b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }.get(ext, "application/octet-stream")
        payload = {
            "model": self.model,
            "max_tokens": 250,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": self.prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        for attempt in range(2):
            r = self._http.post(OPENROUTER_URL, headers=headers, json=payload)
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == 0:
                    time.sleep(2.0)
                    continue
                r.raise_for_status()
            r.raise_for_status()
            data = r.json()
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message", {})
            content = msg.get("content")
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            return (content or "").strip()
        return ""

    def caption_many(self, refs: list[ImageRef], progress_cb=None) -> tuple[dict[str, str], int]:
        out: dict[str, str] = {}
        failures = 0
        todo: list[ImageRef] = []
        seen: set[str] = set()
        for ref in refs:
            if not ref.sha256 or ref.skip_vlm:
                continue
            cached = self.get_cached(ref.sha256)
            if cached:
                out[ref.sha256] = cached
            elif ref.sha256 not in seen:
                seen.add(ref.sha256)
                todo.append(ref)

        if not todo:
            return out, failures

        def _do(ref: ImageRef) -> tuple[str, str | None]:
            try:
                cap = self._call(ref.original_abs, ref.original_abs.suffix.lower())
                if cap:
                    self.cache_path(ref.sha256).write_text(cap, encoding="utf-8")
                return ref.sha256, cap or None
            except Exception as e:
                console.print(f"  [yellow]vlm fail[/] {ref.original_abs.name}: {e}", highlight=False)
                return ref.sha256, None

        with ThreadPoolExecutor(max_workers=self.concurrency) as ex:
            futures = [ex.submit(_do, ref) for ref in todo]
            for fut in as_completed(futures):
                sha, cap = fut.result()
                if cap is None:
                    failures += 1
                else:
                    out[sha] = cap
                if progress_cb:
                    progress_cb(1)
        return out, failures

def _classes(el) -> list[str]:
    raw = el.get("class") if hasattr(el, "get") else None
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(c) for c in raw]
    return str(raw).split()

class FlareMarkdownConverter(MarkdownConverter):
    """markdownify converter tuned for MadCap Flare quirks."""
    def convert_pre(self, el, text, parent_tags):
        lines = text.replace("\u00a0", " ").strip("\r\n").split("\n")
        non_empty = [l for l in lines if l.strip()]
        if non_empty:
            min_indent = min(len(l) - len(l.lstrip(" \t")) for l in non_empty)
            lines = [l[min_indent:].rstrip() if l.strip() else "" for l in lines]
        cleaned = "\n".join(lines)
        return f"\n```\n{cleaned}\n```\n\n"

    def convert_p(self, el, text, parent_tags):
        classes = _classes(el)
        if "Code" in classes:
            stripped = text.strip("\n").rstrip()
            return f"\n```\n{stripped}\n```\n\n"
        if "Procedure" in classes:
            return f"\n**{text.strip()}**\n\n"
        return super().convert_p(el, text, parent_tags)

    def convert_span(self, el, text, parent_tags):
        classes = _classes(el)
        if "Code" in classes:
            return f"`{text}`" if text.strip() else ""
        return text

    def convert_b(self, el, text, parent_tags):
        classes = _classes(el)
        if "Value" in classes:
            return f"`{text}`" if text.strip() else ""
        return super().convert_b(el, text, parent_tags)

    def convert_a(self, el, text, parent_tags):
        if not text.strip() and not el.get("href"):
            return ""
        return super().convert_a(el, text, parent_tags)

def _merge_adjacent_code_fences(md: str) -> str:
    pattern = re.compile(r"```\s*\n([^`]*)\n\s*```\s*\n+\s*```\s*\n")
    while True:
        new_md = pattern.sub(r"```\n\1\n", md)
        if new_md == md:
            break
        md = new_md
    return re.sub(r"\n{3,}", "\n\n", md)

def _strip_anchor_stubs(body: Node) -> None:
    for a in body.css("a[name]"):
        name = a.attributes.get("name") or ""
        if ANCHOR_NAME_RE.match(name) and not (a.text() or "").strip():
            a.decompose()

def preclean_html_text(text: str) -> str:
    text = XML_DECL_RE.sub("", text)
    text = LITERAL_PLACEHOLDER_RE.sub(lambda m: f"&lt;{m.group(1)}placeholder&gt;", text)
    # Escape backslashes that lead to unicodeescape parser errors
    text = re.sub(r'\\([UuxN])', r'\\\\\1', text)
    return text

def collect_topic_id_map(entries: list[TopicEntry], manual_root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for e in entries:
        if not e.path:
            continue
        key = e.path.lower()
        out[key] = e.topic_id
        p = Path(e.path)
        if p.suffix.lower() == ".html":
            out[p.with_suffix(".htm").as_posix().lower()] = e.topic_id
        elif p.suffix.lower() == ".htm":
            out[p.with_suffix(".html").as_posix().lower()] = e.topic_id
    return out

def rewrite_xrefs(body: Node, topic_path: Path, manual_root: Path, topic_id_map: dict[str, str]) -> None:
    topic_dir = topic_path.parent
    to_unwrap: list[Node] = []

    def _process(el: Node) -> None:
        href = el.attributes.get("href")
        if not href or href.startswith(("http://", "https://", "mailto:")):
            return
        frag = ""
        target = href
        if "#" in href:
            target, frag = href.split("#", 1)
        if not target:
            return
        try:
            abs_target = (topic_dir / target).resolve()
            rel = abs_target.relative_to(manual_root.resolve()).as_posix()
        except (OSError, ValueError):
            el.attrs["href"] = ""
            return
        tid = topic_id_map.get(rel.lower())
        if tid:
            new_href = f"#topic-{tid}"
            if frag:
                new_href += f"--{frag}"
            el.attrs["href"] = new_href
        else:
            to_unwrap.append(el)

    for el in body.css("a"):
        _process(el)
    for el in to_unwrap:
        try:
            el.unwrap()
        except Exception:
            try:
                del el.attrs["href"]
            except Exception:
                pass

def _process_single_image(abs_src: Path, ext: str, alt: str, is_icon: bool, state: ImageProcessingState) -> ImageRef | None:
    ref = state.by_original.get(abs_src)
    if ref is not None:
        return ref
    safe = sanitize_filename(abs_src.stem)
    if is_icon:
        dest_name = f"icon_{safe}{ext}"
        owner = state.used_dest_names.get(dest_name)
        if owner is not None and owner != abs_src:
            suffix = hashlib.sha1(abs_src.as_posix().encode("utf-8")).hexdigest()[:6]
            dest_name = f"icon_{safe}_{suffix}{ext}"
        icon_id = f"icon:{safe.lower().replace('_', '-')}"
    else:
        state.counter += 1
        dest_name = f"img_{state.counter:03d}_{safe}{ext}"
        icon_id = ""
    state.used_dest_names[dest_name] = abs_src
    dest_abs = state.dest_dir / dest_name
    try:
        state.dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(abs_src, dest_abs)
    except OSError as e:
        state.failures += 1
        return None
    dest_rel = f"./{state.dest_dir_basename}/{dest_name}"
    try:
        sha = hashlib.sha256(abs_src.read_bytes()).hexdigest()
    except OSError:
        sha = ""
    ref = ImageRef(
        original_abs=abs_src,
        dest_rel=dest_rel,
        alt=alt or icon_id,
        sha256=sha,
        skip_vlm=(ext in VLM_SKIP_EXTS or is_icon),
    )
    state.by_original[abs_src] = ref
    state.refs.append(ref)
    return ref

def process_images_in_body(body: Node, topic_path: Path, manual_root: Path, state: ImageProcessingState, image_mode: str) -> None:
    topic_dir = topic_path.parent
    
    # Process img tags
    for img in body.css("img"):
        src = img.attributes.get("src") or ""
        alt = img.attributes.get("alt") or ""
        if not src or src.startswith(("http://", "https://", "data:")):
            _replace_with_text(img, f"[Image omitted: {alt or src or 'external'}]")
            continue
        try:
            abs_src = (topic_dir / src).resolve()
            abs_src.relative_to(manual_root.resolve())
        except (OSError, ValueError):
            state.failures += 1
            _replace_with_text(img, f"[Image omitted: {alt or Path(src).name}]")
            continue
        if not abs_src.is_file():
            state.failures += 1
            _replace_with_text(img, f"[Image omitted: {alt or Path(src).name}]")
            continue
        ext = abs_src.suffix.lower()
        if ext not in IMAGE_EXTS:
            state.failures += 1
            _replace_with_text(img, f"[Image omitted: {alt or Path(src).name}]")
            continue

        if image_mode == "strip":
            _replace_with_text(img, f"[Image omitted: {alt or abs_src.name}]")
            continue

        is_icon = (
            "Icon" in (img.attributes.get("class") or "").split()
            or "/Resources/Images/Icons/" in abs_src.as_posix()
        )

        ref = _process_single_image(abs_src, ext, alt, is_icon, state)
        if ref:
            img.attrs["src"] = ref.dest_rel
            if is_icon and not (img.attributes.get("alt") or "").strip():
                img.attrs["alt"] = ref.alt
        else:
            _replace_with_text(img, f"[Image omitted: {abs_src.name}]")

    # Process a tags linking to local images (popups / lightboxes)
    for a in body.css("a"):
        href = a.attributes.get("href") or ""
        if not href or href.startswith(("http://", "https://", "data:", "mailto:")):
            continue
        
        # Check if the href points to an image file
        href_path = Path(unquote(href))
        ext = href_path.suffix.lower()
        if ext not in IMAGE_EXTS:
            continue
            
        try:
            abs_src = (topic_dir / unquote(href)).resolve()
            abs_src.relative_to(manual_root.resolve())
        except (OSError, ValueError):
            a.attrs["href"] = ""
            continue
        if not abs_src.is_file():
            a.attrs["href"] = ""
            continue
            
        if image_mode == "strip":
            a.attrs["href"] = ""
            continue
            
        is_icon = "/Resources/Images/Icons/" in abs_src.as_posix()
        ref = _process_single_image(abs_src, ext, "", is_icon, state)
        if ref:
            a.attrs["href"] = ref.dest_rel
        else:
            a.attrs["href"] = ""

def _replace_with_text(node: Node, text: str) -> None:
    node.replace_with(text)


def _inside_table(node: Node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.tag == "table":
            return True
        parent = parent.parent
    return False

def _table_needs_html(table_node: Node) -> bool:
    for cell in table_node.css("td, th"):
        if len(cell.css("p")) > 1 or cell.css_first("ul") or cell.css_first("ol") or cell.css_first("table"):
            return True
        text = cell.text(separator=" ") or ""
        if "|" in text:
            return True
    return False

def extract_complex_tables(body: Node) -> list[str]:
    raw_blocks: list[str] = []
    for table in body.css("table"):
        if not _table_needs_html(table):
            continue
        outer = table.html or ""
        idx = len(raw_blocks)
        raw_blocks.append(outer)
        table.replace_with(TABLE_PLACEHOLDER_FMT.format(i=idx))
    return raw_blocks

def restore_complex_tables(md: str, blocks: list[str]) -> str:
    if not blocks:
        return md
    def _repl(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(blocks):
            return "\n\n" + blocks[idx] + "\n\n"
        return m.group(0)
    return TABLE_PLACEHOLDER_RE.sub(_repl, md)

def convert_topic_body(topic_path: Path, manual_root: Path, topic_id_map: dict[str, str], image_state: ImageProcessingState, image_mode: str) -> str:
    raw = topic_path.read_text(encoding="utf-8", errors="replace")
    raw = preclean_html_text(raw)
    tree = HTMLParser(raw)
    body = tree.css_first(BODY_SELECTOR)
    if body is None:
        return ""
    for sel in STRIP_SELECTORS:
        if sel == "img.Icon":
            continue
        for el in body.css(sel):
            el.decompose()
    _strip_anchor_stubs(body)
    h1 = body.css_first("h1")
    if h1:
        h1.decompose()
    rewrite_xrefs(body, topic_path, manual_root, topic_id_map)
    process_images_in_body(body, topic_path, manual_root, image_state, image_mode)
    raw_tables = extract_complex_tables(body)
    body_html = body.html or ""
    try:
        md = FlareMarkdownConverter(heading_style="ATX").convert(body_html)
    except (SyntaxError, re.error, UnicodeDecodeError) as e:
        # Fallback: manually double escape backslashes in raw HTML string and try again
        escaped_html = body_html.replace('\\U', '\\\\U').replace('\\u', '\\\\u').replace('\\x', '\\\\x')
        try:
            md = FlareMarkdownConverter(heading_style="ATX").convert(escaped_html)
        except Exception:
            # Final fallback: return text contents only
            md = body.text() or ""
    md = _merge_adjacent_code_fences(md)
    md = restore_complex_tables(md, raw_tables)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md

def inject_figure_captions(md: str, refs: list[ImageRef], caption_by_sha: dict[str, str]) -> str:
    by_dest: dict[str, str] = {}
    for ref in refs:
        if ref.sha256 and ref.sha256 in caption_by_sha:
            by_dest[ref.dest_rel] = caption_by_sha[ref.sha256]
    if not by_dest:
        return md
    pattern = re.compile(r"(!\[[^\]]*\]\(([^)]+)\))")
    def _repl(m: re.Match[str]) -> str:
        whole, dest = m.group(1), m.group(2)
        cap = by_dest.get(dest)
        if not cap:
            return whole
        return f"{whole}\n\n> **Figure:** {cap}"
    return pattern.sub(_repl, md)

def slugify_for_anchor(title: str) -> str:
    s = re.sub(r"[^\w\s-]", "", title.lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s or "section"

def parse_help_system_title(manual_root: Path) -> str | None:
    helpsys = manual_root / "Data" / "HelpSystem.xml"
    if not helpsys.is_file():
        return None
    try:
        tree = ET.parse(helpsys)
    except ET.ParseError:
        return None
    root = tree.getroot()
    for el in root.iter():
        tag = el.tag.split("}", 1)[-1]
        if tag == "Title":
            text = (el.text or "").strip()
            if text:
                return text
    for el in root.iter():
        title_attr = el.attrib.get("Title")
        if title_attr and title_attr.strip():
            return title_attr.strip()
    return None

def resolve_unzipped_root(source_path: Path, unzip_dir: Path) -> tuple[Path, bool]:
    """Return the unzipped manual root directory."""
    if source_path.is_dir():
        return source_path, False
    basename = manual_basename_from_zip(source_path)
    sibling = source_path.parent / basename
    if (sibling / "Sources").is_dir() and any((sibling / "Sources").iterdir()):
        return sibling, False
    target = unzip_dir / basename
    if (target / "Sources").is_dir() and any((target / "Sources").iterdir()):
        return target, False
    target.mkdir(parents=True, exist_ok=True)
    unzip_one(source_path, target)
    return target, True


def build_manual(
    source_path: Path,
    output_dir: Path,
    unzip_dir: Path,
    image_mode: str,
    vlm: VLMClient | None,
    overwrite: bool,
    dry_run: bool,
    version: str | None = None,
    module: str | None = None,
) -> ManualReport:
    """Build a consolidated Markdown manual from a MadCap Zip or Folder source."""
    basename = manual_basename_from_zip(source_path)
    
    # Establish filenames based on presence of version/module
    prefix = ""
    if version and module:
        prefix = f"{version}_{module}_"
    elif version:
        prefix = f"{version}_"
    elif module:
        prefix = f"{module}_"
        
    out_name = f"{prefix}{basename}.md"
    out_md = output_dir / out_name
    images_dir_name = f"{prefix}{basename}_images"
    images_dir = output_dir / images_dir_name

    report = ManualReport(
        version=version, module=module, manual_name=basename, output_path=out_md
    )
    if out_md.exists() and not overwrite:
        report.status = "skipped"
        try:
            report.file_size = out_md.stat().st_size
        except OSError:
            pass
        return report

    if dry_run:
        report.status = "dry-run"
        return report

    try:
        manual_root, did_unzip = resolve_unzipped_root(source_path, unzip_dir)
        entries, wrapper_title = load_toc(manual_root)
        if not entries:
            report.status = "error"
            report.error = "no TOC entries"
            return report
        topic_id_map = collect_topic_id_map(entries, manual_root)
    except Exception as e:
        report.status = "error"
        report.error = f"initialization failed: {e}"
        return report

    if images_dir.is_dir():
        try:
            shutil.rmtree(images_dir)
        except OSError as e:
            report.status = "error"
            report.error = f"failed to clean images directory: {e}"
            return report

    image_state = ImageProcessingState(
        dest_dir=images_dir,
        dest_dir_basename=images_dir_name,
    )

    sections: list[tuple[int, str, str, str]] = []

    for entry in entries:
        if not entry.path:
            sections.append((entry.depth, entry.title, "", ""))
            continue
        topic_path = find_topic_file(manual_root, entry.path)
        if topic_path is None:
            if "/" in entry.path and "." in Path(entry.path).name:
                report.topic_failures += 1
                console.print(f"  [yellow]skip[/] missing topic: {entry.path}", highlight=False)
            continue
        if is_shell_path(topic_path, manual_root):
            continue
        try:
            body_md = convert_topic_body(
                topic_path, manual_root, topic_id_map, image_state, image_mode
            )
        except Exception as e:
            report.topic_failures += 1
            console.print(f"  [red]fail[/] {entry.path}: {e}", highlight=False)
            continue
        sections.append((entry.depth, entry.title, entry.topic_id, body_md))
        report.topic_count += 1

    report.image_count = len(image_state.refs)
    report.image_failures = image_state.failures

    captions_by_sha: dict[str, str] = {}
    if image_mode == "vlm" and vlm is not None and image_state.refs:
        n_to_caption = sum(1 for r in image_state.refs if r.sha256 and not r.skip_vlm)
        if n_to_caption:
            console.print(f"  captioning {n_to_caption} unique image(s)...")
        captions_by_sha, vlm_fails = vlm.caption_many(image_state.refs)
        report.image_failures += vlm_fails

    md_parts: list[str] = []
    title = (
        wrapper_title
        or parse_help_system_title(manual_root)
        or basename.replace("_", " ")
    )
    md_parts.append(f"# {title}\n")
    md_parts.append("\n## Table of contents\n")
    seen_slugs: dict[str, int] = {}
    for depth, sec_title, _tid, _body in sections:
        if depth != 0:
            continue
        slug = slugify_for_anchor(sec_title)
        n = seen_slugs.get(slug, 0)
        seen_slugs[slug] = n + 1
        anchor = slug if n == 0 else f"{slug}-{n}"
        md_parts.append(f"- [{sec_title}](#{anchor})")
    md_parts.append("")

    for depth, sec_title, tid, body in sections:
        level = min(depth + 2, 6)
        md_parts.append(f"\n{'#' * level} {sec_title}\n")
        if tid:
            md_parts.append(f'<a id="topic-{tid}"></a>')
            md_parts.append(f"<!-- topic: {tid} -->\n")
        if body:
            section_md = (
                inject_figure_captions(body, image_state.refs, captions_by_sha)
                if image_mode == "vlm" else body
            )
            md_parts.append(section_md)
            md_parts.append("")

    final_md = "\n".join(md_parts).rstrip() + "\n"
    final_md = re.sub(r"\n{3,}", "\n\n", final_md)

    out_md.write_text(final_md, encoding="utf-8")
    report.file_size = out_md.stat().st_size
    report.approx_tokens = max(1, len(final_md) // 4)
    return report
