from __future__ import annotations

import logging
from html import escape

import telebot
from requests import HTTPError

from app.analyzer import RepositoryAnalyzer
from app.config import get_settings
from app.github_client import GitHubClient
from app.report import render_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def run() -> None:
    settings = get_settings()
    github_client = GitHubClient(settings)
    analyzer = RepositoryAnalyzer()
    bot = telebot.TeleBot(settings.telegram_bot_token, parse_mode="HTML")

    @bot.message_handler(commands=["start", "help"])
    def send_welcome(message: telebot.types.Message) -> None:
        bot.reply_to(
            message,
            (
                "Пришлите ссылку на публичный GitHub-репозиторий.\n\n"
                "Я:\n"
                "• скачаю репозиторий\n"
                "• дам обратную связь по качеству\n"
                "• отмечу сильные и слабые стороны\n"
                "• оценю вероятность использования AI в процентах\n\n"
                "Формат ссылки: <code>https://github.com/owner/repo</code>"
            ),
        )

    @bot.message_handler(func=lambda message: True, content_types=["text"])
    def review_repository(message: telebot.types.Message) -> None:
        repo_url = (message.text or "").strip()
        bot.send_chat_action(message.chat.id, "typing")

        try:
            snapshot = github_client.fetch_snapshot(repo_url)
            result = analyzer.analyze(snapshot)
            report = render_report(snapshot, result)
            bot.reply_to(message, report, disable_web_page_preview=True)
        except ValueError as exc:
            bot.reply_to(message, escape(str(exc)))
        except HTTPError as exc:
            logger.exception("GitHub request failed")
            bot.reply_to(
                message,
                escape(f"Не получилось скачать репозиторий с GitHub: {exc.response.status_code}."),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error")
            bot.reply_to(
                message,
                escape(
                    "Во время проверки произошла ошибка. "
                    f"Техническая деталь: {exc}"
                ),
            )

    logger.info("Bot started")
    bot.infinity_polling(skip_pending=True, timeout=30)
