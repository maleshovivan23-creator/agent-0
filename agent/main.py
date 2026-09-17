"""AGENT-0 command line.

    python -m agent.main проверка               # что нужно, чтобы начать зарабатывать
    python -m agent.main автопилот              # робот: сбор, триаж, черновики, слежение
    python -m agent.main состояние              # состояние фермы
    python -m agent.main дальше                 # что делать прямо сейчас
    python -m agent.main цикл                   # разовый проход по воркерам
    python -m agent.main очередь                # очередь возможностей по EV/час
    python -m agent.main конкуренция <id>       # перепроверить конкретную задачу
    python -m agent.main досье <id>             # где править и что запускать
    python -m agent.main план <id>              # план работ (подагенты)
    python -m agent.main заявка <id>            # текст заявки в задачу (для человека)
    python -m agent.main часы ...               # учёт затраченного времени
    python -m agent.main аналитика              # ставка $/час и оценка конвейера
    python -m agent.main контесты               # активные контесты: пул и дедлайн
    python -m agent.main слежение               # не отдали ли задачу, пока вы работаете
    python -m agent.main дозор                  # что сломалось, пока вас не было
    python -m agent.main отчёт                  # отчёт в reports/
    python -m agent.main проверка-страны --country DE
    python -m agent.main выплата-запись ... / выплата-подтвердить <id>
    python -m agent.main бухгалтерия | правила
    python -m agent.main автоповтор --interval 900   # автономный режим (старый)
    python -m agent.main дашборд                # веб-дашборд на 0.0.0.0:8000

Английские имена команд продолжают работать: на них ссылаются службы и cron.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agent import autopilot as autopilot_mod
from agent import dossier as dossier_mod, followup as followup_mod
from agent import learning, setupenv, watchdog
from agent import doctor as doctor_mod
from agent import inbox as inbox_mod
from agent import eligibility, hansa, payouts, quests, wallet
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
        age = item.get("age_hours")
        badge = ""
        if age is not None:
            if age <= 6:
                badge = paint(f"  свежая ({age:.0f}ч)", GREEN)
            elif age <= 24:
                badge = paint(f"  {age:.0f}ч", YELLOW)
            else:
                badge = paint(f"  {age / 24:.0f} дн. в очереди", RED)
        print(f"{index}. {paint(item['title'][:70], BOLD)}{badge}")
        print(f"   {reward} · плейбук {item['playbook']} · триаж {item['triage']} · "
              f"EV/час ~{money(item['ev_per_hour'])} · оценка {item['effort_hours']}ч")
        print(f"   {DIM}{item['why'][:150]}{RESET}")
        if item["url"]:
            print(f"   {item['url']}")
        # Шаги берём из расчёта (farm.next_actions), а не пишем текстом вручную:
        # раньше здесь были жёстко вписаны «досье → план», поэтому для квестов
        # площадки человек получал команды от другого канала, а свои не видел.
        steps = item.get("next") or []
        if steps:
            print(f"   {DIM}первый шаг: {steps[0]}{RESET}")
            rest = steps[1:]
            if rest:
                tail = " → ".join(rest)
                print(f"   {DIM}дальше: {tail[:230]}{'…' if len(tail) > 230 else ''}{RESET}")
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
    fresh = paint(f"новых {outcome['new']}", GREEN) if outcome.get("new") else "новых 0"
    print(
        f"{paint('ГОТОВО', GREEN)} {name}: найдено {outcome.get('found', 0)}, "
        f"{fresh}, сохранено {outcome.get('kept', 0)}, отсеяно {outcome.get('dropped', 0)}, "
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
        f"Итого: найдено {result.found} (впервые {result.new}), сохранено {result.kept}, "
        f"отсеяно как оплаченные {result.dropped}, триаж по {result.triaged}"
    )
    if result.pipeline_ev_usd:
        print(f"Ожидаемая выручка по топу этого цикла: {paint(money(result.pipeline_ev_usd), BOLD)}")
    if getattr(result, "low_value_top", 0):
        print(paint(f"Убыточных задач в топе: {result.low_value_top} — конкуренция съедает "
                    f"награду, они в выручку не попали. Ждите свежих: их разбирают "
                    f"в первые часы.", YELLOW))
    if result.kept:
        print(f"Дальше: {paint('python -m agent.main дальше', BOLD)}")
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


def cmd_watchdog(args: argparse.Namespace) -> int:
    data = watchdog.state()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if data["status"] == "ok" else 1

    level = {"ok": paint("всё в порядке", GREEN),
             "warning": paint("есть замечания", YELLOW),
             "critical": paint("требуется вмешательство", RED)}[data["status"]]
    print(paint("Дозор:", BOLD), level)
    print()
    if data["problems"]:
        for problem in data["problems"]:
            mark = paint("!!", RED) if problem["severity"] == "critical" else paint("!", YELLOW)
            print(f"  {mark} {problem['title']}")
            print(f"     что случилось: {problem['detail']}")
            print(f"     что делать: {problem['advice']}")
    else:
        print("  проблем нет.")
    if data["notes"]:
        print()
        print(paint("Ждут настройки (это не поломка):", DIM))
        for note in data["notes"]:
            print(f"  • {note}")

    print()
    print(paint("Чему научился робот:", BOLD))
    for line in learning.summary_lines():
        print(f"  • {line}")
    return 0


def cmd_learning(args: argparse.Namespace) -> int:
    data = learning.learned_state()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(paint("Обучение на ваших решениях", BOLD))
    print(f"  наблюдений: {data['observations']} · решений: {data['decisions']} · "
          f"отправлено: {data['published']} · заработано: {money(data['earned_usd'])}")
    print()
    if data["queries"]:
        print(paint("Запросы (вес: 1.0 — нейтрально, больше — продуктивнее):", BOLD))
        for item in data["queries"]:
            print(f"  [{item['weight']:>4.2f}] {item['subject'][:62]}")
            print(f"          новых задач {item['new_items']:.0f} за {item['observations']} проходов · "
                  f"отправлено {item['published']} · пропущено {item['skipped']}")
    if data["playbooks"]:
        print()
        print(paint("Типы работ:", BOLD))
        for item in data["playbooks"]:
            print(f"  {item['subject'][:50]}: отправлено {item['published']} из {item['decisions']}")
    if not data["queries"] and not data["playbooks"]:
        print("  данных пока нет: решения по первым заданиям включат обучение.")
    print()
    for line in learning.summary_lines():
        print(f"  • {line}")
    return 0


def cmd_contests(args: argparse.Namespace) -> int:
    """Контесты с дедлайнами: показываем то, что ещё можно взять."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, title, url, reward_usd, score, status, payload FROM opportunities "
            "WHERE channel='audit_contests' ORDER BY score DESC LIMIT 50"
        ).fetchall()
    finally:
        conn.close()

    active: List[Dict[str, Any]] = []
    expired: List[Dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        item = {
            "id": row["id"],
            "title": row["title"],
            "url": row["url"],
            "score": row["score"],
            "status": row["status"],
            "pool": payload.get("prize_pool_usd"),
            "hours_left": payload.get("hours_left"),
            "scope_entries": payload.get("scope_entries"),
            "code_repos": payload.get("code_repos") or [],
            "expired": bool(payload.get("expired")) or row["status"] == "done",
            "ends_at": payload.get("ends_at"),
        }
        (expired if item["expired"] else active).append(item)

    print(paint("Аудит-контесты:", BOLD),
          f"активных {len(active)} · завершённых {len(expired)}")
    print()
    if not active:
        print("  Активных контестов нет. Новые появляются раз в неделю —")
        print("  автопилот проверит сам: python -m agent.main автопилот --status")
    for item in sorted(active, key=lambda it: (it["hours_left"] is None, it["hours_left"] or 0)):
        pool = f"${item['pool']:,.0f}" if item["pool"] else "пул не указан"
        hours = f"{item['hours_left']:.0f}ч до конца" if item["hours_left"] is not None else "срок на площадке"
        scope = f"{item['scope_entries']} файлов в scope" if item["scope_entries"] else "scope не прочитан"
        print(f"  {paint(item['title'][:56], BOLD)}")
        print(f"    {pool} · {hours} · {scope} · EV/час ~{money(item['score'])}")
        if item["code_repos"]:
            print(f"    {DIM}код: {', '.join(item['code_repos'])}{RESET}")
        if item["url"]:
            print(f"    {item['url']}")
        print(f"    {DIM}досье: python -m agent.main досье {item['id']}{RESET}")
        print()
    if expired:
        print(paint("Завершённые (в работу не берём):", DIM))
        for item in expired[:5]:
            end = f", конец {item['ends_at'][:10]}" if item["ends_at"] else ""
            print(f"  {item['title'][:56]}{end}")
    return 0


def cmd_dossier(args: argparse.Namespace) -> int:
    opportunity = get_opportunity(args.opportunity_id)
    if not opportunity:
        print(f"Возможность {args.opportunity_id} не найдена.")
        return 1
    payload = json.loads(opportunity["payload"] or "{}")
    opportunity["payload"] = payload

    is_quest = args.opportunity_id.startswith("market:") or (
        opportunity.get("channel") == "agent_marketplaces"
    )
    if is_quest:
        print(paint("Собираю досье по квесту площадки (без сети, из данных очереди)...", DIM))
    else:
        print(paint("Собираю досье по задаче (публичный API GitHub)...", DIM))
    card, path = dossier_mod.save(opportunity)
    print()
    print(card.to_markdown())
    print(paint(f"Сохранено: {path.relative_to(project_root())}", DIM))
    if not card.tree_available and not is_quest:
        print(paint("Дерево файлов недоступно — проверьте токен: python -m agent.main проверка",
                    YELLOW))
    return 0


def cmd_followup(args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(followup_mod.state(), ensure_ascii=False, indent=2))
        return 0

    watches = followup_mod.check(limit=args.limit)
    if not watches:
        print("Задач в работе нет: слежение включится после первой отправленной заявки")
        print(paint("Как взять задачу: python -m agent.main дальше → "
                    "заявка <id> → статус <id> working", DIM))
        return 0

    marks = {
        "ok": (GREEN, "в порядке"),
        "rival": (YELLOW, "появился соперник"),
        "stale": (YELLOW, "затишье"),
        "closed": (RED, "задача закрыта"),
        "lost": (RED, "выплата ушла другому"),
        "unknown": (DIM, "не проверить"),
    }
    print(paint("Слежение за задачами в работе:", BOLD))
    print()
    for watch in watches:
        colour, label = marks.get(watch.verdict, (DIM, watch.verdict))
        print(f"  {paint(label, colour)} · {watch.title[:60]}")
        print(f"    ${watch.reward_usd:,.0f} · статус {watch.status} · "
              f"заявок {watch.attempts_now} · открытых PR {watch.open_prs_now}")
        if watch.reason:
            print(f"    {watch.reason}")
        if watch.action and watch.verdict != "ok":
            print(f"    {paint('что делать:', BOLD)} {watch.action}")
        print()
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Заполнить .env: секреты вводятся скрыто и никогда не печатаются."""
    if args.show:
        current = setupenv.read_env()
        print(paint(f"Файл {setupenv.env_path()}", BOLD))
        if not current:
            print("  ещё не создан — запустите: python -m agent.main настройка")
            return 0
        for field in setupenv.FIELDS:
            value = (current.get(field.name) or "").strip()
            shown = setupenv.mask(value) if field.secret else (value or "не задано")
            print(f"  {field.title}: {shown}")
        mode = oct(setupenv.env_path().stat().st_mode & 0o777)
        print(f"  права на файл: {mode}")
        return 0

    answers = {
        "GITHUB_TOKEN": args.token,
        "ELIGIBILITY_COUNTRY": args.country,
        "PAYOUT_WALLET": args.wallet,
        "AGENTHANSA_API_KEY": args.marketplace_key,
        "TELEGRAM_BOT_TOKEN": args.telegram_token,
        "TELEGRAM_CHAT_ID": args.telegram_chat,
    }
    interactive = not args.no_ask
    if interactive and not any(answers.values()):
        print(paint("Настройка AGENT-0. Секреты вводятся скрыто и не показываются на экране.",
                    BOLD))
        print(paint("Enter пропускает необязательные пункты. Прервать: Ctrl+C.", DIM))

    updates, problems, notes = setupenv.collect(answers, interactive=interactive)
    if updates:
        setupenv.update_env(updates)
        # процесс уже загрузил окружение на старте — обновляем его на месте,
        # иначе проверка готовности увидит старые значения
        os.environ.update(updates)

    for line in setupenv.summary(updates, problems, notes):
        print(line)

    if problems:
        print()
        print(paint("Исправьте и повторите: python -m agent.main настройка", YELLOW))
        return 2

    if updates:
        print()
        print(paint("Проверяю готовность заново:", BOLD))
        return cmd_doctor(args) if hasattr(args, "json") else cmd_doctor(
            argparse.Namespace(json=False)
        )
    print()
    print(paint("Ничего не изменено.", DIM))
    return 0


def cmd_autopilot(args: argparse.Namespace) -> int:
    if args.status:
        data = autopilot_mod.status()
        print(paint("Автопилот:", BOLD), data.get("state_ru", data["state"]))
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
        print(paint("Готовые тексты ждут вашего действия: python -m agent.main входящие", DIM))
        return 0

    print(paint("Автопилот запущен. Ctrl+C — остановка, "
                f"`touch data/{autopilot_mod.KILL_SWITCH_NAME}` — мгновенный стоп.", BOLD))
    print(paint("Робот не публикует заявки, не отправляет квесты и не подтверждает выплаты.", DIM))
    return autopilot_mod.run(ticks=args.ticks, on_tick=report)


def inbox_show_id(tokens: List[str]) -> Optional[int]:
    """«входящие 3» и «входящие show 3» — оба варианта понятны человеку."""
    cleaned = [token for token in tokens if token != "show"]
    if not cleaned:
        return None
    try:
        return int(cleaned[0])
    except ValueError:
        return None


def cmd_inbox(args: argparse.Namespace) -> int:
    show_id = inbox_show_id(list(args.args or []))
    if show_id is not None:
        item = inbox_mod.get(show_id)
        if not item:
            print(f"Элемент #{show_id} не найден.")
            return 1
        print(paint(f"#{item['id']} [{item['kind_title']}] {item['title']}", BOLD))
        print(f"  награда/сводка: {item['summary']}")
        print(f"  действие человека: {item['action']}")
        print(f"  файл: {item['path']}")
        print()
        print(inbox_mod.text(show_id))
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
    print(paint("Показать текст: python -m agent.main входящие show <id>", DIM))
    print(paint("Отметить: python -m agent.main входящие-отправлено <id> "
                "| входящие-пропустить <id>", DIM))
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


def cmd_hansa(args: argparse.Namespace) -> int:
    """Площадка AgentHansa: статус, лента заданий, кошелёк и отправка работы.

    Отправка работы (``отправить``) — единственное действие, которое выходит
    наружу от имени человека, поэтому она требует флага ``--подтверждаю``.
    Всё остальное безопасно: чтение ленты, отметка, привязка кошелька.
    """
    import json as _json

    action = (args.action or "статус").strip().lower()
    value = (args.value or "").strip()
    aliases = {
        "статус": "статус", "status": "статус", "me": "статус",
        "лента": "лента", "feed": "лента", "inbox": "лента",
        "квесты": "квесты", "quests": "квесты",
        "квест": "квест", "quest": "квест",
        "отметиться": "отметиться", "checkin": "отметиться",
        "кошелёк": "кошелёк", "кошелек": "кошелёк", "wallet": "кошелёк",
        "альянс": "альянс", "alliance": "альянс",
        "регистрация": "регистрация", "register": "регистрация",
        "отправить": "отправить", "submit": "отправить",
        "сети": "сети", "probe": "сети", "проверка-сети": "сети",
    }
    action = aliases.get(action, action)

    if action == "сети":
        url = hansa.base_url() + hansa.ROUTES["me"]
        try:
            session = hansa.build_session("AGENT-0-agenthansa-probe/1.0",
                                          {"Accept": "application/json"})
            response = session.get(url, timeout=15)
            print(f"Площадка отвечает: HTTP {response.status_code} — {hansa.base_url()}")
            print(f"{DIM}Без ключа ответ 401 — это нормально: значит адрес живой "
                  f"и ждёт AGENTHANSA_API_KEY.{RESET}")
            return 0
        except Exception as exc:
            print(f"Площадка недоступна из этой среды: {exc.__class__.__name__}: {exc}")
            print(f"{DIM}Так бывает в песочницах с белым списком хостов. Ключ и "
                  f"работа переносятся туда, где сеть открыта: GitHub Actions "
                  f"(.github/workflows/hansa.yml) или ваша машина.{RESET}")
            return 2

    if action == "регистрация":
        name = args.name or "agent-0"
        description = args.description or (
            "Автономная ферма агента: аудит-контесты, bounty-задачи, поиск и "
            "проверка конкуренции, планы работ и черновики текстов."
        )
        try:
            payload = hansa.register(name, description)
        except hansa.HansaError as exc:
            print(f"Регистрация не прошла: {exc}")
            return 2
        key = str(payload.get("api_key"))
        updates = {"AGENTHANSA_API_KEY": key}
        if payload.get("id"):
            updates["AGENTHANSA_AGENT_ID"] = str(payload["id"])
        setupenv.update_env(updates)
        os.environ.update(updates)
        print(f"Агент зарегистрирован: {payload.get('name') or name}")
        print(f"  Ключ сохранён в .env: {hansa._mask(key)} (показан полностью один раз — "
              f"в файле он лежит открытым текстом)")
        if payload.get("balance"):
            print(f"  Приветственный баланс: ${payload['balance']}")
        print(f"{DIM}Дальше: площадка кошелёк — привязать адрес, иначе выплата "
              f"держится 3-7 дней{RESET}")
        return 0

    if not hansa.api_key():
        print("Ключ площадки не задан: AGENTHANSA_API_KEY пуст.")
        print("Регистрация (одна команда, ключ вернётся один раз):")
        print("  python -m agent.main площадка регистрация --имя agent-0")
        print(f"{DIM}Ключ хранится в .env (права 600) и не печатается. Без ключа "
              f"площадка отвечает 401 на всё, кроме регистрации.{RESET}")
        return 2

    client = hansa.Hansa()
    try:
        if action == "статус":
            me = client.me()
            reputation = client.reputation()
            points = client.points()
            earnings = client.earnings()
            payouts = client.payouts()
            if args.json:
                print(_json.dumps({
                    "agent": {k: me.get(k) for k in
                              ("name", "alliance", "reputation", "tier", "balance", "xp",
                               "wallet_address", "fluxa_agent_id")},
                    "reputation": reputation, "points": points,
                    "earnings": earnings, "payouts": payouts,
                }, ensure_ascii=False, indent=2, default=str))
                return 0
            print(paint(f"Агент {me.get('name', '—')} на площадке AgentHansa", BOLD))
            tier = reputation.get("tier") or me.get("tier") or "—"
            print(f"  Альянс: {me.get('alliance') or 'не выбран'} · репутация "
                  f"{reputation.get('score', me.get('reputation', '—'))} ({tier}) · "
                  f"множитель выплаты {reputation.get('payout_multiplier', '—')}")
            print(f"  Баланс: ${me.get('balance', '0')} · XP {me.get('xp', '—')} · "
                  f"очки {points.get('balance', points.get('points', '—'))}")
            # Имя переменной не должно совпадать с модулем agent.wallet: иначе
            # Python считает его локальным во всей функции и ветка «кошелёк»
            # падает с UnboundLocalError (это ловилось тестом).
            bound_wallet = me.get("wallet_address") or me.get("fluxa_agent_id") or ""
            print(f"  Кошелёк: {bound_wallet or 'не привязан (выплата держится 3-7 дней)'}")
            total = earnings.get("total_usd", earnings.get("total", 0))
            print(f"  Заработано всего: ${total or 0} · выплат записей: {len(payouts)}")
            return 0

        if action == "лента":
            inbox = client.inbox()
            sections = {
                "engagement": "задания от площадки",
                "alliance_war_quests": "квесты альянсов",
                "reddit_karma_quest": "кармический квест Reddit",
                "personal": "личные задачи",
                "side_quests": "мелкие задания",
            }
            if args.json:
                print(_json.dumps(inbox, ensure_ascii=False, indent=2, default=str))
                return 0
            print(paint("Что площадка предлагает сейчас", BOLD))
            total = 0
            for key, title in sections.items():
                rows = inbox.get(key)
                if isinstance(rows, dict):
                    rows = rows.get("items") or []
                if not isinstance(rows, list) or not rows:
                    continue
                total += len(rows)
                print(f"  {title}: {len(rows)}")
                for row in rows[:5]:
                    reward = row.get("reward_amount") or row.get("pay_usd") or row.get("reward")
                    print(f"    · {str(row.get('title') or row.get('name') or '—')[:70]}"
                          f"{f' — ${reward}' if reward else ''}")
            print(f"{DIM}Всего позиций: {total}. Взять в работу: площадка квесты{RESET}")
            return 0

        if action == "квесты":
            quests = client.quests()
            if args.json:
                print(_json.dumps([q.as_dict() for q in quests], ensure_ascii=False, indent=2))
                return 0
            if not quests:
                print("Открытых квестов нет — площадка раздаёт их волнами, заходите позже.")
                return 0
            quests.sort(key=lambda q: q.reward_usd, reverse=True)
            print(paint(f"Квесты площадки: {len(quests)}", BOLD))
            for quest in quests[:15]:
                deadline = f" · до {quest.deadline}" if quest.deadline else ""
                print(f"  ${quest.reward_usd:>7,.0f}  {quest.title[:64]}{deadline}")
                print(f"    {DIM}id {quest.id} · заявок {quest.submissions}"
                      f"{f' из {quest.cap}' if quest.cap else ''}{RESET}")
            print(f"{DIM}Подробно: площадка квест <id>. Отправить работу человеком: "
                  f"площадка отправить <id> --файл <текст> --подтверждаю{RESET}")
            return 0

        if action == "квест":
            if not value:
                print("Укажите id квеста: площадка квест <id>")
                return 2
            quest = client.quest(value)
            print(paint(quest.title, BOLD))
            print(f"  Награда: ${quest.reward_usd:,.2f} · заявок {quest.submissions}"
                  f"{f' из {quest.cap}' if quest.cap else ''} · дедлайн {quest.deadline or '—'}")
            if quest.requirements:
                print(f"  Требования: {quest.requirements[:600]}")
            if quest.description:
                print(f"  Описание: {quest.description[:1200]}")
            return 0

        if action == "отметиться":
            result = client.checkin()
            streak = result.get("streak", result.get("streak_days", "—"))
            print(f"Отметка принята: +{result.get('xp', 10)} XP, серия {streak} дн., "
                  f"начислено ${result.get('reward_usd', result.get('reward', 0))}")
            return 0

        if action == "кошелёк":
            address = value or (get_env("PAYOUT_WALLET", "") or "").strip()
            if not address:
                print("Адрес не задан. Укажите: площадка кошелёк 0x…")
                print(f"{DIM}Или сначала сохраните его вообще для всех каналов: "
                      f"python -m agent.main кошелёк 0x… --save{RESET}")
                return 2
            info = wallet.classify(address)
            if not info.ok:
                print(f"Адрес не годен: {info.problem}")
                print(f"{DIM}{info.advice}{RESET}")
                return 2
            result = client.bind_wallet(info.normalized)
            print(f"Кошелёк привязан: {wallet.mask(info.normalized)}")
            print(f"{DIM}С привязанным кошельком выплата приходит сразу, без задержки "
                  f"3-7 дней. Ответ площадки: {str(result)[:200]}{RESET}")
            return 0

        if action == "альянс":
            if not value:
                me = client.me()
                print(f"Текущий альянс: {me.get('alliance') or 'не выбран'}")
                print("Сменить: площадка альянс red|blue|green")
                return 0
            result = client.choose_alliance(value)
            print(f"Альянс: {result.get('alliance') or value}")
            return 0

        if action == "отправить":
            if not value:
                print("Укажите id квеста: площадка отправить <id> --файл <текст> --подтверждаю")
                return 2
            if not args.file:
                print("Нужен текст работы: --файл <путь> (робот готовит его в отчётах/черновиках)")
                return 2
            path = project_root() / args.file
            if not path.exists():
                print(f"Файл не найден: {path}")
                return 2
            text = path.read_text(encoding="utf-8")
            if not args.confirm:
                print("Отправка работы — действие человека. Робот подготовил текст, "
                      "проверьте его и повторите с флагом --подтверждаю.")
                print(f"{DIM}Текст: {path} · квест {value} · доказательство "
                      f"{args.proof or '—'}{RESET}")
                return 2
            result = client.submit_quest(value, text, args.proof, confirm=True)
            print(f"Работа отправлена: {str(result)[:300]}")
            return 0

        print(f"Неизвестное действие «{action}». Доступно: статус, лента, квесты, квест, "
              f"отметиться, кошелёк, альянс, отправить, сети, регистрация")
        return 2
    except hansa.ConfirmationRequired as exc:
        print(f"Нужно подтверждение человека: {exc}")
        return 2
    except hansa.HansaError as exc:
        print(f"Площадка отказала: {exc}")
        return 2
    except Exception as exc:
        print(f"Связь с площадкой не удалась: {exc.__class__.__name__}: {exc}")
        return 2


def cmd_wallet(args: argparse.Namespace) -> int:
    """Проверить адрес кошелька и, если он верный, сохранить в .env.

    Деньги в крипте не возвращаются: ошибка в одном символе отправляет выплату
    чужому человеку, а площадка при этом отчитается, что заплатила. Поэтому
    адрес проверяется по правилам сети (контрольная сумма EVM, base58check
    TRON, длина Solana) до того, как его увидят на площадке.
    """
    saved = (get_env("PAYOUT_WALLET", "") or "").strip()
    address = (args.address or saved).strip()

    if not address:
        print(paint("Кошелёк не указан.", BOLD))
        print("Проверить адрес:   python -m agent.main кошелёк 0x…")
        print("Сохранить в .env:  python -m agent.main кошелёк 0x… --save")
        print(f"{DIM}Откуда взять адрес (Phantom): Settings → Active Networks → включить Base, "
              f"затем скопировать адрес кнопкой «Copy». Это адрес EVM (0x…), не Solana.{RESET}")
        return 0

    info = wallet.classify(address)
    if args.json:
        print(json.dumps({"address": wallet.mask(address), **info.as_dict()},
                         ensure_ascii=False, indent=2))
        return 0 if info.ok else 2

    print(paint(f"Проверка адреса {wallet.mask(address)}", BOLD))
    print(f"  Тип: {info.title}")
    if info.networks:
        print(f"  Сети: {', '.join(info.networks)}")
    mark = paint("годен", GREEN) if info.ok else paint("не годен", RED)
    print(f"  Вердикт: {mark}")
    if info.problem:
        print(f"  Что не так: {info.problem}")
    if info.advice:
        print(f"  {DIM}{info.advice}{RESET}")

    if not info.ok:
        return 2

    if args.save:
        setupenv.update_env({"PAYOUT_WALLET": info.normalized})
        os.environ["PAYOUT_WALLET"] = info.normalized
        print(f"  Сохранено в .env: PAYOUT_WALLET={wallet.mask(info.normalized)}")
        print(f"{DIM}Адрес ещё нужно привязать в кабинете площадки — "
              f"иначе выплата не уйдёт.{RESET}")
    else:
        stored = wallet.classify(saved) if saved else None
        if stored and stored.ok and stored.normalized.lower() == info.normalized.lower():
            print(f"{DIM}Этот адрес уже записан в .env.{RESET}")
        else:
            print(f"{DIM}Сохранить в .env: python -m agent.main кошелёк "
                  f"{info.normalized} --save{RESET}")

    if info.kind == "solana":
        print(f"{DIM}Важно: у адресов Solana нет контрольной суммы — опечатку проверить "
              f"невозможно. Для площадок, платящих на Base (USDC), нужен EVM-адрес из "
              f"Phantom: это другой адрес того же кошелька.{RESET}")
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


#: Русские названия команд. Английские остаются рабочими — на них ссылаются
#: документация, cron и службы, — но писать по-русски тоже можно.
ALIASES: Dict[str, str] = {
    "состояние": "status",
    "дальше": "next",
    "цикл": "cycle",
    "очередь": "queue",
    "конкуренция": "triage",
    "план": "plan",
    "заявка": "apply",
    "статус": "status-set",
    "часы": "hours-add",
    "аналитика": "analytics",
    "проверка-страны": "eligibility",
    "отчёт": "report",
    "отчет": "report",
    "направления": "directions",
    "каналы-выплат": "payout-rails",
    "автоповтор": "loop",
    "выплата-запись": "payout-add",
    "выплата-подтвердить": "payout-verify",
    "бухгалтерия": "ledger",
    "правила": "policy",
    "политика": "policy",
    "кто-я": "whoami",
    "кошелёк": "wallet",
    "площадка": "hansa",
    "кошелек": "wallet",
    "настройка": "setup",
    "контесты": "contests",
    "досье": "dossier",
    "слежение": "followup",
    "дозор": "watchdog",
    "обучение": "learning",
    "автопилот": "autopilot",
    "входящие": "inbox",
    "входящие-отправлено": "inbox-done",
    "входящие-пропустить": "inbox-skip",
    "смена": "shift",
    "проверка": "doctor",
    "черновик": "quest",
    "дашборд": "serve",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent.main",
        description="AGENT-0: автономная ферма воркеров для заработка без вложений",
        epilog="Команды можно писать по-русски: проверка, автопилот, входящие, смена, "
               "направления, каналы-выплат, черновик, заявка. "
               "Справка по команде: python -m agent.main <команда> --help",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="состояние фермы и деньги")

    next_cmd = sub.add_parser("next", help="что делать прямо сейчас")
    next_cmd.add_argument("--limit", type=int, default=5)

    cycle = sub.add_parser(
        "cycle",
        help="проход по воркерам",
        epilog="Воркеры можно называть по-русски: гитхаб, контесты, площадки, разведка, "
               "либо все сразу (без --channel).",
    )
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

    hansa_cmd = sub.add_parser("hansa", help="площадка агентов AgentHansa: квесты и выплаты")
    hansa_cmd.add_argument("action", nargs="?", default="статус",
                           help="статус | лента | квесты | квест | отметиться | кошелёк | альянс | отправить | сети | регистрация")
    hansa_cmd.add_argument("value", nargs="?", default="", help="id квеста, адрес или альянс")
    hansa_cmd.add_argument("--имя", dest="name", default="", help="имя агента при регистрации")
    hansa_cmd.add_argument("--описание", dest="description", default="", help="чем занимается агент")
    hansa_cmd.add_argument("--файл", dest="file", default="", help="текст работы для отправки")
    hansa_cmd.add_argument("--доказательство", dest="proof", default="", help="ссылка на доказательство")
    hansa_cmd.add_argument("--подтверждаю", dest="confirm", action="store_true",
                           help="подтверждение человека на отправку работы")
    hansa_cmd.add_argument("--json", action="store_true")

    wallet_cmd = sub.add_parser("wallet", help="проверить адрес кошелька для выплат")
    wallet_cmd.add_argument("address", nargs="?", default="")
    wallet_cmd.add_argument("--save", action="store_true", help="записать адрес в .env")
    wallet_cmd.add_argument("--json", action="store_true")

    contests = sub.add_parser("contests", help="контесты: пул, дедлайн, объём работ")
    contests.add_argument("--all", action="store_true")

    dose = sub.add_parser("dossier", help="досье по задаче: где править и что запускать")
    dose.add_argument("opportunity_id")

    follow = sub.add_parser("followup", help="слежение за задачами в работе")
    follow.add_argument("--limit", type=int, default=20)
    follow.add_argument("--json", action="store_true")

    watch = sub.add_parser("watchdog", help="дозор: что сломалось, пока вас не было")
    watch.add_argument("--json", action="store_true")

    learn = sub.add_parser("learning", help="что робот понял по вашим решениям")
    learn.add_argument("--json", action="store_true")

    setup_cmd = sub.add_parser(
        "setup",
        help="заполнить .env (секреты вводятся скрыто)",
        epilog="Секреты не показываются на экране и не попадают в историю команд. "
               "Файл .env закрывается правами 600 и уже исключён из git.",
    )
    setup_cmd.add_argument("--token", default=None, help="токен GitHub")
    setup_cmd.add_argument("--country", default=None, help="код страны получения денег")
    setup_cmd.add_argument("--wallet", default=None, help="адрес кошелька (0x… или T…)")
    setup_cmd.add_argument("--marketplace-key", default=None, help="ключ площадки квестов")
    setup_cmd.add_argument("--telegram-token", default=None)
    setup_cmd.add_argument("--telegram-chat", default=None)
    setup_cmd.add_argument("--no-ask", action="store_true",
                           help="ничего не спрашивать, использовать только переданное")
    setup_cmd.add_argument("--show", action="store_true",
                           help="показать текущие значения маской")

    auto = sub.add_parser("autopilot", help="автономный режим: работает без вас")
    auto.add_argument("--once", action="store_true", help="один проход и выход")
    auto.add_argument("--ticks", type=int, default=None, help="ограничить число проходов")
    auto.add_argument("--prepare", type=int, default=None, help="сколько артефактов готовить за проход")
    auto.add_argument("--status", action="store_true", help="состояние робота без запуска")

    inbox_cmd = sub.add_parser(
        "inbox",
        help="очередь готового к публикации",
        epilog="Примеры: python -m agent.main входящие  — список; "
               "python -m agent.main входящие show 3 — текст элемента №3.",
    )
    inbox_cmd.add_argument("args", nargs="*", help="show <номер> — показать текст элемента")
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

    # Русские имена команд ведут к тем же обработчикам. Службы и cron
    # продолжают работать с английскими — они не меняются.
    try:
        for alias, canonical in ALIASES.items():
            sub._name_parser_map[alias] = sub._name_parser_map[canonical]
    except (AttributeError, KeyError):  # pragma: no cover - защита от иной версии argparse
        pass

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
    "wallet": cmd_wallet,
    "hansa": cmd_hansa,
    "setup": cmd_setup,
    "contests": cmd_contests,
    "dossier": cmd_dossier,
    "followup": cmd_followup,
    "watchdog": cmd_watchdog,
    "learning": cmd_learning,
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
    command = ALIASES.get(args.command, args.command)
    handler = HANDLERS.get(command)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nОстановлено.")
