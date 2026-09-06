# ZLLD_PACKAGE_EXTRACTOR — design notes, assumptions, and test plan

Built from `step1_lld_extractor_brief.md` against the real source of
`ZCR_AURA_CODE_EXTRACTOR`, `ZCR_GET_DEPENDENCY_OBJ_NEW`, `ZCR_GET_OBJECT_SIGNATURE`,
and `Z_GET_DDIC_INFO` (all supplied, none modified). **Written without SAP system
access** — nothing here has been syntax-checked, activated, or run. Treat this as a
first draft to review and test in the real system, not a finished, verified deliverable.

## What to do before activating

1. ~~Create `ZDCCRT_SYNC_LOG` and `ZDCCRT_SYNC_OBJ`~~ — done. See
   `DDIC_TABLES_TO_CREATE_LLD.txt` for the as-built field list (note: `PACKAGE`
   was renamed to `ZPACKAGE` in both tables, and `OBJ_TYPE`/`OBJ_NAME` use
   `TROBJTYPE`/`TROBJ_NAME` data elements — the report has been updated to match).
2. Create screen 9000 (Custom Control `ALV_CONTAINER`) and GUI status `STATUS_9000`
   (function codes `EXEC`/`BACK`/`EXIT`/`CANC`/`SELALL`/`DESELALL`) in SE51/SE41 —
   reuse `ZCR_AURA_CODE_EXTRACTOR`'s own screen 9000 as a template, it's the same
   pattern minus the version-compare column.
3. Maintain text elements `text-001`/`text-002` (selection screen block titles) via
   SE38 Text Elements — "Output Mode" / "Package & Output" respectively (the Output
   Mode block was moved above the Package/Output block on the selection screen, DB
   mode set as the default, and `P_FILE` hidden unless File mode is selected — see
   the report's own selection-screen comments).
4. Work through every item in "Assumptions to verify" below in SE37/SE11 before
   activating. None of these are guesses I'm confident enough in to skip checking —
   they're documented in the code too (search for `VERIFY:`), this list just
   collects them in one place.

## Assumptions to verify against the real system

I don't have SAP access, so the following are written from best understanding of
standard SAP behavior, not confirmed against your system. Each is also flagged
inline in the code with a `VERIFY:` comment at the exact point it matters.

1. **`E070-TRSTATUS = 'R'` means "released."** This is the standard SAP convention,
   used in `GATHER_QUALIFYING_TRANSPORTS` and `DETERMINE_CURRENT_HWM` to filter
   transports. If your landscape uses a different/additional status value for
   "released," both forms need that value added to the `WHERE` clause.
2. ~~`RPY_PROGRAM_READ`'s exact `TABLES` parameter name/type~~ — confirmed against
   this system's actual signature (SE37 screenshots): `SOURCE_EXTENDED` (`LIKE
   ABAPTXT255`) exists exactly as guessed, but there is no `STATE` parameter —
   the real one is `READ_LATEST_VERSION` (`TYPE PROGDIR-STATE`, default `SPACE`).
   Fixed to pass `read_latest_version = 'A'` (the standard SAP convention for
   "active version") explicitly, rather than trust `SPACE`'s default behavior.
   `ABAPTXT255`'s line type assumption (flat `C(255)`, matching `TS_LINE_255`)
   is **confirmed correct** — the first real run produced clean, non-garbled,
   non-truncated source across all 23 extracted objects (verified: plain ASCII,
   CRLF, zero null bytes, no line-length anomalies).
3. ~~`SEO_CLASS_GET_SOURCE`'s exact parameter names/types~~ — moot: this FM does
   not exist on the target system at all (confirmed). Replaced with
   `CL_OO_CLASSNAME_SERVICE=>GET_CLASSPOOL_NAME` + the same `READ_PROGRAM_AND_INCLUDES`
   routine already used for `PROG` objects (a class's compiled classpool is
   itself a `PROG`-type object that `INCLUDE`s its definition, sections, and
   every method's own include). **New assumption to verify**: `GET_CLASSPOOL_NAME`
   exists as a sibling of `GET_PUBSEC_NAME`/`GET_PRISEC_NAME`/`GET_PROSEC_NAME`/
   `GET_CCDEF_NAME` on `CL_OO_CLASSNAME_SERVICE` — those four are already called
   successfully by `ZCR_AURA_CODE_EXTRACTOR`'s own `BUILD_DEPENDENCY_INPUT` on
   this system, so the same utility class is confirmed present. **Confirmed
   working**: the first real FULL run against `ZZBA91` produced complete class
   source for all 17 extracted classes, including full method bodies (e.g.
   `ZCL_CBA_ALIAS_DEFINITION` — every `METHOD`/`ENDMETHOD` pair from its
   private-section method list was present and non-duplicated).
4. ~~`REPOSITORY_ENVIRONMENT_RFC` on a main program's own name covers its includes'
   references too~~ — **confirmed correct**, against a real first FULL run against
   package `ZZBA91`. `ZPCBA91R_IMPORT_ALIAS_MDL` (a 3-include program: `_TOP`,
   `_SCR`, `_F01`) had calls that only appear deep in `_F01`
   (`ZCL_CBA_AUTHORIZATION=>USER_HAS_CREATION_ACCESS`, `CL_GUI_FRONTEND_SERVICES`,
   `CL_SXML_STRING_READER`, `CL_IXML`, `CL_ABAP_CODEPAGE`), and every one of them
   showed up correctly in that object's dependency JSON. This was the single
   riskiest untested assumption in this whole report — it held up.
5. **`D010INC` reverse lookup (`include` → `master`) correctly resolves which
   program owns a given include**, used in `DETERMINE_OWNER`'s `REPS` branch to
   route a transport-touched include back to its whole owning program. If a
   program has no explicit includes, `D010INC` may have no row for it at all —
   handled by falling back to treating the include as its own owner, but verify
   this fallback actually fires correctly (rather than, say, `D010INC` returning
   an unexpected value) for a simple single-include report.
6. **`ii_objects` line type — `ZPOC_TT_OBJ`/`ZPOC_TT_DDIC_OBJ`** — used exactly as
   `ZCR_AURA_CODE_EXTRACTOR` already uses them (`gt_dep TYPE zpoc_tt_obj` there),
   so this should already be correct, but double-check the field names
   (`obj_type`/`object_name` for `zpoc_tt_obj`, `obj_type`/`object_name` for
   `zpoc_tt_ddic_obj`) against the DDIC definitions directly if activation
   complains about component names.

## Design decisions worth knowing about (not silently made)

- **ALV fragment granularity vs. whole-object output.** The brief asks to reuse
  `ADD_CLASS_METHODS` unchanged (fragment-level: one row per class *method*), but
  the LLD output format needs one block per whole object. Reconciled by rolling
  fragments up to an `owner_type`/`owner_name` (`DETERMINE_OWNER`) and extracting
  any owner with ≥1 checked fragment, once. Unchecking *some but not all* of a
  class's method rows currently has no effect on that class's own dependency JSON
  (`ZCR_GET_DEPENDENCY_OBJ_NEW`'s `CLAS` branch is all-or-nothing — it always
  returns every method) — only unchecking *every* fragment belonging to a class
  excludes that class. If finer-grained control turns out to matter in practice,
  that's a real UX gap worth raising, not something silently handled.
- **`ZCR_GET_DEPENDENCY_OBJ_NEW`'s `CLAS` branch reused as-is, quirks included.**
  Calling it with `obj_type='CLAS'` returns an inventory of the class's *own*
  methods + implemented interfaces (via `SEOCOMPO`/`SEOMETAREL`), not "what this
  class calls externally" the way the `PROG` branch's `REPOSITORY_ENVIRONMENT_RFC`
  does. This is the FM's actual existing behavior — not changed, not worked
  around, per the brief's explicit "reuse exactly, do not flatten or simplify."
  Whatever this means for a future call-graph built from this data is step 2's
  parser-update problem, explicitly out of scope here (see the brief).
- **`FUNC` has no native branch in `ZCR_GET_DEPENDENCY_OBJ_NEW`.** Resolved the
  same way the *original* tool's `BUILD_DEPENDENCY_INPUT` does: resolve the FM's
  own include via `FUNCTION_INCLUDE_INFO`, then call with `obj_type='PROG'` on
  that include. This is a real, working call-dependency signal (assumption #4
  above doesn't apply here, since a function module's include is exactly what's
  being scanned, not a container needing sub-include coverage).
- **Package DDIC section is package-scoped, not object-referenced-scoped.**
  `BUILD_PACKAGE_DDIC_JSON` queries `TADIR` directly for every `DOMA`/`DTEL`/
  `TABL`/`TTYP` belonging to the package itself, independent of which specific
  DDIC objects any extracted PROG/CLAS/FUNC happens to reference. Each individual
  `ZCR_GET_DEPENDENCY_OBJ_NEW` call's own `ev_ddic_json` is discarded. This
  matches the brief's "one shared DDIC section... the only version of the truth"
  more literally than "whatever got referenced this run" would.
- **Post-FULL-run HWM baseline** is the most-recently-released transport
  *system-wide* as of now (`DETERMINE_CURRENT_HWM`), not scoped to the package —
  its own devclass is irrelevant, it's used purely as a timestamp anchor so the
  first subsequent incremental run only considers transports released after the
  full extraction was taken.
- **HWM advances even for transports that don't touch the package.**
  `GATHER_QUALIFYING_TRANSPORTS` distinguishes "highest released transport
  *considered* this run" (always advances the HWM, so irrelevant system activity
  is never re-scanned) from "transports that actually *touch* this package" (drives
  extraction). Re-read the brief's "highest transport number processed" as
  "considered," not narrowly "relevant" — if that's wrong, `GATHER_QUALIFYING_TRANSPORTS`
  is the only form that needs to change.

## Known gaps (flagged, not fixed here — see brief's own scope boundaries)

- A `FAILED` sync run does not roll back or retry. `ZDCCRT_SYNC_LOG.STATUS` is
  written as `'SUCCESS'` by `UPDATE_SYNC_LOG` (called only after a run completes),
  but nothing currently writes `'FAILED'`/`'PARTIAL'` on a mid-run error, and
  `ZDCCRT_SYNC_OBJ`/`LAST_TRKORR_HWM` are only touched by `FINALIZE_SYNC_STATE`,
  which only runs after a successful write — so a crash mid-run should leave state
  untouched (safe), but there's no explicit `FAILED` bookkeeping for visibility.
  Not built here since the brief doesn't ask for it; worth a follow-up if partial
  failures turn out to be common in practice.
- Background/batch execution (SM36) is explicitly out of scope per the brief —
  this is a foreground dialog run only, fine at pilot scale.

## Acceptance criteria — test plan

Run these against a real test package once activated:

1. **First run, valid package with objects.** ✅ **Passed** — first real FULL run
   against package `ZZBA91` produced `LLD_EXTRACT_ZZBA91_FULL.TXT`: 23 objects
   (17 CLASS, 5 PROGRAM, 1 FUNCTION_MODULE), `RUN_TYPE: FULL`,
   `REMOVED_OBJECTS: NONE`, exactly one `--- DDIC ---` section covering all four
   categories (263 domains, 828 data elements, plus tables/structures/table
   types), `=== OBJECT ===`/`=== END OBJECT ===` counts balanced (23/23), zero
   null bytes, no truncation or garbling. (Whether a `ZDCCRT_SYNC_LOG` row was
   actually written still needs a DB check — not verified from the output file
   alone.) Also incidentally validated assumption #4 above (the riskiest one)
   and assumption #3's `GET_CLASSPOOL_NAME` fix.
2. **Second run, no new released transports.** Expect: empty ALV grid (not an
   error), and — since the brief's wording implies EXEC should still work on an
   empty grid — a header-only output file with `RUN_TYPE: INCREMENTAL`,
   `OBJECT_COUNT` unchanged/0, and `LAST_TRKORR_HWM` unchanged (nothing to advance
   to). If your review of the behavior says "don't even show the screen when
   there's nothing to sync," that's a one-line change in `MAIN` (skip `CALL
   SCREEN 9000` when `gt_lld_multi_obj` is empty) — not built in speculatively.
3. **Third run, one released transport touching one object.** Expect: `RUN_TYPE:
   INCREMENTAL`, ALV shows only that object (plus any class methods it pulls in
   via `EXPAND_CLASS_METHODS`), not the whole package.
4. **Delete/move a test object out of the package, then sync.** Expect: that
   object's `TYPE:NAME` appears in `REMOVED_OBJECTS`.
5. **Eye-check the dependency JSON for one non-trivial object** (multiple calls,
   ≥1 DDIC table reference) against what you'd get calling
   `ZCR_GET_DEPENDENCY_OBJ_NEW` directly for the same object/type. This is also
   where assumption #4 above (whole-program `REPOSITORY_ENVIRONMENT_RFC` coverage)
   gets its real test — pick a multi-include PROG for this check specifically.

## Reuse map (what's untouched vs. new vs. adapted)

| Form | Status |
|---|---|
| `F4_OUTPUT_FOLDER`, `BUILD_FULL_PATH`, `CONFIRM_OVERWRITE_IF_EXISTS`, `DOWNLOAD_OUTPUT_FILE` | reused verbatim |
| `ADD_PROGRAM_INCLUDES`, `BUILD_OBJECT_LIST_FROM_PROG`, `BUILD_OBJECT_LIST_FROM_CLASS`, `BUILD_OBJECT_LIST_FROM_FUNC`, `ADD_CLASS_METHODS` | reused verbatim |
| `COLLECT_TR_HITS`, `EXPAND_CLASS_METHODS`, `BUILD_TR_LIST` | reused verbatim |
| `ZCR_GET_DEPENDENCY_OBJ_NEW`, `ZCR_GET_OBJECT_SIGNATURE`, `Z_GET_DDIC_INFO` | unchanged, called as-is |
| `BUILD_OBJECT_LIST_FROM_PACKAGE` | new, but composed entirely from the reused single-object builders above |
| Everything else (sync-state, deletion detection, owner rollup, whole-object source/dependency reading, LLD output writer, trimmed ALV) | new |
| `GET_SOURCE_FOR_LLD_OBJECT`, `BUILD_DEPENDENCY_FOR_OBJECT`, `BUILD_PACKAGE_DDIC_JSON`, `PROCESS_CHECKED_LLD_OBJECTS`, `EXTRACT_LLD_OBJECT` | reused verbatim by DB mode (see below) |
| `APPEND_FILE_HEADER` | lightly refactored (removed-objects-list logic extracted to `BUILD_REMOVED_OBJECTS_LIST`) - output unchanged, see "DB output mode" below |
| `MAIN` | lightly modified - output-mode branching added after `CALL SCREEN 9000` returns, file path unchanged, see "DB output mode" below |
| DB-mode staging + JSON walker forms (`GENERATE_RUN_ID` through `DISPLAY_DB_MODE_RESULT`) | new, see "DB output mode" below |
| `CALL_PROCESS_EXTRACTION_RUN` | renamed to `CALL_EXTRACT_TO_DB`, calls `ZLLD_EXTRACT_TO_DB` (real logic) instead of the old stub - see "Native processing" below |

## DB output mode (`step1_db_mode_brief.md`)

**Written without SAP access, same caveat as everything above.** The four staging
tables described in this section by their original `ZLLD_*` names have since been
**created** under renamed, as-built names (`ZDCCRT_STG_RUN`/`_OBJ`/`_DEP`/`_DDIC`) —
see `DDIC_TABLES_TO_CREATE_LLD.txt`'s "DB output mode" section for the exact
old-name → new-name/field map. `ZDCCRT_STG_DEP`'s key was initially missing
`DEPENDENCY_SEQ` (would have failed every dependency after the first per object on
a duplicate key) — **fixed**, `DEPENDENCY_SEQ` is now part of the key. The ABAP
source (`ZLLD_PACKAGE_EXTRACTOR.txt`) has been updated to the as-built names; the
walkthrough below keeps its original `ZLLD_*` names for readability but the tables
themselves and the code that touches them use the real names.

### What to do before activating DB mode

1. ~~Create the four staging tables (`ZLLD_STG_RUN`/`_OBJECT`/`_DEPENDENCY`/`_DDIC`)~~
   — **done**, as `ZDCCRT_STG_RUN`/`_OBJ`/`_DEP`/`_DDIC` (see the as-built note above).
   Fix the `ZDCCRT_STG_DEP` key issue before proceeding.
2. ~~Create function module `ZLLD_PROCESS_EXTRACTION_RUN` in SE37~~ — superseded
   twice over: first renamed to `ZLLD_EXTRACT_TO_DB` with real logic, then folded
   directly into `ZLLD_PACKAGE_EXTRACTOR` as a plain `INCLUDE` (no function module
   at all) — see "Native processing" below and create
   `Step 1/Include ZLLD_EXTRACT_TO_DB_F01.txt` instead.
3. ~~Maintain text element `text-002` (the new selection-screen block title)~~ —
   folded into the base report's own text-element instructions above (`text-001` =
   "Output Mode", `text-002` = "Package & Output").
4. Work through "New assumptions to verify" below before trusting DB-mode output.

### New assumptions to verify against the real system

1. **The hand-rolled JSON walker (`FIND_MATCHING_BRACKET` through
   `STAGE_PACKAGE_DDIC`) has never run against a live system.** The two JSON shapes
   it assumes are confirmed real (validated by this project's Python side against
   actual `ZZBA91` output — see `sap_lld_step2/lld_step2/parser.py`), but the ABAP
   scanner itself is new, untested code. Test against a real, non-trivial dependency
   JSON (multiple entries, at least one with a `SIGNATURE` and one without) before
   trusting `ZLLD_STG_DEPENDENCY`/`ZLLD_STG_DDIC` rows. If parsing breaks, the most
   likely culprits, in order: (a) a JSON shape difference from what `parser.py`
   assumed (unlikely — that's already validated against real output, but re-check
   if `ZCR_GET_DEPENDENCY_OBJ_NEW`/`Z_GET_DDIC_INFO` output ever changes), (b) an
   ABAP string-offset-by-one error in `FIND_MATCHING_BRACKET` or
   `SPLIT_JSON_TOP_LEVEL_ELEMENTS`, (c) a `NAME`/`TYPE` value containing a
   `SIGNATURE_JSON` unescaping edge case `EXTRACT_JSON_STRING_FIELD` doesn't handle
   (see its own "VERIFY" comment).
2. **`CL_SYSTEM_UUID=>CREATE_UUID_X16_STATIC` exists and returns a value assignable
   to `SYSUUID_X16`.** This is a stable, kernel-level SAP class with no known
   version dependency, unlike an add-on library — low risk, but still unconfirmed
   on this specific system.
3. **`SYSUUID_X16` as a reusable DDIC data element.** If SE11 doesn't have it
   available the way `DEVCLASS`/`CHAR4` are, define the field directly as `RAW 16`
   instead — see the note in `DDIC_TABLES_TO_CREATE_LLD.txt`.
4. ~~`STRING` as a table key component (`ZLLD_STG_DDIC-DDIC_NAME`)~~ — **confirmed
   rejected** on the real system (this system does not allow `STRING`/`RAWSTRING` in
   any table key, not just this one field). Fixed throughout
   `DDIC_TABLES_TO_CREATE_LLD.txt`: every key field that was `STRING` is now a
   `CHAR`-length data element instead (`ZLLD_STG_DDIC-DDIC_NAME`,
   `ZLLD_OBJ_CALLS-TARGET_OBJECT`/`DEPENDENCY_TYPE`,
   `ZLLD_OBJ_DDIC_REF-DDIC_OBJECT_NAME`, `ZLLD_DDIC_OBJECTS-NAME`,
   `ZLLD_CHUNK_TOKENS-TOKEN`, `ZLLD_EDGE_KIND_MAP-DEPENDENCY_TYPE`,
   `ZLLD_CONFIG-CONFIG_KEY`). No ABAP source changes were needed anywhere for this —
   every affected field is read/written exclusively through dictionary-typed
   structures (`TYPE zlld_obj_calls`, `TYPE zlld_edge_kind_map`, etc.), so the field's
   ABAP type follows whatever SE11 now says automatically; ABAP's implicit
   `STRING`↔`CHAR(n)` conversion (including Open SQL's automatic trailing-blank trim
   when reading a `CHAR` column into a `STRING` variable) makes every existing
   `PERFORM`/`SELECT`/comparison still correct without modification. `ZLLD_OBJ_CALLS`
   and `ZLLD_EDGE_KIND_MAP`'s `DEPENDENCY_TYPE` were deliberately given the exact same
   `CHAR 10` type (not independently chosen lengths) so a future retrieval-report join
   between them stays type-consistent, not just individually SE11-legal. Non-key
   `STRING` fields (`SOURCE`, `SIGNATURE_JSON`, `DETAIL_JSON`, `CONFIG_VALUE`,
   `ERROR_MESSAGE`, `REMOVED_OBJECTS`) are unaffected — this system's restriction is
   specifically on key fields, not `STRING` columns in general.

### Design decisions worth knowing about (not silently made)

- **`EXTRACT_LLD_OBJECT_DB`/`PROCESS_CHECKED_LLD_OBJECTS_DB` are deliberate
  near-duplicates**, not a shared form with a mode branch inside it. The brief's
  "do not touch any existing extraction logic" is honored most literally by never
  adding a conditional into `EXTRACT_LLD_OBJECT`/`PROCESS_CHECKED_LLD_OBJECTS` at
  all — the small owner-rollup duplication this costs is worth that guarantee.
  Both duplicates call the exact same, unmodified
  `GET_SOURCE_FOR_LLD_OBJECT`/`BUILD_DEPENDENCY_FOR_OBJECT`.
- **The optional "Also write file copy" checkbox (`P_ALSOFL`) was dropped** — not
  needed, per explicit instruction. DB mode now only ever stages/loads; if a
  file-mode copy of the same run is wanted for comparison, run the report a second
  time in File mode against the same package instead.
- **`APPEND_FILE_HEADER`'s removed-objects-list logic was extracted into
  `BUILD_REMOVED_OBJECTS_LIST`, called by both file and DB mode.** This is a
  behavior-preserving refactor — same loop, same string, same output — not new
  logic; done so `ZLLD_STG_RUN.REMOVED_OBJECTS` matches the file header's
  `REMOVED_OBJECTS:` line format exactly, character for character, without
  duplicating the loop.
- **`ZLLD_STG_OBJECT.OBJECT_TYPE` stores the short E071 code (`CLAS`/`PROG`/`FUNC`)**,
  not the long `LLD_TYPE` name (`CLASS`/`PROGRAM`/`FUNCTION_MODULE`) file mode's
  `TYPE:` line uses — matching the brief's literal table spec. A future native
  processing step reading this table needs to know this if it ever needs to compare
  against file-mode output.
- **`STAGE_PACKAGE_DDIC`/`STAGE_DDIC_SECTION` use `MODIFY`, not `INSERT`**, as cheap
  insurance against a duplicate `(RUN_ID, DDIC_TYPE, DDIC_NAME)` — not expected from
  this report's own single-package `BUILD_PACKAGE_DDIC_JSON` call, but harmless
  either way.
- **`edge_kind` is not derived anywhere in DB mode**, per the brief — `DEPENDENCY_TYPE`
  is staged exactly as `ZCR_GET_DEPENDENCY_OBJ_NEW` emits it, open vocabulary, no
  `CHECK` constraint, matching step 2's own treatment of this same field.

## Acceptance criteria — DB-mode test plan

Run these against a real test package once the four tables and the stub FM exist:

1. **File-mode regression: byte-for-byte match against a known-good prior run.**
   Run `ZLLD_PACKAGE_EXTRACTOR` in File mode against the same test package used for
   a prior validated run (`ZZBA91`, per the step 2 real-format validation work), and
   diff the two output files directly (`fc`/`diff`/equivalent). They must be
   byte-for-byte identical except for `EXTRACTED_AT` (the only field expected to
   differ between two runs). This is the primary regression check for this brief —
   show the diff, don't just assert it passed.
2. **First (`FULL`) DB-mode run against `ZZBA91`.** Confirm staged row counts match
   already-established ground truth exactly: 23 rows in `ZLLD_STG_OBJECT`, 474 rows
   in `ZLLD_STG_DEPENDENCY`, 263 rows in `ZLLD_STG_DDIC` — the same numbers already
   confirmed for this package via the file-mode/step 2 pipeline. A mismatch here
   points at the JSON walker (assumption #1 above), not at extraction itself, since
   the underlying `ZCR_GET_DEPENDENCY_OBJ_NEW`/`Z_GET_DDIC_INFO` calls are unchanged
   and already validated.
3. **Subsequent `INCREMENTAL` DB-mode run.** Confirm `ZLLD_STG_RUN.REMOVED_OBJECTS`
   and the staged object subset match the same incremental-sync behavior already
   validated for file mode (see the existing test plan above, items 2-4) — this
   only checks that the same, unmodified sync logic now also flows correctly down
   the DB-mode path, not new sync behavior.
4. **Hand-off contract.** Confirm `EXTRACT_TO_DB` (the include's own form, PERFORMed
   from `CALL_EXTRACT_TO_DB` — see "Native processing" below, this superseded the
   original stub-FM hand-off entirely) is actually reached, returns
   `ev_status = 'PROCESSED'` (or `'FAILED'` with a message on a genuine failure), and
   that status/message surfaces correctly via `DISPLAY_DB_MODE_RESULT`. Separately
   confirm the `ZLLD_STG_RUN` row's own `STATUS`/`ERROR_MESSAGE` columns were updated
   to match (a DB check, not something visible from the confirmation screen alone).
## Native processing (`step2_native_processing_brief.md`)

**Written without SAP access, same caveat as everything above.** Originally built as
a separate function module (`ZLLD_PROCESS_EXTRACTION_RUN`, then renamed to
`ZLLD_EXTRACT_TO_DB`) — folded directly into `ZLLD_PACKAGE_EXTRACTOR` as a plain
`INCLUDE` instead, on request, since a separate function module bought nothing here
but SE37/function-group setup ceremony: this report already organizes its own DB-mode
staging logic as forms in the same program, and the load logic is just more forms
called the same way. `EXTRACT_TO_DB` is a `FORM`, PERFORMed from `CALL_EXTRACT_TO_DB`
right where the function-module call used to be.

**As-built note:** all eight final/config tables described below by their original
`ZLLD_*` names have since been **created** under renamed names (`ZDCCRT_OBJECTS`,
`ZDCCRT_OBJ_CALLS`, `ZDCCRT_OBJ_DDREF`, `ZDCCRT_DDIC_OBJ`, `ZDCCRT_CHUNKS`,
`ZDCCRT_CHUNK_TOK`, `ZDCCRT_EKIND_MAP`, `ZDCCRT_CONFIG`) — see
`DDIC_TABLES_TO_CREATE_LLD.txt`'s "Native processing" section for the exact
old-name → new-name/field map. `Include ZLLD_EXTRACT_TO_DB_F01.txt` and
`ZLLD_RETRIEVE_CANDIDATES.txt` have both been updated to the as-built names.
`ZDCCRT_STG_DDIC`'s `DETAIL_JSON` field (`TYPE Z_STRING`) — **confirmed added**,
non-key.

### What to do before activating

1. ~~Create the eight new final/config tables~~ — **done**, under the renamed
   `ZDCCRT_*` names (see the as-built note above).
2. ~~Add the `ERROR_MESSAGE` field~~ — **done** on `ZDCCRT_STG_RUN`.
3. Create `Step 1/Include ZLLD_EXTRACT_TO_DB_F01.txt` as a normal ABAP program (SE38,
   program type "Include") named `ZLLD_EXTRACT_TO_DB_F01` — the exact name must match
   the `INCLUDE zlld_extract_to_db_f01.` statement already added near the top of
   `ZLLD_PACKAGE_EXTRACTOR` (rename one side or the other if a naming-convention
   check forces a different name in SE38). No function group, no SE37 function
   module, no earlier `ZLLD_PROCESS_EXTRACTION_RUN`/`ZLLD_EXTRACT_TO_DB` FM to build
   or rename — this replaces that whole approach.
4. Run `Report ZLLD_SEED_CONFIG_TABLES.txt` once, after the tables exist, to seed
   `ZLLD_EDGE_KIND_MAP` (18 rows) and `ZLLD_CONFIG` (stopwords +
   `CHUNK_MERGE_MIN_LINES`, plus the retrieval-report keys added later — see
   "Native retrieval" below). Safe to re-run later (uses `MODIFY`).
5. Work through "New assumptions to verify" below before trusting the loaded
   tables.

### New assumptions to verify against the real system

1. **The whole thing is untested against a live system**, same as everything else
   built without SAP access. The riskiest single piece is the chunk-boundary
   scanner (`ZLLD_CHUNK_SOURCE`/`ZLLD_FIRST_TOKEN`) — it assumes `METHOD`/`FORM`/
   `FUNCTION` always appear as the first whitespace-delimited token on their own
   line (standard ABAP pretty-printer formatting), which held for the Python
   chunker's equivalent regex-based approach but has never been exercised against
   this exact line-by-line ABAP scanner.
2. **`FIND ALL OCCURRENCES OF REGEX ... RESULTS lt_matches` (`MATCH_RESULT_TAB`)
   syntax** is standard since a fairly old kernel release, but unconfirmed on this
   specific system — if it's rejected, `ZLLD_TOKENIZE_CHUNK` needs rewriting with
   an explicit offset-scanning loop instead (the same lower-level technique
   `ZLLD_PACKAGE_EXTRACTOR`'s own JSON walker already uses, so a working pattern
   exists in this codebase to fall back to).
3. **`ZLLD_STG_DEPENDENCY`/`ZLLD_STG_DDIC` are already correctly populated** by
   `ZLLD_PACKAGE_EXTRACTOR`'s own JSON walker (a separate, also-untested piece from
   the prior brief) — `ZLLD_EXTRACT_TO_DB` trusts those columns completely and does
   no JSON parsing of its own. If the staged `DEPENDENCY_TYPE`/`DEPENDENCY_NAME`
   columns are wrong, that's a bug in the *staging* JSON walker, not in this native
   processing step — check there first if `ZLLD_OBJ_CALLS` looks wrong.
4. **A DDIC-type dependency's `DEPENDENCY_NAME` always names a real DDIC object** —
   `ZLLD_OBJ_DDIC_REF` is populated straight from `ZLLD_STG_DEPENDENCY` rows whose
   `DEPENDENCY_TYPE` is one of `DOMA`/`DTEL`/`TABL`/`STRU`/`TTYP`, with no
   cross-check against `ZLLD_DDIC_OBJECTS` (a dependency could theoretically
   reference a DDIC object outside the package's own TADIR scope, which
   `ZLLD_DDIC_OBJECTS` never stages — that's expected and fine, `ZLLD_OBJ_DDIC_REF`
   deliberately has no FK, same open-vocabulary approach as `ZLLD_OBJ_CALLS`).

### Design decisions worth knowing about (not silently made)

- **`ZLLD_EDGE_KIND_MAP` is a maintainable table, not a hardcoded `CASE`** — per
  the brief, because the real `DEPENDENCY_TYPE` vocabulary has already proven
  larger than expected (18 types on `ZZBA91` alone). Any unrecognized type
  defaults to `edge_kind = 'reference'`, `is_call_like = false` rather than
  erroring — same open-vocabulary philosophy as everywhere else in this project.
- **The trailing-`ENDCLASS.`-only-chunk problem is fixed at the source**, not
  patched after the fact: `ZLLD_MERGE_SMALL_CHUNKS` merges any chunk under
  `CHUNK_MERGE_MIN_LINES` into its predecessor (or into its successor if it's the
  very first chunk with nothing before it). This means the chunk count for
  `ZZBA91` will legitimately differ from the Python-side count (479) — the brief
  explicitly says not to expect an exact match, only "not wildly different."
- **No IDF/document-frequency weighting is computed or stored anywhere.**
  `ZLLD_CHUNK_TOKENS` stores only raw per-chunk term frequency. This is a
  deliberate scope boundary from the brief, not an oversight — materializing IDF
  at load time would need re-deriving it on every incremental change, the exact
  class of staleness bug the untouched-row guarantee elsewhere on this project
  exists to avoid. The future retrieval report computes it at query time instead.
- **`ZLLD_OBJ_DDIC_REF` is populated for every DDIC-shaped dependency type**
  (`DOMA`/`DTEL`/`TABL`/`STRU`/`TTYP`), not just `TABL` — even though the
  table-sharing bonus itself is not widened beyond `TABL` in this build. This lets
  a future retrieval report widen that scope with a query change only, never a
  schema change. Whether it *should* widen it is explicitly left undecided here,
  per the brief.
- **`ZLLD_OBJ_CALLS`'s key (`SOURCE_OBJECT`, `TARGET_OBJECT`, `DEPENDENCY_TYPE`)
  can silently collapse two staged dependency rows into one** if an object has two
  distinct calls to the same target of the same type with different
  `SIGNATURE_JSON` (e.g. two textually-different call sites that both resolve to
  the same method) — `MODIFY` keeps whichever one is processed last. This is the
  brief's own specified key shape, not something introduced here; flagged because
  it's the most likely reason the `ZZBA91` acceptance-criteria row count (474)
  might come back slightly under, if it ever does.
- **`ZLLD_DDIC_OBJECTS` is always a full package-wide replace, on every run type**
  — unlike the object-scoped tables, there's no meaningful per-object "untouched
  row" concept for it, since `ZLLD_STG_DDIC` itself is always a fresh full-package
  snapshot regardless of `FULL`/`INCR` (see `ZLLD_PACKAGE_EXTRACTOR`'s
  `STAGE_PACKAGE_DDIC`, called unconditionally).
- **Failure handling uses a plain `cv_failed`/`cv_error_message` CHANGING-parameter
  convention**, not ABAP exception classes — consistent with this project's
  established style (e.g. `cv_skip` throughout `ZLLD_PACKAGE_EXTRACTOR`) rather
  than introducing a new pattern for this one piece.

## Acceptance criteria — native processing test plan

Run these against a real test package once the eight tables, the renamed function
module, its forms include, and the seed report all exist:

1. **`FULL` DB-mode extraction of `ZZBA91` end to end** (staging, then
   `ZLLD_EXTRACT_TO_DB`). Confirm: 23 objects in `ZLLD_OBJECTS`; 474 rows in
   `ZLLD_OBJ_CALLS` (total row count, not a call-like-only subset); the
   `EDGE_KIND` distribution matches the brief's table exactly (`METH`→197,
   `OM`→57, `CLAS`→43, `STRU`→35, `MESS`→36, `OA`→26, `DTEL`→19, `INCL`→12,
   `FUNC`→7, `INTF`→7, `DGT`→7, `FUGR`→6, `PROG`→6, `TABL`→6, `MSAG`→3, `TTYP`→3,
   `TYPE`→3, `TRAN`→1); 263 rows in `ZLLD_DDIC_OBJECTS`; chunk count in
   `ZLLD_CHUNKS` close to (not necessarily exactly) the Python figure of 479 —
   investigate before calling it done if it's wildly higher or lower, not just
   "not exactly 479."
2. **Subsequent `INCREMENTAL` run.** Confirm `REMOVED_OBJECTS` processing deletes
   exactly those objects' rows and only those, and — the one this brief calls out
   as mandatory, not optional — snapshot every column of at least one object *not*
   mentioned in that run's staged data before the load, then assert byte-for-byte
   identical values after it. Do not consider this brief done without this
   specific check passing.
3. **Re-run the same `FULL` load twice.** Confirm identical row counts both times
   (idempotency) — the `MODIFY`-based writers plus `ZLLD_DELETE_PACKAGE_ROWS`'s
   full clear-then-reload should already guarantee this, but confirm it directly
   rather than trusting the design.
4. **A deliberately broken run** (e.g. temporarily corrupt one staged row so
   `ZLLD_LOAD_ONE_OBJECT` fails partway through). Confirm `ZLLD_STG_RUN.STATUS`
   ends up `'FAILED'` with a populated `ERROR_MESSAGE`, and that none of the
   final tables show a partially-loaded object from that run (the `ROLLBACK WORK`
   actually rolling back, not just the status flag saying so).

## Native retrieval (`zlld_retrieve_candidates_brief.md`)

**Written without SAP access, same caveat as everything above.** `ZLLD_RETRIEVE_CANDIDATES`
is the third native piece — a direct port of `retrieval.py`'s `retrieve()`, read-only
against the tables `ZLLD_EXTRACT_TO_DB` populates. No untouched-row concerns here.

### The one substitution: TF-IDF cosine, not neural cosine

Python's semantic score is cosine similarity between dense embedding vectors — bounded
[0,1] and comparable across different queries by construction. Lexical TF-IDF has no
such guarantee for *raw* scores, so this report instead computes cosine similarity over
sparse TF-IDF-weighted term vectors (`COMPUTE_IDF_TABLE`/`COMPUTE_REQUIREMENT_VECTOR`/
`COMPUTE_SEMANTIC_SCORES`) — mathematically the same bounded, cross-query-comparable
operation, over a cheap ABAP-native lexical representation instead of a dense neural
one. IDF is computed at query time (`LOG(total_chunk_count / doc_freq)`), scoped to the
given package(s), per the brief's explicit "not materialized at load time" decision —
this re-uses `ZLLD_CHUNK_TOKENS`'s own (`TOKEN`, `CHUNK_ID`) key: since each row already
represents one distinct chunk containing that token, `doc_freq` is just a row count per
token within the scoped chunk set, no extra `DISTINCT` needed.

A requirement token that never appears in any scoped chunk gets weight 0 and is excluded
from the requirement's own vector entirely (it can never match anything anyway, and
including it would only inflate the requirement's norm for no reason) — a deliberate,
documented choice, not an oversight.

### What to do before activating

1. Requires `ZLLD_EXTRACT_TO_DB` already run at least once against the target
   package(s), and `ZLLD_CONFIG` seeded — run `ZLLD_SEED_CONFIG_TABLES.txt` (updated
   for this brief with `SEMANTIC_WEIGHT`/`STRUCTURAL_WEIGHT`/three PLACEHOLDER score
   thresholds/three tier multipliers).
2. Maintain text element `text-001` (selection screen block title) via SE38 Text
   Elements — e.g. "Requirement Input".
3. **Run the calibration procedure below before trusting any real output.** The seeded
   `SEED_MIN_SEMANTIC_SCORE`/`NEW_OBJECT_SCORE_FLOOR`/`NEW_OBJECT_SPREAD_THRESHOLD`
   values are explicit placeholders (0.50/0.20/0.05), not calibrated numbers — this
   project was built without SAP access, so there is no real lexical-score data yet to
   calibrate against.

### Calibration procedure (mandatory — run once activated, per the brief)

1. Run the same 3 nonsense + 5 grounded/ambiguous requirement types used in the
   original Python real-data validation (or close variants) through this report against
   `ZZBA91`, and record the raw top-1 `SEMANTIC_SCORE` for each (visible in the exported
   file's per-object header, or add a temporary `WRITE` in `COMPUTE_SEMANTIC_SCORES` for
   the full ranked list while calibrating).
2. Check for a clean, non-overlapping gap between the nonsense group's scores and the
   grounded group's scores — the same way the original Python calibration did. **Do not
   assume the gap will land anywhere near 0.67** — lexical cosine and neural cosine are
   different mechanisms that happen to share the same [0,1] bound, nothing more. Set
   `SEED_MIN_SEMANTIC_SCORE` (and re-derive `NEW_OBJECT_SCORE_FLOOR`, which operates on
   the blended `base_score`, not the raw semantic score, so needs its own pass once
   `SEED_MIN_SEMANTIC_SCORE` is fixed) from this real gap via SM30 on `ZLLD_CONFIG`.
3. Once thresholds are set, re-run the same 8 real `ZZBA91` requirements (R1-R8) already
   used for the Python-based gem validation and compare **decisions**, not raw scores,
   against the already-known-correct answers (R1/R3/R4 correct top candidate, R2's
   already-implemented case, R4's genuine ambiguity, R5's backend-only decline, R6-R8's
   new-object flags). A meaningfully different decision on any of these is worth
   investigating before trusting this report.
4. Report all of this plainly, including a thin gap or mismatched decisions — an honest
   calibration report is the deliverable, not a passing grade (see the brief's own
   deliverable #4).

### Hand-checkable TF-IDF example (deliverable #2 — verify before trusting real data)

Before running against `ZZBA91`, verify `COMPUTE_SEMANTIC_SCORES` against a tiny,
hand-computable case: stage (or temporarily insert) 2-3 short chunks with a known,
distinct vocabulary (e.g. chunk A repeats "credit_limit" three times, chunk B is
unrelated boilerplate), pick a requirement whose tokens overlap chunk A heavily, and
hand-compute the expected TF-IDF vectors and cosine similarity for each chunk using the
same formula this report implements (`IDF = LOG(total_chunks/doc_freq)`, weight =
`tf * idf`, cosine = dot product / (norm × norm)). Confirm the report's own ranking and
raw score match the hand calculation before trusting it on real, larger data — this is
the brief's own explicit acceptance bar for the scoring implementation itself,
independent of the separate real-data calibration above.

### New assumptions to verify against the real system

1. **Untested against a live system**, same as everything else built without SAP
   access. The regex tokenizer (`TOKENIZE_TEXT`) is a byte-for-byte duplicate of
   `ZLLD_EXTRACT_TO_DB`'s `ZLLD_TOKENIZE_CHUNK` — if the two ever drift out of sync
   (e.g. one gets a bugfix the other doesn't), the requirement vector and the stored
   chunk-token vectors stop sharing a vocabulary and every score becomes meaningless
   without any error being raised. Keep them in lockstep deliberately, not by accident.
2. **`LOG( )` as a native ABAP built-in numeric function** (natural log) — expected to
   exist since a fairly old kernel release, but unconfirmed on this specific system. If
   rejected, substitute a `TRY`/`CATCH`-wrapped call to a math utility class, or a
   manual series approximation as a last resort (unlikely to be needed).
3. **`SELECT-OPTIONS s_pack` restricted to single EQ entries only** — a deliberate
   simplification (`VALIDATE_PACKAGES` rejects ranges/exclusions outright with a clear
   message) rather than building general range-matching against `ZLLD_OBJECTS`, since
   every real usage of this tool so far has meant "these specific one or two packages,"
   never a pattern or exclusion.
4. **`WHERE strlen( c~chunk_text ) >= @gc_min_chunk_length` in Open SQL** — `strlen()`
   as a native SQL expression function inside a `WHERE` clause is standard AMDP/CDS-era
   Open SQL syntax; confirm it's available on this system's kernel/DB combination. If
   rejected, filter in ABAP after fetching instead (a `CHECK` inside the loop over the
   raw result set).

### Design decisions worth knowing about (not silently made)

- **`ZLLD_OBJ_DDIC_REF` is NOT separately queried when reconstructing `DEPENDENCIES`** —
  every DDIC-shaped dependency `ZLLD_EXTRACT_TO_DB` staged into `ZLLD_OBJ_DDIC_REF` was
  also written to `ZLLD_OBJ_CALLS` at load time, so reading `ZLLD_OBJ_CALLS` alone
  already includes every DDIC reference. This matches Python's own
  `_build_real_dependencies_json`, which also reads only `object_calls`, never
  `object_uses_table` — the brief's "from `ZLLD_OBJ_CALLS`... plus `ZLLD_OBJ_DDIC_REF`"
  is read here as "everything needed already lives in one place," not as an instruction
  to union two sources and risk duplicate entries.
- **`SIGNATURE_JSON` is embedded verbatim, unescaped, into the exported DEPENDENCIES
  JSON** — it is already a raw JSON object/array substring; re-escaping it as a JSON
  string value would double-encode it into a shape the parser/gem does not expect. The
  same "never parsed, never re-encoded" rule that applied when this data was staged
  applies just as much on the way back out.
- **The outer `DEPENDENCIES` entry's `TYPE` uses `ZLLD_OBJECTS.OBJECT_TYPE` directly** —
  already the short code (`CLAS`/`PROG`/`FUNC`), unlike Python's `objects.type` column
  which stores the long LLD-level name and needs a reverse-mapping dict
  (`_LLD_TYPE_TO_DEPENDENCY_CODE`) that this report doesn't need at all.
- **`MIN_CHUNK_LENGTH` (15 chars) is applied here even though the brief doesn't
  explicitly call it out** — a judgment call, not a brief requirement: Python applied
  it as defense in depth against trivial chunks even after later also fixing the
  chunker itself, and `ZLLD_EXTRACT_TO_DB`'s own merge-based fix operates on *line*
  count, not character count, so an edge case (e.g. one very long but low-signal line)
  could theoretically still slip through. Cheap insurance, not a redesign.
- **`GT_ADJACENCY`/`GT_ALLOWED`/etc. use plain string tables with linear or sorted-table
  binary-search lookups**, not a Python-style hash-map equivalent — ABAP has no native
  set/dict type; `SORTED TABLE ... WITH NON-UNIQUE KEY` on the adjacency table gets the
  same effective lookup cost via the runtime's automatic binary-search optimization for
  `LOOP AT ... WHERE` on a sorted table's key.

## Acceptance criteria — native retrieval test plan

Run these once `ZLLD_EXTRACT_TO_DB` has loaded `ZZBA91` and the config is seeded:

1. **Hand-checkable TF-IDF example** (see above) — passes before anything else is
   trusted.
2. **Calibration procedure** (see above) — run in full, report the gap (or its
   absence) honestly.
3. **R1-R8 decision comparison** against the already-known-correct answers from the
   Python/gem validation, once thresholds are calibrated.
4. **Output file parses correctly through the existing gem instructions, unchanged** —
   confirm the gem needs no instruction change, per the brief's explicit design goal.
5. **At least one exported file run through the actual gem**, confirming the whole
   native pipeline (extraction → DB load → retrieval → gem) works end to end — the same
   validation standard already applied to the Python version.
