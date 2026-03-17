from __future__ import annotations

import atexit
import html
import io
import json
import logging
import math
import os
import re
import time
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import requests
from requests import HTTPError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_INSTANCE_LOCK_FD: int | None = None
_INSTANCE_LOCK_PATH: Path | None = None

GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
GITHUB_URL_SEARCH_RE = re.compile(
    r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?"
)
TEXT_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".kt",
    ".go",
    ".rb",
    ".php",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".swift",
    ".rs",
    ".sql",
    ".html",
    ".css",
    ".scss",
    ".md",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".env.example",
    ".sh",
}
SKIP_DIR_MARKERS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    ".next",
    ".nuxt",
    "coverage",
    ".idea",
    ".vscode",
    "__pycache__",
    "vendor",
    "target",
}
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


@dataclass(slots=True)
class Settings:
    telegram_bot_token: str
    github_token: str | None = None
    max_files_to_review: int = 40
    max_file_size_kb: int = 180
    provider_order: tuple[str, ...] = ("gemini", "openrouter", "groq")
    provider_cooldown_seconds: int = 300
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    openrouter_api_key: str | None = None
    openrouter_model: str = "openrouter/free"
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-20b"


@dataclass(slots=True)
class RepoFile:
    path: str
    content: str
    size_bytes: int


@dataclass(slots=True)
class RepoSnapshot:
    owner: str
    name: str
    default_branch: str
    description: str
    stars: int
    language: str | None
    files: list[RepoFile] = field(default_factory=list)
    skipped_files: int = 0
    assignment_text: str = ""
    assignment_filename: str = ""


@dataclass(slots=True)
class Finding:
    title: str
    detail: str
    severity: str


@dataclass(slots=True)
class ReviewResult:
    summary: str
    strengths: list[str]
    issues: list[Finding]
    recommendations: list[str]
    ai_probability_percent: int
    ai_rationale: list[str]
    overall_score_percent: int
    detailed_analysis: list[str] = field(default_factory=list)
    ai_detection_signals: list[str] = field(default_factory=list)
    reviewed_files: list[str] = field(default_factory=list)
    assignment_summary: str = ""
    assignment_findings: list[str] = field(default_factory=list)
    review_source: str = "Эвристика"
    provider_attempts: list[str] = field(default_factory=list)


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def release_instance_lock() -> None:
    global _INSTANCE_LOCK_FD, _INSTANCE_LOCK_PATH
    if _INSTANCE_LOCK_FD is not None:
        try:
            os.close(_INSTANCE_LOCK_FD)
        except OSError:
            pass
        _INSTANCE_LOCK_FD = None
    if _INSTANCE_LOCK_PATH is not None and _INSTANCE_LOCK_PATH.exists():
        try:
            _INSTANCE_LOCK_PATH.unlink()
        except OSError:
            pass
    _INSTANCE_LOCK_PATH = None


def acquire_instance_lock() -> bool:
    global _INSTANCE_LOCK_FD, _INSTANCE_LOCK_PATH
    lock_path = Path(".cache/bot_runtime.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            existing_pid = 0
        if existing_pid > 0 and _pid_is_running(existing_pid):
            logger.error("Another bot process is already running with PID %s", existing_pid)
            return False
        try:
            lock_path.unlink()
        except OSError:
            logger.error("Failed to clear stale bot lock at %s", lock_path)
            return False

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        logger.error("Another bot process is already running")
        return False

    os.write(fd, str(os.getpid()).encode("utf-8"))
    _INSTANCE_LOCK_FD = fd
    _INSTANCE_LOCK_PATH = lock_path
    atexit.register(release_instance_lock)
    return True


def run() -> None:
    from assignment_runtime import AssignmentStore
    from smart_review_runtime import SmartReviewer

    if not acquire_instance_lock():
        logger.error("Bot startup aborted because another instance is active")
        return

    settings = get_settings()
    github_client = GitHubClient(settings)
    analyzer = RepositoryAnalyzer()
    reviewer = SmartReviewer()
    assignment_store = AssignmentStore()
    telegram = TelegramBotAPI(settings.telegram_bot_token)
    telegram.delete_webhook(drop_pending_updates=False)
    logger.info("Bot started")

    offset: int | None = None
    while True:
        try:
            updates = telegram.get_updates(offset=offset, timeout=30)
            for update in updates:
                offset = update["update_id"] + 1
                handle_update(update, telegram, github_client, analyzer, reviewer, assignment_store)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 409:
                logger.warning("Telegram long polling conflict: another bot session or webhook is active")
                time.sleep(3)
                continue
            logger.exception("Telegram polling failed")
            time.sleep(5)
        except requests.RequestException:
            logger.exception("Telegram polling failed")
            time.sleep(5)
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected top-level error")
            time.sleep(3)


def legacy_handle_update(
    update: dict,
    telegram: "TelegramBotAPI",
    github_client: "GitHubClient",
    analyzer: "RepositoryAnalyzer",
    reviewer: MultiProviderReviewer,
) -> None:
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    message_id = message.get("message_id")

    if not chat_id or not text:
        return

    if text == "/budget":
        budget = getattr(reviewer, "budget", None)
        if budget is None:
            telegram.send_message(chat_id, "Бюджетный трекер не активен.", reply_to_message_id=message_id)
        else:
            telegram.send_message(
                chat_id,
                f"Статус AI-бюджета:\n{html.escape(budget.format_status())}",
                reply_to_message_id=message_id,
            )
        return

    if text == "/lastdebug":
        debug = getattr(reviewer, "debug", None)
        if debug is None:
            telegram.send_message(chat_id, "AI debug-логгер не активен.", reply_to_message_id=message_id)
        else:
            latest = debug.dir / "latest.json"
            if latest.exists():
                path = latest.read_text(encoding="utf-8").strip()
                telegram.send_message(
                    chat_id,
                    f"Последний debug-файл:\n<code>{html.escape(path)}</code>",
                    reply_to_message_id=message_id,
                )
            else:
                telegram.send_message(chat_id, "Пока нет сохраненных AI debug-запросов.", reply_to_message_id=message_id)
        return

    if text in {"/start", "/help"}:
        telegram.send_message(
            chat_id,
            (
                "Пришлите ссылку на публичный GitHub-репозиторий.\n\n"
                "Я:\n"
                "• скачаю репозиторий\n"
                "• дам обратную связь по качеству\n"
                "• отмечу сильные и слабые стороны\n"
                "• оценю вероятность использования AI в процентах\n\n"
                "Формат ссылки: <code>https://github.com/owner/repo</code>"
            ),
            reply_to_message_id=message_id,
        )
        return

    telegram.send_chat_action(chat_id, "typing")

    try:
        from report_runtime import render_report_v2

        snapshot = github_client.fetch_snapshot(text)
        heuristic = analyzer.analyze(snapshot)
        result = reviewer.review(snapshot, heuristic)
        report = render_report_v2(snapshot, result)
        if status_message_id:
            try:
                telegram.edit_message(
                    chat_id,
                    status_message_id,
                    report,
                    disable_web_page_preview=True,
                )
                return
            except Exception:  # noqa: BLE001
                logger.exception("Failed to replace status message with final report")
        telegram.send_message(
            chat_id,
            report,
            disable_web_page_preview=True,
            reply_to_message_id=message_id,
        )
    except ValueError as exc:
        telegram.send_message(chat_id, html.escape(str(exc)), reply_to_message_id=message_id)
    except HTTPError as exc:
        logger.exception("GitHub request failed")
        telegram.send_message(
            chat_id,
            f"Не получилось скачать репозиторий с GitHub: {exc.response.status_code}.",
            reply_to_message_id=message_id,
        )
    except requests.ConnectionError:
        logger.exception("GitHub connection failed")
        telegram.send_message(
            chat_id,
            (
                "Не получилось подключиться к GitHub. "
                "Похоже, сеть или прокси блокирует доступ к <code>api.github.com</code>."
            ),
            reply_to_message_id=message_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error")
        telegram.send_message(
            chat_id,
            f"Во время проверки произошла ошибка.\n<pre>{html.escape(str(exc))}</pre>",
            reply_to_message_id=message_id,
        )


def handle_update(
    update: dict,
    telegram: "TelegramBotAPI",
    github_client: "GitHubClient",
    analyzer: "RepositoryAnalyzer",
    reviewer: "SmartReviewer",
    assignment_store,
) -> None:
    from assignment_runtime import parse_assignment_document

    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    caption = (message.get("caption") or "").strip()
    document = message.get("document") or {}
    message_id = message.get("message_id")

    if not chat_id:
        return

    if text == "/budget":
        budget = getattr(reviewer, "budget", None)
        if budget is None:
            telegram.send_message(chat_id, "Бюджетный трекер не активен.", reply_to_message_id=message_id)
        else:
            telegram.send_message(
                chat_id,
                f"Статус AI-бюджета:\n{html.escape(budget.format_status())}",
                reply_to_message_id=message_id,
            )
        return

    if text == "/lastdebug":
        debug = getattr(reviewer, "debug", None)
        if debug is None:
            telegram.send_message(chat_id, "AI debug-логгер не активен.", reply_to_message_id=message_id)
        else:
            latest = debug.dir / "latest.json"
            if latest.exists():
                path = latest.read_text(encoding="utf-8").strip()
                telegram.send_message(
                    chat_id,
                    f"Последний debug-файл:\n<code>{html.escape(path)}</code>",
                    reply_to_message_id=message_id,
                )
            else:
                telegram.send_message(chat_id, "Пока нет сохраненных AI debug-запросов.", reply_to_message_id=message_id)
        return

    if text == "/clear_spec":
        cleared = assignment_store.clear(chat_id)
        telegram.send_message(
            chat_id,
            "Загруженное ТЗ удалено." if cleared else "Для этого чата пока нет сохраненного ТЗ.",
            reply_to_message_id=message_id,
        )
        return

    if text in {"/start", "/help"}:
        telegram.send_message(
            chat_id,
            (
                "Пришлите ссылку на публичный GitHub-репозиторий.\n\n"
                "Дополнительно можно заранее отправить ТЗ в формате PDF, DOCX, TXT или MD. "
                "Я сохраню его для этого чата и при следующей проверке сравню проект с требованиями.\n\n"
                "Команды:\n"
                "• <code>/budget</code> — посмотреть расход AI\n"
                "• <code>/lastdebug</code> — путь к последнему AI debug-файлу\n"
                "• <code>/clear_spec</code> — удалить сохраненное ТЗ\n\n"
                "Формат ссылки: <code>https://github.com/owner/repo</code>"
            ),
            reply_to_message_id=message_id,
        )
        return

    if document:
        telegram.send_chat_action(chat_id, "typing")
        try:
            file_path = telegram.get_file_path(document["file_id"])
            file_bytes = telegram.download_file(file_path)
            filename = document.get("file_name") or Path(file_path).name or "assignment"
            parsed_text = parse_assignment_document(filename, file_bytes)
            assignment_store.save(chat_id, filename, parsed_text)
            preview = html.escape(parsed_text[:350])
            telegram.send_message(
                chat_id,
                (
                    f"ТЗ сохранено: <code>{html.escape(filename)}</code>\n"
                    f"Извлечено символов: {len(parsed_text)}\n"
                    f"Короткий фрагмент:\n<pre>{preview}</pre>"
                ),
                reply_to_message_id=message_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Assignment upload failed")
            telegram.send_message(
                chat_id,
                f"Не получилось прочитать документ.\n<pre>{html.escape(str(exc))}</pre>",
                reply_to_message_id=message_id,
            )
            return

        if caption and GITHUB_URL_SEARCH_RE.search(caption):
            process_repository_review(
                repo_url=caption,
                chat_id=chat_id,
                message_id=message_id,
                telegram=telegram,
                github_client=github_client,
                analyzer=analyzer,
                reviewer=reviewer,
                assignment_store=assignment_store,
            )
        return

    if not text:
        return

    telegram.send_chat_action(chat_id, "typing")
    process_repository_review(
        repo_url=text,
        chat_id=chat_id,
        message_id=message_id,
        telegram=telegram,
        github_client=github_client,
        analyzer=analyzer,
        reviewer=reviewer,
        assignment_store=assignment_store,
    )


def process_repository_review(
    *,
    repo_url: str,
    chat_id: int,
    message_id: int | None,
    telegram: "TelegramBotAPI",
    github_client: "GitHubClient",
    analyzer: "RepositoryAnalyzer",
    reviewer: "SmartReviewer",
    assignment_store,
) -> None:
    status_message_id: int | None = None

    def publish_status(status_text: str) -> None:
        nonlocal status_message_id
        if not status_message_id:
            status_message_id = telegram.send_message(chat_id, status_text, reply_to_message_id=message_id)
            return
        try:
            telegram.edit_message(chat_id, status_message_id, status_text)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to update status message")

    try:
        from report_runtime import render_report_v2

        status_message_id = telegram.send_message(
            chat_id,
            "Ссылка принята. Готовлю проект к проверке...",
            reply_to_message_id=message_id,
        )

        snapshot = github_client.fetch_snapshot(extract_repo_url(repo_url))
        publish_status("Ссылка принята. Репозиторий скачан, собираю контекст для проверки...")
        assignment_document = assignment_store.load(chat_id)
        if assignment_document:
            snapshot.assignment_text = assignment_document.text
            snapshot.assignment_filename = assignment_document.filename
            publish_status(f"ТЗ {assignment_document.filename} найдено. Сравниваю требования с проектом...")
        heuristic = analyzer.analyze(snapshot)
        result = reviewer.review(snapshot, heuristic, progress_callback=publish_status)
        publish_status("Проверка завершена. Отправляю итоговый отчет...")
        report = render_report_v2(snapshot, result)
        telegram.send_message(
            chat_id,
            report,
            disable_web_page_preview=True,
            reply_to_message_id=message_id,
        )
    except ValueError as exc:
        publish_status("Не удалось запустить проверку: ссылка или данные проекта не подошли.")
        telegram.send_message(chat_id, html.escape(str(exc)), reply_to_message_id=message_id)
    except HTTPError as exc:
        publish_status("GitHub вернул ошибку при загрузке репозитория.")
        logger.exception("GitHub request failed")
        telegram.send_message(
            chat_id,
            f"Не получилось скачать репозиторий с GitHub: {exc.response.status_code}.",
            reply_to_message_id=message_id,
        )
    except requests.ConnectionError:
        publish_status("Не получилось подключиться к GitHub.")
        logger.exception("GitHub connection failed")
        telegram.send_message(
            chat_id,
            (
                "Не получилось подключиться к GitHub. "
                "Похоже, сеть или прокси блокирует доступ к <code>api.github.com</code>."
            ),
            reply_to_message_id=message_id,
        )
    except Exception as exc:  # noqa: BLE001
        publish_status("Во время проверки произошла ошибка.")
        logger.exception("Unexpected error")
        telegram.send_message(
            chat_id,
            f"Во время проверки произошла ошибка.\n<pre>{html.escape(str(exc))}</pre>",
            reply_to_message_id=message_id,
        )


def extract_repo_url(text: str) -> str:
    candidate = text.strip()
    if GITHUB_URL_RE.match(candidate):
        return candidate
    match = GITHUB_URL_SEARCH_RE.search(candidate)
    if match:
        return match.group(0)
    return candidate


def get_settings() -> Settings:
    load_dotenv_file(".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    provider_order = tuple(
        part.strip().lower()
        for part in os.getenv("AI_PROVIDER_ORDER", "gemini,openrouter,groq").split(",")
        if part.strip()
    )

    return Settings(
        telegram_bot_token=token,
        github_token=os.getenv("GITHUB_TOKEN", "").strip() or None,
        provider_order=provider_order or ("gemini", "openrouter", "groq"),
        provider_cooldown_seconds=int(os.getenv("AI_PROVIDER_COOLDOWN_SECONDS", "300")),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip() or None,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip(),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip() or None,
        openrouter_model=os.getenv("OPENROUTER_MODEL", "openrouter/free").strip(),
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip() or None,
        groq_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip(),
    )


def load_dotenv_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as dotenv_file:
        for raw_line in dotenv_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


class LLMProviderError(RuntimeError):
    def __init__(self, message: str, *, temporary: bool = True) -> None:
        super().__init__(message)
        self.temporary = temporary


class BaseLLMProvider:
    provider_name = "base"

    def __init__(self, api_key: str | None, model: str) -> None:
        self.api_key = api_key.strip() if api_key else None
        self.model = model.strip()
        self.session = requests.Session()

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def label(self) -> str:
        return f"{self.provider_name}:{self.model}"

    def generate_json_review(self, prompt: str) -> dict:
        raise NotImplementedError


class GeminiProvider(BaseLLMProvider):
    provider_name = "gemini"

    def generate_json_review(self, prompt: str) -> dict:
        response = self.session.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            params={"key": self.api_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "responseMimeType": "application/json",
                },
            },
            timeout=90,
        )
        if response.status_code in {429, 500, 502, 503, 504}:
            raise LLMProviderError(f"Gemini temporarily unavailable: {response.status_code}")
        if response.status_code >= 400:
            raise LLMProviderError(f"Gemini request failed: {response.status_code}", temporary=False)
        data = response.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError(f"Gemini returned unexpected response: {exc}") from exc
        return parse_llm_json(text)


class OpenRouterProvider(BaseLLMProvider):
    provider_name = "openrouter"

    def generate_json_review(self, prompt: str) -> dict:
        response = self.session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com",
                "X-Title": "student-repo-checker-bot",
            },
            json={
                "model": self.model,
                "temperature": 0.2,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a strict but fair code reviewer for beginner student repositories. Return valid JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=90,
        )
        if response.status_code in {402, 429, 500, 502, 503, 504}:
            raise LLMProviderError(f"OpenRouter temporarily unavailable: {response.status_code}")
        if response.status_code >= 400:
            raise LLMProviderError(f"OpenRouter request failed: {response.status_code}", temporary=False)
        data = response.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError(f"OpenRouter returned unexpected response: {exc}") from exc
        return parse_llm_json(text)


class GroqProvider(BaseLLMProvider):
    provider_name = "groq"

    def generate_json_review(self, prompt: str) -> dict:
        response = self.session.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "temperature": 0.2,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a strict but fair code reviewer for beginner student repositories. Return valid JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=90,
        )
        if response.status_code in {429, 500, 502, 503, 504}:
            raise LLMProviderError(f"Groq temporarily unavailable: {response.status_code}")
        if response.status_code >= 400:
            raise LLMProviderError(f"Groq request failed: {response.status_code}", temporary=False)
        data = response.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError(f"Groq returned unexpected response: {exc}") from exc
        return parse_llm_json(text)


class MultiProviderReviewer:
    def __init__(self, settings: Settings) -> None:
        all_providers = {
            "gemini": GeminiProvider(settings.gemini_api_key, settings.gemini_model),
            "openrouter": OpenRouterProvider(settings.openrouter_api_key, settings.openrouter_model),
            "groq": GroqProvider(settings.groq_api_key, settings.groq_model),
        }
        self.providers = [all_providers[name] for name in settings.provider_order if name in all_providers]
        self.cooldown_seconds = settings.provider_cooldown_seconds
        self.unavailable_until: dict[str, float] = {}

    def review(self, snapshot: RepoSnapshot, heuristic: ReviewResult) -> ReviewResult:
        configured = [provider for provider in self.providers if provider.is_configured()]
        if not configured:
            heuristic.review_source = "Эвристика"
            return heuristic

        prompt = build_llm_prompt(snapshot, heuristic)
        attempts: list[str] = []
        now = time.time()

        for provider in configured:
            provider_key = provider.label()
            if self.unavailable_until.get(provider_key, 0) > now:
                attempts.append(f"{provider_key} (cooldown)")
                continue

            try:
                payload = provider.generate_json_review(prompt)
                attempts.append(provider_key)
                return merge_llm_with_heuristic(payload, heuristic, provider_key, attempts)
            except LLMProviderError as exc:
                attempts.append(f"{provider_key} ({exc})")
                logger.warning("LLM provider failed: %s", attempts[-1])
                if exc.temporary:
                    self.unavailable_until[provider_key] = time.time() + self.cooldown_seconds
            except requests.RequestException as exc:
                attempts.append(f"{provider_key} ({exc.__class__.__name__})")
                logger.warning("LLM provider request failed: %s", attempts[-1])
                self.unavailable_until[provider_key] = time.time() + self.cooldown_seconds
            except Exception as exc:  # noqa: BLE001
                attempts.append(f"{provider_key} ({exc.__class__.__name__})")
                logger.warning("LLM provider parse failed: %s", attempts[-1])
                self.unavailable_until[provider_key] = time.time() + self.cooldown_seconds

        heuristic.review_source = "Эвристика"
        heuristic.provider_attempts = attempts
        if attempts:
            heuristic.ai_rationale = [
                *heuristic.ai_rationale[:2],
                "Все подключенные AI-провайдеры были недоступны, поэтому сработал локальный резервный анализ.",
            ]
        return heuristic


def parse_llm_json(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.S)
        if not match:
            raise LLMProviderError("Model did not return JSON")
        return json.loads(match.group(0))


def sanitize_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def sanitize_string_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [item for item in (sanitize_text(part) for part in raw) if item]


def clamp_score(value: object, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(5, min(95, number))


def trim_text(text: str, limit: int) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 20] + "\n... [truncated]"


def build_llm_prompt(snapshot: RepoSnapshot, heuristic: ReviewResult) -> str:
    readme = next((file for file in snapshot.files if file.path.split("/")[-1].lower() in README_NAMES), None)
    file_list = "\n".join(f"- {file.path}" for file in snapshot.files[:40])
    source_files = [
        file
        for file in snapshot.files
        if any(file.path.endswith(ext) for ext in (".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".php", ".rb", ".cs", ".kt", ".rs"))
    ]
    selected_files = sorted(source_files, key=lambda item: len(item.content), reverse=True)[:8]
    code_blocks = []
    for file in selected_files:
        code_blocks.append(f"FILE: {file.path}\n```text\n{trim_text(file.content, 1600)}\n```")

    issues_digest = "\n".join(f"- [{issue.severity}] {issue.title}: {issue.detail}" for issue in heuristic.issues[:6])
    strengths_digest = "\n".join(f"- {item}" for item in heuristic.strengths[:4])

    return (
        "Ты проверяешь репозиторий студента-новичка. Будь доброжелательным, но строгим. "
        "Если проект выглядит слишком ровным, взрослым по структуре и при этом без тестов, "
        "вероятность использования AI должна расти. Маленькие, сырые и неровные учебные проекты обычно понижают эту вероятность.\n\n"
        "Верни только JSON без Markdown по схеме:\n"
        "{"
        '"summary":"string",'
        '"strengths":["string"],'
        '"issues":[{"title":"string","detail":"string","severity":"low|medium|high"}],'
        '"recommendations":["string"],'
        '"ai_probability_percent":0,'
        '"ai_rationale":["string"],'
        '"overall_score_percent":0'
        "}\n\n"
        f"REPO: {snapshot.owner}/{snapshot.name}\n"
        f"DESCRIPTION: {snapshot.description or 'нет описания'}\n"
        f"DEFAULT_BRANCH: {snapshot.default_branch}\n"
        f"FILES_ANALYZED: {len(snapshot.files)}\n"
        f"FILES_SKIPPED: {snapshot.skipped_files}\n\n"
        f"FILES:\n{file_list}\n\n"
        f"README:\n{trim_text(readme.content if readme else 'README отсутствует', 2200)}\n\n"
        f"HEURISTIC_SUMMARY: {heuristic.summary}\n"
        f"HEURISTIC_SCORE: {heuristic.overall_score_percent}\n"
        f"HEURISTIC_AI_PERCENT: {heuristic.ai_probability_percent}\n"
        f"HEURISTIC_STRENGTHS:\n{strengths_digest or '- нет'}\n"
        f"HEURISTIC_ISSUES:\n{issues_digest or '- нет'}\n\n"
        f"CODE_SAMPLES:\n{'\n\n'.join(code_blocks)}"
    )


def sanitize_findings(raw: object) -> list[Finding]:
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


def merge_llm_with_heuristic(
    payload: dict,
    heuristic: ReviewResult,
    provider_key: str,
    attempts: list[str],
) -> ReviewResult:
    strengths = sanitize_string_list(payload.get("strengths")) or heuristic.strengths
    recommendations = sanitize_string_list(payload.get("recommendations")) or heuristic.recommendations
    ai_rationale = sanitize_string_list(payload.get("ai_rationale")) or heuristic.ai_rationale
    issues = sanitize_findings(payload.get("issues")) or heuristic.issues
    summary = sanitize_text(payload.get("summary")) or heuristic.summary

    llm_ai = clamp_score(payload.get("ai_probability_percent"), heuristic.ai_probability_percent)
    llm_score = clamp_score(payload.get("overall_score_percent"), heuristic.overall_score_percent)
    blended_ai = clamp_score(round(llm_ai * 0.7 + heuristic.ai_probability_percent * 0.3), heuristic.ai_probability_percent)
    blended_score = clamp_score(round(llm_score * 0.6 + heuristic.overall_score_percent * 0.4), heuristic.overall_score_percent)

    return ReviewResult(
        summary=summary,
        strengths=strengths[:4],
        issues=issues[:5],
        recommendations=recommendations[:4],
        ai_probability_percent=blended_ai,
        ai_rationale=ai_rationale[:3],
        overall_score_percent=blended_score,
        review_source=f"AI: {provider_key}",
        provider_attempts=attempts,
    )

class TelegramBotAPI:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "student-repo-checker-bot"

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        payload = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        response = self.session.get(f"{self.base_url}/getUpdates", params=payload, timeout=timeout + 5)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        return data.get("result", [])

    def delete_webhook(self, drop_pending_updates: bool = False) -> None:
        response = self.session.post(
            f"{self.base_url}/deleteWebhook",
            json={"drop_pending_updates": drop_pending_updates},
            timeout=20,
        )
        response.raise_for_status()

    def send_chat_action(self, chat_id: int, action: str) -> None:
        response = self.session.post(
            f"{self.base_url}/sendChatAction",
            json={"chat_id": chat_id, "action": action},
            timeout=15,
        )
        response.raise_for_status()

    def send_message(
        self,
        chat_id: int,
        text: str,
        disable_web_page_preview: bool = False,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = "HTML",
    ) -> int | None:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id

        response = self.session.post(
            f"{self.base_url}/sendMessage",
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram sendMessage error: {data}")
        result = data.get("result") or {}
        return result.get("message_id")

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        disable_web_page_preview: bool = False,
        parse_mode: str | None = "HTML",
    ) -> None:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        response = self.session.post(f"{self.base_url}/editMessageText", json=payload, timeout=20)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram editMessageText error: {data}")

    def get_file_path(self, file_id: str) -> str:
        response = self.session.get(f"{self.base_url}/getFile", params={"file_id": file_id}, timeout=20)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok") or "result" not in data or "file_path" not in data["result"]:
            raise RuntimeError(f"Telegram getFile error: {data}")
        return data["result"]["file_path"]

    def download_file(self, file_path: str) -> bytes:
        token = self.base_url.removeprefix("https://api.telegram.org/bot")
        response = self.session.get(f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=60)
        response.raise_for_status()
        return response.content


class GitHubClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "User-Agent": "student-repo-checker-bot",
            }
        )
        if settings.github_token:
            self.session.headers["Authorization"] = f"Bearer {settings.github_token}"

    def fetch_snapshot(self, repo_url: str) -> RepoSnapshot:
        match = GITHUB_URL_RE.match(repo_url.strip())
        if not match:
            raise ValueError("Пришлите ссылку в формате https://github.com/owner/repository")

        owner = match.group("owner")
        repo = match.group("repo")
        repo_page = self.get_repo_page(owner, repo)
        default_branch = self.detect_default_branch(owner, repo, repo_page)
        archive = self.download_archive(owner, repo, default_branch)
        files, skipped_files = self.extract_files(archive)

        return RepoSnapshot(
            owner=owner,
            name=repo,
            default_branch=default_branch,
            description=self.extract_description(repo_page),
            stars=0,
            language=None,
            files=files,
            skipped_files=skipped_files,
        )

    def get_repo_page(self, owner: str, repo: str) -> str:
        response = self.session.get(f"https://github.com/{owner}/{repo}", timeout=20)
        if response.status_code == 404:
            raise ValueError("Репозиторий не найден или он приватный.")
        response.raise_for_status()
        return response.text

    def detect_default_branch(self, owner: str, repo: str, repo_page: str) -> str:
        branch = self.extract_branch_from_html(repo_page)
        candidates: list[str] = []
        if branch:
            candidates.append(branch)
        candidates.extend(["main", "master", "dev"])

        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if self.archive_exists(owner, repo, candidate):
                return candidate

        raise ValueError("Не удалось определить ветку репозитория для скачивания.")

    def extract_branch_from_html(self, repo_page: str) -> str | None:
        patterns = (
            r'"defaultBranch":"([^"]+)"',
            r"/commits/([A-Za-z0-9._/-]+)",
            r'branch[^>]*>\s*<span[^>]*>([^<]+)</span>',
        )
        for pattern in patterns:
            match = re.search(pattern, repo_page)
            if match:
                branch = match.group(1).strip().replace("&amp;", "&")
                if branch and "/" not in branch:
                    return branch
        return None

    def archive_exists(self, owner: str, repo: str, branch: str) -> bool:
        response = self.session.head(
            f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}",
            timeout=20,
            allow_redirects=True,
        )
        return response.status_code == 200

    def extract_description(self, repo_page: str) -> str:
        match = re.search(r'<meta name="description" content="([^"]+)"', repo_page)
        if not match:
            return ""
        description = html.unescape(match.group(1)).strip()
        prefix = "GitHub - "
        if description.startswith(prefix):
            parts = description.split(": ", 1)
            if len(parts) == 2:
                description = parts[1]
        return description

    def download_archive(self, owner: str, repo: str, branch: str) -> bytes:
        response = self.session.get(
            f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}",
            timeout=60,
        )
        response.raise_for_status()
        return response.content

    def extract_files(self, archive_bytes: bytes) -> tuple[list[RepoFile], int]:
        files: list[RepoFile] = []
        skipped = 0
        max_size = self.settings.max_file_size_kb * 1024

        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue

                path = Path(member.filename)
                relative_parts = path.parts[1:]
                if not relative_parts:
                    continue

                if any(part in SKIP_DIR_MARKERS for part in relative_parts):
                    skipped += 1
                    continue

                suffix = "".join(path.suffixes[-2:]) if path.name.endswith(".env.example") else path.suffix.lower()
                if suffix not in TEXT_EXTENSIONS and path.name not in {"Dockerfile", "Makefile"}:
                    skipped += 1
                    continue

                if member.file_size > max_size:
                    skipped += 1
                    continue

                raw = zf.read(member)
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    skipped += 1
                    continue

                files.append(
                    RepoFile(
                        path="/".join(relative_parts),
                        content=content,
                        size_bytes=member.file_size,
                    )
                )
                if len(files) >= self.settings.max_files_to_review:
                    break

        return files, skipped


class BaseRepositoryAnalyzer:
    def analyze(self, snapshot: RepoSnapshot) -> ReviewResult:
        if not snapshot.files:
            raise ValueError("В репозитории не нашлось текстовых файлов для анализа.")

        readme = self.find_readme(snapshot.files)
        tests_count = self.count_tests(snapshot.files)
        source_files = [file for file in snapshot.files if self.looks_like_source(file.path)]
        total_lines = sum(self.count_non_empty_lines(file.content) for file in source_files)
        comment_ratio = self.comment_ratio(source_files)

        strengths = self.collect_strengths(snapshot, readme, tests_count, total_lines)
        issues = self.collect_issues(snapshot, readme, tests_count, source_files, total_lines, comment_ratio)
        recommendations = self.collect_recommendations(snapshot, issues, tests_count, readme)
        ai_probability, ai_rationale = self.estimate_ai_probability(snapshot, readme, source_files)
        overall_score = self.score_project(strengths, issues, tests_count, readme)
        summary = self.build_summary(snapshot, overall_score, issues, ai_probability)

        return ReviewResult(
            summary=summary,
            strengths=strengths,
            issues=issues,
            recommendations=recommendations,
            ai_probability_percent=ai_probability,
            ai_rationale=ai_rationale,
            overall_score_percent=overall_score,
        )

    def find_readme(self, files: list[RepoFile]) -> RepoFile | None:
        for file in files:
            if file.path.split("/")[-1].lower() in README_NAMES:
                return file
        return None

    def count_tests(self, files: list[RepoFile]) -> int:
        total = 0
        for file in files:
            lowered = file.path.lower()
            if any(marker in lowered.split("/") for marker in TEST_MARKERS) or lowered.endswith(
                ("_test.py", ".spec.js", ".test.js", ".spec.ts", ".test.ts")
            ):
                total += 1
        return total

    def looks_like_source(self, path: str) -> bool:
        return any(
            path.endswith(ext)
            for ext in (".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".php", ".rb", ".cs", ".cpp", ".c", ".kt", ".rs")
        )

    def count_non_empty_lines(self, content: str) -> int:
        return sum(1 for line in content.splitlines() if line.strip())

    def comment_ratio(self, files: list[RepoFile]) -> float:
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
        return comment_lines / code_lines if code_lines else 0.0

    def collect_strengths(
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
        if readme and self.count_non_empty_lines(readme.content) >= 8:
            strengths.append("README не пустой и помогает понять, что делает проект.")
        if tests_count:
            strengths.append(f"Есть тесты или тестовые директории: найдено {tests_count}.")
        if total_lines >= 120:
            strengths.append("Проект уже выглядит как не совсем учебный скелет, а как рабочая попытка собрать функционал.")
        if not strengths:
            strengths.append("Есть минимальная структура проекта, с которой уже можно продолжать улучшение.")
        return strengths

    def collect_issues(
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
        elif self.count_non_empty_lines(readme.content) < 5:
            issues.append(Finding("README слишком короткий", "Есть файл README, но в нем мало пользы: не хватает запуска, описания и структуры проекта.", "medium"))
        if tests_count == 0:
            issues.append(Finding("Нет тестов", "Для учебного проекта это не критично, но хотя бы 1-2 smoke-теста сильно повышают доверие к работе.", "medium"))
        if total_lines < 40:
            issues.append(Finding("Очень мало исходного кода", "По найденным файлам проект пока выглядит недособранным или слишком маленьким для уверенной оценки.", "medium"))

        large_files = [file for file in source_files if self.count_non_empty_lines(file.content) > 250]
        if large_files:
            issues.append(Finding("Есть слишком крупные файлы", f"Крупные модули сложнее читать и поддерживать. Пример: {large_files[0].path}.", "medium"))

        if self.find_repeated_lines(source_files) >= 8:
            issues.append(Finding("Похоже на копипасту", "В коде много повторяющихся строк и однотипных блоков. Это сигнал к выносу общей логики в функции или модули.", "medium"))

        if comment_ratio < 0.01 and total_lines > 150:
            issues.append(Finding("Мало пояснений в коде", "Когда проект уже разросся, без коротких пояснений сложнее понять ключевые решения.", "low"))

        if snapshot.skipped_files > len(snapshot.files):
            issues.append(Finding("Не вся структура попала в анализ", "В репозитории много бинарных, больших или служебных файлов. Итоговый отзыв строится в основном по текстовым исходникам.", "low"))

        return issues

    def collect_recommendations(
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

    def estimate_ai_probability(
        self,
        snapshot: RepoSnapshot,
        readme: RepoFile | None,
        source_files: list[RepoFile],
    ) -> tuple[int, list[str]]:
        score = 12.0
        rationale: list[str] = []
        total_files = len(snapshot.files)
        total_lines = sum(self.count_non_empty_lines(file.content) for file in source_files)
        tests_count = self.count_tests(snapshot.files)
        filenames = [file.path.lower() for file in snapshot.files]

        if readme:
            lowered = readme.content.lower()
            phrase_hits = sum(1 for phrase in AI_PHRASES if phrase in lowered)
            if phrase_hits >= 2:
                score += 20
                rationale.append("README использует слишком шаблонные и маркетинговые формулировки.")
            elif self.count_non_empty_lines(readme.content) > 24:
                score += 12
                rationale.append("README заметно полирован для новичкового проекта.")

            if tests_count == 0 and self.count_non_empty_lines(readme.content) >= 12 and total_lines >= 120:
                score += 10
                rationale.append("Для новичка описание проекта выглядит заметно взрослее, чем инженерная проверяемость кода.")

        lengths = [self.count_non_empty_lines(file.content) for file in source_files if self.count_non_empty_lines(file.content) > 0]
        if lengths:
            mean = sum(lengths) / len(lengths)
            variance = sum((length - mean) ** 2 for length in lengths) / len(lengths)
            normalized_variance = math.sqrt(variance) / mean if mean else 0
            if len(lengths) >= 6 and normalized_variance < 0.28:
                score += 14
                rationale.append("Многие файлы похожи по размеру и структуре, что иногда встречается у AI-генерации.")

        identifier_words = self.collect_identifiers(source_files)
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

        structural_markers = sum(
            1
            for path in filenames
            if any(marker in path for marker in ("/handlers/", "/services/", "/controllers/", "/routers/", "/middlewares/", "/utils/"))
        )
        if total_files >= 10 and structural_markers >= 3 and tests_count == 0:
            score += 14
            rationale.append("Есть разложенная по слоям структура без тестовой базы, что часто встречается у AI-сборки учебных проектов.")

        config_markers = sum(
            1
            for path in filenames
            if path.endswith(("package.json", "tsconfig.json", ".env.example", "docker-compose.yml", "docker-compose.yaml", "eslint.config.js"))
        )
        if total_files >= 10 and config_markers >= 2 and total_lines >= 150 and tests_count == 0:
            score += 8
            rationale.append("Обвязка и конфиги выглядят более зрелыми, чем уровень проверенности и проработки проекта.")

        if any("console.log" in file.content or "print(" in file.content for file in source_files):
            score -= 5
            rationale.append("В проекте есть следы ручной отладки, это немного снижает подозрение на полностью AI-сгенерированную работу.")

        if total_files <= 6 and total_lines < 90:
            score -= 8
            rationale.append("Проект маленький и неровный по структуре, это больше похоже на обычную ручную учебную работу.")

        if tests_count == 0 and total_lines < 70 and (not readme or self.count_non_empty_lines(readme.content) < 8):
            score -= 6

        if snapshot.skipped_files > len(snapshot.files) and total_files <= 6:
            score -= 4

        score = max(5, min(95, round(score)))
        if not rationale:
            rationale.append("Явных паттернов, которые сильно кричат об AI-генерации, не видно.")
        rationale.append("Это эвристическая оценка, а не доказательство: процент стоит использовать как ориентир, а не как приговор.")
        return int(score), rationale[:3]

    def score_project(
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

    def build_summary(
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

    def collect_identifiers(self, source_files: list[RepoFile]) -> Counter:
        counter: Counter = Counter()
        for file in source_files:
            words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", file.content)
            counter.update(word.lower() for word in words)
        return counter

    def find_repeated_lines(self, source_files: list[RepoFile]) -> int:
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


class RepositoryAnalyzer(BaseRepositoryAnalyzer):
    def analyze(self, snapshot: RepoSnapshot) -> ReviewResult:
        if not snapshot.files:
            raise ValueError("В репозитории не нашлось текстовых файлов для анализа.")

        readme = self.find_readme(snapshot.files)
        tests_count = self.count_tests(snapshot.files)
        source_files = [file for file in snapshot.files if self.looks_like_source(file.path)]
        total_lines = sum(self.count_non_empty_lines(file.content) for file in source_files)
        comment_ratio = self.comment_ratio(source_files)
        reviewed_files = self.select_key_files(snapshot)
        patterns = self.inspect_source_patterns(source_files)

        strengths = super().collect_strengths(snapshot, readme, tests_count, total_lines)
        strengths.extend(self.collect_extra_strengths(patterns))
        strengths = self.deduplicate_strings(strengths, 4)

        issues = super().collect_issues(snapshot, readme, tests_count, source_files, total_lines, comment_ratio)
        issues.extend(self.collect_pattern_issues(patterns))
        issues = self.deduplicate_findings(issues, 6)

        assignment_summary, assignment_findings, assignment_recommendations = self.compare_with_assignment(snapshot, source_files)

        recommendations = super().collect_recommendations(snapshot, issues, tests_count, readme)
        recommendations.extend(self.collect_pattern_recommendations(patterns))
        recommendations.extend(assignment_recommendations)
        recommendations = self.deduplicate_strings(recommendations, 5)

        ai_probability, ai_rationale, ai_signals = self.estimate_ai_probability_v2(
            snapshot,
            readme,
            source_files,
            tests_count,
            patterns,
        )
        overall_score = super().score_project(strengths, issues, tests_count, readme)
        summary = self.build_summary_v2(snapshot, overall_score, issues, ai_probability, assignment_summary)
        detailed_analysis = self.build_detailed_analysis(snapshot, source_files, reviewed_files, patterns, tests_count, comment_ratio)

        return ReviewResult(
            summary=summary,
            strengths=strengths,
            issues=issues,
            recommendations=recommendations,
            ai_probability_percent=ai_probability,
            ai_rationale=ai_rationale,
            overall_score_percent=overall_score,
            detailed_analysis=detailed_analysis,
            ai_detection_signals=ai_signals,
            reviewed_files=reviewed_files,
            assignment_summary=assignment_summary,
            assignment_findings=assignment_findings,
            review_source="Эвристика",
        )

    def select_key_files(self, snapshot: RepoSnapshot) -> list[str]:
        ranked = sorted(snapshot.files, key=lambda item: (self.file_priority(item.path), len(item.content)), reverse=True)
        return [file.path for file in ranked[:8]]

    def file_priority(self, path: str) -> int:
        lowered = path.lower()
        score = 0
        for marker in (
            "readme",
            "package.json",
            "requirements.txt",
            "config",
            "server",
            "index",
            "main",
            "app",
            "handler",
            "router",
            "service",
            "controller",
            "db",
        ):
            if marker in lowered:
                score += 4
        if lowered.endswith((".ts", ".tsx", ".js", ".py", ".go", ".rs", ".java")):
            score += 3
        return score

    def inspect_source_patterns(self, source_files: list[RepoFile]) -> dict[str, object]:
        oversized_files = [
            (file.path, self.count_non_empty_lines(file.content))
            for file in source_files
            if self.count_non_empty_lines(file.content) > 220
        ]
        repeated_blocks = self.find_repeated_lines(source_files)
        risk_ops = 0
        error_handling_hits = 0
        validation_hits = 0
        user_input_hits = 0
        obvious_comment_hits = 0
        outdated_hits = 0
        verbose_identifier_hits = 0
        manual_trace_hits = 0

        obvious_comment_patterns = (
            r"^\s*(#|//)\s*(initialize|create|set|return|check|update|get|send|handle)\b",
            r"^\s*(#|//)\s*(создаем|создать|проверяем|возвращаем|устанавливаем|получаем)\b",
        )
        outdated_patterns = (
            r"\bvar\s+[A-Za-z_]",
            r"\.format\(",
            r"%s",
            r"new Promise\(",
            r"function\s*\(",
        )

        for file in source_files:
            content = file.content
            lowered = content.lower()
            risk_ops += len(re.findall(r"\b(fetch|axios|requests|sqlite|database|db\.|fs\.|readfile|writefile|open\(|httpx|telegraf|express)\b", lowered))
            error_handling_hits += len(re.findall(r"\b(try|catch|except|finally)\b", lowered))
            validation_hits += len(re.findall(r"\b(validate|validation|schema|zod|joi|sanitize|safeparse|required|isfinite|isdigit)\b", lowered))
            user_input_hits += len(re.findall(r"\b(req\.body|req\.query|ctx\.message|input|payload|process\.env|argv|params?)\b", lowered))
            manual_trace_hits += len(re.findall(r"\b(todo|fixme|console\.log|print\(|debugger)\b", lowered))
            verbose_identifier_hits += len(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{23,}\b", content))
            outdated_hits += sum(len(re.findall(pattern, content)) for pattern in outdated_patterns)
            for line in content.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if any(re.search(pattern, stripped, re.IGNORECASE) for pattern in obvious_comment_patterns):
                    obvious_comment_hits += 1

        return {
            "oversized_files": oversized_files,
            "repeated_blocks": repeated_blocks,
            "risk_ops": risk_ops,
            "error_handling_hits": error_handling_hits,
            "validation_hits": validation_hits,
            "user_input_hits": user_input_hits,
            "obvious_comment_hits": obvious_comment_hits,
            "outdated_hits": outdated_hits,
            "verbose_identifier_hits": verbose_identifier_hits,
            "manual_trace_hits": manual_trace_hits,
        }

    def collect_extra_strengths(self, patterns: dict[str, object]) -> list[str]:
        strengths: list[str] = []
        if int(patterns["error_handling_hits"]) >= 4:
            strengths.append("В коде есть базовая обработка ошибок, поэтому критичные сценарии выглядят чуть надежнее.")
        if int(patterns["validation_hits"]) >= 3:
            strengths.append("В проекте заметны следы валидации и защитных проверок, а не только happy-path логики.")
        return strengths

    def collect_pattern_issues(self, patterns: dict[str, object]) -> list[Finding]:
        issues: list[Finding] = []
        oversized_files = patterns["oversized_files"]
        if int(patterns["risk_ops"]) >= 4 and int(patterns["error_handling_hits"]) == 0:
            issues.append(Finding("Слабая обработка ошибок", "В проекте есть работа с сетью, ботом или файловой системой, но почти не видно try/catch или аналогичной защиты.", "medium"))
        if int(patterns["user_input_hits"]) >= 3 and int(patterns["validation_hits"]) == 0:
            issues.append(Finding("Слабая валидация входных данных", "Есть пользовательский ввод или внешние параметры, но не видно явной валидации и защитных проверок.", "medium"))
        if int(patterns["obvious_comment_hits"]) >= 8:
            issues.append(Finding("Есть очевидные комментарии", "Часть комментариев дублирует и без того понятный код. Лучше оставить только объяснение нетривиальных решений.", "low"))
        if int(patterns["outdated_hits"]) >= 3:
            issues.append(Finding("Есть устаревшие конструкции", "В коде встречаются шаблоны, которые выглядят старомодно для современного проекта и мешают читать логику.", "low"))
        if oversized_files and oversized_files[0][1] > 320:
            issues.append(Finding("Есть перегруженные файлы", f"Один из ключевых файлов уже слишком вырос: {oversized_files[0][0]} ({oversized_files[0][1]} строк).", "medium"))
        return issues

    def collect_pattern_recommendations(self, patterns: dict[str, object]) -> list[str]:
        recommendations: list[str] = []
        if int(patterns["risk_ops"]) >= 4 and int(patterns["error_handling_hits"]) == 0:
            recommendations.append("Добавить обработку ошибок вокруг сетевых запросов, БД и файловых операций, чтобы проект не падал на первом сбое.")
        if int(patterns["user_input_hits"]) >= 3 and int(patterns["validation_hits"]) == 0:
            recommendations.append("Явно валидировать пользовательский ввод и внешние параметры до основной бизнес-логики.")
        if int(patterns["obvious_comment_hits"]) >= 8:
            recommendations.append("Почистить комментарии, которые повторяют код, и оставить пояснения только возле реально сложных решений.")
        return recommendations

    def estimate_ai_probability_v2(
        self,
        snapshot: RepoSnapshot,
        readme: RepoFile | None,
        source_files: list[RepoFile],
        tests_count: int,
        patterns: dict[str, object],
    ) -> tuple[int, list[str], list[str]]:
        base_score, base_rationale = super().estimate_ai_probability(snapshot, readme, source_files)
        score = float(base_score)
        signals: list[str] = []

        if int(patterns["obvious_comment_hits"]) >= 8:
            score += 8
            signals.append("В коде много комментариев, которые объясняют очевидные действия, что похоже на учебно-шаблонную генерацию.")
        if int(patterns["verbose_identifier_hits"]) >= 12:
            score += 6
            signals.append("Встречается тяжеловесный нейминг с очень длинными идентификаторами, что часто бывает у AI-кода.")
        if int(patterns["repeated_blocks"]) >= 8:
            score += 8
            signals.append("По проекту размазаны повторяющиеся блоки и одинаковые синтаксические паттерны.")
        if int(patterns["outdated_hits"]) >= 3 and len(snapshot.files) >= 10:
            score += 5
            signals.append("В относительно современном проекте встречаются устаревшие конструкции, что иногда выдает машинную генерацию без контекстной подгонки.")
        if tests_count == 0 and len(snapshot.files) >= 10:
            score += 5
        if int(patterns["manual_trace_hits"]) >= 3:
            score -= 6
            signals.append("В проекте есть следы ручной отладки и черновой эволюции, это немного снижает подозрение на полностью AI-сгенерированную работу.")

        score = max(5, min(95, round(score)))
        rationale = self.deduplicate_strings([*signals, *base_rationale], 3)
        if not rationale:
            rationale = ["Явных паттернов, которые сильно кричат об AI-генерации, не видно."]
        return int(score), rationale, self.deduplicate_strings([*signals, *base_rationale], 5)

    def compare_with_assignment(
        self,
        snapshot: RepoSnapshot,
        source_files: list[RepoFile],
    ) -> tuple[str, list[str], list[str]]:
        assignment_text = snapshot.assignment_text.strip()
        if not assignment_text:
            return "", [], []

        corpus_parts = [snapshot.description, *(file.path for file in snapshot.files)]
        corpus_parts.extend(file.content[:2500] for file in source_files[:10])
        corpus = "\n".join(part for part in corpus_parts if part).lower()

        requirements = self.extract_assignment_requirements(assignment_text)
        findings: list[str] = []
        recommendations: list[str] = []
        covered = 0

        for requirement in requirements:
            keywords = [word for word in re.findall(r"[a-zA-Zа-яА-Я0-9]{4,}", requirement.lower()) if word not in self.stopwords()]
            if not keywords:
                continue
            hits = sum(1 for word in keywords[:6] if word in corpus)
            if hits >= max(2, min(4, len(keywords) // 2)):
                covered += 1
            else:
                findings.append(f"Пока не видно явного покрытия требования: {self.shorten(requirement, 120)}")

        tech_expectations = {
            "README": ("readme",),
            "тесты": ("test", "spec", "pytest", "jest", "vitest"),
            "Docker": ("docker", "docker-compose"),
            "SQLite": ("sqlite", "better-sqlite", "sql"),
            "Telegram": ("telegram", "telegraf", "bot"),
            "API": ("express", "fastapi", "flask", "router", "server"),
        }
        assignment_lower = assignment_text.lower()
        for label, markers in tech_expectations.items():
            if not any(marker.lower() in assignment_lower for marker in markers):
                continue
            if any(marker.lower() in corpus for marker in markers):
                covered += 1
            else:
                findings.append(f"В ТЗ есть ожидание по части «{label}», но в репозитории это пока не читается.")

        total_requirements = max(1, len(requirements))
        uncertain = len(findings)
        summary = (
            f"Загружено ТЗ {snapshot.assignment_filename or 'assignment'}: "
            f"эвристически найдено {total_requirements} явных требований, покрыто примерно {covered}, под вопросом {uncertain}."
        )
        if findings:
            recommendations.append("Сверить проект с пунктами ТЗ и явно закрыть требования, которые сейчас не подтверждаются кодом или README.")
        return summary, findings[:5], recommendations[:2]

    def extract_assignment_requirements(self, text: str) -> list[str]:
        lines = [line.strip(" -•\t") for line in text.splitlines()]
        requirement_lines = [
            line
            for line in lines
            if len(line) >= 20
            and re.search(r"\b(должен|должна|нужно|необходимо|требуется|реализовать|сделать|добавить|поддержать|must|should|implement|build|create|support)\b", line.lower())
        ]
        return self.deduplicate_strings(requirement_lines, 8)

    def build_detailed_analysis(
        self,
        snapshot: RepoSnapshot,
        source_files: list[RepoFile],
        reviewed_files: list[str],
        patterns: dict[str, object],
        tests_count: int,
        comment_ratio: float,
    ) -> list[str]:
        details: list[str] = []
        if reviewed_files:
            details.append("Основной разбор строился по файлам: " + ", ".join(reviewed_files[:5]))
        if source_files:
            largest = max(source_files, key=lambda item: self.count_non_empty_lines(item.content))
            largest_lines = self.count_non_empty_lines(largest.content)
            details.append(f"Самый крупный исходник сейчас {largest.path} ({largest_lines} непустых строк), его стоит первым кандидатом на декомпозицию.")
        details.append(
            "Тестовое покрытие "
            + ("видно в репозитории." if tests_count else "не читается по структуре репозитория и package/config-файлам.")
        )
        details.append(
            "Комментариев "
            + ("достаточно для навигации." if comment_ratio >= 0.03 else "мало, поэтому архитектурные решения приходится угадывать по коду.")
        )
        if int(patterns["risk_ops"]) >= 4:
            details.append(
                "В проекте есть интеграции с внешними сущностями или хранением данных, поэтому устойчивость к ошибкам особенно важна."
            )
        if int(patterns["repeated_blocks"]) >= 6:
            details.append("Повторяющиеся блоки намекают, что часть логики уже пора выносить в утилиты или отдельные сервисы.")
        return self.deduplicate_strings(details, 6)

    def build_summary_v2(
        self,
        snapshot: RepoSnapshot,
        overall_score: int,
        issues: list[Finding],
        ai_probability: int,
        assignment_summary: str,
    ) -> str:
        quality = "хорошее впечатление" if overall_score >= 75 else "нормальная база" if overall_score >= 55 else "сыроватое состояние"
        risk = "низкий" if ai_probability < 35 else "средний" if ai_probability < 65 else "повышенный"
        assignment_note = " Сравнение с ТЗ тоже учтено." if assignment_summary else ""
        return (
            f"Репозиторий {snapshot.owner}/{snapshot.name} оставляет {quality}: итоговая оценка {overall_score}%. "
            f"Найдено {len(issues)} зон для улучшения, а риск заметного влияния AI оценивается как {risk} ({ai_probability}%)."
            f"{assignment_note}"
        )

    def deduplicate_strings(self, items: list[str], limit: int) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in items:
            normalized = re.sub(r"\s+", " ", item.strip().lower())
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(item.strip())
            if len(result) >= limit:
                break
        return result

    def deduplicate_findings(self, items: list[Finding], limit: int) -> list[Finding]:
        result: list[Finding] = []
        seen: set[str] = set()
        for item in items:
            key = re.sub(r"\s+", " ", item.title.strip().lower())
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
            if len(result) >= limit:
                break
        return result

    def shorten(self, text: str, limit: int) -> str:
        text = re.sub(r"\s+", " ", text.strip())
        return text if len(text) <= limit else text[: limit - 3] + "..."

    def stopwords(self) -> set[str]:
        return {
            "если",
            "когда",
            "чтобы",
            "который",
            "которая",
            "которые",
            "должен",
            "должна",
            "нужно",
            "нужно",
            "нужно",
            "надо",
            "будет",
            "проекта",
            "проект",
            "сделать",
            "реализовать",
            "добавить",
            "support",
            "should",
            "must",
            "implement",
            "build",
            "create",
        }


def render_report(snapshot: RepoSnapshot, result: ReviewResult) -> str:
    parts: list[str] = []
    parts.append(f"<b>Проверка репозитория</b>\n<code>{snapshot.owner}/{snapshot.name}</code>")
    if snapshot.description:
        parts.append(f"<b>Описание:</b> {html.escape(snapshot.description)}")
    parts.append(
        f"<b>Итог:</b> {html.escape(result.summary)}\n"
        f"<b>Размер анализа:</b> {len(snapshot.files)} файлов, пропущено {snapshot.skipped_files}"
    )
    parts.append("<b>Сильные стороны:</b>\n" + render_list(result.strengths))
    parts.append("<b>Что поправить:</b>\n" + render_findings(result.issues))
    parts.append("<b>Рекомендации:</b>\n" + render_list(result.recommendations))
    parts.append(
        "<b>Вероятность использования AI:</b> "
        f"<code>{result.ai_probability_percent}%</code>\n"
        + render_list(result.ai_rationale)
    )
    return "\n\n".join(parts)


def render_list(items: list[str]) -> str:
    return "\n".join(f"• {html.escape(item)}" for item in items) if items else "• Ничего критичного не нашлось."


def render_findings(items: list[Finding]) -> str:
    if not items:
        return "• Серьезных проблем не найдено."
    return "\n".join(
        f"• [{item.severity.upper()}] {html.escape(item.title)}: {html.escape(item.detail)}"
        for item in items
    )
