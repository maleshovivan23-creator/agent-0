"""AGENT-0 dashboard.

A dependency-free HTTP server that answers three questions on one screen:

1. **Сколько заработано** — verified income, claimed payouts, effective hourly
   rate (verified money divided by logged hours), and the estimated value of
   the pipeline with the estimate clearly labelled as an estimate.
2. **Что делать сейчас** — the ordered list of highest expected value per hour,
   each with the exact command to start.
3. **Чего делать не стоит** — bounties the triage rejected (already paid, or a
   field of 30+ competitors) and the tactics the policy refuses, with reasons.

Endpoints:
    GET  /                    dashboard
    GET  /api/state           everything as JSON
    POST /api/run             start a farm cycle in the background
    POST /api/payout          record a claimed payout
    POST /api/payout/verify   confirm a payout (the human step)

Binds 0.0.0.0 so it is reachable through the sandbox preview proxy.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from agent import autopilot as autopilot_mod
from agent import doctor as doctor_mod
from agent import inbox as inbox_mod
from agent import followup as followup_mod
from agent import learning as learning_mod
from agent import watchdog as watchdog_mod
from agent import payouts as payout_rails
from agent.config import get_env
from agent.farm import low_value, next_actions, overview, run_cycle
from agent.ledger import (
    analytics,
    record_payout,
    summary,
    top_opportunities,
    verify_payout,
)

_RUNNER_LOCK = threading.Lock()
_RUNNER: Dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
    "log": [],
}


def _log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    _RUNNER["log"].append(f"{stamp} {message}")
    _RUNNER["log"] = _RUNNER["log"][-60:]


def _run_cycle_background(channel: Optional[str] = None) -> None:
    try:
        _log(f"цикл запущен (канал: {channel or 'все'})")
        result = run_cycle(channels=[channel] if channel else None, limit=30)
        with _RUNNER_LOCK:
            _RUNNER["result"] = result.as_dict()
            _RUNNER["error"] = None
        _log(
            f"цикл завершён: найдено {result.found}, сохранено {result.kept}, "
            f"отсеяно {result.dropped}, триаж {result.triaged}"
        )
        for note in result.notes:
            _log(f"примечание: {note}")
    except Exception as exc:
        with _RUNNER_LOCK:
            _RUNNER["error"] = f"{exc.__class__.__name__}: {exc}"
        _log(f"ошибка: {exc.__class__.__name__}: {exc}")
    finally:
        with _RUNNER_LOCK:
            _RUNNER["running"] = False
            _RUNNER["finished_at"] = time.time()


def start_cycle(channel: Optional[str] = None) -> bool:
    with _RUNNER_LOCK:
        if _RUNNER["running"]:
            return False
        _RUNNER["running"] = True
        _RUNNER["started_at"] = time.time()
        _RUNNER["finished_at"] = None
        _RUNNER["result"] = None
        _RUNNER["error"] = None
    threading.Thread(target=_run_cycle_background, args=(channel,), daemon=True).start()
    return True


_READINESS: Dict[str, Any] = {"at": 0.0, "data": None}
_FOLLOWUP: Dict[str, Any] = {"at": 0.0, "data": None}


def _followup(refresh: bool = False, ttl: float = 600.0) -> Dict[str, Any]:
    """Слежение ходит в GitHub — кэшируем, чтобы не жечь лимит на каждый запрос."""
    now = time.time()
    if refresh or _FOLLOWUP["data"] is None or now - _FOLLOWUP["at"] > ttl:
        try:
            _FOLLOWUP["data"] = followup_mod.state()
        except Exception as exc:  # дашборд не должен падать из-за проверки
            _FOLLOWUP["data"] = {"watched": 0, "verdicts": {},
                                 "error": f"{exc.__class__.__name__}: {exc}"}
        _FOLLOWUP["at"] = now
    return _FOLLOWUP["data"]


def _readiness(refresh: bool = False, ttl: float = 600.0) -> Dict[str, Any]:
    """Doctor checks hit the network once, then stay cached for the dashboard."""
    now = time.time()
    if refresh or _READINESS["data"] is None or now - _READINESS["at"] > ttl:
        _READINESS["data"] = doctor_mod.readiness()
        _READINESS["at"] = now
    return _READINESS["data"]


def collect_state() -> Dict[str, Any]:
    ledger = summary()
    stats = analytics()
    farm = overview()

    queue = []
    contests = []
    for row in top_opportunities(limit=60):
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        item = {
            "id": row["id"],
            "channel": row["channel"],
            "title": row["title"],
            "url": row["url"],
            "repo": row["repo"],
            "reward_usd": row["reward_usd"],
            "reward_source": row["reward_source"],
            "score": row["score"],
            "rationale": row["rationale"],
            "fetched_at": row["fetched_at"],
            "playbook": payload.get("playbook", ""),
            "ev_per_hour": payload.get("ev_per_hour", 0.0),
            "expected_value_usd": payload.get("expected_value_usd", 0.0),
            "effort_hours": payload.get("effort_hours", 0.0),
            "triage": payload.get("triage_verdict", ""),
            "triage_notes": payload.get("triage_notes", []),
            "attempts": payload.get("attempts"),
            "open_prs": payload.get("open_prs"),
            "funder": payload.get("funder"),
        }
        (contests if row["channel"] == "audit_contests" else queue).append(item)

    with _RUNNER_LOCK:
        runner = {
            "running": _RUNNER["running"],
            "started_at": _RUNNER["started_at"],
            "finished_at": _RUNNER["finished_at"],
            "result": _RUNNER["result"],
            "error": _RUNNER["error"],
            "log": list(_RUNNER["log"]),
        }

    country = (get_env("ELIGIBILITY_COUNTRY", "") or "").upper()
    return {
        "ledger": ledger,
        "stats": stats,
        "farm": farm,
        "money": payout_rails.recommend(country).as_dict() if country else None,
        "rails": payout_rails.table(),
        "country": country,
        "readiness": _readiness(),
        "autopilot": autopilot_mod.status(),
        "watchdog": watchdog_mod.state(),
        "learning": learning_mod.learned_state(),
        "followup": _followup(),
        "inbox": inbox_mod.pending(limit=10),
        "inbox_counts": inbox_mod.counts(),
        "queue": queue,
        "contests": contests,
        "next": next_actions(limit=5),
        "low_value": low_value(limit=6),
        "payouts": ledger.get("payouts", []),
        "runner": runner,
    }


PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AGENT-0 — ферма воркеров</title>
<style>
  :root {
    --bg:#0b0d10; --panel:#12151a; --panel2:#171b22; --line:#232935;
    --text:#e6e9ef; --dim:#8b94a7; --green:#38d39f; --amber:#f0b429;
    --red:#ff6b6b; --blue:#5aa9ff; --violet:#b98cff;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  header{border-bottom:1px solid var(--line);padding:16px 24px;background:var(--panel);
         display:flex;flex-wrap:wrap;gap:14px;align-items:center;justify-content:space-between}
  h1{font-size:17px;margin:0;letter-spacing:.5px}
  h1 span{color:var(--green)}
  .wrap{padding:20px;max-width:1500px;margin:0 auto}
  .grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(330px,1fr))}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;overflow:hidden}
  .card.wide{grid-column:1/-1}
  .card h2{font-size:12px;margin:0 0 10px;letter-spacing:.8px;text-transform:uppercase;color:var(--dim)}
  .stats{display:flex;flex-wrap:wrap;gap:26px}
  .stat .k{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.6px}
  .stat .v{font-size:21px;margin-top:3px}
  .v.green{color:var(--green)}.v.amber{color:var(--amber)}.v.red{color:var(--red)}.v.blue{color:var(--blue)}
  .muted{color:var(--dim)}
  button{font:inherit;background:var(--panel2);color:var(--text);cursor:pointer;
         border:1px solid var(--line);border-radius:8px;padding:8px 14px}
  button:hover{border-color:var(--green);color:var(--green)}
  button:disabled{opacity:.45;cursor:progress}
  button.primary{border-color:#2b6b56;background:#10352a;color:var(--green)}
  button.tiny{padding:3px 9px;font-size:12px}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th{text-align:left;color:var(--dim);font-weight:normal;font-size:11px;text-transform:uppercase;
     letter-spacing:.5px;padding:5px 6px;border-bottom:1px solid var(--line)}
  td{padding:6px;border-bottom:1px solid #1a1f28;vertical-align:top}
  tr:last-child td{border-bottom:none}
  a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
  code{background:#0d1014;border:1px solid var(--line);border-radius:5px;padding:1px 5px;font-size:12px}
  .pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:10.5px;border:1px solid var(--line);white-space:nowrap}
  .pill.on{color:var(--green);border-color:#2b6b56;background:#10352a}
  .pill.off{color:var(--red);border-color:#5a2b2b;background:#2c1414}
  .pill.wait{color:var(--amber);border-color:#5a4a1f;background:#2b2410}
  .pill.info{color:var(--blue);border-color:#2b4a6b;background:#10202c}
  .pill.aux{color:var(--violet);border-color:#4a3a6b;background:#1d1730}
  .item{border-left:2px solid var(--line);padding:0 0 12px 12px;margin-bottom:12px}
  .item.hot{border-left-color:var(--green)}
  .item.cold{border-left-color:#3a2020;opacity:.75}
  .item .t{font-size:13px}
  .item .m{color:var(--dim);font-size:11.5px;margin-top:3px}
  .log{background:#0d1014;border:1px solid var(--line);border-radius:8px;padding:10px;
       font-size:11.5px;color:var(--dim);max-height:190px;overflow:auto;white-space:pre-wrap}
  .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  input,select{font:inherit;background:#0d1014;color:var(--text);border:1px solid var(--line);
               border-radius:8px;padding:7px;min-width:80px}
  .bar{height:7px;border-radius:4px;background:#232935;overflow:hidden;margin-top:5px}
  .bar > i{display:block;height:100%;background:linear-gradient(90deg,#2b6b56,#38d39f)}
  .foot{color:var(--dim);font-size:12px;margin-top:22px;line-height:1.75}
  details summary{cursor:pointer;color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.6px}
</style>
</head>
<body>
<header>
  <h1>AGENT<span>-0</span> · ферма воркеров</h1>
  <div class="row">
    <span id="runner-pill" class="pill wait">простой</span>
    <select id="channel">
      <option value="">все воркеры</option>
      <option value="github_bounties">GitHub bounty</option>
      <option value="audit_contests">Аудит-контесты</option>
      <option value="agent_marketplaces">Площадки для агентов</option>
      <option value="bug_recon">Разведка (нужен scope)</option>
    </select>
    <button id="run" class="primary">Запустить цикл</button>
  </div>
</header>

<div class="wrap">
  <div class="card wide" style="margin-bottom:14px">
    <div class="stats">
      <div class="stat"><div class="k">Подтверждённый доход</div><div class="v green" id="s-verified">$0.00</div></div>
      <div class="stat"><div class="k">Заявлено, не подтверждено</div><div class="v amber" id="s-claimed">$0.00</div></div>
      <div class="stat"><div class="k">Эффективная ставка</div><div class="v" id="s-rate">$0.00/ч</div></div>
      <div class="stat"><div class="k">Оценка конвейера</div><div class="v blue" id="s-ev">$0.00</div></div>
      <div class="stat"><div class="k">Часов залогировано</div><div class="v" id="s-hours">0</div></div>
      <div class="stat"><div class="k">Отсеяно триажем</div><div class="v red" id="s-dropped">0</div></div>
    </div>
    <div class="muted" style="margin-top:10px;font-size:11.5px" id="s-note"></div>
  </div>

  <div class="card wide" style="margin-bottom:14px">
    <h2>Три направления — работают параллельно</h2>
    <div id="directions"></div>
    <div class="muted" style="font-size:11.5px;margin-top:6px" id="advice"></div>
  </div>

  <div class="card wide" style="margin-bottom:14px">
    <h2>Автопилот и очередь к публикации</h2>
    <div id="autopilot"></div>
    <div id="inbox" style="margin-top:10px"></div>
  </div>

  <div class="card wide" style="margin-bottom:14px">
    <h2>Взятые задачи: не отдали ли их, пока вы работаете</h2>
    <div id="followup"></div>
  </div>

  <div class="card wide" style="margin-bottom:14px">
    <h2>Дозор: что сломалось, пока вас не было</h2>
    <div id="watchdog"></div>
  </div>

  <div class="card wide" style="margin-bottom:14px">
    <h2>Чему научился робот</h2>
    <div id="learning"></div>
  </div>

  <div class="card wide" style="margin-bottom:14px">
    <h2>Готовность: что мешает заработать</h2>
    <div id="readiness"></div>
    <div class="row" style="margin-top:8px">
      <button class="tiny" id="refresh-doctor">Проверить заново</button>
      <span class="muted" style="font-size:11.5px">Проверка делает реальный запрос к GitHub и смотрит настройки.</span>
    </div>
    <details style="margin-top:10px">
      <summary class="muted" style="cursor:pointer;font-size:12px">Путь к первой выплате — 7 шагов</summary>
      <div id="start-plan" class="muted" style="font-size:12px;margin-top:8px"></div>
    </details>
  </div>

  <div class="grid" style="margin-bottom:14px">
    <div class="card wide">
      <h2>Как получить деньги</h2>
      <div class="row" style="margin-bottom:10px">
        <input id="country" placeholder="страна, напр. RU или DE" maxlength="2" style="min-width:150px">
        <button class="tiny" id="check-country">Проверить</button>
        <span class="muted" style="font-size:11.5px">
          Stripe-выплаты (Algora, Opire) работают не во всех странах — это проверяется здесь.
        </span>
      </div>
      <div id="money"></div>
    </div>
  </div>

  <div class="card wide" style="margin-bottom:14px">
    <h2>Что делать сейчас — по убыванию ожидаемой ценности часа</h2>
    <div id="next"></div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>Аудит-контесты (высокий потолок)</h2>
      <div id="contests"></div>
    </div>
    <div class="card">
      <h2>Не тратить время — и почему</h2>
      <div id="lowvalue"></div>
    </div>
  </div>

  <div class="card wide" style="margin-top:14px">
    <h2>Очередь возможностей</h2>
    <div id="queue"></div>
  </div>

  <div class="grid" style="margin-top:14px">
    <div class="card">
      <h2>По каналам</h2>
      <div id="channels"></div>
    </div>
    <div class="card">
      <h2>Бухгалтерия</h2>
      <div id="payouts"></div>
      <div class="row" style="margin-top:10px">
        <input id="p-channel" placeholder="канал" value="github_bounties">
        <input id="p-amount" placeholder="сумма" type="number" step="0.01" style="min-width:80px">
        <input id="p-evidence" placeholder="доказательство" style="min-width:150px">
        <button class="tiny" id="p-add">Добавить заявку</button>
      </div>
      <div class="muted" style="font-size:11.5px;margin-top:8px">
        Доход попадает в «подтверждённый» только после вашей проверки.
      </div>
    </div>
    <div class="card">
      <h2>Журнал воркеров</h2>
      <div class="log" id="log">—</div>
    </div>
  </div>

  <div class="grid" style="margin-top:14px">
    <div class="card">
      <h2>Политика: разрешено</h2>
      <div id="allowed"></div>
    </div>
    <div class="card">
      <h2>Заблокировано — и почему</h2>
      <div id="denied"></div>
    </div>
    <div class="card">
      <h2>Разбор вашего запроса</h2>
      <div id="requested"></div>
    </div>
  </div>

  <div class="foot">
    Ферма работает только там, где платят за проверяемый результат по публичным правилам.
    Никаких капч, сибил-кошельков, airdrop-фарма и эксплуатации чужих систем —
    причины расписаны в блоке «Заблокировано».<br>
    «Оценка конвейера» — это ожидаемая выручка из вероятности успеха и сумм наград, а не гарантия.
    Подтверждение выплаты — действие человека: <code>python -m agent.main выплата-подтвердить &lt;id&gt;</code>.
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const money = (v) => "$" + (Number(v) || 0).toLocaleString("en-US",
  {minimumFractionDigits: 2, maximumFractionDigits: 2});
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const triagePill = (t) => {
  const map = {ready:"on", contested:"wait", paid:"off", closed:"off", assigned:"off"};
  const cls = map[t] || "info";
  return t ? `<span class="pill ${cls}">${esc(t)}</span>` : `<span class="pill info">нет триажа</span>`;
};

async function post(url, body) {
  await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body || {})});
  refresh();
}
$("run").onclick = () => post("/api/run", {channel: $("channel").value || null});
$("refresh-doctor").onclick = async () => {
  $("refresh-doctor").textContent = "Проверяю…";
  try { const data = await (await fetch("/api/doctor")).json(); renderReadiness(data); }
  finally { $("refresh-doctor").textContent = "Проверить заново"; }
};

$("check-country").onclick = () => loadMoney(($("country").value || "").trim().toUpperCase());
$("country").addEventListener("keydown", (e) => { if (e.key === "Enter") $("check-country").click(); });

async function loadMoney(country) {
  const url = "/api/payouts" + (country ? ("?country=" + encodeURIComponent(country)) : "");
  try {
    const data = await (await fetch(url)).json();
    renderMoney(data);
  } catch (e) { /* keep previous view */ }
}

function renderAutopilot(a, counts) {
  if (!a) { $("autopilot").innerHTML = ""; return; }
  const label = {running: ["on", "работает"], idle: ["info", "не запускался"],
                 stale: ["off", "молчит"], stopped: ["off", "остановлен"]}[a.state] || ["info", a.state];
  $("autopilot").innerHTML = `
    <div class="item ${a.state === "running" ? "hot" : "cold"}">
      <div class="t"><span class="pill ${label[0]}">${label[1]}</span>
        <b>интервал ${a.interval}с</b> (${a.min_interval}–${a.max_interval}с) ·
        проходов ${a.ticks} · подготовлено ${a.prepared_total}</div>
      <div class="m">${a.last_tick ? "последний проход " + esc(a.last_tick) + " (" + a.last_tick_age_s + "с назад)" : "проходов ещё не было"}</div>
      ${a.stop_reason ? `<div class="m">остановлен: ${esc(a.stop_reason)} · снять: rm -f data/STOP</div>` : ""}
      <div class="m">Робот ищет, проверяет, планирует и пишет черновики. Публикация, отправка и подтверждение выплат — ваши.</div>
    </div>`;
}

function renderInbox(items, counts) {
  if (!counts) { $("inbox").innerHTML = ""; return; }
  const rows = (items || []).map(i => `
    <div class="item ${i.kind === "brief" ? "" : "hot"}">
      <div class="t"><span class="pill info">${esc(i.kind_title)}</span> <b>${esc(i.title)}</b></div>
      <div class="m">${esc(i.summary)}</div>
      <div class="m">Действие: ${esc(i.action)}</div>
      <div class="row" style="margin-top:6px">
        <button class="tiny" onclick="showText(${i.id})">Показать текст</button>
        <button class="tiny" onclick="resolveItem(${i.id}, 'published')">Отправлено</button>
        <button class="tiny" onclick="resolveItem(${i.id}, 'skipped')">Пропустить</button>
      </div>
    </div>`).join("");
  $("inbox").innerHTML = `<div class="muted" style="font-size:12px;margin-bottom:6px">
      Готово к отправке: <b>${counts.ready}</b> · отправлено: ${counts.published} · пропущено: ${counts.skipped}</div>
    ${rows || '<div class="muted">Очередь пуста — запустите проход кнопкой «Прогнать цикл» или автопилотом.</div>'}
    <pre id="inbox-text" style="display:none;white-space:pre-wrap;font-size:11.5px;margin-top:8px"></pre>`;
}

async function showText(id) {
  const box = $("inbox-text");
  const text = await (await fetch("/api/inbox/" + id + "/text")).text();
  box.textContent = text;
  box.style.display = "block";
  try { await navigator.clipboard.writeText(text); box.textContent = "Скопировано в буфер.\n\n" + text; } catch (e) {}
}

async function resolveItem(id, status) {
  await fetch("/api/inbox/resolve", {method: "POST", headers: {"Content-Type": "application/json"},
                                     body: JSON.stringify({id, status})});
  refresh();
}

function renderFollowup(data) {
  if (!data) { $("followup").innerHTML = ""; return; }
  if (!data.watched) {
    $("followup").innerHTML = '<div class="muted">Задач в работе нет. Слежение включается, ' +
      'когда вы отмечаете задачу: python -m agent.main статус &lt;id&gt; working</div>';
    return;
  }
  const styles = {ok: ["on", "в порядке"], rival: ["info", "появился соперник"],
                  stale: ["info", "затишье"], closed: ["off", "закрыта"],
                  lost: ["off", "выплата ушла"], unknown: ["info", "нет данных"]};
  const rows = (data.items || []).map(w => {
    const [cls, label] = styles[w.verdict] || ["info", w.verdict];
    return `<div class="item ${w.verdict === "ok" ? "hot" : "cold"}">
      <div class="t"><span class="pill ${cls}">${label}</span> <b>${esc(w.title)}</b></div>
      <div class="m">${money(w.reward_usd)} · статус ${esc(w.status)} · заявок ${w.attempts} ·
        открытых PR ${w.open_prs}</div>
      ${w.reason ? `<div class="m">${esc(w.reason)}</div>` : ""}
      ${w.action && w.verdict !== "ok" ? `<div class="m"><b>Что делать:</b> ${esc(w.action)}</div>` : ""}
    </div>`;
  }).join("");
  $("followup").innerHTML = rows;
}

function renderWatchdog(data) {
  if (!data) { $("watchdog").innerHTML = ""; return; }
  const level = {ok: ["on", "всё в порядке"], warning: ["info", "есть замечания"],
                 critical: ["off", "нужно вмешательство"]}[data.status] || ["info", data.status];
  const rows = (data.problems || []).map(p => `
    <div class="item ${p.severity === "critical" ? "cold" : ""}">
      <div class="t"><span class="pill ${p.severity === "critical" ? "off" : "info"}">${p.severity === "critical" ? "важно" : "замечание"}</span>
        <b>${esc(p.title)}</b></div>
      <div class="m">${esc(p.detail)}</div>
      <div class="m">Что делать: ${esc(p.advice)}</div>
    </div>`).join("");
  const notes = (data.notes || []).map(n => `<div class="m">• ${esc(n)}</div>`).join("");
  $("watchdog").innerHTML = `<div class="muted" style="font-size:12px;margin-bottom:8px">
      <span class="pill ${level[0]}">${level[1]}</span> проверено ${esc(data.checked_at)}</div>
    ${rows || '<div class="muted">Проблем нет: каналы отвечают, очередь не зависла.</div>'}
    ${notes ? `<div class="muted" style="margin-top:8px">Ждут настройки (не поломка):</div>${notes}` : ""}`;
}

function renderLearning(data) {
  if (!data) { $("learning").innerHTML = ""; return; }
  const queries = (data.queries || []).map(q => `
    <div class="item ${q.weight > 1.1 ? "hot" : ""}">
      <div class="t"><span class="pill info">вес ${q.weight.toFixed(2)}</span> <b>${esc(q.subject)}</b></div>
      <div class="m">новых задач ${q.new_items} за ${q.observations} проходов ·
        отправлено ${q.published} · пропущено ${q.skipped}</div>
    </div>`).join("");
  const playbooks = (data.playbooks || []).map(p =>
    `<div class="m">${esc(p.subject)}: отправлено ${p.published} из ${p.decisions}</div>`).join("");
  $("learning").innerHTML = `<div class="muted" style="font-size:12px;margin-bottom:8px">
      наблюдений ${data.observations} · решений ${data.decisions} · отправлено ${data.published} ·
      заработано ${money(data.earned_usd)}</div>
    ${queries || '<div class="muted">Данных пока нет: обучение включается, когда вы отправите или пропустите первые задания.</div>'}
    ${playbooks ? `<div class="muted" style="margin-top:8px">Типы работ:</div>${playbooks}` : ""}`;
}

function renderReadiness(data) {
  if (!data) { $("readiness").innerHTML = '<span class="muted">Проверка недоступна.</span>'; return; }
  const style = {ok: ["on", "ок"], warn: ["info", "важно"], block: ["off", "блокер"]};
  const rows = (data.checks || []).map(c => {
    const [cls, label] = style[c.status] || ["info", c.status];
    const fix = c.status === "ok" ? "" :
      `<div class="m"><b>Как исправить:</b> ${esc(c.fix)}${c.impact ? ` · ${esc(c.impact)}` : ""}</div>`;
    return `<div class="item ${c.status === "block" ? "cold" : (c.status === "warn" ? "" : "hot")}">
      <div class="t"><span class="pill ${cls}">${label}</span> <b>${esc(c.title)}</b></div>
      <div class="m">${esc(c.detail)}</div>${fix}</div>`;
  }).join("");
  $("readiness").innerHTML = `<div class="muted" style="font-size:12px;margin-bottom:8px">
      <b>${esc(data.verdict)}</b></div>${rows}`;
  $("start-plan").innerHTML = (data.start_plan || []).map(s => esc(s)).join("<br>");
}

function renderMoney(data) {
  if (!data || !data.country) {
    $("money").innerHTML = '<span class="muted">Укажите страну — покажем рабочие каналы получения денег '
      + 'и то, что заведомо не сработает.</span>';
    return;
  }
  const rails = data.rails || {};
  const blocked = (data.blocked || []).map(k => `<div class="item cold" style="margin-bottom:8px">
      <span class="pill off">не работает</span> <b>${esc(rails[k] ? rails[k].title : k)}</b>
      <div class="m">${esc(rails[k] ? rails[k].summary : "")}</div></div>`).join("");
  const recommended = (data.recommended || []).map(k => {
    const r = rails[k];
    if (!r) return "";
    return `<div class="item hot">
      <div class="t"><span class="pill on">рабочий канал</span> <b>${esc(r.title)}</b></div>
      <div class="m">${esc(r.summary)}</div>
      <div class="m">Требуется: ${esc((r.requires || []).join(", ") || "ничего особенного")} · Стоимость: ${esc(r.costs)}</div>
      <div class="m">${(r.steps || []).map(s => "· " + esc(s)).join("<br>")}</div>
      <div class="m">Ломается, если: ${esc((r.breaks_when || []).join("; "))}</div>
      <div class="m"><a href="${esc(r.source)}" target="_blank" rel="noopener">источник</a></div>
    </div>`;
  }).join("");
  const notes = (data.notes || []).map(n => `<div class="m">• ${esc(n)}</div>`).join("");
  $("money").innerHTML = `<div class="muted" style="font-size:11.5px;margin-bottom:8px">
      Страна: <b>${esc(data.country)}</b> · проверено ${esc(data.verified_on)}
      (не юридическая консультация)</div>${blocked}${recommended}${notes}`;
}
$("p-add").onclick = () => {
  const amount = parseFloat($("p-amount").value);
  if (!amount) return;
  post("/api/payout", {channel: $("p-channel").value || "manual", amount: amount,
                       evidence: $("p-evidence").value || "", note: "внесено через дашборд"});
  $("p-amount").value = ""; $("p-evidence").value = "";
};

async function refresh() {
  let s;
  try { s = await (await fetch("/api/state")).json(); } catch (e) { return; }
  // Неполный ответ (ошибка сервера, оборванная связь) не должен ломать страницу:
  // показываем, что обновление не прошло, и ждём следующего круга.
  const NEED = ["ledger", "stats", "runner", "farm", "queue", "next", "low_value", "contests"];
  const missing = !s ? NEED.slice() : NEED.filter((k) => s[k] == null);
  if (missing.length) {
    const note = $("s-note");
    if (note) note.textContent = "Не удалось получить состояние фермы (нет полей: "
      + missing.join(", ") + ") — обновление пропущено.";
    return;
  }
  const L = s.ledger, A = s.stats, R = s.runner;

  $("s-verified").textContent = money(A.verified_usd);
  $("s-claimed").textContent = money(A.claimed_usd);
  $("s-rate").innerHTML = money(A.effective_usd_per_hour) + "/ч";
  $("s-rate").className = "v " + (A.effective_usd_per_hour >= 10 ? "green"
      : (A.effective_usd_per_hour > 0 ? "amber" : "red"));
  $("s-ev").textContent = money(A.pipeline_ev_usd) + " / " + money(A.pipeline_ev_per_hour) + "ч";
  $("s-hours").textContent = A.hours_logged;
  $("s-dropped").textContent = L.opportunities_dropped;
  const maxEv = Math.max(1, ...(s.queue || []).map(q => q.ev_per_hour || 0));
  $("s-note").textContent = `В очереди ${L.opportunities_queued} задач · привлекательных для работы: `
    + `${A.attractive_count}, отклонено по ценности: ${A.rejected_count} · ` + A.estimate_note;

  const pill = $("runner-pill");
  if (R.running) { pill.className = "pill wait"; pill.textContent = "работает"; }
  else if (R.error) { pill.className = "pill off"; pill.textContent = "ошибка"; }
  else { pill.className = "pill on"; pill.textContent = "готов"; }
  $("run").disabled = R.running;
  $("log").textContent = R.log.length ? R.log.join("\n") : "—";

  renderReadiness(s.readiness);
  renderAutopilot(s.autopilot, s.inbox_counts);
  renderFollowup(s.followup);
  renderWatchdog(s.watchdog);
  renderLearning(s.learning);
  renderInbox(s.inbox, s.inbox_counts);

  const D = s.farm.directions || {directions: [], advice: []};
  $("directions").innerHTML = (D.directions || []).map(d => `
    <div class="item ${d.ev_per_hour >= 10 ? "hot" : ""}">
      <div class="t"><b>${esc(d.title)}</b> <span class="pill info">${(d.share * 100).toFixed(0)}% времени</span></div>
      <div class="m">${esc(d.promise)}</div>
      <div class="m">Потолок: ${esc(d.ceiling)} · вход: ${esc(d.entry_cost)}</div>
      <div class="m">Выплата: ${esc(d.payout_rail)}</div>
      <div class="m">Сейчас: <b>${esc(d.status)}</b>
        ${d.ev_per_hour ? ` · лучший EV/час <b>${money(d.ev_per_hour)}</b>` : ""}
        ${d.verified_usd ? ` · заработано ${money(d.verified_usd)}` : ""}</div>
      <div class="m">${esc(d.next_step)}</div>
    </div>`).join("");
  $("advice").innerHTML = (D.advice || []).map(a => "• " + esc(a)).join("<br>");

  $("next").innerHTML = s.next.length ? s.next.map((n, i) => `
    <div class="item hot">
      <div class="t"><b>${i+1}.</b> ${n.url ? `<a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a>` : esc(n.title)}</div>
      <div class="m">
        <b>${money(n.reward_usd)}</b> · плейбук ${esc(n.playbook)} · ${triagePill(n.triage)} ·
        EV/час <b>${money(n.ev_per_hour)}</b> · оценка ${n.effort_hours}ч ·
        ожидаемо ${money(n.expected_value_usd)}
      </div>
      <div class="m">${esc(n.why)}</div>
      <div class="m">первый шаг: <code>python -m agent.main досье ${esc(n.id)}</code></div>
    </div>`).join("")
    : '<span class="muted">Пока нечего рекомендовать: запустите цикл или подождите свежих задач.</span>';

  $("lowvalue").innerHTML = s.low_value.length ? s.low_value.map(v => `
    <div class="item cold">
      <div class="t">${esc(v.title)}</div>
      <div class="m">${money(v.reward_usd)} · ${triagePill(v.triage)} · EV/час ${money(v.ev_per_hour)}
        ${v.attempts != null ? ` · заявок ${v.attempts}` : ""}
        ${v.open_prs ? ` · открытых PR ${v.open_prs}` : ""}</div>
    </div>`).join("")
    : '<span class="muted">Отклонённых задач нет.</span>';

  $("contests").innerHTML = s.contests.length ? s.contests.map(c => `
    <div class="item">
      <div class="t">${c.url ? `<a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.title)}</a>` : esc(c.title)}</div>
      <div class="m">EV/час <b>${money(c.ev_per_hour)}</b> · оценка ${c.effort_hours}ч · ожидаемо ${money(c.expected_value_usd)}</div>
      <div class="m">${esc(c.rationale)}</div>
      <div class="bar"><i style="width:${Math.min(100, (c.ev_per_hour / maxEv) * 100)}%"></i></div>
    </div>`).join("")
    : '<span class="muted">Контесты не найдены. Запустите цикл.</span>';

  $("queue").innerHTML = s.queue.length ? `<table>
    <tr><th>EV/час</th><th>награда</th><th>задача</th><th>плейбук</th><th>триаж</th><th>конкуренция</th></tr>
    ${s.queue.map(q => `<tr>
      <td>${money(q.ev_per_hour)}</td>
      <td>${money(q.reward_usd)}</td>
      <td>${q.url ? `<a href="${esc(q.url)}" target="_blank" rel="noopener">${esc(q.title)}</a>` : esc(q.title)}
          <div class="muted" style="font-size:11px">${esc(q.repo || q.channel)}</div></td>
      <td>${esc(q.playbook || "—")}</td>
      <td>${triagePill(q.triage)}</td>
      <td>${q.attempts != null ? `заявок ${q.attempts}` : "—"}${q.open_prs ? ` · PR ${q.open_prs}` : ""}</td>
    </tr>`).join("")}</table>` : '<span class="muted">Пусто. Запустите цикл.</span>';

  $("channels").innerHTML = A.by_channel.length ? `<table>
    <tr><th>канал</th><th>в очереди</th><th>заявлено</th><th>доход</th><th>$ / час</th></tr>
    ${A.by_channel.map(c => `<tr>
      <td>${esc(c.channel)}</td><td>${c.queued}</td><td>${money(c.advertised_usd)}</td>
      <td>${money(c.verified_usd)}</td><td>${money(c.effective_usd_per_hour)}</td></tr>`).join("")}
    </table>` : '<span class="muted">Нет данных.</span>';

  $("payouts").innerHTML = L.payouts.length ? `<table>
    <tr><th>#</th><th>канал</th><th>сумма</th><th>статус</th><th></th></tr>
    ${L.payouts.map(p => `<tr>
      <td>${p.id}</td><td>${esc(p.channel)}</td><td>${money(p.amount)}</td>
      <td>${p.verified ? '<span class="pill on">подтверждено</span>' : '<span class="pill wait">заявка</span>'}</td>
      <td>${p.verified ? "" : `<button class="tiny" onclick="post('/api/payout/verify',{id:${p.id}})">подтвердить</button>`}</td>
    </tr>`).join("")}</table>`
    : '<span class="muted">выплат пока нет — это честнее, чем нарисованные цифры</span>';

  $("allowed").innerHTML = s.farm.policy.allowed.map(a => `
    <div style="margin-bottom:9px">
      <span class="pill on">разрешено</span> ${esc(a.label)}
      <div class="muted" style="font-size:11.5px">${esc(a.note || "")}
        ${a.requires_human ? " · нужен человек" : ""}
        ${a.requires_authorization ? " · нужен файл авторизации" : ""}</div>
    </div>`).join("");

  $("denied").innerHTML = s.farm.policy.denied.map(d => `
    <div style="margin-bottom:11px">
      <span class="pill off">заблокировано</span> <b>${esc(d.label)}</b>
      <div class="muted" style="font-size:11.5px"><b>Почему не работает.</b> ${esc(d.why_it_fails)}</div>
      <div class="muted" style="font-size:11.5px"><b>Риск.</b> ${esc(d.risk)}</div>
      <div class="muted" style="font-size:11.5px"><b>Вместо этого.</b> ${esc(d.instead)}</div>
    </div>`).join("");

  $("requested").innerHTML = s.farm.requested.map(r => {
    const cls = r.status === "allowed" ? "on" : (r.status.includes("human") ? "wait" : "off");
    return `<div style="margin-bottom:9px">
      <span class="pill ${cls}">${esc(r.status)}</span> <b>${esc(r.request)}</b>
      <div class="muted" style="font-size:11.5px">${esc(r.comment)}</div></div>`;
  }).join("");
}
refresh();
setInterval(refresh, 2500);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "AGENT-0"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Dict[str, Any], code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/doctor":
            self._json(_readiness(refresh=True))
            return
        if path == "/api/payouts":
            query = parse_qs(parsed.query) if (parsed := urlparse(self.path)) else {}
            country = (query.get("country", [""])[0] or get_env("ELIGIBILITY_COUNTRY", "") or "").upper()
            assessment = payout_rails.recommend(country).as_dict()
            assessment["rails"] = {item["key"]: item for item in payout_rails.table()}
            self._json(assessment)
            return
        if path == "/api/state":
            self._json(collect_state())
            return
        if path == "/healthz":
            data = autopilot_mod.health()
            self._json(data, 200 if data["status"] == "ok" else 503)
            return
        if path == "/health":
            self._json({"ok": True})
            return
        if path == "/api/inbox":
            self._json({"counts": inbox_mod.counts(), "items": inbox_mod.pending(limit=20)})
            return
        if path.startswith("/api/inbox/") and path.endswith("/text"):
            try:
                item_id = int(path.split("/")[3])
            except (IndexError, ValueError):
                self._json({"error": "bad id"}, 400)
                return
            self._send(200, inbox_mod.text(item_id).encode("utf-8"),
                       "text/plain; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/api/inbox/resolve":
            body = self._read_json()
            try:
                item_id = int(body.get("id"))
            except (TypeError, ValueError):
                self._json({"error": "id required"}, 400)
                return
            status = str(body.get("status") or "published")
            if status not in inbox_mod.STATUSES:
                self._json({"error": f"status must be one of {inbox_mod.STATUSES}"}, 400)
                return
            self._json({"resolved": inbox_mod.resolve(item_id, status)})
            return

        if parsed.path == "/api/run":
            body = self._read_json()
            channel = body.get("channel") or (query.get("channel", [None])[0])
            if channel in ("", "null", "None"):
                channel = None
            self._json({"started": start_cycle(channel), "channel": channel})
            return

        if parsed.path == "/api/payout":
            body = self._read_json()
            try:
                amount = float(body.get("amount"))
            except (TypeError, ValueError):
                self._json({"error": "amount required"}, 400)
                return
            payout_id = record_payout(
                channel=str(body.get("channel") or "manual"),
                amount=amount,
                currency=str(body.get("currency") or "USD"),
                evidence=str(body.get("evidence") or ""),
                note=str(body.get("note") or ""),
            )
            self._json({"id": payout_id, "verified": False})
            return

        if parsed.path == "/api/payout/verify":
            body = self._read_json()
            try:
                payout_id = int(body.get("id"))
            except (TypeError, ValueError):
                self._json({"error": "id required"}, 400)
                return
            self._json({"id": payout_id, "verified": verify_payout(payout_id)})
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")


def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"AGENT-0 dashboard: http://{host}:{port}")
    print("Ctrl+C — остановка.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    serve()
