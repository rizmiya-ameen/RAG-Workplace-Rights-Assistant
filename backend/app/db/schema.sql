-- Workplace Rights Assistant schema. Safe to run more than once.

CREATE EXTENSION IF NOT EXISTS vector;

-- One row per source document version (awards change every 1 July).
CREATE TABLE IF NOT EXISTS documents (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id      text        NOT NULL,              -- 'MA000009', 'NES', 'WSV:<slug>'
    doc_type       text        NOT NULL CHECK (doc_type IN ('award', 'nes', 'worksafe')),
    title          text        NOT NULL,
    url            text        NOT NULL,
    effective_date date        NOT NULL,
    sha256         text        NOT NULL,
    fetched_at     timestamptz NOT NULL DEFAULT now(),
    -- Also serves as the btree index on (source_id, effective_date).
    UNIQUE (source_id, effective_date)
);

-- One row per clause (long clauses split as '28.2#1', '28.2#2').
CREATE TABLE IF NOT EXISTS chunks (
    id            bigint  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id   bigint  NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    clause_ref    text    NOT NULL,                   -- '28.2', '15A.1', 'B.2.1', 's87'
    heading       text,
    content       text    NOT NULL,
    is_rate_table boolean NOT NULL DEFAULT false,
    embedding     vector(384),                        -- bge-small-en-v1.5; NULL until embedded
    tsv           tsvector GENERATED ALWAYS AS (
                      to_tsvector('english', coalesce(heading, '') || ' ' || content)
                  ) STORED,
    UNIQUE (document_id, clause_ref)
);

CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_tsv_gin ON chunks USING gin (tsv);

-- Structured pay rates. Never sent to the LLM as context.
CREATE TABLE IF NOT EXISTS pay_rates (
    id              bigint        GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id     bigint        NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    classification  text          NOT NULL,
    employment_type text          NOT NULL
                    CHECK (employment_type IN ('full_time', 'part_time', 'casual')),
    rate_type       text          NOT NULL
                    CHECK (rate_type IN ('weekly', 'hourly', 'penalty', 'overtime', 'allowance')),
    condition       text,
    amount          numeric(10,2) NOT NULL CHECK (amount >= 0),
    unit            text          NOT NULL,
    source_clause   text          NOT NULL,
    effective_date  date          NOT NULL
);

CREATE INDEX IF NOT EXISTS pay_rates_lookup ON pay_rates (document_id, classification, employment_type);

-- Every guardrail decision. Never store raw user text or PII in detail.
CREATE TABLE IF NOT EXISTS guardrail_events (
    id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    request_id text        NOT NULL,
    stage      text        NOT NULL CHECK (stage IN ('input', 'context', 'output')),
    rule       text        NOT NULL,
    detail     jsonb
);

CREATE INDEX IF NOT EXISTS guardrail_events_created_at ON guardrail_events (created_at);
CREATE INDEX IF NOT EXISTS guardrail_events_request_id ON guardrail_events (request_id);

-- Optional: eval results over time.
CREATE TABLE IF NOT EXISTS eval_runs (
    id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    git_sha    text,
    mode       text        NOT NULL,
    metrics    jsonb       NOT NULL
);
