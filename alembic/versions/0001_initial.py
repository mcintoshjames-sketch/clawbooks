"""legacy baseline schema"""

from alembic import op


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


TABLE_STATEMENTS = [
    """
    CREATE TABLE accounts (
        id INTEGER NOT NULL,
        code VARCHAR(32) NOT NULL,
        name VARCHAR(255) NOT NULL,
        kind VARCHAR(32) NOT NULL,
        subtype VARCHAR(64) NOT NULL,
        currency VARCHAR(8) NOT NULL,
        is_active BOOLEAN NOT NULL,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE attachments (
        id INTEGER NOT NULL,
        path VARCHAR(500) NOT NULL,
        sha256 VARCHAR(64) NOT NULL,
        description VARCHAR(500),
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE documents (
        id INTEGER NOT NULL,
        document_type VARCHAR(64) NOT NULL,
        tax_year INTEGER NOT NULL,
        period_start DATE,
        period_end DATE,
        scope VARCHAR(32) NOT NULL,
        original_filename VARCHAR(255) NOT NULL,
        stored_path VARCHAR(500) NOT NULL,
        sha256 VARCHAR(64) NOT NULL,
        notes TEXT,
        created_via VARCHAR(32) NOT NULL,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE document_links (
        id INTEGER NOT NULL,
        document_id INTEGER NOT NULL,
        target_type VARCHAR(32) NOT NULL,
        target_id INTEGER NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_document_links UNIQUE (document_id, target_type, target_id),
        FOREIGN KEY(document_id) REFERENCES documents (id)
    )
    """,
    """
    CREATE TABLE imports (
        id INTEGER NOT NULL,
        source VARCHAR(32) NOT NULL,
        status VARCHAR(32) NOT NULL,
        started_at DATETIME NOT NULL,
        completed_at DATETIME,
        from_date DATE,
        to_date DATE,
        dry_run BOOLEAN NOT NULL,
        source_path VARCHAR(500),
        warnings_json TEXT NOT NULL,
        summary_json TEXT NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE journal_entries (
        id INTEGER NOT NULL,
        entry_date DATE NOT NULL,
        description VARCHAR(500) NOT NULL,
        source_type VARCHAR(64) NOT NULL,
        source_ref VARCHAR(255),
        created_at DATETIME NOT NULL,
        reversal_of_entry_id INTEGER,
        import_run_id INTEGER,
        PRIMARY KEY (id),
        FOREIGN KEY(reversal_of_entry_id) REFERENCES journal_entries (id),
        FOREIGN KEY(import_run_id) REFERENCES imports (id)
    )
    """,
    """
    CREATE TABLE journal_lines (
        id INTEGER NOT NULL,
        entry_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        amount_cents INTEGER NOT NULL,
        memo VARCHAR(500),
        PRIMARY KEY (id),
        FOREIGN KEY(entry_id) REFERENCES journal_entries (id),
        FOREIGN KEY(account_id) REFERENCES accounts (id)
    )
    """,
    """
    CREATE TABLE settlement_applications (
        id INTEGER NOT NULL,
        source_line_id INTEGER NOT NULL,
        settlement_line_id INTEGER NOT NULL,
        applied_amount_cents INTEGER NOT NULL,
        applied_date DATE NOT NULL,
        application_type VARCHAR(32) NOT NULL,
        created_at DATETIME NOT NULL,
        reversed_at DATETIME,
        reversal_reason TEXT,
        PRIMARY KEY (id),
        FOREIGN KEY(source_line_id) REFERENCES journal_lines (id),
        FOREIGN KEY(settlement_line_id) REFERENCES journal_lines (id)
    )
    """,
    """
    CREATE TABLE external_events (
        id INTEGER NOT NULL,
        provider VARCHAR(64) NOT NULL,
        external_id VARCHAR(255) NOT NULL,
        event_type VARCHAR(64) NOT NULL,
        occurred_at DATETIME NOT NULL,
        payload_json TEXT NOT NULL,
        import_run_id INTEGER,
        journal_entry_id INTEGER,
        PRIMARY KEY (id),
        CONSTRAINT uq_external_events_provider_id UNIQUE (provider, external_id),
        FOREIGN KEY(import_run_id) REFERENCES imports (id),
        FOREIGN KEY(journal_entry_id) REFERENCES journal_entries (id)
    )
    """,
    """
    CREATE TABLE external_event_refresh_history (
        id INTEGER NOT NULL,
        external_event_id INTEGER NOT NULL,
        refreshed_at DATETIME NOT NULL,
        payload_json TEXT NOT NULL,
        refresh_source VARCHAR(64) NOT NULL,
        change_note TEXT,
        PRIMARY KEY (id),
        FOREIGN KEY(external_event_id) REFERENCES external_events (id)
    )
    """,
    """
    CREATE TABLE review_blockers (
        id INTEGER NOT NULL,
        blocker_type VARCHAR(64) NOT NULL,
        provider VARCHAR(64) NOT NULL,
        external_id VARCHAR(255) NOT NULL,
        status VARCHAR(32) NOT NULL,
        blocker_date DATE NOT NULL,
        opened_at DATETIME NOT NULL,
        resolved_at DATETIME,
        resolution_type VARCHAR(64),
        resolution_note TEXT,
        resolution_entry_id INTEGER,
        external_event_id INTEGER,
        PRIMARY KEY (id),
        CONSTRAINT uq_review_blockers_provider_id UNIQUE (provider, external_id, blocker_type),
        FOREIGN KEY(resolution_entry_id) REFERENCES journal_entries (id),
        FOREIGN KEY(external_event_id) REFERENCES external_events (id)
    )
    """,
    """
    CREATE TABLE reconciliation_sessions (
        id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        statement_path VARCHAR(500),
        statement_start DATE NOT NULL,
        statement_end DATE NOT NULL,
        statement_starting_balance_cents INTEGER NOT NULL,
        statement_ending_balance_cents INTEGER NOT NULL,
        status VARCHAR(32) NOT NULL,
        created_at DATETIME NOT NULL,
        closed_at DATETIME,
        PRIMARY KEY (id),
        FOREIGN KEY(account_id) REFERENCES accounts (id)
    )
    """,
    """
    CREATE TABLE reconciliation_lines (
        id INTEGER NOT NULL,
        session_id INTEGER NOT NULL,
        transaction_date DATE NOT NULL,
        description VARCHAR(500) NOT NULL,
        amount_cents INTEGER NOT NULL,
        external_ref VARCHAR(255),
        status VARCHAR(32) NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(session_id) REFERENCES reconciliation_sessions (id)
    )
    """,
    """
    CREATE TABLE reconciliation_matches (
        id INTEGER NOT NULL,
        reconciliation_line_id INTEGER NOT NULL,
        journal_line_id INTEGER NOT NULL,
        applied_amount_cents INTEGER NOT NULL,
        created_at DATETIME NOT NULL,
        reversed_at DATETIME,
        reversal_reason TEXT,
        PRIMARY KEY (id),
        FOREIGN KEY(reconciliation_line_id) REFERENCES reconciliation_lines (id),
        FOREIGN KEY(journal_line_id) REFERENCES journal_lines (id)
    )
    """,
    """
    CREATE TABLE reconciliation_session_events (
        id INTEGER NOT NULL,
        session_id INTEGER NOT NULL,
        event_type VARCHAR(32) NOT NULL,
        reason TEXT,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(session_id) REFERENCES reconciliation_sessions (id)
    )
    """,
    """
    CREATE TABLE tax_obligations (
        id INTEGER NOT NULL,
        code VARCHAR(128) NOT NULL,
        description VARCHAR(500) NOT NULL,
        jurisdiction VARCHAR(64) NOT NULL,
        due_date DATE NOT NULL,
        status VARCHAR(32) NOT NULL,
        period_start DATE,
        period_end DATE,
        liability_account_id INTEGER,
        amount_cents INTEGER,
        export_path VARCHAR(500),
        notes TEXT,
        PRIMARY KEY (id),
        FOREIGN KEY(liability_account_id) REFERENCES accounts (id)
    )
    """,
    """
    CREATE TABLE period_locks (
        id INTEGER NOT NULL,
        period_start DATE NOT NULL,
        period_end DATE NOT NULL,
        lock_type VARCHAR(32) NOT NULL,
        action VARCHAR(32) NOT NULL,
        reason TEXT,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE settings (
        "key" VARCHAR(128) NOT NULL,
        value_json TEXT NOT NULL,
        PRIMARY KEY ("key")
    )
    """,
]

INDEX_STATEMENTS = [
    "CREATE UNIQUE INDEX ix_accounts_code ON accounts (code)",
    "CREATE INDEX ix_accounts_kind ON accounts (kind)",
    "CREATE INDEX ix_document_links_document_id ON document_links (document_id)",
    "CREATE INDEX ix_document_links_target_id ON document_links (target_id)",
    "CREATE INDEX ix_document_links_target_type ON document_links (target_type)",
    "CREATE INDEX ix_documents_document_type ON documents (document_type)",
    "CREATE INDEX ix_documents_scope ON documents (scope)",
    "CREATE INDEX ix_documents_sha256 ON documents (sha256)",
    "CREATE INDEX ix_documents_tax_year ON documents (tax_year)",
    "CREATE INDEX ix_external_event_refresh_history_external_event_id ON external_event_refresh_history (external_event_id)",
    "CREATE INDEX ix_external_events_provider ON external_events (provider)",
    "CREATE INDEX ix_imports_source ON imports (source)",
    "CREATE INDEX ix_journal_entries_entry_date ON journal_entries (entry_date)",
    "CREATE INDEX ix_journal_entries_source_type ON journal_entries (source_type)",
    "CREATE INDEX ix_journal_lines_account_id ON journal_lines (account_id)",
    "CREATE INDEX ix_journal_lines_entry_id ON journal_lines (entry_id)",
    "CREATE INDEX ix_period_locks_period_end ON period_locks (period_end)",
    "CREATE INDEX ix_period_locks_period_start ON period_locks (period_start)",
    "CREATE INDEX ix_reconciliation_lines_session_id ON reconciliation_lines (session_id)",
    "CREATE INDEX ix_reconciliation_matches_journal_line_id ON reconciliation_matches (journal_line_id)",
    "CREATE INDEX ix_reconciliation_matches_reconciliation_line_id ON reconciliation_matches (reconciliation_line_id)",
    "CREATE INDEX ix_reconciliation_session_events_event_type ON reconciliation_session_events (event_type)",
    "CREATE INDEX ix_reconciliation_session_events_session_id ON reconciliation_session_events (session_id)",
    "CREATE INDEX ix_reconciliation_sessions_account_id ON reconciliation_sessions (account_id)",
    "CREATE INDEX ix_review_blockers_blocker_date ON review_blockers (blocker_date)",
    "CREATE INDEX ix_review_blockers_blocker_type ON review_blockers (blocker_type)",
    "CREATE INDEX ix_review_blockers_external_id ON review_blockers (external_id)",
    "CREATE INDEX ix_review_blockers_provider ON review_blockers (provider)",
    "CREATE INDEX ix_review_blockers_status ON review_blockers (status)",
    "CREATE INDEX ix_settlement_applications_applied_date ON settlement_applications (applied_date)",
    "CREATE INDEX ix_settlement_applications_settlement_line_id ON settlement_applications (settlement_line_id)",
    "CREATE INDEX ix_settlement_applications_source_line_id ON settlement_applications (source_line_id)",
    "CREATE UNIQUE INDEX ix_tax_obligations_code ON tax_obligations (code)",
    "CREATE INDEX ix_tax_obligations_due_date ON tax_obligations (due_date)",
    "CREATE INDEX ix_tax_obligations_jurisdiction ON tax_obligations (jurisdiction)",
]

DROP_TABLES = [
    "settings",
    "period_locks",
    "tax_obligations",
    "reconciliation_session_events",
    "reconciliation_matches",
    "reconciliation_lines",
    "reconciliation_sessions",
    "review_blockers",
    "external_event_refresh_history",
    "external_events",
    "settlement_applications",
    "journal_lines",
    "journal_entries",
    "imports",
    "document_links",
    "documents",
    "attachments",
    "accounts",
]


def upgrade() -> None:
    for statement in TABLE_STATEMENTS:
        op.execute(statement)
    for statement in INDEX_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for table_name in DROP_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table_name}")
