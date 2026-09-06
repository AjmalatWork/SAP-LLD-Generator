import pytest

from lld_step2.parser import ParseError, parse_extraction_file

_HEADER = """\
RUN_TYPE: FULL
PACKAGE: ZORDER_MGMT
EXTRACTED_AT: 2026-09-06 10:00:00
REMOVED_OBJECTS: NONE

--- DDIC ---
{"PACKAGES":[{"PACKAGE":"ZORDER_MGMT","DOMAIN":[{"NAME":"ZDOM_STATUS","DESCRIPTION":"Status","TYPE":"CHAR","SIZE":1,"POSSIBLE_VALUES":""}],"DATA_ELEMENT":[],"TABLE":[{"NAME":"VBAK","DESCRIPTION":"Sales header","TYPE":"TRANSP","SM30":false,"LOCK_OBJECT":"","FIELD":2,"INDEX":0,"FIELDS":[{"NAME":"VBELN","KEY":"X","DATA_ELEMENT":"VBELN_VA"},{"NAME":"ERDAT","KEY":"","DATA_ELEMENT":"ERDAT"}],"INDEXES":[]}],"STRUCTURE":[],"TABLE_TYPE":[]}]}
"""

_OBJECT_BLOCK = """\
=== OBJECT: ZCL_TEST_ONE ===
TYPE: CLASS
PACKAGE: ZORDER_MGMT

--- SOURCE ---
METHOD DO_THING.
  WRITE: 'hello'.
ENDMETHOD.

--- DEPENDENCIES ---
[{"TYPE":"CLAS","NAME":"ZCL_TEST_ONE","DEPENDENCIES":[{"TYPE":"METH","NAME":"DO_THING","SIGNATURE":{"VISIBILITY":"PUBLIC"}},{"TYPE":"TABL","NAME":"VBAK"},{"TYPE":"INTF","NAME":"IF_SOMETHING"}]}]
=== END OBJECT ===
"""

_SECOND_OBJECT_BLOCK = """\
=== OBJECT: ZCL_TEST_TWO ===
TYPE: PROGRAM
PACKAGE: ZORDER_MGMT

--- SOURCE ---
FORM DO_OTHER.
ENDFORM.

--- DEPENDENCIES ---
[{"TYPE":"PROG","NAME":"ZCL_TEST_TWO","DEPENDENCIES":[]}]
=== END OBJECT ===
"""


def _valid_file(*object_blocks: str) -> str:
    return _HEADER + "\n" + "\n".join(object_blocks)


def test_parses_header():
    extraction = parse_extraction_file(_valid_file(_OBJECT_BLOCK))
    assert extraction.run_type == "FULL"
    assert extraction.package == "ZORDER_MGMT"
    assert extraction.extracted_at == "2026-09-06 10:00:00"
    assert extraction.removed_objects == []


def test_parses_shared_ddic_section():
    extraction = parse_extraction_file(_valid_file(_OBJECT_BLOCK))
    ddic_by_type = {}
    for d in extraction.ddic_objects:
        ddic_by_type.setdefault(d.ddic_type, []).append(d)

    assert {d.name for d in ddic_by_type["DOMA"]} == {"ZDOM_STATUS"}
    assert {d.name for d in ddic_by_type["TABL"]} == {"VBAK"}
    table = ddic_by_type["TABL"][0]
    assert table.detail["DESCRIPTION"] == "Sales header"
    assert [f["NAME"] for f in table.detail["FIELDS"]] == ["VBELN", "ERDAT"]


def test_parses_single_object_with_real_dependency_shape():
    extraction = parse_extraction_file(_valid_file(_OBJECT_BLOCK))
    assert len(extraction.objects) == 1
    obj = extraction.objects[0]
    assert obj.name == "ZCL_TEST_ONE"
    assert obj.type == "CLASS"
    assert obj.package == "ZORDER_MGMT"
    assert "METHOD DO_THING." in obj.source

    by_type = {d.type: d for d in obj.dependencies}
    assert by_type["METH"].name == "DO_THING"
    assert by_type["METH"].signature == {"VISIBILITY": "PUBLIC"}
    assert by_type["TABL"].name == "VBAK"
    assert by_type["TABL"].signature is None
    assert by_type["INTF"].name == "IF_SOMETHING"
    assert by_type["INTF"].signature is None


def test_parses_multiple_concatenated_objects():
    extraction = parse_extraction_file(_valid_file(_OBJECT_BLOCK, _SECOND_OBJECT_BLOCK))
    assert [o.name for o in extraction.objects] == ["ZCL_TEST_ONE", "ZCL_TEST_TWO"]


def test_empty_dependencies_ok():
    extraction = parse_extraction_file(_valid_file(_SECOND_OBJECT_BLOCK))
    assert extraction.objects[0].dependencies == []


def test_removed_objects_parsed_into_tuples():
    header = _HEADER.replace("REMOVED_OBJECTS: NONE", "REMOVED_OBJECTS: CLAS:ZCL_OLD, FUNC:ZFM_OLD")
    extraction = parse_extraction_file(header + "\n" + _OBJECT_BLOCK)
    assert extraction.removed_objects == [("CLAS", "ZCL_OLD"), ("FUNC", "ZFM_OLD")]


def test_tolerates_extra_blank_lines_and_trailing_whitespace():
    noisy = _valid_file(_OBJECT_BLOCK).replace("PACKAGE: ZORDER_MGMT\n", "PACKAGE: ZORDER_MGMT   \n", 1)
    noisy = "\n\n" + noisy + "\n\n\n"
    extraction = parse_extraction_file(noisy)
    assert extraction.package == "ZORDER_MGMT"


def test_missing_run_type_raises():
    bad = _valid_file(_OBJECT_BLOCK).replace("RUN_TYPE: FULL\n", "")
    with pytest.raises(ParseError):
        parse_extraction_file(bad)


def test_invalid_run_type_raises():
    bad = _valid_file(_OBJECT_BLOCK).replace("RUN_TYPE: FULL", "RUN_TYPE: PARTIAL")
    with pytest.raises(ParseError):
        parse_extraction_file(bad)


def test_missing_ddic_marker_raises():
    bad = _valid_file(_OBJECT_BLOCK).replace("--- DDIC ---\n", "")
    with pytest.raises(ParseError):
        parse_extraction_file(bad)


def test_malformed_ddic_json_raises():
    bad = _valid_file(_OBJECT_BLOCK).replace('"PACKAGES"', "PACKAGES")
    with pytest.raises(ParseError):
        parse_extraction_file(bad)


def test_missing_object_header_raises():
    bad = _valid_file(_OBJECT_BLOCK).replace("=== OBJECT: ZCL_TEST_ONE ===\n", "")
    with pytest.raises(ParseError):
        parse_extraction_file(bad)


def test_missing_source_marker_raises():
    bad = _valid_file(_OBJECT_BLOCK).replace("--- SOURCE ---\n", "")
    with pytest.raises(ParseError):
        parse_extraction_file(bad)


def test_missing_dependencies_marker_raises():
    bad = _valid_file(_OBJECT_BLOCK).replace("--- DEPENDENCIES ---\n", "")
    with pytest.raises(ParseError):
        parse_extraction_file(bad)


def test_missing_end_object_raises():
    bad = _valid_file(_OBJECT_BLOCK).replace("=== END OBJECT ===\n", "")
    with pytest.raises(ParseError):
        parse_extraction_file(bad)


def test_invalid_object_type_raises():
    bad = _valid_file(_OBJECT_BLOCK).replace("TYPE: CLASS", "TYPE: WIDGET")
    with pytest.raises(ParseError):
        parse_extraction_file(bad)


def test_invalid_dependencies_json_raises():
    bad = _valid_file(_OBJECT_BLOCK).replace('"TYPE":"METH"', 'TYPE:"METH"')
    with pytest.raises(ParseError):
        parse_extraction_file(bad)


def test_dependencies_list_with_wrong_length_raises():
    bad = _valid_file(_OBJECT_BLOCK).replace(
        '[{"TYPE":"CLAS","NAME":"ZCL_TEST_ONE","DEPENDENCIES":[{"TYPE":"METH","NAME":"DO_THING","SIGNATURE":{"VISIBILITY":"PUBLIC"}},{"TYPE":"TABL","NAME":"VBAK"},{"TYPE":"INTF","NAME":"IF_SOMETHING"}]}]',
        "[]",
    )
    with pytest.raises(ParseError):
        parse_extraction_file(bad)


def test_no_object_blocks_raises():
    with pytest.raises(ParseError):
        parse_extraction_file(_HEADER)


def test_empty_input_raises():
    with pytest.raises(ParseError):
        parse_extraction_file("")
