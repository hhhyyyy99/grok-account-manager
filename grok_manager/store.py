from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from .models import Account, AccountDraft, AccountStatus, InspectionResult, utc_now_iso
from .paths import DATABASE_FILE, ensure_data_dirs, write_private_text_atomic
from .vault import CredentialVault, VaultLockedError


SENSITIVE_ACCOUNT_FIELDS = ("password", "sso_token", "access_token", "refresh_token", "auth_file")


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
    cpa_updated_at TEXT NOT NULL DEFAULT '',
    last_checked_at TEXT NOT NULL DEFAULT '',
    last_login_at TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
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
    "cpa_updated_at": "TEXT NOT NULL DEFAULT ''",
    "source_modified_at": "TEXT NOT NULL DEFAULT ''",
    "enabled": "INTEGER NOT NULL DEFAULT 1",
}


class AccountStore:
    """SQLite-backed public account repository.

    Each operation opens its own connection, which keeps UI and worker threads
    independent and avoids sharing sqlite connection state across threads.
    """

    def __init__(
        self,
        path: Path = DATABASE_FILE,
        vault: Optional[CredentialVault] = None,
    ):
        ensure_data_dirs()
        self.path = Path(path)
        self.vault = vault or CredentialVault(
            self.path.with_name("credentials.vault.json")
        )
        if not self.vault.is_unlocked:
            raise VaultLockedError("账号存储需要已解锁的凭据保险库")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._account_locks: Dict[int, threading.RLock] = {}
        self._account_locks_guard = threading.Lock()
        with self._connect() as conn:
            conn.execute("PRAGMA secure_delete = ON")
            conn.executescript(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            existing = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(accounts)").fetchall()
            }
            for name, definition in MIGRATION_COLUMNS.items():
                if name not in existing:
                    conn.execute("ALTER TABLE accounts ADD COLUMN %s %s" % (name, definition))
            conn.execute(
                "UPDATE accounts SET status = 'unknown', status_detail = '' "
                "WHERE status = 'logging_in'"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_accounts_enabled ON accounts(enabled)"
            )
            conn.execute("PRAGMA user_version = 5")
        if self._migrate_plaintext_credentials():
            self._compact_after_migration()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def account_lock(self, account_id: int) -> threading.RLock:
        """Per-account reentrant lock shared by login, remint, silent refresh, expire marks."""
        key = int(account_id)
        with self._account_locks_guard:
            lock = self._account_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._account_locks[key] = lock
            return lock

    def _credential_context(self, email: str, field: str) -> str:
        return "account:%s:%s" % (email.strip().lower(), field)

    def _encrypt_credential(self, email: str, field: str, value: str) -> str:
        normalized = str(value or "").strip()
        return (
            self.vault.encrypt_text(normalized, self._credential_context(email, field))
            if normalized
            else ""
        )

    def _decrypt_row(self, row: Mapping[str, Any]) -> Account:
        values = dict(row)
        email = str(values.get("email") or "").strip().lower()
        for field in SENSITIVE_ACCOUNT_FIELDS:
            raw = str(values.get(field) or "")
            if raw and not self.vault.is_encrypted(raw):
                raise VaultLockedError(
                    "账号库仍包含未迁移的明文凭据，请使用带主密码的应用启动"
                )
            values[field] = (
                self.vault.decrypt_text(raw, self._credential_context(email, field))
                if raw
                else ""
            )
        return Account.from_row(values)

    def _migrate_plaintext_credentials(self) -> bool:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM accounts").fetchall()
            plaintext_rows = [
                row
                for row in rows
                if any(
                    str(row[field] or "") and not self.vault.is_encrypted(str(row[field]))
                    for field in SENSITIVE_ACCOUNT_FIELDS
                )
            ]
            if not plaintext_rows:
                return False
            dump = "\n".join(conn.iterdump()) + "\n"
            backup = self.path.with_name("accounts.sqlite3.pre-vault-v1.sql.gmvault")
            if not backup.exists():
                write_private_text_atomic(
                    backup,
                    self.vault.encrypt_text(dump, "account-store:pre-vault-v1-backup"),
                )
            for row in plaintext_rows:
                email = str(row["email"]).strip().lower()
                updates = {
                    field: self._encrypt_credential(email, field, str(row[field] or ""))
                    for field in SENSITIVE_ACCOUNT_FIELDS
                }
                conn.execute(
                    "UPDATE accounts SET password = ?, sso_token = ?, "
                    "access_token = ?, refresh_token = ?, auth_file = ? WHERE id = ?",
                    (
                        updates["password"],
                        updates["sso_token"],
                        updates["access_token"],
                        updates["refresh_token"],
                        updates["auth_file"],
                        int(row["id"]),
                    ),
                )
            return True

    def _compact_after_migration(self) -> None:
        connection = sqlite3.connect(str(self.path), timeout=30)
        try:
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("VACUUM")
        finally:
            connection.close()

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
            conn.execute("PRAGMA secure_delete = ON")
            with conn:
                yield conn
        finally:
            conn.close()

    def upsert(self, draft: AccountDraft) -> Account:
        email = draft.email.strip().lower()
        if not email or "@" not in email:
            raise ValueError("无效邮箱: %r" % draft.email)
        now = utc_now_iso()
        existing = self.get_by_email(email)
        password = draft.password.strip()
        sso_token = draft.sso_token.strip()
        access_token = draft.access_token.strip()
        refresh_token = draft.refresh_token.strip()
        auth_file = draft.auth_file.strip()
        if existing is not None:
            if password == existing.password:
                password = ""
            if sso_token == existing.sso_token:
                sso_token = ""
            if access_token == existing.access_token:
                access_token = ""
            if refresh_token == existing.refresh_token:
                refresh_token = ""
            if auth_file == existing.auth_file:
                auth_file = ""
        cpa_material = bool(access_token or refresh_token or auth_file or draft.token_expires_at.strip())
        # SSO and CPA use independent clocks. source_modified_at is accounts.txt/SSO age;
        # cpa_source_modified_at is auth last_refresh (or file mtime fallback).
        source_modified = draft.source_modified_at.strip()
        cpa_source_modified = (
            str(getattr(draft, "cpa_source_modified_at", "") or "").strip()
            or source_modified
        )
        cpa_stamp = cpa_source_modified or now if cpa_material else ""
        values = (
            email,
            self._encrypt_credential(email, "password", password),
            self._encrypt_credential(email, "sso_token", sso_token),
            self._encrypt_credential(email, "access_token", access_token),
            self._encrypt_credential(email, "refresh_token", refresh_token),
            draft.token_expires_at.strip(),
            self._encrypt_credential(email, "auth_file", auth_file),
            draft.source.strip(),
            source_modified,
            cpa_stamp,
            now,
            now,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO accounts (
                    email, password, sso_token, access_token, refresh_token,
                    token_expires_at, auth_file, source, source_modified_at,
                    cpa_updated_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    password = CASE WHEN excluded.password != '' THEN excluded.password ELSE accounts.password END,
                    sso_token = CASE
                        WHEN excluded.sso_token != '' AND (
                            accounts.last_login_at = '' OR
                            excluded.source_modified_at >= accounts.last_login_at
                        ) THEN excluded.sso_token
                        ELSE accounts.sso_token
                    END,
                    access_token = CASE
                        WHEN excluded.access_token != '' AND (
                            accounts.cpa_updated_at = ''
                            OR (
                                excluded.cpa_updated_at != ''
                                AND excluded.cpa_updated_at >= accounts.cpa_updated_at
                            )
                        ) THEN excluded.access_token ELSE accounts.access_token END,
                    refresh_token = CASE
                        WHEN excluded.refresh_token != '' AND (
                            accounts.cpa_updated_at = ''
                            OR (
                                excluded.cpa_updated_at != ''
                                AND excluded.cpa_updated_at >= accounts.cpa_updated_at
                            )
                        ) THEN excluded.refresh_token ELSE accounts.refresh_token END,
                    token_expires_at = CASE
                        WHEN excluded.token_expires_at != '' AND (
                            accounts.cpa_updated_at = ''
                            OR (
                                excluded.cpa_updated_at != ''
                                AND excluded.cpa_updated_at >= accounts.cpa_updated_at
                            )
                        ) THEN excluded.token_expires_at ELSE accounts.token_expires_at END,
                    auth_file = CASE
                        WHEN excluded.auth_file != '' AND (
                            accounts.cpa_updated_at = ''
                            OR (
                                excluded.cpa_updated_at != ''
                                AND excluded.cpa_updated_at >= accounts.cpa_updated_at
                            )
                        ) THEN excluded.auth_file ELSE accounts.auth_file END,
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
                        WHEN excluded.access_token != '' AND excluded.access_token != accounts.access_token AND (
                            accounts.cpa_updated_at = ''
                            OR (
                                excluded.cpa_updated_at != ''
                                AND excluded.cpa_updated_at >= accounts.cpa_updated_at
                            )
                        ) THEN 'unknown'
                        ELSE accounts.status
                    END,
                    status_detail = CASE
                        WHEN excluded.sso_token != '' AND excluded.sso_token != accounts.sso_token AND (
                            accounts.last_login_at = '' OR
                            excluded.source_modified_at >= accounts.last_login_at
                        ) THEN ''
                        WHEN excluded.access_token != '' AND excluded.access_token != accounts.access_token AND (
                            accounts.cpa_updated_at = ''
                            OR (
                                excluded.cpa_updated_at != ''
                                AND excluded.cpa_updated_at >= accounts.cpa_updated_at
                            )
                        ) THEN ''
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
                        WHEN excluded.access_token != '' AND excluded.access_token != accounts.access_token AND (
                            accounts.cpa_updated_at = ''
                            OR (
                                excluded.cpa_updated_at != ''
                                AND excluded.cpa_updated_at >= accounts.cpa_updated_at
                            )
                        ) THEN 'unknown'
                        ELSE accounts.cpa_status
                    END,
                    cpa_detail = CASE
                        WHEN excluded.access_token != '' AND excluded.access_token != accounts.access_token AND (
                            accounts.cpa_updated_at = ''
                            OR (
                                excluded.cpa_updated_at != ''
                                AND excluded.cpa_updated_at >= accounts.cpa_updated_at
                            )
                        ) THEN ''
                        ELSE accounts.cpa_detail
                    END,
                    cpa_updated_at = CASE
                        WHEN (
                            (excluded.access_token != '' AND excluded.access_token != accounts.access_token)
                            OR (excluded.refresh_token != '' AND excluded.refresh_token != accounts.refresh_token)
                            OR (excluded.token_expires_at != '' AND excluded.token_expires_at != accounts.token_expires_at)
                            OR (excluded.auth_file != '' AND excluded.auth_file != accounts.auth_file)
                        ) AND (
                            accounts.cpa_updated_at = ''
                            OR (
                                excluded.cpa_updated_at != ''
                                AND excluded.cpa_updated_at >= accounts.cpa_updated_at
                            )
                        ) THEN excluded.cpa_updated_at
                        ELSE accounts.cpa_updated_at
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
        return self._decrypt_row(row) if row else None

    def get_by_email(self, email: str) -> Optional[Account]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE email = ? COLLATE NOCASE", (email.strip(),)
            ).fetchone()
        return self._decrypt_row(row) if row else None

    def get_many(self, account_ids: Sequence[int]) -> List[Account]:
        ids = [int(value) for value in account_ids]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE id IN (%s) ORDER BY id" % placeholders, ids
            ).fetchall()
        return [self._decrypt_row(row) for row in rows]

    def list_accounts(
        self,
        search: str = "",
        status: str = "",
        enabled: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[Account]:
        where_sql, params = self._account_filter(search, status, enabled)
        sql = "SELECT * FROM accounts" + where_sql
        sql += " ORDER BY enabled DESC, id DESC"
        clean_offset = max(0, int(offset))
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend((max(1, int(limit)), clean_offset))
        elif clean_offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(clean_offset)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._decrypt_row(row) for row in rows]

    def count_accounts(
        self,
        search: str = "",
        status: str = "",
        enabled: Optional[bool] = None,
    ) -> int:
        where_sql, params = self._account_filter(search, status, enabled)
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM accounts" + where_sql, params).fetchone()
        return int(row[0])

    @staticmethod
    def _account_filter(
        search: str,
        status: str,
        enabled: Optional[bool] = None,
    ) -> Tuple[str, List[Any]]:
        clauses = []
        params: List[Any] = []
        if search.strip():
            escaped_search = (
                search.strip()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            clauses.append("email COLLATE NOCASE LIKE ? ESCAPE '\\'")
            params.append("%%%s%%" % escaped_search)
        clean_status = status.strip()
        if clean_status == AccountStatus.MISSING_CPA.value:
            clauses.append("access_token = '' AND auth_file = ''")
        elif clean_status:
            clauses.append("status = ?")
            params.append(clean_status)
        if enabled is not None:
            clauses.append("enabled = ?")
            params.append(1 if enabled else 0)
        return (" WHERE " + " AND ".join(clauses) if clauses else "", params)

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

    def ids_for_cpa_statuses(self, statuses: Sequence[str]) -> List[int]:
        clean = [str(value) for value in statuses if str(value)]
        if not clean:
            return []
        placeholders = ",".join("?" for _ in clean)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM accounts WHERE cpa_status IN (%s) ORDER BY id" % placeholders,
                clean,
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def mark_cpa_expired(self, account_id: int, detail: str = "CPA 凭据已过期") -> None:
        """Mark CPA (and overall status) expired without touching SSO fields."""
        with self.account_lock(account_id):
            self._mark_cpa_expired_unlocked(account_id, detail)

    def mark_cpa_expired_if_refresh_unchanged(
        self,
        account_id: int,
        expected_refresh: Optional[str],
        detail: str = "CPA 凭据已过期",
        *,
        expected_access: Optional[str] = None,
        expected_cpa_updated_at: Optional[str] = None,
    ) -> bool:
        """Expire only when every provided CPA snapshot field still matches.

        None means the field was not included in the snapshot. An empty string is an
        explicit snapshot value and must compare equal exactly.
        """
        with self.account_lock(account_id):
            account = self.get(account_id)
            if account is None:
                return False
            current_refresh = str(account.refresh_token or "").strip()
            if expected_refresh is not None and current_refresh != str(expected_refresh).strip():
                return False
            if expected_access is not None:
                current_access = str(account.access_token or "").strip()
                if current_access != str(expected_access).strip():
                    return False
            if expected_cpa_updated_at is not None:
                current_stamp = str(account.cpa_updated_at or "").strip()
                if current_stamp != str(expected_cpa_updated_at).strip():
                    return False
            self._mark_cpa_expired_unlocked(account_id, detail)
            return True

    def _mark_cpa_expired_unlocked(self, account_id: int, detail: str) -> None:
        now = utc_now_iso()
        text = str(detail or "CPA 凭据已过期")[:1000]
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET cpa_status = ?, cpa_detail = ?,
                    status = ?, status_detail = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    AccountStatus.EXPIRED.value,
                    text,
                    AccountStatus.EXPIRED.value,
                    text,
                    now,
                    int(account_id),
                ),
            )

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
        # Hold the account lock and refuse stale snapshots that predate a CPA rotation.
        with self.account_lock(result.account_id):
            current = self.get(result.account_id)
            if current is None:
                return
            observed_access = str(getattr(result, "observed_access_token", "") or "").strip()
            observed_cpa_updated = str(
                getattr(result, "observed_cpa_updated_at", "") or ""
            ).strip()
            observed_sso = str(getattr(result, "observed_sso_token", "") or "").strip()
            observed_last_login = str(
                getattr(result, "observed_last_login_at", "") or ""
            ).strip()
            current_access = str(current.access_token or "").strip()
            current_stamp = str(current.cpa_updated_at or "").strip()
            current_sso = str(current.sso_token or "").strip()
            current_last_login = str(current.last_login_at or "").strip()
            if bool(getattr(result, "cpa_snapshot", False)):
                # Empty observation means the probe saw no CPA material; refuse to
                # overwrite an account that gained CPA credentials after the probe.
                if not observed_access and current_access:
                    return
                if observed_access and observed_access != current_access:
                    return
                if (
                    observed_cpa_updated
                    and current_stamp
                    and observed_cpa_updated != current_stamp
                ):
                    return
                if not observed_cpa_updated and current_stamp and current_access:
                    return
            elif observed_access or observed_cpa_updated:
                if observed_access and observed_access != current_access:
                    return
                if (
                    observed_cpa_updated
                    and current_stamp
                    and observed_cpa_updated != current_stamp
                ):
                    return
            if bool(getattr(result, "sso_snapshot", False)):
                if observed_sso != current_sso or observed_last_login != current_last_login:
                    return
            elif observed_sso or observed_last_login:
                if observed_sso and observed_sso != current_sso:
                    return
                if observed_last_login and observed_last_login != current_last_login:
                    return
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
        with self.account_lock(account_id):
            now = utc_now_iso()
            account = self.get(account_id)
            if account is None:
                raise ValueError("登录凭据对应的账号不存在")
            # Login deliberately stores the provided auth_file (often empty after the
            # transient managed file is deleted). CPA renewals use apply_cpa_credentials
            # which preserves an existing pointer when the new value is blank.
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE accounts
                    SET access_token = ?, refresh_token = ?, token_expires_at = ?,
                        sso_token = CASE WHEN ? != '' THEN ? ELSE sso_token END,
                        auth_file = ?, status = ?, status_detail = ?,
                        sso_status = ?, sso_detail = ?, cpa_status = ?, cpa_detail = ?,
                        cpa_updated_at = ?,
                        last_login_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        self._encrypt_credential(account.email, "access_token", access_token),
                        self._encrypt_credential(account.email, "refresh_token", refresh_token),
                        expires_at.strip(),
                        self._encrypt_credential(account.email, "sso_token", sso_token),
                        self._encrypt_credential(account.email, "sso_token", sso_token),
                        self._encrypt_credential(account.email, "auth_file", auth_file),
                        AccountStatus.UNKNOWN.value,
                        detail[:1000],
                        AccountStatus.UNKNOWN.value,
                        "登录后待巡检",
                        AccountStatus.UNKNOWN.value,
                        "登录后待巡检",
                        now,
                        now,
                        now,
                        int(account_id),
                    ),
                )

    def apply_cpa_credentials(
        self,
        account_id: int,
        access_token: str,
        refresh_token: str,
        expires_at: str,
        auth_file: str = "",
        detail: str = "CPA 凭据已续期",
        *,
        preserve_status: bool = False,
    ) -> None:
        """Update CPA tokens only; leave SSO fields untouched.

        auth_file is a metadata pointer (often the hotload path). Empty input keeps
        the existing pointer so temporary managed files can be deleted without
        wiping inspection metadata.
        """
        with self.account_lock(account_id):
            now = utc_now_iso()
            account = self.get(account_id)
            if account is None:
                raise ValueError("CPA 续期对应的账号不存在")
            access_token = str(access_token or "").strip()
            refresh_token = str(refresh_token or "").strip()
            if not access_token or not refresh_token:
                raise ValueError("CPA 续期需要 access_token 与 refresh_token")
            auth_file = str(auth_file or "").strip()
            if preserve_status:
                status = account.status
                status_detail = detail[:1000] if detail else account.status_detail
                cpa_status = account.cpa_status
                cpa_detail = detail[:1000] if detail else account.cpa_detail
            else:
                status = AccountStatus.UNKNOWN.value
                status_detail = detail[:1000]
                cpa_status = AccountStatus.UNKNOWN.value
                cpa_detail = "续期后待巡检"
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE accounts
                    SET access_token = ?, refresh_token = ?, token_expires_at = ?,
                        auth_file = CASE WHEN ? != '' THEN ? ELSE auth_file END,
                        status = ?, status_detail = ?,
                        cpa_status = ?, cpa_detail = ?,
                        cpa_updated_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        self._encrypt_credential(account.email, "access_token", access_token),
                        self._encrypt_credential(account.email, "refresh_token", refresh_token),
                        expires_at.strip(),
                        self._encrypt_credential(account.email, "auth_file", auth_file),
                        self._encrypt_credential(account.email, "auth_file", auth_file),
                        status,
                        status_detail,
                        cpa_status,
                        cpa_detail,
                        now,
                        now,
                        int(account_id),
                    ),
                )

    def touch_cpa_auth_file(self, account_id: int, auth_file: str) -> None:
        """Update only the CPA auth_file metadata pointer."""
        path = str(auth_file or "").strip()
        if not path:
            return
        account = self.get(account_id)
        if account is None:
            raise ValueError("账号不存在")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET auth_file = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    self._encrypt_credential(account.email, "auth_file", path),
                    utc_now_iso(),
                    int(account_id),
                ),
            )

    def apply_password_reset(self, account_id: int, password: str) -> None:
        normalized = str(password or "").strip()
        if not normalized:
            raise ValueError("新密码不能为空")
        now = utc_now_iso()
        account = self.get(account_id)
        if account is None:
            raise ValueError("密码重置对应的账号不存在")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE accounts
                SET password = ?, status = ?, status_detail = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    self._encrypt_credential(account.email, "password", normalized),
                    AccountStatus.NEEDS_LOGIN.value,
                    "密码已重置，等待重新登录",
                    now,
                    int(account_id),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("密码重置对应的账号不存在")

    def delete(self, account_ids: Sequence[int]) -> int:
        ids = [int(value) for value in account_ids]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM accounts WHERE id IN (%s)" % placeholders, ids)
            return int(cursor.rowcount)

    def set_enabled(self, account_ids: Sequence[int], enabled: bool) -> int:
        ids = [int(value) for value in account_ids]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        params: List[Any] = [1 if enabled else 0, utc_now_iso()] + ids
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE accounts SET enabled = ?, updated_at = ? "
                "WHERE id IN (%s)" % placeholders,
                params,
            )
            return int(cursor.rowcount)

    def stats(self) -> Dict[str, int]:
        values = {"total": 0, "enabled": 0, "disabled": 0}
        with self._connect() as conn:
            values["total"] = int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
            values["enabled"] = int(
                conn.execute("SELECT COUNT(*) FROM accounts WHERE enabled = 1").fetchone()[0]
            )
            values["disabled"] = int(
                conn.execute("SELECT COUNT(*) FROM accounts WHERE enabled = 0").fetchone()[0]
            )
            rows = conn.execute("SELECT status, COUNT(*) count FROM accounts GROUP BY status").fetchall()
        for row in rows:
            values[str(row["status"])] = int(row["count"])
        return values
