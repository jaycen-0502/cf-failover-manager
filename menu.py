#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cf-menu：配置与代码分离后的中文运维菜单。"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("CF_FAILOVER_ROOT", "/opt/cf-failover"))
CONFIG_PATH = ROOT / "config.json"
ENGINE = ROOT / "failover_engine.py"
STATE_DIR = Path(os.environ.get("CF_FAILOVER_STATE_DIR", "/tmp"))
SYSTEMCTL = ["systemctl"]


class MenuError(RuntimeError):
    pass


def load_config() -> dict[str, Any]:
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MenuError(f"读取配置失败: {exc}") from exc
    if not isinstance(value, dict):
        raise MenuError("配置文件顶层必须是 JSON 对象")
    value.setdefault("global", {})
    value.setdefault("lines", {})
    if not isinstance(value["global"], dict) or not isinstance(value["lines"], dict):
        raise MenuError("配置中的 global 和 lines 必须是对象")
    invalid_ids = [key for key in value["lines"] if not str(key).isdigit() or int(key) < 1 or str(int(key)) != str(key)]
    if invalid_ids:
        raise MenuError("线路编号必须全部为正整数")
    if any(not isinstance(line, dict) for line in value["lines"].values()):
        raise MenuError("每条线路配置都必须是 JSON 对象")
    try:
        admin_id = int(value["global"].get("tg_admin_id", 0))
    except (TypeError, ValueError) as exc:
        raise MenuError("TG 管理员 ID 必须是数字") from exc
    if admin_id < 0:
        raise MenuError("TG 管理员 ID 不能是负数")
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
    suffix = "（直接回车保留）" if default else ""
    value = getpass.getpass(f"{prompt}{suffix}: ").strip()
    return value or default


def secret_status(value: Any) -> str:
    text = str(value or "")
    if not text or text.startswith("YOUR_"):
        return "未配置"
    return "已配置"


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
        item = item.strip().lower()
        if item.endswith("."):
            item = item[:-1]
        if _valid_domain(item) and item not in result:
            result.append(item)
    return result


def _valid_domain(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 253 or "." not in value:
        return False
    normalized = value[:-1] if value.endswith(".") else value
    if not normalized or normalized.endswith("."):
        return False
    labels = normalized.split(".")
    return all(
        0 < len(label) <= 63
        and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
        for label in labels
    )


def _line_is_ready(config: dict[str, Any], line: Any) -> bool:
    token = str(config.get("global", {}).get("cf_api_token", ""))
    if not token or token.startswith("YOUR_") or not isinstance(line, dict):
        return False
    region = line.get("region", "jp")
    if not line.get("name") or not isinstance(region, str) or region not in {"jp", "us", "eu"}:
        return False
    try:
        ipaddress.IPv4Address(str(line.get("main_ip", "")))
        ipaddress.IPv4Address(str(line.get("backup_ip", "")))
        port = int(line.get("port", config.get("global", {}).get("tcp_port", 22)))
    except (ipaddress.AddressValueError, TypeError, ValueError):
        return False
    domains = line.get("domains")
    return 1 <= port <= 65535 and isinstance(domains, list) and bool(domains) and all(_valid_domain(item) for item in domains)


def run(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    print("$ " + " ".join(args))
    if os.environ.get("CF_FAILOVER_DRY_RUN") == "1":
        return subprocess.CompletedProcess(args, 0, "", "")
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if result.returncode and not check:
        detail = (result.stderr or result.stdout or "命令失败").strip()
        print(f"⚠️ 命令未成功（返回码 {result.returncode}）：{detail}")
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
    run(SYSTEMCTL + ["enable", f"cf-failover@{line_id}.service"], check=True)
    run(SYSTEMCTL + ["restart", f"cf-failover@{line_id}.service"], check=True)


def _save_global(config: dict[str, Any]) -> None:
    save_config(config)
    run(SYSTEMCTL + ["daemon-reload"], check=False)


def _start_line_services(config: dict[str, Any]) -> None:
    for line_id, line in config.get("lines", {}).items():
        if not _line_is_ready(config, line):
            print(f"跳过 {line_id} 号线：Token、线路名称、区域、主/备 IP、端口或域名配置不完整/无效")
            run(SYSTEMCTL + ["disable", "--now", f"cf-failover@{line_id}.service"], check=False)
            continue
        run(SYSTEMCTL + ["enable", f"cf-failover@{line_id}.service"], check=True)
        run(SYSTEMCTL + ["restart", f"cf-failover@{line_id}.service"], check=True)


def _stop_line_services(config: dict[str, Any]) -> None:
    for line_id in config.get("lines", {}):
        run(SYSTEMCTL + ["disable", "--now", f"cf-failover@{line_id}.service"], check=False)


def _restart_all_services(config: dict[str, Any]) -> None:
    token = str(config.get("global", {}).get("cf_api_token", ""))
    if not token or token.startswith("YOUR_"):
        _stop_line_services(config)
        print("Cloudflare Token 未配置，所有线路服务保持停用")
    else:
        _start_line_services(config)

    global_cfg = config.get("global", {})
    bot_token = str(global_cfg.get("tg_bot_token", ""))
    try:
        admin_id = int(global_cfg.get("tg_admin_id", 0))
    except (TypeError, ValueError):
        admin_id = 0
    if bot_token and not bot_token.startswith("YOUR_") and admin_id > 0:
        run(SYSTEMCTL + ["enable", "tg-bot.service"], check=True)
        run(SYSTEMCTL + ["restart", "tg-bot.service"], check=True)
    else:
        run(SYSTEMCTL + ["disable", "--now", "tg-bot.service"], check=False)
        print("Telegram Token 或管理员 ID 未配置，TG Bot 保持停用")


def set_global_secret(config: dict[str, Any], key: str, label: str) -> None:
    if key not in {"cf_api_token", "tg_bot_token"}:
        raise MenuError("不支持的密钥类型")
    current = str(config["global"].get(key, ""))
    value = ask_secret(f"{label}（当前{secret_status(current)}）", current)
    if not value or value.startswith("YOUR_"):
        raise MenuError(f"{label}不能为空；要删除请使用删除选项")
    _verify_global_secret(key, value)
    config["global"][key] = value
    _save_global(config)
    if key == "cf_api_token":
        _start_line_services(config)
    else:
        try:
            admin_id = int(config["global"].get("tg_admin_id", 0))
        except (TypeError, ValueError):
            admin_id = 0
        if admin_id > 0:
            run(SYSTEMCTL + ["enable", "tg-bot.service"], check=True)
            run(SYSTEMCTL + ["restart", "tg-bot.service"], check=True)
        else:
            run(SYSTEMCTL + ["disable", "--now", "tg-bot.service"], check=False)
            print("Token 已保存，但 TG 管理员 ID 未配置；请先选择菜单 5 设置 ID，再启动机器人")
    print(f"{label}已保存：{secret_status(value)}")


def _verify_global_secret(key: str, value: str) -> None:
    """Save only credentials confirmed active by their providers."""
    if key == "cf_api_token":
        request = urllib.request.Request(
            "https://api.cloudflare.com/client/v4/user/tokens/verify",
            headers={"Authorization": f"Bearer {value}", "Content-Type": "application/json"},
        )
    elif key == "tg_bot_token":
        request = urllib.request.Request(f"https://api.telegram.org/bot{value}/getMe")
    else:
        raise MenuError("不支持的密钥类型")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        provider = "Cloudflare" if key == "cf_api_token" else "Telegram"
        raise MenuError(f"{provider} Token 验证失败；请检查 Token 和服务器网络，配置未更改") from exc
    if not isinstance(result, dict):
        raise MenuError("Token 服务返回格式无效，配置未更改")
    if key == "cf_api_token":
        valid = result.get("success") is True and isinstance(result.get("result"), dict) and result["result"].get("status") == "active"
    else:
        valid = result.get("ok") is True
    if not valid:
        provider = "Cloudflare" if key == "cf_api_token" else "Telegram"
        raise MenuError(f"{provider} Token 无效或已停用，配置未更改")


def delete_global_secret(config: dict[str, Any], key: str, label: str) -> None:
    if key not in {"cf_api_token", "tg_bot_token"}:
        raise MenuError("不支持的密钥类型")
    current = str(config["global"].get(key) or "")
    if not current:
        print(f"{label}当前已经是空的")
        return
    if current.startswith("YOUR_"):
        if key == "cf_api_token":
            _stop_line_services(config)
        else:
            run(SYSTEMCTL + ["disable", "--now", "tg-bot.service"], check=False)
        config["global"][key] = ""
        _save_global(config)
        print(f"示例占位值已清除：{label}")
        return
    if input(f"确定删除{label}？相关服务会停用。请输入 DELETE: ").strip() != "DELETE":
        print("已取消")
        return
    if key == "cf_api_token":
        _stop_line_services(config)
    else:
        run(SYSTEMCTL + ["disable", "--now", "tg-bot.service"], check=False)
    config["global"][key] = ""
    _save_global(config)
    print(f"{label}已删除，相关服务已停用")


def show_lines(config: dict[str, Any]) -> None:
    print("\n线路列表")
    for line_id, line in sorted(config["lines"].items(), key=lambda item: int(item[0]) if item[0].isdigit() else item[0]):
        state_path = STATE_DIR / f"cf_failover_{line_id}_state.txt"
        state = state_path.read_text(encoding="utf-8").strip() if state_path.exists() else "MAIN"
        status = run(SYSTEMCTL + ["is-active", f"cf-failover@{line_id}.service"]).stdout.strip() or "unknown"
        domains = line.get("domains", [])
        print(f"[{line_id}] {line.get('name', '')} | 服务={status} | 指向={state}")
        print(f"    监控脚本: failover_engine.py --line {line_id}")
        print(f"    systemd: cf-failover@{line_id}.service")
        print(f"    主 IP: {line.get('main_ip')} | 备 IP: {line.get('backup_ip')} | 域名数: {len(domains)}")
        print(f"    绑定域名: {', '.join(domains) if domains else '（无）'}")


def configure_global(config: dict[str, Any]) -> None:
    while True:
        global_cfg = config["global"]
        print("\n全局配置")
        print(f"  Cloudflare Token: {secret_status(global_cfg.get('cf_api_token'))}")
        print(f"  Telegram Token:   {secret_status(global_cfg.get('tg_bot_token'))}")
        print(f"  TG 管理员 ID:     {global_cfg.get('tg_admin_id', 0)}")
        print(f"  TG 群组 ID:       {global_cfg.get('tg_group_id') or '未配置'}")
        print("  [1] 填入/更新 Cloudflare Token")
        print("  [2] 删除 Cloudflare Token（停用全部线路）")
        print("  [3] 填入/更新 Telegram Token")
        print("  [4] 删除 Telegram Token（停用 TG Bot）")
        print("  [5] 修改 TG 管理员数字 ID")
        print("  [6] 修改 TG 群组 ID")
        print("  [0] 返回")
        choice = input("请选择: ").strip()
        if choice == "0":
            return
        if choice == "1":
            set_global_secret(config, "cf_api_token", "Cloudflare API Token")
        elif choice == "2":
            delete_global_secret(config, "cf_api_token", "Cloudflare API Token")
        elif choice == "3":
            set_global_secret(config, "tg_bot_token", "Telegram Bot Token")
        elif choice == "4":
            delete_global_secret(config, "tg_bot_token", "Telegram Bot Token")
        elif choice == "5":
            global_cfg["tg_admin_id"] = ask_int("TG 管理员数字 ID", int(global_cfg.get("tg_admin_id", 0)), 1, 10**18)
            _save_global(config)
            if global_cfg.get("tg_bot_token") and not str(global_cfg.get("tg_bot_token")).startswith("YOUR_"):
                run(SYSTEMCTL + ["enable", "tg-bot.service"], check=True)
                run(SYSTEMCTL + ["restart", "tg-bot.service"], check=True)
        elif choice == "6":
            group = ask("TG 群组 ID（输入 DELETE 可清空）", str(global_cfg.get("tg_group_id", "")))
            global_cfg["tg_group_id"] = "" if group.upper() == "DELETE" else group
            _save_global(config)
            if global_cfg.get("tg_bot_token") and int(global_cfg.get("tg_admin_id", 0)) > 0:
                run(SYSTEMCTL + ["restart", "tg-bot.service"], check=True)
        else:
            print("请输入 0~6")


def _save_line(config: dict[str, Any], line_id: str, line: dict[str, Any]) -> None:
    ready = _line_is_ready(config, line)
    if not ready:
        run(SYSTEMCTL + ["disable", "--now", f"cf-failover@{line_id}.service"], check=False)
    config["lines"][line_id] = line
    save_config(config)
    if ready:
        restart_line(line_id)
        print(f"{line_id} 号线已保存，服务已自动重启")
    else:
        print(f"{line_id} 号线配置已保存，但 Token、主/备 IP 或域名不完整，服务已停用")


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
    if not line_id.isdigit() or int(line_id) < 1:
        raise MenuError("线路编号必须是正整数")
    line_id = str(int(line_id))
    old = config["lines"].get(line_id, {})
    if not old:
        _save_line(config, line_id, _full_edit_line(config, line_id, old))
        return line_id
    while True:
        _show_line_detail(line_id, old)
        print("\n线路编辑：1查看  2填入/改主IP  3填入/改备用IP  4添加域名  5删除域名  6完整编辑  7删除主IP  8删除备用IP  0返回")
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
            old["domains"] = remaining
        elif choice == "6":
            old = _full_edit_line(config, line_id, old)
        elif choice == "7":
            delete_line_ip(config, line_id, "main_ip")
            old = config["lines"][line_id]
            continue
        elif choice == "8":
            delete_line_ip(config, line_id, "backup_ip")
            old = config["lines"][line_id]
            continue
        else:
            print("请输入 0~8")
            continue
        _save_line(config, line_id, old)
    return line_id


def delete_line_ip(config: dict[str, Any], line_id: str, field: str) -> None:
    """清除主/备用生产 IP；清除后立即停用该线路，防止空配置被误用。"""
    if field not in {"main_ip", "backup_ip"}:
        raise MenuError("只能删除 main_ip 或 backup_ip")
    line = config["lines"].get(line_id)
    if not isinstance(line, dict):
        raise MenuError("线路不存在")
    label = "主用 IP" if field == "main_ip" else "备用 IP"
    if not line.get(field):
        print(f"{label}当前已经是空的")
        return
    if input(f"确定删除{line_id}号线的{label}？请输入 DELETE: ").strip() != "DELETE":
        print("已取消")
        return
    run(SYSTEMCTL + ["disable", "--now", f"cf-failover@{line_id}.service"], check=False)
    line[field] = ""
    save_config(config)
    print(f"{line_id}号线{label}已删除，线路服务已停用；填回 IP 后会自动恢复服务")


def delete_line(config: dict[str, Any]) -> None:
    line_id = ask("要删除的线路编号")
    if not line_id.isdigit() or int(line_id) < 1:
        raise MenuError("线路编号必须是正整数")
    line_id = str(int(line_id))
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
    if not line_id.isdigit() or int(line_id) < 1:
        raise MenuError("线路编号必须是正整数")
    line_id = str(int(line_id))
    if line_id not in config["lines"]:
        raise MenuError("线路不存在")
    if not _line_is_ready(config, config["lines"][line_id]):
        raise MenuError("该线路配置不完整或无效，暂时不能手动切换")
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
                _restart_all_services(config)
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
