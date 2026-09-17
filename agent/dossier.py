"""Досье по задаче: где в репозитории и что запускать.

Дороже всего в bounty — не писать код, а понять, куда его писать. Первый час
уходит на чтение дерева репозитория, поиск нужных модулей и выяснение команды
запуска тестов. Этот час не оплачивается и повторяется в каждой задаче.

Модуль делает его за несколько API-запросов и складывает в досье:

* какие файлы, судя по формулировке задачи, придётся менять (совпадение имён,
  путей и упоминаний в тексте issue);
* как в проекте запускаются тесты и сборка — по манифестам в корне;
* какие файлы приложить к PR и что проверить перед отправкой.

Всё берётся из публичного API GitHub. Если данных нет (нет токена, приватный
репозиторий, лимит), досье честно об этом пишет, а не выдумывает структуру.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from agent.config import get_env
from agent.http import build_session

#: Что считаем «точкой входа» для разных стеков.
MANIFESTS = (
    ("package.json", "npm"),
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("requirements.txt", "python"),
    ("Makefile", "make"),
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
    ("composer.json", "php"),
    ("Gemfile", "ruby"),
    ("build.gradle", "gradle"),
    ("pom.xml", "maven"),
)

#: Тестовые и документационные директории — их полезно знать сразу.
NOTABLE_DIRS = ("test", "tests", "spec", "docs", "src", "lib", "app", "packages")

#: Упоминание файла в тексте задачи: src/foo/bar.ts, app/main.py
FILE_IN_TEXT = re.compile(r"\b([\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|rb|php|java|kt|cs|sol|c|cpp|h|md|json|yaml|yml|toml))\b")

#: Слова из формулировки, которые не помогают искать код.
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "when", "have", "has",
    "not", "are", "was", "were", "will", "would", "should", "could", "into",
    "about", "there", "their", "which", "while", "your", "you", "our", "they",
    "issue", "bug", "fix", "bugfix", "feature", "support", "add", "update",
    "bounty", "please", "thanks", "problem", "error", "не", "или", "для", "как",
    "что", "это", "при", "если", "так", "его", "она", "они", "быть", "надо",
}

TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")

#: Строка со списком файлов: ./src/foo.rs, src/foo.rs, src/foo.rs (250 nSLOC)
SCOPE_LINE = re.compile(
    r"^[\w./-]+\.(?:sol|rs|py|ts|tsx|js|jsx|go|cairo|move|vy|java|kt|cs|c|cc|cpp|h|hpp)$"
)


def paths_from_lines(text: str) -> List[str]:
    """Разобрать список файлов: по одному пути в строке, с ведущими ``./`` и хвостами."""
    result: List[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip().lstrip("-*• \t")
        if not line:
            continue
        # отрезаем хвост с метриками: «src/foo.rs, 250» или «src/foo.rs (250 nSLOC)»
        candidate = re.split(r"[,\s(]", line, maxsplit=1)[0].strip().removeprefix("./")
        if not candidate or "/" not in candidate:
            continue
        if not SCOPE_LINE.match(candidate):
            continue
        if candidate not in result:
            result.append(candidate)
    return result


@dataclass
class Dossier:
    opportunity_id: str
    repo: str
    title: str
    url: str = ""
    language: str = ""
    size_kb: int = 0
    branch: str = ""
    tree_available: bool = False
    total_files: int = 0
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    mentioned: List[str] = field(default_factory=list)
    layout: List[str] = field(default_factory=list)
    manifests: List[str] = field(default_factory=list)
    commands: List[str] = field(default_factory=list)
    code_root: str = ""
    source_counts: Dict[str, int] = field(default_factory=dict)
    scope_repos: List[str] = field(default_factory=list)
    scope_files: List[str] = field(default_factory=list)
    scope_notes: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        parts = [
            f"# Досье по задаче: {self.title}",
            "",
            f"- ID: `{self.opportunity_id}`",
            f"- Репозиторий: [{self.repo}](https://github.com/{self.repo})",
            f"- Язык: {self.language or 'не определён'} · размер: ~{self.size_kb} КБ",
            f"- Ветка по умолчанию: `{self.branch or 'н/д'}` · файлов в дереве: {self.total_files}",
            "",
        ]

        if self.mentioned:
            parts += ["## Файлы, упомянутые в задаче", ""]
            parts += [f"- `{path}`" for path in self.mentioned]
            parts.append("")

        parts += ["## Где, скорее всего, нужно править", ""]
        if self.candidates:
            for index, item in enumerate(self.candidates, 1):
                parts.append(f"{index}. `{item['path']}` — {item['reason']}")
        else:
            parts.append("- Совпадений по названиям не нашлось: смотрите дерево ниже "
                         "и ищите по символам из задачи через поиск GitHub.")
        parts.append("")

        if self.code_root or self.source_counts:
            parts += ["## Код", ""]
            if self.code_root:
                parts.append(f"- Корень кода: `{self.code_root}/` — остальное служебное")
            if self.source_counts:
                shown = ", ".join(
                    f"{name}: {count}" for name, count in
                    sorted(self.source_counts.items(), key=lambda pair: -pair[1])[:6]
                )
                parts.append(f"- Исходные файлы: {shown}")
            parts.append("")

        parts += ["## Структура", ""]
        if self.layout:
            parts += [f"- `{entry}`" for entry in self.layout]
        else:
            parts.append("- дерево недоступно (лимит API или приватный репозиторий)")
        parts.append("")

        if self.scope_repos or self.scope_files or self.scope_notes:
            parts += ["## Код и область проверки (scope)", ""]
            for repo in self.scope_repos:
                parts.append(f"- Код контеста: https://github.com/{repo}")
            if self.scope_files:
                parts.append(f"- Файлов в области проверки: {len(self.scope_files)}")
                for path in self.scope_files[:12]:
                    parts.append(f"  - `{path}`")
                if len(self.scope_files) > 12:
                    parts.append(f"  - …и ещё {len(self.scope_files) - 12}")
            parts += [f"- {note}" for note in self.scope_notes]
            parts.append("")

        parts += ["## Сборка и тесты", ""]
        if self.commands:
            parts += [f"- `{command}`" for command in self.commands]
        else:
            parts.append("- манифесты не найдены: уточните команду запуска в README проекта")
        if self.manifests:
            parts.append("")
            parts.append(f"Манифесты: {', '.join('`' + name + '`' for name in self.manifests)}")
        parts.append("")

        parts += [
            "## Что приложить к PR",
            "",
            "- Минимальный диф: только то, что нужно для задачи.",
            "- Вывод теста до и после исправления (или шаги воспроизведения).",
            "- Ссылку на issue в описании PR: `Fixes #<номер>`.",
            "",
            "## Порядок работы",
            "",
            "1. Прочитать `CONTRIBUTING.md` и правила проекта, если они есть.",
            "2. Воспроизвести проблему до правки — иначе не понять, что исправлено.",
            "3. Правку делать в отдельной ветке, тесты — те, что перечислены выше.",
            "4. Проверить, что диф не трогает посторонние файлы.",
            "",
        ]

        if self.notes:
            parts += ["## Примечания", ""] + [f"- {note}" for note in self.notes] + [""]
        return "\n".join(parts)


class DossierBuilder:
    """Собирает досье из публичного API GitHub. Ничего не пишет на GitHub."""

    def __init__(self, token: str = "") -> None:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.session = build_session("AGENT-0-dossier/1.0", headers)

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            response = self.session.get(url, params=params, timeout=25)
        except Exception:
            return None
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def build(self, opportunity: Dict[str, Any]) -> Dossier:
        payload = opportunity.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload or "{}")
            except Exception:
                payload = {}
        payload = payload or {}

        repo = str(opportunity.get("repo") or "").split("#")[0]
        if not repo and "github:" in str(opportunity.get("id", "")):
            repo = str(opportunity["id"]).split("github:")[1].split("#")[0]

        dossier = Dossier(
            opportunity_id=str(opportunity.get("id")),
            repo=repo,
            title=str(opportunity.get("title") or ""),
            url=str(opportunity.get("url") or ""),
        )
        if not repo:
            dossier.notes.append("репозиторий не определён — досье построено по описанию задачи")
            return dossier

        meta = self._get(f"https://api.github.com/repos/{repo}") or {}
        dossier.language = str(meta.get("language") or "")
        dossier.size_kb = int(meta.get("size") or 0)
        dossier.branch = str(meta.get("default_branch") or "")
        if not meta:
            dossier.notes.append("метаданные репозитория недоступны (лимит API или приватный репозиторий)")

        branch = dossier.branch or "main"
        tree = self._get(
            f"https://api.github.com/repos/{repo}/git/trees/{branch}",
            {"recursive": "1"},
        ) or {}
        entries = tree.get("tree") if isinstance(tree, dict) else None
        if isinstance(entries, list):
            paths = [str(item.get("path")) for item in entries if item.get("type") == "blob"]
            dossier.tree_available = True
            dossier.total_files = len(paths)
            dossier.layout = self._layout(paths)
            keywords = self._keywords(opportunity, payload)
            dossier.mentioned = self._mentioned(opportunity, payload, paths)
            dossier.code_root, dossier.source_counts, dominant = self._code_root(paths)
            dossier.candidates = self._candidates(paths, keywords, prefer_suffix=dominant)
            dossier.manifests = [name for name in (p for p in paths) if "/" not in name
                                 and name in {m[0] for m in MANIFESTS}]
            dossier.commands = self._commands(repo, dossier.manifests, paths)
            if not dossier.candidates and len(paths) <= 30:
                self._read_scope(dossier, repo, paths)
        else:
            dossier.notes.append("дерево файлов недоступно: возможно, исчерпан лимит API")

        if dossier.size_kb > 200_000:
            dossier.notes.append("репозиторий очень большой: ограничьтесь модулем, "
                                 "к которому относится задача")
        return dossier

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _layout(paths: List[str]) -> List[str]:
        """Верхний уровень: отдельно папки, отдельно файлы."""
        dirs: Dict[str, int] = {}
        files: List[str] = []
        for path in paths:
            if "/" in path:
                top = path.split("/", 1)[0]
                dirs[top] = dirs.get(top, 0) + 1
            else:
                files.append(path)
        notable: List[str] = []
        for name in sorted(dirs, key=lambda key: dirs[key], reverse=True)[:12]:
            marker = " ←" if name.lower() in NOTABLE_DIRS else ""
            notable.append(f"{name}/ — {dirs[name]} файлов{marker}")
        for name in sorted(files)[:8]:
            notable.append(f"{name} — файл в корне")
        return notable

    @staticmethod
    def _keywords(opportunity: Dict[str, Any], payload: Dict[str, Any]) -> List[str]:
        text = " ".join(
            str(part or "")
            for part in (
                opportunity.get("title"),
                payload.get("body"),
                payload.get("labels"),
                opportunity.get("rationale"),
            )
        )
        words = {word.lower() for word in TOKEN.findall(text)}
        return sorted(word for word in words if word not in STOPWORDS and len(word) >= 4)

    @staticmethod
    def _mentioned(opportunity: Dict[str, Any], payload: Dict[str, Any],
                   paths: List[str]) -> List[str]:
        """Файлы, прямо упомянутые в задаче, — самый сильный сигнал."""
        text = " ".join(
            str(part or "") for part in (opportunity.get("title"), payload.get("body"))
        )
        known = set(paths)
        found: List[str] = []
        for match in FILE_IN_TEXT.findall(text):
            candidate = match.strip("./")
            if candidate in known and candidate not in found:
                found.append(candidate)
                continue
            # в тексте часто пишут только имя файла — найдём по совпадению хвоста
            suffix_matches = [path for path in paths if path.endswith("/" + candidate)]
            if len(suffix_matches) == 1 and suffix_matches[0] not in found:
                found.append(suffix_matches[0])
        return found[:10]

    @staticmethod
    def _candidates(paths: List[str], keywords: List[str],
                    prefer_suffix: str = "") -> List[Dict[str, Any]]:
        """Файлы, чей путь пересекается со словами задачи.

        Совпадение совпадению не равно: в репозиториях полно служебных
        каталогов (`.agents/`, `docs/`, `references/`), которые ловят те же
        слова, что и настоящий код. Поэтому исходники получают вес, а
        документация — штраф: человеку нужно место правки, а не README.
        """
        skip_suffixes = (".lock", ".min.js", ".snap", ".png", ".svg", ".jpg", ".woff")
        service_markers = (".agents/", "docs/", "references/", "vendor/", "third_party/")
        source_suffixes = (".sol", ".rs", ".ts", ".tsx", ".js", ".py", ".go", ".java",
                           ".kt", ".cs", ".c", ".cpp", ".h", ".move", ".cairo", ".vy")
        scored: List[Tuple[int, str, List[str]]] = []
        for path in paths:
            lowered = path.lower()
            if lowered.endswith(skip_suffixes) or "/node_modules/" in lowered:
                continue
            name = lowered.rsplit("/", 1)[-1]
            score = 0
            matched: List[str] = []
            for word in keywords:
                if word in name:
                    score += 3
                    matched.append(word)
                elif word in lowered:
                    score += 1
                    matched.append(word)
            if not score:
                continue
            if lowered.endswith(source_suffixes):
                score += 2
            if prefer_suffix and lowered.endswith(prefer_suffix):
                # Аудит читают на языке самого проекта: для Solidity-контеста
                # контракты важнее, чем вспомогательные TS-скрипты рядом.
                score += 3
            if any(marker in lowered for marker in service_markers) or lowered.endswith(".md"):
                score -= 3
            if any(part in lowered for part in ("/src/", "/contracts/", "/core/", "/lib/")):
                score += 1
            if score <= 0:
                continue
            scored.append((score, path, matched))
        scored.sort(key=lambda triple: (-triple[0], len(triple[1])))
        result: List[Dict[str, Any]] = []
        for score, path, matched in scored[:8]:
            reason = "совпадение по названию" if score >= 4 else "совпадение по пути"
            words = [word for word in matched[:3]]
            if words:
                reason += f": {', '.join(words)}"
            if path.lower().endswith(source_suffixes):
                reason += " · исходный файл"
            result.append({"path": path, "score": score, "reason": reason})
        return result

    @staticmethod
    def _code_root(paths: List[str]) -> Tuple[str, Dict[str, int], str]:
        """Где настоящий код и сколько в нём исходников.

        В контестах код часто лежит вложенным каталогом или подмодулем
        (``tare-io__tare-contracts/…``), а рядом — служебные файлы платформы.
        Считаем исходники по верхнеуровневым каталогам и называем главный.
        """
        suffixes = {".sol": "Solidity", ".rs": "Rust", ".ts": "TypeScript",
                    ".tsx": "TypeScript", ".js": "JavaScript", ".py": "Python",
                    ".go": "Go", ".java": "Java", ".kt": "Kotlin", ".cs": "C#",
                    ".move": "Move", ".cairo": "Cairo", ".vy": "Vyper", ".c": "C",
                    ".cpp": "C++"}
        by_root: Dict[str, int] = {}
        counts: Dict[str, int] = {}
        for path in paths:
            suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
            language = suffixes.get(suffix)
            if not language:
                continue
            root = path.split("/", 1)[0] if "/" in path else ""
            by_root[root] = by_root.get(root, 0) + 1
            counts[language] = counts.get(language, 0) + 1
        if not by_root:
            return "", {}, ""
        top_root = max(by_root, key=lambda key: by_root[key])
        language_to_suffix = {
            "Solidity": ".sol", "Rust": ".rs", "TypeScript": ".ts", "Python": ".py",
            "Go": ".go", "Java": ".java", "Kotlin": ".kt", "C#": ".cs", "Move": ".move",
            "Cairo": ".cairo", "Vyper": ".vy", "C": ".c", "C++": ".cpp",
            "JavaScript": ".js",
        }
        top_language = max(counts, key=lambda key: counts[key]) if counts else ""
        dominant = language_to_suffix.get(top_language, "")
        # «Корнем кода» называем каталог только если он действительно вмещает
        # исходники; иначе код лежит в корне репозитория.
        if top_root and by_root[top_root] >= max(3, sum(by_root.values()) * 0.6):
            return top_root, counts, dominant
        return "", counts, dominant

    def _commands(self, repo: str, manifests: List[str], paths: List[str]) -> List[str]:
        """Команды запуска из манифестов: то, что человек всё равно бы искал."""
        commands: List[str] = []
        if "package.json" in manifests:
            data = self._get_file(repo, "package.json")
            if isinstance(data, dict):
                scripts = data.get("scripts") or {}
                for name in ("test", "lint", "build", "dev"):
                    if name in scripts:
                        commands.append(f"npm run {name}")
        if "pyproject.toml" in manifests or "requirements.txt" in manifests or "setup.py" in manifests:
            commands.append("python -m pytest -q  (если тесты на pytest)")
            if "requirements.txt" in manifests:
                commands.append("pip install -r requirements.txt")
            if "pyproject.toml" in manifests:
                commands.append("pip install -e .")
        if "Makefile" in manifests:
            commands.append("make test  (проверьте цели в Makefile)")
        if "go.mod" in manifests:
            commands.append("go test ./...")
        if "Cargo.toml" in manifests:
            commands.append("cargo test")
        if "composer.json" in manifests:
            commands.append("composer test  (уточните скрипты в composer.json)")
        if any(path.startswith("tests/") or path.startswith("test/") for path in paths):
            commands.append("python -m pytest tests/ -q  (тесты найдены в репозитории)")
        # убираем дубли, сохраняя порядок
        seen = set()
        unique = []
        for command in commands:
            if command not in seen:
                seen.add(command)
                unique.append(command)
        return unique[:6]

    def _read_text(self, repo: str, path: str) -> str:
        """Текст небольшого файла (README, scope.txt): то, ради чего стоит идти в API."""
        try:
            response = self.session.get(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                params={"ref": None},
                timeout=25,
            )
        except Exception:
            return ""
        if response.status_code != 200:
            return ""
        try:
            data = response.json()
        except ValueError:
            return ""
        if not isinstance(data, dict) or not data.get("content"):
            return ""
        import base64

        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
        except Exception:
            return ""

    def _read_scope(self, dossier: Dossier, repo: str, paths: List[str]) -> None:
        """Контесты держат код в отдельном репозитории — найдём его в scope и README.

        ``scope.txt`` у Code4rena и Sherlock — построчный список файлов, а не
        проза. Считаем их точно: число файлов в области проверки — главный
        ориентир объёма работы, а «примерно столько-то» здесь бесполезно.
        """
        scope_text = self._read_text(repo, "scope.txt") if "scope.txt" in paths else ""
        out_text = self._read_text(repo, "out_of_scope.txt") if "out_of_scope.txt" in paths else ""
        readme_text = self._read_text(repo, "README.md") if "README.md" in paths else ""

        files = paths_from_lines(scope_text)
        dossier.scope_files = files[:200]

        # Ссылки на репозитории ищем и в README, и в scope, но сортируем по
        # частоте: код контеста упоминается многократно, случайные ссылки — раз.
        mentions: Dict[str, int] = {}
        for blob in (readme_text, scope_text):
            for match in re.findall(r"github\.com/([\w.-]+/[\w.-]+)", blob):
                cleaned = match.rstrip(").,/").removesuffix(".git")
                if not cleaned or cleaned.startswith(repo):
                    continue
                mentions[cleaned] = mentions.get(cleaned, 0) + 1
        dossier.scope_repos = [
            name for name, _ in sorted(mentions.items(), key=lambda pair: -pair[1])[:3]
        ]

        if files:
            dossier.scope_notes.append(
                f"файлов в области проверки: {len(files)} — вне этого списка "
                "находки не оплачиваются"
            )
            top_dirs: Dict[str, int] = {}
            for path in files:
                parts = path.split("/")
                key = "/".join(parts[:3]) if len(parts) >= 3 else path.rsplit("/", 1)[0]
                top_dirs[key] = top_dirs.get(key, 0) + 1
            leaders = sorted(top_dirs.items(), key=lambda pair: -pair[1])[:4]
            if leaders:
                dossier.scope_notes.append(
                    "больше всего файлов в: "
                    + ", ".join(f"{name} ({count})" for name, count in leaders)
                )
        out_files = paths_from_lines(out_text)
        if out_files:
            dossier.scope_notes.append(
                f"вне области проверки: {len(out_files)} файлов — их правки не оплачиваются"
            )
        if dossier.scope_repos:
            dossier.scope_notes.append(
                "код лежит отдельно от репозитория контеста: клонируйте его перед работой"
            )

    def _get_file(self, repo: str, path: str) -> Any:
        data = self._get(f"https://api.github.com/repos/{repo}/contents/{path}")
        if not isinstance(data, dict):
            return None
        content = data.get("content")
        if not content:
            return None
        import base64

        try:
            raw = base64.b64decode(content).decode("utf-8", errors="ignore")
            return json.loads(raw)
        except Exception:
            return None


def build(opportunity: Dict[str, Any], token: str = "") -> Dossier:
    return DossierBuilder(token=token or (get_env("GITHUB_TOKEN", "") or "")).build(opportunity)


def save(opportunity: Dict[str, Any]) -> Tuple[Dossier, Any]:
    """Собрать досье и сохранить в reports/inbox. Возвращает (досье, путь)."""
    import re as _re
    from agent.config import project_root

    dossier = build(opportunity)
    reports = project_root() / "reports" / "inbox"
    reports.mkdir(parents=True, exist_ok=True)
    safe = _re.sub(r"[^A-Za-z0-9_.-]+", "-", str(opportunity.get("id") or "task")).strip("-")
    path = reports / f"dossier-{safe}.md"
    path.write_text(dossier.to_markdown(), encoding="utf-8")
    return dossier, path
