-- ============================================================================
--  KALYPSO — миграция 0003: сессии входа и запросы межфилиального доступа
-- ============================================================================

-- Серверные сессии. В cookie — случайный токен; в БД — только его SHA-256.
CREATE TABLE app_session (
    token_hash   BYTEA PRIMARY KEY CHECK (length(token_hash) = 32),
    user_id      UUID NOT NULL REFERENCES app_user (user_id) ON DELETE RESTRICT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at   TIMESTAMPTZ,
    ip           INET,
    user_agent   TEXT
);
CREATE INDEX app_session_user_idx ON app_session (user_id);

-- Пациент уже есть в регистре, но ведётся другим филиалом: врач не видит его
-- данных, пока куратор не одобрит доступ (дедупликация без утечки).
CREATE TABLE branch_access_request (
    request_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id    UUID NOT NULL REFERENCES patient (patient_id) ON DELETE RESTRICT,
    branch_code   TEXT NOT NULL REFERENCES branch (branch_code) ON DELETE RESTRICT,
    requested_by  UUID NOT NULL REFERENCES app_user (user_id) DEFAULT app_user_id(),
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by    UUID REFERENCES app_user (user_id),
    decided_at    TIMESTAMPTZ,
    decision_note TEXT,
    CONSTRAINT branch_access_request_decision CHECK ((status = 'pending') = (decided_at IS NULL))
);
CREATE UNIQUE INDEX branch_access_request_pending_uq
    ON branch_access_request (patient_id, branch_code) WHERE status = 'pending';

CREATE TRIGGER branch_access_request_audit AFTER INSERT OR UPDATE OR DELETE ON branch_access_request
    FOR EACH ROW EXECUTE FUNCTION audit_row('request_id');

GRANT SELECT, INSERT, UPDATE, DELETE ON app_session TO kalypso_app;
GRANT SELECT, INSERT, UPDATE         ON branch_access_request TO kalypso_app;
