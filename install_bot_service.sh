#!/usr/bin/env bash

set -e

SERVICE_NAME=uv-bot
WORKDIR="$(pwd)"
USER_NAME="$(whoami)"

echo "Создаю systemd-сервис для папки:"
echo "$WORKDIR"

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=UV Bot Service
After=network.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$WORKDIR
ExecStart=/usr/bin/env uv run bot.py
Restart=always
RestartSec=5

# чтобы не убивался при logout
KillSignal=SIGINT
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

echo "Перезагружаю systemd..."
sudo systemctl daemon-reload

echo "Включаю автозапуск..."
sudo systemctl enable $SERVICE_NAME

echo "Запускаю сервис..."
sudo systemctl start $SERVICE_NAME

echo "Готово."
echo "Статус:"
systemctl status $SERVICE_NAME --no-pager
