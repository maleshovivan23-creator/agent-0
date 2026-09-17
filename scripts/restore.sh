#!/usr/bin/env bash
# Восстановление рабочего окружения: после перезапуска машины, обновления
# песочницы или переезда на другой сервер.
#
# Что живёт в git и возвращается само: код, публичные настройки оператора
# (data/operator.yaml — адреса кошельков и страна) и отчёты площадок.
# Что приходится создавать заново: .venv (окружение Python), .env (секреты) и
# data/agent.db (база наблюдений — она собирается заново первым же циклом).
#
# Использование: ./scripts/restore.sh
set -euo pipefail

cd "$(dirname "$0")/.."
echo "Каталог: $(pwd)"

if [ ! -d .venv ]; then
  echo "Создаю окружение Python..."
  python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements-dev.txt

mkdir -p data reports/inbox logs snapshots

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Создан .env из шаблона. Ключи площадок вписывает человек:"
  echo "  .venv/bin/python -m agent.main настройка"
fi
chmod 600 .env

echo
.venv/bin/python -m agent.main проверка || true

echo
echo "Дальше:"
echo "  .venv/bin/python -m agent.main автопилот                  # робот на переднем плане"
echo "  .venv/bin/python -m agent.main дашборд --host 0.0.0.0 --port 8000"
echo "  .venv/bin/python -m agent.main входящие                   # что человек делает руками"
