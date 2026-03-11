# ODX: Open Document eXtended

ODX is an open format for digital documents designed to overcome the structural limits of PDF. It provides adaptive layout, native OCR layer, built-in versioning, and deep semantic structure, while preserving visual fidelity and cryptographic integrity. It's a fast, lightweight, and accessible document format for the modern web.

## Installation

You can install ODX and its components directly from the repository:

```bash
git clone https://github.com/marcorzzn/odx.git
cd odx
pip install -e .
```

## Quick Start

Here is a minimal complete example to create and read an ODX document:

```python
from odxlib import ODXWriter, ODXReader

# Create an ODX document
writer = ODXWriter()
writer.set_meta(title="Hello ODX", lang="en")
writer.set_text("Hello World! This is an ODX file.")
writer.save("hello.odx")

# Read it back
reader = ODXReader("hello.odx")
print(reader.get_text())
```

## Documentation & Examples

- **[Format Specification (v0.1)](SPEC.md)**: The formal specification of the ODX binary container and its core layers.
- **[Interactive Viewer Demo](docs/examples/odx_viewer_demo.html)**: An example HTML renderer for ODX files. You can open this file directly in your browser to see how ODX documents are rendered without installing any software.
- **[Contributing Guide](CONTRIBUTING.md)**: How to contribute to the ODX project.