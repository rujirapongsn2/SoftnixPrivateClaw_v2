"""Bounded, safe previews for files agents hand back to chat."""

import zipfile

import pytest

from sbot.api.file_preview import PreviewError, preview_document


def _docx(path, paragraphs: list[str]) -> None:
    body = "".join(
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)


def test_docx_preview_extracts_paragraphs_without_rendering_office_markup(tmp_path):
    path = tmp_path / "report.docx"
    _docx(path, ["Executive summary", "Market grew 12%"])

    assert preview_document(path) == {
        "text": "Executive summary\nMarket grew 12%",
        "truncated": False,
    }


def test_text_preview_is_supported_and_legacy_office_stays_download_only(tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("# Notes\nhello", encoding="utf-8")
    assert preview_document(notes) == {"text": "# Notes\nhello", "truncated": False}

    with pytest.raises(PreviewError, match="not supported"):
        preview_document(tmp_path / "slides.ppt")


def test_pptx_preview_preserves_declared_slide_order_and_treats_text_as_text(tmp_path):
    path = tmp_path / 'ข้อเสนอ.pptx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('ppt/presentation.xml', '''<p:presentation
            xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
            <p:sldIdLst><p:sldId r:id="b"/><p:sldId r:id="a"/></p:sldIdLst></p:presentation>''')
        archive.writestr('ppt/_rels/presentation.xml.rels', '''<Relationships>
            <Relationship Id="a" Target="slides/slide1.xml"/>
            <Relationship Id="b" Target="slides/slide2.xml"/></Relationships>''')
        for i, value in [(1, 'Second'), (2, 'First &lt;script&gt;')]:
            archive.writestr(f'ppt/slides/slide{i}.xml', f'''<a:p
                xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
                <a:r><a:t>{value}</a:t></a:r></a:p>''')
    assert preview_document(path) == {'text': 'Slide 1\nFirst <script>\n\nSlide 2\nSecond', 'truncated': False}


def test_pptx_preview_rejects_oversized_slide_xml_before_decompressing(tmp_path, monkeypatch):
    from sbot.api import file_preview
    monkeypatch.setattr(file_preview, 'DOCUMENT_XML_MAX_BYTES', 10)
    path = tmp_path / 'large.pptx'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('ppt/presentation.xml', 'x' * 100)
    with pytest.raises(PreviewError, match='too large'):
        preview_document(path)


@pytest.mark.parametrize('suffix', ['.json', '.yaml', '.xml', '.log', '.py', '.js', '.sql', '.toml'])
def test_structured_and_source_files_are_previewed_as_inert_text(tmp_path, suffix):
    path = tmp_path / ('sample' + suffix)
    text = '<script>alert("x")</script>\nข้อมูลทดสอบ'
    path.write_text(text)
    assert preview_document(path) == {'text': text, 'truncated': False}


def test_fingerprint_matches_content_not_filename_and_is_bounded(tmp_path, monkeypatch):
    from sbot.api import file_preview
    source = tmp_path / 'source.md'
    published = tmp_path / 'published.md'
    source.write_text('same content')
    published.write_text('same content')
    assert file_preview.preview_fingerprint(source) == file_preview.preview_fingerprint(published)
    published.write_text('different content')
    assert file_preview.preview_fingerprint(source) != file_preview.preview_fingerprint(published)
    monkeypatch.setattr(file_preview, 'DOCUMENT_MAX_BYTES', 3)
    assert file_preview.preview_fingerprint(source) == {'sha256': None}
