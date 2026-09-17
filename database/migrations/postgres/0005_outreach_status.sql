-- Stabilization 2026-09: adopt the canonical outreach lifecycle on
-- PostgreSQL/Supabase (mirror of SQLite schema v6).
--
-- Record status moves to ready -> sent -> responded -> follow_up -> closed
-- (plus the honest terminal 'failed'), and the attempts ledger gains the
-- full event set (opened_in_email | sent | responded | follow_up |
-- failed | closed). Non-destructive: legacy values are REMAPPED in place,
-- never deleted ('draft' and 'opened_in_email' both become 'ready'; the
-- mail-client handoff nuance stays verbatim in the attempts ledger).
--
-- ORDER MATTERS: the remap runs AFTER the old constraint is dropped (the
-- new 'ready' value is not allowed by the legacy allowance) but BEFORE the
-- new CHECK is added (PostgreSQL validates every existing row at
-- ADD CONSTRAINT, so live legacy rows must already be canonical by then).

ALTER TABLE outreach_messages
    DROP CONSTRAINT IF EXISTS outreach_messages_status_check;

UPDATE outreach_messages
    SET status = 'ready'
    WHERE status IN ('draft', 'opened_in_email');

ALTER TABLE outreach_messages
    ADD CONSTRAINT outreach_messages_status_check CHECK (status IN (
        'ready', 'sent', 'responded', 'follow_up', 'failed', 'closed'));

ALTER TABLE outreach_attempts
    DROP CONSTRAINT IF EXISTS outreach_attempts_event_check;
ALTER TABLE outreach_attempts
    ADD CONSTRAINT outreach_attempts_event_check CHECK (event IN (
        'opened_in_email', 'sent', 'responded', 'follow_up',
        'failed', 'closed'));