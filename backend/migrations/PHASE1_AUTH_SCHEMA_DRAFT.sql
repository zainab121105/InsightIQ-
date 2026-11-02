-- InsightIQ Phase 1 authentication schema draft
-- REVIEW ONLY: do not execute as-is.
--
-- Target: MySQL 8.0.46, InnoDB, utf8mb4.
-- This draft does not alter sales_transactions and does not perform legacy
-- workspace creation or data backfill.
--
-- Application invariants not expressible safely with simple constraints:
-- * last-owner removal/demotion must lock the business row and active
--   memberships in one transaction, then reject the operation unless another
--   active owner is assigned in that same transaction;
-- * refresh-token rotation and preview consumption must use conditional
--   updates/transactions and accept exactly one successful state transition;
-- * reuse of a consumed refresh token must revoke its session and all
--   currently usable refresh tokens in that session; consumed/expired history remains
--   stored and unusable for audit and replay detection.
-- * only one refresh-token row may be current for a session; the generated
--   current-token marker below implements this with a MySQL-compatible
--   unique index (NULL markers are not unique).
--
-- The existing documented schema uses utf8mb4_0900_ai_ci for
-- sales_transactions. This draft uses that same collation for new tables; it
-- does not alter the existing table or silently change its collation.

CREATE TABLE businesses (
    business_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    name       VARCHAR(150) NOT NULL,
    slug       VARCHAR(150) NOT NULL,
    status     VARCHAR(20)  NOT NULL DEFAULT 'active',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
               ON UPDATE CURRENT_TIMESTAMP(6),
    CONSTRAINT pk_businesses
        PRIMARY KEY (business_id),
    CONSTRAINT uq_businesses_slug
        UNIQUE (slug),
    CONSTRAINT chk_businesses_status
        CHECK (status IN ('active', 'suspended', 'archived'))
) ENGINE=InnoDB
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

CREATE TABLE users (
    user_id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    email              VARCHAR(320) NOT NULL,
    display_name       VARCHAR(150) NOT NULL,
    password_hash      VARCHAR(255) NOT NULL,
    status             VARCHAR(20) NOT NULL DEFAULT 'active',
    email_verified_at  DATETIME(6) NULL,
    created_at         DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at         DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                       ON UPDATE CURRENT_TIMESTAMP(6),
    CONSTRAINT pk_users
        PRIMARY KEY (user_id),
    -- Application must normalize before signup/login: trim, apply the
    -- documented Unicode/case policy, and persist the normalized value.
    -- utf8mb4_0900_ai_ci supplies case/accent-insensitive uniqueness here;
    -- changing collation later would change which emails collide.
    CONSTRAINT uq_users_email
        UNIQUE (email),
    CONSTRAINT chk_users_status
        CHECK (status IN ('active', 'locked', 'disabled'))
) ENGINE=InnoDB
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

CREATE TABLE business_memberships (
    business_id BIGINT UNSIGNED NOT NULL,
    user_id     BIGINT UNSIGNED NOT NULL,
    role        VARCHAR(20) NOT NULL,
    status      VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                ON UPDATE CURRENT_TIMESTAMP(6),
    CONSTRAINT pk_business_memberships
        PRIMARY KEY (business_id, user_id),
    CONSTRAINT fk_memberships_business
        FOREIGN KEY (business_id) REFERENCES businesses (business_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_memberships_user
        FOREIGN KEY (user_id) REFERENCES users (user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_memberships_role
        CHECK (role IN ('owner', 'admin', 'member')),
    CONSTRAINT chk_memberships_status
        CHECK (status IN ('active', 'invited', 'revoked')),
    KEY ix_memberships_user_status_business (user_id, status, business_id)
) ENGINE=InnoDB
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

CREATE TABLE user_sessions (
    session_id              CHAR(36) NOT NULL,
    user_id                 BIGINT UNSIGNED NOT NULL,
    access_token_jti        CHAR(36) NOT NULL,
    csrf_token_hash         BINARY(32) NOT NULL,
    status                  VARCHAR(20) NOT NULL DEFAULT 'active',
    issued_at               DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    last_used_at            DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    expires_at              DATETIME(6) NOT NULL,
    revoked_at              DATETIME(6) NULL,
    revoked_reason          VARCHAR(100) NULL,
    user_agent              VARCHAR(512) NULL,
    ip_address              VARBINARY(16) NULL,
    created_at              DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at              DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                            ON UPDATE CURRENT_TIMESTAMP(6),
    CONSTRAINT pk_user_sessions
        PRIMARY KEY (session_id),
    CONSTRAINT uq_user_sessions_access_jti
        UNIQUE (access_token_jti),
    CONSTRAINT fk_user_sessions_user
        FOREIGN KEY (user_id) REFERENCES users (user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_user_sessions_status
        CHECK (status IN ('active', 'revoked', 'expired')),
    KEY ix_user_sessions_user_status_expiry (user_id, status, expires_at)
) ENGINE=InnoDB
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

CREATE TABLE user_session_refresh_tokens (
    refresh_token_id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    session_id             CHAR(36) NOT NULL,
    token_family_id        CHAR(36) NOT NULL,
    token_hash             BINARY(32) NOT NULL,
    status                 VARCHAR(20) NOT NULL DEFAULT 'current',
    issued_at              DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    expires_at             DATETIME(6) NOT NULL,
    consumed_at            DATETIME(6) NULL,
    replaces_token_id      BIGINT UNSIGNED NULL,
    replaced_by_token_id   BIGINT UNSIGNED NULL,
    revoked_at             DATETIME(6) NULL,
    revoked_reason         VARCHAR(100) NULL,
    created_at             DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    current_token_marker   TINYINT GENERATED ALWAYS AS
        (CASE WHEN status = 'current' THEN 1 ELSE NULL END) STORED,
    CONSTRAINT pk_session_refresh_tokens
        PRIMARY KEY (refresh_token_id),
    CONSTRAINT uq_session_refresh_tokens_hash
        UNIQUE (token_hash),
    CONSTRAINT uq_session_refresh_tokens_current
        UNIQUE (session_id, current_token_marker),
    CONSTRAINT fk_refresh_tokens_session
        FOREIGN KEY (session_id) REFERENCES user_sessions (session_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_refresh_tokens_replaced_by
        FOREIGN KEY (session_id, token_family_id, replaced_by_token_id)
        REFERENCES user_session_refresh_tokens
            (session_id, token_family_id, refresh_token_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_refresh_tokens_replaces
        FOREIGN KEY (session_id, token_family_id, replaces_token_id)
        REFERENCES user_session_refresh_tokens
            (session_id, token_family_id, refresh_token_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_refresh_tokens_status
        CHECK (status IN ('current', 'consumed', 'revoked', 'expired')),
    CONSTRAINT chk_refresh_tokens_time_order
        CHECK (issued_at <= expires_at),
    CONSTRAINT chk_refresh_tokens_state
        CHECK (
            (status = 'current' AND consumed_at IS NULL AND revoked_at IS NULL)
            OR
            (status = 'consumed' AND consumed_at IS NOT NULL AND revoked_at IS NULL)
            OR
            (status = 'revoked' AND consumed_at IS NULL AND revoked_at IS NOT NULL)
            OR
            (status = 'expired' AND consumed_at IS NULL AND revoked_at IS NULL)
        ),
    CONSTRAINT uq_refresh_tokens_replaced_by
        UNIQUE (replaced_by_token_id),
    CONSTRAINT uq_refresh_tokens_replaces
        UNIQUE (replaces_token_id),
    CONSTRAINT uq_refresh_tokens_session_family_id
        UNIQUE (session_id, token_family_id, refresh_token_id),
    KEY ix_refresh_tokens_session_status (session_id, status, expires_at),
    KEY ix_refresh_tokens_family (token_family_id, issued_at),
    KEY ix_refresh_tokens_replaced_by (replaced_by_token_id)
) ENGINE=InnoDB
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

-- MySQL 8.0.46 does not allow a CHECK constraint to reference the
-- AUTO_INCREMENT refresh_token_id. The application must reject
-- replaces_token_id = refresh_token_id and
-- replaced_by_token_id = refresh_token_id before writing a link.

-- Refresh lookup and response handling are application-transaction logic:
-- 1. Hash the presented token and look up that hash first. Do not treat a
--    missing row as equivalent to a known historical row before checking its
--    status.
-- 2. For status='current', lock the token row and its session with SELECT ...
--    FOR UPDATE, then verify the session is active and the token is not past
--    expires_at. Rotate only a valid current token.
-- 3. For status='consumed', retain the distinction and treat the request as
--    refresh-token replay. Revoke the session, mark every current token in
--    the affected session as revoked, retain consumed/expired rows,
--    and write the security audit event in the same transaction.
-- 4. For status='revoked' or status='expired', reject the request as an
--    unusable token without treating it as consumed-token replay. If a
--    current row is found past expires_at, first transition it to expired
--    under the same lock, then apply the ordinary expired response.
-- 5. For a missing hash, reject as an invalid token without revealing whether
--    any related session or family exists. Never recover or reconstruct a raw
--    replacement token from its hash.
-- 6. A valid rotation marks the predecessor consumed, inserts exactly one
--    same-session, same-family current replacement, and sets
--    replaced_by_token_id on the predecessor in one transaction. The
--    composite FKs and unique references prevent cross-family replacement and
--    branching.
-- Concurrent refresh handling is application and client coordination:
-- clients should use single-flight refresh coordination so only one refresh
-- request is in flight per session. SELECT ... FOR UPDATE serializes requests
-- that still race: the winner commits the rotation, and a stale waiter then
-- observes status='consumed'. Because the raw replacement token cannot be
-- recovered from its hash and no unreviewed idempotent retry is supported, the
-- stale request is rejected; under the replay policy above, the session and
-- all currently usable session tokens are revoked and an audit event is recorded.
-- Raw refresh tokens are never stored.
-- Session-wide replay revocation must update only rows whose status='current'
-- to status='revoked' (with revoked_at/reason). Consumed and expired historical
-- rows must not be changed, because their CHECK-constrained state is already
-- terminal. The session revocation, usable-token updates, and audit insert
-- must commit or roll back together, leaving no usable token in the session.
-- Status/timestamp transitions for refresh, password-reset, and verification
-- rows must be exposed only through the session/token service. MySQL CHECK
-- constraints prevent contradictory states, while the service transaction
-- prevents a consumed row from being consumed again or changing back to
-- current.
-- Raw refresh tokens are never stored.

CREATE TABLE security_audit_events (
    event_id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    event_type           VARCHAR(80) NOT NULL,
    actor_user_id        BIGINT UNSIGNED NULL,
    target_user_id       BIGINT UNSIGNED NULL,
    target_business_id   BIGINT UNSIGNED NULL,
    session_id           CHAR(36) NULL,
    outcome              VARCHAR(20) NOT NULL,
    request_id           CHAR(36) NULL,
    -- Application allowlist only: diagnostic fields with bounded size.
    -- Never include passwords, raw access/refresh tokens, CSRF secrets,
    -- credentials, authorization headers, or other sensitive values.
    metadata_json        JSON NULL COMMENT
        'Allowlisted non-secret metadata only; no credentials or raw secrets',
    created_at           DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT pk_security_audit_events
        PRIMARY KEY (event_id),
    CONSTRAINT fk_audit_actor_user
        FOREIGN KEY (actor_user_id) REFERENCES users (user_id)
        ON DELETE SET NULL ON UPDATE RESTRICT,
    CONSTRAINT fk_audit_target_user
        FOREIGN KEY (target_user_id) REFERENCES users (user_id)
        ON DELETE SET NULL ON UPDATE RESTRICT,
    CONSTRAINT fk_audit_target_business
        FOREIGN KEY (target_business_id) REFERENCES businesses (business_id)
        ON DELETE SET NULL ON UPDATE RESTRICT,
    CONSTRAINT fk_audit_session
        FOREIGN KEY (session_id) REFERENCES user_sessions (session_id)
        ON DELETE SET NULL ON UPDATE RESTRICT,
    CONSTRAINT chk_security_audit_outcome
        CHECK (outcome IN ('success', 'failure', 'denied')),
    KEY ix_audit_event_type_created (event_type, created_at),
    KEY ix_audit_actor_created (actor_user_id, created_at),
    KEY ix_audit_business_created (target_business_id, created_at),
    KEY ix_audit_request (request_id)
) ENGINE=InnoDB
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

CREATE TABLE password_reset_tokens (
    token_id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id        BIGINT UNSIGNED NOT NULL,
    token_hash     BINARY(32) NOT NULL,
    issued_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    expires_at     DATETIME(6) NOT NULL,
    consumed_at    DATETIME(6) NULL,
    revoked_at     DATETIME(6) NULL,
    created_at     DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT pk_password_reset_tokens
        PRIMARY KEY (token_id),
    CONSTRAINT uq_password_reset_token_hash
        UNIQUE (token_hash),
    CONSTRAINT fk_password_reset_user
        FOREIGN KEY (user_id) REFERENCES users (user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_password_reset_time_order
        CHECK (issued_at <= expires_at),
    CONSTRAINT chk_password_reset_single_use
        CHECK (NOT (consumed_at IS NOT NULL AND revoked_at IS NOT NULL)),
    KEY ix_password_reset_user_expiry (user_id, expires_at)
) ENGINE=InnoDB
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

-- Password-reset and email-verification consumption is single-use application
-- transaction logic: lock the unconsumed, unrevoked, unexpired row, conditionally
-- set consumed_at, and commit exactly one successful update. The checks below
-- reject contradictory timestamps; they cannot by themselves make a timestamp
-- immutable because MySQL CHECK constraints cannot compare OLD and NEW values.

CREATE TABLE email_verification_tokens (
    token_id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id        BIGINT UNSIGNED NOT NULL,
    token_hash     BINARY(32) NOT NULL,
    issued_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    expires_at     DATETIME(6) NOT NULL,
    consumed_at    DATETIME(6) NULL,
    revoked_at     DATETIME(6) NULL,
    created_at     DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT pk_email_verification_tokens
        PRIMARY KEY (token_id),
    CONSTRAINT uq_email_verification_token_hash
        UNIQUE (token_hash),
    CONSTRAINT fk_email_verification_user
        FOREIGN KEY (user_id) REFERENCES users (user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_email_verification_time_order
        CHECK (issued_at <= expires_at),
    CONSTRAINT chk_email_verification_single_use
        CHECK (NOT (consumed_at IS NOT NULL AND revoked_at IS NOT NULL)),
    KEY ix_email_verification_user_expiry (user_id, expires_at)
) ENGINE=InnoDB
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

CREATE TABLE upload_previews (
    preview_id             CHAR(36) NOT NULL,
    token_hash             BINARY(32) NOT NULL,
    user_id                BIGINT UNSIGNED NOT NULL,
    business_id            BIGINT UNSIGNED NOT NULL,
    status                 VARCHAR(20) NOT NULL DEFAULT 'active',
    validated_records_json JSON NULL,
    staging_reference      VARCHAR(255) NULL,
    created_at             DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    expires_at             DATETIME(6) NOT NULL,
    consumed_at            DATETIME(6) NULL,
    updated_at             DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                           ON UPDATE CURRENT_TIMESTAMP(6),
    CONSTRAINT pk_upload_previews
        PRIMARY KEY (preview_id),
    CONSTRAINT uq_upload_previews_token_hash
        UNIQUE (token_hash),
    -- The referenced membership primary key is exactly
    -- business_memberships (business_id BIGINT UNSIGNED, user_id BIGINT
    -- UNSIGNED). This prevents pairing a user with a business they never
    -- belonged to at record creation.
    CONSTRAINT fk_upload_previews_membership
        FOREIGN KEY (business_id, user_id)
        REFERENCES business_memberships (business_id, user_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_upload_previews_status
        CHECK (status IN ('active', 'consumed', 'expired', 'revoked')),
    KEY ix_upload_previews_business_expiry (business_id, expires_at),
    KEY ix_upload_previews_user_status_expiry (user_id, status, expires_at)
) ENGINE=InnoDB
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

-- Intentionally absent from this draft: any sales_transactions alteration,
-- legacy workspace creation, or legacy data backfill.
