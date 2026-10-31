-- Documents for the RAG path: stable TfNSW policy pages (fares, bikes,
-- accessibility, luggage, etc.).
--
-- Real-time service alerts are deliberately NOT stored here. They go stale
-- within hours, so the app fetches them from the TfNSW API at query time.
--
-- EMBEDDING DIMENSION: vector(1024) below must match your embedding model,
-- e.g. 1024 for bge-large-en-v1.5, 1536 for OpenAI text-embedding-3-small.
-- Change it BEFORE the first `docker compose up`, or later with:
--   ALTER TABLE docs.chunks ALTER COLUMN embedding TYPE vector(<dim>);
-- (only while the column is empty).

CREATE TABLE IF NOT EXISTS docs.documents (
    doc_id        text PRIMARY KEY,           -- stable slug, e.g. 'bikes-on-public-transport'
    title         text NOT NULL,
    url           text NOT NULL,
    source        text NOT NULL DEFAULT 'tfnsw',
    fetched_at    timestamptz NOT NULL DEFAULT now(),
    content_hash  text                        -- skip re-embedding unchanged pages
);

CREATE TABLE IF NOT EXISTS docs.chunks (
    chunk_id     bigserial PRIMARY KEY,
    doc_id       text NOT NULL REFERENCES docs.documents (doc_id) ON DELETE CASCADE,
    chunk_index  integer NOT NULL,
    heading      text,                        -- section heading the chunk sits under
    content      text NOT NULL,
    token_count  integer,
    embedding    vector(1024),
    -- Keyword side of hybrid search. Postgres ts_rank is not true BM25,
    -- but it is a solid baseline; swap in a BM25 extension later if the
    -- evals say keyword recall is the bottleneck.
    tsv          tsvector GENERATED ALWAYS AS (
                     setweight(to_tsvector('english', coalesce(heading, '')), 'A') ||
                     setweight(to_tsvector('english', content), 'B')
                 ) STORED,
    UNIQUE (doc_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw
    ON docs.chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_tsv_gin
    ON docs.chunks USING gin (tsv);
