from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import requests

if TYPE_CHECKING:
    from bot_runtime import Finding, ReviewResult, RepoSnapshot


@dataclass(slots=True)
class ProviderUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0


@dataclass(slots=True)
class ProviderResult:
    payload: dict[str, Any]
    usage: ProviderUsage
    provider_key: str
    raw_text: str = ""
    raw_response: dict[str, Any] | None = None


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, temporary: bool = True, usage: ProviderUsage | None = None) -> None:
        super().__init__(message)
        self.temporary = temporary
        self.usage = usage


class BudgetExceededError(RuntimeError):
    pass


class BudgetManager:
    def __init__(self) -> None:
        self.state_path = Path(os.getenv("AI_BUDGET_STATE_FILE", ".cache/ai_budget_state.json"))
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.daily_token_limit = int(os.getenv("AI_DAILY_TOKEN_LIMIT", "2000000"))
        self.monthly_token_limit = int(os.getenv("AI_MONTHLY_TOKEN_LIMIT", "25000000"))
        self.daily_usd_limit = float(os.getenv("AI_DAILY_USD_LIMIT", "1.0"))
        self.monthly_usd_limit = float(os.getenv("AI_MONTHLY_USD_LIMIT", "10.0"))
        self.hard_stop = os.getenv("AI_HARD_STOP_ON_EXHAUST", "true").lower() != "false"
        self.max_prompt_chars = int(os.getenv("AI_MAX_PROMPT_CHARS", "40000"))
        self.low_budget_prompt_chars = int(os.getenv("AI_LOW_BUDGET_PROMPT_CHARS", "40000"))
        self.max_output_tokens = int(os.getenv("AI_MAX_OUTPUT_TOKENS", "5000"))
        self.low_budget_max_output_tokens = int(os.getenv("AI_LOW_BUDGET_MAX_OUTPUT_TOKENS", "5000"))
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"days": {}, "months": {}}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"days": {}, "months": {}}

    def _save_state(self) -> None:
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _keys(self) -> tuple[str, str]:
        now = datetime.now(UTC)
        return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")

    def _bucket(self, scope: str, key: str) -> dict[str, Any]:
        bucket = self.state.setdefault(scope, {}).setdefault(
            key,
            {"tokens": 0, "cost_usd": 0.0, "calls": 0, "providers": {}},
        )
        return bucket

    def get_totals(self) -> dict[str, Any]:
        day_key, month_key = self._keys()
        day = self._bucket("days", day_key)
        month = self._bucket("months", month_key)
        return {
            "day_tokens": int(day["tokens"]),
            "month_tokens": int(month["tokens"]),
            "day_cost_usd": float(day["cost_usd"]),
            "month_cost_usd": float(month["cost_usd"]),
        }

    def get_policy(self) -> dict[str, int]:
        totals = self.get_totals()
        near_limit = (
            totals["day_tokens"] >= self.daily_token_limit * 0.75
            or totals["month_tokens"] >= self.monthly_token_limit * 0.75
            or totals["day_cost_usd"] >= self.daily_usd_limit * 0.75
            or totals["month_cost_usd"] >= self.monthly_usd_limit * 0.75
        )
        return {
            "max_prompt_chars": self.low_budget_prompt_chars if near_limit else self.max_prompt_chars,
            "max_output_tokens": self.low_budget_max_output_tokens if near_limit else self.max_output_tokens,
        }

    def assert_can_spend(self, estimated_input_tokens: int) -> None:
        totals = self.get_totals()
        projected_day_tokens = totals["day_tokens"] + estimated_input_tokens
        projected_month_tokens = totals["month_tokens"] + estimated_input_tokens
        if projected_day_tokens > self.daily_token_limit or projected_month_tokens > self.monthly_token_limit:
            raise BudgetExceededError("Локальный лимит токенов для AI-проверок исчерпан.")
        if totals["day_cost_usd"] >= self.daily_usd_limit or totals["month_cost_usd"] >= self.monthly_usd_limit:
            raise BudgetExceededError("Локальный денежный лимит для AI-проверок исчерпан.")

    def record_usage(self, provider: str, usage: ProviderUsage) -> None:
        day_key, month_key = self._keys()
        for scope, key in (("days", day_key), ("months", month_key)):
            bucket = self._bucket(scope, key)
            bucket["tokens"] += usage.total_tokens
            bucket["cost_usd"] += usage.estimated_cost_usd
            bucket["calls"] += 1
            provider_bucket = bucket["providers"].setdefault(
                provider,
                {"tokens": 0, "cost_usd": 0.0, "calls": 0},
            )
            provider_bucket["tokens"] += usage.total_tokens
            provider_bucket["cost_usd"] += usage.estimated_cost_usd
            provider_bucket["calls"] += 1
        self._save_state()

    def format_status(self) -> str:
        totals = self.get_totals()
        return (
            f"День: {totals['day_tokens']}/{self.daily_token_limit} токенов, ${totals['day_cost_usd']:.4f}/${self.daily_usd_limit:.2f}. "
            f"Месяц: {totals['month_tokens']}/{self.monthly_token_limit} токенов, ${totals['month_cost_usd']:.4f}/${self.monthly_usd_limit:.2f}."
        )


class DebugLogger:
    def __init__(self) -> None:
        self.dir = Path(os.getenv("AI_DEBUG_DIR", ".cache/ai_debug"))
        self.dir.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        provider_key: str,
        prompt: str,
        *,
        status: str,
        response_json: dict[str, Any] | None = None,
        response_text: str = "",
        parsed_payload: dict[str, Any] | None = None,
        error: str = "",
        usage: ProviderUsage | None = None,
    ) -> str:
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", provider_key)[:80]
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.dir / f"{timestamp}_{safe_name}.json"
        record = {
            "timestamp_utc": timestamp,
            "provider_key": provider_key,
            "status": status,
            "prompt": prompt,
            "response_text": response_text,
            "response_json": make_json_safe(response_json),
            "parsed_payload": make_json_safe(parsed_payload),
            "error": error,
            "usage": {
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
                "cached_tokens": usage.cached_tokens if usage else 0,
                "total_tokens": usage.total_tokens if usage else 0,
                "estimated_cost_usd": usage.estimated_cost_usd if usage else 0.0,
            },
        }
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        latest = self.dir / "latest.json"
        latest.write_text(str(path), encoding="utf-8")
        return str(path)


def make_json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return make_json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): make_json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [make_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [make_json_safe(item) for item in value]
    return value


def estimate_text_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def trim_text(text: str, limit: int) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 20] + "\n... [truncated]"


def output_tokens_for_attempt(provider_key: str, policy: dict[str, int], attempt_no: int) -> int:
    base_tokens = max(256, int(policy["max_output_tokens"]))
    if not provider_key.startswith("openrouter:"):
        return base_tokens

    retry_step = int(os.getenv("OPENROUTER_RETRY_TOKEN_STEP", str(max(400, base_tokens // 2))))
    retry_cap = max(base_tokens, int(os.getenv("OPENROUTER_RETRY_MAX_OUTPUT_TOKENS", str(max(base_tokens, 5000)))))
    return min(retry_cap, base_tokens + max(0, attempt_no - 1) * retry_step)


def file_priority(path: str) -> int:
    lowered = path.lower()
    score = 0
    important_names = (
        "readme",
        "package.json",
        "requirements.txt",
        "tsconfig.json",
        "dockerfile",
        "main.",
        "index.",
        "app.",
        "server.",
        "bot.",
        "handler",
        "service",
        "router",
        "controller",
        "middleware",
        "config",
    )
    for marker in important_names:
        if marker in lowered:
            score += 5
    if lowered.endswith((".ts", ".tsx", ".js", ".py", ".java", ".go", ".rs")):
        score += 3
    if "/test" in lowered or lowered.endswith((".spec.ts", ".test.ts", ".spec.js", ".test.js", "_test.py")):
        score += 2
    return score


def select_reviewed_files(snapshot: "RepoSnapshot") -> list["RepoFile"]:
    reviewed_limit = int(os.getenv("AI_REVIEWED_FILES_LIMIT", "16"))
    source_files = [
        file
        for file in snapshot.files
        if any(file.path.endswith(ext) for ext in (".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".php", ".rb", ".cs", ".kt", ".rs"))
    ]
    return sorted(source_files, key=lambda item: (file_priority(item.path), len(item.content)), reverse=True)[:reviewed_limit]


def clamp_score(value: object, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(5, min(95, number))


def safe_int(value: object, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def sanitize_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def sanitize_string_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [item for item in (sanitize_text(part) for part in raw) if item]


def sanitize_findings(raw: object) -> list["Finding"]:
    from bot_runtime import Finding

    if not isinstance(raw, list):
        return []
    findings: list[Finding] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "medium")).lower()
        if severity not in {"low", "medium", "high"}:
            severity = "medium"
        title = sanitize_text(item.get("title"))
        detail = sanitize_text(item.get("detail"))
        if title and detail:
            findings.append(Finding(title=title, detail=detail, severity=severity))
    findings.sort(key=lambda item: {"low": 1, "medium": 2, "high": 3}[item.severity], reverse=True)
    return findings


def source_comment_stats(snapshot: "RepoSnapshot") -> tuple[int, int]:
    source_exts = (".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".php", ".rb", ".cs", ".kt", ".rs")
    total_lines = 0
    comment_lines = 0
    for file in snapshot.files:
        if not file.path.endswith(source_exts):
            continue
        for raw_line in file.content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            total_lines += 1
            if line.startswith("//") or line.startswith("#") or line.startswith("/*") or line.startswith("*"):
                comment_lines += 1
    return comment_lines, total_lines


def has_browser_frontend(snapshot: "RepoSnapshot") -> bool:
    frontend_markers = (
        ".html",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".vue",
        ".svelte",
    )
    frontend_dirs = ("/public/", "/frontend/", "/client/", "/web/")
    for file in snapshot.files:
        lowered = file.path.lower()
        if lowered.endswith(frontend_markers):
            return True
        if any(marker in lowered for marker in frontend_dirs):
            return True
    return False


def should_filter_comment_claim(text: str, snapshot: "RepoSnapshot") -> bool:
    lowered = text.lower()
    if not any(marker in lowered for marker in ("коммент", "jsdoc", "create table if not exists")):
        return False
    comment_lines, total_lines = source_comment_stats(snapshot)
    if total_lines == 0:
        return True
    return (comment_lines / total_lines) < 0.02


def post_filter_ai_review(review: "ReviewResult", snapshot: "RepoSnapshot") -> "ReviewResult":
    from bot_runtime import Finding, ReviewResult

    web_security_markers = ("xss", "csrf", "content security policy", "csp")
    truncation_markers = ("обрезан", "обрезана", "обрезанн", "незавершен", "незавершена", "неполная реализация")
    browser_frontend = has_browser_frontend(snapshot)

    def drop_text_item(text: str) -> bool:
        lowered = text.lower()
        if not browser_frontend and any(marker in lowered for marker in web_security_markers):
            return True
        if any(marker in lowered for marker in truncation_markers):
            return True
        if should_filter_comment_claim(text, snapshot):
            return True
        return False

    filtered_issues: list[Finding] = []
    removed_items = 0
    for issue in review.issues:
        haystack = f"{issue.title} {issue.detail}"
        if drop_text_item(haystack):
            removed_items += 1
            continue
        filtered_issues.append(issue)

    filtered_recommendations = []
    for item in review.recommendations:
        if drop_text_item(item):
            removed_items += 1
            continue
        filtered_recommendations.append(item)

    filtered_analysis = []
    for item in review.detailed_analysis:
        if drop_text_item(item):
            removed_items += 1
            continue
        filtered_analysis.append(item)

    filtered_signals = []
    for item in review.ai_detection_signals:
        if drop_text_item(item):
            removed_items += 1
            continue
        filtered_signals.append(item)

    filtered_rationale = []
    for item in review.ai_rationale:
        if drop_text_item(item):
            removed_items += 1
            continue
        filtered_rationale.append(item)

    if removed_items == 0:
        return review

    adjusted_ai_probability = review.ai_probability_percent
    if removed_items > 0:
        adjusted_ai_probability = max(5, review.ai_probability_percent - min(20, removed_items * 4))

    if not filtered_rationale:
        filtered_rationale = ["AI-оценка скорректирована после удаления неподтвержденных шаблонных сигналов."]

    if not filtered_signals:
        filtered_signals = ["После локальной фильтрации остались только подтверждаемые по коду сигналы."]

    return ReviewResult(
        summary=review.summary,
        strengths=review.strengths,
        issues=filtered_issues[:5],
        recommendations=filtered_recommendations[:4],
        ai_probability_percent=adjusted_ai_probability,
        ai_rationale=filtered_rationale[:3],
        overall_score_percent=review.overall_score_percent,
        detailed_analysis=filtered_analysis[:6],
        ai_detection_signals=filtered_signals[:5],
        reviewed_files=review.reviewed_files,
        assignment_summary=review.assignment_summary,
        assignment_findings=review.assignment_findings,
        review_source=review.review_source,
        provider_attempts=review.provider_attempts,
    )


def infer_overall_score_from_payload(payload: dict[str, Any]) -> int:
    strengths = sanitize_string_list(payload.get("strengths"))
    issues = sanitize_findings(payload.get("issues"))
    recommendations = sanitize_string_list(payload.get("recommendations"))
    score = 55
    score += min(15, len(strengths) * 5)
    score -= sum({"low": 4, "medium": 10, "high": 18}[issue.severity] for issue in issues[:5])
    if not issues:
        score += 6
    if recommendations and not issues:
        score -= 4
    return clamp_score(score, 50)


def infer_ai_probability_from_payload(payload: dict[str, Any]) -> int:
    rationale = sanitize_string_list(payload.get("ai_rationale"))
    signals = sanitize_string_list(payload.get("ai_detection_signals"))
    text = " ".join([*rationale, *signals, sanitize_text(payload.get("summary"))]).lower()
    score = 22

    positive_markers = (
        "ai",
        "нейро",
        "академич",
        "полирован",
        "шаблон",
        "стериль",
        "copypaste",
        "copy-paste",
        "повтор",
        "verbose",
        "длинн",
        "naming",
        "generic",
    )
    negative_markers = (
        "ручн",
        "debug",
        "чернов",
        "manual",
        "живой",
        "неровн",
        "нович",
    )

    score += min(30, len(signals) * 6)
    for marker in positive_markers:
        if marker in text:
            score += 6
    for marker in negative_markers:
        if marker in text:
            score -= 5
    return clamp_score(score, 35)


META_THINKING_MARKERS = (
    "okay, let's start by understanding the task",
    "let's tackle this code review",
    "the user wants me to",
    "the goal is to provide",
    "first, i need to",
    "i need to review",
    "i need to follow the user's instructions",
    "i'll go through each file",
    "the main files are",
    "the readme is also important",
    "the user provided a detailed schema",
    "now, considering the ai detection signals",
    "putting this all together",
    "i need to analyze the provided repository",
    "looking at the code samples provided",
    "the repo is",
    "the files analyzed include",
    "return a json response",
    "верни только json",
    "нужно вернуть json",
)


def looks_like_meta_thinking(text: str) -> bool:
    lowered = re.sub(r"\s+", " ", text.strip().lower())
    if not lowered:
        return False
    return any(marker in lowered for marker in META_THINKING_MARKERS)


SCHEMA_FRAGMENT_MARKERS = (
    "ai detection signals",
    "reviewed files",
    "reviewed_files",
    "assignment summary",
    "assignment_summary",
    "assignment findings",
    "assignment_findings",
    "ai probability",
    "ai_probability",
    "ai rationale",
    "ai_rationale",
    "overall score",
    "structured json report",
    "json report",
    "return valid json only",
    "recommendations, etc",
)


def looks_like_schema_fragment(text: str) -> bool:
    lowered = re.sub(r"\s+", " ", text.strip().lower())
    if not lowered:
        return False
    if lowered.startswith("{") or lowered.startswith('"summary"') or lowered.startswith('{"summary"'):
        return True
    return any(marker in lowered for marker in SCHEMA_FRAGMENT_MARKERS)


def is_usable_review_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if looks_like_meta_thinking(stripped) or looks_like_schema_fragment(stripped):
        return False
    if looks_mostly_non_russian(stripped):
        return False
    return True


def filter_bad_sentences(items: list[str]) -> list[str]:
    return [item for item in items if is_usable_review_text(item)]


def looks_mostly_non_russian(text: str) -> bool:
    if not text:
        return False
    latin = len(re.findall(r"[A-Za-z]", text))
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", text))
    if latin < 20:
        return False
    return cyrillic == 0 or latin > cyrillic * 2


def extract_reasoning_text(raw_response: dict[str, Any] | None) -> str:
    if not isinstance(raw_response, dict):
        return ""
    choices = raw_response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""

    parts: list[str] = []
    direct_reasoning = sanitize_text(message.get("reasoning"))
    if direct_reasoning:
        parts.append(direct_reasoning)

    reasoning_details = message.get("reasoning_details")
    if isinstance(reasoning_details, list):
        for item in reasoning_details:
            if isinstance(item, dict):
                text = sanitize_text(item.get("text"))
                if text:
                    parts.append(text)

    unique_parts: list[str] = []
    seen: set[str] = set()
    for part in parts:
        normalized = re.sub(r"\s+", " ", part.strip())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_parts.append(part.strip())
    return "\n\n".join(unique_parts)


def _split_review_sentences(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    parts = re.split(r"(?<=[.!?])\s+", normalized)
    ignored_markers = (
        "starting with the strengths",
        "next, the issues",
        "for recommendations",
        "looking at ai detection signals",
        "putting this all together",
        "overall score",
        "сильные стороны",
        "что поправить",
        "рекомендации",
        "сигналы ai",
        "детальный разбор",
    )
    result: list[str] = []
    for part in parts:
        cleaned = part.strip(" -•\t\r\n")
        lowered = cleaned.lower().rstrip(".:;")
        if len(cleaned) < 12:
            continue
        if lowered in ignored_markers:
            continue
        result.append(cleaned)
    return result


def _pick_section(text: str, start_markers: tuple[str, ...], end_markers: tuple[str, ...]) -> str:
    lowered = text.lower()
    start_index: int | None = None
    for marker in start_markers:
        idx = lowered.find(marker)
        if idx != -1 and (start_index is None or idx < start_index):
            start_index = idx
    if start_index is None:
        return ""

    section = text[start_index:]
    section_lower = lowered[start_index:]
    end_index: int | None = None
    for marker in end_markers:
        idx = section_lower.find(marker)
        if idx > 0 and (end_index is None or idx < end_index):
            end_index = idx
    return section[:end_index].strip() if end_index else section.strip()


def parse_review_text_fallback(text: str, base_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    base_payload = dict(base_payload or {})
    normalized = text.strip()
    if not normalized:
        return base_payload
    if normalized.startswith("{"):
        return base_payload

    summary = sanitize_text(base_payload.get("summary"))
    strengths = sanitize_string_list(base_payload.get("strengths"))
    recommendations = sanitize_string_list(base_payload.get("recommendations"))
    ai_signals = sanitize_string_list(base_payload.get("ai_detection_signals"))
    ai_rationale = sanitize_string_list(base_payload.get("ai_rationale"))
    detailed_analysis = sanitize_string_list(base_payload.get("detailed_analysis"))
    reviewed_files = sanitize_string_list(base_payload.get("reviewed_files"))
    issues = sanitize_findings(base_payload.get("issues"))

    full_sentences = filter_bad_sentences(_split_review_sentences(normalized))
    if not summary and full_sentences:
        summary = full_sentences[0]
    if not detailed_analysis:
        detailed_analysis = full_sentences[:6]

    strengths_section = _pick_section(
        normalized,
        ("starting with the strengths", "strengths", "сильные стороны"),
        ("next, the issues", "issues", "recommendations", "ai detection", "сигналы ai", "детальный разбор"),
    )
    issues_section = _pick_section(
        normalized,
        ("next, the issues", "issues", "what to fix", "что поправить", "проблемы"),
        ("recommendations", "ai detection", "сигналы ai", "детальный разбор"),
    )
    recommendations_section = _pick_section(
        normalized,
        ("for recommendations", "recommendations", "рекомендации"),
        ("putting this all together", "overall score", "ai probability", "вероятность использования ai"),
    )
    ai_section = _pick_section(
        normalized,
        ("looking at ai detection", "ai detection", "ai signals", "признаки ai", "сигналы ai"),
        ("for recommendations", "recommendations", "overall score", "итог"),
    )

    if not strengths:
        strengths = filter_bad_sentences(_split_review_sentences(strengths_section))[:3]
    if not recommendations:
        recommendations = filter_bad_sentences(_split_review_sentences(recommendations_section))[:4]
    if not ai_signals:
        ai_signals = filter_bad_sentences(_split_review_sentences(ai_section))[:5]
    if not ai_rationale:
        ai_rationale = ai_signals[:3]
    if not issues:
        issue_sentences = filter_bad_sentences(_split_review_sentences(issues_section))[:5]
        issues = [
            {
                "title": sentence[:72].rstrip(" .,:;"),
                "detail": sentence,
                "severity": "medium",
            }
            for sentence in issue_sentences
        ]

    if summary and is_usable_review_text(summary):
        base_payload["summary"] = summary
    if strengths:
        base_payload["strengths"] = strengths
    if issues:
        base_payload["issues"] = [
            {
                "title": issue.title,
                "detail": issue.detail,
                "severity": issue.severity,
            }
            if hasattr(issue, "title")
            else issue
            for issue in issues
        ]
    if recommendations:
        base_payload["recommendations"] = recommendations
    if detailed_analysis:
        base_payload["detailed_analysis"] = detailed_analysis
    if ai_signals:
        base_payload["ai_detection_signals"] = ai_signals
    if ai_rationale:
        base_payload["ai_rationale"] = ai_rationale
    if reviewed_files:
        base_payload["reviewed_files"] = reviewed_files
    return base_payload


def is_retryable_provider_error(provider_key: str, exc: ProviderError) -> bool:
    if exc.temporary:
        return True

    message = str(exc).lower()
    retryable_markers = (
        "incomplete structured review",
        "does not include numeric scores",
        "did not include numeric scores",
        "не вернула json",
        "raw preview",
        "unsupported format content",
        "неподдерживаемый формат",
        "invalid json",
        "meta reasoning instead of review",
        "non-russian review",
        "no content generated",
        "finished with provider error",
        "truncated review",
    )
    return provider_key.startswith("openrouter:") and any(marker in message for marker in retryable_markers)


def parse_llm_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.S)
        if not match:
            raise ProviderError("Модель не вернула JSON")
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ProviderError("AI returned invalid JSON", temporary=False) from exc


def extract_text_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            text = extract_text_content(item)
            if text:
                parts.append(text)
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        preferred_keys = ("text", "content", "output_text", "arguments", "value")
        parts: list[str] = []
        for key in preferred_keys:
            if key in content:
                text = extract_text_content(content[key])
                if text:
                    parts.append(text)
        if parts:
            return "\n".join(parts)
        for value in content.values():
            text = extract_text_content(value)
            if text:
                parts.append(text)
        if parts:
            return "\n".join(parts)
        return json.dumps(content, ensure_ascii=False)
    if content is None:
        return ""
    if isinstance(content, (int, float, bool)):
        return str(content)
    return json.dumps(content, ensure_ascii=False)


def build_llm_prompt(snapshot: "RepoSnapshot", heuristic: "ReviewResult", max_prompt_chars: int) -> str:
    readme = next((file for file in snapshot.files if file.path.split("/")[-1].lower() in {"readme.md", "readme.txt"}), None)
    file_list_limit = int(os.getenv("AI_FILE_LIST_LIMIT", "80"))
    file_list = "\n".join(f"- {file.path}" for file in snapshot.files[:file_list_limit])
    selected_files = select_reviewed_files(snapshot)
    budget_per_file = max(1200, max_prompt_chars // max(1, len(selected_files) + 4))
    code_blocks = []
    for file in selected_files:
        code_blocks.append(f"FILE: {file.path}\n```text\n{trim_text(file.content, budget_per_file)}\n```")

    prompt = (
        "Ты преподаватель программирования и проверяешь учебную работу студента-новичка по репозиторию. Будь доброжелательным, но строгим. "
        "Смотри на проект как на студенческую сдачу: оценивай корректность, понятность, структуру, аккуратность и признаки того, понимает ли студент свой код. "
        "Если проект выглядит слишком ровным, взрослым по структуре и при этом без тестов, "
        "вероятность использования AI должна расти. Маленькие, сырые и неровные учебные проекты обычно понижают эту вероятность.\n\n"
        "Не требуй от учебной работы production-уровня, но отмечай, где студент недоделал базовые вещи, которые уже важны даже для учебного проекта. "
        "Пиши только о тех проблемах, которые действительно видны в файлах. Не придумывай уязвимости или недостающие части кода, если их нельзя подтвердить по показанным фрагментам. "
        "Если репозиторий не содержит явного браузерного фронтенда, не приписывай ему XSS, CSRF и похожие web-угрозы. "
        "Если какой-то фрагмент кода в prompt обрезан, не делай вывод, что функция незавершена или сломана только из-за этого. "
        "Проверь прежде всего основные файлы проекта: входные точки, конфиг, README, основные модули и самые крупные файлы. "
        "Не копируй эвристику дословно, а делай самостоятельные выводы по файлам. "
        "Используй признаки возможной AI-генерации:\n"
        "- слишком академичный стиль;\n"
        "- избыточные и очевидные комментарии;\n"
        "- слишком шаблонный или громоздкий нейминг;\n"
        "- логическая стерильность и повторяющиеся конструкции;\n"
        "- устаревшие конструкции в современном проекте;\n"
        "- слабая обработка ошибок и граничных случаев;\n"
        "- отсутствие следов живой командной разработки.\n\n"
        "Результат должен быть подробным, но компактным. "
        "Не больше 3 сильных сторон, 5 проблем, 4 рекомендаций, 6 пунктов детального разбора и 5 AI-сигналов. "
        "Верни только JSON без Markdown по схеме:\n"
        "{"
        '"summary":"string",'
        '"strengths":["string"],'
        '"issues":[{"title":"string","detail":"string","severity":"low|medium|high"}],'
        '"recommendations":["string"],'
        '"detailed_analysis":["string"],'
        '"ai_detection_signals":["string"],'
        '"reviewed_files":["string"],'
        '"ai_probability_percent":0,'
        '"ai_rationale":["string"],'
        '"overall_score_percent":0'
        "}\n\n"
        f"REPO: {snapshot.owner}/{snapshot.name}\n"
        f"DESCRIPTION: {snapshot.description or 'нет описания'}\n"
        f"DEFAULT_BRANCH: {snapshot.default_branch}\n"
        f"FILES_ANALYZED: {len(snapshot.files)}\n"
        f"FILES_SKIPPED: {snapshot.skipped_files}\n\n"
        f"REVIEWED_FILES:\n" + "\n".join(f"- {file.path}" for file in selected_files) + "\n\n"
        f"FILES:\n{file_list}\n\n"
        f"README:\n{trim_text(readme.content if readme else 'README отсутствует', max(2000, max_prompt_chars // 3))}\n\n"
        f"CODE_SAMPLES:\n{'\n\n'.join(code_blocks)}"
    )
    return trim_text(prompt, max_prompt_chars)


def merge_llm_with_heuristic(
    payload: dict[str, Any],
    heuristic: "ReviewResult",
    provider_key: str,
    attempts: list[str],
) -> "ReviewResult":
    from bot_runtime import ReviewResult

    strengths = sanitize_string_list(payload.get("strengths")) or heuristic.strengths
    recommendations = sanitize_string_list(payload.get("recommendations")) or heuristic.recommendations
    ai_rationale = sanitize_string_list(payload.get("ai_rationale"))
    detailed_analysis = sanitize_string_list(payload.get("detailed_analysis"))
    ai_detection_signals = sanitize_string_list(payload.get("ai_detection_signals"))
    reviewed_files = sanitize_string_list(payload.get("reviewed_files"))
    issues = sanitize_findings(payload.get("issues")) or heuristic.issues
    summary = sanitize_text(payload.get("summary")) or heuristic.summary
    if not ai_rationale:
        ai_rationale = ai_detection_signals[:3] or ["AI-оценка построена по структуре проекта, стилю кода и выбранным признакам."]

    llm_ai = clamp_score(payload.get("ai_probability_percent"), heuristic.ai_probability_percent)
    llm_score = clamp_score(payload.get("overall_score_percent"), heuristic.overall_score_percent)
    blended_ai = clamp_score(round(llm_ai * 0.9 + heuristic.ai_probability_percent * 0.1), heuristic.ai_probability_percent)
    blended_score = clamp_score(round(llm_score * 0.85 + heuristic.overall_score_percent * 0.15), heuristic.overall_score_percent)

    return ReviewResult(
        summary=summary,
        strengths=strengths[:4],
        issues=issues[:5],
        recommendations=recommendations[:4],
        ai_probability_percent=blended_ai,
        ai_rationale=ai_rationale[:3],
        overall_score_percent=blended_score,
        detailed_analysis=detailed_analysis[:6],
        ai_detection_signals=ai_detection_signals[:5],
        reviewed_files=reviewed_files[:8],
        review_source=f"AI: {provider_key}",
        provider_attempts=attempts,
    )


def build_llm_prompt_v2(snapshot: "RepoSnapshot", max_prompt_chars: int) -> str:
    readme = next((file for file in snapshot.files if file.path.split("/")[-1].lower() in {"readme.md", "readme.txt"}), None)
    file_list_limit = int(os.getenv("AI_FILE_LIST_LIMIT", "80"))
    file_list = "\n".join(f"- {file.path}" for file in snapshot.files[:file_list_limit])
    selected_files = select_reviewed_files(snapshot)
    budget_per_file = max(1200, max_prompt_chars // max(1, len(selected_files) + 4))
    code_blocks = [
        f"FILE: {file.path}\n```text\n{trim_text(file.content, budget_per_file)}\n```"
        for file in selected_files
    ]
    assignment_block = ""
    assignment_text = getattr(snapshot, "assignment_text", "").strip()
    if assignment_text:
        assignment_block = (
            f"\n\nASSIGNMENT_FILE: {getattr(snapshot, 'assignment_filename', 'uploaded-task')}\n"
            f"ASSIGNMENT_TEXT:\n{trim_text(assignment_text, max(1200, max_prompt_chars // 3))}"
        )
    schema_fields = [
        '"summary":"string"',
        '"strengths":["string"]',
        '"issues":[{"title":"string","detail":"string","severity":"low|medium|high"}]',
        '"recommendations":["string"]',
        '"detailed_analysis":["string"]',
        '"ai_detection_signals":["string"]',
        '"ai_probability_percent":0',
        '"ai_rationale":["string"]',
        '"overall_score_percent":0',
    ]
    if assignment_text:
        schema_fields.insert(6, '"assignment_summary":"string"')
        schema_fields.insert(7, '"assignment_findings":["string"]')
    schema = "{" + ",".join(schema_fields) + "}"

    prompt = (
        "Ты преподаватель программирования и проверяешь учебную работу студента-новичка по репозиторию. Будь доброжелательным, но строгим. "
        "Смотри на проект как на студенческую сдачу: оценивай корректность, понятность, структуру, аккуратность и признаки того, понимает ли студент свой код. "
        "Сделай самостоятельный разбор по реальным файлам проекта. Не пересказывай локальную эвристику и не выдумывай отсутствующие детали.\n\n"
        "Не требуй от учебной работы production-уровня, но отмечай, где студент недоделал базовые вещи, которые уже важны даже для учебного проекта. "
        "Пиши только о тех проблемах, которые действительно видны в файлах. Не придумывай уязвимости или недостающие части кода, если их нельзя подтвердить по показанным фрагментам. "
        "Если репозиторий не содержит явного браузерного фронтенда, не приписывай ему XSS, CSRF и похожие web-угрозы. "
        "Если какой-то фрагмент кода в prompt обрезан, не делай вывод, что функция незавершена или сломана только из-за этого. "
        "Проверь прежде всего входные точки, конфиг, README, основные модули, самые крупные файлы и все, что влияет на пользовательский сценарий. "
        "Если приложено ТЗ, сравни проект с ним и явно отметь, что покрыто, а что выглядит недоделанным или отсутствующим.\n\n"
        "Используй признаки возможной AI-генерации:\n"
        "- слишком академичный стиль;\n"
        "- избыточные и очевидные комментарии;\n"
        "- шаблонный или громоздкий нейминг;\n"
        "- логическая стерильность и повторы;\n"
        "- устаревшие конструкции для современного проекта;\n"
        "- слабая обработка ошибок и edge cases;\n"
        "- отсутствие следов живой разработки и ручной эволюции.\n\n"
        "Результат должен быть конкретным, по файлам и признакам. Не больше 3 сильных сторон, 5 проблем, 4 рекомендаций, 6 пунктов детального разбора и 5 AI-сигналов. "
        "Верни только JSON без Markdown по схеме:\n"
        f"{schema}\n\n"
        f"REPO: {snapshot.owner}/{snapshot.name}\n"
        f"DESCRIPTION: {snapshot.description or 'нет описания'}\n"
        f"DEFAULT_BRANCH: {snapshot.default_branch}\n"
        f"FILES_ANALYZED: {len(snapshot.files)}\n"
        f"FILES_SKIPPED: {snapshot.skipped_files}\n\n"
        f"REVIEWED_FILES:\n" + "\n".join(f"- {file.path}" for file in selected_files) + "\n\n"
        f"FILES:\n{file_list}\n\n"
        f"README:\n{trim_text(readme.content if readme else 'README отсутствует', max(2000, max_prompt_chars // 3))}\n\n"
        f"CODE_SAMPLES:\n{'\n\n'.join(code_blocks)}"
        f"{assignment_block}"
    )
    return trim_text(prompt, max_prompt_chars)


def build_ai_review_result_v2(
    payload: dict[str, Any],
    provider_key: str,
    attempts: list[str],
    fallback_reviewed_files: list[str],
) -> "ReviewResult":
    from bot_runtime import ReviewResult

    detailed_analysis = sanitize_string_list(payload.get("detailed_analysis"))
    summary = sanitize_text(payload.get("summary")) or (detailed_analysis[0] if detailed_analysis else "")
    strengths = sanitize_string_list(payload.get("strengths")) or detailed_analysis[:3]
    issues = sanitize_findings(payload.get("issues"))
    recommendations = sanitize_string_list(payload.get("recommendations"))
    ai_detection_signals = sanitize_string_list(payload.get("ai_detection_signals"))
    ai_rationale = sanitize_string_list(payload.get("ai_rationale")) or ai_detection_signals[:3]
    has_native_detailed_analysis = bool(detailed_analysis)
    has_native_recommendations = bool(recommendations)
    has_native_ai_rationale = bool(sanitize_string_list(payload.get("ai_rationale")))

    detailed_analysis = filter_bad_sentences(detailed_analysis)
    strengths = filter_bad_sentences(strengths)
    recommendations = filter_bad_sentences(recommendations)
    ai_detection_signals = filter_bad_sentences(ai_detection_signals)
    ai_rationale = filter_bad_sentences(ai_rationale)
    issues = [
        issue
        for issue in issues
        if is_usable_review_text(issue.title) and is_usable_review_text(issue.detail)
    ]

    if summary and (looks_like_meta_thinking(summary) or looks_like_schema_fragment(summary)):
        raise ProviderError("AI returned meta reasoning instead of review", temporary=False)
    if looks_mostly_non_russian(summary):
        raise ProviderError("AI returned non-Russian review", temporary=False)

    meta_fragments = [summary, *strengths[:2], *detailed_analysis[:2], *ai_rationale[:2], *ai_detection_signals[:2]]
    if sum(1 for fragment in meta_fragments if fragment and looks_like_meta_thinking(fragment)) >= 2:
        raise ProviderError("AI returned meta reasoning instead of review", temporary=False)
    if "[length]" in provider_key.lower() and (
        not has_native_detailed_analysis or not has_native_recommendations or not has_native_ai_rationale
    ):
        raise ProviderError("AI returned truncated review", temporary=False)

    if not recommendations and issues:
        recommendations = [f"Исправить проблему: {issue.title}" for issue in issues[:3]]
    if not summary or (not strengths and not issues and not recommendations):
        raise ProviderError("AI returned incomplete structured review", temporary=False)
    if not ai_rationale:
        ai_rationale = ["AI-оценка построена по структуре проекта, стилю кода и выбранным признакам."]

    raw_ai_probability = payload.get("ai_probability_percent")
    raw_overall_score = payload.get("overall_score_percent")
    inferred_ai_probability = infer_ai_probability_from_payload(payload)
    inferred_overall_score = infer_overall_score_from_payload(payload)
    ai_probability = (
        inferred_ai_probability
        if raw_ai_probability is None
        else clamp_score(raw_ai_probability, inferred_ai_probability)
    )
    overall_score = (
        inferred_overall_score
        if raw_overall_score is None
        else clamp_score(raw_overall_score, inferred_overall_score)
    )

    return ReviewResult(
        summary=summary,
        strengths=strengths[:4],
        issues=issues[:5],
        recommendations=recommendations[:4],
        ai_probability_percent=ai_probability,
        ai_rationale=ai_rationale[:3],
        overall_score_percent=overall_score,
        detailed_analysis=detailed_analysis[:6],
        ai_detection_signals=ai_detection_signals[:5],
        reviewed_files=(sanitize_string_list(payload.get("reviewed_files")) or fallback_reviewed_files)[:8],
        assignment_summary=sanitize_text(payload.get("assignment_summary")),
        assignment_findings=sanitize_string_list(payload.get("assignment_findings"))[:5],
        review_source=f"AI: {provider_key}",
        provider_attempts=attempts,
    )


class BaseProvider:
    provider_name = "base"

    def __init__(self) -> None:
        self.session = requests.Session()

    def is_configured(self) -> bool:
        return False

    def label(self) -> str:
        return self.provider_name

    def generate(self, prompt: str, max_output_tokens: int) -> ProviderResult:
        raise NotImplementedError


class GeminiProvider(BaseProvider):
    provider_name = "gemini"

    def __init__(self) -> None:
        super().__init__()
        self.api_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite").strip()
        self.input_per_million = float(os.getenv("GEMINI_INPUT_COST_PER_MTOK", "0.10"))
        self.output_per_million = float(os.getenv("GEMINI_OUTPUT_COST_PER_MTOK", "0.40"))

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def label(self) -> str:
        return f"{self.provider_name}:{self.model}"

    def generate(self, prompt: str, max_output_tokens: int) -> ProviderResult:
        response = self.session.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            params={"key": self.api_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "maxOutputTokens": max_output_tokens,
                    "responseMimeType": "application/json",
                },
            },
            timeout=self.timeout_seconds,
        )
        if response.status_code in {429, 500, 502, 503, 504}:
            raise ProviderError(f"Gemini temporarily unavailable: {response.status_code}")
        if response.status_code >= 400:
            raise ProviderError(f"Gemini request failed: {response.status_code}", temporary=False)
        data = response.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Gemini returned unexpected response: {exc}") from exc

        usage_data = data.get("usageMetadata", {})
        usage = ProviderUsage(
            prompt_tokens=safe_int(usage_data.get("promptTokenCount"), estimate_text_tokens(prompt)),
            completion_tokens=safe_int(usage_data.get("candidatesTokenCount"), estimate_text_tokens(text)),
            cached_tokens=safe_int(usage_data.get("cachedContentTokenCount"), 0),
        )
        usage.total_tokens = max(usage.prompt_tokens + usage.completion_tokens, safe_int(usage_data.get("totalTokenCount"), 0))
        usage.estimated_cost_usd = (
            usage.prompt_tokens * self.input_per_million / 1_000_000
            + usage.completion_tokens * self.output_per_million / 1_000_000
        )
        try:
            payload = parse_llm_json(text)
        except ProviderError as exc:
            raise ProviderError(str(exc), temporary=False, usage=usage) from exc
        return ProviderResult(payload=payload, usage=usage, provider_key=self.label(), raw_text=text, raw_response=data)


class OpenRouterProvider(BaseProvider):
    provider_name = "openrouter"
    temporary_statuses = {408, 409, 429, 500, 502, 503, 504}

    def __init__(self) -> None:
        super().__init__()
        self.api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        self.model = os.getenv("OPENROUTER_MODEL", "openrouter/free").strip()
        self.input_per_million = float(os.getenv("OPENROUTER_INPUT_COST_PER_MTOK", "0.0"))
        self.output_per_million = float(os.getenv("OPENROUTER_OUTPUT_COST_PER_MTOK", "0.0"))
        self.timeout_seconds = int(os.getenv("OPENROUTER_TIMEOUT_SECONDS", "180"))

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def label(self) -> str:
        return f"{self.provider_name}:{self.model}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com",
            "X-Title": "student-repo-checker-bot",
        }

    def _messages(self, prompt: str) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": "Ты преподаватель программирования и проверяешь студенческую учебную работу по репозиторию. Давай строгую, но педагогичную обратную связь, ориентируясь на уровень студента, а не на production-стандарты. Верни только валидный JSON на русском языке. Не показывай chain-of-thought, план, разбор задания, служебные рассуждения или описание схемы ответа.",
            },
            {"role": "user", "content": prompt},
        ]

    def _build_payload(self, prompt: str, max_output_tokens: int, *, structured_output: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.2,
            "max_tokens": max_output_tokens,
            "usage": {"include": True},
            "messages": self._messages(prompt),
        }
        if structured_output:
            payload["response_format"] = {"type": "json_object"}
            payload["plugins"] = [{"id": "response-healing"}]
        return payload

    def _format_error_message(self, response: requests.Response, data: dict[str, Any] | None = None) -> str:
        error = data.get("error") if isinstance(data, dict) else None
        code = ""
        message = ""
        metadata = ""
        request_id = (
            response.headers.get("x-request-id")
            or response.headers.get("request-id")
            or response.headers.get("cf-ray")
            or ""
        )
        if isinstance(error, dict):
            code = sanitize_text(error.get("code"))
            message = sanitize_text(error.get("message"))
            raw_metadata = error.get("metadata")
            if isinstance(raw_metadata, dict) and raw_metadata:
                metadata = "; ".join(f"{key}={value}" for key, value in list(raw_metadata.items())[:4])
        parts = [f"OpenRouter error {response.status_code}"]
        if code:
            parts.append(code)
        if message:
            parts.append(message)
        if metadata:
            parts.append(f"metadata: {metadata}")
        if request_id:
            parts.append(f"request_id: {request_id}")
        return " | ".join(parts)

    def _raise_for_error_response(self, response: requests.Response, data: dict[str, Any] | None = None) -> None:
        parsed = data
        if parsed is None:
            try:
                candidate = response.json()
                if isinstance(candidate, dict):
                    parsed = candidate
            except ValueError:
                parsed = None

        temporary = response.status_code in self.temporary_statuses
        if response.status_code == 404:
            temporary = False
        if response.status_code >= 400:
            raise ProviderError(self._format_error_message(response, parsed), temporary=temporary)
        if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
            raise ProviderError(self._format_error_message(response, parsed), temporary=temporary)

    def _build_usage(self, prompt: str, text: str, usage_data: dict[str, Any]) -> ProviderUsage:
        usage = ProviderUsage(
            prompt_tokens=safe_int(usage_data.get("prompt_tokens"), estimate_text_tokens(prompt)),
            completion_tokens=safe_int(usage_data.get("completion_tokens"), estimate_text_tokens(text)),
            cached_tokens=safe_int((usage_data.get("prompt_tokens_details") or {}).get("cached_tokens"), 0),
        )
        usage.total_tokens = safe_int(usage_data.get("total_tokens"), usage.prompt_tokens + usage.completion_tokens)
        if "cost" in usage_data and isinstance(usage_data["cost"], (int, float)) and self.model == "openrouter/free":
            usage.estimated_cost_usd = 0.0
        else:
            usage.estimated_cost_usd = (
                usage.prompt_tokens * self.input_per_million / 1_000_000
                + usage.completion_tokens * self.output_per_million / 1_000_000
            )
        return usage

    def _provider_key(self, data: dict[str, Any]) -> str:
        routed_model = sanitize_text(data.get("model")) or self.model
        routed_provider = sanitize_text(data.get("provider"))
        finish_reason = sanitize_text((data.get("choices") or [{}])[0].get("finish_reason"))
        provider_key = f"{self.provider_name}:{routed_model}"
        if routed_provider:
            provider_key += f" via {routed_provider}"
        if finish_reason:
            provider_key += f" [{finish_reason}]"
        return provider_key

    def _extract_response_text(self, data: dict[str, Any]) -> tuple[str, str]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("OpenRouter returned no choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ProviderError("OpenRouter returned unexpected response: missing message")

        reasoning_text = extract_reasoning_text(data)
        text = extract_text_content(message.get("content"))
        usable_reasoning = "\n\n".join(filter_bad_sentences(_split_review_sentences(reasoning_text)))
        if not text and usable_reasoning:
            text = usable_reasoning
        if not text:
            finish_reason = sanitize_text(choices[0].get("finish_reason"))
            if finish_reason == "error":
                raise ProviderError("OpenRouter finished with provider error")
            raise ProviderError("OpenRouter returned no content generated", temporary=False)
        return text, usable_reasoning

    def generate(self, prompt: str, max_output_tokens: int) -> ProviderResult:
        headers = self._headers()
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "max_tokens": max_output_tokens,
            "usage": {"include": True},
            "response_format": {"type": "json_object"},
            "plugins": [{"id": "response-healing"}],
            "messages": [
                {
                    "role": "system",
                    "content": "Ты строгий, но доброжелательный ревьюер учебных репозиториев. Верни только валидный JSON на русском языке. Не показывай chain-of-thought, план, разбор задания, служебные рассуждения или описание схемы ответа.",
                },
                {"role": "user", "content": prompt},
            ],
        }
        response = self.session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=self.timeout_seconds,
        )
        if response.status_code == 400:
            fallback_payload = self._build_payload(prompt, max_output_tokens, structured_output=False)
            response = self.session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=fallback_payload,
                timeout=self.timeout_seconds,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(f"OpenRouter returned non-JSON response: {exc}") from exc
        self._raise_for_error_response(response, data)
        text, usable_reasoning = self._extract_response_text(data)

        usage_data = data.get("usage") or {}
        usage = self._build_usage(prompt, text, usage_data)
        provider_key = self._provider_key(data)
        try:
            payload = parse_llm_json(text)
        except ProviderError as exc:
            recovery_text = "\n\n".join(part for part in (text, usable_reasoning) if part).strip()
            recovered_payload = parse_review_text_fallback(recovery_text)
            if recovered_payload:
                return ProviderResult(
                    payload=recovered_payload,
                    usage=usage,
                    provider_key=provider_key,
                    raw_text=recovery_text,
                    raw_response=data,
                )
            preview = trim_text(recovery_text, 260).replace("\n", " ")
            raise ProviderError(f"{exc}. Raw preview: {preview}", temporary=False, usage=usage) from exc
        return ProviderResult(payload=payload, usage=usage, provider_key=provider_key, raw_text=text, raw_response=data)


class GroqProvider(BaseProvider):
    provider_name = "groq"

    def __init__(self) -> None:
        super().__init__()
        self.api_key = os.getenv("GROQ_API_KEY", "").strip()
        self.model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
        self.input_per_million = float(os.getenv("GROQ_INPUT_COST_PER_MTOK", "0.075"))
        self.output_per_million = float(os.getenv("GROQ_OUTPUT_COST_PER_MTOK", "0.30"))

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def label(self) -> str:
        return f"{self.provider_name}:{self.model}"

    def generate(self, prompt: str, max_output_tokens: int) -> ProviderResult:
        response = self.session.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "temperature": 0.2,
                "max_completion_tokens": max_output_tokens,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a programming teacher reviewing a student's coursework repository. Be strict but pedagogical, calibrate feedback to a beginner student level rather than production standards, and return valid JSON only. All user-facing strings must be in Russian. Do not output chain-of-thought, planning, or task interpretation.",
                    },
                    {"role": "user", "content": prompt},
                ],
            },
                timeout=self.timeout_seconds,
        )
        if response.status_code in {429, 498, 500, 502, 503, 504}:
            raise ProviderError(f"Groq temporarily unavailable: {response.status_code}")
        if response.status_code >= 400:
            raise ProviderError(f"Groq request failed: {response.status_code}", temporary=False)
        data = response.json()
        try:
            text = extract_text_content(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Groq returned unexpected response: {exc}") from exc

        usage_data = data.get("usage") or {}
        usage = ProviderUsage(
            prompt_tokens=safe_int(usage_data.get("prompt_tokens"), estimate_text_tokens(prompt)),
            completion_tokens=safe_int(usage_data.get("completion_tokens"), estimate_text_tokens(text)),
            cached_tokens=safe_int((usage_data.get("prompt_tokens_details") or {}).get("cached_tokens"), 0),
        )
        usage.total_tokens = safe_int(usage_data.get("total_tokens"), usage.prompt_tokens + usage.completion_tokens)
        usage.estimated_cost_usd = (
            usage.prompt_tokens * self.input_per_million / 1_000_000
            + usage.completion_tokens * self.output_per_million / 1_000_000
        )
        try:
            payload = parse_llm_json(text)
        except ProviderError as exc:
            raise ProviderError(str(exc), temporary=False, usage=usage) from exc
        return ProviderResult(payload=payload, usage=usage, provider_key=self.label(), raw_text=text, raw_response=data)


class GigaChatProvider(BaseProvider):
    provider_name = "gigachat"

    def __init__(self) -> None:
        super().__init__()
        self.auth_key = os.getenv("GIGACHAT_AUTH_KEY", "").strip()
        self.scope = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()
        self.model = os.getenv("GIGACHAT_MODEL", "GigaChat-2-Lite").strip()
        self.verify_ssl = _resolve_verify_value(os.getenv("GIGACHAT_VERIFY_SSL", "true").strip())

    def is_configured(self) -> bool:
        return bool(self.auth_key)

    def label(self) -> str:
        return f"{self.provider_name}:{self.model}"

    def _get_access_token(self) -> str:
        response = self.session.post(
            "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid.uuid4()),
                "Authorization": f"Basic {self.auth_key}",
            },
            data={"scope": self.scope},
            timeout=60,
            verify=self.verify_ssl,
        )
        if response.status_code in {429, 500, 502, 503, 504}:
            raise ProviderError(f"GigaChat temporarily unavailable: {response.status_code}")
        if response.status_code >= 400:
            raise ProviderError(f"GigaChat auth failed: {response.status_code}", temporary=False)
        data = response.json()
        token = data.get("access_token")
        if not token:
            raise ProviderError("GigaChat auth response missing access_token", temporary=False)
        return token

    def generate(self, prompt: str, max_output_tokens: int) -> ProviderResult:
        access_token = self._get_access_token()
        response = self.session.post(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={
                "model": self.model,
                "temperature": 0.2,
                "max_tokens": max_output_tokens,
                "messages": [
                    {
                        "role": "system",
                        "content": "Ты преподаватель программирования и проверяешь студенческую учебную работу по репозиторию. Давай строгую, но педагогичную обратную связь с поправкой на уровень новичка. Верни только JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=90,
            verify=self.verify_ssl,
        )
        if response.status_code in {429, 500, 502, 503, 504}:
            raise ProviderError(f"GigaChat temporarily unavailable: {response.status_code}")
        if response.status_code >= 400:
            raise ProviderError(f"GigaChat request failed: {response.status_code}", temporary=False)
        data = response.json()
        try:
            text = extract_text_content(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"GigaChat returned unexpected response: {exc}") from exc
        usage_data = data.get("usage") or {}
        usage = ProviderUsage(
            prompt_tokens=safe_int(usage_data.get("prompt_tokens"), estimate_text_tokens(prompt)),
            completion_tokens=safe_int(usage_data.get("completion_tokens"), estimate_text_tokens(text)),
            cached_tokens=0,
        )
        usage.total_tokens = safe_int(usage_data.get("total_tokens"), usage.prompt_tokens + usage.completion_tokens)
        usage.estimated_cost_usd = 0.0
        try:
            payload = parse_llm_json(text)
        except ProviderError as exc:
            raise ProviderError(str(exc), temporary=False, usage=usage) from exc
        return ProviderResult(payload=payload, usage=usage, provider_key=self.label(), raw_text=text, raw_response=data)


def _resolve_verify_value(raw: str) -> bool | str:
    lowered = raw.lower()
    if lowered in {"false", "0", "no"}:
        return False
    if lowered in {"true", "1", "yes", ""}:
        return True
    return raw


class BaseSmartReviewer:
    def __init__(self) -> None:
        self.budget = BudgetManager()
        self.debug = DebugLogger()
        order = [
            part.strip().lower()
            for part in os.getenv("AI_PROVIDER_ORDER", "gemini,openrouter,groq,gigachat").split(",")
            if part.strip()
        ]
        factories = {
            "gemini": GeminiProvider,
            "openrouter": OpenRouterProvider,
            "groq": GroqProvider,
            "gigachat": GigaChatProvider,
        }
        self.providers = [factories[name]() for name in order if name in factories]
        self.cooldown_seconds = int(os.getenv("AI_PROVIDER_COOLDOWN_SECONDS", "300"))
        self.unavailable_until: dict[str, float] = {}

    def review(self, snapshot: "RepoSnapshot", heuristic: "ReviewResult") -> "ReviewResult":
        configured = [provider for provider in self.providers if provider.is_configured()]
        reviewed_files = [file.path for file in select_reviewed_files(snapshot)]
        if not configured:
            heuristic.review_source = "Эвристика"
            heuristic.provider_attempts = ["AI-ключи не настроены"]
            heuristic.reviewed_files = reviewed_files
            return heuristic

        policy = self.budget.get_policy()
        prompt = build_llm_prompt_v2(snapshot, policy["max_prompt_chars"])
        estimated_tokens = estimate_text_tokens(prompt)

        try:
            self.budget.assert_can_spend(estimated_tokens)
        except BudgetExceededError:
            heuristic.review_source = "Эвристика"
            heuristic.provider_attempts = [self.budget.format_status()]
            heuristic.reviewed_files = reviewed_files
            heuristic.ai_rationale = [
                *heuristic.ai_rationale[:2],
                "AI-проверка остановлена локальным бюджетным лимитом, чтобы не сжигать токены дальше.",
            ]
            return heuristic

        attempts: list[str] = []
        now = time.time()

        for provider in configured:
            provider_key = provider.label()
            if self.unavailable_until.get(provider_key, 0) > now:
                attempts.append(f"{provider_key} (cooldown)")
                continue

            try:
                result = provider.generate(prompt, policy["max_output_tokens"])
                self.budget.record_usage(provider_key, result.usage)
                debug_path = self.debug.write(
                    provider_key,
                    prompt,
                    status="success",
                    response_json=result.raw_response,
                    response_text=result.raw_text,
                    parsed_payload=result.payload,
                    usage=result.usage,
                )
                merged = build_ai_review_result_v2(
                    result.payload,
                    result.provider_key,
                    [*attempts, result.provider_key],
                    reviewed_files,
                )
                if not merged.reviewed_files:
                    merged.reviewed_files = reviewed_files
                merged = post_filter_ai_review(merged, snapshot)
                merged.provider_attempts.extend([f"debug: {debug_path}", self.budget.format_status()])
                return merged
            except ProviderError as exc:
                if exc.usage and exc.usage.total_tokens > 0:
                    self.budget.record_usage(provider_key, exc.usage)
                debug_path = self.debug.write(
                    provider_key,
                    prompt,
                    status="error",
                    error=str(exc),
                    usage=exc.usage,
                )
                attempts.append(f"{provider_key} ({exc})")
                attempts.append(f"debug: {debug_path}")
                if exc.temporary:
                    self.unavailable_until[provider_key] = time.time() + self.cooldown_seconds
            except requests.RequestException as exc:
                debug_path = self.debug.write(
                    provider_key,
                    prompt,
                    status="request_error",
                    error=f"{exc.__class__.__name__}: {exc}",
                )
                attempts.append(f"{provider_key} ({exc.__class__.__name__})")
                attempts.append(f"debug: {debug_path}")
                self.unavailable_until[provider_key] = time.time() + self.cooldown_seconds
            except Exception as exc:  # noqa: BLE001
                debug_path = self.debug.write(
                    provider_key,
                    prompt,
                    status="unexpected_error",
                    error=f"{exc.__class__.__name__}: {exc}",
                )
                attempts.append(f"{provider_key} ({exc.__class__.__name__})")
                attempts.append(f"debug: {debug_path}")
                self.unavailable_until[provider_key] = time.time() + self.cooldown_seconds

        heuristic.review_source = "Эвристика"
        heuristic.reviewed_files = reviewed_files
        heuristic.provider_attempts = [*attempts, self.budget.format_status()]
        heuristic.ai_rationale = [
            *heuristic.ai_rationale[:2],
            "Все AI-провайдеры были недоступны или остановлены бюджетной защитой, поэтому сработал локальный резервный анализ.",
        ]
        return heuristic


class LegacySmartReviewer(BaseSmartReviewer):
    def review(
        self,
        snapshot: "RepoSnapshot",
        heuristic: "ReviewResult",
        progress_callback: Callable[[str], None] | None = None,
    ) -> "ReviewResult":
        configured = [provider for provider in self.providers if provider.is_configured()]
        reviewed_files = [file.path for file in select_reviewed_files(snapshot)]
        if not configured:
            if progress_callback:
                progress_callback("AI не настроен, поэтому проверяю проект локально.")
            heuristic.review_source = "Эвристика"
            heuristic.provider_attempts = ["AI-ключи не настроены"]
            heuristic.reviewed_files = reviewed_files
            return heuristic

        policy = self.budget.get_policy()
        prompt = build_llm_prompt_v2(snapshot, policy["max_prompt_chars"])
        estimated_tokens = estimate_text_tokens(prompt)

        try:
            self.budget.assert_can_spend(estimated_tokens)
        except BudgetExceededError:
            if progress_callback:
                progress_callback("AI остановлен бюджетной защитой, продолжаю локальную проверку.")
            heuristic.review_source = "Эвристика"
            heuristic.provider_attempts = [self.budget.format_status()]
            heuristic.reviewed_files = reviewed_files
            heuristic.ai_rationale = [
                *heuristic.ai_rationale[:2],
                "AI-проверка остановлена локальным бюджетным лимитом, чтобы не сжигать токены дальше.",
            ]
            return heuristic

        attempts: list[str] = []
        now = time.time()

        for provider in configured:
            provider_key = provider.label()
            if self.unavailable_until.get(provider_key, 0) > now:
                attempts.append(f"{provider_key} (cooldown)")
                continue

            try:
                if progress_callback:
                    progress_callback(f"Жду ответа от {provider_key}...")
                result = provider.generate(prompt, policy["max_output_tokens"])
                self.budget.record_usage(provider_key, result.usage)
                debug_path = self.debug.write(
                    provider_key,
                    prompt,
                    status="success",
                    response_json=result.raw_response,
                    response_text=result.raw_text,
                    parsed_payload=result.payload,
                    usage=result.usage,
                )
                merged = build_ai_review_result_v2(
                    result.payload,
                    result.provider_key,
                    [*attempts, result.provider_key],
                    reviewed_files,
                )
                if not merged.reviewed_files:
                    merged.reviewed_files = reviewed_files
                merged.provider_attempts.extend([f"debug: {debug_path}", self.budget.format_status()])
                if progress_callback:
                    progress_callback(f"Ответ получен от {result.provider_key}. Собираю итоговый отчет.")
                return merged
            except ProviderError as exc:
                if exc.usage and exc.usage.total_tokens > 0:
                    self.budget.record_usage(provider_key, exc.usage)
                debug_path = self.debug.write(
                    provider_key,
                    prompt,
                    status="error",
                    error=str(exc),
                    usage=exc.usage,
                )
                attempts.append(f"{provider_key} ({exc})")
                attempts.append(f"debug: {debug_path}")
                if exc.temporary:
                    self.unavailable_until[provider_key] = time.time() + self.cooldown_seconds
            except requests.RequestException as exc:
                debug_path = self.debug.write(
                    provider_key,
                    prompt,
                    status="request_error",
                    error=f"{exc.__class__.__name__}: {exc}",
                )
                attempts.append(f"{provider_key} ({exc.__class__.__name__})")
                attempts.append(f"debug: {debug_path}")
                self.unavailable_until[provider_key] = time.time() + self.cooldown_seconds
            except Exception as exc:  # noqa: BLE001
                debug_path = self.debug.write(
                    provider_key,
                    prompt,
                    status="unexpected_error",
                    error=f"{exc.__class__.__name__}: {exc}",
                )
                attempts.append(f"{provider_key} ({exc.__class__.__name__})")
                attempts.append(f"debug: {debug_path}")
                self.unavailable_until[provider_key] = time.time() + self.cooldown_seconds

        heuristic.review_source = "Эвристика"
        heuristic.reviewed_files = reviewed_files
        heuristic.provider_attempts = [*attempts, self.budget.format_status()]
        heuristic.ai_rationale = [
            *heuristic.ai_rationale[:2],
            "Все AI-провайдеры были недоступны или остановлены бюджетной защитой, поэтому сработал локальный резервный анализ.",
        ]
        if progress_callback:
            progress_callback("AI недоступен или не смог вернуть валидный ответ. Проверяю проект локально.")
        return heuristic
class SmartReviewer(BaseSmartReviewer):
    def review(
        self,
        snapshot: "RepoSnapshot",
        heuristic: "ReviewResult",
        progress_callback: Callable[[str], None] | None = None,
    ) -> "ReviewResult":
        configured = [provider for provider in self.providers if provider.is_configured()]
        reviewed_files = [file.path for file in select_reviewed_files(snapshot)]
        if not configured:
            if progress_callback:
                progress_callback("AI не настроен, поэтому проверяю проект локально.")
            heuristic.review_source = "Эвристика"
            heuristic.provider_attempts = ["AI-ключи не настроены"]
            heuristic.reviewed_files = reviewed_files
            return heuristic

        policy = self.budget.get_policy()
        prompt = build_llm_prompt_v2(snapshot, policy["max_prompt_chars"])
        estimated_tokens = estimate_text_tokens(prompt)

        try:
            self.budget.assert_can_spend(estimated_tokens)
        except BudgetExceededError:
            if progress_callback:
                progress_callback("AI остановлен бюджетной защитой, продолжаю локальную проверку.")
            heuristic.review_source = "Эвристика"
            heuristic.provider_attempts = [self.budget.format_status()]
            heuristic.reviewed_files = reviewed_files
            heuristic.ai_rationale = [
                *heuristic.ai_rationale[:2],
                "AI-проверка остановлена локальным бюджетным лимитом, чтобы не сжигать токены дальше.",
            ]
            return heuristic

        attempts: list[str] = []
        for provider in configured:
            provider_key = provider.label()
            if self.unavailable_until.get(provider_key, 0) > time.time():
                attempts.append(f"{provider_key} (cooldown)")
                continue

            max_attempts = int(os.getenv("OPENROUTER_MAX_ATTEMPTS", "3")) if provider_key.startswith("openrouter:") else 1
            for attempt_no in range(1, max_attempts + 1):
                current_key = provider_key if attempt_no == 1 else f"{provider_key} retry {attempt_no}"
                attempt_output_tokens = output_tokens_for_attempt(provider_key, policy, attempt_no)
                try:
                    if progress_callback:
                        progress_callback(f"Жду ответа от {provider_key}...")
                    result = provider.generate(prompt, attempt_output_tokens)
                    self.budget.record_usage(provider_key, result.usage)
                    usable_reasoning = "\n\n".join(
                        filter_bad_sentences(_split_review_sentences(extract_reasoning_text(result.raw_response)))
                    )
                    enriched_text = "\n\n".join(
                        part
                        for part in (
                            result.raw_text,
                            usable_reasoning,
                        )
                        if part
                    ).strip()
                    enriched_payload = parse_review_text_fallback(enriched_text, result.payload)
                    try:
                        merged = build_ai_review_result_v2(
                            enriched_payload,
                            result.provider_key,
                            [*attempts, current_key],
                            reviewed_files,
                        )
                    except ProviderError as exc:
                        debug_path = self.debug.write(
                            provider_key,
                            prompt,
                            status="error",
                            response_json=result.raw_response,
                            response_text=result.raw_text,
                            parsed_payload=enriched_payload,
                            error=str(exc),
                            usage=result.usage,
                        )
                        attempts.append(f"{current_key} ({exc})")
                        attempts.append(f"debug: {debug_path}")
                        if is_retryable_provider_error(provider_key, exc) and attempt_no < max_attempts:
                            if progress_callback:
                                progress_callback(f"{provider_key} вернул неполный ответ, пробую еще раз...")
                            continue
                        break

                    debug_path = self.debug.write(
                        provider_key,
                        prompt,
                        status="success",
                        response_json=result.raw_response,
                        response_text=result.raw_text,
                        parsed_payload=enriched_payload,
                        usage=result.usage,
                    )
                    if not merged.reviewed_files:
                        merged.reviewed_files = reviewed_files
                    merged.provider_attempts.extend([f"debug: {debug_path}", self.budget.format_status()])
                    if progress_callback:
                        progress_callback(f"Ответ получен от {result.provider_key}. Собираю итоговый отчет.")
                    return merged
                except ProviderError as exc:
                    if exc.usage and exc.usage.total_tokens > 0:
                        self.budget.record_usage(provider_key, exc.usage)
                    debug_path = self.debug.write(
                        provider_key,
                        prompt,
                        status="error",
                        error=str(exc),
                        usage=exc.usage,
                    )
                    attempts.append(f"{current_key} ({exc})")
                    attempts.append(f"debug: {debug_path}")
                    if is_retryable_provider_error(provider_key, exc) and attempt_no < max_attempts:
                        if progress_callback:
                            progress_callback(f"{provider_key} ответил неудачно, повторяю запрос...")
                        continue
                    if exc.temporary:
                        self.unavailable_until[provider_key] = time.time() + self.cooldown_seconds
                    break
                except requests.RequestException as exc:
                    debug_path = self.debug.write(
                        provider_key,
                        prompt,
                        status="request_error",
                        error=f"{exc.__class__.__name__}: {exc}",
                    )
                    attempts.append(f"{current_key} ({exc.__class__.__name__})")
                    attempts.append(f"debug: {debug_path}")
                    self.unavailable_until[provider_key] = time.time() + self.cooldown_seconds
                    break
                except Exception as exc:  # noqa: BLE001
                    debug_path = self.debug.write(
                        provider_key,
                        prompt,
                        status="unexpected_error",
                        error=f"{exc.__class__.__name__}: {exc}",
                    )
                    attempts.append(f"{current_key} ({exc.__class__.__name__})")
                    attempts.append(f"debug: {debug_path}")
                    break

        heuristic.review_source = "Эвристика"
        heuristic.reviewed_files = reviewed_files
        heuristic.provider_attempts = [*attempts, self.budget.format_status()]
        heuristic.ai_rationale = [
            *heuristic.ai_rationale[:2],
            "Все AI-провайдеры были недоступны, вернули неполный ответ или были остановлены бюджетной защитой, поэтому сработал локальный резервный анализ.",
        ]
        if progress_callback:
            progress_callback("AI недоступен или не смог вернуть валидный ответ. Проверяю проект локально.")
        return heuristic
