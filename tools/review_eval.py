from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from bot_runtime import GitHubClient, RepositoryAnalyzer, get_settings
from smart_review_runtime import SmartReviewer


def load_cases(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        cases = data.get("cases", [])
    else:
        cases = data
    if not isinstance(cases, list):
        raise ValueError("Eval cases file must contain a list or a {\"cases\": [...]} object.")
    return [case for case in cases if isinstance(case, dict)]


def contains_marker(items: list[str], markers: list[str]) -> bool:
    haystack = "\n".join(items).lower()
    return any(marker.lower() in haystack for marker in markers)


def evaluate_case(case: dict[str, Any], reviewer: SmartReviewer, analyzer: RepositoryAnalyzer, github: GitHubClient) -> dict[str, Any]:
    repo_url = str(case["repo_url"]).strip()
    mode = str(case.get("mode", "full")).strip().lower()
    snapshot = github.fetch_snapshot(repo_url)
    heuristic = analyzer.analyze(snapshot)
    result = heuristic if mode == "heuristic" else reviewer.review(snapshot, heuristic)

    issue_texts = [f"{issue.title}: {issue.detail}" for issue in result.issues]
    findings: list[str] = []

    expected_source = str(case.get("expected_source_contains", "")).strip()
    if expected_source and expected_source.lower() not in result.review_source.lower():
        findings.append(f"source mismatch: expected contains '{expected_source}', got '{result.review_source}'")

    ai_min = case.get("ai_probability_min")
    ai_max = case.get("ai_probability_max")
    if ai_min is not None and result.ai_probability_percent < int(ai_min):
        findings.append(f"ai_probability too low: {result.ai_probability_percent} < {ai_min}")
    if ai_max is not None and result.ai_probability_percent > int(ai_max):
        findings.append(f"ai_probability too high: {result.ai_probability_percent} > {ai_max}")

    required_issue_markers = [str(item) for item in case.get("required_issue_markers", [])]
    for marker in required_issue_markers:
        if marker.lower() not in "\n".join(issue_texts).lower():
            findings.append(f"missing issue marker: {marker}")

    banned_issue_markers = [str(item) for item in case.get("banned_issue_markers", [])]
    if contains_marker(issue_texts, banned_issue_markers):
        findings.append("banned issue marker present")

    banned_signal_markers = [str(item) for item in case.get("banned_signal_markers", [])]
    if contains_marker(result.ai_detection_signals, banned_signal_markers):
        findings.append("banned AI signal marker present")

    banned_summary_markers = [str(item) for item in case.get("banned_summary_markers", [])]
    if contains_marker([result.summary], banned_summary_markers):
        findings.append("banned summary marker present")

    required_summary_markers = [str(item) for item in case.get("required_summary_markers", [])]
    for marker in required_summary_markers:
        if marker.lower() not in result.summary.lower():
            findings.append(f"missing summary marker: {marker}")

    return {
        "name": case.get("name", repo_url),
        "repo_url": repo_url,
        "mode": mode,
        "review_source": result.review_source,
        "ai_probability_percent": result.ai_probability_percent,
        "overall_score_percent": result.overall_score_percent,
        "summary": result.summary,
        "issues": issue_texts,
        "ai_detection_signals": result.ai_detection_signals,
        "passed": not findings,
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run regression checks for repository review quality.")
    parser.add_argument("--cases", default="eval_cases.example.json", help="Path to eval cases JSON file.")
    parser.add_argument("--output", default=".cache/eval_report.json", help="Where to write the eval result JSON.")
    args = parser.parse_args()

    cases_path = Path(args.cases)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    github = GitHubClient(settings)
    analyzer = RepositoryAnalyzer()
    reviewer = SmartReviewer()

    cases = load_cases(cases_path)
    results = [evaluate_case(case, reviewer, analyzer, github) for case in cases]

    output_path.write_text(json.dumps({"cases": results}, ensure_ascii=False, indent=2), encoding="utf-8")

    passed = sum(1 for item in results if item["passed"])
    total = len(results)
    print(f"Eval report: {passed}/{total} passed")
    for item in results:
        status = "PASS" if item["passed"] else "FAIL"
        print(f"[{status}] {item['name']} -> {item['review_source']}")
        for finding in item["findings"]:
            print(f"  - {finding}")
    print(f"Saved: {output_path}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
