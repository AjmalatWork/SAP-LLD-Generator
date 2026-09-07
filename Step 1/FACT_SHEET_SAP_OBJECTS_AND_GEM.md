# Fact Sheet: SAP ABAP Objects and Gem Instructions

Source-of-truth data only. No narrative, no prioritization, no development history.

---

## 1. Report ZLLD_PACKAGE_EXTRACTOR

**Purpose:** Extracts ABAP objects (programs, classes, function modules) from a selected SAP package, either as a flat text file or as staged/loaded database rows, with transport-based incremental sync.

### Inputs (selection screen)

| Parameter | Type | Default | Notes |
|---|---|---|---|
| P_MDB | Radiobutton (group OUTM) | Selected (X) | Output Mode = Database |
| P_MFILE | Radiobutton (group OUTM) | Not selected | Output Mode = File |
| P_PACK | DEVCLASS | blank | Package to extract; validated against TDEVC; required (checked in code, not marked OBLIGATORY) |
| P_FILE | STRING | `C:\temp\` | Output folder; visible/required only when P_MFILE is selected; F4 opens a folder browser |

### Outputs

- **File mode:** one `.TXT` file per run, downloaded to the client via `GUI_DOWNLOAD`, filename derived from package/run type.
- **Database mode:** rows written to `ZDCCRT_STG_RUN`, `ZDCCRT_STG_OBJ`, `ZDCCRT_STG_DEP`, `ZDCCRT_STG_DDIC`, then (via the included `EXTRACT_TO_DB` logic) to `ZDCCRT_OBJECTS`, `ZDCCRT_OBJ_CALLS`, `ZDCCRT_OBJ_DDREF`, `ZDCCRT_DDIC_OBJ`, `ZDCCRT_CHUNKS`, `ZDCCRT_CHUNK_TOK`. A per-table row-count summary is written to the ABAP list (spool) at the end of a successful run.
- Both modes update `ZDCCRT_SYNC_LOG` and `ZDCCRT_SYNC_OBJ` with the run's sync state.

### Tables read

TADIR, TFDIR, TDEVC, E070, E071, D010INC, SEOCOMPO, ZDCCRT_SYNC_LOG, ZDCCRT_SYNC_OBJ, ZDCCRT_STG_RUN, ZDCCRT_STG_OBJ, ZDCCRT_STG_DEP, ZDCCRT_STG_DDIC, ZDCCRT_OBJECTS, ZDCCRT_EKIND_MAP, ZDCCRT_CONFIG

### Tables written

ZDCCRT_SYNC_LOG, ZDCCRT_SYNC_OBJ, ZDCCRT_STG_RUN, ZDCCRT_STG_OBJ, ZDCCRT_STG_DEP, ZDCCRT_STG_DDIC, ZDCCRT_OBJECTS, ZDCCRT_OBJ_CALLS, ZDCCRT_OBJ_DDREF, ZDCCRT_DDIC_OBJ, ZDCCRT_CHUNKS, ZDCCRT_CHUNK_TOK

### Function modules called (unmodified, as-is)

- `ZCR_GET_DEPENDENCY_OBJ_NEW` — returns an object's dependencies as JSON
- `ZCR_GET_OBJECT_SIGNATURE` — returns a method's signature
- `Z_GET_DDIC_INFO` — returns package DDIC catalog as JSON
- `RPY_PROGRAM_READ`, `FUNCTION_INCLUDE_INFO`, `RS_GET_ALL_INCLUDES` — standard SAP source-reading APIs

### Key logic

- **Run-type determination:** a package with no row in `ZDCCRT_SYNC_LOG` triggers a FULL run (every PROG/CLAS/FUNC in the package, including individual function-group members expanded from FUGR); a package with an existing sync-log row triggers an INCREMENTAL run (only objects touched by transports released since the last sync's high-water mark, determined via E070/E071).
- **Deletion detection:** runs on every extraction regardless of run type, by diffing the package's live TADIR contents against the object snapshot stored in `ZDCCRT_SYNC_OBJ` from the last successful run.
- **Class method expansion:** for both FULL-run and incremental-run paths, a class hit is expanded to its methods via two sources — SEOCOMPO (CMPTYPE = '1', methods a class newly declares) and RTTI (`CL_ABAP_TYPEDESCR=>DESCRIBE_BY_NAME`, catching methods that only redefine an inherited method, e.g. common `_EXT` OData provider class overrides, which never get their own SEOCOMPO row).
- **Source chunking (DB mode only):** an object's source is split into chunks on METHOD/ENDMETHOD, FORM/ENDFORM, FUNCTION/ENDFUNCTION boundaries; a fixed ~100-line chunk size is used as fallback if no such boundary exists in the source. Any resulting chunk shorter than the configured minimum line count is merged into an adjacent chunk.
- **Tokenization (DB mode only):** each chunk's text is split on non-alphanumeric characters and underscores, lowercased, and filtered against a configurable stopword list; term frequency per token per chunk is stored. No IDF/document-frequency weighting is computed or stored here.
- **Edge-kind derivation (DB mode only):** each staged dependency's raw `DEPENDENCY_TYPE` is looked up in `ZDCCRT_EKIND_MAP` to derive a normalized `EDGE_KIND` and an `IS_CALL_LIKE` flag; an unrecognized type defaults to `edge_kind = 'reference'`, `is_call_like = false`.
- **DDIC scope:** the DDIC section (domains, data elements, tables, structures, table types) is package-scoped, not object-scoped — one shared snapshot per package, queried once via TADIR, independent of which specific objects were extracted in a given run.
- **Transaction handling (DB mode):** all writes to the six final tables for one run happen inside a single commit/rollback unit; on any failure, the run rolls back and `ZDCCRT_STG_RUN-STATUS` is set to `FAILED` with an error message in its own separate LUW.
- **ALV review screen:** before writing output, extracted objects are shown in an ALV grid (Include/Checked, Object, TRs Appeared In, Most Recent TR columns) for developer confirmation; only the Include checkbox is editable.

### Real numbers from an actual run (DB mode, package ZZBA91, FULL run)

| Table | Rows |
|---|---|
| ZDCCRT_STG_OBJ (staged) | 23 |
| ZDCCRT_STG_DEP (staged) | 474 |
| ZDCCRT_STG_DDIC (staged) | 263 |
| ZDCCRT_OBJECTS (loaded) | 23 |
| ZDCCRT_OBJ_CALLS (loaded) | 474 |
| ZDCCRT_OBJ_DDREF (loaded) | 63 |
| ZDCCRT_CHUNKS (loaded) | 207 |
| ZDCCRT_CHUNK_TOK (loaded) | 15,568 |
| ZDCCRT_DDIC_OBJ (loaded) | 263 |

Object type breakdown for the same package (file mode run): 17 CLASS, 5 PROGRAM, 1 FUNCTION_MODULE (23 total).

---

## 2. Include ZLLD_EXTRACT_TO_DB_F01

**Purpose:** Contains the `EXTRACT_TO_DB` logic (chunking, tokenization, edge-kind derivation, final-table loading) used by ZLLD_PACKAGE_EXTRACTOR's database output mode. Included directly into that report (`INCLUDE zlld_extract_to_db_f01.`) rather than implemented as a separate function module.

### Inputs

Called as `PERFORM extract_to_db USING iv_run_id CHANGING ev_status ev_message.` — takes the RUN_ID of an already-staged extraction run (written by ZLLD_PACKAGE_EXTRACTOR) and returns a status (`PROCESSED` or `FAILED`) and a message.

### Outputs

Rows in the six final query-ready tables (see table list above); updates `ZDCCRT_STG_RUN-STATUS`/`ERROR_MESSAGE`; a per-table row-count summary written to the list.

### Tables read

ZDCCRT_STG_RUN, ZDCCRT_STG_OBJ, ZDCCRT_STG_DEP, ZDCCRT_STG_DDIC, ZDCCRT_EKIND_MAP, ZDCCRT_CONFIG, ZDCCRT_CHUNKS (for deletion), ZDCCRT_OBJECTS (for deletion), ZDCCRT_DDIC_OBJ (for deletion)

### Tables written

ZDCCRT_STG_RUN (status update), ZDCCRT_OBJECTS, ZDCCRT_OBJ_CALLS, ZDCCRT_OBJ_DDREF, ZDCCRT_CHUNKS, ZDCCRT_CHUNK_TOK, ZDCCRT_DDIC_OBJ

### Key logic

- Same run-type branching as the calling report: a FULL run deletes and rebuilds every row for the package; an INCREMENTAL run deletes and rebuilds rows only for touched or removed objects.
- DDIC snapshot is always fully replaced for the package on every run (FULL or INCREMENTAL), independent of object-level incremental logic.
- Config values (`STOPWORDS`, `CHUNK_MERGE_MIN_LINES`) are read from `ZDCCRT_CONFIG` with safe fallback defaults if a key is missing.

---

## 3. Report ZLLD_GET_CANDIDATES

**Purpose:** Given a requirement (as a local text file) and one or more packages, searches previously extracted/loaded objects, scores and ranks them by relevance, and exports a candidate file for downstream review by the gem.

### Inputs (selection screen)

| Parameter | Type | Default | Notes |
|---|---|---|---|
| P_REQF | STRING, obligatory | blank | Path to a local `.txt` file containing the requirement text; F4 opens a file picker |
| S_PACK | Select-option on ZDCCRT_OBJECTS-ZPACKAGE, obligatory, single value only | blank | Package(s) to search |
| P_NOTE | STRING | blank | Optional developer scoping note (typically an object name) |
| P_CONF | Listbox: blank / UNSURE / LIKELY / CERTAIN | blank | Confidence tier for P_NOTE; required if P_NOTE is given, must be blank if not |
| P_TOPN | Integer | 5 | Number of top candidates to export |
| P_FILE | STRING, obligatory | `C:\temp\` | Output folder for the candidate file; F4 opens a folder browser |

### Outputs

One `LLD_CANDIDATES_<packages>.TXT` file, containing a metadata block plus a DDIC section plus one block per exported candidate object (source code, dependency JSON, score breakdown). Written via `GUI_DOWNLOAD`. A run summary (candidates exported, likely-new-object flag, disagreement count) is also written to the list.

### Tables read

ZDCCRT_OBJECTS, ZDCCRT_OBJ_CALLS, ZDCCRT_OBJ_DDREF (indirectly, via OBJ_CALLS), ZDCCRT_DDIC_OBJ, ZDCCRT_CHUNKS, ZDCCRT_CHUNK_TOK, ZDCCRT_EKIND_MAP, ZDCCRT_CONFIG

### Tables written

None — read-only against all query-ready tables.

### Key logic

- **Scope resolution:** every object in the selected package(s) is loaded, with a bidirectional adjacency graph built from `ZDCCRT_OBJ_CALLS` restricted to call-like edge kinds (per `ZDCCRT_EKIND_MAP`), and an object→DDIC-table index built from `ZDCCRT_OBJ_DDREF` scoped to `DDIC_TYPE = 'TABL'` only.
- **Semantic scoring:** TF-IDF cosine similarity. IDF is computed at query time over the scoped chunk/token set (not stored). A requirement vector is built from the same tokenizer used at extraction time. Each object's semantic score is the maximum cosine similarity across any one of its chunks (not an average).
- **Scoping-note resolution:** P_NOTE is matched against known object names in three passes — exact match, then case-insensitive, then substring (either direction). An unresolved note is recorded as a disagreement, not an error.
- **Structural scoring:** seeds are (a) the top 3 objects by semantic score whose score clears `SEED_MIN_SEMANTIC_SCORE`, and (b) the resolved scoping-note object, if any (ungated by the semantic threshold). Structural score is the maximum of: hop-distance decay from a seed (1.0 at the seed itself, 0.6 at 1 hop, 0.3 at 2 hops, via BFS over the adjacency graph) or a flat bonus for sharing a DDIC table with a seed.
- **Base score:** `base_score = SEMANTIC_WEIGHT × semantic + STRUCTURAL_WEIGHT × structural` (weights from `ZDCCRT_CONFIG`).
- **Tier adjustment:** applied only when a scoping note resolves.
  - CERTAIN: hard filter — only the named object and objects within a configured hop radius are considered candidates at all.
  - LIKELY: named object's score is multiplied by `LIKELY_NAMED_MULTIPLIER`; its 1-hop neighbors by `LIKELY_HOP1_MULTIPLIER`.
  - UNSURE: named object's score is multiplied by `UNSURE_NAMED_MULTIPLIER` (no separate neighbor boost).
  - No note: `final_score = base_score` for every object.
- **Disagreement detection:** two independent checks —
  1. Under CERTAIN, if the top-scoring object outside the hard filter beats the best in-filter object by more than a configured margin.
  2. Under LIKELY/UNSURE, if the tier-adjusted top result differs from the top result without any tier adjustment applied.
- **New-object detection:** flags `LIKELY_NEW_OBJECT` (using `base_score`, never the tier-adjusted `final_score`) if either (a) the top candidate's base score is below `NEW_OBJECT_SCORE_FLOOR`, or (b) the spread between the top and 5th-ranked candidate's base score is below `NEW_OBJECT_SPREAD_THRESHOLD`.
- **Export:** top P_TOPN candidates by adjusted score are written, each with its full reconstructed source (chunks concatenated in order, not just the matching fragment) and a reconstructed flat dependency JSON list.

### Candidate file structure (as exported)

```
=== RETRIEVAL METADATA ===
REQUIREMENT_FILE: <path>
REQUIREMENT: <full requirement text>
PACKAGES: <comma-separated>
SCOPING_NOTE: <text or (none)>
CONFIDENCE: <tier or (none)>
LIKELY_NEW_OBJECT: <X or blank>
LIKELY_NEW_OBJECT_REASON: <text or (none)>
DISAGREEMENTS: <count>
  - [<kind>] <message>
=== END RETRIEVAL METADATA ===

RUN_TYPE: INCREMENTAL
PACKAGE: <package>
EXTRACTED_AT: <timestamp>
REMOVED_OBJECTS: NONE

--- DDIC ---
<JSON: {"PACKAGES":[{"PACKAGE":...,"DOMAIN":[...],"DATA_ELEMENT":[...],"TABLE":[...],"STRUCTURE":[...],"TABLE_TYPE":[...]}]}>

=== OBJECT: <name> ===
TYPE: <CLAS|PROG|FUNC>
PACKAGE: <package>
FINAL_SCORE: <4 decimals>
SEMANTIC_SCORE: <4 decimals>
STRUCTURAL_SCORE: <4 decimals>
TIER_ADJUSTMENT_APPLIED: <label>

--- SOURCE ---
<full reconstructed source>

--- DEPENDENCIES ---
<JSON: [{"TYPE":...,"NAME":...,"SIGNATURE":{...}?}, ...]>
=== END OBJECT ===
```
(repeated per exported candidate)

### Config values read (ZDCCRT_CONFIG)

| Key | Default | Purpose |
|---|---|---|
| STOPWORDS | (list) | Noise words filtered before tokenizing |
| CHUNK_MERGE_MIN_LINES | 3 | Minimum chunk size in lines |
| SEMANTIC_WEIGHT | 0.30 | Weight of semantic score in base_score |
| STRUCTURAL_WEIGHT | 0.70 | Weight of structural score in base_score |
| SEED_MIN_SEMANTIC_SCORE | 0.30 | Minimum semantic score for an object to qualify as an automatic structural seed |
| NEW_OBJECT_SCORE_FLOOR | 0.20 | base_score floor below which LIKELY_NEW_OBJECT is flagged |
| NEW_OBJECT_SPREAD_THRESHOLD | 0.05 | Minimum top-vs-5th-candidate score spread before LIKELY_NEW_OBJECT is flagged |
| LIKELY_NAMED_MULTIPLIER | 1.5 | LIKELY tier multiplier for the named object |
| LIKELY_HOP1_MULTIPLIER | 1.2 | LIKELY tier multiplier for the named object's 1-hop neighbors |
| UNSURE_NAMED_MULTIPLIER | 1.1 | UNSURE tier multiplier for the named object |

### Constants not in ZDCCRT_CONFIG (fixed in code)

| Constant | Value | Purpose |
|---|---|---|
| gc_top_seed_objects | 3 | Number of top-semantic candidates considered for automatic seeding |
| gc_min_chunk_length | 15 | Minimum chunk character length to be scored at all |
| gc_structural_seed | 1.0 | Structural score at a seed object itself |
| gc_structural_1hop | 0.6 | Structural score at 1 hop from a seed |
| gc_structural_2hop | 0.3 | Structural score at 2 hops from a seed |
| gc_structural_shared_tbl | 0.4 | Structural score bonus for sharing a DDIC table with a seed |
| gc_certain_hop_radius | 1 | Hop radius used for CERTAIN tier's hard filter |
| gc_certain_conflict_margin | 0.15 | Margin used to flag a CERTAIN-tier disagreement |
| gc_unadjusted_ranking_size | 5 | Size of the unadjusted-ranking baseline used for disagreement comparison |

### Real numbers from an actual run

Requirement: "Get criteria box data" (SD_FORM_02-660), packages ZZBA91 + ZZBA91_API, scoping note `ZCL_CBA_CRITERION_POPUP`, confidence LIKELY:

| Object | FINAL_SCORE | SEMANTIC_SCORE | STRUCTURAL_SCORE |
|---|---|---|---|
| ZCL_CBA_CRITERION_POPUP | 1.2000 | 0.3334 | 1.0000 |
| ZCL_CBA_FORMULA_CLEANING | 0.0606 | 0.2019 | 0.0000 |
| ZCL_CBA_SHORTEST_PATH | 0.0564 | 0.1879 | 0.0000 |
| ZCL_CBA_IMPORT_MODEL | 0.0444 | 0.1482 | 0.0000 |
| ZPCBA91R_IMPORT_ALIAS_MDL | 0.0432 | 0.1439 | 0.0000 |

Result: `LIKELY_NEW_OBJECT` = blank (not flagged); 0 disagreements.

---

## 4. Report ZLLD_SEED_CONFIG_TABLES

**Purpose:** One-time (idempotent) seeding of `ZDCCRT_EKIND_MAP` and `ZDCCRT_CONFIG`. Run once after the tables exist, before the first extraction or retrieval run; safe to re-run at any time.

### Inputs

None (no selection screen).

### Outputs

Rows written to `ZDCCRT_EKIND_MAP` (18 rows) and `ZDCCRT_CONFIG` (10 rows). A confirmation line is written to the list.

### Tables read

None.

### Tables written

ZDCCRT_EKIND_MAP, ZDCCRT_CONFIG (both via MODIFY, so re-running updates existing rows rather than duplicating them).

### Key logic

Two forms: `SEED_EDGE_KIND_MAP` (loads the 18-row DEPENDENCY_TYPE → EDGE_KIND / IS_CALL_LIKE mapping) and `SEED_CONFIG` (loads the STOPWORDS list plus the 9 numeric config keys used by ZLLD_GET_CANDIDATES). Both are simple loops over an inline VALUE table calling MODIFY per row.

### ZDCCRT_EKIND_MAP seed data (18 rows)

| DEPENDENCY_TYPE | EDGE_KIND | IS_CALL_LIKE |
|---|---|---|
| METH | calls_method | X |
| OM | calls_method | X |
| FUNC | calls_function | X |
| CLAS | class_ref | |
| STRU | structure_ref | |
| MESS | message_ref | |
| OA | reference | |
| DTEL | data_element_ref | |
| INCL | includes | |
| INTF | implements | |
| DGT | generic_type_ref | |
| FUGR | function_group_ref | |
| PROG | program_ref | |
| TABL | table_ref | |
| MSAG | message_class_ref | |
| TTYP | table_type_ref | |
| TYPE | type_ref | |
| TRAN | transaction_ref | |

---

## 5. Report ZLLD_EMPTY_TABLES

**Purpose:** One-shot utility to empty the 12 run-data `ZDCCRT_*` tables before a fresh test run, leaving the 2 seed/config tables (`ZDCCRT_EKIND_MAP`, `ZDCCRT_CONFIG`) untouched.

### Inputs

None (no selection screen). Displays a confirmation popup (Yes/Cancel, default Cancel) before deleting anything.

### Outputs

A per-table deleted-row count and a total, written to the list.

### Tables read

None.

### Tables written (DELETE, unconditional, all clients/packages/runs)

ZDCCRT_SYNC_LOG, ZDCCRT_SYNC_OBJ, ZDCCRT_STG_RUN, ZDCCRT_STG_OBJ, ZDCCRT_STG_DEP, ZDCCRT_STG_DDIC, ZDCCRT_OBJECTS, ZDCCRT_OBJ_CALLS, ZDCCRT_OBJ_DDREF, ZDCCRT_DDIC_OBJ, ZDCCRT_CHUNKS, ZDCCRT_CHUNK_TOK

### Key logic

One generic form (`EMPTY_ONE_TABLE`) performs a dynamic `DELETE FROM (iv_tabname)` for each of the 12 table names in a fixed list, since all 12 share the same "delete everything, report the count" shape.

---

## 6. Z-Tables — Full Field Reference

### ZDCCRT_SYNC_LOG — "one row per package, its sync state"

| Field | Key | Data Element | Type | Length |
|---|---|---|---|---|
| MANDT | X | MANDT | CLNT | 3 |
| ZPACKAGE | X | DEVCLASS | CHAR | 30 |
| LAST_SYNC_TS | | TIMESTAMPL | DEC | 21,7 |
| LAST_TRKORR_HWM | | TRKORR | CHAR | 20 |
| RUN_TYPE | | CHAR4 | CHAR | 4 |
| OBJECT_COUNT | | INT4 | INT4 | 10 |
| STATUS | | CHAR10 | CHAR | 10 |

One row per package; no row = never synced = next run is FULL.

### ZDCCRT_SYNC_OBJ — "object list from the last successful run, for deletion detection"

| Field | Key | Data Element | Type | Length |
|---|---|---|---|---|
| MANDT | X | MANDT | CLNT | 3 |
| ZPACKAGE | X | DEVCLASS | CHAR | 30 |
| OBJ_TYPE | X | TROBJTYPE | CHAR | 4 |
| OBJ_NAME | X | TROBJ_NAME | CHAR | 120 |
| LAST_SEEN_TS | | TIMESTAMPL | DEC | 21,7 |

Full snapshot replaced after every successful run (not append-only). Values used for OBJ_TYPE: PROG/CLAS/FUNC only.

### ZDCCRT_STG_RUN — "one row per DB-mode extraction run"

| Field | Key | Data Element | Type | Length |
|---|---|---|---|---|
| MANDT | X | MANDT | CLNT | 3 |
| RUN_ID | X | SYSUUID_X16 | RAW | 16 |
| ZPACKAGE | | DEVCLASS | CHAR | 30 |
| RUN_TYPE | | CHAR4 | CHAR | 4 |
| EXTRACTED_AT | | TIMESTAMPL | DEC | 21,7 |
| REMOVED_OBJECTS | | Z_STRING | STRING | - |
| STATUS | | CHAR12 | CHAR | 12 |
| ERROR_MESSAGE | | Z_STRING | STRING | - |

RUN_ID is the join key across all four staging tables. STATUS values: EXTRACTED / PROCESSED / FAILED.

### ZDCCRT_STG_OBJ — "one row per staged object"

| Field | Key | Data Element | Type | Length |
|---|---|---|---|---|
| MANDT | X | MANDT | CLNT | 3 |
| RUN_ID | X | SYSUUID_X16 | RAW | 16 |
| OBJ_NAME | X | SEOCLSNAME | CHAR | 30 |
| OBJ_TYPE | | CHAR4 | CHAR | 4 |
| ZPACKAGE | | DEVCLASS | CHAR | 30 |
| SOURCE | | Z_STRING | STRING | - |

### ZDCCRT_STG_DEP — "one row per dependency entry"

| Field | Key | Data Element | Type | Length |
|---|---|---|---|---|
| MANDT | X | MANDT | CLNT | 3 |
| RUN_ID | X | SYSUUID_X16 | RAW | 16 |
| OBJ_NAME | X | SEOCLSNAME | CHAR | 30 |
| DEPENDENCY_SEQ | X | INT4 | INT4 | 10 |
| DEPENDENCY_TYPE | | Z_STRING | STRING | - |
| DEPENDENCY_NAME | | Z_STRING | STRING | - |
| SIGNATURE_JSON | | Z_STRING | STRING | - |

### ZDCCRT_STG_DDIC — "one row per DDIC object referenced by the package"

| Field | Key | Data Element | Type | Length |
|---|---|---|---|---|
| MANDT | X | MANDT | CLNT | 3 |
| RUN_ID | X | SYSUUID_X16 | RAW | 16 |
| DDIC_TYPE | X | CHAR4 | CHAR | 4 |
| DDIC_NAME | X | CHAR30 | CHAR | 30 |
| DETAIL_JSON | | Z_STRING | STRING | - |

DDIC_TYPE values: DOMA / DTEL / TABL / STRU / TTYP.

### ZDCCRT_OBJECTS — "one row per final, query-ready object"

| Field | Key | Data Element | Type | Length |
|---|---|---|---|---|
| MANDT | X | MANDT | CLNT | 3 |
| OBJ_NAME | X | SEOCLSNAME | CHAR | 30 |
| OBJ_TYPE | | CHAR4 | CHAR | 4 |
| ZPACKAGE | | DEVCLASS | CHAR | 30 |

### ZDCCRT_OBJ_CALLS — "one row per dependency edge, every DEPENDENCY_TYPE"

| Field | Key | Data Element | Type | Length |
|---|---|---|---|---|
| MANDT | X | MANDT | CLNT | 3 |
| SOURCE_OBJECT | X | SEOCLSNAME | CHAR | 30 |
| TARGET_OBJECT | X | CHAR60_CP | CHAR | 60 |
| DEPENDENCY_TYPE | X | CHAR10 | CHAR | 10 |
| EDGE_KIND | | Z_STRING | STRING | - |
| SIGNATURE_JSON | | Z_STRING | STRING | - |

Key is (SOURCE_OBJECT, TARGET_OBJECT, DEPENDENCY_TYPE); written via MODIFY.

### ZDCCRT_OBJ_DDREF — "structured index of DDIC-type dependencies"

| Field | Key | Data Element | Type | Length |
|---|---|---|---|---|
| MANDT | X | MANDT | CLNT | 3 |
| OBJ_NAME | X | SEOCLSNAME | CHAR | 30 |
| DDIC_TYPE | X | CHAR4 | CHAR | 4 |
| DDIC_OBJECT_NAME | X | CHAR30 | CHAR | 30 |

Populated for every DDIC-shaped dependency type (DOMA/DTEL/TABL/STRU/TTYP), not only TABL.

### ZDCCRT_DDIC_OBJ — "package-wide DDIC catalog, final form of ZDCCRT_STG_DDIC"

| Field | Key | Data Element | Type | Length |
|---|---|---|---|---|
| MANDT | X | MANDT | CLNT | 3 |
| ZPACKAGE | X | DEVCLASS | CHAR | 30 |
| DDIC_TYPE | X | CHAR4 | CHAR | 4 |
| DDIC_NAME | X | CHAR30 | CHAR | 30 |
| DETAIL_JSON | | Z_STRING | STRING | - |

Fully replaced for the package on every run (FULL or INCREMENTAL).

### ZDCCRT_CHUNKS — "one row per source chunk"

| Field | Key | Data Element | Type | Length |
|---|---|---|---|---|
| MANDT | X | MANDT | CLNT | 3 |
| CHUNK_ID | X | SYSUUID_X16 | RAW | 16 |
| OBJ_NAME | | SEOCLSNAME | CHAR | 30 |
| CHUNK_INDEX | | I | INT4 | 10 |
| CHUNK_TEXT | | Z_STRING | STRING | - |

CHUNK_ID alone is the unique key; OBJ_NAME is not part of the key.

### ZDCCRT_CHUNK_TOK — "inverted index: one row per distinct token per chunk"

| Field | Key | Data Element | Type | Length |
|---|---|---|---|---|
| MANDT | X | MANDT | CLNT | 3 |
| TOKEN | X | CHAR40 | CHAR | 40 |
| CHUNK_ID | X | SYSUUID_X16 | RAW | 16 |
| TERM_FREQUENCY | | I | INT4 | 10 |

No document-frequency/IDF column; IDF is computed at query time by the retrieval report.

### ZDCCRT_EKIND_MAP — "maintainable DEPENDENCY_TYPE to EDGE_KIND mapping"

| Field | Key | Data Element | Type | Length |
|---|---|---|---|---|
| MANDT | X | MANDT | CLNT | 3 |
| DEPENDENCY_TYPE | X | CHAR10 | CHAR | 10 |
| EDGE_KIND | | Z_STRING | STRING | - |
| IS_CALL_LIKE | | FLAG | CHAR | 1 |

18 rows seeded (see ZLLD_SEED_CONFIG_TABLES section above). Unmapped type defaults to edge_kind = 'reference', is_call_like = false.

### ZDCCRT_CONFIG — "small tunable-without-a-transport key/value store"

| Field | Key | Data Element | Type | Length |
|---|---|---|---|---|
| MANDT | X | MANDT | CLNT | 3 |
| CONFIG_KEY | X | CHAR40 | CHAR | 40 |
| CONFIG_VALUE | | Z_STRING | STRING | - |

10 rows seeded (see ZLLD_GET_CANDIDATES config table above).

---

## 7. Gem Instructions (LLD Gem Instruction.txt) + Excel Template

### Role and scope (gem's own terms)

"An Expert SAP ABAP Low-Level Design (LLD) Assistant" specializing in: SAP ABAP 7.4+/ECC custom object structures; structural anchor identification (not line-number based); requirement-to-code traceability; conservative, evidence-grounded design proposals; disagreement surfacing between developer intent and retrieved evidence; Excel LLD proposal reporting.

Explicitly stated scope boundary: "You do not perform retrieval, scoring, or ranking. That has already happened before this file reached you. Your job is reasoning over already-narrowed evidence, not searching for it." The supplied candidate file is stated as the only source of code/dependency truth.

### Section index (numbered sections in the instruction file)

| Section | Purpose (one line) |
|---|---|
| 1. Role | Defines the gem's identity, specialization, and scope boundary (reasoning, not retrieval) |
| 2. Knowledge Base | Identifies the one file in its knowledge base (the Excel template) and its role as layout/formula source only, not a reasoning source |
| 3. Input Format | Defines the two-part candidate file format it expects (header block + object blocks) |
| 4. Input Validation | Defines required responses when no file, or a header-less file, is provided |
| 5. Strict Evidence Rule | Lists what the gem must never do — invent objects/methods/calls, infer table-sharing from score alone, silently resolve disagreements, design new-object internals |
| 6. Reasoning Workflow | Defines the fixed sequence: read header → surface disagreements → check new-object flag → read objects → propose anchor → stop for developer response |
| 7. Chunking (Conditional) | Defines when to apply 500-line physical chunking to a large candidate file, and when not to |
| 8. Anchor Proposal Structure | Defines the required output format for a normal (non-new-object) proposal, and how to weigh FINAL_SCORE/SEMANTIC_SCORE/STRUCTURAL_SCORE/TIER_ADJUSTMENT_APPLIED |
| 9. New-Object Case | Defines the required output format when LIKELY_NEW_OBJECT = true, and what the gem must not do (sketch a new object's design) |
| 10. Handling Disagreement Flags | Defines the required per-flag response structure and the rule against resolving a disagreement by default to either side |
| 11. Remediation/Sample Code | Defines the explicit commands that trigger a code snippet, and its required format/labeling |
| 12. Proposal ID and Cumulative State | Defines the LLD-NNN numbering scheme and the fields tracked per proposal for later Excel export |
| 13. Generate Excel Command | Defines the explicit commands that trigger Excel export, and the export procedure/validation steps |
| 14. Continue Command | Defines what "continue" means in a chunking context vs. a completed-proposal context |
| 15. Reset Command | Defines the single explicit command that clears cumulative proposal state |

### Explicit commands the gem responds to

| Command(s) | Effect |
|---|---|
| GENERATE SAMPLE CODE / SHOW PROPOSED CODE / DRAFT THE CHANGE | Produces an illustrative ABAP code snippet for the current proposal, labeled as illustrative, not production-ready |
| GENERATE EXCEL FILE / GENERATE EXCEL / CREATE EXCEL / EXPORT EXCEL / GENERATE REPORT | Triggers Excel export of all cumulative proposals; does not process a new candidate file or reset state |
| CONTINUE | Advances to the next 500-line chunk if mid-chunking; otherwise asks whether a new requirement/candidate file is coming |
| RESET | Clears all cumulative proposals and resets Proposal ID numbering to LLD-001 |

### Output structure — Anchor Proposal (Section 8, used when LIKELY_NEW_OBJECT = false and no unresolved disagreement blocks it)

| Field | Content |
|---|---|
| Requirement | Brief restatement |
| Proposed Object | Object name, type, package |
| Anchor Point | Structural anchor description (e.g. "in method X of class Y, after Z") — never a line number |
| Proposed Change | Description of what should happen at the anchor (not full code, unless explicitly requested) |
| Confidence | High / Medium / Low, self-stated, using the four retrieval-metadata fields as context, never as a substitute for justification against the actual source |
| Rationale | Specific lines/methods/calls/table usage that justify the anchor |
| Disagreement Status | States whether the proposal follows the developer's note, the independent retrieval result, or a third option, and why |
| Alternative Candidates Considered | 1-2 other candidates and why they were not chosen |

### Output structure — New-Object Case (Section 9, used when LIKELY_NEW_OBJECT = true)

| Field | Content |
|---|---|
| Requirement | Brief restatement |
| Finding | "No existing anchor identified." |
| Reason | Restates the header's stated reason |
| Closest Candidates | Top 2-3 candidates and their scores, for reference only |
| Recommendation | States the requirement likely needs a new object; explicitly declines to design its structure, and routes to manual design |

### Output structure — Disagreement Handling (Section 10, applied per flag)

1. State what the flag says, in plain terms.
2. State what each side's implication would be if followed.
3. State the gem's own read on which is more likely correct, grounded in the supplied source/dependencies.
4. Explicitly ask the developer to confirm before finalizing, if the disagreement would change which object the anchor lands in.

### Score-weighting rules stated in the gem instructions (Section 8)

- STRUCTURAL_SCORE is treated as the more trustworthy component (reflects graph proximity and, when present, DDIC/table relationships).
- SEMANTIC_SCORE is treated as weak corroboration only — a high SEMANTIC_SCORE alone must not raise stated confidence if STRUCTURAL_SCORE is low or zero.
- A table-sharing claim as supporting evidence must be verified directly against the candidate's own DEPENDENCIES entries (a real TABL-type entry naming the same table) — never inferred from a high STRUCTURAL_SCORE alone.
- A candidate whose high FINAL_SCORE is driven mainly by a scoping-note tier boost is not to be treated as more independently confirmed than an unboosted candidate with a comparable score.

### Candidate file input format expected by the gem (Section 3)

**Part A (header block):** original requirement (plain English); PACKAGES the requirement is scoped to; developer's scoping note and confidence tier (certain/likely/unsure/none); disagreement flags with explanations; a likely_new_object flag (true/false) with stated reason.

**Part B (one or more object blocks):** `=== OBJECT: <name> ===`, TYPE, PACKAGE, FINAL_SCORE, SEMANTIC_SCORE, STRUCTURAL_SCORE, TIER_ADJUSTMENT_APPLIED, `--- SOURCE ---` (ABAP source or retrieved chunks), `--- DEPENDENCIES ---` (flat JSON list of `{TYPE, NAME, SIGNATURE?}` entries), `=== END OBJECT ===`.

### Excel Template Structure (LLD_Dashboard_Template.xlsx)

Two sheets:

**"LLD Dashboard"**
- KPI cards: Total Proposals Logged, Anchor Proposals Made, New Object Recommendations, Disagreements Flagged, High/Medium/Low Confidence counts, Decisions Pending.
- A Confidence Breakdown table.
- All values are formula-driven, computed from the "LLD Proposals" sheet's data range — never hardcoded by the gem.

**"LLD Proposals"**
- Row 1: instructions banner. Row 2: header row. Row 3 onward: one row per proposal (no example row).
- Columns: Proposal ID, Requirement, Proposed Object, Anchor Point, Confidence, Disagreement Status, New Object Flag, Developer Decision, Notes.
- Confidence column: data-validation dropdown, values exactly High / Medium / Low.
- New Object Flag column: data-validation dropdown, values exactly Yes / No.
- Developer Decision column: defaults to "Pending"; changes only on an explicit developer statement in conversation.

### Per-proposal fields tracked internally (Section 12, populate the Proposals sheet row)

Proposal ID (LLD-NNN sequential), Requirement (as stated in the candidate file header), Proposed Object (blank if new-object case), Anchor Point (blank if new-object case), Confidence (High/Medium/Low), Disagreement Status (None, or a restatement + resolution), New Object Flag (Yes/No), Developer Decision (Pending by default), Notes.
