#!/usr/bin/env bash
# Установка AGENT-0 «под ключ» на свою машину или сервер.
# Использование: ./scripts/install.sh [каталог] [пользователь]
set -euo pipefail

TARGET="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
USER_NAME="${2:-$(id -un)}"

cd "$TARGET"
echo "Каталог: $TARGET (пользователь $USER_NAME)"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
mkdir -p data reports logs

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Создан .env — заполните GITHUB_TOKEN, ELIGIBILITY_COUNTRY и PAYOUT_WALLET"
fi

echo
echo "Проверка готовности:"
.venv/bin/python -m agent.main doctor || true

if command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" = "0" ]; then
  sed "s#%i#${USER_NAME}#; s#/opt/agent-0#${TARGET}#g" deploy/agent0-autopilot.service \
    > /etc/systemd/system/agent0-autopilot.service
  sed "s#/opt/agent-0#${TARGET}#g" deploy/agent0-dashboard.service \
    > /etc/systemd/system/agent0-dashboard.service
  systemctl daemon-reload
  systemctl enable --now agent0-autopilot agent0-dashboard
  echo "Службы установлены: systemctl status agent0-autopilot"
else
  echo "systemd не настроен (нужны права root). Варианты запуска:"
  echo "  .venv/bin/python -m agent.main autopilot        # робот на переднем плане"
  echo "  crontab deploy/crontab.example                  # по расписанию"
fi
