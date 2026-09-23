#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloudflare 多线路容灾与 Telegram 管理中心。

这是一个单文件、仅依赖 Python 标准库的 Linux 运维工具。它负责保存线路
配置、生成监控/手动切换脚本、管理 systemd，并以带备份的方式更新 tg_bot.py。

安全设计要点：
* 所有生成动作都先写入临时文件再原子替换；
* 修改 Telegram 机器人前始终创建 .bak，并在写入后重新执行 AST 语法检查；
* 破坏性操作要求二次确认；
* CF_MANAGER_ROOT/CF_MANAGER_DRY_RUN 可用于测试和演练，避免在开发机误操作。
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import datetime
import io
import getpass
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence


VERSION = "2.0.0"


class Colors:
    """ANSI 颜色。NO_COLOR 或非交互终端会关闭颜色，方便日志重定向。"""

    RESET = "\033[0m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"
    BOLD = "\033[1m"

    @classmethod
    def enabled(cls) -> bool:
        return not os.environ.get("NO_COLOR") and sys.stdout.isatty()


def colour(text: str, code: str) -> str:
    return f"{code}{text}{Colors.RESET}" if Colors.enabled() else text


def info(message: str) -> None:
    print(colour(f"[信息] {message}", Colors.CYAN))


def success(message: str) -> None:
    print(colour(f"[完成] {message}", Colors.GREEN))


def warning(message: str) -> None:
    print(colour(f"[注意] {message}", Colors.YELLOW))


def error(message: str) -> None:
    print(colour(f"[错误] {message}", Colors.RED), file=sys.stderr)


class ManagerError(RuntimeError):
    """用户可理解的业务错误。"""


def _env_path(name: str, default: str | Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


class Paths:
    """生产路径集中管理，测试时可通过 CF_MANAGER_ROOT 重定向。"""

    def __init__(self) -> None:
        root = os.environ.get("CF_MANAGER_ROOT")
        if root:
            base = Path(root).expanduser().resolve()
            self.bin_dir = _env_path("CF_BIN_DIR", base / "usr" / "local" / "bin")
            self.systemd_dir = _env_path(
                "CF_SYSTEMD_DIR", base / "etc" / "systemd" / "system"
            )
            self.tg_bot = _env_path("CF_TG_BOT", base / "root" / "tg_bot.py")
            self.config_file = _env_path(
                "CF_MANAGER_CONFIG", base / "etc" / "cf_manager" / "lines.json"
            )
            self.backup_dir = _env_path("CF_BACKUP_DIR", base / "root")
            self.runtime_dir = _env_path("CF_RUNTIME_DIR", base / "tmp")
        else:
            self.bin_dir = _env_path("CF_BIN_DIR", "/usr/local/bin")
            self.systemd_dir = _env_path(
                "CF_SYSTEMD_DIR", "/etc/systemd/system"
            )
            self.tg_bot = _env_path("CF_TG_BOT", "/root/tg_bot.py")
            self.config_file = _env_path(
                "CF_MANAGER_CONFIG", "/etc/cf_manager/lines.json"
            )
            self.backup_dir = _env_path("CF_BACKUP_DIR", "/root")
            self.runtime_dir = _env_path("CF_RUNTIME_DIR", "/tmp")

    def failover_path(self, line_id: int) -> Path:
        # 1 号线兼容历史名称 cf_failover.py，新增/重建线路统一带编号。
        return self.bin_dir / ("cf_failover.py" if line_id == 1 else f"cf_failover_{line_id}.py")

    def manual_path(self, line_id: int) -> Path:
        return self.bin_dir / f"cf{line_id}.py"

    def service_path(self, line_id: int) -> Path:
        return self.systemd_dir / f"cf-failover-{line_id}.service"

    def state_path(self, line_id: int) -> Path:
        return self.runtime_dir / f"cf_failover_{line_id}_state.txt"

    def lock_path(self, line_id: int) -> Path:
        return self.runtime_dir / f"cf_failover_{line_id}.lock"


@dataclasses.dataclass
class DomainRecord:
    zone_id: str
    record_id: str
    name: str
    proxied: bool = False

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DomainRecord":
        record_id = value.get("record_id") or value.get("rec_id") or value.get("id")
        if not value.get("zone_id") or not record_id or not value.get("name"):
            raise ManagerError("域名记录必须包含 zone_id、record_id/rec_id 和 name")
        return cls(
            zone_id=str(value["zone_id"]),
            record_id=str(record_id),
            name=str(value["name"]),
            proxied=bool(value.get("proxied", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        # 同时保留 rec_id 别名，兼容旧脚本和规格书中的字段名称。
        return {
            "zone_id": self.zone_id,
            "record_id": self.record_id,
            "rec_id": self.record_id,
            "name": self.name,
            "proxied": False,
        }


@dataclasses.dataclass
class LineConfig:
    line_id: int
    alias: str
    region: str
    main_ip: str
    backup_ip: str
    port: int = 22
    failover_threshold_ms: int = 280
    recovery_threshold_ms: int = 160
    external_threshold_ms: int = 150
    probe_count: int = 5
    probe_timeout_s: float = 2.5
    confirmation_delay_s: int = 20
    interval_s: int = 30
    domains: list[DomainRecord] = dataclasses.field(default_factory=list)

    def validate(self) -> None:
        if self.line_id < 1 or self.line_id > 9999:
            raise ManagerError("线路编号必须在 1~9999 之间")
        if not self.alias.strip():
            raise ManagerError("线路别名不能为空")
        if self.region not in REGIONS:
            raise ManagerError(f"区域必须是 {', '.join(REGIONS)}")
        for label, address in (("主节点", self.main_ip), ("备用节点", self.backup_ip)):
            try:
                ipaddress.IPv4Address(address)
            except ipaddress.AddressValueError as exc:
                raise ManagerError(f"{label}不是合法 IPv4 地址: {address}") from exc
        if not 1 <= self.port <= 65535:
            raise ManagerError("探活端口必须在 1~65535 之间")
        if self.failover_threshold_ms <= 0:
            raise ManagerError("切备阈值必须大于 0")
        if self.recovery_threshold_ms <= 0:
            raise ManagerError("恢复阈值必须大于 0")
        if self.external_threshold_ms <= 0:
            raise ManagerError("外部仲裁阈值必须大于 0")
        if self.probe_count < 1 or self.probe_count > 20:
            raise ManagerError("探测包数量必须在 1~20 之间")
        if self.confirmation_delay_s < 0 or self.interval_s < 1:
            raise ManagerError("复核等待时间不能为负，轮询周期必须大于 0")
        if not self.domains:
            raise ManagerError("至少需要绑定一条 Cloudflare A 记录")
        for domain in self.domains:
            DomainRecord.from_dict(domain.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LineConfig":
        domains = [DomainRecord.from_dict(item) for item in value.get("domains", [])]
        config = cls(
            line_id=int(value.get("line_id", value.get("id"))),
            alias=str(value.get("alias", "")),
            region=str(value.get("region", "jp")),
            main_ip=str(value.get("main_ip", "")),
            backup_ip=str(value.get("backup_ip", "")),
            port=int(value.get("port", 22)),
            failover_threshold_ms=int(value.get("failover_threshold_ms", 280)),
            recovery_threshold_ms=int(value.get("recovery_threshold_ms", 160)),
            external_threshold_ms=int(value.get("external_threshold_ms", 150)),
            probe_count=int(value.get("probe_count", 5)),
            probe_timeout_s=float(value.get("probe_timeout_s", 2.5)),
            confirmation_delay_s=int(value.get("confirmation_delay_s", 20)),
            interval_s=int(value.get("interval_s", 30)),
            domains=domains,
        )
        return config

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_id": self.line_id,
            "alias": self.alias,
            "region": self.region,
            "main_ip": self.main_ip,
            "backup_ip": self.backup_ip,
            "port": self.port,
            "failover_threshold_ms": self.failover_threshold_ms,
            "recovery_threshold_ms": self.recovery_threshold_ms,
            "external_threshold_ms": self.external_threshold_ms,
            "probe_count": self.probe_count,
            "probe_timeout_s": self.probe_timeout_s,
            "confirmation_delay_s": self.confirmation_delay_s,
            "interval_s": self.interval_s,
            "domains": [item.to_dict() for item in self.domains],
        }


REGIONS: dict[str, tuple[str, ...]] = {
    "jp": ("jp", "hk", "sg"),
    "us": ("us",),
    "eu": ("de", "nl", "uk"),
}


@dataclasses.dataclass
class Credentials:
    cf_api_token: str = ""
    tg_token: str = ""
    tg_group_chat_id: str = ""
    tg_private_chat_id: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "CF_API_TOKEN": self.cf_api_token,
            "TG_TOKEN": self.tg_token,
            "TG_GROUP_CHAT_ID": self.tg_group_chat_id,
            "TG_PRIVATE_CHAT_ID": self.tg_private_chat_id,
        }

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "Credentials":
        return cls(
            cf_api_token=values.get("CF_API_TOKEN", values.get("CF_API_TOKEN", "")),
            tg_token=values.get("TG_TOKEN", values.get("TG_BOT_TOKEN", "")),
            tg_group_chat_id=values.get("TG_GROUP_CHAT_ID", ""),
            tg_private_chat_id=values.get("TG_PRIVATE_CHAT_ID", ""),
        )


class ConfigStore:
    """线路配置 JSON 仓库，保存采用原子替换。"""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[int, LineConfig]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManagerError(f"读取配置失败 {self.path}: {exc}") from exc
        values = raw.get("lines", raw) if isinstance(raw, dict) else raw
        if not isinstance(values, list):
            raise ManagerError("配置文件格式错误：lines 必须是数组")
        result: dict[int, LineConfig] = {}
        for value in values:
            config = LineConfig.from_dict(value)
            config.validate()
            if config.line_id in result:
                raise ManagerError(f"配置中存在重复线路: {config.line_id}")
            result[config.line_id] = config
        return result

    def save(self, lines: Mapping[int, LineConfig]) -> None:
        payload = {
            "version": 1,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "lines": [lines[key].to_dict() for key in sorted(lines)],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def atomic_write(path: Path, content: str, mode: int | None = None) -> None:
    """在同一目录中临时写入并替换，尽量避免中途断电留下半个脚本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None and os.name != "nt":
            os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def executable_write(path: Path, content: str) -> None:
    # Generated scripts embed API credentials, so only root should be able to read them.
    atomic_write(path, content, 0o700)


def snapshot_files(paths: Iterable[Path]) -> dict[Path, tuple[bool, bytes, int | None]]:
    snapshots = {}
    for path in set(paths):
        if path.exists():
            snapshots[path] = (True, path.read_bytes(), path.stat().st_mode & 0o777)
        else:
            snapshots[path] = (False, b"", None)
    return snapshots


def restore_files(snapshots: Mapping[Path, tuple[bool, bytes, int | None]]) -> None:
    for path, (existed, content, mode) in snapshots.items():
        if not existed:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.rollback.", dir=str(path.parent))
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if mode is not None and os.name != "nt":
                os.chmod(temp_path, mode)
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)


def mask_secret(value: str) -> str:
    if not value:
        return "(未配置)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


_CREDENTIAL_NAMES = (
    "CF_API_TOKEN",
    "TG_TOKEN",
    "TG_BOT_TOKEN",
    "TG_GROUP_CHAT_ID",
    "TG_PRIVATE_CHAT_ID",
)


def extract_credentials_from_text(text: str) -> dict[str, str]:
    """提取常见的常量赋值和 os.environ.get 默认值，不执行目标脚本。"""
    found: dict[str, str] = {}
    for name in _CREDENTIAL_NAMES:
        patterns = (
            rf"\b{name}\b\s*=\s*['\"]([^'\"]*)['\"]",
            rf"\b{name}\b\s*=\s*os\.environ\.get\(\s*['\"]{name}['\"]\s*,\s*['\"]([^'\"]*)['\"]",
            rf"\b{name}\b\s*=\s*os\.getenv\(\s*['\"]{name}['\"]\s*,\s*['\"]([^'\"]*)['\"]",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match and match.group(1):
                found[name] = match.group(1)
                break
    return found


def discover_credentials(paths: Paths) -> Credentials:
    values: dict[str, str] = {}
    for name in _CREDENTIAL_NAMES:
        env_value = os.environ.get(name)
        if env_value:
            values[name] = env_value
    candidates = sorted(paths.bin_dir.glob("cf_failover*.py"))
    if paths.tg_bot.exists():
        candidates.append(paths.tg_bot)
    for path in candidates:
        try:
            values.update({key: val for key, val in extract_credentials_from_text(path.read_text(encoding="utf-8", errors="ignore")).items() if val})
        except OSError:
            continue
    return Credentials.from_mapping(values)


class CloudflareError(ManagerError):
    pass


class CloudflareClient:
    """Cloudflare v4 API 的极薄封装，方便测试时注入 opener。"""

    BASE_URL = "https://api.cloudflare.com/client/v4"

    def __init__(
        self,
        token: str,
        opener: Callable[..., Any] | None = None,
        timeout: float = 15.0,
    ):
        self.token = token.strip()
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout
        if not self.token:
            raise CloudflareError("Cloudflare API Token 不能为空")

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if query:
            path = f"{path}?{urllib.parse.urlencode(query)}"
        data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "cf-manager/" + VERSION,
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.BASE_URL + path, data=data, headers=headers, method=method
        )
        try:
            response = self.opener(request, timeout=self.timeout)
            raw = response.read()
            result = json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            raise CloudflareError(f"Cloudflare API 请求失败: {exc}") from exc
        if not result.get("success"):
            messages = result.get("errors") or result.get("messages") or []
            detail = "; ".join(str(item.get("message", item)) for item in messages)
            raise CloudflareError(detail or "Cloudflare API 返回失败")
        return result

    def verify_token(self) -> bool:
        result = self._request("GET", "/user/tokens/verify")
        return result.get("result", {}).get("status") == "active"

    def list_zones(self) -> list[dict[str, Any]]:
        zones: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self._request(
                "GET", "/zones", query={"status": "active", "per_page": 50, "page": page}
            )
            zones.extend(response.get("result", []))
            page_count = int(response.get("result_info", {}).get("total_pages", page))
            if page >= page_count:
                return zones
            page += 1

    def list_a_records(self, zone_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                f"/zones/{urllib.parse.quote(zone_id, safe='')}/dns_records",
                query={"type": "A", "per_page": 100, "page": page},
            )
            records.extend(response.get("result", []))
            page_count = int(response.get("result_info", {}).get("total_pages", page))
            if page >= page_count:
                return records
            page += 1

    def create_a_record(self, zone_id: str, name: str, content: str) -> dict[str, Any]:
        result = self._request(
            "POST",
            f"/zones/{urllib.parse.quote(zone_id, safe='')}/dns_records",
            payload={"type": "A", "name": name, "content": content, "ttl": 60, "proxied": False},
        )
        return dict(result.get("result", {}))


def _json_literal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def render_failover_script(config: LineConfig, credentials: Credentials, paths: Paths | None = None) -> str:
    """生成含三级复核、区域仲裁和双推通知规范的独立监控脚本。"""
    config.validate()
    paths = paths or Paths()
    domains_json = _json_literal([item.to_dict() for item in config.domains])
    template = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""由 cf_manager.py 生成的线路监控脚本。修改配置请回到管理中心。"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LINE_ID = __LINE_ID__
LINE_ALIAS = __LINE_ALIAS__
REGION = __REGION__
MAIN_IP = __MAIN_IP__
BACKUP_IP = __BACKUP_IP__
PORT = __PORT__
FAILOVER_THRESHOLD_MS = __FAILOVER_THRESHOLD_MS__
RECOVERY_THRESHOLD_MS = __RECOVERY_THRESHOLD_MS__
EXTERNAL_THRESHOLD_MS = __EXTERNAL_THRESHOLD_MS__
PROBE_COUNT = __PROBE_COUNT__
PROBE_TIMEOUT_S = __PROBE_TIMEOUT_S__
CONFIRMATION_DELAY_S = __CONFIRMATION_DELAY_S__
INTERVAL_S = __INTERVAL_S__
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", __CF_API_TOKEN__)
TG_TOKEN = os.environ.get("TG_TOKEN", __TG_TOKEN__)
TG_GROUP_CHAT_ID = os.environ.get("TG_GROUP_CHAT_ID", __TG_GROUP_CHAT_ID__)
TG_PRIVATE_CHAT_ID = os.environ.get("TG_PRIVATE_CHAT_ID", __TG_PRIVATE_CHAT_ID__)
DOMAINS_CONFIG = __DOMAINS_CONFIG__
STATE_FILE = Path(os.environ.get("CF_STATE_FILE", __STATE_FILE__))
LOCK_FILE = Path(os.environ.get("CF_LOCK_FILE", __LOCK_FILE__))
CHECK_HOST_NODES = {"jp": ["jp", "hk", "sg"], "us": ["us"], "eu": ["de", "nl", "uk"]}


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def _probe_once(ip):
    started = time.monotonic()
    try:
        with socket.create_connection((ip, PORT), timeout=PROBE_TIMEOUT_S):
            return (time.monotonic() - started) * 1000.0
    except OSError:
        return None


def probe_average(ip):
    samples = [_probe_once(ip) for _ in range(PROBE_COUNT)]
    valid = [sample for sample in samples if sample is not None]
    return (sum(valid) / len(valid)) if valid else None


def check_ip_health_failover(ip):
    """连续三次复核；只有三次均超限才返回故障。"""
    samples = []
    for round_no in range(3):
        average = probe_average(ip)
        samples.append(average)
        if average is not None and average <= FAILOVER_THRESHOLD_MS:
            return False, samples
        log("线路 %s 第 %s 次复核异常: %s" % (LINE_ID, round_no + 1, average if average is not None else "timeout"))
        if round_no < 2:
            time.sleep(CONFIRMATION_DELAY_S)
    return True, samples


def _extract_times(value):
    if isinstance(value, dict):
        if isinstance(value.get("time"), (int, float)):
            yield float(value["time"]) * 1000.0
        for child in value.values():
            yield from _extract_times(child)
    elif isinstance(value, list):
        for child in value:
            yield from _extract_times(child)


def check_external_latency_by_region(ip):
    """调用 Check-Host 区域节点，任意正常节点都拦截误切。"""
    nodes = CHECK_HOST_NODES.get(REGION, CHECK_HOST_NODES["jp"])
    query = urllib.parse.urlencode({"host": "%s:%s" % (ip, PORT), "max_nodes": ",".join(nodes)})
    request = urllib.request.Request("https://check-host.net/check-tcp?" + query, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            first = json.loads(response.read().decode("utf-8"))
        request_id = first.get("request_id")
        if not request_id:
            return False
        result_url = "https://check-host.net/check-result/" + urllib.parse.quote(str(request_id), safe="")
        for _ in range(8):
            time.sleep(2)
            with urllib.request.urlopen(result_url, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
            times = list(_extract_times(result))
            if any(value <= EXTERNAL_THRESHOLD_MS for value in times):
                return True
        return False
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        log("Check-Host 仲裁请求失败，将按保守策略拦截切线")
        return True


def _api_request(method, path, payload):
    request = urllib.request.Request(
        "https://api.cloudflare.com/client/v4" + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + CF_API_TOKEN, "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("success"):
        raise RuntimeError(result)
    return result


def update_dns(target_ip):
    changed = 0
    for record in DOMAINS_CONFIG:
        zone_id = record.get("zone_id")
        record_id = record.get("record_id") or record.get("rec_id")
        payload = {"type": "A", "name": record["name"], "content": target_ip, "ttl": 60, "proxied": False}
        _api_request("PUT", "/zones/%s/dns_records/%s" % (zone_id, record_id), payload)
        changed += 1
    return changed


def send_telegram(chat_id, text):
    if not TG_TOKEN or not chat_id:
        return
    url = "https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15).read()
    except OSError as exc:
        log("Telegram 通知失败: %s" % exc)


def notify_switch(target_ip, reason, samples):
    # 群组消息刻意不放任何毫秒数字；详细数据只发私聊。
    group_text = "【%s】主节点连接超时/线路异常，已切换备用节点。" % LINE_ALIAS
    private_text = "【%s】线路 %s 已切换到 %s。主节点=%s，复核延迟=%s，原因=%s" % (LINE_ALIAS, LINE_ID, target_ip, MAIN_IP, samples, reason)
    send_telegram(TG_GROUP_CHAT_ID, group_text)
    send_telegram(TG_PRIVATE_CHAT_ID, private_text)


def write_state(value):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(value + "\n", encoding="utf-8")


def read_state():
    try:
        value = STATE_FILE.read_text(encoding="utf-8").strip().upper()
        return value if value in ("MAIN", "BACKUP") else "MAIN"
    except OSError:
        return "MAIN"


@contextlib.contextmanager
def process_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def switch_to(state, reason, samples):
    target = MAIN_IP if state == "MAIN" else BACKUP_IP
    update_dns(target)
    write_state(state)
    notify_switch(target, reason, samples)
    log("线路 %s 已切换到 %s (%s)" % (LINE_ID, state, target))


def run_once():
    with process_lock():
        state = read_state()
        if state == "MAIN":
            failed, samples = check_ip_health_failover(MAIN_IP)
            if not failed:
                return
            if check_external_latency_by_region(MAIN_IP):
                log("外部仲裁显示主节点可达，判定监控机出口异常，熔断拦截切线")
                return
            switch_to("BACKUP", "连续三次复核异常", samples)
            return
        # 备用状态只在主节点恢复到恢复阈值以下时切回。
        average = probe_average(MAIN_IP)
        if average is not None and average <= RECOVERY_THRESHOLD_MS:
            switch_to("MAIN", "主节点恢复", [average])


def print_status():
    print("线路 %s | %s | 当前状态: %s" % (LINE_ID, LINE_ALIAS, read_state()))


def main():
    if "--status" in sys.argv:
        print_status()
        return 0
    if "--once" in sys.argv:
        run_once()
        return 0
    log("启动线路 %s (%s)，轮询周期 %ss" % (LINE_ID, LINE_ALIAS, INTERVAL_S))
    while True:
        try:
            run_once()
        except Exception as exc:
            log("本轮检查异常: %s" % exc)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
'''
    replacements = {
        "__LINE_ID__": str(config.line_id),
        "__LINE_ALIAS__": repr(config.alias),
        "__REGION__": repr(config.region),
        "__MAIN_IP__": repr(config.main_ip),
        "__BACKUP_IP__": repr(config.backup_ip),
        "__PORT__": str(config.port),
        "__FAILOVER_THRESHOLD_MS__": str(config.failover_threshold_ms),
        "__RECOVERY_THRESHOLD_MS__": str(config.recovery_threshold_ms),
        "__EXTERNAL_THRESHOLD_MS__": str(config.external_threshold_ms),
        "__PROBE_COUNT__": str(config.probe_count),
        "__PROBE_TIMEOUT_S__": repr(config.probe_timeout_s),
        "__CONFIRMATION_DELAY_S__": str(config.confirmation_delay_s),
        "__INTERVAL_S__": str(config.interval_s),
        "__CF_API_TOKEN__": repr(credentials.cf_api_token),
        "__TG_TOKEN__": repr(credentials.tg_token),
        "__TG_GROUP_CHAT_ID__": repr(credentials.tg_group_chat_id),
        "__TG_PRIVATE_CHAT_ID__": repr(credentials.tg_private_chat_id),
        "__DOMAINS_CONFIG__": domains_json,
        "__STATE_FILE__": repr(str(paths.state_path(config.line_id))),
        "__LOCK_FILE__": repr(str(paths.lock_path(config.line_id))),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return textwrap.dedent(template)


def render_manual_script(config: LineConfig, credentials: Credentials) -> str:
    config.validate()
    records = _json_literal([item.to_dict() for item in config.domains])
    template = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线路 __LINE_ID__ 手动切换工具：cf__LINE_ID__.py [main|backup|IPv4]。"""
from __future__ import annotations
import json
import os
import socket
import sys
import urllib.request
import urllib.parse

CF_API_TOKEN = os.environ.get("CF_API_TOKEN", __CF_API_TOKEN__)
MAIN_IP = __MAIN_IP__
BACKUP_IP = __BACKUP_IP__
RECORDS = __RECORDS__


def update(target_ip):
    for record in RECORDS:
        path = "/zones/%s/dns_records/%s" % (record["zone_id"], record.get("record_id") or record.get("rec_id"))
        payload = {"type": "A", "name": record["name"], "content": target_ip, "ttl": 60, "proxied": False}
        request = urllib.request.Request(
            "https://api.cloudflare.com/client/v4" + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + CF_API_TOKEN, "Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("success"):
            raise RuntimeError(result)
    print("已将 %s 条记录切换到 %s" % (len(RECORDS), target_ip))


def main():
    target = sys.argv[1].lower() if len(sys.argv) > 1 else "main"
    target_ip = {"main": MAIN_IP, "backup": BACKUP_IP}.get(target, target)
    socket.inet_aton(target_ip)
    update(target_ip)


if __name__ == "__main__":
    main()
'''
    replacements = {
        "__LINE_ID__": str(config.line_id),
        "__CF_API_TOKEN__": repr(credentials.cf_api_token),
        "__MAIN_IP__": repr(config.main_ip),
        "__BACKUP_IP__": repr(config.backup_ip),
        "__RECORDS__": records,
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return textwrap.dedent(template)


def render_systemd_unit(config: LineConfig, paths: Paths | None = None) -> str:
    paths = paths or Paths()
    failover = paths.failover_path(config.line_id)
    return textwrap.dedent(
        f'''\
        [Unit]
        Description=Cloudflare failover line {config.line_id} - {config.alias}
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=simple
        ExecStart=/usr/bin/env python3 {failover}
        Restart=always
        RestartSec=5
        User=root
        Environment=PYTHONUNBUFFERED=1

        [Install]
        WantedBy=multi-user.target
        '''
    )


def _find_matching_brace(source: str, opening: int) -> int:
    """找到 Python 字典对应的 }，跳过字符串/注释。"""
    pairs = {"{": "}", "[": "]", "(": ")"}
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    comment = False
    for index in range(opening, len(source)):
        char = source[index]
        if comment:
            if char == "\n":
                comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char == "#":
            comment = True
        elif char in ("'", '"'):
            quote = char
        elif char in pairs:
            stack.append(pairs[char])
        elif char in ("}", "]", ")"):
            if not stack or stack.pop() != char:
                raise ManagerError("tg_bot.py 的括号结构不完整")
            if not stack:
                return index
    raise ManagerError("tg_bot.py 找不到字典结束括号")


def _dict_assignment_span(source: str, name: str) -> tuple[int, int] | None:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ManagerError(f"tg_bot.py 原文件语法错误，已停止修改: {exc}") from exc
    lines = source.splitlines(keepends=True)
    offsets: list[int] = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Dict):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                start = offsets[node.value.lineno - 1] + node.value.col_offset
                opening = source.find("{", start)
                if opening < 0:
                    continue
                return opening, _find_matching_brace(source, opening)
    return None


def _dict_assignment_node(source: str, name: str) -> ast.Dict | None:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Dict):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                return node.value
    return None


def _remove_dict_entry(source: str, name: str, key: str) -> str:
    """Remove a literal one-entry-per-line mapping without rewriting user formatting."""
    mapping = _dict_assignment_node(source, name)
    if mapping is None:
        return source
    lines = source.splitlines(keepends=True)
    for key_node, value_node in zip(mapping.keys, mapping.values):
        if key_node is None:
            continue
        try:
            current_key = ast.literal_eval(key_node)
        except (ValueError, TypeError):
            continue
        if current_key != key:
            continue
        if key_node.lineno != value_node.end_lineno:
            raise ManagerError(f"{name} 中的 {key!r} 是多行值，拒绝猜测性删除")
        line_index = key_node.lineno - 1
        line = lines[line_index]
        prefix_bytes = key_node.col_offset
        end_bytes = value_node.end_col_offset
        try:
            prefix = line.encode("utf-8")[:prefix_bytes].decode("utf-8")
            suffix = line.encode("utf-8")[:end_bytes].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManagerError(f"无法安全定位 {name} 中的 {key!r}") from exc
        suffix = line[len(suffix):].strip()
        if prefix.strip() or suffix.strip(" ,#\t\r\n"):
            raise ManagerError(f"{name} 中的 {key!r} 与其他表达式共行，拒绝删除")
        del lines[line_index]
        return "".join(lines)
    return source


def _literal_dict_keys(source: str, span: tuple[int, int]) -> set[str]:
    snippet = source[span[0] : span[1] + 1]
    try:
        value = ast.literal_eval(snippet)
        if isinstance(value, dict):
            return {str(key) for key in value}
    except (ValueError, SyntaxError):
        pass
    return set()


def _insert_dict_entry(source: str, name: str, key: str, value: str, tag: str) -> str:
    span = _dict_assignment_span(source, name)
    if span is None:
        raise ManagerError(f"tg_bot.py 中找不到字典 {name}，请先确认机器人代码结构")
    if key in _literal_dict_keys(source, span):
        return source
    opening, closing = span
    body = source[opening + 1 : closing]
    line_start = source.rfind("\n", 0, opening) + 1
    multiline = "\n" in body
    if multiline:
        indent_match = re.search(r"\n([ \t]+)(?:['\"]|\})", body)
        indent = indent_match.group(1) if indent_match else "    "
        # 最后一项可能带行尾注释，不能只看 body.rstrip().endswith(",")。
        last_line = body.rstrip().splitlines()[-1] if body.rstrip() else ""
        last_code = last_line.split("#", 1)[0].rstrip()
        prefix = ""
        if last_code and not last_code.endswith(","):
            prefix = ","
        # 插入点在右花括号之前，注释末尾必须换行，否则原右花括号会被注释吃掉。
        insertion = prefix + "\n" + indent + repr(key) + ": " + repr(value) + ",  # " + tag + "\n"
    else:
        prefix = ", " if body.strip() else ""
        insertion = prefix + repr(key) + ": " + repr(value) + "  # " + tag
    return source[:closing] + insertion + source[closing:]


def _expand_switch_regex(source: str, max_line_id: int) -> str:
    updated = []
    for line in source.splitlines(keepends=True):
        if "switch|backup" in line:
            line = re.sub(
                r"(?<=\[)1-\d+(?=\\?\])",
                f"1-{max_line_id}",
                line,
            )
        updated.append(line)
    return "".join(updated)


def _write_validated_python(path: Path, source: str) -> None:
    try:
        compile(source, str(path), "exec")
    except SyntaxError as exc:
        raise ManagerError(f"生成后的 Python 语法检查失败: {exc}") from exc
    atomic_write(path, source)


def inject_tg_bot(path: Path, config: LineConfig, max_line_id: int | None = None) -> list[str]:
    """对机器人做 AST 定位 + 标记插入，成功后才写回；返回 BotFather 命令。"""
    if not path.exists():
        raise ManagerError(f"找不到 Telegram 机器人文件: {path}")
    original = path.read_text(encoding="utf-8")
    try:
        ast.parse(original)
    except SyntaxError as exc:
        raise ManagerError(f"原 tg_bot.py 语法错误，未做任何修改: {exc}") from exc
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    candidate = original
    candidate = _insert_dict_entry(
        candidate,
        "SYSTEMD_SERVICES",
        f"容灾集群 {config.line_id} 号",
        f"cf-failover-{config.line_id}",
        f"cf_manager:{config.line_id}",
    )
    candidate = _insert_dict_entry(
        candidate,
        "COMMAND_MAPPING",
        f"run_cf{config.line_id}",
        f"/usr/local/bin/cf{config.line_id}.py",
        f"cf_manager:{config.line_id}",
    )
    candidate = _insert_dict_entry(
        candidate,
        f"COMMAND_MAPPING",
        f"cf_status{config.line_id}",
        f"/usr/local/bin/cf_failover_{config.line_id}.py --status",
        f"cf_manager:{config.line_id}",
    )
    before_regex_update = candidate
    candidate = _expand_switch_regex(candidate, max_line_id or config.line_id)
    if candidate == before_regex_update and "switch|backup" in candidate:
        warning("未识别 tg_bot.py 的 switch/backup 正则格式；已保留原代码，请手动确认新线路命令解析范围")
    try:
        ast.parse(candidate)
    except SyntaxError as exc:
        shutil.copy2(backup, path)
        raise ManagerError(f"TG 注入后语法检查失败，已从 .bak 恢复: {exc}") from exc
    atomic_write(path, candidate)
    return [
        f"switch_{config.line_id} - [运维] {config.line_id}号线·一键切主",
        f"backup_{config.line_id} - [运维] {config.line_id}号线·一键切备",
        f"run_cf{config.line_id} - cf{config.line_id}.py {config.alias}",
        f"cf_status{config.line_id} - {config.line_id}号线状态日志",
    ]


def remove_tg_bot_line(path: Path, line_id: int, remaining_max: int = 1) -> None:
    """按字典 key 删除本工具管理的映射，也兼容没有注释标记的历史行。"""
    if not path.exists():
        return
    original = path.read_text(encoding="utf-8")
    ast.parse(original)
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    candidate = original
    for mapping_name, key in (
        ("SYSTEMD_SERVICES", f"容灾集群 {line_id} 号"),
        ("COMMAND_MAPPING", f"run_cf{line_id}"),
        ("COMMAND_MAPPING", f"cf_status{line_id}"),
    ):
        candidate = _remove_dict_entry(candidate, mapping_name, key)
    candidate = _expand_switch_regex(candidate, max(1, remaining_max))
    try:
        ast.parse(candidate)
    except SyntaxError as exc:
        shutil.copy2(backup, path)
        raise ManagerError(f"移除 TG 指令后语法检查失败，已恢复备份: {exc}") from exc
    atomic_write(path, candidate)


class CommandRunner:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run or os.environ.get("CF_MANAGER_DRY_RUN") == "1"

    def run(self, args: Sequence[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        print(colour("$ " + " ".join(args), Colors.BLUE))
        if self.dry_run:
            return subprocess.CompletedProcess(args, 0, "", "")
        try:
            result = subprocess.run(args, text=True, capture_output=True, check=False)
        except FileNotFoundError:
            result = subprocess.CompletedProcess(args, 127, "", "命令不存在")
        if check and result.returncode:
            detail = (result.stderr or result.stdout or "").strip()
            raise ManagerError(f"命令执行失败 (exit {result.returncode}): {' '.join(args)}\n{detail}")
        return result


def _is_root() -> bool:
    return os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0)


def confirm(prompt: str, word: str = "YES") -> bool:
    answer = input(f"{prompt} 输入 {word} 确认: ").strip()
    return answer == word


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    value = input(prompt + suffix + ": ").strip()
    return value if value else (default or "")


def ask_int(prompt: str, default: int, minimum: int = 1, maximum: int = 65535) -> int:
    while True:
        value = ask(prompt, str(default))
        try:
            parsed = int(value)
            if minimum <= parsed <= maximum:
                return parsed
        except ValueError:
            pass
        warning(f"请输入 {minimum}~{maximum} 之间的整数")


def choose_region() -> str:
    choices = [("jp", "日本/亚太，仲裁 jp,hk,sg"), ("us", "美国/北美，仲裁 us"), ("eu", "欧洲，仲裁 de,nl,uk")]
    for index, (_, label) in enumerate(choices, 1):
        print(f"  [{index}] {label}")
    while True:
        selected = ask("目标区域", "1")
        try:
            return choices[int(selected) - 1][0]
        except (ValueError, IndexError):
            warning("请选择 1、2 或 3")


def prompt_ipv4(prompt: str, default: str | None = None) -> str:
    while True:
        value = ask(prompt, default)
        try:
            return str(ipaddress.IPv4Address(value))
        except ipaddress.AddressValueError:
            warning("请输入合法 IPv4，例如 38.64.57.64")


def choose_credentials(paths: Paths) -> Credentials:
    discovered = discover_credentials(paths)
    if discovered.cf_api_token:
        print(f"检测到本机 Cloudflare Token: {mask_secret(discovered.cf_api_token)}")
        reuse = ask("是否沿用本机 Token？Y/n", "Y").lower()
        if reuse not in ("n", "no"):
            try:
                if CloudflareClient(discovered.cf_api_token).verify_token():
                    success("Token 校验通过")
                    return discovered
            except CloudflareError as exc:
                warning(str(exc))
    while True:
        token = getpass.getpass("请输入 Cloudflare API Token（输入不回显）: ").strip()
        try:
            client = CloudflareClient(token)
            if client.verify_token():
                success("Token 校验通过")
                discovered.cf_api_token = token
                break
        except CloudflareError as exc:
            error(str(exc))
    if not discovered.tg_token:
        discovered.tg_token = ask("Telegram Bot Token（可留空）", "")
    if not discovered.tg_group_chat_id:
        discovered.tg_group_chat_id = ask("TG 群组 Chat ID（可留空）", "")
    if not discovered.tg_private_chat_id:
        discovered.tg_private_chat_id = ask("TG 私聊 Chat ID（可留空）", "")
    return discovered


def choose_domains(client: CloudflareClient) -> list[DomainRecord]:
    zones = client.list_zones()
    if not zones:
        raise CloudflareError("当前 Token 没有可用的 active Zone")
    print("\n可用 Cloudflare Zone:")
    for index, zone in enumerate(zones, 1):
        print(f"  [{index}] {zone.get('name')}  ({zone.get('id')})")
    zone_index = ask_int("请选择 Zone", 1, 1, len(zones))
    zone = zones[zone_index - 1]
    zone_id = str(zone["id"])
    records = client.list_a_records(zone_id)
    print("\n该 Zone 的 A 记录（可输入逗号多选，输入 new 创建）:")
    for index, record in enumerate(records, 1):
        print(f"  [{index}] {record.get('name')} -> {record.get('content')}  ({record.get('id')})")
    print(f"  [{len(records) + 1}] 新建子域名 A 记录")
    while True:
        selected = ask("记录编号", "1")
        if selected.lower() == "new" or str(len(records) + 1) in {part.strip() for part in selected.split(",")}:
            name = ask("新记录完整域名")
            content = prompt_ipv4("初始解析 IP")
            created = client.create_a_record(zone_id, name, content)
            records.append(created)
            success(f"已创建记录 {created.get('name')} ({created.get('id')})")
            # 新记录的序号就是追加后的最后一项，默认只选刚创建的记录。
            selected = str(len(records))
        try:
            indexes = [int(part.strip()) for part in selected.split(",") if part.strip()]
            chosen = [records[index - 1] for index in indexes]
            if chosen and all(1 <= index <= len(records) for index in indexes):
                return [
                    DomainRecord(
                        zone_id=zone_id,
                        record_id=str(record["id"]),
                        name=str(record["name"]),
                        proxied=False,
                    )
                    for record in chosen
                ]
        except (ValueError, IndexError, KeyError):
            pass
        warning("请输入合法编号，例如 1,2")


def prompt_line(paths: Paths, existing: LineConfig | None = None) -> tuple[LineConfig, Credentials]:
    existing_ids = discover_line_ids(paths)
    suggested = existing.line_id if existing else (max(existing_ids, default=0) + 1)
    line_id = ask_int("线路编号", suggested, 1, 9999)
    alias = ask("线路别名", existing.alias if existing else f"{line_id}号线")
    region = choose_region() if existing is None else ask("区域 jp/us/eu", existing.region)
    main_ip = prompt_ipv4("主节点 IP", existing.main_ip if existing else None)
    backup_ip = prompt_ipv4("备用节点 IP", existing.backup_ip if existing else None)
    port = ask_int("探活端口", existing.port if existing else 22, 1, 65535)
    failover = ask_int("切备阈值(ms)", existing.failover_threshold_ms if existing else 280, 1, 60000)
    recovery = ask_int("恢复阈值(ms)", existing.recovery_threshold_ms if existing else 160, 1, 60000)
    external = ask_int("外部仲裁阈值(ms)", existing.external_threshold_ms if existing else 150, 1, 60000)
    credentials = choose_credentials(paths)
    client = CloudflareClient(credentials.cf_api_token)
    domains = choose_domains(client)
    config = LineConfig(
        line_id=line_id,
        alias=alias,
        region=region,
        main_ip=main_ip,
        backup_ip=backup_ip,
        port=port,
        failover_threshold_ms=failover,
        recovery_threshold_ms=recovery,
        external_threshold_ms=external,
        domains=domains,
    )
    config.validate()
    return config, credentials


def discover_line_ids(paths: Paths) -> set[int]:
    result: set[int] = set()
    try:
        result.update(ConfigStore(paths.config_file).load())
    except ManagerError:
        pass
    for path in paths.bin_dir.glob("cf_failover*.py"):
        if path.name == "cf_failover.py":
            result.add(1)
        else:
            match = re.fullmatch(r"cf_failover_(\d+)\.py", path.name)
            if match:
                result.add(int(match.group(1)))
    return result


def install_line(config: LineConfig, credentials: Credentials, paths: Paths, runner: CommandRunner, store: ConfigStore, inject_bot: bool = True) -> list[str]:
    config.validate()
    lines = store.load()
    if config.line_id in lines:
        raise ManagerError(f"线路 {config.line_id} 已存在，请使用编辑功能")
    failover_path = paths.failover_path(config.line_id)
    manual_path = paths.manual_path(config.line_id)
    service_path = paths.service_path(config.line_id)
    targets = (failover_path, manual_path, service_path)
    if any(path.exists() for path in targets):
        raise ManagerError(f"线路 {config.line_id} 的目标文件已存在，但配置库中没有登记；拒绝覆盖")
    snapshots = snapshot_files(
        [*targets, paths.config_file, paths.tg_bot, paths.tg_bot.with_suffix(paths.tg_bot.suffix + ".bak")]
    )
    try:
        executable_write(failover_path, render_failover_script(config, credentials, paths))
        executable_write(manual_path, render_manual_script(config, credentials))
        atomic_write(service_path, render_systemd_unit(config, paths))
        lines[config.line_id] = config
        store.save(lines)
        if inject_bot and paths.tg_bot.exists():
            commands = inject_tg_bot(paths.tg_bot, config, max(lines))
        else:
            commands = [
                f"switch_{config.line_id} - [运维] {config.line_id}号线·一键切主",
                f"backup_{config.line_id} - [运维] {config.line_id}号线·一键切备",
                f"run_cf{config.line_id} - cf{config.line_id}.py {config.alias}",
                f"cf_status{config.line_id} - {config.line_id}号线状态日志",
            ]
            if inject_bot:
                warning(f"未找到 {paths.tg_bot}，已跳过 TG 热注入")
        if sys.platform.startswith("linux") and _is_root():
            runner.run(["systemctl", "daemon-reload"], check=True)
            runner.run(["systemctl", "enable", "--now", f"cf-failover-{config.line_id}"], check=True)
        else:
            warning("当前不是可直接管理 systemd 的 Linux root 环境，已跳过启动服务")
    except Exception:
        if sys.platform.startswith("linux") and _is_root():
            runner.run(["systemctl", "disable", "--now", f"cf-failover-{config.line_id}"], check=False)
            runner.run(["systemctl", "daemon-reload"], check=False)
        restore_files(snapshots)
        raise
    return commands


def read_state(paths: Paths, line_id: int) -> str:
    path = paths.state_path(line_id)
    try:
        value = path.read_text(encoding="utf-8").strip().upper()
        return value if value in ("MAIN", "BACKUP") else "未知"
    except OSError:
        return "未知"


def status_overview(paths: Paths, lines: Mapping[int, LineConfig], runner: CommandRunner) -> None:
    if not lines:
        warning("尚未登记线路。请先使用新增向导。")
        return
    print("\n线路状态总览")
    print("编号  名称                         服务状态       DNS状态")
    print("-" * 72)
    for line_id, config in sorted(lines.items()):
        if sys.platform.startswith("linux"):
            result = runner.run(["systemctl", "is-active", f"cf-failover-{line_id}"], check=False)
            service = (result.stdout or "unknown").strip()
        else:
            service = "非 Linux"
        marker = "运行中" if service == "active" else "异常/未知"
        print(f"{line_id:>2}    {config.alias[:28]:<28} {marker:<12} {read_state(paths, line_id)}")
    selected = input("输入线路编号查看最近 25 条日志，直接回车返回: ").strip()
    if selected.isdigit() and int(selected) in lines:
        line_id = int(selected)
        if sys.platform.startswith("linux"):
            result = runner.run(["journalctl", "-u", f"cf-failover-{line_id}", "-n", "25", "--no-pager"])
            print(result.stdout or result.stderr)
        else:
            warning("journalctl 仅在 Linux 上可用")


def cloudflare_lookup(paths: Paths) -> None:
    credentials = choose_credentials(paths)
    client = CloudflareClient(credentials.cf_api_token)
    try:
        domains = choose_domains(client)
    except CloudflareError as exc:
        error(str(exc))
        return
    print("\n可直接粘贴到线路配置的域名记录:")
    print(json.dumps([item.to_dict() for item in domains], ensure_ascii=False, indent=2))


def edit_line(paths: Paths, store: ConfigStore, runner: CommandRunner) -> None:
    lines = store.load()
    if not lines:
        warning("没有可编辑的线路")
        return
    for line_id, config in sorted(lines.items()):
        print(f"  [{line_id}] {config.alias}")
    selected = ask_int("线路编号", min(lines), 1, 9999)
    if selected not in lines:
        error("线路不存在")
        return
    old = lines[selected]
    config, credentials = prompt_line(paths, old)
    if config.line_id != selected and config.line_id in lines:
        raise ManagerError("新的线路编号已被占用")
    old_generated = (paths.failover_path(selected), paths.manual_path(selected), paths.service_path(selected))
    new_generated = (paths.failover_path(config.line_id), paths.manual_path(config.line_id), paths.service_path(config.line_id))
    if config.line_id != selected and any(path.exists() for path in new_generated):
        raise ManagerError(f"新线路编号 {config.line_id} 的目标文件已存在，拒绝覆盖")
    tg_backup = paths.tg_bot.with_suffix(paths.tg_bot.suffix + ".bak")
    snapshots = snapshot_files(
        [*old_generated, *new_generated, paths.state_path(selected), paths.lock_path(selected),
         paths.config_file, paths.tg_bot, tg_backup]
    )
    try:
        if sys.platform.startswith("linux") and _is_root():
            runner.run(["systemctl", "stop", f"cf-failover-{selected}"], check=True)
            if config.line_id != selected:
                runner.run(["systemctl", "disable", f"cf-failover-{selected}"], check=True)
        if config.line_id != selected:
            lines.pop(selected)
        lines[config.line_id] = config
        executable_write(paths.failover_path(config.line_id), render_failover_script(config, credentials, paths))
        executable_write(paths.manual_path(config.line_id), render_manual_script(config, credentials))
        atomic_write(paths.service_path(config.line_id), render_systemd_unit(config, paths))
        store.save(lines)
        if paths.tg_bot.exists():
            remove_tg_bot_line(paths.tg_bot, selected, max(lines, default=1))
            inject_tg_bot(paths.tg_bot, config, max(lines))
        if config.line_id != selected:
            for path in old_generated:
                path.unlink(missing_ok=True)
            paths.state_path(selected).unlink(missing_ok=True)
            paths.lock_path(selected).unlink(missing_ok=True)
        if sys.platform.startswith("linux") and _is_root():
            runner.run(["systemctl", "daemon-reload"], check=True)
            runner.run(["systemctl", "enable", "--now", f"cf-failover-{config.line_id}"], check=True)
    except Exception:
        restore_files(snapshots)
        if sys.platform.startswith("linux") and _is_root():
            runner.run(["systemctl", "daemon-reload"], check=False)
            runner.run(["systemctl", "enable", "--now", f"cf-failover-{selected}"], check=False)
        raise
    success(f"线路 {config.line_id} 已更新")


def delete_line(paths: Paths, store: ConfigStore, runner: CommandRunner) -> None:
    lines = store.load()
    if not lines:
        warning("没有可删除的线路")
        return
    for line_id, config in sorted(lines.items()):
        print(f"  [{line_id}] {config.alias}")
    line_id = ask_int("要删除的线路编号", min(lines), 1, 9999)
    if line_id not in lines:
        error("线路不存在")
        return
    config = lines[line_id]
    if not confirm(f"将停止并删除 {config.alias} 的脚本、服务和缓存。", "DELETE"):
        info("已取消")
        return
    delete_paths = (
        paths.failover_path(line_id),
        paths.manual_path(line_id),
        paths.service_path(line_id),
        paths.state_path(line_id),
        paths.lock_path(line_id),
    )
    tg_backup = paths.tg_bot.with_suffix(paths.tg_bot.suffix + ".bak")
    snapshots = snapshot_files(
        [*delete_paths, paths.config_file, paths.tg_bot, tg_backup]
    )
    try:
        if sys.platform.startswith("linux") and _is_root():
            runner.run(["systemctl", "disable", "--now", f"cf-failover-{line_id}"], check=True)
        for path in delete_paths:
            path.unlink(missing_ok=True)
        lines.pop(line_id)
        store.save(lines)
        if paths.tg_bot.exists():
            remove_tg_bot_line(paths.tg_bot, line_id, max(lines, default=1))
        if sys.platform.startswith("linux") and _is_root():
            runner.run(["systemctl", "daemon-reload"], check=True)
    except Exception:
        restore_files(snapshots)
        if sys.platform.startswith("linux") and _is_root():
            runner.run(["systemctl", "daemon-reload"], check=False)
            runner.run(["systemctl", "enable", "--now", f"cf-failover-{line_id}"], check=False)
        raise
    success(f"线路 {line_id} 已下线并清理")


def restart_tg_bot(paths: Paths, runner: CommandRunner) -> None:
    if not paths.tg_bot.exists():
        warning(f"找不到 {paths.tg_bot}")
        return
    if not sys.platform.startswith("linux") or not _is_root():
        warning("当前环境不是 Linux root，跳过 TG 进程重启")
        return
    if runner.dry_run:
        info(f"演练模式：将精确查找并重启 {paths.tg_bot}")
        return
    script_path = paths.tg_bot.resolve()
    matching_pids: list[int] = []
    proc_root = Path("/proc")
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        pid = int(proc.name)
        if pid == os.getpid():
            continue
        try:
            argv = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        for raw_arg in argv:
            if not raw_arg:
                continue
            try:
                arg = Path(os.fsdecode(raw_arg))
                if arg.is_absolute() and arg.resolve() == script_path:
                    matching_pids.append(pid)
                    break
            except (OSError, RuntimeError):
                continue
    for pid in matching_pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, 15)
    deadline = time.monotonic() + 5
    while matching_pids and time.monotonic() < deadline:
        alive = []
        for pid in matching_pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except ProcessLookupError:
                pass
        matching_pids = alive
        if matching_pids:
            time.sleep(0.1)
    for pid in matching_pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, 9)
    paths.tg_bot.parent.mkdir(parents=True, exist_ok=True)
    with paths.tg_bot.with_suffix(".log").open("ab") as log_file:
        subprocess.Popen(
            [sys.executable, str(paths.tg_bot)],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    success("Telegram 机器人已重新启动")


def service_menu(paths: Paths, store: ConfigStore, runner: CommandRunner) -> None:
    lines = store.load()
    print("\n[1] 重启单条线路  [2] 重启全部线路  [3] 重启 TG 机器人  [4] 清理状态锁  [0] 返回")
    selected = input("请选择: ").strip()
    if selected == "1":
        line_id = ask_int("线路编号", min(lines) if lines else 1, 1, 9999)
        if sys.platform.startswith("linux") and _is_root():
            runner.run(["systemctl", "restart", f"cf-failover-{line_id}"], check=False)
    elif selected == "2":
        if sys.platform.startswith("linux") and _is_root():
            for line_id in lines:
                runner.run(["systemctl", "restart", f"cf-failover-{line_id}"], check=False)
    elif selected == "3":
        restart_tg_bot(paths, runner)
    elif selected == "4":
        for line_id in lines:
            paths.state_path(line_id).unlink(missing_ok=True)
            paths.lock_path(line_id).unlink(missing_ok=True)
        success("已清理登记线路的状态和锁文件")
    elif selected != "0":
        warning("无效选项")


def backup_cluster(paths: Paths, store: ConfigStore) -> Path:
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output = paths.backup_dir / f"cf_full_backup_{timestamp}.tar.gz"
    sequence = 1
    while output.exists():
        output = paths.backup_dir / f"cf_full_backup_{timestamp}_{sequence}.tar.gz"
        sequence += 1
    paths.backup_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for pattern in ("cf*.py", "cf_failover*.py"):
        files.extend(paths.bin_dir.glob(pattern))
    files.extend(paths.systemd_dir.glob("cf-failover-*.service"))
    files.extend([paths.tg_bot, paths.config_file])
    unique_files: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        resolved = path.resolve()
        if path.exists() and resolved not in seen:
            seen.add(resolved)
            unique_files.append(path)
    restore = textwrap.dedent(
        '''\
        #!/bin/sh
        # cf_manager 自动生成的恢复脚本；请先确认当前机器和备份来源。
        set -eu
        BASE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
        [ -d "$BASE/usr/local/bin" ] && install -d /usr/local/bin && cp -a "$BASE/usr/local/bin/." /usr/local/bin/
        [ -d "$BASE/etc/systemd/system" ] && install -d /etc/systemd/system && cp -a "$BASE/etc/systemd/system/." /etc/systemd/system/
        [ -f "$BASE/root/tg_bot.py" ] && cp -a "$BASE/root/tg_bot.py" /root/tg_bot.py
        [ -f "$BASE/etc/cf_manager/lines.json" ] && install -d /etc/cf_manager && cp -a "$BASE/etc/cf_manager/lines.json" /etc/cf_manager/lines.json
        systemctl daemon-reload
        echo "文件已恢复；请按需执行 systemctl enable --now cf-failover-N。"
        '''
    )
    with tarfile.open(output, "w:gz") as archive:
        for path in unique_files:
            resolved = path.resolve()
            if resolved.is_relative_to(paths.bin_dir.resolve()):
                arcname = f"usr/local/bin/{path.name}"
            elif resolved.is_relative_to(paths.systemd_dir.resolve()):
                arcname = f"etc/systemd/system/{path.name}"
            elif resolved == paths.tg_bot.resolve():
                arcname = "root/tg_bot.py"
            elif resolved == paths.config_file.resolve():
                arcname = "etc/cf_manager/lines.json"
            else:
                raise ManagerError(f"拒绝打包位于预期目录之外的文件: {path}")
            archive.add(path, arcname=arcname)
        restore_info = tarfile.TarInfo("restore_cf_cluster.sh")
        restore_bytes = restore.encode("utf-8")
        restore_info.size = len(restore_bytes)
        restore_info.mode = 0o755
        archive.addfile(restore_info, io.BytesIO(restore_bytes))
    if os.name != "nt":
        os.chmod(output, 0o600)
    return output


def print_menu() -> None:
    print("\n" + "=" * 62)
    print("     Cloudflare 容灾集群与 TG 机器人统一管理中心 (v2.0)")
    print("=" * 62)
    print(" [1] 线路状态总览（集群状态、当前主备、日志）")
    print(" [2] 新增容灾线路（全自动向导）")
    print(" [3] Cloudflare 域名与解析检索")
    print(" [4] 线路参数调整")
    print(" [5] 下线/删除线路")
    print(" [6] 服务与缓存管理")
    print(" [7] 集群全量备份与迁移包制作")
    print(" [0] 退出控制台")
    print("=" * 62)


def run_menu(paths: Paths, runner: CommandRunner) -> int:
    store = ConfigStore(paths.config_file)
    while True:
        print_menu()
        selected = input("请输入选项 [0-7]: ").strip()
        try:
            if selected == "0":
                return 0
            if selected == "1":
                status_overview(paths, store.load(), runner)
            elif selected == "2":
                config, credentials = prompt_line(paths)
                commands = install_line(config, credentials, paths, runner, store)
                success(f"线路 {config.line_id} 已生成并登记")
                print("\nBotFather 指令（复制到 /setcommands）:")
                print("\n".join(commands))
            elif selected == "3":
                cloudflare_lookup(paths)
            elif selected == "4":
                edit_line(paths, store, runner)
            elif selected == "5":
                delete_line(paths, store, runner)
            elif selected == "6":
                service_menu(paths, store, runner)
            elif selected == "7":
                output = backup_cluster(paths, store)
                success(f"备份已生成: {output}")
            else:
                warning("请输入 0~7")
        except (ManagerError, CloudflareError) as exc:
            error(str(exc))
        except KeyboardInterrupt:
            print()
            info("已取消本次操作")
        except EOFError:
            print()
            return 0


def self_test() -> int:
    """不触网、不调用 systemd 的快速回归测试，适合部署前运行。"""
    with tempfile.TemporaryDirectory(prefix="cf-manager-test-") as temp:
        root = Path(temp)
        paths = Paths()
        paths.bin_dir = root / "usr" / "local" / "bin"
        paths.systemd_dir = root / "etc" / "systemd" / "system"
        paths.tg_bot = root / "root" / "tg_bot.py"
        paths.config_file = root / "etc" / "cf_manager" / "lines.json"
        paths.backup_dir = root / "root"
        paths.runtime_dir = root / "tmp"
        config = LineConfig(
            line_id=6,
            alias="6号线（测试）",
            region="eu",
            main_ip="38.64.57.64",
            backup_ip="38.207.162.102",
            domains=[DomainRecord("zone", "record", "edge.example.com")],
        )
        credentials = Credentials("token", "tg", "-1001", "1002")
        failover = render_failover_script(config, credentials, paths)
        manual = render_manual_script(config, credentials)
        compile(failover, "generated_failover.py", "exec")
        compile(manual, "generated_manual.py", "exec")
        assert "check_ip_health_failover" in failover
        assert "check_external_latency_by_region" in failover
        assert "主节点连接超时/线路异常" in failover
        assert "毫秒" not in failover.split("group_text", 1)[-1].split("private_text", 1)[0]
        bot = textwrap.dedent(
            '''\
            import re
            SYSTEMD_SERVICES = {
                '容灾集群 1 号': 'cf-failover-1',
            }
            COMMAND_MAPPING = {
                'run_cf1': '/usr/local/bin/cf1.py',
            }
            COMMAND_RE = re.compile(r'^(switch|backup)_([1-5])$')
            '''
        )
        paths.tg_bot.parent.mkdir(parents=True, exist_ok=True)
        paths.tg_bot.write_text(bot, encoding="utf-8")
        commands = inject_tg_bot(paths.tg_bot, config, 6)
        parsed = ast.parse(paths.tg_bot.read_text(encoding="utf-8"))
        assert parsed and paths.tg_bot.with_suffix(".py.bak").exists()
        assert "([1-6])" in paths.tg_bot.read_text(encoding="utf-8")
        assert commands[0].startswith("switch_6")
        store = ConfigStore(paths.config_file)
        store.save({6: config})
        assert store.load()[6].domains[0].record_id == "record"
        backup = backup_cluster(paths, store)
        assert backup.exists()
    print("cf_manager self-test: PASS")
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cloudflare 多线路容灾管理中心")
    parser.add_argument("--version", action="version", version=f"cf_manager {VERSION}")
    parser.add_argument("--self-test", action="store_true", help="运行离线自测")
    parser.add_argument("--dry-run", action="store_true", help="不执行 systemctl/pkill 等外部命令")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        return self_test()
    if args.dry_run:
        os.environ["CF_MANAGER_DRY_RUN"] = "1"
    paths = Paths()
    if not _is_root() and not os.environ.get("CF_MANAGER_ROOT"):
        warning("当前不是 root；生成/编辑生产路径可能失败，请使用 sudo。")
    try:
        return run_menu(paths, CommandRunner())
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
