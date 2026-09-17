"""AGENT-0 command line.

    python -m agent.main status                 # состояние фермы и политика
    python -m agent.main cycle                  # один проход по воркерам
    python -m agent.main queue                  # очередь лучших возможностей
    python -m agent.main loop --interval 900    # автономный режим
    python -m agent.main draft <opportunity_id> # черновик плана работ
    python -m agent.main payout-add ...
    python -m agent.main payout-verify <id>
    python -m agent.main ledger                 # бухгалтерия
    python -m agent.main policy                 # что разрешено, что запрещено
    python -m agent.main serve                  # дашборд на 0.0.0.0:8000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List

from agent.config import load_environment
from agent.farm import overview, run_cycle
from agent.ledger import (
    connect,
    record_payout,
    summary,
    top_opportunities,
    verify_payout,
)

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _supports_color() -> bool:
    return sys.stdout.isatty()


def paint(text: str, color: str) -> str:
    if not _supports_color():
        return text
    return f"{color}{text}{RESET}"


def cmd_status(args: argparse.Namespace) -> int:
    data = overview()
    ledger = summary()

    print(paint("AGENT-0 — ферма автономных воркеров", BOLD))
    print()
    verified = ledger["verified_usd"]
    claimed = ledger["claimed_usd"]
    print(f"Возможностей в базе: {ledger['opportunities_total']} "
          f"(в очереди: {ledger['opportunities_queued']})")
    print(f"Подтверждённый доход: {paint(f'${verified:.2f}', GREEN)}  "
          f"| Заявлено, но не подтверждено: ${claimed:.2f}")
    print()

    from agent.channels import build_all

    print(paint("Работает сейчас:", BOLD))
    for name, channel in build_all().items():
        requirements = ", ".join(channel.decision.requirements) or "нет"
        print(f"  {paint('ВКЛ', GREEN)}  {channel.title}  [{name}]")
        print(f"       {DIM}capability: {channel.capability}; требования: {requirements}{RESET}")
        print(f"       {DIM}{channel.description}{RESET}")

    print()
    print(paint("Заблокировано политикой — реализовано не будет:", BOLD))
    for channel in data["channels"]:
        if channel["allowed"]:
            continue
        print(f"  {paint('ВЫКЛ', RED)} {channel['title']}")
        print(f"       {DIM}почему не работает: {channel['reason']}{RESET}")
        if channel["instead"]:
            print(f"       {DIM}вместо этого: {channel['instead']}{RESET}")
    return 0


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
        f"сохранено {outcome.get('kept', 0)}"
    )
    for item in outcome.get("top", [])[:5]:
        reward = f"${item['reward_usd']:,.0f}" if item["reward_usd"] else "награда н/д"
        print(f"    [{item['score']:>6.2f}] {reward:>12}  {item['title'][:70]}")
        print(f"             {DIM}{item['rationale'][:120]}{RESET}")


def cmd_cycle(args: argparse.Namespace) -> int:
    channels = [args.channel] if args.channel else None
    result = run_cycle(channels=channels, limit=args.limit)
    print()
    for outcome in result.channels:
        _print_channel_result(outcome)
        print()
    for note in result.notes:
        print(paint(f"примечание: {note}", YELLOW))
    print(f"Итого: найдено {result.found}, сохранено {result.kept}")
    if result.kept:
        print(f"Смотреть очередь: {paint('python -m agent.main queue', BOLD)}")
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    rows = top_opportunities(limit=args.limit, channel=args.channel)
    if not rows:
        print("Очередь пуста. Запустите: python -m agent.main cycle")
        return 0
    print(f"{'score':>7}  {'награда':>10}  {'канал':<16}  задача")
    for row in rows:
        reward = f"${row['reward_usd']:,.0f}" if row["reward_usd"] else "н/д"
        print(f"{row['score']:>7.2f}  {reward:>10}  {row['channel']:<16}  {row['title'][:60]}")
        print(f"         {DIM}{row['id']}{RESET}")
        print(f"         {DIM}{row['rationale'][:130]}{RESET}")
    return 0


def cmd_draft(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM opportunities WHERE id=?", (args.opportunity_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        print(f"Возможность {args.opportunity_id} не найдена. Сначала: cycle")
        return 1

    payload = json.loads(row["payload"] or "{}")
    from agent.llm import LLMClient

    prompt = (
        "Ты — инженер, который готовит план выполнения открытой bounty-задачи.\n"
        f"Задача: {row['title']}\n"
        f"Репозиторий: {row['repo']}\n"
        f"Ссылка: {row['url']}\n"
        f"Награда: {row['reward_usd']} (источник: {row['reward_source']})\n"
        f"Метки: {', '.join(payload.get('labels', []))}\n"
        f"Комментариев в задаче: {payload.get('comments')}\n\n"
        "Составь план: 1) что именно нужно изменить; 2) какие файлы посмотреть; "
        "3) как проверить результат; 4) что указать в pull request. "
        "Если данных не хватает — перечисли вопросы, на которые нужно ответить до начала работы."
    )
    client = LLMClient()
    if not client.is_available():
        print(paint("Локальная модель (Ollama) недоступна — печатаю каркас плана.", YELLOW))
        print()
        print(f"Задача: {row['title']}")
        print(f"Ссылка: {row['url']}")
        print(f"Оценка: {row['rationale']}")
        print()
        print("Проверить вручную перед началом работы:")
        print("  1. Задача ещё открыта и награда не выплачена?")
        print("  2. Есть ли уже PR от других участников?")
        print("  3. Правила программы разрешают оплату внешнему участнику?")
        return 0

    print(client.generate(prompt))
    return 0


def cmd_loop(args: argparse.Namespace) -> int:
    channels = [args.channel] if args.channel else None
    interval = max(60, args.interval)
    print(f"Автономный режим: цикл каждые {interval}с. Ctrl+C — остановка.")
    while True:
        result = run_cycle(channels=channels, limit=args.limit)
        print(f"[{time.strftime('%H:%M:%S')}] найдено {result.found}, сохранено {result.kept}")

        from agent.telegram import TelegramNotifier

        notifier = TelegramNotifier()
        if notifier.enabled() and result.kept:
            lines = [f"AGENT-0: +{result.kept} новых возможностей"]
            for outcome in result.channels:
                for item in outcome.get("top", [])[:3]:
                    reward = f"${item['reward_usd']:,.0f}" if item["reward_usd"] else "н/д"
                    lines.append(f"[{reward}] {item['title'][:80]}")
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
    print(f"Возможности: {data['opportunities_total']} (в очереди {data['opportunities_queued']})")
    print(f"Подтверждено: ${data['verified_usd']:.2f} | Заявлено: ${data['claimed_usd']:.2f}")
    print()
    if data["channels"]:
        print(paint("По каналам:", BOLD))
        for row in data["channels"]:
            reward = row["reward"] or 0
            print(
                f"  {row['channel']:<16} задач {row['n']:>4}  "
                f"суммарная заявленная награда ${reward:,.0f}  "
                f"средний score {(row['avg_score'] or 0):.2f}"
            )
    print()
    print(paint("Выплаты:", BOLD))
    if not data["payouts"]:
        print("  пока пусто — это честнее, чем нарисованные цифры")
    for row in data["payouts"]:
        mark = paint("подтверждено", GREEN) if row["verified"] else paint("заявка", YELLOW)
        print(f"  #{row['id']} {row['channel']} ${row['amount']:.2f} {row['currency']} — {mark}")
        if row["evidence"]:
            print(f"      доказательство: {row['evidence']}")
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
        color = GREEN if item["status"] == "allowed" else (YELLOW if "human" in item["status"] else RED)
        print(f"  {paint(item['status'].upper(), color):<28} {item['request']}")
        print(f"    {DIM}{item['comment']}{RESET}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from agent.dashboard import serve

    serve(host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent.main",
        description="AGENT-0: автономная ферма воркеров для заработка без вложений",
    )
    parser.add_argument("--json", action="store_true", help="вывод в JSON, где поддерживается")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="состояние фермы")

    cycle = sub.add_parser("cycle", help="один проход по воркерам")
    cycle.add_argument("--channel", default=None)
    cycle.add_argument("--limit", type=int, default=25)

    queue = sub.add_parser("queue", help="очередь лучших возможностей")
    queue.add_argument("--limit", type=int, default=20)
    queue.add_argument("--channel", default=None)

    draft = sub.add_parser("draft", help="черновик плана работ по задаче")
    draft.add_argument("opportunity_id")

    loop = sub.add_parser("loop", help="автономный режим")
    loop.add_argument("--channel", default=None)
    loop.add_argument("--limit", type=int, default=25)
    loop.add_argument("--interval", type=int, default=900)

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

    serve = sub.add_parser("serve", help="веб-дашборд")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)

    return parser


def main(argv: List[str] | None = None) -> int:
    load_environment()
    parser = build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "status": cmd_status,
        "cycle": cmd_cycle,
        "queue": cmd_queue,
        "draft": cmd_draft,
        "loop": cmd_loop,
        "payout-add": cmd_payout_add,
        "payout-verify": cmd_payout_verify,
        "ledger": cmd_ledger,
        "policy": cmd_policy,
        "serve": cmd_serve,
    }

    if not args.command:
        return cmd_status(args)
    return handlers[args.command](args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nОстановлено.")
