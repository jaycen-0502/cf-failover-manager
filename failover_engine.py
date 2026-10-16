#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用多线路容灾引擎：所有线路由 /opt/cf-failover/config.json 驱动。"""
from __future__ import annotations

import argparse
import contextlib
try:
    import fcntl
except ImportError:  # Windows 仅用于离线测试；生产运行仍要求 Linux 文件锁。
    fcntl = None
import json
import os
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
    if not isinstance(cfg.get("global"), dict) or not isinstance(cfg.get("lines"), dict):
        raise EngineError("配置必须包含 global 和 lines")
    return cfg


def get_line(cfg: Mapping[str, Any], line_id: str) -> dict[str, Any]:
    line = cfg["lines"].get(str(line_id))
    if not isinstance(line, dict):
        raise EngineError(f"找不到 {line_id} 号线配置")
    required = ("name", "main_ip", "backup_ip", "domains")
    missing = [key for key in required if not line.get(key)]
    if missing:
        raise EngineError(f"{line_id} 号线缺少配置: {', '.join(missing)}")
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


def _load_cache() -> dict[str, dict[str, str]]:
    try:
        value = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: Mapping[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = CACHE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, CACHE_PATH)
    if os.name != "nt":
        os.chmod(CACHE_PATH, 0o600)


def resolve_domain_ids(token: str, domains: list[str]) -> list[dict[str, str]]:
    """按完整域名自动查 Zone/Record ID，并缓存结果。"""
    if not token or token.startswith("YOUR_"):
        raise EngineError("未配置有效 Cloudflare API Token")
    cache = _load_cache()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    records: list[dict[str, str]] = []
    changed = False
    for domain in domains:
        cached = cache.get(domain)
        if cached and cached.get("zone_id") and cached.get("rec_id"):
            records.append(cached)
            continue
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
        item = {"zone_id": zone_id, "rec_id": str(found[0]["id"]), "name": domain}
        cache[domain] = item
        records.append(item)
        changed = True
    if changed:
        _save_cache(cache)
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
    group_text = f"【{line['name']}】线路异常/解析已切换到{('主节点' if mode == 'MAIN' else '备用节点')}。"
    private = f"【{line['name']}】{title}\n目标 IP: {target_ip}\n解析成功: {count}/{total}"
    if latencies:
        private += "\n复核延迟: " + ", ".join(f"{value:.1f}ms" for value in latencies)
    send_telegram(str(global_cfg.get("tg_bot_token", "")), global_cfg.get("tg_group_id"), group_text)
    send_telegram(str(global_cfg.get("tg_bot_token", "")), global_cfg.get("tg_admin_id"), private)


def update_dns(cfg: Mapping[str, Any], line_id: str, line: Mapping[str, Any], target_ip: str, mode: str, latencies: list[float] | None = None) -> int:
    records = resolve_domain_ids(str(cfg["global"].get("cf_api_token", "")), list(line.get("domains", [])))
    token = str(cfg["global"].get("cf_api_token", ""))
    with ThreadPoolExecutor(max_workers=min(10, max(1, len(records)))) as executor:
        results = list(executor.map(lambda record: _cloudflare_update(record, target_ip, token), records))
    count = sum(results)
    notify(cfg, line, target_ip, mode, count, len(line.get("domains", [])), latencies)
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
    return alive >= max(1, min_alive)


def check_external_latency(ip: str, port: int, region: str, threshold: float) -> bool:
    nodes = REGION_NODES.get(region, REGION_NODES["jp"])
    query = urllib.parse.urlencode({"host": f"{ip}:{port}", "max_nodes": ",".join(nodes)})
    try:
        first = _request_json(urllib.request.Request("https://check-host.net/check-tcp?" + query), 15)
        request_id = first.get("request_id")
        if not request_id:
            return True
        observed: list[float] = []
        for _ in range(8):
            time.sleep(2)
            result = _request_json(urllib.request.Request("https://check-host.net/check-result/" + urllib.parse.quote(str(request_id), safe="")), 15)
            values: list[float] = []
            def walk(value: Any) -> None:
                if isinstance(value, dict):
                    if isinstance(value.get("time"), (int, float)):
                        values.append(float(value["time"]) * 1000)
                    for child in value.values():
                        walk(child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)
            walk(result)
            if values and any(value <= threshold for value in values):
                return True
            observed.extend(values)
        # 无有效第三方探测结果属于不确定状态，按保守策略拦截自动切换。
        return not observed
    except EngineError as exc:
        log(f"Check-Host 仲裁失败，按保守策略拦截切换: {exc}")
        return True


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
    cfg = load_config()
    line = get_line(cfg, str(args.line))
    if args.status:
        print(f"{line['name']}: {get_state(str(args.line))}")
        return 0
    if args.switch:
        switch_once(cfg, str(args.line), line, "MAIN")
        return 0
    if args.backup:
        switch_once(cfg, str(args.line), line, "BACKUP")
        return 0
    return monitor(str(args.line))


if __name__ == "__main__":
    raise SystemExit(main())
