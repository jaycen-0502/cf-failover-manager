#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "请使用 root 运行：sudo bash install.sh" >&2
  exit 1
fi

APP_DIR="/opt/cf-failover"
SRC_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 python3-venv python3-pip ca-certificates
else
  echo "当前安装脚本只支持 Debian/Ubuntu（需要 apt-get）" >&2
  exit 1
fi

install -d -m 700 "$APP_DIR"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$SRC_DIR/requirements.txt"
install -o root -g root -m 700 "$SRC_DIR/failover_engine.py" "$SRC_DIR/menu.py" "$SRC_DIR/tg_bot.py" "$APP_DIR/"
install -o root -g root -m 644 "$SRC_DIR/systemd/cf-failover@.service" "$SRC_DIR/systemd/tg-bot.service" /etc/systemd/system/
if [[ ! -f "$APP_DIR/config.json" ]]; then
  install -o root -g root -m 600 "$SRC_DIR/config.example.json" "$APP_DIR/config.json"
  echo "已创建 $APP_DIR/config.json，请先配置 Token 和线路。"
fi
ln -sfn "$APP_DIR/menu.py" /usr/local/bin/cf-menu
ln -sfn "$APP_DIR/menu.py" /usr/local/bin/cfm
systemctl daemon-reload
echo "安装完成：编辑 $APP_DIR/config.json 后运行 cf-menu。"
