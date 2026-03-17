from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(slots=True)
class Settings:
    telegram_bot_token: str
    github_token: str | None = None
    workdir: str = ".cache"
    max_files_to_review: int = 40
    max_file_size_kb: int = 180


def get_settings() -> Settings:
    load_dotenv()

    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    github_token = os.getenv("GITHUB_TOKEN", "").strip() or None

    if not telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    return Settings(
        telegram_bot_token=telegram_bot_token,
        github_token=github_token,
    )
