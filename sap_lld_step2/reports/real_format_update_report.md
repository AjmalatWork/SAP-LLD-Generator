# Step 2 real-format update — validation report

Updates step 2's parser, schema, and loader to consume the real extraction
format `ZLLD_PACKAGE_EXTRACTOR` (step 1) actually produces, replacing the
simplified `{"calls": [...], "tables_used": [...]}` mock schema step 2 was
originally built and tested against. Validated end-to-end against the real
file from `ZZBA91` (23 objects) and a full pytest pass.

## What the real file's format actually is (ground truth, not the brief's illustrative JSON)

Before writing any code, I parsed the real `ZZBA91` extraction file directly
(not from memory of the brief) to pin down its exact shape. Three things
differ from the brief's illustrative JSON:

1. **Keys are UPPERCASE** (`"PACKAGES"`, `"TYPE"`, `"SIGNATURE"`,
   `"IS_STATIC"`, ...), not lowercase.
2. **Booleans are inconsistent by field**, not uniformly JSON `true`/`false`:
   `SM30` (a genuine `ABAP_BOOL` field) serializes as real JSON `true`/
   `false`; `IS_STATIC`/`OPTIONAL`/`KEY` (plain `CHAR1` flags) serialize as
   string `"X"` when true and are **omitted entirely** when false
   (`Z_GET_DDIC_INFO` doesn't use `compress = abap_true` so it emits `false`/
   `""` explicitly; `ZCR_GET_DEPENDENCY_OBJ_NEW` does, so it drops blank
   fields instead).
3. **`DEPENDENCIES` is a JSON array with exactly one entry**, not a bare
   object — `[{"TYPE":..., "NAME":..., "DEPENDENCIES":[...]}]`.
4. **The real dependency-type vocabulary is much larger than documented**:
   observed in one real package alone — `CLAS, DGT, DTEL, FUGR, FUNC, INCL,
   INTF, MESS, METH, MSAG, OA, OM, PROG, STRU, TABL, TRAN, TTYP, TYPE` — vs.
   the brief's `PROG, INCL, FUNC, METH, OM, INTF, DOMA, DTEL, TABL, TTYP`.
   The parser and loader treat this as an open vocabulary (no enum/CHECK
   constraint), not a fixed list, since a different package would likely
   surface still more values.
5. **The real file is encoded in cp1252** (Windows-1252), not UTF-8 —
   `GUI_DOWNLOAD` writes in the SAP GUI's local codepage. `pipeline.py` now
   tries UTF-8 first (the mock generator's own output) and falls back to
   cp1252 rather than guessing from the filename.

These are reported here because none of them were things I could have
gotten right by only reading the brief — they required reading the actual
file with `json.loads`, not eyeballing text.

## Schema

- `object_calls` gained `dependency_type` (raw), `edge_kind` (normalized via
  `graph_loader.EDGE_KIND_MAP`), `signature_json` (raw blob, `NULL` when the
  source data has no `SIGNATURE` key). Unique key widened to
  `(source_object, target_object, dependency_type)` since the same target
  name can legitimately appear under more than one dependency type.
- `object_uses_table` lost `fields` (moved to `ddic_objects`), gained
  `ddic_type`; `table_name` renamed to `ddic_object_name` since it can now
  hold a domain/data-element/table-type name, not only a table.
- New `ddic_objects` table: `(package, ddic_type, name)` primary key,
  `detail_json` blob. `ddic_type` uses five buckets — `DOMA`/`DTEL`/`TABL`/
  `STRU`/`TTYP` — mirroring `Z_GET_DDIC_INFO`'s own already-split arrays
  (a structure's real DDIC/TADIR type is also `TABL`, but the FM's output
  and the dependency-entry `TYPE` vocabulary both already treat `STRU` as
  its own category, so this schema does too rather than reconciling it away).

## Parser

Now returns a single `ExtractionFile` (header + shared DDIC + objects)
rather than streaming bare `ParsedObject`s, since `RUN_TYPE` has to be known
before any loader decision can be made. Fails clearly (not silently) on a
missing/invalid `RUN_TYPE`, a malformed DDIC blob, or a `DEPENDENCIES` array
that isn't exactly one entry — the same "malformed input stops the load"
philosophy the original parser already had, extended to the new sections.

## Loader — FULL vs INCREMENTAL (the part with genuine new risk)

Step 2 had previously only ever been tested with a single full load, re-run
twice to check idempotency — never against an incremental load that's
supposed to leave most existing data untouched. Built and tested both paths:

- **FULL**: `DELETE FROM objects WHERE package = %s` (cascades to
  `object_calls`-as-source, `object_uses_table`, `code_chunks` via existing
  FK) plus `DELETE FROM ddic_objects WHERE package = %s`, then reload from
  scratch. Verified scoped correctly: loading package A, then B, then A
  again leaves B's rows completely untouched (`test_full_run_wipes_and_
  reloads_scoped_to_package`).
- **INCREMENTAL**: `REMOVED_OBJECTS` processed first (`objects` row deleted,
  cascading; `object_calls` rows where the removed name appears as a
  *target* cleaned up explicitly too, since `target_object` has no FK by
  design). Then only the objects actually present in the file are upserted.
  `ddic_objects` is upsert-only — never deleted by an incremental run, since
  its shared section may legitimately be a subset.

**The untouched-row check is the one that actually matters here**, and it's
checked strictly: `test_incremental_run_deletes_removed_updates_target_
leaves_others_untouched` snapshots every column (including the embedding
vector, as text) of every object *not* mentioned in a small incremental
file, runs the incremental load, and asserts the snapshot is **byte-
identical** afterward — not just "still present." It passes.

## Bugs found and fixed while building this (all caught by writing the tests)

1. **Reserved-keyword table alias**: `get_tables_used`'s join used `do` as
   an alias for `ddic_objects` — `DO` is a reserved PostgreSQL keyword,
   causing a syntax error. Renamed to `dd`.
2. **Object-name collision across mock-generated packages**: two independent
   `generate()` calls with different seeds could produce the same object
   name (naming only varied by loop index + a small suffix vocabulary, not
   the package), which silently let one package's mock object steal
   another's `objects` row when both were loaded into the same database.
   This can't happen with real ABAP data (object names are genuinely unique
   system-wide), but it's a real bug in the mock generator — fixed by
   folding the package name into the generated identifier.
3. **Duplicate dependency entry in `generate_incremental`'s mutation**: the
   "add a new dependency to simulate a change" step could add a `TABL`
   reference the object already had, producing a Python-list-length vs.
   actual-DB-row-count mismatch (the DB correctly deduplicates via the
   unique constraint; the test's expectation was wrong, not the loader).
   Fixed to pick a genuinely new table name.

None of these were subtle design disagreements — they were straightforward
bugs, all caught by the new tests before this was called done, not
discovered afterward.

## `ZORDER_MGMT_extract.txt`: reversed from "retire" to "convert"

I initially told the user I'd retire this fixture (per the brief's
"do the fabricated-signature route" caution). While actually building step
3's test suite against the new format, I found its entire semantic-matching
test coverage (`test_clear_match_ranks_top_object_first`, the
`likely_new_object` grounded-requirement cases, etc.) depends on this
file's hand-verified, business-meaningful content — the mock generator's
synthetic ABAP source is deliberately not semantically meaningful, and the
real `ZZBA91` file is both too complex for hand-verified assertions and
(being genuine customer data) not committable to the repo at all. Converting
was the better call once this became concrete, not a change of mind without
cause: I wrote a one-off script that mechanically reparsed the old file's
`calls`/`tables_used` JSON and re-emitted it in the real shape (each `calls`
entry as a `CLAS`-type dependency, each `tables_used` entry as `TABL`, a
synthesized shared DDIC section from the union of all table/field data),
verified the known ground truth (e.g. `ZCL_ORDER_LOGGER` still the sole user
of `ZORDER_LOG`, `ZCL_TAX_CALCULATOR` still zero outgoing calls) survived
the conversion exactly, then deleted the one-off script per its own
docstring. All of step 3's existing tests (15 passed, 1 known `xfail`) run
unchanged against the converted file.

## Mock generator: updated, not deprecated

Now emits the real format directly — file header, shared DDIC section
scoped to only the DDIC names actually referenced (mirroring how a real
incremental file's section can legitimately be a subset), and per-object
`DEPENDENCIES` with realistic `METH`/`OM`/`INTF`/`CLAS`/`TABL`/`DTEL`
entries and plausible `SIGNATURE` enrichment. Mirrors the real system's
actual behavior where it matters for testing: `CLASS`-type mock objects get
`METH` entries representing their *own* method inventory (matching
`ZCR_GET_DEPENDENCY_OBJ_NEW`'s real, documented `CLAS`-branch quirk), while
`PROGRAM`/`FUNCTION_MODULE`-type objects get `OM`-type bare method-call
references and `CLAS`-type object references separately (matching how real
`REPOSITORY_ENVIRONMENT_RFC` output actually splits these). New
`generate_incremental()` builds a small `INCREMENTAL`-run file from an
existing `generate()` call's own specs, for repeatable incremental-loader
testing without a second real sync.

## Downstream effect on step 3 (data shape changed, scoring code did not)

Loading *every* dependency type into `object_calls` (not just genuine
calls) would have silently widened what counts as "1 hop" for step 3's
structural scoring and the embedding-quality benchmark's "related" pair
labeling — e.g. two objects both referencing the same message class would
otherwise look identical to two objects that actually call each other. Per
the brief's explicit "no step 3 scoring changes," I did not touch any
scoring logic or weight — instead, both consumers now filter to
`graph_loader.CALL_LIKE_EDGE_KINDS` (a new shared constant) when fetching
adjacency, restoring their original, already-tested behavior exactly. This
is a data-plumbing fix to keep existing behavior working under the new,
broader schema, not a scoring change. `retrieval.py`'s "shares a DDIC table"
bonus was similarly scoped to `ddic_type = 'TABL'` specifically (not any
DDIC reference), matching the step 3 brief's literal wording.

One thing this update *did* need to touch in step 3: `retrieval.py`'s
`export_candidates_to_file` produces a file meant to be re-parsed by
`lld_step2.parser` — since the parser's expected shape changed entirely,
the export now emits a real file header + a reconstructed real-shape
`DEPENDENCIES` blob (pulled from the object's actual stored `object_calls`
rows) instead of the old `calls`/`tables_used` shape. This was a required
compatibility fix (the alternative was the exported file being unparseable
by the very parser it's designed to be fed to), not a scoring or behavior
change.

## Signature spot-checks (read by eye, per the brief)

**`Z_CBA_COMPUTE_ALIAS_FORM_FILE` → `OM:CREATE_DYN_JOB`** — DB `signature_json`:

```json
{"IS_STATIC": "X", "EXCEPTIONS": ["ZCX_CBA_ALCOS_EXCEPTION"],
 "PARAMETERS": [{"KIND": "IMPORTING", "NAME": "IW_JOB_PHASE", "TYPE": "ZBA91_PHASE"},
                {"KIND": "IMPORTING", "NAME": "IW_VARIANT", "TYPE": "VARIANT"}, ...],
 "VISIBILITY": "PUBLIC"}
```

vs. the raw file's `SIGNATURE` for that same entry:

```json
{"VISIBILITY":"PUBLIC","IS_STATIC":"X",
 "PARAMETERS":[{"NAME":"IW_JOB_PHASE","KIND":"IMPORTING","TYPE":"ZBA91_PHASE"},
               {"NAME":"IW_VARIANT","KIND":"IMPORTING","TYPE":"VARIANT"}, ...],
 "EXCEPTIONS":["ZCX_CBA_ALCOS_EXCEPTION"]}
```

Identical field-for-field (key ordering differs only because Postgres's
JSONB doesn't preserve insertion order — semantically the same object).

**`ddic_objects` resolution** — `ZPCBA91R_COPY_TABLE` references `ZDCBAT_AC_DOM`
(a `TABL`-type dependency); `get_tables_used()` correctly resolves its full
12-field list from `ddic_objects.detail_json` (`AC_DOMAIN`,
`AICRAFT_FAMILY`, `AIRCRAFT`, ...). The same object also references
`DD02T`/`DDOBJNAME`/`E071` — standard SAP objects never themselves part of
`ZZBA91`'s own DDIC section — and those correctly degrade to an empty field
list rather than erroring.

**`edge_kind` distribution across the whole real file** (all 18 raw
dependency types seen, all mapped, none crashed):

| dependency_type | edge_kind | count |
|---|---|---|
| METH | calls_method | 197 |
| OM | calls_method | 57 |
| CLAS | class_ref | 43 |
| STRU | structure_ref | 35 |
| MESS | message_ref | 36 |
| OA | reference | 26 |
| DTEL | data_element_ref | 19 |
| INCL | includes | 12 |
| FUNC | calls_function | 7 |
| INTF | implements | 7 |
| DGT | generic_type_ref | 7 |
| FUGR | function_group_ref | 6 |
| PROG | program_ref | 6 |
| TABL | table_ref | 6 |
| MSAG | message_class_ref | 3 |
| TTYP | table_type_ref | 3 |
| TYPE | type_ref | 3 |
| TRAN | transaction_ref | 1 |

## Full end-to-end run (real file, not just tests)

```
parsed 23 objects (RUN_TYPE=FULL, PACKAGE=ZZBA91)
loaded 23 objects, 474 dependency entries, 263 DDIC objects, 0 removed objects processed
embedded 479 chunks total across 23 objects
```

Re-ran the same file a second time: object/dependency/DDIC-object counts
identical (23 / 474 / 263) — idempotent, as required.

## Test suite: 48 passed, 1 known xfail

- `test_parser.py` (20) — header, shared DDIC section, real dependency
  shape, every malformed-input case.
- `test_chunking.py` (4) — untouched, unaffected by this update.
- `test_pipeline.py` (5) — full-load counts/edges/idempotency/traversal/
  similarity against the updated mock generator.
- `test_real_format_loader.py` (4, new) — `OM`/`INTF` edge_kind, `TABL`
  resolution via `ddic_objects`, package-scoped FULL wipe, and the
  incremental untouched-row byte-identity check.
- `test_retrieval.py` (16, 15 passed + 1 known `xfail`) — step 3's full
  suite, unchanged in intent, running against the converted
  `ZORDER_MGMT_extract.txt`.

## Explicitly not done (per brief)

- No change to step 3's scoring weights or logic (see "Downstream effect"
  above for what *did* need touching and why it isn't a scoring change).
- No re-run of the embedding-quality benchmark itself (it operates on chunk
  text, unaffected by this schema change) — only its "related" pair
  labeling query was fixed to keep working under the new schema.
- No UI for browsing `ddic_objects`/`signature_json`.
- No changes to step 1.
