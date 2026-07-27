#!/usr/bin/env python3
"""Post an AI code review summary to the current GitLab merge request."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass

import requests
from openai import OpenAI


COMMENT_MARKER = "<!-- ai-cr-comment -->"
DEFAULT_MODEL = "gpt-5.3-codex"
DEFAULT_MAX_DIFF_CHARS = 120_000
DEFAULT_MAX_CONTEXT_CHARS = 20_000


@dataclass(frozen=True)
class ReviewContext:
    project_id: str
    mr_iid: str
    api_url: str
    gitlab_token: str
    target_branch: str
    openai_api_key: str
    model: str
    max_diff_chars: int
    max_context_chars: int


def get_required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def load_context() -> ReviewContext:
    return ReviewContext(
        project_id=get_required_env("CI_PROJECT_ID"),
        mr_iid=get_required_env("CI_MERGE_REQUEST_IID"),
        api_url=get_required_env("CI_API_V4_URL").rstrip("/"),
        gitlab_token=(
            os.environ.get("GITLAB_TOKEN", "").strip()
            or os.environ.get("GITLAB_API_TOKEN", "").strip()
        ),
        target_branch=get_required_env("CI_MERGE_REQUEST_TARGET_BRANCH_NAME"),
        openai_api_key=get_required_env("OPENAI_API_KEY"),
        model=(os.environ.get("AI_CR_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL),
        max_diff_chars=int(
            os.environ.get("AI_CR_MAX_DIFF_CHARS", DEFAULT_MAX_DIFF_CHARS)
        ),
        max_context_chars=int(
            os.environ.get("AI_CR_MAX_CONTEXT_CHARS", DEFAULT_MAX_CONTEXT_CHARS)
        ),
    )


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def ensure_target_branch(target_branch: str) -> None:
    subprocess.run(
        ["git", "fetch", "origin", target_branch, "--depth", "100"],
        check=True,
        capture_output=True,
        text=True,
    )


def collect_changed_files(target_branch: str) -> str:
    return run_git("diff", "--name-only", f"origin/{target_branch}...HEAD").strip()


def collect_diff(target_branch: str) -> str:
    return run_git(
        "diff",
        "--find-renames",
        "--unified=3",
        f"origin/{target_branch}...HEAD",
    )


def truncate_text(text: str, limit: int, label: str) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return (
        text[:limit]
        + f"\n\n...[truncated {omitted} characters from {label} to fit model context]..."
    )


def build_prompt(changed_files: str, diff_text: str) -> list[dict[str, str]]:
    system = (
        "You are a senior software engineer performing merge request review. "
        "Focus on bugs, regressions, security issues, missing validation, and missing tests. "
        "Ignore cosmetic style nitpicks. "
        "If there are no meaningful issues, say exactly 'No major findings.'"
    )
    user = f"""Review this merge request diff.

Return concise markdown with:
1. A short heading.
2. Flat bullets for each finding, sorted by severity.
3. For each finding include file path and why it matters.
4. If relevant, add one short 'Testing gaps' section.

Changed files:
{changed_files or "(unable to determine changed files)"}

Unified diff:
{diff_text}
"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def generate_review(openai_api_key: str, model: str, prompt: list[dict[str, str]]) -> str:
    client = OpenAI(api_key=openai_api_key)
    response = client.responses.create(model=model, input=prompt)
    text = (response.output_text or "").strip()
    if not text:
        raise RuntimeError("OpenAI returned an empty review")
    return text


def gitlab_headers(token: str) -> dict[str, str]:
    if not token:
        raise RuntimeError("missing required environment variable: GITLAB_TOKEN")
    return {"PRIVATE-TOKEN": token}


def delete_previous_comment(ctx: ReviewContext) -> None:
    notes_url = (
        f"{ctx.api_url}/projects/{ctx.project_id}/merge_requests/{ctx.mr_iid}/notes"
    )
    response = requests.get(notes_url, headers=gitlab_headers(ctx.gitlab_token), timeout=30)
    response.raise_for_status()
    for note in response.json():
        body = note.get("body", "")
        if COMMENT_MARKER in body:
            requests.delete(
                f"{notes_url}/{note['id']}",
                headers=gitlab_headers(ctx.gitlab_token),
                timeout=30,
            ).raise_for_status()


def post_comment(ctx: ReviewContext, review_text: str) -> None:
    notes_url = (
        f"{ctx.api_url}/projects/{ctx.project_id}/merge_requests/{ctx.mr_iid}/notes"
    )
    body = (
        f"{COMMENT_MARKER}\n"
        "## AI Code Review\n\n"
        f"_Model: `{ctx.model}`_\n\n"
        f"{review_text}"
    )
    response = requests.post(
        notes_url,
        headers=gitlab_headers(ctx.gitlab_token),
        data={"body": body},
        timeout=30,
    )
    response.raise_for_status()


def main() -> int:
    try:
        ctx = load_context()
        ensure_target_branch(ctx.target_branch)
        changed_files = truncate_text(
            collect_changed_files(ctx.target_branch),
            ctx.max_context_chars,
            "changed file list",
        )
        diff_text = truncate_text(
            collect_diff(ctx.target_branch),
            ctx.max_diff_chars,
            "diff",
        )
        prompt = build_prompt(changed_files, diff_text)
        review_text = generate_review(ctx.openai_api_key, ctx.model, prompt)
        delete_previous_comment(ctx)
        post_comment(ctx, review_text)
        print("Posted AI code review comment to merge request.")
        return 0
    except Exception as exc:  # pragma: no cover - used in CI
        print(f"AI code review failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
