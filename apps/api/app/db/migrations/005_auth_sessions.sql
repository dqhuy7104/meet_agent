CREATE TABLE auth_sessions (
    token_digest text PRIMARY KEY,
    account_id text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    csrf_token text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX auth_sessions_account_id ON auth_sessions(account_id);
CREATE TABLE auth_attempts (
    peer text NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX auth_attempts_peer_time ON auth_attempts(peer, attempted_at);
