-- 0017_users (T47): adjuster accounts, roles, and who did what.
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    email TEXT NOT NULL,                -- trimmed and lowercased; unique per org
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('adjuster', 'supervisor')),
    password_hash TEXT NOT NULL,        -- scrypt$N$r$p$salthex$hashhex
    must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_by TEXT                     -- the user id that added this one; NULL for the seeded supervisor
);
CREATE UNIQUE INDEX users_org_email ON users (org_id, email);
ALTER TABLE reviews ADD COLUMN user_id TEXT;    -- who recorded it; NULL for rows written before T47
ALTER TABLE decisions ADD COLUMN user_id TEXT;  -- who decided; NULL for rows written before T47
ALTER TABLE org_settings ADD COLUMN updated_by TEXT;  -- the name shown on the Settings page
ALTER TABLE org_settings ADD COLUMN updated_at TEXT;
