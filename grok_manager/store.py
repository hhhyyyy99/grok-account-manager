from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence

from .models import Account, AccountDraft, AccountStatus, InspectionResult, utc_now_iso
from .paths import DATABASE_FILE, ensure_data_dirs


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password TEXT NOT NULL DEFAULT '',
    sso_token TEXT NOT NULL DEFAULT '',
    access_token TEXT NOT NULL DEFAULT '',
    refresh_token TEXT NOT NULL DEFAULT '',
    token_expires_at TEXT NOT NULL DEFAULT '',
    sso_expires_at TEXT NOT NULL DEFAULT '',
    auth_file TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    source_modified_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unknown',
    status_detail TEXT NOT NULL DEFAULT '',
    sso_status TEXT NOT NULL DEFAULT 'unknown',
    sso_detail TEXT NOT NULL DEFAULT '',
    cpa_status TEXT NOT NULL DEFAULT 'unknown',
    cpa_detail TEXT NOT NULL DEFAULT '',
    last_checked_at TEXT NOT NULL DEFAULT '',
    last_login_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
CREATE INDEX IF NOT EXISTS idx_accounts_updated ON accounts(updated_at DESC);
"""


MIGRATION_COLUMNS = {
    "sso_expires_at": "TEXT NOT NULL DEFAULT ''",
    "sso_status": "TEXT NOT NULL DEFAULT 'unknown'",
    "sso_detail": "TEXT NOT NULL DEFAULT ''",
    "cpa_status": "TEXT NOT NULL DEFAULT 'unknown'",
    "cpa_detail": "TEXT NOT NULL DEFAULT ''",
    "source_modified_at": "TEXT NOT NULL DEFAULT ''",
}


class AccountStore:
    """SQLite-backed public account repository.

    Each operation opens its own connection, which keeps UI and worker threads
    independent and avoids sharing sqlite connection state across threads.
    """

    def __init__(self, path: Path = DATABASE_FILE):
        ensure_data_dirs()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            existing = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(accounts)").fetchall()
            }
            for name, definition in MIGRATION_COLUMNS.items():
                if name not in existing:
                    conn.execute("ALTER TABLE accounts ADD COLUMN %s %s" % (name, definition))
            conn.execute("PRAGMA user_version = 3")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 30000")
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:
                yield conn
        finally:
            conn.close()

    def upsert(self, draft: AccountDraft) -> Account:
        email = draft.email.strip().lower()
        if not email or "@" not in email:
            raise ValueError("无效邮箱: %r" % draft.email)
        now = utc_now_iso()
        values = (
            email,
            draft.password.strip(),
            draft.sso_token.strip(),
            draft.access_token.strip(),
            draft.refresh_token.strip(),
            draft.token_expires_at.strip(),
            draft.auth_file.strip(),
            draft.source.strip(),
            draft.source_modified_at.strip(),
            now,
            now,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO accounts (
                    email, password, sso_token, access_token, refresh_token,
                    token_expires_at, auth_file, source, source_modified_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    password = CASE WHEN excluded.password != '' THEN excluded.password ELSE accounts.password END,
                    sso_token = CASE
                        WHEN excluded.sso_token != '' AND (
                            accounts.last_login_at = '' OR
                            excluded.source_modified_at >= accounts.last_login_at
                        ) THEN excluded.sso_token
                        ELSE accounts.sso_token
                    END,
                    access_token = CASE WHEN excluded.access_token != '' THEN excluded.access_token ELSE accounts.access_token END,
                    refresh_token = CASE WHEN excluded.refresh_token != '' THEN excluded.refresh_token ELSE accounts.refresh_token END,
                    token_expires_at = CASE WHEN excluded.token_expires_at != '' THEN excluded.token_expires_at ELSE accounts.token_expires_at END,
                    auth_file = CASE WHEN excluded.auth_file != '' THEN excluded.auth_file ELSE accounts.auth_file END,
                    source = CASE WHEN excluded.source != '' THEN excluded.source ELSE accounts.source END,
                    source_modified_at = CASE
                        WHEN excluded.source_modified_at > accounts.source_modified_at
                        THEN excluded.source_modified_at
                        ELSE accounts.source_modified_at
                    END,
                    status = CASE
                        WHEN excluded.sso_token != '' AND excluded.sso_token != accounts.sso_token AND (
                            accounts.last_login_at = '' OR
                            excluded.source_modified_at >= accounts.last_login_at
                        ) THEN 'unknown'
                        WHEN excluded.access_token != '' AND excluded.access_token != accounts.access_token THEN 'unknown'
                        ELSE accounts.status
                    END,
                    status_detail = CASE
                        WHEN excluded.sso_token != '' AND excluded.sso_token != accounts.sso_token AND (
                            accounts.last_login_at = '' OR
                            excluded.source_modified_at >= accounts.last_login_at
                        ) THEN ''
                        WHEN excluded.access_token != '' AND excluded.access_token != accounts.access_token THEN ''
                        ELSE accounts.status_detail
                    END,
                    sso_status = CASE
                        WHEN excluded.sso_token != '' AND excluded.sso_token != accounts.sso_token AND (
                            accounts.last_login_at = '' OR
                            excluded.source_modified_at >= accounts.last_login_at
                        ) THEN 'unknown'
                        ELSE accounts.sso_status
                    END,
                    sso_detail = CASE
                        WHEN excluded.sso_token != '' AND excluded.sso_token != accounts.sso_token AND (
                            accounts.last_login_at = '' OR
                            excluded.source_modified_at >= accounts.last_login_at
                        ) THEN ''
                        ELSE accounts.sso_detail
                    END,
                    cpa_status = CASE
                        WHEN excluded.access_token != '' AND excluded.access_token != accounts.access_token THEN 'unknown'
                        ELSE accounts.cpa_status
                    END,
                    cpa_detail = CASE
                        WHEN excluded.access_token != '' AND excluded.access_token != accounts.access_token THEN ''
                        ELSE accounts.cpa_detail
                    END,
                    updated_at = excluded.updated_at
                """,
                values,
            )
        account = self.get_by_email(email)
        if account is None:
            raise RuntimeError("账号写入后无法读取: %s" % email)
        return account

    def upsert_many(self, drafts: Iterable[AccountDraft]) -> List[Account]:
        by_email: Dict[str, Account] = {}
        for draft in drafts:
            account = self.upsert(draft)
            by_email[account.email] = account
        return list(by_email.values())

    def get(self, account_id: int) -> Optional[Account]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
        return Account.from_row(row) if row else None

    def get_by_email(self, email: str) -> Optional[Account]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE email = ? COLLATE NOCASE", (email.strip(),)
            ).fetchone()
        return Account.from_row(row) if row else None

    def get_many(self, account_ids: Sequence[int]) -> List[Account]:
        ids = [int(value) for value in account_ids]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE id IN (%s) ORDER BY id" % placeholders, ids
            ).fetchall()
        return [Account.from_row(row) for row in rows]

    def list_accounts(
        self,
        search: str = "",
        status: str = "",
        limit: Optional[int] = None,
    ) -> List[Account]:
        clauses = []
        params = []
        if search.strip():
            escaped_search = (
                search.strip()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            clauses.append("email COLLATE NOCASE LIKE ? ESCAPE '\\'")
            params.append("%%%s%%" % escaped_search)
        if status.strip():
            clauses.append("status = ?")
            params.append(status.strip())
        sql = "SELECT * FROM accounts"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Account.from_row(row) for row in rows]

    def ids_for_statuses(self, statuses: Sequence[str]) -> List[int]:
        clean = [str(value) for value in statuses if str(value)]
        if not clean:
            return []
        placeholders = ",".join("?" for _ in clean)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM accounts WHERE status IN (%s) ORDER BY id" % placeholders,
                clean,
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def set_status(self, account_ids: Sequence[int], status: str, detail: str = "") -> None:
        ids = [int(value) for value in account_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        params = [status, detail[:1000], utc_now_iso()] + ids
        with self._connect() as conn:
            conn.execute(
                "UPDATE accounts SET status = ?, status_detail = ?, updated_at = ? "
                "WHERE id IN (%s)" % placeholders,
                params,
            )

    def apply_inspection(self, result: InspectionResult) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET status = ?, status_detail = ?, last_checked_at = ?,
                    token_expires_at = CASE WHEN ? != '' THEN ? ELSE token_expires_at END,
                    sso_expires_at = CASE WHEN ? != '' THEN ? ELSE sso_expires_at END,
                    sso_status = ?, sso_detail = ?, cpa_status = ?, cpa_detail = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    result.status,
                    result.detail[:1000],
                    result.checked_at,
                    result.expires_at,
                    result.expires_at,
                    result.sso_expires_at,
                    result.sso_expires_at,
                    result.sso_status or AccountStatus.UNKNOWN.value,
                    result.sso_detail[:1000],
                    result.cpa_status or AccountStatus.UNKNOWN.value,
                    result.cpa_detail[:1000],
                    result.checked_at,
                    result.account_id,
                ),
            )

    def apply_login_credentials(
        self,
        account_id: int,
        access_token: str,
        refresh_token: str,
        expires_at: str,
        auth_file: str,
        detail: str = "批量登录成功",
        sso_token: str = "",
    ) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET access_token = ?, refresh_token = ?, token_expires_at = ?,
                    sso_token = CASE WHEN ? != '' THEN ? ELSE sso_token END,
                    auth_file = ?, status = ?, status_detail = ?,
                    sso_status = ?, sso_detail = ?, cpa_status = ?, cpa_detail = ?,
                    last_login_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    access_token.strip(),
                    refresh_token.strip(),
                    expires_at.strip(),
                    sso_token.strip(),
                    sso_token.strip(),
                    auth_file.strip(),
                    AccountStatus.UNKNOWN.value,
                    detail[:1000],
                    AccountStatus.UNKNOWN.value,
                    "登录后待巡检",
                    AccountStatus.UNKNOWN.value,
                    "登录后待巡检",
                    now,
                    now,
                    int(account_id),
                ),
            )

    def delete(self, account_ids: Sequence[int]) -> int:
        ids = [int(value) for value in account_ids]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM accounts WHERE id IN (%s)" % placeholders, ids)
            return int(cursor.rowcount)

    def stats(self) -> Dict[str, int]:
        values = {"total": 0}
        with self._connect() as conn:
            values["total"] = int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
            rows = conn.execute("SELECT status, COUNT(*) count FROM accounts GROUP BY status").fetchall()
        for row in rows:
            values[str(row["status"])] = int(row["count"])
        return values
