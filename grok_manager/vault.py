from __future__ import annotations

import base64
import binascii
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from .paths import VAULT_FILE, ensure_data_dirs, write_private_text_atomic


VAULT_VERSION = 1
ENVELOPE_PREFIX = "gmv1:"
VERIFIER_CONTEXT = b"grok-account-manager:vault-verifier:v1"
VERIFIER_VALUE = b"grok-account-manager credential vault"


class VaultError(RuntimeError):
    pass


class VaultLockedError(VaultError):
    pass


class VaultPasswordError(VaultError):
    pass


class VaultFormatError(VaultError):
    pass


@dataclass(frozen=True)
class KdfParameters:
    memory_cost: int = 64 * 1024
    iterations: int = 3
    lanes: int = 4
    length: int = 32

    def as_json(self, salt: bytes) -> Dict[str, Any]:
        return {
            "name": "argon2id",
            "salt": _b64encode(salt),
            "memoryCostKiB": self.memory_cost,
            "iterations": self.iterations,
            "lanes": self.lanes,
            "length": self.length,
        }

    @classmethod
    def from_json(cls, value: Dict[str, Any]) -> tuple["KdfParameters", bytes]:
        if value.get("name") != "argon2id":
            raise VaultFormatError("凭据保险库 KDF 类型不受支持")
        try:
            parameters = cls(
                memory_cost=int(value["memoryCostKiB"]),
                iterations=int(value["iterations"]),
                lanes=int(value["lanes"]),
                length=int(value["length"]),
            )
            salt = _b64decode(str(value["salt"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise VaultFormatError("凭据保险库 KDF 参数无效") from exc
        if (
            len(salt) < 16
            or parameters.length != 32
            or parameters.memory_cost < 8 * 1024
            or parameters.iterations < 1
            or parameters.lanes < 1
        ):
            raise VaultFormatError("凭据保险库 KDF 参数不安全或无效")
        return parameters, salt


class CredentialVault:
    """Password-unlocked authenticated encryption for managed secrets."""

    def __init__(
        self,
        path: Path = VAULT_FILE,
        *,
        kdf_parameters: Optional[KdfParameters] = None,
    ):
        self.path = Path(path)
        self.kdf_parameters = kdf_parameters or KdfParameters()
        self._key: Optional[bytearray] = None
        self._lock = threading.RLock()

    @property
    def is_initialized(self) -> bool:
        return self.path.is_file()

    @property
    def is_unlocked(self) -> bool:
        with self._lock:
            return self._key is not None

    def initialize(self, password: str) -> None:
        if self.is_initialized:
            raise VaultError("凭据保险库已初始化")
        self._validate_new_password(password)
        ensure_data_dirs()
        salt = os.urandom(16)
        key = self._derive(password, salt, self.kdf_parameters)
        verifier = self._seal_with_key(key, VERIFIER_VALUE, VERIFIER_CONTEXT)
        document = {
            "version": VAULT_VERSION,
            "kdf": self.kdf_parameters.as_json(salt),
            "verifier": verifier,
            "secrets": {},
        }
        write_private_text_atomic(
            self.path,
            json.dumps(document, ensure_ascii=True, indent=2) + "\n",
        )
        with self._lock:
            self._replace_key(key)

    def unlock(self, password: str) -> None:
        document = self._load_metadata()
        parameters, salt = KdfParameters.from_json(document["kdf"])
        key = self._derive(password, salt, parameters)
        try:
            verifier = self._open_with_key(
                key,
                str(document["verifier"]),
                VERIFIER_CONTEXT,
            )
        except (InvalidTag, VaultFormatError) as exc:
            raise VaultPasswordError("主密码错误或保险库已损坏") from exc
        if verifier != VERIFIER_VALUE:
            raise VaultPasswordError("主密码错误或保险库已损坏")
        with self._lock:
            self._replace_key(key)

    def lock(self) -> None:
        with self._lock:
            self._replace_key(None)

    def encrypt_text(self, value: str, context: str) -> str:
        if not value:
            return ""
        return self.encrypt_bytes(str(value).encode("utf-8"), context)

    def decrypt_text(self, value: str, context: str) -> str:
        if not value:
            return ""
        if not self.is_encrypted(value):
            raise VaultFormatError("遇到未加密的凭据字段")
        try:
            return self.decrypt_bytes(value, context).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise VaultFormatError("凭据明文不是有效 UTF-8") from exc

    def encrypt_bytes(self, value: bytes, context: str) -> str:
        key = self._current_key()
        return self._seal_with_key(key, bytes(value), self._context_bytes(context))

    def decrypt_bytes(self, value: str, context: str) -> bytes:
        key = self._current_key()
        try:
            return self._open_with_key(key, value, self._context_bytes(context))
        except InvalidTag as exc:
            raise VaultFormatError("凭据密文校验失败") from exc

    def put_secret(self, name: str, value: str) -> None:
        key = str(name or "").strip()
        if not key:
            raise ValueError("保险库 secret 名称不能为空")
        with self._lock:
            document = self._load_metadata()
            secrets = document.setdefault("secrets", {})
            if not isinstance(secrets, dict):
                raise VaultFormatError("保险库 secret 区域无效")
            secrets[key] = self.encrypt_text(value, "vault-secret:%s" % key)
            write_private_text_atomic(
                self.path,
                json.dumps(document, ensure_ascii=True, indent=2) + "\n",
            )

    def get_secret(self, name: str) -> str:
        key = str(name or "").strip()
        with self._lock:
            document = self._load_metadata()
            secrets = document.get("secrets") or {}
            if not isinstance(secrets, dict):
                raise VaultFormatError("保险库 secret 区域无效")
            raw = str(secrets.get(key) or "")
            if not raw:
                return ""
            return self.decrypt_text(raw, "vault-secret:%s" % key)

    def delete_secret(self, name: str) -> bool:
        key = str(name or "").strip()
        if not key:
            return False
        with self._lock:
            document = self._load_metadata()
            secrets = document.get("secrets") or {}
            if not isinstance(secrets, dict) or key not in secrets:
                return False
            secrets.pop(key, None)
            document["secrets"] = secrets
            write_private_text_atomic(
                self.path,
                json.dumps(document, ensure_ascii=True, indent=2) + "\n",
            )
            return True

    @staticmethod
    def is_encrypted(value: str) -> bool:
        return str(value or "").startswith(ENVELOPE_PREFIX)

    @staticmethod
    def _validate_new_password(password: str) -> None:
        if len(str(password or "")) < 12:
            raise VaultPasswordError("主密码至少需要 12 个字符")

    @staticmethod
    def _derive(password: str, salt: bytes, parameters: KdfParameters) -> bytes:
        try:
            material = str(password).encode("utf-8")
        except UnicodeEncodeError as exc:
            raise VaultPasswordError("主密码编码失败") from exc
        return Argon2id(
            salt=salt,
            length=parameters.length,
            iterations=parameters.iterations,
            lanes=parameters.lanes,
            memory_cost=parameters.memory_cost,
        ).derive(material)

    def _load_metadata(self) -> Dict[str, Any]:
        if not self.is_initialized:
            raise VaultError("凭据保险库尚未初始化")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VaultFormatError("凭据保险库元数据无法读取") from exc
        if not isinstance(document, dict) or document.get("version") != VAULT_VERSION:
            raise VaultFormatError("凭据保险库版本不受支持")
        if not isinstance(document.get("kdf"), dict) or not isinstance(
            document.get("verifier"), str
        ):
            raise VaultFormatError("凭据保险库元数据不完整")
        return document

    def _current_key(self) -> bytes:
        with self._lock:
            if self._key is None:
                raise VaultLockedError("凭据保险库已锁定")
            return bytes(self._key)

    def _replace_key(self, key: Optional[bytes]) -> None:
        if self._key is not None:
            for index in range(len(self._key)):
                self._key[index] = 0
        self._key = bytearray(key) if key is not None else None

    @staticmethod
    def _context_bytes(context: str) -> bytes:
        value = str(context or "").strip()
        if not value:
            raise ValueError("凭据加密必须提供上下文")
        return value.encode("utf-8")

    @staticmethod
    def _seal_with_key(key: bytes, value: bytes, context: bytes) -> str:
        nonce = os.urandom(12)
        encrypted = AESGCM(key).encrypt(nonce, value, context)
        return ENVELOPE_PREFIX + _b64encode(nonce + encrypted)

    @staticmethod
    def _open_with_key(key: bytes, value: str, context: bytes) -> bytes:
        if not str(value).startswith(ENVELOPE_PREFIX):
            raise VaultFormatError("凭据密文格式无效")
        payload = _b64decode(str(value)[len(ENVELOPE_PREFIX) :])
        if len(payload) < 12 + 16:
            raise VaultFormatError("凭据密文长度无效")
        return AESGCM(key).decrypt(payload[:12], payload[12:], context)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        if _b64encode(decoded) != value:
            raise ValueError("non-canonical base64")
        return decoded
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise VaultFormatError("凭据密文 Base64 无效") from exc
