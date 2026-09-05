# Step 2 — Structural graph + embedding store

Parses an ABAP/ECC extraction file into a persistent structural dependency
graph (Postgres) and a persistent vector store of embedded code chunks
(pgvector), so both can be queried by later phases. See the original brief
for full context; this covers step 2 only — no retrieval/tiering logic, no
LLM, no UI.

Runs fully offline (after first-time package/model download) on a local
machine. No external services.

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

## 3. Generate a mock extraction file

No real SAP extraction file is available yet. Generate a synthetic one that
matches the exact input format the parser expects:

```bash
python -m lld_step2.mock_generator --count 18 --seed 42 --out data/mock_extraction.txt
```

This produces 18 synthetic objects (classes/programs/function modules) with a
realistic mix of calls, table usage, a few 3+ hop call chains, and at least
one dependency-free object.

## 4. Run the pipeline

```bash
python -m lld_step2.pipeline data/mock_extraction.txt
```

This reads the file, parses it, loads the graph tables (`objects`,
`object_calls`, `object_uses_table`), then chunks each object's source and
embeds/stores the chunks (`code_chunks`). Progress is logged per object;
malformed input exits with a clear message rather than a stack trace.

Re-running the pipeline on the same file is safe — it replaces each object's
own rows rather than duplicating them (see "Re-run behavior" below).

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

Parser and chunking tests are pure unit tests (no DB needed). The
pipeline/acceptance tests in `tests/test_pipeline.py` need Postgres running
(step 1) — they generate a fresh mock file, run the full pipeline against a
truncated test database, and check:

- object/call/table-usage row counts match the generator's known output
- specific caller/callee/table edges are correct
- re-running the pipeline doesn't duplicate rows
- a recursive multi-hop traversal matches a known dependency chain
- a vector similarity query returns chunks ordered by distance with raw text
  intact

If Postgres isn't reachable, these tests are skipped automatically (unit
tests still run).

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

Given a plain-English business requirement (plus an optional developer scoping note),
queries the step-2 graph and vector store and produces a small, ranked, explainable
candidate set — the input to a later LLM step (step 4, not built yet). No LLM calls, no
UI, deterministic scoring only. See [`lld_step2/retrieval.py`](lld_step2/retrieval.py).

### Running it

```bash
python -m lld_step2.retrieval_cli "Allow partial order shipment when stock is insufficient" \
    --scoping-note "ZCL_STOCK_MANAGER" --confidence likely --top-n 5 --export candidates.txt
```

- `--scoping-note` is optional free text naming object(s)/flow(s) the developer suspects
  are relevant. `--confidence` (`certain` | `likely` | `unsure`) is required if and only if
  a scoping note is given.
- `--export PATH` writes the result to a text file in the same block format as a SAP
  extraction file, with a `=== RETRIEVAL METADATA ===` header (disagreements +
  `likely_new_object`) in front of the object blocks — this is what step 4 will read.
  Strip the header with `lld_step2.retrieval.extract_object_blocks()` before feeding the
  file to `lld_step2.parser.parse_extraction_file` (the parser itself is unmodified and
  only understands `=== OBJECT: ===` blocks).

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

- **Re-run behavior**: `objects` rows are upserted by primary key (name).
  `object_calls`, `object_uses_table`, and `code_chunks` rows are deleted and
  re-inserted per object on every load, scoped to that object only. This
  keeps the loader idempotent without needing to diff individual rows.
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

LLM/Gemini gem integration (step 4), any UI, real SAP data (mock generator +
`ZORDER_MGMT_extract.txt` stand in until a real extraction exists), and
incremental/transport-log-based sync. Step 2 (graph + embedding store) and
step 3 (retrieval + tiering) are both built. See the respective briefs for
full detail.
