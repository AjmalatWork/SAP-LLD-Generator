# Step 2 — Structural graph + embedding store

Parses the real LLD extraction file format step 1 (`ZLLD_PACKAGE_EXTRACTOR`)
produces into a persistent structural dependency graph (Postgres) and a
persistent vector store of embedded code chunks (pgvector), so both can be
queried by later phases. See the original brief for full context; this
covers step 2 only — no retrieval/tiering logic, no LLM, no UI.

Runs fully offline (after first-time package/model download) on a local
machine. No external services.

**Format note:** this parses the *real* format (file-level `RUN_TYPE`/
`PACKAGE`/`REMOVED_OBJECTS` header, a shared `--- DDIC ---` section, and each
object's `DEPENDENCIES` as a flat list of typed entries with real SIGNATURE
enrichment) — not the earlier simplified `{"calls": [...], "tables_used":
[...]}` mock schema step 2 was originally built and tested against before a
real extraction file existed. See `reports/` for the update history.

## 1. Start the database

Requires Docker Desktop.

```bash
cd sap_lld_step2
docker compose up -d
```

This starts Postgres 16 with the `pgvector` extension on `localhost:5432`
and applies `sql/init.sql` (schema) on first startup. Credentials/port are
set in `docker-compose.yml` defaults (`lld` / `lld` / `sap_lld`); override via
a `.env` file (see `.env.example`) if needed — e.g. if port 5432 is already
taken by another local Postgres instance, set `POSTGRES_PORT=5433` and update
`LLD_DATABASE_URL` to match before running the pipeline/tests:

```bash
export LLD_DATABASE_URL="postgresql://lld:lld@localhost:5433/sap_lld"   # macOS/Linux
$env:LLD_DATABASE_URL = "postgresql://lld:lld@localhost:5433/sap_lld"   # Windows PowerShell
```

To reset the database entirely (drops all data):

```bash
docker compose down -v
```

## 2. Install Python dependencies

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
pip install -e .
```

The first run will download the embedding model (`sentence-transformers/
all-MiniLM-L6-v2` by default, a few hundred MB) — this needs internet once;
after that it's cached locally and everything runs offline.

## 3. Get an extraction file

**A real one:** run `ZLLD_PACKAGE_EXTRACTOR` (step 1) against a package. Its
output is directly usable here — no conversion needed. Real files come out
of `GUI_DOWNLOAD` in the SAP GUI's local codepage (confirmed cp1252 on the
one real file tested so far, not UTF-8) — the pipeline detects this
automatically (tries UTF-8 first, falls back to cp1252). **Real extraction
files contain genuine business/proprietary content — never commit one to
this repo** (see `.gitignore`'s `data/LLD_EXTRACT_*` pattern); keep it local.

**A synthetic one**, for fast repeatable testing without a real SAP system:

```bash
python -m lld_step2.mock_generator --count 18 --seed 42 --package ZMOCK_PKG --out data/mock_extraction.txt
```

Emits the same real format (header, shared DDIC section, real per-object
DEPENDENCIES shape with `METH`/`OM`/`INTF`/`TABL`/etc. entries and plausible
SIGNATURE enrichment) — 18 synthetic objects with a realistic mix of object
references, DDIC references, a few 3+ hop chains, and at least one
dependency-free object. `lld_step2.mock_generator.generate_incremental()`
(Python API, not yet wired into the CLI) builds a small `INCREMENTAL`-run
file from an existing `generate()` call's specs — one object mutated, one
listed in `REMOVED_OBJECTS`, everything else absent — for testing the
incremental loader path without a second real sync; see
`tests/test_real_format_loader.py` for how it's used.

## 4. Run the pipeline

```bash
python -m lld_step2.pipeline data/mock_extraction.txt
```

Reads the file's `RUN_TYPE` and branches accordingly:

- **`FULL`** — wipes and reloads everything scoped to the file's `PACKAGE`
  (`objects`, `object_calls`, `object_uses_table`, `code_chunks`,
  `ddic_objects`), then loads from scratch. Same semantics as re-running the
  whole pipeline used to be, just now explicitly package-scoped rather than
  assumed to be the whole database.
- **`INCREMENTAL`** — first deletes every object named in `REMOVED_OBJECTS`
  (and cleans up any other object's edges pointing at it), then upserts only
  the objects actually present in this file's `=== OBJECT ===` blocks
  (delete-then-insert that one object's own rows, same idempotent pattern as
  before). Everything else already in the database — anything not mentioned
  in this file and not in `REMOVED_OBJECTS` — is left completely untouched.
  `ddic_objects` entries are only ever added/updated, never deleted by an
  incremental run (only `REMOVED_OBJECTS` drives deletion, and it doesn't
  touch this table) — an incremental file's shared DDIC section may
  legitimately cover only a subset of the package, and that must not be read
  as "everything else was removed."

Malformed input (including a missing/invalid `RUN_TYPE`) exits with a clear
message rather than a stack trace or a silent default.

## 5. Inspect the graph

```bash
python -m lld_step2.cli inspect ZCL_000_CORE
python -m lld_step2.cli reachable ZCL_000_CORE --hops 2
```

`inspect` prints direct callers, direct callees, and tables used for an
object. `reachable` runs a recursive CTE to find everything reachable from an
object within N hops.

## 6. Run the tests

```bash
pytest
```

Parser and chunking tests (`test_parser.py`, `test_chunking.py`) are pure
unit tests (no DB needed). The rest need Postgres running and are skipped
automatically if it isn't reachable:

- **`test_pipeline.py`** — generates a fresh mock file, runs the full
  pipeline, and checks object/dependency counts match the generator's known
  output, specific caller/callee/table edges are correct, re-running doesn't
  duplicate rows, multi-hop traversal matches a known chain, and vector
  similarity search returns chunks ordered by distance with raw text intact.
- **`test_real_format_loader.py`** — the FULL/INCREMENTAL loader semantics
  specifically (the part with genuine new risk, since step 2 had previously
  only ever been tested with a single full load): `OM`/`INTF`-type
  dependencies load with the correct `edge_kind`, a `TABL` reference
  resolves its field list against `ddic_objects` by name, a `FULL` run of
  one package never touches another package's rows, and a simulated
  incremental run deletes a `REMOVED_OBJECTS` entry, updates one object, and
  leaves every other object's rows byte-identical to before (checked via a
  direct snapshot comparison, not just "still present").
- **`test_retrieval.py`** — step 3's tests (see below).

## Embedding quality investigation tools

Ad hoc tools used to evaluate whether embedding-similarity is a trustworthy signal for
step 3, before committing to a semantic/structural scoring weight. Not part of the core
pipeline — see [`reports/embedding_investigation_report.md`](reports/embedding_investigation_report.md)
for the findings and recommendation.

- `python -m lld_step2.embedding_bench --model original [--min-chunk-length N]` — benchmarks
  an embedding source against related/unrelated chunk pairs derived mechanically from the
  graph tables (direct call edge or shared table = related). Works against the original
  model's stored embeddings, or any model already embedded via `alt_embedding.py`.
- `python -m lld_step2.alt_embedding --model <sentence-transformers model name>` — embeds all
  currently-loaded chunks with an alternate model and stores them in a separate
  `investigation_chunk_embeddings` table (created by this script), without touching
  `code_chunks.embedding`. Lets two models' embeddings be compared side by side.

## Step 3 — retrieval + tiering

Given a plain-English business requirement, the package(s) it belongs to, and an
optional developer scoping note, queries the step-2 graph and vector store and produces
a small, ranked, explainable candidate set — the input to a later LLM step (step 4, not
built yet). No LLM calls, no UI, deterministic scoring only. See
[`lld_step2/retrieval.py`](lld_step2/retrieval.py).

### Running it

```bash
python -m lld_step2.retrieval_cli "Allow partial order shipment when stock is insufficient" \
    --packages ZORDER_MGMT --scoping-note "ZCL_STOCK_MANAGER" --confidence likely --top-n 5 --export candidates.txt
```

- `--packages` is **required**: a comma-separated list of the package(s) the
  application being worked on actually lives in (e.g. `ZZBA91` or `ZZBA91,ZZBA92`).
  Retrieval only ever searches loaded data scoped to these packages — an application in
  a real system may share one database with thousands of unrelated packages, and a
  developer working on it knows which one or two they're in, so an unscoped search
  across everything loaded would return irrelevant cross-package noise at best and a
  false match at worst (see `reports/step3_real_data_validation_report.md` for a real
  instance of this: a "nonsense" requirement scored a genuine match purely because an
  unrelated second package happened to be loaded in the same database). Naming a
  package with no loaded data is a **hard error**, raised before any other processing —
  silently searching nothing on a typo'd package name would be a worse failure mode
  than failing loudly.
- `--scoping-note` is optional free text naming object(s)/flow(s) the developer suspects
  are relevant, resolved only against objects within `--packages`. `--confidence`
  (`certain` | `likely` | `unsure`) is required if and only if a scoping note is given.
- `--export PATH` writes the result to a text file in the same block format as a SAP
  extraction file, with a `=== RETRIEVAL METADATA ===` header (requirement, packages,
  scoping note, disagreements, `likely_new_object`) in front of the object blocks —
  this is what step 4 will read. Each object block's header also carries
  `FINAL_SCORE`/`SEMANTIC_SCORE`/`STRUCTURAL_SCORE`/`TIER_ADJUSTMENT_APPLIED` for that
  candidate, exposed as separate components (not just the blended score) since
  structural proximity and semantic similarity are not equally trustworthy on real
  data — see `reports/step3_real_data_validation_report.md`. Strip the header with
  `lld_step2.retrieval.extract_object_blocks()` before feeding the file to
  `lld_step2.parser.parse_extraction_file` — the parser tolerates these four
  retrieval-only fields on an object's header line (skips them; a real step 1
  extraction file never has them) but otherwise only understands `=== OBJECT: ===`
  blocks.

### Tunable constants

All in one place at the top of [`lld_step2/retrieval.py`](lld_step2/retrieval.py), each
commented with what it does:

| Constant | Default | Raise it to... | Lower it to... |
|---|---|---|---|
| `SEMANTIC_WEIGHT` / `STRUCTURAL_WEIGHT` | 0.30 / 0.70 | trust the embedding model's ranking more | lean harder on graph proximity (current evidence favors this) |
| `MIN_CHUNK_LENGTH` | 15 | exclude more short/boilerplate chunks from semantic scoring | let shorter chunks compete (risks the `ENDCLASS.`-style artifact) |
| `TOP_SEED_OBJECTS` | 3 | cast a wider net for structural seeding | anchor structural scoring more narrowly |
| `SEED_MIN_SEMANTIC_SCORE` | 0.67 | make seed eligibility stricter (fewer false anchors, but `likely_new_object` fires more readily) | let weaker semantic matches still seed structural scoring |
| `STRUCTURAL_SCORE_1_HOP` / `_2_HOP` / `_SHARED_TABLE` | 0.6 / 0.3 / 0.4 | widen how far structural relevance reaches | tighten it to closer neighbors only |
| `LIKELY_NAMED_MULTIPLIER` / `_HOP1_MULTIPLIER` | 1.5 / 1.2 | trust developer hints more | let independent signal override hints more easily |
| `UNSURE_NAMED_MULTIPLIER` | 1.1 | give `unsure` hints more pull | make them a near no-op |
| `CERTAIN_CONFLICT_MARGIN` | 0.15 | require a bigger gap before flagging a certain-hint conflict (fewer, higher-confidence flags) | flag conflicts more readily |
| `NEW_OBJECT_SCORE_FLOOR` | 0.35 | require a stronger match before trusting any candidate | let weaker top matches through without a new-object flag |
| `NEW_OBJECT_SPREAD_THRESHOLD` | 0.05 | require a clearer standout before trusting the ranking | tolerate flatter score distributions |

**`SEMANTIC_WEIGHT`/`STRUCTURAL_WEIGHT` and `SEED_MIN_SEMANTIC_SCORE` are both derived
from real measurements, not guesses** — see
[`lld_step2/embedding_bench.py`](lld_step2/embedding_bench.py) and
[`reports/embedding_investigation_report.md`](reports/embedding_investigation_report.md)
for the weighting, and [`reports/step3_scoring_fix_report.md`](reports/step3_scoring_fix_report.md)
for the semantic seed floor. **Re-run `embedding_bench.py`'s benchmark (and the
nonsense-vs-genuine calibration described in the fix report) against the first larger,
real extraction file before trusting either number long-term** — both were calibrated
against the small (15-object), densely-connected `ZORDER_MGMT_extract.txt` sample.

### A finding from building this: seed self-membership circularity (fixed)

While testing, the top-3 semantic-seed objects were found to unconditionally get
`structural_score = 1.0` just for being in that set — regardless of how weak their
actual semantic match was — which made `likely_new_object` mathematically unreachable
(confirmed: three nonsense requirements all scored a top `base_score` around 0.87–0.89,
nowhere near the 0.35 floor). Fixed by adding `SEED_MIN_SEMANTIC_SCORE` as a separate
gate on seed eligibility. Full calibration numbers and regression check in
[`reports/step3_scoring_fix_report.md`](reports/step3_scoring_fix_report.md).

## Configuration

All configurable via environment variables (see `.env.example`), read in
[`lld_step2/config.py`](lld_step2/config.py):

| Variable                | Default                                             | Purpose                                    |
|-------------------------|------------------------------------------------------|---------------------------------------------|
| `LLD_DATABASE_URL`      | `postgresql://lld:lld@localhost:5432/sap_lld`       | psycopg connection string                  |
| `LLD_EMBEDDING_MODEL`   | `sentence-transformers/all-MiniLM-L6-v2`            | any local sentence-transformers model name |
| `LLD_CHUNK_SIZE_LINES`  | `100`                                                | fallback fixed-size chunk window, in lines |

**Note on changing the embedding model:** the `code_chunks.embedding` column
is declared as `vector(384)` in `sql/init.sql` to match the default model's
output dimension. If you switch to a model with a different dimension,
update that column definition and re-create the table/volume
(`docker compose down -v && docker compose up -d`) to match.

## Design notes

- **Re-run behavior**: see "Run the pipeline" above for FULL vs INCREMENTAL
  semantics. Within either, `objects` rows are upserted by primary key
  (name); `object_calls`, `object_uses_table`, and `code_chunks` rows are
  deleted and re-inserted per object on every load, scoped to that object
  only — this keeps the loader idempotent without needing to diff individual
  rows.
- **Every dependency type becomes an `object_calls` edge** — not just calls
  in the everyday sense. The real extraction format's `DEPENDENCIES` list
  mixes genuine calls (`METH`/`OM`/`FUNC`) with includes/interfaces/DDIC/
  message/transaction references (`INCL`/`INTF`/`TABL`/`DOMA`/`MESS`/`TRAN`/
  etc.) all in one flat list, and this project loads all of them, tagged
  with the raw `dependency_type` plus a normalized `edge_kind` (see
  `graph_loader.EDGE_KIND_MAP`). Consumers that care specifically about
  call-graph proximity (`embedding_bench.py`'s "related" labeling,
  `retrieval.py`'s structural scoring) filter to `graph_loader.
  CALL_LIKE_EDGE_KINDS` rather than treating every row as a call.
- **`DOMA`/`DTEL`/`TABL`/`TTYP`-typed dependencies additionally get an
  `object_uses_table` edge** (by name only — full field-level detail lives
  in `ddic_objects`, joined by `(package, ddic_type, name)`). `STRU`-typed
  ones do not get this second edge, per the brief, even though `STRU` rows
  do exist in `ddic_objects` — only `object_calls` sees them.
- **`ddic_objects` is a shared, package-scoped side table**, not linked to
  `objects` by foreign key — a referenced DDIC object (e.g. a standard SAP
  table) may have no row there at all if it was never itself part of the
  package's own DDIC section. `get_tables_used()` degrades to an empty field
  list in that case rather than failing.
- **Chunking strategy** lives entirely in
  [`lld_step2/chunking.py`](lld_step2/chunking.py), isolated from the
  loader/embedding code, so the splitting heuristic can be improved (e.g. a
  smarter ABAP-aware splitter) without touching anything else.
- **Unresolved call targets**: `object_calls.target_object` is plain text,
  not a foreign key — a call to an object not present in the extraction file
  (e.g. a standard SAP BAPI) is stored as-is rather than failing.
- **Raw chunk text is always kept** alongside its embedding in `code_chunks`,
  since later phases need the actual code back, not just similarity scores.

## Out of scope

LLM/Gemini gem integration (step 4) and any UI. Step 1 (ABAP extraction),
step 2 (graph + embedding store), and step 3 (retrieval + tiering) are all
built. See the respective briefs for full detail.
