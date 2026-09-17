"""Досье: файлы-кандидаты и команды запуска — без выдумывания структуры."""

from __future__ import annotations

import base64
import json
from typing import Any, Dict


from agent import dossier


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


class FakeSession:
    """Отвечает как GitHub: метаданные, дерево и содержимое файлов."""

    def __init__(self, tree: list, files: Dict[str, str] | None = None,
                 repo_meta: Dict[str, Any] | None = None) -> None:
        self.tree = tree
        self.files = files or {}
        self.repo_meta = repo_meta or {}
        self.calls: list[str] = []

    def get(self, url: str, params: Any = None, timeout: int = 0) -> FakeResponse:
        self.calls.append(url)
        if url.endswith("/git/trees/main") or "/git/trees/" in url:
            return FakeResponse({"tree": [{"path": p, "type": "blob"} for p in self.tree]})
        if "/contents/" in url:
            path = url.split("/contents/", 1)[1]
            if path in self.files:
                encoded = base64.b64encode(self.files[path].encode()).decode()
                return FakeResponse({"content": encoded})
            return FakeResponse({}, status_code=404)
        return FakeResponse(self.repo_meta)


def builder(tree: list, files: Dict[str, str] | None = None, meta: Dict[str, Any] | None = None):
    instance = dossier.DossierBuilder()
    instance.session = FakeSession(tree, files, meta)  # type: ignore[assignment]
    return instance


TREE = [
    "README.md", "package.json", "src/index.ts", "src/parser/tokenizer.ts",
    "src/parser/wrapper.ts", "src/utils/helpers.ts", "tests/tokenizer.test.ts",
    "docs/guide.md",
]


def test_candidates_come_from_the_words_of_the_task() -> None:
    card = builder(TREE).build({
        "id": "github:acme/parser#7", "title": "Tokenizer fails on nested wrapper",
        "repo": "acme/parser", "payload": {"body": "see src/parser/tokenizer.ts"},
    })
    paths = [item["path"] for item in card.candidates]
    assert "src/parser/tokenizer.ts" in paths
    assert card.mentioned == ["src/parser/tokenizer.ts"], "упомянутый файл найден точно"


def test_commands_come_from_manifests_not_from_imagination() -> None:
    manifest = json.dumps({"scripts": {"test": "jest", "build": "tsc"}})
    card = builder(TREE, {"package.json": manifest}).build({
        "id": "github:acme/parser#7", "title": "Fix tokenizer", "repo": "acme/parser",
        "payload": {},
    })
    assert "npm run test" in card.commands
    assert "npm run build" in card.commands


def test_layout_separates_files_from_directories() -> None:
    card = builder(TREE).build({
        "id": "github:acme/parser#7", "title": "Fix", "repo": "acme/parser", "payload": {},
    })
    layout = " ".join(card.layout)
    assert "src/ — 4 файлов" in layout
    assert "README.md — файл в корне" in layout


def test_missing_tree_is_reported_honestly() -> None:
    instance = dossier.DossierBuilder()
    instance.session = FakeSession([], {}, {})  # type: ignore[assignment]
    instance.session.tree = None  # type: ignore[assignment]

    class Broken(FakeSession):
        def get(self, url: str, params: Any = None, timeout: int = 0) -> FakeResponse:
            return FakeResponse({}, status_code=404)

    instance.session = Broken([])  # type: ignore[assignment]
    card = instance.build({"id": "github:acme/parser#7", "title": "Fix",
                           "repo": "acme/parser", "payload": {}})
    assert card.tree_available is False
    assert any("дерево" in note for note in card.notes)
    assert "дерево недоступно" in card.to_markdown()


def test_scope_file_gives_file_list_and_code_repository() -> None:
    scope = "./crates/library/src/lib.rs\n./programs/vaults/src/lib.rs\n./programs/vaults/src/state.rs\n"
    readme = "Details\nCode: https://github.com/acme/protocol-core\n"
    card = builder(
        ["README.md", "scope.txt", "out_of_scope.txt"],
        {"scope.txt": scope, "README.md": readme,
         "out_of_scope.txt": "./tests/fixtures.rs\n"},
    ).build({"id": "contest:acme/2026-01-x", "title": "Code4rena: 2026-01-x",
             "repo": "acme/2026-01-x", "payload": {}})
    assert card.scope_files == ["crates/library/src/lib.rs", "programs/vaults/src/lib.rs",
                                "programs/vaults/src/state.rs"]
    assert card.scope_repos == ["acme/protocol-core"]
    text = "\n".join(card.scope_notes)
    assert "файлов в области проверки: 3" in text
    assert "вне области проверки: 1" in text


def test_paths_from_lines_handles_variants() -> None:
    text = "./a/b.rs\nsrc/c.sol, 250\nпросто текст без пути\n- src/d.ts (120 nSLOC)\n\n"
    assert dossier.paths_from_lines(text) == ["a/b.rs", "src/c.sol", "src/d.ts"]


def test_markdown_always_tells_what_to_attach_to_the_pr() -> None:
    card = builder(TREE).build({
        "id": "github:acme/parser#7", "title": "Fix tokenizer", "repo": "acme/parser",
        "payload": {},
    })
    text = card.to_markdown()
    assert "## Что приложить к PR" in text
    assert "Fixes #<номер>" in text
    assert "воспроизвести" in text.lower()


SOURCES = [
    "README.md",
    "docs/solidity.md",
    ".agents/skills/solidity-auditor/SKILL.md",
    "tare-io__tare-contracts/contracts/Loans.sol",
    "tare-io__tare-contracts/contracts/PortfolioVault.sol",
    "tare-io__tare-contracts/contracts/NavCalculator.sol",
    "tare-io__tare-contracts/src/anvil.ts",
]


def test_source_files_rank_above_documentation() -> None:
    card = builder(SOURCES).build({
        "id": "contest:acme/2026-01-x", "title": "Solidity contest for tare contracts",
        "repo": "acme/2026-01-x", "payload": {},
    })
    paths = [item["path"] for item in card.candidates]
    assert paths[0].endswith(".sol"), paths
    assert all(".agents/" not in path for path in paths[:3])


def test_nested_code_root_is_named() -> None:
    card = builder(SOURCES).build({
        "id": "contest:acme/2026-01-x", "title": "Solidity contest", "repo": "acme/2026-01-x",
        "payload": {},
    })
    assert card.code_root == "tare-io__tare-contracts"
    assert card.source_counts.get("Solidity") == 3
    assert "Корень кода" in card.to_markdown()


def test_dominant_src_directory_is_named_as_the_code_root() -> None:
    card = builder(TREE).build({
        "id": "github:acme/parser#7", "title": "Fix tokenizer", "repo": "acme/parser",
        "payload": {},
    })
    assert card.code_root == "src", "почти все исходники лежат в src/"


def test_repository_with_code_in_the_root_has_no_code_root() -> None:
    card = builder(["main.py", "utils.py", "tests/test_main.py"]).build({
        "id": "github:acme/tool#3", "title": "Fix utils", "repo": "acme/tool", "payload": {},
    })
    assert card.code_root == "", "если код в корне, отдельного корня кода нет"
    assert card.source_counts.get("Python") == 3


def test_dominant_language_decides_which_files_come_first() -> None:
    tree = ["contracts/A.sol", "contracts/B.sol", "contracts/C.sol", "scripts/build.ts"]
    card = builder(tree).build({
        "id": "contest:acme/2026-01-x", "title": "Solidity audit of contracts",
        "repo": "acme/2026-01-x", "payload": {},
    })
    assert card.candidates[0]["path"].endswith(".sol")
