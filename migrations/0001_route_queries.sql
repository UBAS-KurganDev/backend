CREATE TABLE route_queries (
    id UUID PRIMARY KEY,
    request_json JSONB NOT NULL,
    request_fingerprint TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    available_at TIMESTAMPTZ NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    result_json JSONB,
    failure_code TEXT,
    failure_message TEXT,
    failure_retryable BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CHECK (
        (status = 'running') = (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX route_queries_active_fingerprint_key
    ON route_queries (request_fingerprint)
    WHERE status IN ('queued', 'running');

CREATE INDEX route_queries_claim_index
    ON route_queries (available_at, created_at)
    WHERE status = 'queued';

CREATE INDEX route_queries_expired_lease_index
    ON route_queries (lease_expires_at)
    WHERE status = 'running';
