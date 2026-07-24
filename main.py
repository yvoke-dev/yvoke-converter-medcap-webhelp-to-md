"""Unified CLI entry point for MadCap Flare WebHelp HTML5 to Markdown conversion."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console

# Import local packages
from .config import (
    DEFAULT_CACHE_DIR,
    DEFAULT_UNZIP_DIR,
    DEFAULT_REPORT_PATH,
    DEFAULT_VLM_MODEL,
)
from .utils import (
    manual_basename_from_zip,
    already_unzipped,
)
from .converter import (
    VLMClient,
    build_manual,
    ManualReport,
)
from .validator import validate

load_dotenv()
console = Console()
app = typer.Typer(add_completion=False, help="General MadCap Flare HTML5 to Markdown converter.")

def discover_sources(source_path: Path) -> list[Path]:
    """Discover manual sources (either zip files or directories containing Data/Tocs)."""
    if source_path.is_file():
        return [source_path]
    
    if (source_path / "Data" / "Tocs").is_dir():
        return [source_path]
        
    # Recursively find all zip files and directories with Data/Tocs
    sources: list[Path] = []
    for p in source_path.rglob("*.zip"):
        if p.is_file():
            sources.append(p)
            
    for toc_dir in source_path.rglob("Tocs"):
        if toc_dir.is_dir() and toc_dir.parent.name == "Data":
            manual_root = toc_dir.parent.parent
            # Avoid adding subfolders of already unzipped directories
            if not any(manual_root.is_relative_to(s) for s in sources if s.is_dir() and s != manual_root):
                sources.append(manual_root)
                
    return sorted(list(set(sources)))

def write_report(reports: list[ManualReport], report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    rows.append("| manual | topics | images | tokens | size | status | error |")
    rows.append("|---|---:|---:|---:|---:|---|---|")
    
    over_200k = 0
    bad_topics = 0
    bad_images = 0
    
    for r in reports:
        flags: list[str] = []
        if r.approx_tokens > 200_000:
            flags.append("⚠️")
            over_200k += 1
        if r.topic_failure_pct > 10.0:
            flags.append("❌")
            bad_topics += 1
        if r.image_failure_pct > 5.0:
            flags.append("🖼️")
            bad_images += 1
            
        status = r.status if r.status != "ok" else " ".join(flags) or "ok"
        err = r.error or ""
        
        rows.append(
            f"| {r.manual_name} | {r.topic_count} | {r.image_count} | "
            f"{r.approx_tokens:,} | {r.file_size:,} | {status} | {err} |"
        )
        
    header = "# Consolidated Manuals Report\n\n"
    header += f"Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n\n"
    header += (
        f"Processed {len(reports)} manual(s) "
        f"({over_200k} ⚠️ over 200k tokens, {bad_topics} ❌ with conversion "
        f"failures, {bad_images} 🖼️ with >5% image failures).\n\n"
    )
    report_path.write_text(header + "\n".join(rows) + "\n", encoding="utf-8")

@app.command()
def run(
    source: Path = typer.Argument(
        ...,
        help="Input MadCap Flare source (zip file, unzipped manual directory, or directory containing multiple manuals).",
    ),
    output_dir: Optional[Path] = typer.Option(
        None,
        help="Directory to write output Markdown and image folders to. Defaults to same directory as source.",
    ),
    unzip_dir: Path = typer.Option(
        DEFAULT_UNZIP_DIR,
        help="Directory to unpack zip files into.",
    ),
    images: str = typer.Option(
        "vlm",
        help="Image processing mode: 'strip' (drops images), 'link' (extracts + links), or 'vlm' (extracts + links + captions with VLM).",
    ),
    vlm_model: Optional[str] = typer.Option(
        None,
        help="Model ID to use for VLM captioning (defaults to google/gemini-2.5-flash via OpenRouter).",
    ),
    vlm_concurrency: int = typer.Option(
        8,
        help="Number of concurrent VLM API requests.",
    ),
    cache_dir: Path = typer.Option(
        DEFAULT_CACHE_DIR,
        help="VLM caption cache directory.",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Overwrite existing output markdown files.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would be processed without executing conversion.",
    ),
    no_validate: bool = typer.Option(
        False,
        "--no-validate",
        help="Skip post-conversion structural validation.",
    ),
    version: Optional[str] = typer.Option(
        None,
        help="Version string to add to document frontmatter metadata.",
    ),
    module: Optional[str] = typer.Option(
        None,
        help="Module code to add to document frontmatter metadata.",
    ),
    report_path: Path = typer.Option(
        DEFAULT_REPORT_PATH,
        help="Path to write the consolidated build report.",
    ),
):
    """Transform MadCap Flare HTML5 exports to consolidated Markdown."""
    t0 = time.time()
    
    if not source.exists():
        console.print(f"[red]Source path does not exist:[/] {source}")
        sys.exit(1)
        
    sources = discover_sources(source)
    if not sources:
        console.print(f"[yellow]No MadCap manual zips or folders found under:[/] {source}")
        sys.exit(0)
        
    console.print(f"Discovered [green]{len(sources)}[/] manual source(s)")
    
    vlm_client = None
    if images == "vlm" and not dry_run:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            console.print("[red]OPENROUTER_API_KEY is not set in environment.[/]")
            sys.exit(2)
        model = vlm_model or os.environ.get("OIM_VLM_MODEL") or DEFAULT_VLM_MODEL
        console.print(f"VLM Captioning enabled. Model: [cyan]{model}[/]")
        vlm_client = VLMClient(
            api_key=api_key,
            model=model,
            cache_dir=cache_dir,
            concurrency=vlm_concurrency,
        )
        
    reports: list[ManualReport] = []
    has_failures = False
    
    try:
        for i, src in enumerate(sources, start=1):
            basename = manual_basename_from_zip(src)
            out_dir = output_dir or src.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            
            console.print(f"\n[{i}/{len(sources)}] Processing: [cyan]{basename}[/]")
            
            rep = build_manual(
                source_path=src,
                output_dir=out_dir,
                unzip_dir=unzip_dir,
                image_mode=images,
                vlm=vlm_client,
                overwrite=overwrite,
                dry_run=dry_run,
                version=version,
                module=module,
            )
            reports.append(rep)
            
            if rep.status == "error":
                console.print(f"  [red]Conversion failed:[/] {rep.error}")
                has_failures = True
                continue
            elif rep.status == "skipped":
                console.print(f"  [dim]Skipped[/] (Markdown output already exists)")
                continue
            elif rep.status == "dry-run":
                console.print(f"  [dim]Dry-run[/] (Planned conversion)")
                continue
                
            console.print(
                f"  Converted: {rep.topic_count} topics, {rep.image_count} images, "
                f"~{rep.approx_tokens:,} tokens, {rep.file_size:,} bytes"
            )
            
            # Post-conversion validation
            if not no_validate:
                try:
                    from .converter import resolve_unzipped_root
                    manual_root, _ = resolve_unzipped_root(src, unzip_dir)
                    passed, info = validate(manual_root, rep.output_path)
                    s = info["stats"]
                    console.print(
                        f"  Validation: topics={s['topics']} headings_missing="
                        f"{s['missing_headings']} low_cov={s['low_coverage']} "
                        f"drained={s['drained']} phrase_fail={s['phrase_fail']}"
                    )
                    if not passed:
                        console.print("  [red]Validation failed[/]")
                        for issue in info["issues"][:10]:
                            console.print(f"    - {issue}")
                        rep.status = "validation-failed"
                        rep.error = "Heading/Content coverage validation failed"
                        has_failures = True
                    else:
                        console.print("  [green]Validation passed[/]")
                except Exception as e:
                    console.print(f"  [red]Validation crashed:[/] {e}")
                    rep.status = "validation-error"
                    rep.error = f"Validation failed with exception: {e}"
                    has_failures = True
                    
    finally:
        if vlm_client:
            vlm_client.close()
            
    elapsed = time.time() - t0
    console.print(
        f"\nProcessed {len(reports)} manual(s) in {elapsed:.1f}s "
        f"({elapsed / max(1, len(reports)):.1f}s/manual)"
    )
    
    if not dry_run:
        write_report(reports, report_path)
        console.print(f"Consolidated build report written to: {report_path}")
        
    if has_failures:
        sys.exit(1)

if __name__ == "__main__":
    app()
