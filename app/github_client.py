from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import requests

from app.config import Settings
from app.models import RepoFile, RepoSnapshot

GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
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

        metadata = self._get_repo_metadata(owner, repo)
        archive = self._download_archive(owner, repo, metadata["default_branch"])
        files, skipped_files = self._extract_files(archive)

        return RepoSnapshot(
            owner=owner,
            name=repo,
            default_branch=metadata["default_branch"],
            description=metadata.get("description") or "",
            stars=metadata.get("stargazers_count", 0),
            language=metadata.get("language"),
            files=files,
            skipped_files=skipped_files,
        )

    def _get_repo_metadata(self, owner: str, repo: str) -> dict:
        response = self.session.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            timeout=20,
        )

        if response.status_code == 404:
            raise ValueError("Репозиторий не найден или он приватный.")
        response.raise_for_status()
        return response.json()

    def _download_archive(self, owner: str, repo: str, branch: str) -> bytes:
        response = self.session.get(
            f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}",
            timeout=60,
        )
        response.raise_for_status()
        return response.content

    def _extract_files(self, archive_bytes: bytes) -> tuple[list[RepoFile], int]:
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
