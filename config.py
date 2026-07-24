"""Configuration defaults for MadCap Flare HTML5 to Markdown conversion."""

import re
from pathlib import Path

# CSS Selector for the main body content of the topics
BODY_SELECTOR = "#mc-main-content"

# CSS Selectors to strip from the HTML structure before converting to Markdown
STRIP_SELECTORS = [
    # Navigation / TOC / Breadcrumbs widgets
    ".MCMiniTocBox_0",
    ".miniToc",
    ".MCBreadcrumbsBox_0",
    ".breadcrumbs",
    ".nocontent",
    # Inline icon images with no alt text
    "img.Icon",
]

# Image file extensions that should be processed/extracted
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

# Image file extensions to skip for VLM captioning (e.g. SVGs, which are icons or drawings)
VLM_SKIP_EXTS = {".svg"}

# VLM Captioning Prompt (generic and product-agnostic)
VLM_PROMPT = (
    "This is a screenshot from software product documentation. "
    "Describe what the image shows in 1-3 sentences, focusing on: "
    "the dialog or screen name if visible, the specific UI elements, fields, "
    "and any sample values shown. Do not speculate beyond what is visible. "
    "Do not start with \"This image shows\" — start directly with the content."
)

# Default VLM Model from OpenRouter
DEFAULT_VLM_MODEL = "google/gemini-2.5-flash"

# Default OpenRouter Completions URL
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Clean language suffixes from zip/manual filenames (e.g. "_en-us.html5")
LANG_SUFFIX_RE = re.compile(r"_[a-z]{2}-[a-z]{2}\.html5$", re.IGNORECASE)

# Regexes for pre-cleaning raw topic HTML files
XML_DECL_RE = re.compile(r"<\?xml[^>]*\?>", re.IGNORECASE)
LITERAL_PLACEHOLDER_RE = re.compile(r"<(/?)placeholder>", re.IGNORECASE)

# Default workspace-relative directories
DEFAULT_CACHE_DIR = Path("data/vlm_cache")
DEFAULT_UNZIP_DIR = Path("data/html_unzipped_consolidated")
DEFAULT_REPORT_PATH = Path("data/manuals_consolidated_report.md")
