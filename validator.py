"""Validation logic to compare consolidated Markdown against MadCap source HTML."""

from __future__ import annotations

import re
from pathlib import Path
from selectolax.parser import HTMLParser

from .config import BODY_SELECTOR, STRIP_SELECTORS
from .utils import is_shell_path
from .toc import find_topic_file, load_toc
from .converter import preclean_html_text, _strip_anchor_stubs

WORD_RE = re.compile(r"[a-z0-9]+")
MD_LINK_RE = re.compile(r"\]\([^)]+\)")  # [text](url) — drop the (url) half
MD_ANCHOR_RE = re.compile(r"<a id=\"[^\"]+\"></a>|<!--\s*topic:[^>]*-->")

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at",
    "for", "with", "as", "by", "is", "are", "was", "were", "be", "been",
    "being", "this", "that", "these", "those", "it", "its", "from", "if",
    "not", "no", "can", "you", "your", "we", "our", "they", "their",
    "will", "all", "any", "one", "each", "more", "use", "used", "using",
    "have", "has", "had", "do", "does", "did",
}

def tokens(s: str) -> list[str]:
    return WORD_RE.findall(s.lower())

def find_headings(md: str) -> set[str]:
    """Return lower-cased normalized text of every Markdown heading."""
    out: set[str] = set()
    for m in re.finditer(r"^#{1,6}\s+(.+?)\s*$", md, re.MULTILINE):
        line = re.sub(r"[^a-z0-9]+", " ", m.group(1).lower()).strip()
        if line:
            out.add(line)
    return out

def phrase_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()

def distinctive_phrases(text: str, n: int = 5, max_phrases: int = 20) -> list[str]:
    """Pick non-overlapping n-gram phrases from the text whose tokens are mostly non-stop words."""
    toks = tokens(text)
    out: list[str] = []
    seen: set[str] = set()
    for i in range(0, len(toks) - n + 1, n):
        slice_ = toks[i:i + n]
        if sum(1 for t in slice_ if t not in STOPWORDS) < 3:
            continue
        phrase = " ".join(slice_)
        if phrase in seen:
            continue
        seen.add(phrase)
        out.append(phrase)
        if len(out) >= max_phrases:
            break
    return out

def strip_html_markers(s: str) -> str:
    s = MD_ANCHOR_RE.sub(" ", s)
    s = MD_LINK_RE.sub(" ", s)
    return s

def normalize_full(s: str) -> str:
    return " ".join(tokens(strip_html_markers(s)))

def topic_text(topic_path: Path) -> str:
    """Extract visible text from the source HTML topic."""
    raw = topic_path.read_text(encoding="utf-8", errors="replace")
    raw = preclean_html_text(raw)
    tree = HTMLParser(raw)
    body = tree.css_first(BODY_SELECTOR)
    if body is None:
        return ""
    for sel in STRIP_SELECTORS:
        for el in body.css(sel):
            el.decompose()
    _strip_anchor_stubs(body)
    h1 = body.css_first("h1")
    if h1:
        h1.decompose()
    return body.text(separator=" ")

def validate_markdown_syntax(md_text: str) -> tuple[bool, str | None]:
    """Verify that the markdown is structurally parseable and lacks critical raw parsing failures."""
    try:
        import markdown
        markdown.markdown(md_text)
    except ImportError:
        pass
    except Exception as e:
        return False, f"Markdown compilation failed: {e}"

    if md_text.count("```") % 2 != 0:
        return False, "Unbalanced code block fences (odd number of ```)"
        
    return True, None

def validate(manual_root: Path, md_path: Path) -> tuple[bool, dict]:
    """Compare a consolidated Markdown manual against the source unzipped HTML files.
    
    Returns (passed, details_dict).
    """
    entries, _ = load_toc(manual_root)
    md_raw = md_path.read_text(encoding="utf-8")
    
    syntax_passed, syntax_err = validate_markdown_syntax(md_raw)
    
    md_norm = normalize_full(md_raw)
    md_word_set = set(md_norm.split())
    md_headings = find_headings(md_raw)

    stats = dict(
        topics=0, missing_headings=0, low_coverage=0, drained=0,
        phrase_fail=0, source_words=0, consolidated_words=len(md_norm.split()),
        syntax_ok=1 if syntax_passed else 0,
    )
    bad: list[str] = []
    if not syntax_passed:
        bad.append(f"syntax-error: {syntax_err}")

    for entry in entries:
        if not entry.path:
            key = phrase_key(entry.title)
            if key and key not in md_headings:
                stats["missing_headings"] += 1
                bad.append(f"missing-heading: (group) {entry.title!r}")
            continue
        topic_path = find_topic_file(manual_root, entry.path)
        if topic_path is None or is_shell_path(topic_path, manual_root):
            continue
        stats["topics"] += 1
        title_key = phrase_key(entry.title)
        if title_key and title_key not in md_headings:
            stats["missing_headings"] += 1
            bad.append(f"missing-heading: {entry.title!r}")

        text = topic_text(topic_path)
        src_tokens = tokens(text)
        if not src_tokens:
            continue
        stats["source_words"] += len(src_tokens)
        src_set = set(src_tokens)
        missing = src_set - md_word_set
        cov = 1.0 - (len(missing) / max(1, len(src_set)))
        if cov < 0.9:
            stats["low_coverage"] += 1
            if cov < 0.2:
                stats["drained"] += 1
                bad.append(
                    f"drained:{entry.path} ({cov:.0%} coverage, "
                    f"{len(missing)}/{len(src_set)} words missing)"
                )
            else:
                bad.append(
                    f"low-cov:{entry.path} ({cov:.0%}, "
                    f"{len(missing)}/{len(src_set)} missing)"
                )
        phrases = distinctive_phrases(text)
        miss = sum(1 for p in phrases if p not in md_norm)
        if phrases and miss / len(phrases) > 0.3:
            stats["phrase_fail"] += 1

    passed = (
        syntax_passed
        and stats["missing_headings"] == 0
        and stats["low_coverage"] == 0
        and stats["drained"] == 0
    )
    return passed, {"stats": stats, "issues": bad}
