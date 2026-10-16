#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cf-menu：配置与代码分离后的中文运维菜单。"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("CF_FAILOVER_ROOT", "/opt/cf-failover"))
CONFIG_PATH = ROOT / "config.json"
ENGINE = ROOT / "failover_engine.py"
SYSTEMCTL = ["systemctl"]


class MenuError(RuntimeError):
    pass


def load_config() -> dict[str, Any]:
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MenuError(f"读取配置失败: {exc}") from exc
    value.setdefault("global", {})
    value.setdefault("lines", {})
    return value


def save_config(config: dict[str, Any]) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".config.", dir=str(ROOT))
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, CONFIG_PATH)
        if os.name != "nt":
            os.chmod(CONFIG_PATH, 0o600)
    finally:
        temp.unlink(missing_ok=True)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def ask_secret(prompt: str, default: str = "") -> str:
    import getpass
    if default:
        shown = default[:4] + "..." if len(default) > 4 else "已配置"
        value = getpass.getpass(f"{prompt} [{shown}]（直接回车保留）: ").strip()
    else:
        value = getpass.getpass(f"{prompt}: ").strip()
    return value or default


def ask_ip(prompt: str, default: str = "") -> str:
    while True:
        value = ask(prompt, default)
        try:
            return str(ipaddress.IPv4Address(value))
        except ipaddress.AddressValueError:
            print("请输入合法 IPv4 地址，例如 203.0.113.10")


def ask_int(prompt: str, default: int, minimum: int = 1, maximum: int = 60000) -> int:
    while True:
        try:
            value = int(ask(prompt, str(default)))
            if minimum <= value <= maximum:
                return value
        except ValueError:
            pass
        print(f"请输入 {minimum}~{maximum} 之间的整数")


def parse_domains(value: str) -> list[str]:
    """把空格、逗号和换行分隔的输入统一成去重后的域名列表。"""
    result: list[str] = []
    for item in value.replace(",", " ").split():
        item = item.strip().rstrip(".").lower()
        if item and item not in result and "." in item and " " not in item:
            result.append(item)
    return result


def run(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    print("$ " + " ".join(args))
    if os.environ.get("CF_FAILOVER_DRY_RUN") == "1":
        return subprocess.CompletedProcess(args, 0, "", "")
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise MenuError((result.stderr or result.stdout or "命令失败").strip())
    return result


def service(action: str, line_id: str | None = None, check: bool = False) -> None:
    name = f"cf-failover@{line_id}.service" if line_id else "cf-failover@*.service"
    args = SYSTEMCTL + [action, name]
    run(args, check=check)


def restart_line(line_id: str) -> None:
    """配置落盘后重载并重启对应线路，避免旧进程继续使用旧配置。"""
    run(SYSTEMCTL + ["daemon-reload"], check=False)
    run(SYSTEMCTL + ["enable", f"cf-failover@{line_id}.service"], check=False)
    run(SYSTEMCTL + ["restart", f"cf-failover@{line_id}.service"], check=False)


def show_lines(config: dict[str, Any]) -> None:
    print("\n线路列表")
    for line_id, line in sorted(config["lines"].items(), key=lambda item: int(item[0]) if item[0].isdigit() else item[0]):
        state_path = Path("/tmp") / f"cf_failover_{line_id}_state.txt"
        state = state_path.read_text(encoding="utf-8").strip() if state_path.exists() else "MAIN"
        status = run(SYSTEMCTL + ["is-active", f"cf-failover@{line_id}.service"]).stdout.strip() or "unknown"
        domains = line.get("domains", [])
        print(f"[{line_id}] {line.get('name', '')} | 服务={status} | 指向={state}")
        print(f"    监控脚本: failover_engine.py --line {line_id}")
        print(f"    systemd: cf-failover@{line_id}.service")
        print(f"    主 IP: {line.get('main_ip')} | 备 IP: {line.get('backup_ip')} | 域名数: {len(domains)}")
        print(f"    绑定域名: {', '.join(domains) if domains else '（无）'}")


def configure_global(config: dict[str, Any]) -> None:
    global_cfg = config["global"]
    print("\n全局配置（输入为空保留原值）")
    token = ask_secret("Cloudflare API Token", str(global_cfg.get("cf_api_token", "")))
    tg_token = ask_secret("Telegram Bot Token", str(global_cfg.get("tg_bot_token", "")))
    admin = ask_int("TG 管理员数字 ID", int(global_cfg.get("tg_admin_id", 0)), 0, 10**18)
    group = ask("TG 群组 ID（可留空）", str(global_cfg.get("tg_group_id", "")))
    global_cfg.update({"cf_api_token": token, "tg_bot_token": tg_token, "tg_admin_id": admin, "tg_group_id": group})
    save_config(config)
    run(SYSTEMCTL + ["restart", "tg-bot.service"], check=False)
    print("全局配置已保存，TG Bot 已尝试重启")


def _save_line(config: dict[str, Any], line_id: str, line: dict[str, Any]) -> None:
    config["lines"][line_id] = line
    save_config(config)
    restart_line(line_id)
    print(f"{line_id} 号线已保存，服务已自动重启")


def _show_line_detail(line_id: str, line: dict[str, Any]) -> None:
    print(f"\n线路名称: {line.get('name', line_id)}")
    print(f"监控脚本: failover_engine.py --line {line_id}")
    print(f"systemd: cf-failover@{line_id}.service")
    print(f"区域: {line.get('region', 'jp')} | 探活端口: {line.get('port', 22)}")
    print(f"主 IP: {line.get('main_ip')} | 备用 IP: {line.get('backup_ip')}")
    print(f"故障/恢复阈值: {line.get('latency_threshold', 280)} / {line.get('recovery_max_lat', 160)} ms")
    print("绑定域名:")
    for index, domain in enumerate(line.get("domains", []), 1):
        print(f"  [{index}] {domain}")


def _full_edit_line(config: dict[str, Any], line_id: str, old: dict[str, Any]) -> dict[str, Any]:
    name = ask("线路名称", old.get("name", f"{line_id}号线"))
    region = ask("仲裁区域 jp/us/eu", old.get("region", "jp"))
    if region not in {"jp", "us", "eu"}:
        raise MenuError("区域只能是 jp、us 或 eu")
    main_ip = ask_ip("主用 IP", old.get("main_ip", ""))
    backup_ip = ask_ip("备用 IP", old.get("backup_ip", ""))
    port = ask_int("探活端口", int(old.get("port", config.get("global", {}).get("tcp_port", 22))), 1, 65535)
    threshold = ask_int("故障延迟阈值(ms)", int(old.get("latency_threshold", 280)))
    recovery = ask_int("恢复延迟阈值(ms)", int(old.get("recovery_max_lat", 160)))
    wait_s = ask_int("三级复核间隔(秒)", int(old.get("recheck_wait", 20)), 1, 600)
    current = list(old.get("domains", []))
    print("当前域名：" + (" ".join(current) if current else "（无）"))
    domain_text = ask("域名列表（空格/逗号/换行分隔，留空保留）", "")
    domains = parse_domains(domain_text) if domain_text else current
    if not domains:
        raise MenuError("至少需要一个域名")
    return {
        "name": name,
        "region": region,
        "main_ip": main_ip,
        "backup_ip": backup_ip,
        "port": port,
        "latency_threshold": threshold,
        "recovery_max_lat": recovery,
        "recheck_wait": wait_s,
        "domains": domains,
    }


def edit_line(config: dict[str, Any]) -> str:
    line_id = ask("线路编号")
    old = config["lines"].get(line_id, {})
    if not old:
        _save_line(config, line_id, _full_edit_line(config, line_id, old))
        return line_id
    while True:
        _show_line_detail(line_id, old)
        print("\n线路编辑：1查看  2改主IP  3改备用IP  4添加域名  5删除域名  6完整编辑  0返回")
        choice = input("请选择: ").strip()
        if choice == "0":
            break
        if choice == "1":
            continue
        if choice == "2":
            old["main_ip"] = ask_ip("新的主用 IP", old.get("main_ip", ""))
        elif choice == "3":
            old["backup_ip"] = ask_ip("新的备用 IP", old.get("backup_ip", ""))
        elif choice == "4":
            additions = parse_domains(input("输入要添加的域名（空格/逗号/换行分隔）: "))
            if not additions:
                raise MenuError("没有识别到合法域名")
            old["domains"] = list(dict.fromkeys(list(old.get("domains", [])) + additions))
        elif choice == "5":
            domains = list(old.get("domains", []))
            if not domains:
                raise MenuError("当前没有绑定域名")
            for index, domain in enumerate(domains, 1):
                print(f"  [{index}] {domain}")
            selected = input("输入要删除的序号（可多个，用空格分隔）: ").split()
            indexes = {int(item) for item in selected if item.isdigit()}
            if not indexes or any(index < 1 or index > len(domains) for index in indexes):
                raise MenuError("删除序号无效")
            if input("确认删除请输入 DELETE: ").strip() != "DELETE":
                print("已取消")
                continue
            remaining = [domain for index, domain in enumerate(domains, 1) if index not in indexes]
            if not remaining:
                raise MenuError("至少保留一个域名")
            old["domains"] = remaining
        elif choice == "6":
            old = _full_edit_line(config, line_id, old)
        else:
            print("请输入 0~6")
            continue
        _save_line(config, line_id, old)
    return line_id


def delete_line(config: dict[str, Any]) -> None:
    line_id = ask("要删除的线路编号")
    if line_id not in config["lines"]:
        raise MenuError("线路不存在")
    if input(f"确定删除 {config['lines'][line_id].get('name', line_id)}？输入 DELETE: ").strip() != "DELETE":
        print("已取消")
        return
    run(SYSTEMCTL + ["disable", "--now", f"cf-failover@{line_id}.service"], check=False)
    del config["lines"][line_id]
    save_config(config)
    print(f"{line_id} 号线已删除")


def force_switch(config: dict[str, Any]) -> None:
    line_id = ask("线路编号")
    if line_id not in config["lines"]:
        raise MenuError("线路不存在")
    action = ask("输入 1 切主，2 切备")
    flag = "--switch" if action == "1" else "--backup" if action == "2" else ""
    if not flag:
        raise MenuError("只能输入 1 或 2")
    run([sys.executable, str(ENGINE), "--line", line_id, flag], check=True)


def backup_config(config: dict[str, Any]) -> None:
    target = Path(ask("备份文件路径", f"{ROOT}/config.backup.json"))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CONFIG_PATH, target)
    print(f"配置已备份到 {target}")


def menu() -> int:
    while True:
        try:
            config = load_config()
            print("\n" + "=" * 58)
            print(" Cloudflare 多线路容灾与 TG 机器人管理面板")
            print("=" * 58)
            print(" [1] 查看线路与实时状态")
            print(" [2] 新增/修改线路（IP、域名、阈值）")
            print(" [3] 删除线路")
            print(" [4] 配置全局 Token 与管理员 ID")
            print(" [5] 手动切主/切备")
            print(" [6] 重启全部服务与 TG Bot")
            print(" [7] 导出 config.json 备份")
            print(" [0] 退出")
            choice = input("请输入选项 [0-7]: ").strip()
            if choice == "0":
                return 0
            if choice == "1":
                show_lines(config)
            elif choice == "2":
                edit_line(config)
            elif choice == "3":
                delete_line(config)
            elif choice == "4":
                configure_global(config)
            elif choice == "5":
                force_switch(config)
            elif choice == "6":
                for line_id in config["lines"]:
                    run(SYSTEMCTL + ["enable", "--now", f"cf-failover@{line_id}.service"], check=False)
                run(SYSTEMCTL + ["restart", "tg-bot.service"], check=False)
                print("全部服务已尝试重启")
            elif choice == "7":
                backup_config(config)
            else:
                print("请输入 0~7")
        except (MenuError, EOFError, KeyboardInterrupt) as exc:
            if isinstance(exc, (EOFError, KeyboardInterrupt)):
                print()
                return 130 if isinstance(exc, KeyboardInterrupt) else 0
            print(f"错误：{exc}")


if __name__ == "__main__":
    raise SystemExit(menu())
