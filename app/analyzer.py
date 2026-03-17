from __future__ import annotations

import math
import re
from collections import Counter

from app.models import Finding, RepoFile, RepoSnapshot, ReviewResult

README_NAMES = {"readme.md", "readme.txt"}
TEST_MARKERS = {"test", "tests", "__tests__", "spec"}
BEGINNER_POSITIVE_MARKERS = {
    "requirements.txt",
    "package.json",
    ".gitignore",
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
}
AI_PHRASES = (
    "this project is a modern",
    "robust and scalable",
    "clean architecture",
    "enterprise-grade",
    "production-ready",
    "comprehensive solution",
)


class RepositoryAnalyzer:
    def analyze(self, snapshot: RepoSnapshot) -> ReviewResult:
        files = snapshot.files
        if not files:
            raise ValueError("В репозитории не нашлось текстовых файлов для анализа.")

        readme = self._find_readme(files)
        tests_count = self._count_tests(files)
        source_files = [f for f in files if self._looks_like_source(f.path)]
        total_lines = sum(self._count_non_empty_lines(f.content) for f in source_files)
        comment_ratio = self._comment_ratio(source_files)

        strengths = self._collect_strengths(snapshot, readme, tests_count, total_lines)
        issues = self._collect_issues(snapshot, readme, tests_count, source_files, total_lines, comment_ratio)
        recommendations = self._collect_recommendations(snapshot, issues, tests_count, readme)

        ai_probability, ai_rationale = self._estimate_ai_probability(snapshot, readme, source_files)
        overall_score = self._score_project(strengths, issues, tests_count, readme)

        summary = self._build_summary(snapshot, overall_score, issues, ai_probability)

        return ReviewResult(
            summary=summary,
            strengths=strengths,
            issues=issues,
            recommendations=recommendations,
            ai_probability_percent=ai_probability,
            ai_rationale=ai_rationale,
            overall_score_percent=overall_score,
        )

    def _find_readme(self, files: list[RepoFile]) -> RepoFile | None:
        for file in files:
            if file.path.split("/")[-1].lower() in README_NAMES:
                return file
        return None

    def _count_tests(self, files: list[RepoFile]) -> int:
        total = 0
        for file in files:
            lowered = file.path.lower()
            if any(marker in lowered.split("/") for marker in TEST_MARKERS) or lowered.endswith(("_test.py", ".spec.js", ".test.js", ".spec.ts", ".test.ts")):
                total += 1
        return total

    def _looks_like_source(self, path: str) -> bool:
        return any(path.endswith(ext) for ext in (".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".php", ".rb", ".cs", ".cpp", ".c", ".kt", ".rs"))

    def _count_non_empty_lines(self, content: str) -> int:
        return sum(1 for line in content.splitlines() if line.strip())

    def _comment_ratio(self, files: list[RepoFile]) -> float:
        comment_lines = 0
        code_lines = 0
        for file in files:
            for line in file.content.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                code_lines += 1
                if stripped.startswith(("#", "//", "/*", "*", "--")):
                    comment_lines += 1
        if code_lines == 0:
            return 0.0
        return comment_lines / code_lines

    def _collect_strengths(
        self,
        snapshot: RepoSnapshot,
        readme: RepoFile | None,
        tests_count: int,
        total_lines: int,
    ) -> list[str]:
        strengths: list[str] = []

        filenames = {file.path.split("/")[-1].lower() for file in snapshot.files}
        found_markers = sorted(BEGINNER_POSITIVE_MARKERS.intersection(filenames))
        if found_markers:
            strengths.append(f"Есть базовая инженерная обвязка: {', '.join(found_markers)}.")
        if readme and self._count_non_empty_lines(readme.content) >= 8:
            strengths.append("README не пустой и помогает понять, что делает проект.")
        if tests_count:
            strengths.append(f"Есть тесты или тестовые директории: найдено {tests_count}.")
        if total_lines >= 120:
            strengths.append("Проект уже выглядит как не совсем учебный скелет, а как рабочая попытка собрать функционал.")
        if not strengths:
            strengths.append("Есть минимальная структура проекта, с которой уже можно продолжать улучшение.")
        return strengths

    def _collect_issues(
        self,
        snapshot: RepoSnapshot,
        readme: RepoFile | None,
        tests_count: int,
        source_files: list[RepoFile],
        total_lines: int,
        comment_ratio: float,
    ) -> list[Finding]:
        issues: list[Finding] = []

        if not readme:
            issues.append(Finding("Нет README", "Новому человеку будет сложно понять, как запускать и проверять проект.", "high"))
        elif self._count_non_empty_lines(readme.content) < 5:
            issues.append(Finding("README слишком короткий", "Есть файл README, но в нем мало пользы: не хватает запуска, описания и структуры проекта.", "medium"))

        if tests_count == 0:
            issues.append(Finding("Нет тестов", "Для учебного проекта это не критично, но хотя бы 1-2 smoke-теста сильно повышают доверие к работе.", "medium"))

        if total_lines < 40:
            issues.append(Finding("Очень мало исходного кода", "По найденным файлам проект пока выглядит недособранным или слишком маленьким для уверенной оценки.", "medium"))

        large_files = [file for file in source_files if self._count_non_empty_lines(file.content) > 250]
        if large_files:
            issues.append(
                Finding(
                    "Есть слишком крупные файлы",
                    f"Крупные модули сложнее читать и поддерживать. Пример: {large_files[0].path}.",
                    "medium",
                )
            )

        repeated_fragments = self._find_repeated_lines(source_files)
        if repeated_fragments >= 8:
            issues.append(
                Finding(
                    "Похоже на копипасту",
                    "В коде много повторяющихся строк и однотипных блоков. Это сигнал к выносу общей логики в функции или модули.",
                    "medium",
                )
            )

        if comment_ratio < 0.01 and total_lines > 150:
            issues.append(
                Finding(
                    "Мало пояснений в коде",
                    "Когда проект уже разросся, без коротких пояснений сложнее понять ключевые решения.",
                    "low",
                )
            )

        if snapshot.skipped_files > len(snapshot.files):
            issues.append(
                Finding(
                    "Не вся структура попала в анализ",
                    "В репозитории много бинарных, больших или служебных файлов. Итоговый отзыв строится в основном по текстовым исходникам.",
                    "low",
                )
            )

        return issues

    def _collect_recommendations(
        self,
        snapshot: RepoSnapshot,
        issues: list[Finding],
        tests_count: int,
        readme: RepoFile | None,
    ) -> list[str]:
        recommendations: list[str] = []
        titles = {issue.title for issue in issues}

        if "Нет README" in titles or "README слишком короткий" in titles:
            recommendations.append("Добавить README с 4 блоками: идея проекта, стек, запуск, что уже работает и что не готово.")
        if tests_count == 0:
            recommendations.append("Добавить хотя бы 1-2 простых теста на основной сценарий или ручной чек-лист проверки.")
        if any(issue.title == "Есть слишком крупные файлы" for issue in issues):
            recommendations.append("Разбить самые большие файлы на модули: отдельно бизнес-логику, работу с данными и интерфейс.")
        if any(issue.title == "Похоже на копипасту" for issue in issues):
            recommendations.append("Убрать повторяющийся код в отдельные функции, чтобы проект выглядел осознанно, а не как набор копий.")
        if not readme and snapshot.description:
            recommendations.append("Перенести краткое описание из GitHub в README и дополнить его примерами использования.")
        if not recommendations:
            recommendations.append("Следующий шаг для роста проекта: добавить демонстрационный сценарий и коротко описать архитектуру.")

        return recommendations[:4]

    def _estimate_ai_probability(
        self,
        snapshot: RepoSnapshot,
        readme: RepoFile | None,
        source_files: list[RepoFile],
    ) -> tuple[int, list[str]]:
        score = 18.0
        rationale: list[str] = []

        if readme:
            lowered = readme.content.lower()
            phrase_hits = sum(1 for phrase in AI_PHRASES if phrase in lowered)
            if phrase_hits >= 2:
                score += 18
                rationale.append("README использует слишком шаблонные и маркетинговые формулировки.")
            elif self._count_non_empty_lines(readme.content) > 30:
                score += 8
                rationale.append("README заметно полирован для новичкового проекта.")

        lengths = [self._count_non_empty_lines(file.content) for file in source_files if self._count_non_empty_lines(file.content) > 0]
        if lengths:
            mean = sum(lengths) / len(lengths)
            variance = sum((length - mean) ** 2 for length in lengths) / len(lengths)
            normalized_variance = math.sqrt(variance) / mean if mean else 0
            if len(lengths) >= 6 and normalized_variance < 0.28:
                score += 14
                rationale.append("Многие файлы похожи по размеру и структуре, что иногда встречается у AI-генерации.")

        identifier_words = self._collect_identifiers(source_files)
        if identifier_words:
            top_share = identifier_words.most_common(1)[0][1] / sum(identifier_words.values())
            if top_share < 0.035 and len(identifier_words) > 160:
                score += 10
                rationale.append("Код использует широкий и ровный словарь идентификаторов, что может выглядеть слишком сглаженно.")

        polished_files = 0
        for file in source_files:
            content = file.content.lower()
            if "todo" in content or "fixme" in content:
                score -= 4
            if re.search(r"\b(interface|service|repository|manager|factory)\b", content):
                polished_files += 1
        if polished_files >= 5:
            score += 12
            rationale.append("Для новичкового проекта структура выглядит довольно взрослой и равномерно оформленной.")

        beginner_markers = {file.path.split("/")[-1].lower() for file in snapshot.files}
        if {"venv", ".idea", ".vscode"}.intersection(beginner_markers):
            score -= 3
        if any("console.log" in file.content or "print(" in file.content for file in source_files):
            score -= 5
            rationale.append("В проекте есть следы ручной отладки, это немного снижает подозрение на полностью AI-сгенерированную работу.")

        score = max(5, min(95, round(score)))
        if not rationale:
            rationale.append("Явных паттернов, которые сильно кричат об AI-генерации, не видно.")
        rationale.append("Это эвристическая оценка, а не доказательство: процент стоит использовать как ориентир, а не как приговор.")
        return int(score), rationale[:3]

    def _score_project(
        self,
        strengths: list[str],
        issues: list[Finding],
        tests_count: int,
        readme: RepoFile | None,
    ) -> int:
        score = 55
        score += min(15, len(strengths) * 5)
        score -= sum({"high": 14, "medium": 8, "low": 4}[issue.severity] for issue in issues)
        if tests_count:
            score += 7
        if readme:
            score += 5
        return max(20, min(96, score))

    def _build_summary(
        self,
        snapshot: RepoSnapshot,
        overall_score: int,
        issues: list[Finding],
        ai_probability: int,
    ) -> str:
        if overall_score >= 75:
            quality = "хорошее впечатление"
        elif overall_score >= 55:
            quality = "нормальная база"
        else:
            quality = "сыроватое состояние"

        risk = "низкий" if ai_probability < 35 else "средний" if ai_probability < 65 else "повышенный"
        return (
            f"Репозиторий {snapshot.owner}/{snapshot.name} оставляет {quality}: "
            f"итоговая оценка {overall_score}%. "
            f"Найдено {len(issues)} зон для улучшения, а риск заметного влияния AI оценивается как {risk} ({ai_probability}%)."
        )

    def _collect_identifiers(self, source_files: list[RepoFile]) -> Counter:
        counter: Counter = Counter()
        for file in source_files:
            words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", file.content)
            counter.update(word.lower() for word in words)
        return counter

    def _find_repeated_lines(self, source_files: list[RepoFile]) -> int:
        repeated = 0
        counter: Counter = Counter()
        for file in source_files:
            for line in file.content.splitlines():
                stripped = line.strip()
                if len(stripped) < 18:
                    continue
                counter[stripped] += 1
        for value in counter.values():
            if value >= 3:
                repeated += 1
        return repeated
