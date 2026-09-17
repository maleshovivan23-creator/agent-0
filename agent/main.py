"""AGENT-0 command line.

    python -m agent.main status                 # состояние фермы
    python -m agent.main next                   # что делать прямо сейчас
    python -m agent.main cycle                  # проход по воркерам (сбор + триаж)
    python -m agent.main queue                  # очередь возможностей по EV/час
    python -m agent.main triage <id>            # перепроверить конкретную задачу
    python -m agent.main plan <id>              # план работ (подагенты)
    python -m agent.main apply <id>             # текст заявки в задачу (для человека)
    python -m agent.main hours-add ...          # учёт затраченного времени
    python -m agent.main analytics              # ставка $/час и оценка конвейера
    python -m agent.main report                 # отчёт в reports/
    python -m agent.main eligibility --country DE
    python -m agent.main payout-add ... / payout-verify <id>
    python -m agent.main ledger | policy
    python -m agent.main loop --interval 900    # автономный режим
    python -m agent.main serve                  # дашборд на 0.0.0.0:8000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from agent import autopilot as autopilot_mod
from agent import doctor as doctor_mod
from agent import inbox as inbox_mod
from agent import eligibility, payouts, quests
from agent.config import get_env, load_environment, project_root
from agent.farm import next_actions, overview, run_cycle
from agent.ledger import (
    analytics,
    connect,
    get_opportunity,
    log_hours,
    record_payout,
    set_status,
    summary,
    top_opportunities,
    verify_payout,
)
from agent.subagents import build_brief

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def paint(text: str, color: str) -> str:
    return f"{color}{text}{RESET}" if sys.stdout.isatty() else text


def money(value: float) -> str:
    return f"${value:,.2f}"


# --------------------------------------------------------------------- status


def cmd_status(args: argparse.Namespace) -> int:
    data = overview()
    ledger_data = summary()
    stats = analytics()

    print(paint("AGENT-0 — ферма автономных воркеров", BOLD))
    print()
    print(f"Возможностей в базе: {ledger_data['opportunities_total']} "
          f"(в очереди: {ledger_data['opportunities_queued']}, отсеяно триажем: "
          f"{ledger_data['opportunities_dropped']})")
    print(f"Подтверждённый доход: {paint(money(stats['verified_usd']), GREEN)}  "
          f"| Заявлено: {money(stats['claimed_usd'])}")
    print(f"Затрачено часов: {stats['hours_logged']}  "
          f"| Эффективная ставка: "
          f"{paint(money(stats['effective_usd_per_hour']) + '/ч', GREEN if stats['effective_usd_per_hour'] > 0 else YELLOW)}")
    print(f"Оценка конвейера: {money(stats['pipeline_ev_usd'])} ожидаемой выручки "
          f"на {stats['pipeline_hours']}ч работы "
          f"({money(stats['pipeline_ev_per_hour'])}/ч) — это оценка, не гарантия")
    print()

    print(paint("Работает сейчас:", BOLD))
    for channel in data["channels"]:
        state = paint("ВКЛ", GREEN) if channel["allowed"] else paint("ВЫКЛ", RED)
        ready = "" if channel.get("ready", True) else paint(" (нужен scope.yaml)", YELLOW)
        print(f"  {state} {channel['title']}{ready}")
        print(f"      {DIM}{channel['capability']} · {channel['description']}{RESET}")

    print()
    directions = data.get("directions", {}).get("directions", [])
    if directions:
        print(paint("Три направления:", BOLD))
        for item in directions:
            print(f"  {item['share'] * 100:>3.0f}% времени · {item['title']} — {item['status']}")
        print()

    print(paint("Отключено политикой (и почему):", BOLD))
    for channel in data["disabled"]:
        print(f"  {paint('ВЫКЛ', RED)} {channel['title']} — {channel['reason'][:100]}...")
    print()
    print(f"Триаж: до {data['triage_budget']} задач за цикл. "
          f"Ставка времени оператора: {money(data['hourly_rate'])}/ч.")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    actions = next_actions(limit=args.limit)
    if not actions:
        print("Очередь пуста. Запустите: python -m agent.main cycle")
        return 0
    print(paint("Что делать сейчас — по убыванию ожидаемой ценности часа:", BOLD))
    print()
    for index, item in enumerate(actions, 1):
        reward = money(item["reward_usd"]) if item["reward_usd"] else "награда н/д"
        print(f"{index}. {paint(item['title'][:70], BOLD)}")
        print(f"   {reward} · плейбук {item['playbook']} · триаж {item['triage']} · "
              f"EV/час ~{money(item['ev_per_hour'])} · оценка {item['effort_hours']}ч")
        print(f"   {DIM}{item['why'][:150]}{RESET}")
        if item["url"]:
            print(f"   {item['url']}")
        print(f"   {DIM}первый шаг: python -m agent.main plan {item['id']}{RESET}")
        print()
    return 0


# ---------------------------------------------------------------------- cycle


def _print_channel_result(outcome: Dict[str, Any]) -> None:
    name = outcome.get("title", outcome.get("name"))
    if not outcome.get("allowed"):
        print(f"{paint('ЗАБЛОКИРОВАНО', RED)} {name}: {outcome.get('policy_reason', '')}")
        return
    if outcome.get("ready") is False:
        print(f"{paint('НЕ ГОТОВ', YELLOW)} {name}: {outcome.get('policy_reason', '')}")
        return
    if outcome.get("error"):
        print(f"{paint('ОШИБКА', RED)} {name}: {outcome['error']}")
    print(
        f"{paint('ГОТОВО', GREEN)} {name}: найдено {outcome.get('found', 0)}, "
        f"сохранено {outcome.get('kept', 0)}, отсеяно {outcome.get('dropped', 0)}, "
        f"проверено триажем {outcome.get('triaged', 0)}"
    )
    for item in outcome.get("top", [])[:5]:
        reward = money(item["reward_usd"]) if item["reward_usd"] else "н/д"
        verdict = item.get("triage_verdict") or "—"
        print(f"    [{item['score']:>6.2f}] {reward:>10}  {item['title'][:58]}")
        print(f"             {DIM}триаж: {verdict} · EV/час ~{money(item.get('ev_per_hour') or 0)}{RESET}")


def cmd_cycle(args: argparse.Namespace) -> int:
    channels = [args.channel] if args.channel else None
    result = run_cycle(channels=channels, limit=args.limit, do_triage=not args.no_triage)
    print()
    for outcome in result.channels:
        _print_channel_result(outcome)
        print()
    for note in result.notes:
        print(paint(f"примечание: {note}", YELLOW))
    print(
        f"Итого: найдено {result.found}, сохранено {result.kept}, "
        f"отсеяно как оплаченные {result.dropped}, триаж по {result.triaged}"
    )
    print(f"Ожидаемая выручка по топу этого цикла: {money(result.pipeline_ev_usd)}")
    if result.kept:
        print(f"Дальше: {paint('python -m agent.main next', BOLD)}")
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    rows = top_opportunities(limit=args.limit, channel=args.channel)
    if not rows:
        print("Очередь пуста. Запустите: python -m agent.main cycle")
        return 0
    print(f"{'score':>7}  {'награда':>10}  {'EV/час':>8}  {'канал':<16}  задача")
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        reward = money(row["reward_usd"]) if row["reward_usd"] else "н/д"
        ev_hour = payload.get("ev_per_hour") or 0
        print(f"{row['score']:>7.2f}  {reward:>10}  {money(ev_hour):>8}  {row['channel']:<16}  {row['title'][:52]}")
        print(f"         {DIM}{row['id']} · плейбук {payload.get('playbook', 'н/д')} · "
              f"триаж {payload.get('triage_verdict', 'нет')}{RESET}")
    return 0


# ---------------------------------------------------------------------- triage


def cmd_triage(args: argparse.Namespace) -> int:
    opportunity = get_opportunity(args.opportunity_id)
    if not opportunity:
        print(f"Возможность {args.opportunity_id} не найдена.")
        return 1
    payload = json.loads(opportunity["payload"] or "{}")
    triage = payload.get("triage")

    if not triage or args.refresh:
        from agent.triage import TriageClient

        repo = opportunity["repo"]
        number = int(str(opportunity["id"]).rsplit("#", 1)[-1]) if "#" in str(opportunity["id"]) else 0
        if not repo or not number:
            print("Для этой возможности триаж неприменим (не GitHub issue).")
            return 1
        verdict = TriageClient(token=get_env("GITHUB_TOKEN", "") or "").triage(
            opportunity_id=str(opportunity["id"]),
            repo=repo,
            number=number,
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
        )
        triage = verdict.as_dict()

    print(paint(f"Триаж: {opportunity['title']}", BOLD))
    print(f"  Вердикт: {triage['verdict']}  |  шанс успеха ~{triage['probability']:.1%}")
    print(f"  Заявок /attempt: {triage['attempts']}  |  открытых PR: {triage['open_prs']}")
    if triage.get("verified_amount"):
        print(f"  Подтверждённая сумма от бота: {money(triage['verified_amount'])}")
    if triage.get("funder"):
        print(f"  Заказчик (подтверждён платформой): {triage['funder']}")
    print(f"  Обновлено: {triage['stale_days']} дн. назад, создано {triage['age_days']} дн. назад")
    for note in triage.get("notes", []):
        print(f"  - {note}")
    if triage["verdict"] in ("paid", "closed", "assigned", "contested"):
        print(paint("\nВывод: не тратить время на эту задачу.", RED))
    elif triage["verdict"] == "contested":
        print(paint("\nВывод: высокая конкуренция — браться только при явном преимуществе.", YELLOW))
    else:
        print(paint("\nВывод: задача выглядит свободной.", GREEN))
    return 0


# ---------------------------------------------------------------- plan / apply


def cmd_plan(args: argparse.Namespace) -> int:
    opportunity = get_opportunity(args.opportunity_id)
    if not opportunity:
        print(f"Возможность {args.opportunity_id} не найдена.")
        return 1
    payload = json.loads(opportunity["payload"] or "{}")
    opportunity["payload"] = payload

    print(paint("Собираю план работ (подагенты)...", DIM))
    brief = build_brief(opportunity)
    markdown = brief.to_markdown()

    reports = project_root() / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    safe_id = str(opportunity["id"]).replace("/", "_").replace(":", "-").replace("#", "-")
    path = reports / f"brief-{safe_id}.md"
    path.write_text(markdown, encoding="utf-8")

    print()
    print(markdown)
    print(paint(f"Сохранено: {path.relative_to(project_root())}", DIM))

    if reminder_needed(payload):
        print()
        print(paint("Напоминание: сначала проверьте триаж — "
                    f"python -m agent.main triage {opportunity['id']}", YELLOW))
    return 0


def reminder_needed(payload: Dict[str, Any]) -> bool:
    return payload.get("triage_verdict") in ("paid", "assigned", "contested", None)


def cmd_apply(args: argparse.Namespace) -> int:
    """Generate the claim/proposal text. Posting stays a human action."""
    opportunity = get_opportunity(args.opportunity_id)
    if not opportunity:
        print(f"Возможность {args.opportunity_id} не найдена.")
        return 1
    payload = json.loads(opportunity["payload"] or "{}")

    country = args.country or get_env("ELIGIBILITY_COUNTRY", "") or ""
    if not country:
        print(paint("Нужна проверка выплат: укажите страну, например "
                    "--country DE (это не формальность: Stripe не платит в часть стран).", YELLOW))
        return 2

    rail = args.rail or eligibility.recommend_rail(country)
    verdict = eligibility.preflight(country, rail)
    print(paint(f"Выплата: {verdict.country_name} · {eligibility.RAILS[rail]['title']}", BOLD))
    print(f"  Статус: {verdict.status} — {verdict.reason}")
    if not verdict.usable:
        print()
        print(paint("Эта задача вам не подходит по платёжному каналу. Альтернативы:", RED))
        for alternative in verdict.alternatives:
            print(f"  - {alternative}")
        print(f"\nИсточник: {verdict.source}")
        return 2
    print(f"  Источник: {verdict.source}")
    print()

    verdict_triage = payload.get("triage_verdict")
    if verdict_triage in ("paid", "closed", "assigned"):
        print(paint(f"Триаж: {verdict_triage} — заявку подавать не стоит.", RED))
        return 2
    if verdict_triage == "contested":
        attempts = payload.get("attempts", "?")
        print(paint(f"Внимание: конкуренция высокая (заявок: {attempts}). "
                    "Заявка имеет смысл только с сильным планом.", YELLOW))
        print()

    number = str(opportunity["id"]).rsplit("#", 1)[-1] if "#" in str(opportunity["id"]) else "N"
    playbook_key = payload.get("playbook") or "generic"
    from agent import playbooks

    playbook = playbooks.get(playbook_key)

    print(paint("Текст для комментария в задаче (публикуете вы, не агент):", BOLD))
    print()
    print(f"/attempt #{number}")
    print()
    print(f"**План:** {playbook.title}")
    for index, step in enumerate(playbook.steps[:4], 1):
        print(f"{index}. {step}")
    print()
    print("**Оценка сроков:** [часы, которые вы готовы назвать]")
    print("**Готовность:** [что уже проверено локально]")
    print()
    print(paint("Что делает человек:", BOLD))
    print("  1. Проверить срок выполнения и что задача не закрыта.")
    print("  2. Опубликовать комментарий выше от своего имени.")
    print("  3. Отметить статус: python -m agent.main status-set "
          f"{opportunity['id']} working")
    print("  4. Сделать работу, затем план: python -m agent.main plan "
          f"{opportunity['id']}")
    set_status(str(opportunity["id"]), "proposed")
    return 0


def cmd_status_set(args: argparse.Namespace) -> int:
    if set_status(args.opportunity_id, args.status):
        if args.status in ("proposed", "working", "done"):
            inbox_mod.mark_published(args.opportunity_id)
        print(paint(f"Статус {args.opportunity_id} -> {args.status}", GREEN))
        return 0
    print(f"Возможность {args.opportunity_id} не найдена.")
    return 1


# ------------------------------------------------------------------- analytics


def cmd_hours_add(args: argparse.Namespace) -> int:
    log_hours(args.channel, args.hours, args.id or "", args.note or "")
    stats = analytics()
    print(f"Записано {args.hours}ч по каналу {args.channel}.")
    print(f"Всего часов: {stats['hours_logged']} | "
          f"эффективная ставка: {money(stats['effective_usd_per_hour'])}/ч")
    return 0


def cmd_analytics(args: argparse.Namespace) -> int:
    stats = analytics()
    print(paint("Экономика фермы", BOLD))
    print(f"  Подтверждённый доход: {money(stats['verified_usd'])}")
    print(f"  Заявлено (ждёт подтверждения): {money(stats['claimed_usd'])}")
    print(f"  Часов залогировано: {stats['hours_logged']}")
    rate = stats["effective_usd_per_hour"]
    color = GREEN if rate >= 10 else (YELLOW if rate > 0 else RED)
    print(f"  Эффективная ставка: {paint(money(rate) + '/ч', color)}")
    print()
    print(f"  Конвейер: {money(stats['pipeline_ev_usd'])} ожидаемой выручки "
          f"на {stats['pipeline_hours']}ч ({money(stats['pipeline_ev_per_hour'])}/ч)")
    print(f"  {DIM}{stats['estimate_note']}{RESET}")
    print()
    if stats["by_channel"]:
        print(paint("По каналам (очередь):", BOLD))
        for row in stats["by_channel"]:
            print(f"  {row['channel']:<16} задач {row['queued']:>3} · "
                  f"заявленные награды {money(row['advertised_usd']):>12} · "
                  f"средний score {row['avg_score']:>6.2f}")
    print()
    print(paint("Топ конвейера с оценкой ценности:", BOLD))
    for item in stats["pipeline"][:8]:
        print(f"  {money(item['expected_value_usd']):>10} ожидаемо · "
              f"{item['effort_hours']:>5}ч · {item['title'][:52]}")
    print()
    if rate < 5 and stats["hours_logged"] > 0:
        print(paint("Ставка низкая. Что делать: брать только задачи с триажем 'ready', "
                    "перестать логировать время на задачи ниже $5/ч и поднимать "
                    "потолок через аудит-контесты (канал audit_contests).", YELLOW))
    return 0


def cmd_eligibility(args: argparse.Namespace) -> int:
    print(paint("Платёжные каналы", BOLD))
    for rail in eligibility.summary_table():
        print(f"  {rail['rail']:<16} {rail['title']}")
        print(f"    {DIM}{rail['note']} ({rail['source']}){RESET}")
    print()
    for rail in {args.rail} if args.rail else {"stripe_connect"}:
        verdict = eligibility.assess(args.country, rail)
        print(paint(f"Ваша страна: {verdict.country_name} · канал {rail}", BOLD))
        color = GREEN if verdict.usable else RED
        print(f"  Статус: {paint(verdict.status, color)}")
        print(f"  {verdict.reason}")
        for alternative in verdict.alternatives:
            print(f"  - {alternative}")
        print(f"  Источник: {verdict.source}")
    print()
    print(f"{DIM}Проверено: {eligibility.VERIFIED_ON}. Списки стран меняются — "
          f"сверяйтесь с официальной страницей платформы перед первой работой.{RESET}")
    return 0


# ---------------------------------------------------------------------- report


def cmd_report(args: argparse.Namespace) -> int:
    stats = analytics()
    ledger_data = summary()
    data = overview()
    actions = next_actions(limit=10)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"# AGENT-0 — отчёт на {stamp}",
        "",
        "## Деньги",
        "",
        f"- Подтверждённый доход: {money(stats['verified_usd'])}",
        f"- Заявлено, не подтверждено: {money(stats['claimed_usd'])}",
        f"- Часов залогировано: {stats['hours_logged']}",
        f"- Эффективная ставка: {money(stats['effective_usd_per_hour'])}/ч",
        f"- Оценка конвейера: {money(stats['pipeline_ev_usd'])} ожидаемой выручки "
        f"на {stats['pipeline_hours']}ч ({money(stats['pipeline_ev_per_hour'])}/ч)",
        "",
        f"> {stats['estimate_note']}",
        "",
        "## Очередь: что делать",
        "",
    ]
    for index, item in enumerate(actions, 1):
        reward = money(item["reward_usd"]) if item["reward_usd"] else "н/д"
        lines.append(f"{index}. **{item['title']}** — {reward}, триаж `{item['triage']}`, "
                     f"EV/час ~{money(item['ev_per_hour'])}, оценка {item['effort_hours']}ч")
        if item["url"]:
            lines.append(f"   - {item['url']}")
        lines.append(f"   - {item['why']}")
    lines += ["", "## Возможности в базе", ""]
    for row in ledger_data["channels"]:
        lines.append(f"- `{row['channel']}`: {row['n']} задач, "
                     f"суммарно заявленных наград {money(row['reward'] or 0)}")
    lines += ["", "## Что ферма не делает", ""]
    for channel in data["disabled"]:
        lines.append(f"- **{channel['title']}** — {channel['reason']}")
    lines += ["", "## Выплаты", ""]
    if ledger_data["payouts"]:
        for payout in ledger_data["payouts"]:
            state = "подтверждено" if payout["verified"] else "заявка"
            lines.append(f"- #{payout['id']} {payout['channel']} "
                         f"{money(payout['amount'])} — {state}"
                         + (f" ({payout['evidence']})" if payout["evidence"] else ""))
    else:
        lines.append("- пока пусто")

    reports = project_root() / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"report-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print()
    print(paint(f"Сохранено: {path.relative_to(project_root())}", DIM))
    return 0


# ----------------------------------------------------------------------- infra


def cmd_loop(args: argparse.Namespace) -> int:
    channels = [args.channel] if args.channel else None
    interval = max(60, args.interval)
    print(f"Автономный режим: цикл каждые {interval}с. Ctrl+C — остановка.")
    while True:
        result = run_cycle(channels=channels, limit=args.limit, do_triage=not args.no_triage)
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] найдено {result.found}, сохранено {result.kept}, "
              f"отсеяно {result.dropped}, EV {money(result.pipeline_ev_usd)}")

        from agent.telegram import TelegramNotifier

        notifier = TelegramNotifier()
        if notifier.enabled() and result.kept:
            lines = [f"AGENT-0: +{result.kept} новых возможностей "
                     f"(EV {money(result.pipeline_ev_usd)})"]
            for outcome in result.channels:
                for item in outcome.get("top", [])[:3]:
                    reward = money(item["reward_usd"]) if item["reward_usd"] else "н/д"
                    lines.append(f"[{reward}] {item['title'][:70]} "
                                 f"(EV/час {money(item.get('ev_per_hour') or 0)})")
            notifier.send("\n".join(lines))
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("Остановлено.")
            return 0


def cmd_payout_add(args: argparse.Namespace) -> int:
    payout_id = record_payout(
        channel=args.channel,
        amount=args.amount,
        currency=args.currency,
        evidence=args.evidence or "",
        note=args.note or "",
    )
    print(f"Записано как заявка #{payout_id} (не подтверждено).")
    print(f"Подтвердить: python -m agent.main payout-verify {payout_id}")
    return 0


def cmd_payout_verify(args: argparse.Namespace) -> int:
    if verify_payout(args.payout_id):
        print(paint(f"Выплата #{args.payout_id} подтверждена и учтена как доход.", GREEN))
        return 0
    print(f"Выплата #{args.payout_id} не найдена.")
    return 1


def cmd_ledger(args: argparse.Namespace) -> int:
    data = summary()
    print(f"Возможности: {data['opportunities_total']} "
          f"(в очереди {data['opportunities_queued']}, отсеяно {data['opportunities_dropped']})")
    print(f"Подтверждено: {money(data['verified_usd'])} | Заявлено: {money(data['claimed_usd'])}")
    print()
    print(paint("Выплаты:", BOLD))
    if not data["payouts"]:
        print("  пока пусто — это честнее, чем нарисованные цифры")
    for row in data["payouts"]:
        mark = paint("подтверждено", GREEN) if row["verified"] else paint("заявка", YELLOW)
        print(f"  #{row['id']} {row['channel']} {money(row['amount'])} {row['currency']} — {mark}")
        if row["evidence"]:
            print(f"      доказательство: {row['evidence']}")
    print()
    print(paint("Отказы (записи о том, что ферма не стала делать):", BOLD))
    for block in data["policy_blocks"][:10]:
        print(f"  - {block['channel']}: {block['reason'][:110]}")
    return 0


def cmd_policy(args: argparse.Namespace) -> int:
    data = overview()
    print(paint("Что ферма делает:", BOLD))
    for item in data["policy"]["allowed"]:
        reqs = []
        if item["requires_human"]:
            reqs.append("подтверждение человека")
        if item["requires_authorization"]:
            reqs.append("файл авторизации")
        suffix = f" [{', '.join(reqs)}]" if reqs else ""
        print(f"  + {item['label']}{suffix}")
        if item["note"]:
            print(f"    {DIM}{item['note']}{RESET}")

    print()
    print(paint("Что ферма не делает и почему:", BOLD))
    for item in data["policy"]["denied"]:
        print(f"  - {item['label']}")
        print(f"    {DIM}почему не работает: {item['why_it_fails']}{RESET}")
        print(f"    {DIM}риск: {item['risk']}{RESET}")
        print(f"    {DIM}вместо этого: {item['instead']}{RESET}")

    print()
    print(paint("Ваш запрос — разбор по пунктам:", BOLD))
    for item in data["requested"]:
        color = GREEN if item["status"] == "allowed" else (
            YELLOW if "human" in item["status"] else RED
        )
        print(f"  {paint(item['status'].upper(), color):<28} {item['request']}")
        print(f"    {DIM}{item['comment']}{RESET}")
    return 0


def cmd_directions(args: argparse.Namespace) -> int:
    from agent.directions import portfolio

    data = portfolio()
    print(paint("Три направления — работают параллельно", BOLD))
    print()
    for item in data["directions"]:
        print(f"{paint(item['title'], BOLD)}  ·  доля времени ~{item['share'] * 100:.0f}%")
        print(f"  зачем: {item['promise']}")
        print(f"  потолок: {item['ceiling']}  ·  вход: {item['entry_cost']}")
        print(f"  выплата: {item['payout_rail']}")
        print(f"  сейчас: {item['status']}")
        print(f"  {DIM}следующий шаг: {item['next_step']}{RESET}")
        print()

    planned = data["planned"]
    print(paint("Итоги по всем направлениям:", BOLD))
    print(f"  Подтверждённый доход {money(planned['verified_usd'])} · "
          f"часов {planned['hours_logged']} · "
          f"ставка {money(planned['effective_usd_per_hour'])}/ч")
    print(f"  Оценка конвейера: {money(planned['pipeline_ev_usd'])}")
    print()
    print(paint("Что делать дальше:", BOLD))
    for line in data["advice"]:
        print(f"  • {line}")
    return 0


def cmd_payout_rails(args: argparse.Namespace) -> int:
    assessment = payouts.recommend(args.country)
    print(paint(f"Как получить деньги — {args.country.upper()}", BOLD))
    print()

    if assessment.blocked:
        print(paint("Не работает:", RED))
        for key in assessment.blocked:
            rail = payouts.rail(key)
            if rail:
                print(f"  ✗ {rail.title} — {rail.summary.splitlines()[0]}")
        print()

    print(paint("Рабочие каналы (по порядку предпочтения):", BOLD))
    for key in assessment.recommended:
        rail = payouts.rail(key)
        if not rail:
            continue
        print()
        print(f"  {paint(rail.title, GREEN)}")
        print(f"    {rail.summary}")
        print(f"    Требуется: {', '.join(rail.requires) or 'ничего особенного'}")
        print(f"    Стоимость: {rail.costs}")
        for step in rail.steps:
            print(f"      · {step}")
        for risk in rail.breaks_when:
            print(f"      {DIM}ломается, если: {risk}{RESET}")
        print(f"      {DIM}источник: {rail.source}{RESET}")

    if assessment.notes:
        print()
        print(paint("Важные детали для вашей страны:", BOLD))
        for note in assessment.notes:
            print(f"  • {note}")

    print()
    print(paint("Чек-лист до начала работы:", BOLD))
    for item in payouts.checklist(args.country):
        print(f"  ☐ {item}")
    print()
    print(f"{DIM}Проверено: {payouts.VERIFIED_ON}. Это не юридическая и не налоговая "
          f"консультация — уточняйте у местного специалиста.{RESET}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    data = doctor_mod.readiness()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if data["can_start"] else 1

    print(paint("Проверка готовности: что нужно, чтобы ферма начала зарабатывать", BOLD))
    print()
    marks = {"ok": paint("ок ", GREEN), "warn": paint("!  ", YELLOW), "block": paint("СТОП", RED)}
    for check in data["checks"]:
        print(f"  [{marks[check['status']]}] {check['title']}: {check['detail']}")
        if check["status"] != "ok" and check["impact"]:
            print(f"         зачем: {check['impact']}")

    print()
    if data["next_actions"]:
        print(paint("Что мешает и как починить — по порядку:", BOLD))
        for index, item in enumerate(data["next_actions"], 1):
            level = paint("блокер", RED) if item["required"] and item["status"] != "ok" else paint("важно", YELLOW)
            print(f"  {index}. [{level}] {item['title']}: {item['fix']}")
    print()
    print(paint(data["verdict"], BOLD if data["can_start"] else YELLOW))
    print()
    print(paint("Путь к первой выплате:", BOLD))
    for step in data["start_plan"]:
        print(f"  {step}")
    return 0 if data["can_start"] else 1


def cmd_quest(args: argparse.Namespace) -> int:
    opportunity = get_opportunity(args.opportunity_id)
    if not opportunity:
        print(f"Возможность {args.opportunity_id} не найдена.")
        return 1
    payload = json.loads(opportunity["payload"] or "{}")
    opportunity["payload"] = payload

    if payload.get("platform") is None:
        print(paint("Это не квест площадки — черновик всё равно собран, "
                    "но проверьте формат задания на площадке.", YELLOW))

    print(paint("Готовлю черновик заявки (подагенты)...", DIM))
    path = quests.save_submission(opportunity)
    result = quests.submission_markdown(opportunity)

    print()
    print(result["markdown"])
    print()
    if result["words"] < quests.WORD_MIN:
        print(paint(f"Черновик {result['words']} слов — короче нормы {quests.WORD_MIN}–"
                    f"{quests.WORD_MAX}. Допишите содержательную часть, объём считается "
                    f"проверяющим.", YELLOW))
    print(paint(f"Сохранено: {path.relative_to(project_root())}", DIM))
    print(paint("Отправка и ключ площадки — вашими руками: ферма не публикует текст "
                "от вашего имени.", DIM))
    return 0


def cmd_autopilot(args: argparse.Namespace) -> int:
    if args.status:
        data = autopilot_mod.status()
        print(paint("Автопилот:", BOLD), data["state"])
        print(f"  интервал: {data['interval']}с (диапазон {data['min_interval']}–{data['max_interval']}с)")
        print(f"  проходов: {data['ticks']} · подготовлено артефактов: {data['prepared_total']} · "
              f"в очереди к публикации: {data['inbox_ready']}")
        if data["last_tick"]:
            print(f"  последний проход: {data['last_tick']} "
                  f"({data['last_tick_age_s']}с назад)")
        if data["stop_reason"]:
            print(paint(f"  остановлен: {data['stop_reason']}", YELLOW))
        return 0

    if autopilot_mod.stop_reason():
        print(paint(f"Автопилот остановлен: {autopilot_mod.stop_reason()}", YELLOW))
        print(f"Снять стоп: rm -f data/{autopilot_mod.KILL_SWITCH_NAME} "
              f"и AUTOPILOT_ENABLED=true в .env")
        return 0

    def report(result: autopilot_mod.TickResult) -> None:
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] найдено {result.found}, сохранено {result.kept}, "
              f"триаж {result.triaged}, подготовлено {len(result.prepared)}, "
              f"EV ${result.pipeline_ev_usd:,.0f}, следующий проход через "
              f"{result.next_interval / 60:.0f} мин")
        for item in result.prepared:
            print(f"    готово: {item['kind']} — {item['title'][:60]} "
                  f"({item['path']})")
        for note in result.skipped[:3]:
            print(f"    пропущено: {note}")

    if args.once:
        report(autopilot_mod.tick(args.prepare))
        print()
        print(paint("Артефакты ждут одного действия человека: python -m agent.main inbox", DIM))
        return 0

    print(paint("Автопилот запущен. Ctrl+C — остановка, "
                f"`touch data/{autopilot_mod.KILL_SWITCH_NAME}` — мгновенный стоп.", BOLD))
    print(paint("Робот не публикует заявки, не отправляет квесты и не подтверждает выплаты.", DIM))
    return autopilot_mod.run(ticks=args.ticks, on_tick=report)


def cmd_inbox(args: argparse.Namespace) -> int:
    if args.show:
        item = inbox_mod.get(args.show)
        if not item:
            print(f"Элемент #{args.show} не найден.")
            return 1
        print(paint(f"#{item['id']} [{item['kind_title']}] {item['title']}", BOLD))
        print(f"  награда/сводка: {item['summary']}")
        print(f"  действие человека: {item['action']}")
        print(f"  файл: {item['path']}")
        print()
        print(inbox_mod.text(args.show))
        return 0

    items = inbox_mod.recent(args.limit) if args.all else inbox_mod.pending(args.limit)
    counts = inbox_mod.counts()
    print(paint("Очередь к публикации (робот уже всё подготовил):", BOLD),
          f"готово {counts['ready']} · отправлено {counts['published']} · пропущено {counts['skipped']}")
    print()
    if not items:
        print("  пусто — запустите: python -m agent.main autopilot --once")
        return 0
    for item in items:
        mark = {"ready": paint("готово", GREEN), "published": paint("отправлено", DIM),
                "skipped": paint("пропущено", YELLOW)}.get(item["status"], item["status"])
        print(f"  #{item['id']} [{mark}] {item['kind_title']}: {item['title'][:64]}")
        print(f"      {item['summary']} · {item['action']}")
    print()
    print(paint("Показать текст: python -m agent.main inbox show <id>", DIM))
    print(paint("Отметить: python -m agent.main inbox-done <id> | inbox-skip <id>", DIM))
    return 0


def cmd_inbox_resolve(args: argparse.Namespace, status: str) -> int:
    if inbox_mod.resolve(args.item_id, status):
        print(paint(f"#{args.item_id} -> {status}", GREEN))
        return 0
    print(f"#{args.item_id} не найден или уже закрыт.")
    return 1


def cmd_shift(args: argparse.Namespace) -> int:
    text = autopilot_mod.shift_report(args.hours)
    print(text)
    if args.save:
        reports = project_root() / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        path = reports / f"shift-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.md"
        path.write_text(text + "\n", encoding="utf-8")
        print()
        print(paint(f"Сохранено: {path.relative_to(project_root())}", DIM))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from agent.dashboard import serve

    serve(host=args.host, port=args.port)
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    data = overview()
    print(f"Модель для планов: {data.get('llm', 'см. agent.llm')}")
    conn = connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM opportunities").fetchone()
        print(f"Всего возможностей: {row['n']}")
    finally:
        conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent.main",
        description="AGENT-0: автономная ферма воркеров для заработка без вложений",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="состояние фермы и деньги")

    next_cmd = sub.add_parser("next", help="что делать прямо сейчас")
    next_cmd.add_argument("--limit", type=int, default=5)

    cycle = sub.add_parser("cycle", help="проход по воркерам")
    cycle.add_argument("--channel", default=None)
    cycle.add_argument("--limit", type=int, default=25)
    cycle.add_argument("--no-triage", action="store_true", help="без проверки конкуренции")

    queue = sub.add_parser("queue", help="очередь возможностей")
    queue.add_argument("--limit", type=int, default=20)
    queue.add_argument("--channel", default=None)

    triage = sub.add_parser("triage", help="проверить, свободна ли задача")
    triage.add_argument("opportunity_id")
    triage.add_argument("--refresh", action="store_true")

    plan = sub.add_parser("plan", help="план работ через подагентов")
    plan.add_argument("opportunity_id")

    apply_cmd = sub.add_parser("apply", help="текст заявки (с проверкой выплаты)")
    apply_cmd.add_argument("opportunity_id")
    apply_cmd.add_argument("--country", default=None)
    apply_cmd.add_argument("--rail", default=None, choices=list(eligibility.RAILS.keys()))

    status_set = sub.add_parser("status-set", help="перевести задачу в другой статус")
    status_set.add_argument("opportunity_id")
    status_set.add_argument("status", choices=["queued", "proposed", "working", "submitted", "paid", "dropped"])

    hours = sub.add_parser("hours-add", help="записать затраченное время")
    hours.add_argument("--channel", required=True)
    hours.add_argument("--hours", type=float, required=True)
    hours.add_argument("--id", default=None)
    hours.add_argument("--note", default=None)

    sub.add_parser("analytics", help="ставка $/час и оценка конвейера")

    eligibility_cmd = sub.add_parser("eligibility", help="проверка платёжного канала")
    eligibility_cmd.add_argument("--country", required=True)
    eligibility_cmd.add_argument("--rail", default=None, choices=list(eligibility.RAILS.keys()))

    sub.add_parser("report", help="отчёт в reports/")

    sub.add_parser("directions", help="три направления: состояние и распределение времени")

    payout_rails = sub.add_parser("payout-rails", help="как получить деньги в вашей стране")
    payout_rails.add_argument("--country", required=True)

    loop = sub.add_parser("loop", help="автономный режим")
    loop.add_argument("--channel", default=None)
    loop.add_argument("--limit", type=int, default=25)
    loop.add_argument("--interval", type=int, default=900)
    loop.add_argument("--no-triage", action="store_true")

    payout_add = sub.add_parser("payout-add", help="записать заявку на выплату")
    payout_add.add_argument("--channel", required=True)
    payout_add.add_argument("--amount", type=float, required=True)
    payout_add.add_argument("--currency", default="USD")
    payout_add.add_argument("--evidence", default="")
    payout_add.add_argument("--note", default="")

    payout_verify = sub.add_parser("payout-verify", help="подтвердить выплату")
    payout_verify.add_argument("payout_id", type=int)

    sub.add_parser("ledger", help="бухгалтерия")
    sub.add_parser("policy", help="что разрешено, что запрещено")
    sub.add_parser("whoami", help="техническая сводка")

    auto = sub.add_parser("autopilot", help="автономный режим: работает без вас")
    auto.add_argument("--once", action="store_true", help="один проход и выход")
    auto.add_argument("--ticks", type=int, default=None, help="ограничить число проходов")
    auto.add_argument("--prepare", type=int, default=None, help="сколько артефактов готовить за проход")
    auto.add_argument("--status", action="store_true", help="состояние робота без запуска")

    inbox_cmd = sub.add_parser("inbox", help="очередь готового к публикации")
    inbox_cmd.add_argument("show", nargs="?", type=int, default=None, help="показать текст элемента")
    inbox_cmd.add_argument("--all", action="store_true", help="включая отработанные")
    inbox_cmd.add_argument("--limit", type=int, default=20)

    done = sub.add_parser("inbox-done", help="отметить элемент отправленным")
    done.add_argument("item_id", type=int)
    skip = sub.add_parser("inbox-skip", help="отметить элемент ненужным")
    skip.add_argument("item_id", type=int)

    shift = sub.add_parser("shift", help="отчёт: что робот сделал без вас")
    shift.add_argument("--hours", type=float, default=24.0)
    shift.add_argument("--save", action="store_true")

    doctor_cmd = sub.add_parser("doctor", help="что нужно, чтобы начать зарабатывать")
    doctor_cmd.add_argument("--json", action="store_true")

    quest = sub.add_parser("quest", help="черновик заявки на квест площадки")
    quest.add_argument("opportunity_id")

    serve = sub.add_parser("serve", help="веб-дашборд")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)

    return parser


HANDLERS = {
    "status": cmd_status,
    "next": cmd_next,
    "cycle": cmd_cycle,
    "queue": cmd_queue,
    "triage": cmd_triage,
    "plan": cmd_plan,
    "apply": cmd_apply,
    "status-set": cmd_status_set,
    "hours-add": cmd_hours_add,
    "analytics": cmd_analytics,
    "eligibility": cmd_eligibility,
    "report": cmd_report,
    "directions": cmd_directions,
    "payout-rails": cmd_payout_rails,
    "loop": cmd_loop,
    "payout-add": cmd_payout_add,
    "payout-verify": cmd_payout_verify,
    "ledger": cmd_ledger,
    "policy": cmd_policy,
    "whoami": cmd_whoami,
    "autopilot": cmd_autopilot,
    "inbox": cmd_inbox,
    "inbox-done": lambda args: cmd_inbox_resolve(args, "published"),
    "inbox-skip": lambda args: cmd_inbox_resolve(args, "skipped"),
    "shift": cmd_shift,
    "doctor": cmd_doctor,
    "quest": cmd_quest,
    "serve": cmd_serve,
}


def main(argv: List[str] | None = None) -> int:
    load_environment()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        return cmd_status(args)
    return HANDLERS[args.command](args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nОстановлено.")
