from __future__ import annotations

from dataclasses import dataclass, field


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
