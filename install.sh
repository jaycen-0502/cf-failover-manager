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
  DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-pip ca-certificates
else
  echo "未检测到 apt-get，请先手动安装 python3、pip3 和 ca-certificates" >&2
fi

python3 -m pip install --break-system-packages pyTelegramBotAPI 2>/dev/null || python3 -m pip install pyTelegramBotAPI
install -d -m 700 "$APP_DIR"
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
