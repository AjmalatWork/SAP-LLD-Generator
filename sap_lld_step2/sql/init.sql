-- Step 2 schema: structural graph + embedding store
-- NOTE: the vector dimension below (384) matches the default embedding model
-- (sentence-transformers/all-MiniLM-L6-v2). If you change LLD_EMBEDDING_MODEL
-- to a model with a different output dimension, update the dimension here
-- (and re-create the volume / table) to match.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS objects (
    name    TEXT PRIMARY KEY,
    type    TEXT NOT NULL,
    package TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS object_calls (
    id              SERIAL PRIMARY KEY,
    source_object   TEXT NOT NULL REFERENCES objects(name) ON DELETE CASCADE,
    target_object   TEXT NOT NULL,
    UNIQUE (source_object, target_object)
);

CREATE INDEX IF NOT EXISTS idx_object_calls_source ON object_calls(source_object);
CREATE INDEX IF NOT EXISTS idx_object_calls_target ON object_calls(target_object);

CREATE TABLE IF NOT EXISTS object_uses_table (
    id           SERIAL PRIMARY KEY,
    object_name  TEXT NOT NULL REFERENCES objects(name) ON DELETE CASCADE,
    table_name   TEXT NOT NULL,
    fields       JSONB NOT NULL DEFAULT '[]'::jsonb,
    UNIQUE (object_name, table_name)
);

CREATE INDEX IF NOT EXISTS idx_object_uses_table_object ON object_uses_table(object_name);
CREATE INDEX IF NOT EXISTS idx_object_uses_table_table ON object_uses_table(table_name);

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
