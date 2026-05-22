CREATE TABLE IF NOT EXISTS jobs (
    job_id             UUID PRIMARY KEY,
    status             TEXT        NOT NULL DEFAULT 'queued',
    original_filename  TEXT,
    output_filename    TEXT,
    options            JSONB,
    output_path        TEXT,
    error              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at       TIMESTAMPTZ,
    input_size         BIGINT,
    output_size        BIGINT,
    compression_ratio  DOUBLE PRECISION,
    size_reduction_pct DOUBLE PRECISION,
    duration_seconds   DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);
CREATE INDEX IF NOT EXISTS jobs_created_at_idx ON jobs (created_at DESC);