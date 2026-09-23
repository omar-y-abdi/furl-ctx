-- Apply with a dedicated service role. Never expose this schema through a public API.
CREATE SCHEMA IF NOT EXISTS furl_remote;
REVOKE ALL ON SCHEMA furl_remote FROM PUBLIC;
CREATE TABLE IF NOT EXISTS furl_remote.tenants (
    tenant_key text PRIMARY KEY CHECK (tenant_key ~ '^[a-f0-9]{64}$'),
    snapshot bytea NOT NULL DEFAULT ''::bytea,
    expires_at timestamptz NOT NULL,
    CHECK (octet_length(snapshot) <= 67108864)
);
REVOKE ALL ON furl_remote.tenants FROM PUBLIC;
CREATE INDEX IF NOT EXISTS tenants_expiry ON furl_remote.tenants (expires_at);
