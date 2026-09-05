from lld_step2.chunking import split_source_into_chunks


def test_splits_on_method_boundaries():
    source = "\n".join(
        [
            "METHOD FIRST.",
            "  WRITE: 'a'.",
            "ENDMETHOD.",
            "METHOD SECOND.",
            "  WRITE: 'b'.",
            "ENDMETHOD.",
        ]
    )
    chunks = split_source_into_chunks(source)
    assert len(chunks) == 2
    assert "FIRST" in chunks[0]
    assert "SECOND" in chunks[1]


def test_splits_on_form_boundaries():
    source = "\n".join(["FORM DO_X.", "  x = 1.", "ENDFORM."])
    chunks = split_source_into_chunks(source)
    assert len(chunks) == 1
    assert "DO_X" in chunks[0]


def test_falls_back_to_fixed_size_when_no_boundaries():
    lines = [f"line {i}" for i in range(250)]
    source = "\n".join(lines)
    chunks = split_source_into_chunks(source, fallback_chunk_size_lines=100)
    assert len(chunks) == 3
    assert "line 0" in chunks[0]
    assert "line 249" in chunks[-1]


def test_empty_source_yields_no_chunks():
    assert split_source_into_chunks("") == []
    assert split_source_into_chunks("   \n  \n") == []
