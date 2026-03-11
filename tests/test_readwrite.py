import pytest
from pathlib import Path
from odxlib import ODXReader, ODXWriter, ODXValidator

def test_odx_read_write(tmp_path):
    # 1. Create a document
    doc_path = tmp_path / "test.odx"
    writer = ODXWriter()
    writer.set_meta(title="Test Doc", lang="en")
    writer.set_text("Hello World")
    writer.set_semantic_from_text("Hello World")
    writer.save(str(doc_path))
    
    # 2. Read it back
    reader = ODXReader(str(doc_path))
    assert reader.get_text().strip() == "Hello World"
    assert reader.get_meta()["title"] == "Test Doc"
    
    # 3. Validate
    validator = ODXValidator(str(doc_path))
    report = validator.validate()
    # Assuming validate() returns a dict with 'errors' list or similar
    # Based on the code seen, it might return a report or print it.
    # Let's check if there are no errors.
    assert len(validator._errors) == 0

def test_sample_fixture():
    fixture_path = Path(__file__).parent / "fixtures" / "sample.odx"
    assert fixture_path.exists()
    
    reader = ODXReader(str(fixture_path))
    assert "Hello ODX!" in reader.get_text()
    assert reader.get_meta()["title"] == "Sample Document"
