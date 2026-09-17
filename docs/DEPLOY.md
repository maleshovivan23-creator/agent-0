# Развёртывание: робот, который работает без вас

Три способа запустить AGENT-0 в автономном режиме и один способ мгновенно его остановить.

## Что именно автономно, а что нет

| Робот делает сам | Делает человек |
|---|---|
| Ищет задачи в трёх направлениях, каждые 5–60 минут | Публикует заявку в задаче (`inbox show <id>` → скопировать) |
| Проверяет конкуренцию и отсеивает занятые задачи | Отправляет текст квеста на площадке |
| Считает EV/час и распределяет время по направлениям | Делает саму работу |
| Пишет планы работ, черновики заявок и ответов | Подтверждает выплату (`payout-verify`) |
| Складывает готовое в очередь `inbox` | Заводит кошелёк/счёт для выплат |
| Пишет отчёт смены и heartbeat для мониторинга | Снимает стоп-кран, если понадобилось |

Публикация, отправка и подтверждение денег — **всегда человек**. Это не ограничение
техники, а осознанная граница: аккаунт, подпись и налоговая ответственность ваши.

## Вариант 1: systemd (рекомендуется для сервера)

```bash
git clone <repo> /opt/agent-0 && cd /opt/agent-0
sudo ./scripts/install.sh /opt/agent-0 "$USER"     # venv, зависимости, .env, doctor
sudo systemctl enable --now agent0-autopilot agent0-dashboard
systemctl status agent0-autopilot
journalctl -u agent0-autopilot -f                  # живой лог
curl -s localhost:8000/healthz | python -m json.tool
```

Юниты лежат в `deploy/agent0-autopilot.service` и `deploy/agent0-dashboard.service`:
`Restart=always`, ограничение памяти 1 ГБ, CPU 50%, запись только в `data/` и `reports/`.

## Вариант 2: cron (когда systemd нет)

```bash
crontab deploy/crontab.example    # проход каждые 15 минут днём, раз в 3 часа ночью
```

Плюс: робот не держит процесс. Минус: нет heartbeat, поэтому `autopilot --status`
покажет `idle` — это нормально для cron-режима.

## Вариант 3: Docker

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f autopilot
```

Данные и отчёты — на volume, обновление: `git pull && docker compose ... up -d --build`.

## Проверка, что робот жив

```bash
python -m agent.main autopilot --status     # состояние, интервал, последний проход
python -m agent.main doctor                 # готовность: блокеры и как починить
python -m agent.main shift --hours 24       # что сделано за сутки
curl -s localhost:8000/healthz              # для мониторинга: 200 = ок, 503 = проблема
```

`/healthz` возвращает `ok` / `degraded` / `stopped`, возраст последнего прохода,
состояние базы и причину остановки — этого достаточно для Uptime Kuma, Zabbix или
простого `curl` в cron.

## Стоп-кран

```bash
touch data/STOP                  # мгновенная остановка, файл виден в дашборде
rm -f data/STOP                  # снять
# или в .env: AUTOPILOT_ENABLED=false
```

Робот останавливается до следующего прохода, ничего не публикует и не тратит лимиты.
Очередь `inbox` и вся бухгалтерия сохраняются.

## Обновление и перенос

```bash
git pull && .venv/bin/pip install -q -r requirements.txt
sudo systemctl restart agent0-autopilot agent0-dashboard
```

Состояние живёт в `data/agent.db` (SQLite) и `reports/` — скопируйте их на новую
машину, и робот продолжит с того же места: очередь, часы, выплаты, heartbeat.
