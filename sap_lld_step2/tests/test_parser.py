import pytest

from lld_step2.parser import ParseError, parse_extraction_file

VALID_BLOCK = """\
=== OBJECT: ZCL_TEST_ONE ===
TYPE: CLASS
PACKAGE: ZFI_CORE

--- SOURCE ---
METHOD DO_THING.
  WRITE: 'hello'.
ENDMETHOD.

--- DEPENDENCIES ---
{
  "calls": ["ZCL_TEST_TWO"],
  "tables_used": [{"table": "VBAK", "fields": ["VBELN", "ERDAT"]}]
}
=== END OBJECT ===
"""

SECOND_BLOCK = """\
=== OBJECT: ZCL_TEST_TWO ===
TYPE: PROGRAM
PACKAGE: ZSD_SALES

--- SOURCE ---
FORM DO_OTHER.
ENDFORM.

--- DEPENDENCIES ---
{"calls": [], "tables_used": []}
=== END OBJECT ===
"""


def test_parses_single_object():
    objs = list(parse_extraction_file(VALID_BLOCK))
    assert len(objs) == 1
    obj = objs[0]
    assert obj.name == "ZCL_TEST_ONE"
    assert obj.type == "CLASS"
    assert obj.package == "ZFI_CORE"
    assert "METHOD DO_THING." in obj.source
    assert obj.calls == ["ZCL_TEST_TWO"]
    assert len(obj.tables_used) == 1
    assert obj.tables_used[0].table == "VBAK"
    assert obj.tables_used[0].fields == ["VBELN", "ERDAT"]


def test_parses_multiple_concatenated_objects():
    objs = list(parse_extraction_file(VALID_BLOCK + "\n" + SECOND_BLOCK))
    assert [o.name for o in objs] == ["ZCL_TEST_ONE", "ZCL_TEST_TWO"]


def test_empty_calls_and_tables_used_ok():
    objs = list(parse_extraction_file(SECOND_BLOCK))
    assert objs[0].calls == []
    assert objs[0].tables_used == []


def test_tolerates_extra_blank_lines_and_trailing_whitespace():
    noisy = VALID_BLOCK.replace("PACKAGE: ZFI_CORE", "PACKAGE: ZFI_CORE   ")
    noisy = "\n\n" + noisy + "\n\n\n"
    objs = list(parse_extraction_file(noisy))
    assert objs[0].package == "ZFI_CORE"


def test_missing_object_header_raises():
    bad = VALID_BLOCK.replace("=== OBJECT: ZCL_TEST_ONE ===\n", "")
    with pytest.raises(ParseError):
        list(parse_extraction_file(bad))


def test_missing_source_marker_raises():
    bad = VALID_BLOCK.replace("--- SOURCE ---\n", "")
    with pytest.raises(ParseError):
        list(parse_extraction_file(bad))


def test_missing_dependencies_marker_raises():
    bad = VALID_BLOCK.replace("--- DEPENDENCIES ---\n", "")
    with pytest.raises(ParseError):
        list(parse_extraction_file(bad))


def test_missing_end_object_raises():
    bad = VALID_BLOCK.replace("=== END OBJECT ===\n", "")
    with pytest.raises(ParseError):
        list(parse_extraction_file(bad))


def test_invalid_type_raises():
    bad = VALID_BLOCK.replace("TYPE: CLASS", "TYPE: WIDGET")
    with pytest.raises(ParseError):
        list(parse_extraction_file(bad))


def test_invalid_json_raises():
    bad = VALID_BLOCK.replace('"calls": ["ZCL_TEST_TWO"],', '"calls": [ZCL_TEST_TWO],')
    with pytest.raises(ParseError):
        list(parse_extraction_file(bad))


def test_empty_input_raises():
    with pytest.raises(ParseError):
        list(parse_extraction_file(""))
