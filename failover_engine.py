#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用多线路容灾引擎：所有线路由 /opt/cf-failover/config.json 驱动。"""
from __future__ import annotations

import argparse
try:
    import fcntl
except ImportError:  # Windows 仅用于离线测试；生产运行仍要求 Linux 文件锁。
    fcntl = None
import json
import ipaddress
import os
import re
import socket
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Mapping

CONFIG_PATH = Path(os.environ.get("CF_FAILOVER_CONFIG", "/opt/cf-failover/config.json"))
CACHE_PATH = Path(os.environ.get("CF_FAILOVER_DNS_CACHE", "/opt/cf-failover/dns_cache.json"))
STATE_DIR = Path(os.environ.get("CF_FAILOVER_STATE_DIR", "/tmp"))
TZ_BJS = timezone(timedelta(hours=8))
REGION_NODES = {"jp": ("jp", "hk", "sg"), "us": ("us",), "eu": ("de", "nl", "uk")}


class EngineError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[{datetime.now(TZ_BJS):%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def load_config() -> dict[str, Any]:
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            cfg = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise EngineError(f"读取配置失败: {CONFIG_PATH}: {exc}") from exc
    if not isinstance(cfg, dict):
        raise EngineError("配置顶层必须是 JSON 对象")
    if not isinstance(cfg.get("global"), dict) or not isinstance(cfg.get("lines"), dict):
        raise EngineError("配置必须包含 global 和 lines")
    if any(not str(key).isdigit() or int(key) < 1 or str(int(key)) != str(key) for key in cfg["lines"]):
        raise EngineError("线路编号必须全部为正整数")
    return cfg


def get_line(cfg: Mapping[str, Any], line_id: str) -> dict[str, Any]:
    if not str(line_id).isdigit() or int(line_id) < 1 or str(int(line_id)) != str(line_id):
        raise EngineError("线路编号必须是正整数")
    line = cfg["lines"].get(str(line_id))
    if not isinstance(line, dict):
        raise EngineError(f"找不到 {line_id} 号线配置")
    required = ("name", "main_ip", "backup_ip", "domains")
    missing = [key for key in required if not line.get(key)]
    if missing:
        raise EngineError(f"{line_id} 号线缺少配置: {', '.join(missing)}")
    domains = line["domains"]
    def valid_domain(domain: Any) -> bool:
        if not isinstance(domain, str) or len(domain) > 253 or "." not in domain:
            return False
        normalized = domain[:-1] if domain.endswith(".") else domain
        if not normalized or normalized.endswith("."):
            return False
        return all(
            0 < len(label) <= 63
            and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
            for label in normalized.split(".")
        )
    if not isinstance(domains, list) or not domains or any(not valid_domain(domain) for domain in domains):
        raise EngineError(f"{line_id} 号线 domains 必须是非空域名列表")
    region = line.get("region", "jp")
    if not isinstance(region, str) or region not in REGION_NODES:
        raise EngineError(f"{line_id} 号线 region 必须是 jp、us 或 eu")
    for key in ("main_ip", "backup_ip"):
        try:
            ipaddress.IPv4Address(str(line[key]))
        except ipaddress.AddressValueError as exc:
            raise EngineError(f"{line_id} 号线的 {key} 不是合法 IPv4 地址") from exc
    try:
        port = int(line.get("port", cfg.get("global", {}).get("tcp_port", 22)))
    except (TypeError, ValueError) as exc:
        raise EngineError(f"{line_id} 号线探活端口无效") from exc
    if not 1 <= port <= 65535:
        raise EngineError(f"{line_id} 号线探活端口必须在 1~65535 之间")
    return line


def _request_json(request: urllib.request.Request, timeout: float = 10) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise EngineError(f"网络请求失败: {exc}") from exc
    if not isinstance(value, dict):
        raise EngineError("API 返回格式错误")
    return value


def _load_cache() -> dict[str, Any]:
    try:
        value = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: Mapping[str, Any], deleted: list[str] | None = None) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_handle:
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            existing = _load_cache()
            merged = dict(existing)
            for key in deleted or []:
                merged.pop(key, None)
            merged.update(cache)
            temp = CACHE_PATH.with_suffix(".tmp")
            temp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp, CACHE_PATH)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    if os.name != "nt":
        os.chmod(CACHE_PATH, 0o600)


def resolve_domain_ids(token: str, domains: list[str], force_refresh: bool = False) -> list[dict[str, str]]:
    """按完整域名自动查 Zone/Record ID，并缓存结果。"""
    if not token or token.startswith("YOUR_"):
        raise EngineError("未配置有效 Cloudflare API Token")
    cache = _load_cache()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    records: list[dict[str, str]] = []
    changed = False
    deleted: list[str] = []
    updates: dict[str, list[dict[str, str]]] = {}
    for domain in domains:
        cached = cache.get(domain)
        cached_records = cached if isinstance(cached, list) else [cached] if isinstance(cached, dict) else []
        if isinstance(cached, list) and cached_records and all(isinstance(item, dict) and item.get("zone_id") and item.get("rec_id") for item in cached_records) and not force_refresh:
            records.extend(cached_records)
            continue
        if domain in cache:
            cache.pop(domain, None)
            deleted.append(domain)
            changed = True
        parts = domain.rstrip(".").split(".")
        if len(parts) < 2:
            log(f"域名格式无效，跳过: {domain}")
            continue
        # 从最长后缀逐步尝试，兼容 co.uk、com.au 等多级公共后缀。
        zone_id = ""
        for start in range(0, len(parts) - 1):
            candidate = ".".join(parts[start:])
            zone_url = "https://api.cloudflare.com/client/v4/zones?" + urllib.parse.urlencode(
                {"name": candidate, "status": "active", "per_page": 50}
            )
            zone = _request_json(urllib.request.Request(zone_url, headers=headers))
            zones = zone.get("result") or []
            if zones:
                zone_id = str(zones[0]["id"])
                break
        if not zone_id:
            log(f"找不到 Zone，跳过: {domain}")
            continue
        record_url = "https://api.cloudflare.com/client/v4/zones/%s/dns_records?%s" % (
            urllib.parse.quote(zone_id, safe=""),
            urllib.parse.urlencode({"type": "A", "name": domain, "per_page": 100}),
        )
        result = _request_json(urllib.request.Request(record_url, headers=headers))
        found = result.get("result") or []
        if not found:
            log(f"找不到 A 记录，跳过: {domain}")
            continue
        items = [
            {"zone_id": zone_id, "rec_id": str(record["id"]), "name": domain}
            for record in found
            if record.get("id") and record.get("type", "A") == "A" and record.get("name", domain).lower() == domain.lower()
        ]
        if not items:
            log(f"找不到有效 A 记录，跳过: {domain}")
            continue
        updates[domain] = items
        records.extend(items)
        changed = True
    if changed:
        _save_cache(updates, deleted)
    return records


def _cloudflare_update(record: Mapping[str, str], target_ip: str, token: str) -> bool:
    url = "https://api.cloudflare.com/client/v4/zones/%s/dns_records/%s" % (
        urllib.parse.quote(record["zone_id"], safe=""), urllib.parse.quote(record["rec_id"], safe="")
    )
    payload = {"type": "A", "name": record["name"], "content": target_ip, "ttl": 60, "proxied": False}
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="PUT",
    )
    try:
        return bool(_request_json(request, 15).get("success"))
    except EngineError as exc:
        log(f"更新 DNS 失败 [{record.get('name')}]: {exc}")
        return False


def send_telegram(token: str, chat_id: Any, text: str) -> None:
    if not token or not chat_id or str(chat_id).startswith("YOUR_"):
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": str(chat_id), "text": text}).encode("utf-8")
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=payload), timeout=8).read()
    except OSError as exc:
        log(f"Telegram 通知失败: {exc}")


def notify(cfg: Mapping[str, Any], line: Mapping[str, Any], target_ip: str, mode: str, count: int, total: int, latencies: list[float] | None = None) -> None:
    global_cfg = cfg["global"]
    title = "一键切主" if mode == "MAIN" else "一键切备"
    # 群组消息不显示毫秒；详细数据只发管理员私聊。
    if count == total:
        if mode == "MAIN":
            group_text = f"【{line['name']}】主节点已恢复，解析已切回主节点。"
        else:
            group_text = f"【{line['name']}】主节点连接超时/线路异常，已切换备用节点。"
    else:
        group_text = f"【{line['name']}】线路异常，DNS 更新未完成，请检查配置。"
    private = f"【{line['name']}】{title}\n目标 IP: {target_ip}\n解析成功: {count}/{total}"
    if latencies:
        private += "\n复核延迟: " + ", ".join(f"{value:.1f}ms" for value in latencies)
    send_telegram(str(global_cfg.get("tg_bot_token", "")), global_cfg.get("tg_group_id"), group_text)
    send_telegram(str(global_cfg.get("tg_bot_token", "")), global_cfg.get("tg_admin_id"), private)


def update_dns(cfg: Mapping[str, Any], line_id: str, line: Mapping[str, Any], target_ip: str, mode: str, latencies: list[float] | None = None) -> int:
    token = str(cfg["global"].get("cf_api_token", ""))
    domains = list(line.get("domains", []))
    records = resolve_domain_ids(token, domains)
    with ThreadPoolExecutor(max_workers=min(10, max(1, len(records)))) as executor:
        results = list(executor.map(lambda record: _cloudflare_update(record, target_ip, token), records))
    def fully_updated(items: list[dict[str, str]], outcomes: list[bool]) -> set[str]:
        totals: dict[str, int] = {}
        successes: dict[str, int] = {}
        for record, ok in zip(items, outcomes):
            name = record["name"]
            totals[name] = totals.get(name, 0) + 1
            successes[name] = successes.get(name, 0) + int(bool(ok))
        return {name for name, total in totals.items() if successes.get(name) == total}

    successful_domains = fully_updated(records, results)
    failed_domains = [domain for domain in domains if domain not in successful_domains]
    retry_records: list[dict[str, str]] = []
    if failed_domains:
        try:
            refreshed = resolve_domain_ids(token, failed_domains, force_refresh=True)
            retry_records = [record for record in refreshed if record["name"] in failed_domains]
            with ThreadPoolExecutor(max_workers=min(10, max(1, len(retry_records)))) as executor:
                retry_results = list(executor.map(lambda record: _cloudflare_update(record, target_ip, token), retry_records))
            successful_domains.update(fully_updated(retry_records, retry_results))
        except EngineError as exc:
            log(f"DNS 记录刷新/重试失败: {exc}")
    count = sum(1 for domain in domains if domain in successful_domains)
    if count != len(domains):
        previous_mode = get_state(line_id)
        rollback_ip = line["main_ip"] if previous_mode == "MAIN" else line["backup_ip"]
        # DNS updates are transactional at the line level: undo successful
        # domains too, or a failed switch can leave part of the line split.
        rollback_by_domain: dict[str, list[dict[str, str]]] = {}
        for record in records:
            rollback_by_domain.setdefault(record["name"], []).append(record)
        for domain in failed_domains:
            refreshed_records = [record for record in retry_records if record["name"] == domain]
            if refreshed_records:
                rollback_by_domain[domain] = refreshed_records
        rollback_records = {
            (record["zone_id"], record["rec_id"]): record
            for items in rollback_by_domain.values()
            for record in items
        }
        with ThreadPoolExecutor(max_workers=min(10, max(1, len(rollback_records)))) as executor:
            rollback_results = list(
                executor.map(lambda record: _cloudflare_update(record, rollback_ip, token), rollback_records.values())
            )
        if not all(rollback_results):
            log(f"严重：DNS 部分更新后回滚也未全部成功，请立即核对线路状态和解析（目标 {rollback_ip}）")
        else:
            log(f"DNS 更新未完成，已尝试回滚到当前状态 {previous_mode}")
        count = 0
    notify(cfg, line, target_ip, mode, count, len(domains), latencies)
    return count


def tcping(ip: str, port: int, count: int = 5, timeout: float = 2.5) -> tuple[bool, float, list[float]]:
    samples: list[float] = []
    for _ in range(count):
        started = time.monotonic()
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                samples.append((time.monotonic() - started) * 1000)
        except OSError:
            pass
    if not samples:
        return False, 0.0, []
    filtered = samples[:]
    if len(filtered) >= 3:
        filtered.remove(max(filtered))
    return True, statistics.fmean(filtered), filtered


def is_monitor_network_alive(min_alive: int = 2) -> bool:
    checks = (("1.1.1.1", 53), ("210.140.100.1", 53), ("104.16.132.229", 443))
    alive = 0
    for host, port in checks:
        try:
            with socket.create_connection((host, port), timeout=2):
                alive += 1
        except OSError:
            pass
    return alive >= min(3, max(1, min_alive))


def check_external_latency(ip: str, port: int, region: str, threshold: float) -> bool:
    countries = REGION_NODES.get(region)
    if not countries:
        log(f"Check-Host 区域无效: {region}，按保守策略拦截切换")
        return True
    headers = {"Accept": "application/json"}
    try:
        hosts = _request_json(
            urllib.request.Request("https://check-host.net/nodes/hosts", headers=headers), 15
        ).get("nodes")
        if not isinstance(hosts, dict):
            raise EngineError("Check-Host 节点列表格式无效")
        nodes = _select_check_host_nodes(hosts, countries)

        query = urllib.parse.urlencode(
            [("host", f"{ip}:{port}"), *(("node", node) for node in nodes)]
        )
        first = _request_json(
            urllib.request.Request("https://check-host.net/check-tcp?" + query, headers=headers), 15
        )
        request_id = first.get("request_id")
        selected_nodes = first.get("nodes")
        if not request_id or not isinstance(selected_nodes, dict) or set(selected_nodes) != set(nodes):
            log("Check-Host 未接受完整区域节点请求，按保守策略拦截切换")
            return True
        for _ in range(8):
            time.sleep(2)
            result = _request_json(
                urllib.request.Request(
                    "https://check-host.net/check-result/"
                    + urllib.parse.quote(str(request_id), safe=""),
                    headers=headers,
                ),
                15,
            )
            values = _extract_tcp_latencies(result)
            if values and any(value <= threshold for value in values):
                return True
            if all(_has_completed_tcp_probe(result.get(node)) for node in nodes):
                return False
        # 节点未完成或没有给出可判定结果时按保守策略阻止切换。
        return True
    except EngineError as exc:
        log(f"Check-Host 仲裁失败，按保守策略拦截切换: {exc}")
        return True


def _select_check_host_nodes(hosts: Mapping[str, Any], countries: tuple[str, ...]) -> list[str]:
    grouped: dict[str, list[str]] = {country: [] for country in countries}
    for hostname, details in hosts.items():
        if not isinstance(hostname, str) or not isinstance(details, dict):
            continue
        location = details.get("location")
        if not isinstance(location, list) or not location:
            continue
        country = str(location[0]).lower()
        if country == "gb":
            country = "uk"
        if country in grouped:
            grouped[country].append(hostname)

    nodes: list[str] = []
    for country in countries:
        candidates = sorted(grouped[country])
        nodes.extend(candidates[:3] if country == "us" else candidates[:1])
    if len(nodes) < 2:
        raise EngineError("Check-Host 可用区域节点少于两个")
    return nodes


def _tcp_probe_status(value: Any) -> int | None:
    if (
        isinstance(value, list)
        and len(value) >= 2
        and value[0] in (0, 1, "0", "1", False, True)
        and isinstance(value[1], (int, float))
    ):
        return int(value[0])
    return None


def _extract_tcp_latencies(result: Any) -> list[float]:
    """读取 Check-Host TCP 的 [状态, 秒数, ...] 结果并换算为毫秒。"""
    values: list[float] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("time"), (int, float)):
                values.append(float(value["time"]) * 1000)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            status = _tcp_probe_status(value)
            if status == 1 and isinstance(value[1], (int, float)):
                values.append(float(value[1]) * 1000)
            elif status is None:
                for child in value:
                    walk(child)

    walk(result)
    return values


def _has_completed_tcp_probe(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value) and (
            "time" in value
            or "error" in value
            or any(_has_completed_tcp_probe(child) for child in value.values())
        )
    if isinstance(value, list):
        if _tcp_probe_status(value) is not None:
            return True
        return any(_has_completed_tcp_probe(child) for child in value)
    return False


def state_path(line_id: str) -> Path:
    return STATE_DIR / f"cf_failover_{line_id}_state.txt"


def lock_path(line_id: str) -> Path:
    return STATE_DIR / f"cf_failover_{line_id}.lock"


def get_state(line_id: str) -> str:
    try:
        value = state_path(line_id).read_text(encoding="utf-8").strip().upper()
        return value if value in {"MAIN", "BACKUP"} else "MAIN"
    except OSError:
        return "MAIN"


def set_state(line_id: str, value: str) -> None:
    state_path(line_id).parent.mkdir(parents=True, exist_ok=True)
    state_path(line_id).write_text(value + "\n", encoding="utf-8")


def switch_once(cfg: Mapping[str, Any], line_id: str, line: Mapping[str, Any], mode: str) -> int:
    target = line["main_ip"] if mode == "MAIN" else line["backup_ip"]
    count = update_dns(cfg, line_id, line, target, mode)
    total = len(line["domains"])
    if count != total:
        raise EngineError(f"DNS 仅成功更新 {count}/{total} 条，未写入状态文件，请检查 Token、域名和记录权限")
    set_state(line_id, mode)
    print(f"已切换 {line['name']} -> {mode} ({target})，成功 {count}/{total} 条")
    return count


def monitor(line_id: str) -> int:
    if fcntl is None:
        raise EngineError("常驻监控必须运行在 Linux（需要 fcntl 文件锁）")
    cfg = load_config()
    line = get_line(cfg, line_id)
    lock_path(line_id).parent.mkdir(parents=True, exist_ok=True)
    with lock_path(line_id).open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log(f"{line_id} 号线已有监控进程，退出")
            return 0
        global_cfg = cfg["global"]
        port = int(line.get("port", global_cfg.get("tcp_port", 22)))
        interval = int(global_cfg.get("check_interval", 30))
        recovery_interval = int(global_cfg.get("recovery_interval", 1800))
        timeout = float(global_cfg.get("probe_timeout", 2.5))
        external_threshold = float(global_cfg.get("external_threshold_ms", 150))
        last_recovery = time.time()
        log(f"启动 {line['name']} | 主 IP {line['main_ip']} | 备用 IP {line['backup_ip']}")
        while True:
            try:
                cfg = load_config()
                line = get_line(cfg, line_id)
                global_cfg = cfg["global"]
                port = int(line.get("port", global_cfg.get("tcp_port", 22)))
                interval = int(global_cfg.get("check_interval", 30))
                recovery_interval = int(global_cfg.get("recovery_interval", 1800))
                timeout = float(global_cfg.get("probe_timeout", 2.5))
                external_threshold = float(global_cfg.get("external_threshold_ms", 150))
                state = get_state(line_id)
                if state == "MAIN":
                    failed, latency, samples = tcping(line["main_ip"], port, 5, timeout)
                    if not failed or latency > float(line.get("latency_threshold", 280)):
                        wait_s = int(line.get("recheck_wait", 20))
                        log(f"{line['name']} 初检异常，{wait_s}s 后二次复核")
                        time.sleep(wait_s)
                        failed2, latency2, samples2 = tcping(line["main_ip"], port, 5, timeout)
                        if not failed2 or latency2 > float(line.get("latency_threshold", 280)):
                            time.sleep(wait_s)
                            failed3, latency3, samples3 = tcping(line["main_ip"], port, 5, timeout)
                            if not failed3 or latency3 > float(line.get("latency_threshold", 280)):
                                min_alive = int(global_cfg.get("monitor_network_min_alive", 2))
                                if is_monitor_network_alive(min_alive) and not check_external_latency(line["main_ip"], port, str(line.get("region", "jp")), external_threshold):
                                    count = update_dns(cfg, line_id, line, line["backup_ip"], "BACKUP", samples + samples2 + samples3)
                                    if count == len(line["domains"]):
                                        set_state(line_id, "BACKUP")
                                        last_recovery = time.time()
                                    else:
                                        log(f"DNS 只更新 {count}/{len(line['domains'])} 条，暂不写入 BACKUP 状态")
                                else:
                                    log("监控机出口或外部仲裁不满足切换条件，拦截误切")
                elif time.time() - last_recovery >= recovery_interval:
                    ok, latency, samples = tcping(line["main_ip"], port, 5, timeout)
                    if ok and latency <= float(line.get("recovery_max_lat", 160)):
                        count = update_dns(cfg, line_id, line, line["main_ip"], "MAIN", samples)
                        if count == len(line["domains"]):
                            set_state(line_id, "MAIN")
                        else:
                            log(f"DNS 只更新 {count}/{len(line['domains'])} 条，暂不写入 MAIN 状态")
                    last_recovery = time.time()
            except Exception as exc:
                log(f"巡检异常: {exc}")
            time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通用 Cloudflare 多线路容灾引擎")
    parser.add_argument("--line", required=True)
    parser.add_argument("--switch", action="store_true")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    if args.switch and args.backup:
        parser.error("--switch 和 --backup 不能同时使用")
    try:
        cfg = load_config()
        if args.status:
            line = cfg["lines"].get(str(args.line))
            if not isinstance(line, dict):
                raise EngineError(f"找不到 {args.line} 号线配置")
            print(f"{line['name']}: {get_state(str(args.line))}")
            return 0
        line = get_line(cfg, str(args.line))
        if args.switch:
            switch_once(cfg, str(args.line), line, "MAIN")
            return 0
        if args.backup:
            switch_once(cfg, str(args.line), line, "BACKUP")
            return 0
        return monitor(str(args.line))
    except EngineError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
