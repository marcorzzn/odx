# Getting Started with ODX

ODX (Open Document eXtended) is a flexible format for document analysis and OCR results. This repository provides the core library, CLI tools, and converters to work with ODX files.

## Installation

```bash
git clone https://github.com/marcorzzn/odx.git
cd odx
pip install -e .
```

To install optional dependencies (e.g. for OCR or PDF conversion):
```bash
pip install -e ".[ocr]"
pip install -e ".[pdf]"
pip install -e ".[all]"
```

## Quick Start

### Validate an ODX file

```bash
python odx_cli.py validate sample.odx
```

### Convert PDF to ODX

```bash
python odx_cli.py convert document.pdf
```

### OCR Pipeline

```bash
python odx_cli.py ocr image.png output.odx
```

## Repository Structure

| Directory/File | Description |
| --- | --- |
| `odxlib/` | Core library for parsing and writing ODX. |
| `converters/` | Tooling for converting from other formats (like PDF) to ODX. |
| `odx_renderer/` | Tools for visualizing ODX data (HTML rendering). |
| `docs/` | Documentation and examples. |
| `odx_cli.py` | Unified command-line interface. |
| `SPEC.md` | Formal specification of the ODX format. |
