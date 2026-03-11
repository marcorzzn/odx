# Getting Started with ODX

ODX (Open Document eXtended) is a modern open format for digital documents, designed to overcome the structural limits of PDF. This repository provides the core library, CLI tools, and converters to work with `.odx` files.

## Installation

**Minimal install** (core library only, no external dependencies):

```bash
git clone https://github.com/marcorzzn/odx.git
cd odx
pip install -e .
```

**With recommended extras:**

```bash
# Better compression (highly recommended)
pip install -e ".[compress]"

# PDF conversion support
pip install -e ".[compress,pdf]"

# Full install with OCR
pip install -e ".[all]"
```

## Quick Start

### Create a new ODX document

```python
from odxlib import ODXWriter, ODXReader

# Create a document
writer = ODXWriter()
writer.set_meta(title="My Document", lang="en", authors=[{"name": "Your Name"}])
writer.set_text("This is my first ODX document.")
writer.set_semantic_from_text("This is my first ODX document.")
writer.save("hello.odx")

# Read it back
reader = ODXReader("hello.odx")
print(reader.get_text())
print(reader.get_meta())
```

### Use the CLI

```bash
# Create a document
python odx_cli.py new "My Document" --lang en --text "Hello, world!"

# Show info
python odx_cli.py info hello.odx

# Validate
python odx_cli.py validate hello.odx

# Convert from PDF
python odx_cli.py convert document.pdf

# Extract text
python odx_cli.py extract document.odx
```

### Convert a PDF to ODX

```python
from converters.pdf_to_odx import PDFtoODXConverter

converter = PDFtoODXConverter()
stats = converter.convert("document.pdf", "document.odx")
print(f"Size reduction: {stats['size_reduction_pct']}%")
```

### Render to HTML

```python
from odx_renderer.render_html import ODXHTMLRenderer

renderer = ODXHTMLRenderer()
renderer.render_to_file("document.odx", "document.html")
```

## Repository Structure

| Directory | Contents |
|-----------|----------|
| `odxlib/` | Core library: reader, writer, validator |
| `odxlib/ocr/` | OCR pipeline: Tesseract, EasyOCR, TrOCR |
| `converters/` | Format converters (PDF ↔ ODX) |
| `odx_renderer/` | HTML renderer for ODX files |
| `odx_cli.py` | Unified command-line interface |
| `tests/` | Test suite |
| `docs/` | Documentation and examples |

## Further Reading

- [Format Specification (SPEC.md)](../SPEC.md) — the full binary format spec
- [Interactive Viewer Demo](examples/odx_viewer_demo.html) — open in browser, no install needed
