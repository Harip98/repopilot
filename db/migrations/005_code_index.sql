-- Optional hybrid code search for the agent's find_code tool: full-text and vector
-- similarity over overlapping line windows of a run's workspace.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE code_chunks (
    id         bigserial PRIMARY KEY,
    index_key  text NOT NULL,              -- one index per run (the run id)
    path       text NOT NULL,
    start_line int NOT NULL,
    end_line   int NOT NULL,
    content    text NOT NULL,
    tsv        tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    embedding  vector(768) NOT NULL,        -- nomic-embed-text dimensions
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX code_chunks_key_idx ON code_chunks (index_key);
CREATE INDEX code_chunks_tsv_idx ON code_chunks USING gin (tsv);
-- Exact (sequential) vector search: a run's index is a few hundred chunks at most.
