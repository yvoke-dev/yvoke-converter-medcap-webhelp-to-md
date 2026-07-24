# MadCap to Markdown (`yvoke-converter-medcap-webhelp-to-md`)

A generalized, product-agnostic conversion tool to compile unzipped or zipped MadCap Flare WebHelp HTML5 manuals into single consolidated, clean Markdown files. 

It preserves Table of Contents (TOC) hierarchy, handles complex HTML tables, deep-links cross-references using internal anchors, and processes inlined images/screenshots (including optional VLM-based caption generation).

---

## Features

- **Consolidated Output**: Generates **one self-contained Markdown file per manual** with the full TOC order preserved.
- **TOC Anchor Binding**: Injects `<a id="topic-..."></a>` anchors and comments for each sub-topic, mapping directly to original source topic paths for robust deep-linking.
- **Complex Table Restoration**: Automatically detects tables with nested block-level structures (lists, paragraphs) or literal `|` pipes, emitting them as clean raw HTML blocks so Markdown formatting does not break.
- **Structured Image Processing**: Copies inline images/icons to a sibling folder and updates links. Dedupes repeated images/glyphs (e.g. navigation icons) to prevent duplication.
- **VLM Screenshot Captions**: (Optional) Sends screenshots to a Vision-Language Model (VLM) via OpenRouter to generate detailed, contextual blockquote captions (`> **Figure:** ...`) with a disk-backed SHA-256 cache.
- **Heading & Vocabulary Validation**: Structural verification checks that 100% of TOC topic titles are present as Markdown headings and vocabulary word coverage is $\ge90\%$ per topic compared to the source HTML.

---

## Installation & Dependencies

To install and run, you need Python 3.9+ and the following dependencies. You can manage them using `pip` or `uv`:

```bash
# Using uv (recommended)
uv pip install selectolax markdownify pyjson5 typer httpx python-dotenv rich

# Using pip
pip install selectolax markdownify pyjson5 typer httpx python-dotenv rich
```

---

## How to Run

The converter exposes a CLI interface via the `main.py` entry point. Because the project uses relative package imports, you should run it as a module from the parent directory, or by setting `PYTHONPATH`.

### 1. Simple Directory or Zip Conversion
Transform a single `.zip` archive or an already unzipped MadCap manual folder:

```bash
# From the parent directory (yvoke/):
uv run python -m yvoke-converter-medcap-webhelp-to-md.main /path/to/manual.html5.zip

# Or from within this directory (yvoke-converter-medcap-webhelp-to-md/):
PYTHONPATH=.. uv run python -m yvoke-converter-medcap-webhelp-to-md.main /path/to/manual.html5.zip
```

### 2. Image Processing Options
You can control how inlined images/screenshots are handled using the `--images` option:

* **`strip`**: Omit all images, replacing them with a textual placeholder `[Image omitted: alt-text]`.
* **`link`**: Extract all images to a sibling folder (e.g., `manual_images/`) and update image link references.
* **`vlm`** (Default): Extract images and query a VLM via OpenRouter to caption screenshots. Captions are cached on disk under `data/vlm_cache/` based on the image's SHA-256 hash.

```bash
# Run with VLM captioning enabled (Requires OPENROUTER_API_KEY environment variable)
export OPENROUTER_API_KEY="your-openrouter-key"
PYTHONPATH=.. uv run python -m yvoke-converter-medcap-webhelp-to-md.main /path/to/manual.zip --images vlm
```

### 3. Processing a Directory of Manuals (Batch Mode)
If you point `source` to a directory containing multiple `.zip` files or unzipped manual folders, the script automatically discovers and processes them sequentially:

```bash
PYTHONPATH=.. uv run python -m yvoke-converter-medcap-webhelp-to-md.main /path/to/manuals_directory/ --output-dir /path/to/output_markdowns/
```

---

## Running Tests

To run the unit tests, run the following command from the parent directory (`yvoke/`), or set `PYTHONPATH` from within this directory:

```bash
# From the parent directory (yvoke/):
uv run python -m yvoke-converter-medcap-webhelp-to-md.test_madcap

# Or from within this directory (yvoke-converter-medcap-webhelp-to-md/):
PYTHONPATH=.. uv run python -m yvoke-converter-medcap-webhelp-to-md.test_madcap
```

---

## CLI Options & Settings

| CLI Option | Description | Default |
|---|---|---|
| `source` *(Argument)* | Input zip file, unzipped manual directory, or directory containing multiple manuals. | *Required* |
| `--output-dir` | Folder to save output consolidated Markdown and images. | Parent folder of the source |
| `--unzip-dir` | Fallback directory to unpack zip manuals. | `data/html_unzipped_consolidated` |
| `--images` | Image handling mode (`strip` \| `link` \| `vlm`). | `vlm` |
| `--vlm-model` | Overrides default VLM model. | `google/gemini-2.5-flash` |
| `--vlm-concurrency` | Number of parallel threads querying the VLM. | `8` |
| `--cache-dir` | Directory containing SHA-256 cached VLM captions. | `data/vlm_cache` |
| `--overwrite` | Force overwrite existing consolidated Markdown files. | `False` |
| `--dry-run` | Scan and output discovery plan without writing files. | `False` |
| `--no-validate` | Skip post-conversion structural coverage validation checks. | `False` (Validation runs) |
| `--version` | Optional version string to inject in the document's frontmatter. | `None` |
| `--module` | Optional module code to inject in the document's frontmatter. | `None` |
| `--report-path` | Path to save the consolidated markdown build report. | `data/manuals_consolidated_report.md` |

---

## Configuration Settings (`config.py`)

All core HTML elements and text processing parameters are extracted into [config.py](file:///Users/eduardpal/work/yvoke/yvoke-converter-medcap-webhelp-to-md/config.py):

* **`BODY_SELECTOR`**: Sets the target div containing the topic prose (defaults to `#mc-main-content`).
* **`STRIP_SELECTORS`**: List of CSS elements (navbars, breadcrumbs, search bars, empty icons) to strip before converting to Markdown.
* **`VLM_PROMPT`**: The product-agnostic prompt template passed to the VLM to caption UI screenshots.
* **`IMAGE_EXTS`**: File extensions identified as valid image files.
* **`VLM_SKIP_EXTS`**: File extensions (like `.svg`) containing vector icons that should skip VLM description queries.
