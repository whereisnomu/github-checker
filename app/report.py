from __future__ import annotations

from app.models import Finding, RepoSnapshot, ReviewResult


def render_report(snapshot: RepoSnapshot, result: ReviewResult) -> str:
    parts: list[str] = []
    parts.append(f"<b>Проверка репозитория</b>\n<code>{snapshot.owner}/{snapshot.name}</code>")
    if snapshot.description:
        parts.append(f"<b>Описание:</b> {snapshot.description}")

    parts.append(
        f"<b>Итог:</b> {result.summary}\n"
        f"<b>Размер анализа:</b> {len(snapshot.files)} файлов, пропущено {snapshot.skipped_files}"
    )

    parts.append("<b>Сильные стороны:</b>\n" + _render_list(result.strengths))
    parts.append("<b>Что поправить:</b>\n" + _render_findings(result.issues))
    parts.append("<b>Рекомендации:</b>\n" + _render_list(result.recommendations))
    parts.append(
        "<b>Вероятность использования AI:</b> "
        f"<code>{result.ai_probability_percent}%</code>\n"
        + _render_list(result.ai_rationale)
    )

    return "\n\n".join(parts)


def _render_list(items: list[str]) -> str:
    return "\n".join(f"• {item}" for item in items) if items else "• Ничего критичного не нашлось."


def _render_findings(items: list[Finding]) -> str:
    if not items:
        return "• Серьезных проблем не найдено."
    return "\n".join(f"• [{item.severity.upper()}] {item.title}: {item.detail}" for item in items)
