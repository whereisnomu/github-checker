from __future__ import annotations

import html
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_runtime import Finding, RepoSnapshot, ReviewResult


def render_report_v2(snapshot: "RepoSnapshot", result: "ReviewResult") -> str:
    parts: list[str] = []
    parts.append(f"<b>Проверка репозитория</b>\n<code>{snapshot.owner}/{snapshot.name}</code>")
    if snapshot.description:
        parts.append(f"<b>Описание:</b> {html.escape(snapshot.description)}")

    parts.append(
        f"<b>Источник отчета:</b> {html.escape(result.review_source)}\n"
        f"<b>Итог:</b> {html.escape(result.summary)}\n"
        f"<b>Размер анализа:</b> {len(snapshot.files)} файлов, пропущено {snapshot.skipped_files}"
    )

    if result.reviewed_files:
        parts.append("<b>Просмотренные файлы:</b>\n" + render_list(result.reviewed_files))

    parts.append("<b>Сильные стороны:</b>\n" + render_list(result.strengths))
    parts.append("<b>Что поправить:</b>\n" + render_findings(result.issues))

    if result.assignment_summary:
        assignment_block = [f"<b>Сравнение с ТЗ:</b>\n{html.escape(result.assignment_summary)}"]
        if result.assignment_findings:
            assignment_block.append(render_list(result.assignment_findings))
        parts.append("\n".join(assignment_block))

    if result.detailed_analysis:
        parts.append("<b>Детальный разбор:</b>\n" + render_list(result.detailed_analysis))

    parts.append("<b>Рекомендации:</b>\n" + render_list(result.recommendations))
    parts.append(
        "<b>Вероятность использования AI:</b> "
        f"<code>{result.ai_probability_percent}%</code>\n"
        + render_list(result.ai_rationale)
    )

    if result.ai_detection_signals:
        parts.append("<b>Сигналы AI-детекта:</b>\n" + render_list(result.ai_detection_signals))

    if result.provider_attempts:
        parts.append("<b>Статус AI и бюджета:</b>\n" + render_list(result.provider_attempts))

    return "\n\n".join(parts)


def render_list(items: list[str]) -> str:
    return "\n".join(f"• {html.escape(item)}" for item in items) if items else "• Ничего критичного не нашлось."


def render_findings(items: list["Finding"]) -> str:
    if not items:
        return "• Серьезных проблем не найдено."
    return "\n".join(
        f"• [{item.severity.upper()}] {html.escape(item.title)}: {html.escape(item.detail)}"
        for item in items
    )
