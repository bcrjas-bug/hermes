# 🧹 Metadata Scrubber

A high-fidelity, privacy-first metadata scrubber and inspector for **PNG**, **JPEG**, and **PDF** files with direct **in-place disk overwriting**, **live web preview**, and a **command-line interface (CLI)**.

---

## ✨ Features

- **PNG Sanitization (Lossless)**:
  - Purges text chunks (`tEXt`, `zTXt`, `iTXt` containing prompts, AI generation parameters, authors, software).
  - Purges `eXIf` metadata chunks, modification timestamps (`tIME`), and device color profiles (`iCCP`).
  - **100% Lossless**: Retains critical chunks (`IHDR`, `PLTE`, `IDAT`, `IEND`) and standard rendering profiles without recompressing or losing pixel quality.

- **JPEG Sanitization (Lossless)**:
  - Strips EXIF camera info, GPS coordinates, lens data, serial numbers (`APP1`).
  - Strips XMP packets, Photoshop IPTC/IRB (`APP13`), comments (`COM`), and vendor markers (`APP2`-`APP12`).
  - **Color Preservation**: Intelligently preserves Adobe `APP14` color transform markers so CMYK images are never color-inverted.
  - **Zero Generation Loss**: Leaves Huffman tables, Quantization tables, and DCT scan data intact with zero re-encoding artifacts.

- **PDF Sanitization**:
  - Purges Document Information Dictionary (`/Info`: Title, Author, Subject, Creator, Producer, Dates).
  - Strips XML RDF packets from root catalog (`/Metadata`).
  - Removes private application data (`/PieceInfo`) and sanitizes annotation author tags (`/T`).
  - **Purges Revision History**: Rewrites PDF streams from scratch, eliminating historical revisions and trailing object chains.

- **Interactive Web Interface**:
  - Drag-and-drop live preview with image thumbnails and PDF summaries.
  - Privacy risk classification (🔴 GPS location & author alerts, 🟡 timestamps, 🔵 technical).
  - **In-Place Overwrite**: Uses the modern Chromium File System Access API (`FileSystemFileHandle`) to overwrite original files directly on disk.
  - Fallback 1-click individual download and "Download All (.ZIP)" for non-Chromium browsers.
  - **Local Mac Path Scanner**: Enter or paste any folder path (e.g. `~/Desktop/photos`) to scrub in-place at native disk speeds.
  - Dark / Light adaptive theme adhering to system settings.

- **Command-Line Interface (CLI)**:
  - Default in-place overwrite mode for rapid batch processing.
  - Deep `--inspect` mode to examine metadata without modifying files.
  - `--dry-run` to preview stripped items and space savings.
  - Optional `--backup` flag (`.bak`) and `--output` directory flag.

---

## 🚀 Quick Start

### 1. Requirements

Installed dependencies in Python 3.13:
- `pillow`
- `pypdf`
- `aiohttp`

### 2. Launch the Web Application

```bash
cd /Users/benjamincrowrojas/Desktop/metadata_scrubber
python3 app.py
```

Your default web browser will automatically open:
```
http://127.0.0.1:8765
```

### 3. Using the CLI (`scrub.py`)

#### Scrub file(s) or folder in-place:
```bash
python3 scrub.py /path/to/photo.jpg /path/to/document.pdf
# Or an entire folder:
python3 scrub.py /Users/benjamincrowrojas/Desktop/my_photos
```

#### Inspect metadata without changing files:
```bash
python3 scrub.py --inspect /Users/benjamincrowrojas/Desktop/INE.pdf
```

#### Simulate scrubbing (Dry Run):
```bash
python3 scrub.py --dry-run /Users/benjamincrowrojas/Desktop/my_photos
```

#### Scrub in-place with `.bak` backups:
```bash
python3 scrub.py --backup /path/to/folder
```

#### Export to a separate directory instead of in-place overwrite:
```bash
python3 scrub.py -o /path/to/cleaned_folder /path/to/originals
```

---

## 🧪 Running Tests

To run the complete test suite:

```bash
cd /Users/benjamincrowrojas/Desktop/metadata_scrubber
python3 test_engine.py
python3 test_edge_cases.py
python3 test_app.py
```

---

## 📁 Project Structure

```
metadata_scrubber/
├── app.py              # Web server (aiohttp.web) & REST API
├── engine.py           # Core inspection & lossless scrubbing engine
├── scrub.py            # CLI tool with in-place overwriting
├── test_engine.py      # Core unit tests
├── test_edge_cases.py  # Edge-case tests (GPS, annotations, ICC)
├── test_app.py         # Web server integration tests
├── templates/
│   └── index.html      # Modern SPA frontend template
└── static/
    ├── app.css         # Adaptive light/dark modern styling
    ├── app.js          # File System Access API & client controller
    └── jszip.min.js    # Offline ZIP bundling
```
