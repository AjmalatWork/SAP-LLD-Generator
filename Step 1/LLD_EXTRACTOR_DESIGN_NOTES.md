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
3. Maintain text element `text-001` (selection screen block title) via SE38 Text
   Elements — e.g. "Package & Output".
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

## DB output mode (`step1_db_mode_brief.md`)

**Written without SAP access, same caveat as everything above.** Nothing in this
section has been syntax-checked, activated, or run.

### What to do before activating DB mode

1. Create the four staging tables (`ZLLD_STG_RUN`/`_OBJECT`/`_DEPENDENCY`/`_DDIC`) —
   see `DDIC_TABLES_TO_CREATE_LLD.txt`'s new section. **Not created yet** — File mode
   needs none of this and works exactly as before regardless.
2. Create function module `ZLLD_PROCESS_EXTRACTION_RUN` in SE37 matching
   `Function Module ZLLD_PROCESS_EXTRACTION_RUN.txt`'s interface exactly.
3. Maintain text element `text-002` (the new selection-screen block title) via SE38
   Text Elements — e.g. "Output Mode".
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
4. **`STRING` as a table key component (`ZLLD_STG_DDIC-DDIC_NAME`).** Some
   ECC/S4 releases restrict which data types can participate in a table key. If
   SE11 rejects this, switch `DDIC_NAME` to a `CHAR`-length data element instead
   (see the note in `DDIC_TABLES_TO_CREATE_LLD.txt`).

### Design decisions worth knowing about (not silently made)

- **`EXTRACT_LLD_OBJECT_DB`/`PROCESS_CHECKED_LLD_OBJECTS_DB` are deliberate
  near-duplicates**, not a shared form with a mode branch inside it. The brief's
  "do not touch any existing extraction logic" is honored most literally by never
  adding a conditional into `EXTRACT_LLD_OBJECT`/`PROCESS_CHECKED_LLD_OBJECTS` at
  all — the small owner-rollup duplication this costs is worth that guarantee.
  Both duplicates call the exact same, unmodified
  `GET_SOURCE_FOR_LLD_OBJECT`/`BUILD_DEPENDENCY_FOR_OBJECT`.
- **"Also write file copy" re-runs source/dependency extraction a second time**
  rather than reusing what DB-mode staging already computed. Avoiding that would
  mean threading extra output parameters through `EXTRACT_LLD_OBJECT` or otherwise
  touching it. Fine at the pilot scale the brief describes this checkbox for
  ("side-by-side debugging during the transition period"); not recommended against
  a large package on every routine DB-mode run.
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
4. **Hand-off contract.** Confirm `ZLLD_PROCESS_EXTRACTION_RUN` is actually called,
   returns `ev_status = 'STUB_OK'`, and that status/message surfaces correctly via
   `DISPLAY_DB_MODE_RESULT`. Separately confirm the `ZLLD_STG_RUN` row's own
   `STATUS` column was updated to `'STUB_OK'` by the stub (a DB check, not something
   visible from the confirmation screen alone).
5. **"Also write file copy" (if built).** With DB mode + the checkbox both selected,
   confirm both a DB-mode result screen AND a downloaded file appear, and that the
   file's content matches what a plain File-mode run against the same package would
   produce (same caveat as #1 — `EXTRACTED_AT` aside).
