# copiar

Mirrors your work GitHub contribution graph to your personal account by making backdated commits to a target repository. Runs daily via GitHub Actions.

All operations are **idempotent** — re-running never creates duplicate commits.

## How it works

1. Fetches your work account's public contribution data via GitHub's GraphQL API (no work account token needed — public profiles are queryable with your personal PAT)
2. Clones the mirror repo and counts existing commits per day
3. Creates only the delta (new commits needed to match the target count)
4. Pushes to the mirror repo

## Setup

### 1. Create a personal access token

Go to **GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)**

Required scopes: `repo`, `read:user`, `delete_repo` (only needed for `--reset-repo`)

### 2. Configure the Actions workflow

In your `copiar` repo settings → **Secrets and variables → Actions**:

| Type | Name | Value |
|---|---|---|
| Secret | `PERSONAL_GITHUB_TOKEN` | your PAT |
| Variable | `WORK_GITHUB_USERNAME` | your work GitHub username |
| Variable | `PERSONAL_GITHUB_USERNAME` | your personal GitHub username |
| Variable | `TARGET_REPO_NAME` | e.g. `contribution-mirror` |

The target repo is created automatically on first run if it doesn't exist.

### 3. Local usage

```bash
cp .env.example .env
# fill in .env with your values

# preview what would be synced (no writes)
uv run copiar.py --dry-run --backfill

# full backfill
uv run copiar.py --yes --backfill

# wipe and recreate the mirror repo (e.g. to fix wrong-author commits)
uv run copiar.py --reset-repo --yes --backfill

# sync a specific date range
uv run copiar.py --yes --start 2025-01-01 --end 2025-03-31
```

## CLI flags

| Flag | Description |
|---|---|
| `--backfill` | Start from work account creation date |
| `--start YYYY-MM-DD` | Start date |
| `--end YYYY-MM-DD` | End date (default: today) |
| `--dry-run` | Fetch and print delta, no git ops |
| `--yes` / `-y` | Skip confirmation prompt |
| `--keep-repo` | Don't delete the temp clone after push |
| `--local-dir PATH` | Use a specific directory instead of a temp dir |
| `--env PATH` | Path to an alternate `.env` file |
| `--reset-repo` | Delete and recreate the mirror repo before running (requires `delete_repo` token scope) |

## Automation

The workflow runs daily at 03:00 UTC, syncing the previous day's contributions.

You can also trigger it manually from **Actions → Sync Contributions → Run workflow**, with options to backfill or specify a custom date range.

## Development

```bash
uv run ruff check copiar.py && uv run ruff format copiar.py
uv run ty check copiar.py
```

## License

MIT
