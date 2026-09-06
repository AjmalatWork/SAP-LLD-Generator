-- Step 2 schema: structural graph + embedding store
-- NOTE: the vector dimension below (384) matches the default embedding model
-- (sentence-transformers/all-MiniLM-L6-v2). If you change LLD_EMBEDDING_MODEL
-- to a model with a different output dimension, update the dimension here
-- (and re-create the volume / table) to match.
--
-- This schema targets the REAL extraction format step 1 (ZLLD_PACKAGE_EXTRACTOR)
-- produces, not the earlier simplified {"calls": [...], "tables_used": [...]}
-- mock format. If you have an existing local dev database from before this
-- schema version, it will NOT be auto-migrated by this file (CREATE TABLE IF
-- NOT EXISTS does not ALTER existing tables) - reset it with
-- `docker compose down -v && docker compose up -d` to pick up the new shape.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS objects (
    name    TEXT PRIMARY KEY,
    type    TEXT NOT NULL,   -- LLD-level type: CLASS / PROGRAM / FUNCTION_MODULE
                              -- (distinct vocabulary from object_calls.dependency_type
                              -- below - never conflate the two).
    package TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_objects_package ON objects(package);

-- One row per dependency entry found in an object's real DEPENDENCIES list.
-- dependency_type is the raw TYPE value from the source JSON (METH, OM, PROG,
-- INCL, FUNC, INTF, CLAS, FUGR, MESS, MSAG, TRAN, STRU, TYPE, DGT, OA, ... -
-- this vocabulary is open-ended, observed to be broader in real data than any
-- brief or source FM documents exhaustively, so it is NOT constrained by a
-- CHECK/enum here). edge_kind is a normalized grouping derived at load time
-- (see graph_loader.py's EDGE_KIND_MAP) so traversal queries can group
-- semantically-equivalent edge types without enumerating every raw value.
-- signature_json is the raw SIGNATURE blob as-is (populated only for
-- METH/OM/FUNC/INCL/PROG in the source data, NULL otherwise) - stored for
-- step 4's future consumption, not parsed further by step 2/3.
CREATE TABLE IF NOT EXISTS object_calls (
    id              SERIAL PRIMARY KEY,
    source_object   TEXT NOT NULL REFERENCES objects(name) ON DELETE CASCADE,
    target_object   TEXT NOT NULL,
    dependency_type TEXT NOT NULL,
    edge_kind       TEXT NOT NULL,
    signature_json  JSONB,
    UNIQUE (source_object, target_object, dependency_type)
);

CREATE INDEX IF NOT EXISTS idx_object_calls_source ON object_calls(source_object);
CREATE INDEX IF NOT EXISTS idx_object_calls_target ON object_calls(target_object);
CREATE INDEX IF NOT EXISTS idx_object_calls_edge_kind ON object_calls(edge_kind);

-- DOMA/DTEL/TABL/TTYP dependency references only (see graph_loader.py) -
-- everything else (including STRU) is a plain object_calls edge instead.
-- ddic_object_name has no FK to ddic_objects: a referenced DDIC object may
-- not (yet) have a row there, e.g. a standard SAP table/domain never
-- extracted via Z_GET_DDIC_INFO for this package.
CREATE TABLE IF NOT EXISTS object_uses_table (
    id                SERIAL PRIMARY KEY,
    object_name       TEXT NOT NULL REFERENCES objects(name) ON DELETE CASCADE,
    ddic_object_name  TEXT NOT NULL,
    ddic_type         TEXT NOT NULL,   -- DOMA / DTEL / TABL / TTYP
    UNIQUE (object_name, ddic_object_name, ddic_type)
);

CREATE INDEX IF NOT EXISTS idx_object_uses_table_object ON object_uses_table(object_name);
CREATE INDEX IF NOT EXISTS idx_object_uses_table_ddic_name ON object_uses_table(ddic_object_name);

-- One row per DDIC object found in a file's shared --- DDIC --- section.
-- ddic_type uses the same five buckets Z_GET_DDIC_INFO's own JSON already
-- splits into: DOMA (domain), DTEL (data_element), TABL (table), STRU
-- (structure), TTYP (table_type) - even though a structure's real
-- TADIR/DDIC object type is also TABL, Z_GET_DDIC_INFO's output (and the
-- dependency-entry TYPE vocabulary) already treats STRU as its own distinct
-- category, so this table mirrors that rather than reconciling it away.
-- detail_json holds everything else from that object's JSON entry
-- (description, fields, indexes, etc.) as-is - not normalized into
-- per-field rows, per the brief, unless a concrete future need requires it.
CREATE TABLE IF NOT EXISTS ddic_objects (
    package     TEXT NOT NULL,
    ddic_type   TEXT NOT NULL,
    name        TEXT NOT NULL,
    detail_json JSONB NOT NULL,
    PRIMARY KEY (package, ddic_type, name)
);

CREATE INDEX IF NOT EXISTS idx_ddic_objects_name ON ddic_objects(name);

CREATE TABLE IF NOT EXISTS code_chunks (
    id           SERIAL PRIMARY KEY,
    object_name  TEXT NOT NULL REFERENCES objects(name) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    chunk_text   TEXT NOT NULL,
    embedding    vector(384),
    UNIQUE (object_name, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_code_chunks_object ON code_chunks(object_name);

-- Approximate nearest-neighbor index for similarity search (cosine distance).
-- Built lazily; ivfflat needs data present to train well, safe to create empty too.
CREATE INDEX IF NOT EXISTS idx_code_chunks_embedding
    ON code_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
