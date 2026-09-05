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
2. **`RPY_PROGRAM_READ`'s exact `TABLES` parameter name/type** (`READ_SINGLE_PROGRAM_SOURCE`).
   Written assuming a parameter behaving like `SOURCE_EXTENDED` with `STATE = 'A'`
   for "active version." Check the actual signature in SE37 — some releases name
   this differently or don't need an explicit state parameter at all.
3. **`SEO_CLASS_GET_SOURCE`'s exact parameter names/types** (`READ_CLASS_SOURCE`).
   Written assuming an importing `CLSKEY` (`TYPE SEOCLSKEY`) and an exporting
   `SOURCE` table of plain text lines. Adjust the one `CALL FUNCTION` if your
   release's signature differs — nothing else depends on the exact shape.
4. **`REPOSITORY_ENVIRONMENT_RFC` on a main program's own name covers its includes'
   references too**, not just the top-level program's own lines. This is the basis
   for calling `ZCR_GET_DEPENDENCY_OBJ_NEW` with `obj_type='PROG'` once per whole
   program in `BUILD_DEPENDENCY_FOR_OBJECT`, rather than once per include (which is
   what the *original* per-fragment tool does). If real testing shows this
   assumption is wrong — i.e. a multi-include program's dependency JSON is missing
   calls that only appear in an include — the fix is to call the FM once per
   include (main program + every `RS_GET_ALL_INCLUDES` result) and merge/dedup the
   resulting JSON candidate lists, the same way the original tool effectively does
   it per-fragment. **This is the single most important thing to eye-check per
   acceptance criterion 5** — pick a PROG object that actually has includes for
   that check, not a single-include one, or this specific risk won't be exercised.
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

1. **First run, valid package with objects.** Expect: every PROG/CLAS/FUNC (incl.
   FUGR members) present in the ALV, `RUN_TYPE: FULL` in the output header, no
   `REMOVED_OBJECTS` (i.e. `NONE`), exactly one `--- DDIC ---` section, and a new
   `ZDCCRT_SYNC_LOG` row for the package after EXEC.
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
