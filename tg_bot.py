#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动态读取 config.json 的 Telegram 控制机器人。"""
from __future__ import annotations

import json
import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import telebot
except ImportError:  # 让安装前执行 --check-config 仍然可用
    telebot = None

ROOT = Path(os.environ.get("CF_FAILOVER_ROOT", "/opt/cf-failover"))
CONFIG_PATH = ROOT / "config.json"
ENGINE = ROOT / "failover_engine.py"
STATE_DIR = Path(os.environ.get("CF_FAILOVER_STATE_DIR", "/tmp"))


def load_config() -> dict[str, Any]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(config.get("global"), dict) or not isinstance(config.get("lines"), dict):
        raise ValueError("配置必须是包含 global 和 lines 的 JSON 对象")
    if any(not str(key).isdigit() or int(key) < 1 or str(int(key)) != str(key) for key in config["lines"]):
        raise ValueError("线路编号必须全部为正整数")
    if any(not isinstance(line, dict) for line in config["lines"].values()):
        raise ValueError("每条线路配置都必须是 JSON 对象")
    return config


def line_for(config: dict[str, Any], line_id: str) -> dict[str, Any] | None:
    value = config.get("lines", {}).get(str(line_id))
    return value if isinstance(value, dict) else None


def valid_domain(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 253 or "." not in value:
        return False
    normalized = value[:-1] if value.endswith(".") else value
    if not normalized or normalized.endswith("."):
        return False
    return all(
        0 < len(label) <= 63
        and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
        for label in normalized.split(".")
    )


def line_ready(config: dict[str, Any], line: dict[str, Any] | None) -> bool:
    token = str(config.get("global", {}).get("cf_api_token", ""))
    region = line.get("region", "jp") if isinstance(line, dict) else None
    if (
        not token
        or token.startswith("YOUR_")
        or not isinstance(line, dict)
        or not line
        or not line.get("name")
        or not isinstance(region, str)
        or region not in {"jp", "us", "eu"}
    ):
        return False
    try:
        ipaddress.IPv4Address(str(line.get("main_ip", "")))
        ipaddress.IPv4Address(str(line.get("backup_ip", "")))
        port = int(line.get("port", config.get("global", {}).get("tcp_port", 22)))
    except (ipaddress.AddressValueError, TypeError, ValueError):
        return False
    domains = line.get("domains")
    return 1 <= port <= 65535 and isinstance(domains, list) and bool(domains) and all(
        valid_domain(domain) for domain in domains
    )


def allowed(message: Any, config: dict[str, Any]) -> bool:
    return str(getattr(getattr(message, "from_user", None), "id", "")) == str(config.get("global", {}).get("tg_admin_id", ""))


def parse_dynamic_command(text: str) -> tuple[str, str] | None:
    """解析 /switch_1 与群组中的 /switch_1@bot_name 两种命令格式。"""
    parts = str(text or "").strip().split(maxsplit=1)
    if not parts:
        return None
    command = parts[0].lstrip("/")
    command = command.split("@", 1)[0]
    match = re.fullmatch(r"(switch|backup|restart)_(\d+)", command)
    return (match.group(1), str(int(match.group(2)))) if match else None


def run_engine(line_id: str, flag: str) -> str:
    try:
        result = subprocess.run(
            [sys.executable, str(ENGINE), "--line", str(line_id), flag],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "执行超时：请检查 Cloudflare API 或线路网络"
    return (result.stdout or result.stderr or "无输出").strip()[-3500:]


def status_card(config: dict[str, Any]) -> str:
    rows = ["【线路状态】"]
    for line_id, line in sorted(config.get("lines", {}).items(), key=lambda item: int(item[0])):
        state_file = STATE_DIR / f"cf_failover_{line_id}_state.txt"
        state = state_file.read_text(encoding="utf-8").strip() if state_file.exists() else "MAIN"
        service = subprocess.run(
            ["systemctl", "is-active", f"cf-failover@{line_id}.service"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip() or "unknown"
        rows.append(f"{line_id}号线 {line.get('name', '')}\n状态: {state} | 服务: {service}\n主/备: {line.get('main_ip')} / {line.get('backup_ip')}\n域名数: {len(line.get('domains', []))}")
    return "\n\n".join(rows)


def build_bot() -> Any:
    if telebot is None:
        raise RuntimeError("缺少 pyTelegramBotAPI，请执行 pip install pyTelegramBotAPI")
    config = load_config()
    token = str(config.get("global", {}).get("tg_bot_token", ""))
    if not token or token.startswith("YOUR_"):
        raise RuntimeError("未配置 tg_bot_token")
    try:
        admin_id = int(config.get("global", {}).get("tg_admin_id", 0))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("tg_admin_id 必须是有效数字") from exc
    if admin_id <= 0:
        raise RuntimeError("请先在菜单中配置正数 TG 管理员 ID")
    bot = telebot.TeleBot(token)

    @bot.message_handler(commands=["status"])
    def status(message: Any) -> None:
        current = load_config()
        if allowed(message, current):
            bot.reply_to(message, status_card(current))

    @bot.message_handler(commands=["restart_all"])
    def restart_all(message: Any) -> None:
        current = load_config()
        if not allowed(message, current):
            return
        restarted = []
        skipped = []
        for line_id, line in current.get("lines", {}).items():
            if line_ready(current, line):
                subprocess.run(["systemctl", "restart", f"cf-failover@{line_id}.service"], check=False)
                restarted.append(line_id)
            else:
                subprocess.run(["systemctl", "disable", "--now", f"cf-failover@{line_id}.service"], check=False)
                skipped.append(line_id)
        result = "已提交重启线路: " + (", ".join(restarted) if restarted else "无")
        if skipped:
            result += "\n配置不完整，已保持停用: " + ", ".join(skipped)
        bot.reply_to(message, result)

    @bot.message_handler(func=lambda message: True)
    def dynamic_command(message: Any) -> None:
        current = load_config()
        if not allowed(message, current):
            return
        text = str(getattr(message, "text", "")).strip()
        parsed = parse_dynamic_command(text)
        if parsed is None:
            return
        command, line_id = parsed
        if command == "switch":
            mode = "MAIN"
        elif command == "backup":
            mode = "BACKUP"
        elif command == "restart":
            line = line_for(current, line_id)
            if line is None:
                return
            if not line_ready(current, line):
                subprocess.run(["systemctl", "disable", "--now", f"cf-failover@{line_id}.service"], check=False)
                bot.reply_to(message, "线路配置不完整或无效，服务已保持停用")
                return
            result = subprocess.run(["systemctl", "restart", f"cf-failover@{line_id}.service"], capture_output=True, text=True, check=False)
            bot.reply_to(message, f"{line.get('name', line_id)} 已重启，返回码 {result.returncode}")
            return
        line = line_for(current, line_id)
        if line is None:
            bot.reply_to(message, "找不到该线路")
            return
        output = run_engine(line_id, "--switch" if mode == "MAIN" else "--backup")
        bot.reply_to(message, f"{line.get('name', line_id)}\n{output}")

    return bot


def main() -> int:
    if "--check-config" in sys.argv:
        load_config()
        print(f"配置可读取: {CONFIG_PATH}")
        return 0
    bot = build_bot()
    bot.infinity_polling(skip_pending=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
