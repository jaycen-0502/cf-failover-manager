#!/usr/bin/env bash
set -Eeuo pipefail

# 大白话：第一次运行就下载，之后运行就升级；最后用 cf-menu 进入 v2 中文菜单。
REPO_DIR="${CF_MANAGER_DIR:-$HOME/cf-failover-manager}"
REPO_URL="${CF_MANAGER_REPO:-https://github.com/jaycen-0502/cf-failover-manager.git}"
if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    SUDO=""
else
    SUDO="sudo"
fi

if [[ -d "$REPO_DIR/.git" ]]; then
    git -C "$REPO_DIR" pull --ff-only
else
    git clone "$REPO_URL" "$REPO_DIR"
fi

$SUDO bash "$REPO_DIR/install.sh"

echo "安装/升级完成。以后可以直接运行：sudo cf-menu"
exec $SUDO /usr/local/bin/cf-menu
