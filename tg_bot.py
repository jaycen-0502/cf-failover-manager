#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动态读取 config.json 的 Telegram 控制机器人。"""
from __future__ import annotations

import json
import os
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


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def line_for(config: dict[str, Any], line_id: str) -> dict[str, Any] | None:
    value = config.get("lines", {}).get(str(line_id))
    return value if isinstance(value, dict) else None


def allowed(message: Any, config: dict[str, Any]) -> bool:
    return str(getattr(getattr(message, "from_user", None), "id", "")) == str(config.get("global", {}).get("tg_admin_id", ""))


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
    for line_id, line in sorted(config.get("lines", {}).items(), key=lambda item: item[0]):
        state_file = Path("/tmp") / f"cf_failover_{line_id}_state.txt"
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
        for line_id in current.get("lines", {}):
            subprocess.run(["systemctl", "restart", f"cf-failover@{line_id}.service"], check=False)
        bot.reply_to(message, "已提交全部线路重启")

    @bot.message_handler(func=lambda message: True)
    def dynamic_command(message: Any) -> None:
        current = load_config()
        if not allowed(message, current):
            return
        text = str(getattr(message, "text", "")).strip()
        normalized = text.lstrip("/")
        if normalized.startswith("switch_"):
            line_id = normalized.removeprefix("switch_")
            mode = "MAIN"
        elif normalized.startswith("backup_"):
            line_id = normalized.removeprefix("backup_")
            mode = "BACKUP"
        elif normalized.startswith("restart_"):
            line_id = normalized.removeprefix("restart_")
            line = line_for(current, line_id)
            if line is None:
                return
            result = subprocess.run(["systemctl", "restart", f"cf-failover@{line_id}.service"], capture_output=True, text=True, check=False)
            bot.reply_to(message, f"{line.get('name', line_id)} 已重启，返回码 {result.returncode}")
            return
        else:
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
