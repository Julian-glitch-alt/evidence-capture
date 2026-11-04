-- Per-account login throttling: failed logins also carry the account email.
ALTER TABLE login_failures ADD COLUMN email TEXT;
CREATE INDEX login_failures_email_at ON login_failures(email, at);
