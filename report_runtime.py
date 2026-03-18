from __future__ import annotations

import html
import os
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

    if result.rubric_breakdown:
        parts.append("<b>Оценка по критериям:</b>\n" + render_list(result.rubric_breakdown))

    parts.append("<b>Рекомендации:</b>\n" + render_list(result.recommendations))
    parts.append(
        "<b>Вероятность использования AI:</b> "
        f"<code>{result.ai_probability_percent}%</code>\n"
        + render_list(result.ai_rationale)
    )

    if result.ai_detection_signals:
        parts.append("<b>Сигналы AI-детекта:</b>\n" + render_list(result.ai_detection_signals))

    ai_status_block = render_ai_status(result)
    if ai_status_block:
        parts.append(ai_status_block)

    return "\n\n".join(parts)


def render_ai_status(result: "ReviewResult") -> str:
    show_debug = os.getenv("REPORT_SHOW_DEBUG_DETAILS", "false").lower() == "true"
    if show_debug and result.provider_attempts:
        return "<b>Статус AI и бюджета:</b>\n" + render_list(result.provider_attempts)

    if not result.provider_attempts:
        return ""

    concise_lines: list[str] = []
    if result.review_source.startswith("AI:"):
        concise_lines.append("AI-отчет собран успешно.")
    else:
        concise_lines.append("AI не дал достаточно надежный итоговый ответ, поэтому включен локальный резервный анализ.")

    lower_attempts = " ".join(result.provider_attempts).lower()
    if "cooldown" in lower_attempts or "temporarily unavailable" in lower_attempts or "429" in lower_attempts:
        concise_lines.append("Часть AI-провайдеров была временно недоступна.")
    if any(marker in lower_attempts for marker in ("truncated review", "incomplete structured review", "no content generated")):
        concise_lines.append("Часть AI-ответов оказалась неполной или пустой.")

    concise_lines.append("Технические подробности доступны командой /lastdebug.")
    return "<b>Статус AI:</b>\n" + render_list(concise_lines)


def render_list(items: list[str]) -> str:
    return "\n".join(f"• {html.escape(item)}" for item in items) if items else "• Ничего критичного не нашлось."


def render_findings(items: list["Finding"]) -> str:
    if not items:
        return "• Серьезных проблем не найдено."
    return "\n".join(
        f"• [{item.severity.upper()}] {html.escape(item.title)}: {html.escape(item.detail)}"
        for item in items
    )
