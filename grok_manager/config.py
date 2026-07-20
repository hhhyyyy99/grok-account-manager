from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .paths import (
    CONFIG_FILE,
    LEGACY_CONFIG_FILE,
    ensure_data_dirs,
    write_private_text_atomic,
)


@dataclass
class ManagerConfig:
    live_probe: bool = True
    probe_timeout_seconds: int = 20
    inspection_workers: int = 6
    login_workers: int = 2
    login_timeout_seconds: int = 300
    register_count: int = 1
    register_threads: int = 1
    mint_workers: int = 1
    auto_import_on_start: bool = True
    # CPA access_token guardian: refresh when within lead window; loop interval.
    cpa_guard_enabled: bool = True
    cpa_guard_interval_seconds: int = 300
    cpa_guard_lead_seconds: int = 1800

    def normalized(self) -> "ManagerConfig":
        self.probe_timeout_seconds = max(3, min(int(self.probe_timeout_seconds), 120))
        self.inspection_workers = max(1, min(int(self.inspection_workers), 32))
        self.login_workers = max(1, min(int(self.login_workers), 10))
        self.login_timeout_seconds = max(60, min(int(self.login_timeout_seconds), 1800))
        self.register_count = max(1, int(self.register_count))
        self.register_threads = max(1, min(int(self.register_threads), 10))
        self.mint_workers = max(0, min(int(self.mint_workers), 10))
        self.cpa_guard_enabled = bool(self.cpa_guard_enabled)
        self.cpa_guard_interval_seconds = max(30, min(int(self.cpa_guard_interval_seconds), 86400))
        self.cpa_guard_lead_seconds = max(60, min(int(self.cpa_guard_lead_seconds), 21600))
        return self


class ConfigStore:
    def __init__(self, path: Path = CONFIG_FILE):
        self.path = Path(path)

    def load(self) -> ManagerConfig:
        ensure_data_dirs()
        source = self.path
        if not source.is_file() and self.path == CONFIG_FILE and LEGACY_CONFIG_FILE.is_file():
            source = LEGACY_CONFIG_FILE
        if not source.is_file():
            return ManagerConfig().normalized()
        try:
            raw = json.loads(source.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("管理端配置读取失败: %s" % exc) from exc
        if not isinstance(raw, dict):
            raise ValueError("管理端配置必须是 JSON 对象")
        known = {item.name for item in fields(ManagerConfig)}
        values = {key: value for key, value in raw.items() if key in known}
        return ManagerConfig(**values).normalized()

    def save(self, config: ManagerConfig) -> Path:
        config.normalized()
        return write_private_text_atomic(
            self.path,
            json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n",
        )
