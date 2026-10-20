#!/usr/bin/env python3
"""Command-line interface for the Metadata Scrubber.

Default behavior: Overwrites files in-place with sanitized copies.
Supports: PNG, JPEG, PDF.
"""

import argparse
import sys
from pathlib import Path
from engine import (
    detect_file_type,
    inspect_metadata,
    scrub_file_inplace,
    scrub_directory_inplace,
    scrub_bytes
)

# Terminal styling helpers
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
GRAY = "\033[90m"
RESET = "\033[0m"


def format_bytes(num: int) -> str:
    """Format bytes into human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"


def print_inspection(filepath: Path):
    with open(filepath, "rb") as f:
        data = f.read()

    info = inspect_metadata(data, filepath.name)
    ftype = info.get("file_type", "UNKNOWN")
    count = info.get("metadata_count", 0)

    print(f"\n{BOLD}{CYAN}=== Inspection: {filepath.name} ({ftype}) ==={RESET}")
    print(f"Path: {filepath}")
    print(f"Size: {format_bytes(len(data))}")
    print(f"Found Metadata Entries: {BOLD}{count}{RESET}")

    if count == 0:
        print(f"{GREEN}✓ No metadata found! File is already clean.{RESET}\n")
        return

    print("-" * 65)
    for entry in info.get("entries", []):
        risk = entry.get("risk", "low")
        cat = entry.get("category", "")
        key = entry.get("key", "")
        val = entry.get("value", "")

        if risk == "high":
            badge = f"{RED}[HIGH RISK]{RESET}"
        elif risk == "medium":
            badge = f"{YELLOW}[MEDIUM]{RESET}"
        else:
            badge = f"{GRAY}[LOW]{RESET}"

        print(f"{badge} {BOLD}{cat}:{RESET} {key} -> {val}")
    print("-" * 65 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Scrub metadata from PNG, JPEG, and PDF files. Overwrites files in-place by default."
    )
    parser.add_argument(
        "targets",
        nargs="+",
        help="One or more files or directories to scrub"
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Inspect and display metadata without making any changes"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate scrubbing and show what would be removed without modifying files"
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Create a .bak backup file before overwriting in-place"
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="Optional output directory (if you want to save sanitized copies instead of in-place overwrite)"
    )

    args = parser.parse_args()

    # Collect paths
    all_paths = [Path(t).resolve() for t in args.targets]

    if args.inspect:
        for p in all_paths:
            if p.is_file():
                print_inspection(p)
            elif p.is_dir():
                for sub in p.glob("**/*"):
                    if sub.is_file() and sub.suffix.lower() in (".png", ".jpg", ".jpeg", ".pdf"):
                        print_inspection(sub)
            else:
                print(f"{RED}Error: Path not found: {p}{RESET}", file=sys.stderr)
        return

    print(f"\n{BOLD}{CYAN}🧹 Metadata Scrubber (In-Place Mode){RESET}")
    print(f"{GRAY}Supported formats: PNG, JPEG, PDF{RESET}\n")

    total_processed = 0
    total_saved = 0

    for target in all_paths:
        if not target.exists():
            print(f"{RED}Error: Path does not exist: {target}{RESET}", file=sys.stderr)
            continue

        if target.is_file():
            files = [target]
        else:
            files = [
                f for f in target.glob("**/*")
                if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".pdf")
            ]

        if not files:
            print(f"{YELLOW}No supported PNG, JPEG, or PDF files found in {target}{RESET}")
            continue

        for f in files:
            try:
                with open(f, "rb") as stream:
                    orig_bytes = stream.read()

                if args.dry_run:
                    _, summary = scrub_bytes(orig_bytes, f.name)
                    print(f"{YELLOW}[DRY-RUN]{RESET} {f.name}: would strip {summary['stripped_count']} items ({format_bytes(summary['bytes_saved'])} saved)")
                    total_processed += 1
                    total_saved += summary['bytes_saved']
                elif args.output:
                    out_dir = Path(args.output).resolve()
                    out_dir.mkdir(parents=True, exist_ok=True)
                    out_file = out_dir / f.name
                    clean_bytes, summary = scrub_bytes(orig_bytes, f.name)
                    with open(out_file, "wb") as out_stream:
                        out_stream.write(clean_bytes)
                    print(f"{GREEN}✓ Saved copy:{RESET} {f.name} -> {out_file} ({format_bytes(summary['bytes_saved'])} saved)")
                    total_processed += 1
                    total_saved += summary['bytes_saved']
                else:
                    # In-place overwrite (User's preferred default!)
                    summary = scrub_file_inplace(f, backup=args.backup)
                    print(f"{GREEN}✓ Overwritten in-place:{RESET} {BOLD}{f.name}{RESET} "
                          f"({format_bytes(summary['original_size'])} → {format_bytes(summary['cleaned_size'])}, "
                          f"stripped {summary['stripped_count']} items)")
                    total_processed += 1
                    total_saved += summary['bytes_saved']

            except Exception as e:
                print(f"{RED}✗ Failed to process {f.name}: {e}{RESET}", file=sys.stderr)

    print(f"\n{BOLD}{GREEN}Done! Processed {total_processed} file(s), saved {format_bytes(total_saved)} total.{RESET}\n")


if __name__ == "__main__":
    main()
