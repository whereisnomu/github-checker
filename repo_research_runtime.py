from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_runtime import RepoFile, RepoSnapshot


SOURCE_EXTENSIONS = (".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".php", ".rb", ".cs", ".kt", ".rs", ".html", ".css")
ENTRYPOINT_MARKERS = ("index.", "main.", "app.", "server.", "bot.", "cli.")
CONFIG_MARKERS = ("package.json", "requirements.txt", "pyproject.toml", "tsconfig.json", ".env.example", "docker-compose.yml", "docker-compose.yaml", "dockerfile")
TEST_MARKERS = ("test", "tests", "__tests__", "spec")


@dataclass(slots=True)
class RepoResearchDigest:
    overview_lines: list[str] = field(default_factory=list)
    dependency_lines: list[str] = field(default_factory=list)
    architecture_lines: list[str] = field(default_factory=list)
    rubric_lines: list[str] = field(default_factory=list)
    key_file_lines: list[str] = field(default_factory=list)
    reviewed_files: list[str] = field(default_factory=list)
    backend_name: str = "builtin"
    external_pack_excerpt: str = ""

    def to_prompt_block(self, max_chars: int = 14000) -> str:
        blocks: list[str] = []
        if self.overview_lines:
            blocks.append("RESEARCH_OVERVIEW:\n" + "\n".join(f"- {line}" for line in self.overview_lines))
        if self.dependency_lines:
            blocks.append("DEPENDENCIES_AND_RUNTIME:\n" + "\n".join(f"- {line}" for line in self.dependency_lines))
        if self.architecture_lines:
            blocks.append("ARCHITECTURE_NOTES:\n" + "\n".join(f"- {line}" for line in self.architecture_lines))
        if self.rubric_lines:
            blocks.append("RUBRIC_HINTS:\n" + "\n".join(f"- {line}" for line in self.rubric_lines))
        if self.key_file_lines:
            blocks.append("KEY_FILE_DIGESTS:\n" + "\n".join(f"- {line}" for line in self.key_file_lines))
        if self.external_pack_excerpt:
            blocks.append("REPO_PACK_EXCERPT:\n" + self.external_pack_excerpt)
        text = "\n\n".join(blocks).strip()
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 20].rstrip() + "\n... [truncated]"


def build_repo_research(snapshot: "RepoSnapshot", *, reviewed_limit: int = 16) -> RepoResearchDigest:
    builtin = _build_builtin_repo_research(snapshot, reviewed_limit=reviewed_limit)
    backend = os.getenv("REPO_RESEARCH_BACKEND", "auto").strip().lower() or "auto"
    if backend in {"builtin", "local"}:
        return builtin

    external = _run_optional_backend(snapshot, backend=backend)
    if not external:
        return builtin

    builtin.backend_name = external.backend_name
    builtin.external_pack_excerpt = external.external_pack_excerpt
    builtin.overview_lines.insert(0, f"Дополнительная AI-friendly сводка собрана через backend: {external.backend_name}.")
    return builtin


def _build_builtin_repo_research(snapshot: "RepoSnapshot", *, reviewed_limit: int = 16) -> RepoResearchDigest:
    files = list(snapshot.files)
    source_files = [file for file in files if file.path.lower().endswith(SOURCE_EXTENSIONS)]
    top_dirs = Counter(file.path.split("/", 1)[0] for file in files if "/" in file.path)
    ext_counter = Counter(Path(file.path).suffix.lower() or "<noext>" for file in files)
    total_lines = sum(_count_non_empty_lines(file.content) for file in source_files)
    readme = next((file for file in files if file.path.split("/")[-1].lower() in {"readme.md", "readme.txt"}), None)
    entrypoints = [file.path for file in files if _is_entrypoint(file.path)]
    config_files = [file.path for file in files if _is_config_file(file.path)]
    test_files = [file.path for file in files if _looks_like_test(file.path)]
    dependencies, scripts = _extract_dependency_info(files)
    frameworks = _infer_frameworks(files, dependencies)
    error_files, validation_files, todo_files, console_files = _quality_markers(source_files)
    reviewed = _select_research_files(files, reviewed_limit)
    key_file_lines = [_summarize_file(file) for file in reviewed]

    overview_lines = [
        f"Всего текстовых файлов в анализе: {len(files)}, исходников: {len(source_files)}, примерный объем: {total_lines} непустых строк.",
        f"Главные каталоги: {_join_counter(top_dirs, 5)}." if top_dirs else "Проект почти без вложенной структуры.",
        f"Типы файлов: {_join_counter(ext_counter, 6)}.",
        f"README: {'есть' if readme else 'нет'}, тестовых файлов: {len(test_files)}, конфигов: {len(config_files)}.",
    ]
    if entrypoints:
        overview_lines.append("Похожие на точки входа файлы: " + ", ".join(entrypoints[:6]) + ".")

    dependency_lines: list[str] = []
    if dependencies:
        dependency_lines.append("Основные зависимости: " + ", ".join(dependencies[:12]) + ".")
    if scripts:
        dependency_lines.append("Скрипты запуска/сборки: " + ", ".join(scripts[:8]) + ".")
    if frameworks:
        dependency_lines.append("По зависимостям и импортам видны технологии: " + ", ".join(frameworks[:10]) + ".")

    architecture_lines = [
        f"Ключевые файлы для чтения: {', '.join(file.path for file in reviewed[:8])}.",
        f"Файлов с явной обработкой ошибок: {error_files}, с признаками валидации: {validation_files}, с TODO/FIXME: {todo_files}.",
        f"Следы отладочного вывода встречаются в {console_files} файлах." if console_files else "Явных следов отладочного вывода почти нет.",
    ]

    rubric_lines = [
        _rubric_line("Полнота и соответствие задаче", readme is not None or snapshot.assignment_text, len(files), len(test_files)),
        _rubric_line("Корректность и надежность", error_files > 0, len(source_files), validation_files),
        _rubric_line("Структура и декомпозиция", len(top_dirs) >= 2 or len(files) <= 3, len(reviewed), len(config_files)),
        _rubric_line("Читаемость и стиль", total_lines > 0, total_lines, console_files),
        _rubric_line("Тестируемость и проверяемость", len(test_files) > 0, len(test_files), len(scripts)),
        _rubric_line("Документация и запуск", readme is not None, len(config_files), len(dependencies)),
    ]

    return RepoResearchDigest(
        overview_lines=overview_lines,
        dependency_lines=dependency_lines,
        architecture_lines=architecture_lines,
        rubric_lines=rubric_lines,
        key_file_lines=key_file_lines,
        reviewed_files=[file.path for file in reviewed],
    )


def _run_optional_backend(snapshot: "RepoSnapshot", *, backend: str) -> RepoResearchDigest | None:
    backend = backend.lower()
    if backend in {"auto", "repopack", "repomix"}:
        digest = _run_repomix_backend(snapshot)
        if digest:
            return digest
    return None


def _run_repomix_backend(snapshot: "RepoSnapshot") -> RepoResearchDigest | None:
    commands = [
        ["repomix"],
        ["repopack"],
        ["npx", "repomix"],
        ["npx", "repopack"],
    ]
    timeout_seconds = int(os.getenv("REPO_RESEARCH_TOOL_TIMEOUT_SECONDS", "120"))
    output_name = "repo-pack.txt"

    with tempfile.TemporaryDirectory(prefix="repo-research-") as tmp_dir:
        root = Path(tmp_dir)
        _write_snapshot_to_tempdir(snapshot, root)
        output_path = root / output_name

        for command in commands:
            executable = command[0]
            if executable != "npx" and shutil.which(executable) is None:
                continue
            full_command = [*command, str(root), "--style", "plain", "--output", output_name]
            try:
                completed = subprocess.run(
                    full_command,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if completed.returncode != 0 or not output_path.exists():
                continue

            try:
                packed_text = output_path.read_text(encoding="utf-8")
            except OSError:
                continue
            excerpt = packed_text.strip()
            if not excerpt:
                continue
            backend_name = "repomix" if "repomix" in " ".join(command).lower() else "repopack"
            return RepoResearchDigest(
                backend_name=backend_name,
                external_pack_excerpt=_trim_pack_excerpt(excerpt),
            )
    return None


def _write_snapshot_to_tempdir(snapshot: "RepoSnapshot", root: Path) -> None:
    for file in snapshot.files:
        target = root / Path(file.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file.content, encoding="utf-8")


def _trim_pack_excerpt(text: str, limit: int = 9000) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 20].rstrip() + "\n... [truncated]"


def _count_non_empty_lines(content: str) -> int:
    return sum(1 for line in content.splitlines() if line.strip())


def _join_counter(counter: Counter[str], limit: int) -> str:
    return ", ".join(f"{name} ({count})" for name, count in counter.most_common(limit)) or "нет данных"


def _is_entrypoint(path: str) -> bool:
    lowered = path.lower()
    name = lowered.split("/")[-1]
    return any(marker in name for marker in ENTRYPOINT_MARKERS)


def _is_config_file(path: str) -> bool:
    lowered = path.lower()
    name = lowered.split("/")[-1]
    return name in CONFIG_MARKERS or lowered.endswith("/dockerfile")


def _looks_like_test(path: str) -> bool:
    lowered = path.lower()
    return any(marker in lowered.split("/") for marker in TEST_MARKERS) or lowered.endswith(
        ("_test.py", ".spec.js", ".test.js", ".spec.ts", ".test.ts")
    )


def _extract_dependency_info(files: list["RepoFile"]) -> tuple[list[str], list[str]]:
    dependencies: list[str] = []
    scripts: list[str] = []
    for file in files:
        name = file.path.split("/")[-1].lower()
        if name == "package.json":
            try:
                payload = json.loads(file.content)
            except json.JSONDecodeError:
                continue
            deps = list((payload.get("dependencies") or {}).keys()) + list((payload.get("devDependencies") or {}).keys())
            dependencies.extend(dep for dep in deps if isinstance(dep, str))
            scripts.extend(name for name in (payload.get("scripts") or {}).keys() if isinstance(name, str))
        elif name in {"requirements.txt", "requirements-dev.txt"}:
            for line in file.content.splitlines():
                candidate = line.strip()
                if not candidate or candidate.startswith("#"):
                    continue
                dependencies.append(re.split(r"[<>=!~]", candidate, maxsplit=1)[0].strip())
        elif name == "pyproject.toml":
            dependencies.extend(re.findall(r'^\s*"([^"]+)"', file.content, re.M))
    seen: set[str] = set()
    normalized_deps = [item for item in dependencies if item and not (item in seen or seen.add(item))]
    seen_scripts: set[str] = set()
    normalized_scripts = [item for item in scripts if item and not (item in seen_scripts or seen_scripts.add(item))]
    return normalized_deps, normalized_scripts


def _infer_frameworks(files: list["RepoFile"], dependencies: list[str]) -> list[str]:
    combined = " ".join(dependencies).lower()
    framework_map = {
        "react": "React",
        "vue": "Vue",
        "telegraf": "Telegraf",
        "express": "Express",
        "fastapi": "FastAPI",
        "flask": "Flask",
        "django": "Django",
        "sqlite": "SQLite",
        "better-sqlite3": "better-sqlite3",
        "pytest": "pytest",
        "jest": "Jest",
        "vitest": "Vitest",
    }
    found = [label for marker, label in framework_map.items() if marker in combined]
    if found:
        return found

    import_text = "\n".join(file.content[:1200] for file in files[:20]).lower()
    found = [label for marker, label in framework_map.items() if marker in import_text]
    return found


def _quality_markers(source_files: list["RepoFile"]) -> tuple[int, int, int, int]:
    error_files = 0
    validation_files = 0
    todo_files = 0
    console_files = 0
    for file in source_files:
        lowered = file.content.lower()
        if any(marker in lowered for marker in ("try:", "except ", "try {", "catch (", "catch{", "console.error", "throw new error")):
            error_files += 1
        if any(marker in lowered for marker in ("validate", "schema", "required", "isnan", "parseint", "zod", "yup", "if (!", "if not ")):
            validation_files += 1
        if "todo" in lowered or "fixme" in lowered:
            todo_files += 1
        if "console.log" in lowered or "print(" in lowered:
            console_files += 1
    return error_files, validation_files, todo_files, console_files


def _research_priority(path: str) -> int:
    lowered = path.lower()
    score = 0
    for marker in ("readme", "package.json", "requirements", "config", "server", "index", "main", "app", "bot", "db", "handler", "service", "router", "controller"):
        if marker in lowered:
            score += 5
    if lowered.endswith(SOURCE_EXTENSIONS):
        score += 3
    return score


def _select_research_files(files: list["RepoFile"], limit: int) -> list["RepoFile"]:
    return sorted(files, key=lambda item: (_research_priority(item.path), len(item.content)), reverse=True)[:limit]


def _summarize_file(file: "RepoFile") -> str:
    content = file.content
    non_empty_lines = _count_non_empty_lines(content)
    function_count = len(re.findall(r"\b(function|def|async function|const\s+\w+\s*=\s*\(|export function|class)\b", content))
    import_count = len(re.findall(r"^\s*(import|from .+ import|require\()", content, re.M))
    role = _infer_role(file.path, content)
    notes: list[str] = []
    if re.search(r"\btry\b", content):
        notes.append("есть обработка ошибок")
    if re.search(r"\b(validate|required|schema|zod|yup)\b", content, re.I):
        notes.append("есть признаки валидации")
    if "console.log" in content or "print(" in content:
        notes.append("есть отладочный вывод")
    if "todo" in content.lower() or "fixme" in content.lower():
        notes.append("есть TODO/FIXME")
    note_text = "; ".join(notes[:3]) if notes else "без явных спецсигналов"
    return (
        f"{file.path} | роль: {role} | строк: {non_empty_lines} | "
        f"функции/классы: {function_count} | imports: {import_count} | {note_text}"
    )


def _infer_role(path: str, content: str) -> str:
    lowered = path.lower()
    if "readme" in lowered:
        return "документация"
    if "config" in lowered or path.split("/")[-1].lower() in CONFIG_MARKERS:
        return "конфиг"
    if any(marker in lowered for marker in ("test", "spec")):
        return "тест"
    if any(marker in lowered for marker in ("db", "model", "repository")):
        return "данные/хранилище"
    if any(marker in lowered for marker in ("handler", "router", "controller", "view")):
        return "обработчики/интерфейс"
    if any(marker in lowered for marker in ("service", "utils", "helper")):
        return "бизнес-логика/утилиты"
    if any(marker in lowered for marker in ("index", "main", "app", "server", "bot")):
        return "точка входа"
    lowered_content = content[:1200].lower()
    if "addEventListener".lower() in lowered_content or "document." in lowered_content:
        return "UI/DOM логика"
    if "express" in lowered_content or "fastapi" in lowered_content or "flask" in lowered_content:
        return "сервер"
    return "исходный код"


def _rubric_line(name: str, primary_signal: bool, first_value: int, second_value: int) -> str:
    status = "скорее покрыто" if primary_signal else "есть риск недоработки"
    return f"{name}: {status}; вспомогательные сигналы {first_value}/{second_value}."

def main() -> int:
    import argparse
    from bot_runtime import GitHubClient, extract_repo_url, get_settings

    parser = argparse.ArgumentParser(description="Build a compact research digest for a GitHub repository.")
    parser.add_argument("repo_url", help="GitHub repository URL")
    parser.add_argument("--output", default=".cache/repo_research.txt", help="Where to save the digest text")
    parser.add_argument("--backend", default="", help="Research backend override: builtin, repomix/repopack, auto")
    args = parser.parse_args()

    if args.backend:
        os.environ["REPO_RESEARCH_BACKEND"] = args.backend

    settings = get_settings()
    github = GitHubClient(settings)
    snapshot = github.fetch_snapshot(extract_repo_url(args.repo_url))
    research = build_repo_research(snapshot)
    text = research.to_prompt_block(max_chars=20000)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")

    print(text)
    print(f"\nSaved: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
