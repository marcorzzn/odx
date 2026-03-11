# Getting Started with ODX

ODX (Open Document XML) is a flexible format for document analysis and OCR results. This repository provides the core library, CLI tools, and converters to work with ODX files.

## Installation

```bash
git clone https://github.com/marcorzzn/odx.git
cd odx
pip install -e .
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

- `odxlib/`: Core library for parsing and writing ODX.
- `converters/`: Tooling for converting from other formats (like PDF) to ODX.
- `odx_renderer/`: Tools for visualizing ODX data.
- `odx_cli.py`: Unified command-line interface.
