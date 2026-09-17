"""Описание задачи: робот должен читать условия, а не догадываться о них.

Карточка в выдаче говорит «$199, две работы, 29 дней» — этого хватает, чтобы
оценить выгоду, и мало, чтобы взяться за работу. Условия лежат на странице
задачи, и снимает их раннер GitHub Actions (из песочницы домен закрыт): оттуда
они попадают в отчёт, из отчёта — в черновик работы.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import task_detail

META_PAGE = """<!doctype html><html><head>
<meta name="description" content="Improve Yukon's QSB CUDA implementations and submit evidence before the deadline.">
<title>Quantum-Safe Bitcoin</title></head><body>…</body></html>"""

OG_PAGE = """<html><head>
<meta property="og:description" content="Share 199 USDC for verified Yukon improvements, split by points.">
</head><body></body></html>"""

JSON_LD_PAGE = """<html><head><script type="application/ld+json">
{"@type": "JobPosting", "title": "Task", "description": "Produce promoted Yukon improvements and submit evidence to the platform."}
</script></head><body></body></html>"""

ESCAPED_PAGE = ('<html><body><script>self.__next_f.push([1,"{\\"description\\":'
                '\\"Read the rules, clone the production track, then submit through the CLI.\\"}"])'
                '</script></body></html>')


def test_meta_description_is_read() -> None:
    assert "Improve Yukon's QSB" in task_detail.extract_description(META_PAGE)


def test_og_description_is_read() -> None:
    assert "Share 199 USDC" in task_detail.extract_description(OG_PAGE)


def test_json_ld_description_is_read() -> None:
    assert "Produce promoted Yukon improvements" in task_detail.extract_description(JSON_LD_PAGE)


def test_escaped_json_description_is_read() -> None:
    text = task_detail.extract_description(ESCAPED_PAGE)
    assert "clone the production track" in text


def test_a_page_without_description_gives_nothing() -> None:
    assert task_detail.extract_description("") == ""
    assert task_detail.extract_description("<html><body>ничего</body></html>") == ""


def test_a_short_meta_is_not_taken_for_a_description() -> None:
    """«taskmarket.dev» в мете — это описание сайта, а не условия задачи."""
    page = ('<meta name="description" content="taskmarket.dev">'
            '<script type="application/ld+json">'
            '{"description": "Real conditions live here and they are long enough to use."}'
            '</script>')
    assert "Real conditions" in task_detail.extract_description(page)


def test_description_is_cleaned_and_capped() -> None:
    page = '<meta name="description" content="' + ("шаг " * 2000) + '">'
    text = task_detail.extract_description(page)
    assert len(text) <= task_detail.MAX_DESCRIPTION_CHARS
    assert "\n" not in text


def test_problem_report_explains_a_silent_page() -> None:
    """«0 описаний» на раннере — повод починить разбор, и это видно в логе."""
    assert "не пришла" in task_detail.describe_problem("")
    assert "незнакома" in task_detail.describe_problem("<html>новая разметка</html>")
    assert task_detail.describe_problem(META_PAGE) == "описание найдено"


# --- запрос страницы ----------------------------------------------------------

class FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class FakeSession:
    def __init__(self, text: str = "", status_code: int = 200, boom: bool = False) -> None:
        self.text = text
        self.status_code = status_code
        self.boom = boom
        self.closed = False

    def get(self, url: str, timeout: int = 0) -> FakeResponse:
        if self.boom:
            raise RuntimeError("network down")
        return FakeResponse(self.text, self.status_code)

    def close(self) -> None:
        self.closed = True


def test_fetch_detail_reads_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.channels import taskmarket

    monkeypatch.setattr(taskmarket, "build_session", lambda *a, **k: FakeSession(META_PAGE))
    assert "Improve Yukon" in taskmarket.fetch_detail("0xabc")


def test_fetch_detail_survives_a_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.channels import taskmarket

    monkeypatch.setattr(taskmarket, "build_session", lambda *a, **k: FakeSession(boom=True))
    assert taskmarket.fetch_detail("0xabc") == ""


def test_fetch_detail_ignores_a_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.channels import taskmarket

    monkeypatch.setattr(taskmarket, "build_session",
                        lambda *a, **k: FakeSession("нет", status_code=404))
    assert taskmarket.fetch_detail("0xabc") == ""


def test_fetch_detail_needs_an_id() -> None:
    from agent.channels.taskmarket import fetch_detail

    assert fetch_detail("") == ""


# --- кого читать и куда писать ------------------------------------------------

def test_expensive_tasks_without_a_crowd_are_read_first() -> None:
    rows = [
        {"id": "cheap", "reward_usd": 2.0, "submissions": 0},
        {"id": "rich", "reward_usd": 199.0, "submissions": 2},
        {"id": "rich_crowded", "reward_usd": 150.0, "submissions": 40},
        {"id": "rich_free", "reward_usd": 150.0, "submissions": 0},
        {"id": "broken", "reward_usd": "не число", "submissions": None},
        {"id": "", "reward_usd": 500.0},
    ]
    picked = [row["id"] for row in task_detail.pick_for_detailing(rows, 2)]
    assert picked == ["rich", "rich_free"], picked
    assert task_detail.pick_for_detailing(rows, 0) == []


def test_description_lands_in_the_matching_row() -> None:
    rows = [{"id": "0xa"}, {"id": "0xb"}]
    assert task_detail.apply_description(rows, "0xb", "условия") is True
    assert rows[1]["description"] == "условия"
    assert "description" not in rows[0]
    assert task_detail.apply_description(rows, "0xнет", "условия") is False
    assert task_detail.apply_description(rows, "0xa", "") is False


def test_description_from_row_is_optional() -> None:
    assert task_detail.description_from_row({"description": " текст "}) == "текст"
    assert task_detail.description_from_row({}) is None
    assert task_detail.description_from_row({"description": "   "}) is None


# --- отчёт и черновик ----------------------------------------------------------

class DetailChannel:
    """Канал с описанием: harvest как у Taskmarket, плюс чтение страницы."""

    name = "taskmarket"
    title = "Taskmarket"
    last_error = ""

    def harvest(self, limit: int = 50):
        from agent.ledger import Opportunity

        return [
            Opportunity(id="taskmarket:0xrich", channel="taskmarket", title="Дорогая задача",
                        reward_usd=199.0, url="https://taskmarket.dev/tasks/0xrich",
                        payload={"submissions": 2, "hours_left": 100.0, "reward_usd": 199.0,
                                 "id": "0xrich", "url": "https://taskmarket.dev/tasks/0xrich"}),
            Opportunity(id="taskmarket:0xcheap", channel="taskmarket", title="Дешёвая задача",
                        reward_usd=2.0, url="https://taskmarket.dev/tasks/0xcheap",
                        payload={"submissions": 30, "hours_left": 3.0, "reward_usd": 2.0,
                                 "id": "0xcheap", "url": "https://taskmarket.dev/tasks/0xcheap"}),
        ]

    def fetch_detail(self, task_id: str) -> str:
        return META_PAGE and task_detail.extract_description(META_PAGE) if task_id == "0xrich" else ""


def test_snapshot_stores_descriptions_for_the_top_tasks(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import main, snapshots

    monkeypatch.setattr(snapshots, "project_root", lambda: tmp_path)
    monkeypatch.setattr(main, "project_root", lambda: tmp_path)
    monkeypatch.setattr(main, "build_channel", lambda name: DetailChannel())

    assert main.main(["снапшот", "taskmarket", "--detail", "1"]) == 0
    payload = json.loads((tmp_path / "snapshots" / "taskmarket-report.json")
                         .read_text(encoding="utf-8"))
    assert payload["detail_requested"] == 1
    assert payload["detail_found"] == 1
    rich = next(row for row in payload["tasks"] if row["id"] == "0xrich")
    assert "Improve Yukon's QSB" in rich["description"]
    cheap = next(row for row in payload["tasks"] if row["id"] == "0xcheap")
    assert "description" not in cheap, "описание стоит запроса — читаем только верх списка"


class BlindChannel(DetailChannel):
    """Разметка площадки сменилась: страница приходит, описания в ней нет."""

    def fetch_detail(self, task_id: str) -> str:
        return ""


def test_snapshot_warns_when_the_page_markup_changed(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture) -> None:
    from agent import main, snapshots

    monkeypatch.setattr(snapshots, "project_root", lambda: tmp_path)
    monkeypatch.setattr(main, "project_root", lambda: tmp_path)
    monkeypatch.setattr(main, "build_channel", lambda name: BlindChannel())

    assert main.main(["снапшот", "taskmarket", "--detail", "1"]) == 0
    out = capsys.readouterr().out
    assert "описаний 0 из 1" in out
    assert "разметка страницы изменилась" in out


def test_draft_shows_the_conditions_from_the_platform() -> None:
    from agent.channels.taskmarket import draft_markdown

    text = draft_markdown({
        "id": "taskmarket:0xrich", "title": "Дорогая задача", "reward_usd": 199.0,
        "payload": {"submissions": 1, "hours_left": 40.0,
                    "description": "Read the rules and submit evidence before the deadline."},
    })
    assert "## Что просит площадка" in text
    assert "Read the rules and submit evidence" in text
    assert "сверьте условия и срок" in text


def test_draft_without_a_description_has_no_empty_section() -> None:
    from agent.channels.taskmarket import draft_markdown

    text = draft_markdown({"id": "taskmarket:0xrich", "title": "Задача", "reward_usd": 5.0,
                           "payload": {}})
    assert "## Что просит площадка" not in text
