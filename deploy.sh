#!/usr/bin/env bash
set -Eeuo pipefail

# 大白话：第一次运行就下载，之后运行就升级；最后用 cfm 进入中文菜单。
REPO_DIR="${CF_MANAGER_DIR:-$HOME/cf-failover-manager}"
REPO_URL="${CF_MANAGER_REPO:-git@github.com:jaycen-0502/cf-failover-manager.git}"
INSTALL_PATH="${CF_MANAGER_INSTALL_PATH:-/usr/local/bin/cf_manager}"

if [[ -d "$REPO_DIR/.git" ]]; then
    git -C "$REPO_DIR" pull --ff-only
else
    git clone "$REPO_URL" "$REPO_DIR"
fi

sudo install -o root -g root -m 700 "$REPO_DIR/cf_manager.py" "$INSTALL_PATH"
sudo ln -sfn "$INSTALL_PATH" /usr/local/bin/cfm
sudo python3 "$INSTALL_PATH" --self-test

echo "安装/升级完成。以后可以直接运行：sudo cfm"
exec sudo "$INSTALL_PATH"
