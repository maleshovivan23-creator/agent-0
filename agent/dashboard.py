"""AGENT-0 dashboard.

A dependency-free HTTP server that renders the farm state: which workers are
allowed, which are refused and why, what opportunities were found, and what
income has actually been verified.

Endpoints:
    GET  /                dashboard
    GET  /api/state       full state as JSON
    POST /api/run         start a farm cycle in the background
    POST /api/payout      record a claimed payout
    POST /api/payout/verify  confirm a payout (this is the human step)

The server binds 0.0.0.0 so it is reachable behind the sandbox preview proxy.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from agent.farm import overview, run_cycle
from agent.ledger import record_payout, summary, top_opportunities, verify_payout

# --------------------------------------------------------------------------
# background runner
# --------------------------------------------------------------------------

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
    _RUNNER["log"] = _RUNNER["log"][-40:]


def _run_cycle_background(channel: Optional[str] = None) -> None:
    try:
        _log(f"цикл запущен (канал: {channel or 'все'})")
        result = run_cycle(channels=[channel] if channel else None, limit=25)
        with _RUNNER_LOCK:
            _RUNNER["result"] = result.as_dict()
            _RUNNER["error"] = None
        _log(f"цикл завершён: найдено {result.found}, сохранено {result.kept}")
        for note in result.notes:
            _log(f"примечание: {note}")
    except Exception as exc:  # keep the dashboard alive no matter what
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
    thread = threading.Thread(target=_run_cycle_background, args=(channel,), daemon=True)
    thread.start()
    return True


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


def collect_state() -> Dict[str, Any]:
    ledger = summary()
    farm = overview()
    queue = []
    for row in top_opportunities(limit=30):
        queue.append(
            {
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
            }
        )
    with _RUNNER_LOCK:
        runner = {
            "running": _RUNNER["running"],
            "started_at": _RUNNER["started_at"],
            "finished_at": _RUNNER["finished_at"],
            "result": _RUNNER["result"],
            "error": _RUNNER["error"],
            "log": list(_RUNNER["log"]),
        }
    return {"ledger": ledger, "farm": farm, "queue": queue, "runner": runner}


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AGENT-0 — ферма воркеров</title>
<style>
  :root {
    --bg: #0b0d10; --panel: #12151a; --panel-2: #171b22; --line: #232935;
    --text: #e6e9ef; --dim: #8b94a7; --green: #38d39f; --amber: #f0b429;
    --red: #ff6b6b; --blue: #5aa9ff;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  header {
    border-bottom: 1px solid var(--line); padding: 18px 24px;
    display: flex; flex-wrap: wrap; gap: 16px; align-items: center;
    justify-content: space-between; background: var(--panel);
  }
  h1 { font-size: 18px; margin: 0; letter-spacing: .5px; }
  h1 span { color: var(--green); }
  .muted { color: var(--dim); }
  .wrap { padding: 24px; max-width: 1400px; margin: 0 auto; }
  .grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 16px; overflow: hidden;
  }
  .card h2 { font-size: 13px; margin: 0 0 12px; letter-spacing: .8px; text-transform: uppercase; color: var(--dim); }
  .stats { display: flex; flex-wrap: wrap; gap: 24px; }
  .stat .k { font-size: 11px; color: var(--dim); text-transform: uppercase; letter-spacing: .6px; }
  .stat .v { font-size: 22px; margin-top: 4px; }
  .v.green { color: var(--green); } .v.amber { color: var(--amber); } .v.red { color: var(--red); }
  button {
    font: inherit; background: var(--panel-2); color: var(--text); cursor: pointer;
    border: 1px solid var(--line); border-radius: 8px; padding: 9px 16px;
  }
  button:hover { border-color: var(--green); color: var(--green); }
  button:disabled { opacity: .45; cursor: progress; }
  button.primary { border-color: #2b6b56; background: #10352a; color: var(--green); }
  button.tiny { padding: 4px 10px; font-size: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--dim); font-weight: normal; font-size: 11px;
       text-transform: uppercase; letter-spacing: .6px; padding: 6px 8px; border-bottom: 1px solid var(--line); }
  td { padding: 8px; border-bottom: 1px solid #1a1f28; vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  a { color: var(--blue); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; border: 1px solid var(--line); }
  .pill.on { color: var(--green); border-color: #2b6b56; background: #10352a; }
  .pill.off { color: var(--red); border-color: #5a2b2b; background: #2c1414; }
  .pill.wait { color: var(--amber); border-color: #5a4a1f; background: #2b2410; }
  .reason { color: var(--dim); font-size: 12px; margin-top: 4px; }
  .q { border-left: 2px solid var(--line); padding-left: 12px; margin-bottom: 14px; }
  .q .t { font-size: 13px; }
  .q .m { color: var(--dim); font-size: 11.5px; margin-top: 3px; }
  .log { background: #0d1014; border: 1px solid var(--line); border-radius: 8px;
         padding: 10px; font-size: 12px; color: var(--dim); max-height: 200px; overflow: auto; white-space: pre-wrap; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  input, select {
    font: inherit; background: #0d1014; color: var(--text); border: 1px solid var(--line);
    border-radius: 8px; padding: 8px; min-width: 90px;
  }
  .score { color: var(--green); }
  .foot { color: var(--dim); font-size: 12px; margin-top: 24px; line-height: 1.7; }
</style>
</head>
<body>
<header>
  <h1>AGENT<span>-0</span> · ферма воркеров</h1>
  <div class="row">
    <span id="runner-pill" class="pill wait">простой</span>
    <button id="run" class="primary">Запустить цикл</button>
    <select id="channel">
      <option value="">все воркеры</option>
      <option value="github_bounties">github_bounties</option>
      <option value="bug_recon">bug_recon</option>
    </select>
  </div>
</header>

<div class="wrap">
  <div class="card" style="margin-bottom:16px">
    <div class="stats">
      <div class="stat"><div class="k">Подтверждённый доход</div><div class="v green" id="s-verified">$0.00</div></div>
      <div class="stat"><div class="k">Заявлено, не подтверждено</div><div class="v amber" id="s-claimed">$0.00</div></div>
      <div class="stat"><div class="k">Возможностей в базе</div><div class="v" id="s-opps">0</div></div>
      <div class="stat"><div class="k">В очереди</div><div class="v" id="s-queue">0</div></div>
      <div class="stat"><div class="k">Отказов политики</div><div class="v red" id="s-blocks">0</div></div>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>Очередь возможностей</h2>
      <div id="queue"><span class="muted">Пусто. Запустите цикл.</span></div>
    </div>

    <div class="card">
      <h2>Журнал воркеров</h2>
      <div class="log" id="log">—</div>
      <h2 style="margin-top:16px">Бухгалтерия</h2>
      <div id="payouts"></div>
      <div class="row" style="margin-top:12px">
        <input id="p-channel" placeholder="канал" value="github_bounties">
        <input id="p-amount" placeholder="сумма" type="number" step="0.01">
        <input id="p-evidence" placeholder="доказательство (tx/инвойс)" style="min-width:200px">
        <button class="tiny" id="p-add">Добавить заявку</button>
      </div>
      <div class="reason">Доход попадает в «подтверждённый» только после проверки человеком.</div>
    </div>
  </div>

  <div class="grid" style="margin-top:16px">
    <div class="card">
      <h2>Разрешённые возможности политики</h2>
      <div id="allowed"></div>
    </div>
    <div class="card">
      <h2>Заблокировано — и почему</h2>
      <div id="denied"></div>
    </div>
    <div class="card">
      <h2>Разбор запроса</h2>
      <div id="requested"></div>
    </div>
  </div>

  <div class="foot">
    Ферма выполняет только работу, за которую платит внешний заказчик по публичным правилам.
    Никаких капч, сибил-кошельков, airdrop-фарма и эксплуатации чужих систем: причины расписаны в блоке «Заблокировано».
    Подтверждение выплаты — действие человека: <code>python -m agent.main payout-verify &lt;id&gt;</code>.
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const money = (v) => "$" + (v || 0).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});

async function post(url, body) {
  await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
  refresh();
}

$("run").onclick = () => post("/api/run", {channel: $("channel").value || null});
$("p-add").onclick = () => {
  const amount = parseFloat($("p-amount").value);
  if (!amount) return;
  post("/api/payout", {
    channel: $("p-channel").value || "manual",
    amount: amount,
    evidence: $("p-evidence").value || "",
    note: "внесено через дашборд"
  });
  $("p-amount").value = ""; $("p-evidence").value = "";
};

async function refresh() {
  let state;
  try {
    state = await (await fetch("/api/state")).json();
  } catch (e) { return; }

  const L = state.ledger, R = state.runner;
  $("s-verified").textContent = money(L.verified_usd);
  $("s-claimed").textContent = money(L.claimed_usd);
  $("s-opps").textContent = L.opportunities_total;
  $("s-queue").textContent = L.opportunities_queued;
  $("s-blocks").textContent = L.policy_blocks.length;

  const pill = $("runner-pill");
  if (R.running) { pill.className = "pill wait"; pill.textContent = "работает"; }
  else if (R.error) { pill.className = "pill off"; pill.textContent = "ошибка"; }
  else { pill.className = "pill on"; pill.textContent = "готов"; }
  $("run").disabled = R.running;

  $("log").textContent = R.log.length ? R.log.join("\n") : "—";

  $("queue").innerHTML = state.queue.length ? state.queue.map(q => `
    <div class="q">
      <div class="t"><span class="score">[${q.score.toFixed(1)}]</span>
        ${q.reward_usd ? `<b>${money(q.reward_usd)}</b>` : `<span class="muted">награда н/д</span>`}
        ${q.url ? `<a href="${q.url}" target="_blank" rel="noopener">${esc(q.title)}</a>` : esc(q.title)}
      </div>
      <div class="m">${esc(q.repo || q.channel)} · ${esc(q.rationale)}</div>
    </div>`).join("") : '<span class="muted">Пусто. Запустите цикл.</span>';

  $("payouts").innerHTML = L.payouts.length ? `<table><tr><th>#</th><th>канал</th><th>сумма</th><th>статус</th><th></th></tr>
    ${L.payouts.map(p => `<tr>
      <td>${p.id}</td><td>${esc(p.channel)}</td><td>$${(p.amount || 0).toFixed(2)}</td>
      <td>${p.verified ? '<span class="pill on">подтверждено</span>' : '<span class="pill wait">заявка</span>'}</td>
      <td>${p.verified ? "" : `<button class="tiny" onclick="post('/api/payout/verify',{id:${p.id}})">подтвердить</button>`}</td>
    </tr>`).join("")}</table>` : '<span class="muted">выплат пока нет — это честнее, чем нарисованные цифры</span>';

  $("allowed").innerHTML = state.farm.policy.allowed.map(a => `
    <div style="margin-bottom:10px">
      <span class="pill on">разрешено</span> ${esc(a.label)}
      <div class="reason">${esc(a.note || "")}
        ${a.requires_human ? ' · требуется подтверждение человека' : ''}
        ${a.requires_authorization ? ' · требуется файл авторизации' : ''}
      </div>
    </div>`).join("");

  $("denied").innerHTML = state.farm.policy.denied.map(d => `
    <div style="margin-bottom:12px">
      <span class="pill off">заблокировано</span> <b>${esc(d.label)}</b>
      <div class="reason"><b>Почему не работает.</b> ${esc(d.why_it_fails)}</div>
      <div class="reason"><b>Риск.</b> ${esc(d.risk)}</div>
      <div class="reason"><b>Делать вместо этого.</b> ${esc(d.instead)}</div>
    </div>`).join("");

  $("requested").innerHTML = state.farm.requested.map(r => {
    const cls = r.status === "allowed" ? "on" : (r.status.includes("human") ? "wait" : "off");
    return `<div style="margin-bottom:10px">
      <span class="pill ${cls}">${esc(r.status)}</span> <b>${esc(r.request)}</b>
      <div class="reason">${esc(r.comment)}</div></div>`;
  }).join("");
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "AGENT-0"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        pass

    # ------------------------------------------------------------------ utils

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # ------------------------------------------------------------------- verbs

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/state":
            self._json(collect_state())
            return
        if parsed.path == "/health":
            self._json({"ok": True})
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/api/run":
            body = self._read_json()
            channel = body.get("channel") or (query.get("channel", [None])[0])
            if channel in ("", "null", "None"):
                channel = None
            started = start_cycle(channel)
            self._json({"started": started, "channel": channel})
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
