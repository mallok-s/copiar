#!/usr/bin/env python3
"""copiar — GitHub Activity Mirror.

Fetches contribution data from a work GitHub account and recreates the
activity on a personal account by making backdated git commits.
All operations are idempotent — re-running never creates duplicate commits.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TypeAlias

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

ContributionMap: TypeAlias = dict[str, int]

GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class Config:
    personal_token: str
    work_username: str
    personal_username: str
    target_repo: str
    start_date: date
    end_date: date
    local_dir: Path | None
    dry_run: bool
    yes: bool
    keep_repo: bool
    backfill: bool


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def run_git(
    args: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
) -> str:
    """Run a git command; exit 1 with stderr on failure."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )
    if result.returncode != 0:
        print(f"git {' '.join(args)} failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


def configure_git(repo_dir: Path) -> None:
    """Set local git user.name and user.email (required in CI)."""
    run_git(["config", "user.name", "copiar"], cwd=repo_dir)
    run_git(["config", "user.email", "copiar@users.noreply.github.com"], cwd=repo_dir)


def load_existing_commits(repo_dir: Path) -> ContributionMap:
    """Single-pass git log → date count map."""
    # If the repo has no commits yet, return empty map
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {}

    output = run_git(
        ["log", "--format=%ad", "--date=format:%Y-%m-%d"],
        cwd=repo_dir,
    )
    counts: ContributionMap = {}
    for line in output.splitlines():
        day = line.strip()
        if day:
            counts[day] = counts.get(day, 0) + 1
    return counts


def create_commits_for_day(repo_dir: Path, day: str, count: int) -> None:
    """Create `count` backdated empty commits for `day` at noon UTC."""
    timestamp = f"{day}T12:00:00+00:00"
    date_env = {
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_COMMITTER_DATE": timestamp,
    }
    for i in range(count):
        run_git(
            [
                "commit",
                "--allow-empty",
                "-m",
                f"mirror: {day} ({i + 1}/{count})",
            ],
            cwd=repo_dir,
            env=date_env,
        )


def push_to_remote(repo_dir: Path) -> None:
    """Detect current branch and push without force."""
    branch = run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_dir,
    ).strip()
    run_git(["push", "origin", branch], cwd=repo_dir)


# ---------------------------------------------------------------------------
# Repo management
# ---------------------------------------------------------------------------


def ensure_repo_exists(username: str, token: str, repo_name: str) -> tuple[str, bool]:
    """Create repo if absent; return (HTTPS URL with token, repo_has_commits)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Check if it exists
    resp = requests.get(
        f"{GITHUB_API}/repos/{username}/{repo_name}",
        headers=headers,
        timeout=30,
    )

    if resp.status_code == 200:
        data = resp.json()
        has_commits = (data.get("size", 0) > 0) or (
            data.get("default_branch") is not None and data.get("pushed_at") is not None
        )
        url = f"https://{token}@github.com/{username}/{repo_name}.git"
        return url, has_commits

    if resp.status_code == 404:
        # Create it
        create_resp = requests.post(
            f"{GITHUB_API}/user/repos",
            headers=headers,
            json={
                "name": repo_name,
                "private": True,
                "description": "GitHub contribution mirror",
                "auto_init": False,
            },
            timeout=30,
        )
        if create_resp.status_code == 422:
            print(
                f"Error: repo name conflict creating '{repo_name}'. Exit.",
                file=sys.stderr,
            )
            sys.exit(1)
        create_resp.raise_for_status()
        url = f"https://{token}@github.com/{username}/{repo_name}.git"
        return url, False

    print(
        f"Error checking repo: {resp.status_code} {resp.text}",
        file=sys.stderr,
    )
    sys.exit(1)


def _repo_has_remote_commits(username: str, repo_name: str, token: str) -> bool:
    """Check if the remote repo has any commits via REST API."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.get(
        f"{GITHUB_API}/repos/{username}/{repo_name}/commits",
        headers=headers,
        params={"per_page": 1},
        timeout=30,
    )
    if resp.status_code == 409:
        # Empty repo
        return False
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    commits = resp.json()
    return isinstance(commits, list) and len(commits) > 0


def prepare_local_repo(
    local_dir: Path | None,
    remote_url: str,
    repo_has_commits: bool,
    username: str,
    repo_name: str,
    token: str,
) -> Path:
    """Clone if remote has history, else init fresh. Always configures git."""
    if local_dir is not None:
        repo_dir = local_dir
        repo_dir.mkdir(parents=True, exist_ok=True)

        # Check if it's already a git repo pointing to the right remote
        check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if check.returncode == 0:
            # Already a git repo — pull latest
            if repo_has_commits or _repo_has_remote_commits(username, repo_name, token):
                run_git(["pull", "--rebase", "origin"], cwd=repo_dir)
            configure_git(repo_dir)
            return repo_dir
        # Not yet a git repo in this dir
    else:
        repo_dir = Path(tempfile.mkdtemp(prefix="copiar-"))

    actually_has_commits = repo_has_commits or _repo_has_remote_commits(
        username, repo_name, token
    )

    if actually_has_commits:
        # Clone into the target directory
        subprocess.run(
            ["git", "clone", remote_url, str(repo_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        # Init fresh repo
        run_git(["init", "-b", "main"], cwd=repo_dir)
        run_git(["remote", "add", "origin", remote_url], cwd=repo_dir)

    configure_git(repo_dir)
    return repo_dir


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def _graphql(query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
    """Execute a GraphQL query; exit 1 on auth failure."""
    resp = requests.post(
        GITHUB_GRAPHQL,
        json={"query": query, "variables": variables},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if resp.status_code == 401:
        print(
            "Error: GitHub API returned 401. Check that your token has "
            "'repo' and 'read:user' scopes.",
            file=sys.stderr,
        )
        sys.exit(1)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        print(f"GraphQL errors: {data['errors']}", file=sys.stderr)
        sys.exit(1)
    result: dict[str, Any] = data["data"]
    return result


def fetch_user_created_at(username: str, token: str) -> date:
    """Return the account creation date for `username`."""
    query = """
    query($login: String!) {
      user(login: $login) {
        createdAt
      }
    }
    """
    data = _graphql(query, {"login": username}, token)
    user = data.get("user")
    if user is None:
        print(
            f"Error: GitHub user '{username}' not found.",
            file=sys.stderr,
        )
        sys.exit(1)
    created_at_str: str = user["createdAt"]
    return date.fromisoformat(created_at_str[:10])


def _fetch_contributions_chunk(
    username: str,
    token: str,
    start: date,
    end: date,
) -> ContributionMap:
    """Fetch contributions for a single ≤365-day chunk."""
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar {
            weeks {
              contributionDays {
                date
                contributionCount
              }
            }
          }
        }
      }
    }
    """
    variables: dict[str, Any] = {
        "login": username,
        "from": f"{start.isoformat()}T00:00:00Z",
        "to": f"{end.isoformat()}T23:59:59Z",
    }
    data = _graphql(query, variables, token)
    user = data.get("user")
    if user is None:
        print(
            f"Error: GitHub user '{username}' not found.",
            file=sys.stderr,
        )
        sys.exit(1)
    calendar = user["contributionsCollection"]["contributionCalendar"]
    result: ContributionMap = {}
    for week in calendar["weeks"]:
        for day_data in week["contributionDays"]:
            count: int = day_data["contributionCount"]
            if count > 0:
                result[day_data["date"]] = count
    return result


def fetch_contributions(
    work_username: str,
    token: str,
    start: date,
    end: date,
) -> ContributionMap:
    """Fetch contributions, splitting into ≤365-day chunks."""
    merged: ContributionMap = {}
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(end, chunk_start + timedelta(days=364))
        chunk = _fetch_contributions_chunk(work_username, token, chunk_start, chunk_end)
        merged.update(chunk)
        chunk_start = chunk_end + timedelta(days=1)
    return merged


# ---------------------------------------------------------------------------
# CLI / config
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        prog="copiar",
        description="Mirror GitHub contributions to your personal account.",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Start from work account creation date",
    )
    parser.add_argument(
        "--start",
        metavar="YYYY-MM-DD",
        help="ISO start date",
    )
    parser.add_argument(
        "--end",
        metavar="YYYY-MM-DD",
        help="ISO end date (default: today)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + print delta, no git ops",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompt",
    )
    parser.add_argument(
        "--keep-repo",
        action="store_true",
        help="Don't delete temp dir after push",
    )
    parser.add_argument(
        "--local-dir",
        metavar="PATH",
        help="Use specific dir instead of tempdir",
    )
    parser.add_argument(
        "--env",
        metavar="PATH",
        help="Path to alternate .env file",
    )
    return parser.parse_args()


def load_config(args: argparse.Namespace) -> Config:
    """Load config from env + CLI args."""
    env_path = Path(args.env) if args.env else None
    load_dotenv(dotenv_path=env_path)

    def require_env(key: str) -> str:
        val = os.environ.get(key, "").strip()
        if not val:
            print(
                f"Error: {key} is required (set in env or .env file).",
                file=sys.stderr,
            )
            sys.exit(1)
        return val

    personal_token = require_env("PERSONAL_GITHUB_TOKEN")
    work_username = require_env("WORK_GITHUB_USERNAME")
    personal_username = require_env("PERSONAL_GITHUB_USERNAME")
    target_repo = require_env("TARGET_REPO_NAME")

    today = datetime.now(tz=UTC).date()

    # Determine date range
    if args.start:
        start_date = date.fromisoformat(args.start)
    elif os.environ.get("START_DATE"):
        start_date = date.fromisoformat(os.environ["START_DATE"])
    else:
        # Default: yesterday (will be overridden for --backfill in main)
        start_date = today - timedelta(days=1)

    if args.end:
        end_date = date.fromisoformat(args.end)
    elif os.environ.get("END_DATE"):
        end_date = date.fromisoformat(os.environ["END_DATE"])
    else:
        end_date = today

    local_dir: Path | None = None
    if args.local_dir:
        local_dir = Path(args.local_dir)
    elif os.environ.get("LOCAL_REPO_DIR"):
        local_dir = Path(os.environ["LOCAL_REPO_DIR"])

    return Config(
        personal_token=personal_token,
        work_username=work_username,
        personal_username=personal_username,
        target_repo=target_repo,
        start_date=start_date,
        end_date=end_date,
        local_dir=local_dir,
        dry_run=args.dry_run,
        yes=args.yes,
        keep_repo=args.keep_repo,
        backfill=args.backfill,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:  # noqa: C901, PLR0912, PLR0915
    """Entry point."""
    args = parse_args()
    config = load_config(args)

    # Override start date for backfill
    if config.backfill and not args.start:
        print(f"Fetching account creation date for '{config.work_username}'...")
        account_start = fetch_user_created_at(
            config.work_username, config.personal_token
        )
        config = Config(
            personal_token=config.personal_token,
            work_username=config.work_username,
            personal_username=config.personal_username,
            target_repo=config.target_repo,
            start_date=account_start,
            end_date=config.end_date,
            local_dir=config.local_dir,
            dry_run=config.dry_run,
            yes=config.yes,
            keep_repo=config.keep_repo,
            backfill=config.backfill,
        )
        print(f"Backfill start: {config.start_date}")

    print(
        f"Fetching contributions for '{config.work_username}' "
        f"from {config.start_date} to {config.end_date}..."
    )
    contributions = fetch_contributions(
        config.work_username,
        config.personal_token,
        config.start_date,
        config.end_date,
    )

    if not contributions:
        print("No contributions found in the specified date range.")
        return

    total_target = sum(contributions.values())
    print(
        f"Found {len(contributions)} active days ({total_target} total contributions)."
    )

    if config.dry_run:
        # In dry run we can't compute existing without cloning — show full target
        print("\n[dry-run] Contribution delta (target counts):")
        for day in sorted(contributions):
            print(f"  {day}: {contributions[day]}")
        print(
            f"\n[dry-run] Would create up to {total_target} commits "
            f"across {len(contributions)} days."
        )
        return

    # Ensure mirror repo exists
    print(f"Checking mirror repo '{config.target_repo}'...")
    remote_url, repo_has_commits = ensure_repo_exists(
        config.personal_username,
        config.personal_token,
        config.target_repo,
    )

    # Prepare local repo (clone or init)
    repo_dir: Path | None = None
    temp_dir: Path | None = None
    try:
        repo_dir = prepare_local_repo(
            config.local_dir,
            remote_url,
            repo_has_commits,
            config.personal_username,
            config.target_repo,
            config.personal_token,
        )
        if config.local_dir is None:
            temp_dir = repo_dir

        # Load existing commits → compute delta
        existing = load_existing_commits(repo_dir)
        needed: ContributionMap = {
            day: target - existing.get(day, 0)
            for day, target in contributions.items()
            if target - existing.get(day, 0) > 0
        }

        if not needed:
            print("Mirror is already up to date.")
            return

        total_needed = sum(needed.values())
        print(f"\nCommits to create: {total_needed} across {len(needed)} days.")

        # Confirmation
        if not config.yes:
            try:
                answer = input("Proceed? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return
            if answer not in {"y", "yes"}:
                print("Aborted.")
                return

        # Create commits
        for day in sorted(needed):
            count = needed[day]
            print(f"  {day}: +{count} commits", end="", flush=True)
            create_commits_for_day(repo_dir, day, count)
            print(" ✓")

        # Push
        print("Pushing to remote...")
        push_to_remote(repo_dir)

        print(
            f"\nDone! View your profile: https://github.com/{config.personal_username}"
        )

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if temp_dir is not None and not config.keep_repo:
            shutil.rmtree(temp_dir, ignore_errors=True)
        elif repo_dir is not None and config.local_dir is None and config.keep_repo:
            print(f"Kept repo at: {repo_dir}")


if __name__ == "__main__":
    main()
