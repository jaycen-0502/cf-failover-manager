#!/usr/bin/env python3
"""用于本地演练 cf_manager TG 注入的最小示例，不是真实机器人。"""
import re

SYSTEMD_SERVICES = {
    "容灾集群 1 号": "cf-failover-1",
}

COMMAND_MAPPING = {
    "run_cf1": "/usr/local/bin/cf1.py",
}

COMMAND_RE = re.compile(r"^(switch|backup)_([1-5])$")
