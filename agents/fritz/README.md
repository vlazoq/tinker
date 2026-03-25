# Fritz — Git & Remote Platform Integration for Tinker

Fritz is Tinker's version control layer. When Grub produces working code, Fritz
commits it, opens pull requests, waits for CI, and merges — all automatically.

Think of Fritz as the final step in the pipeline:

```
Your problem statement
        │
        ▼
 Tinker (design)   →  architecture artifacts
        │
        ▼
  Grub (code)      →  working, tested files on disk
        │
        ▼
  Fritz (ship)     →  commit → PR → CI green → merge
```

Without Fritz, Grub's output sits on your disk and you commit it manually.
With Fritz, the whole loop — design, code, review, commit, ship — runs
autonomously overnight.

---

## What Fritz Does

| Capability | Detail |
|---|---|
| Local git | commit, branch, tag, push |
| GitHub | PRs, merges, releases, collaborators, CI status polling |
| Gitea | Same API surface, self-hosted (your NAS, home lab) |
| Multi-remote | Push to GitHub and Gitea simultaneously in one call |
| Push policy | Configurable rules: PR-required, CI-gated, protected branches |
| Audit trail | Every action logged to SQLite: what was pushed, when, by whom |
| Bot account | Uses a dedicated AI committer account (keeps your history clean) |

---

## Quick Start

```python
from fritz import FritzAgent, FritzConfig

config = FritzConfig.from_file("fritz_config.json")
fritz  = FritzAgent(config)

# Commit files and open a PR (or push to feature branch — see push policy)
await fritz.commit_and_ship(
    files=["src/router.py", "tests/test_router.py"],
    message="feat: add request router with 3 route handlers",
    task_id="grub-abc123",   # links the commit back to Grub's task
)
```

That single call:
1. Stages the files with `git add`
2. Commits with the given message + a `[tinker: task_id]` trailer
3. Checks the push policy (PR required? Protected branch? CI gate?)
4. If a PR is required: opens one on GitHub or Gitea with a generated description
5. Polls CI until green (or times out)
6. Merges (squash by default) and deletes the feature branch

---

## Configuration

Fritz is configured by `fritz_config.json` (auto-created on first run).
Environment variables with the `FRITZ_` prefix override the JSON file.

### Minimal config — local git only

```json
{
  "repo_path": ".",
  "platform": "none",
  "committer_name": "Tinker Bot",
  "committer_email": "tinker@localhost"
}
```

### Full config — GitHub with bot account

```json
{
  "repo_path": ".",
  "platform": "github",
  "remote_url": "https://github.com/yourname/yourrepo",
  "bot_token": "ghp_...",
  "committer_name": "Tinker Bot",
  "committer_email": "tinker-bot@users.noreply.github.com",
  "push_policy": {
    "allow_push_to_main": false,
    "require_pr": true,
    "require_ci_green": true,
    "auto_merge_method": "squash",
    "protected_branches": ["production", "release"],
    "ci_timeout_seconds": 600
  }
}
```

### Gitea (self-hosted, on your NAS)

```json
{
  "repo_path": ".",
  "platform": "gitea",
  "remote_url": "http://nas:3000/yourname/yourrepo",
  "bot_token": "your-gitea-token",
  "committer_name": "Tinker Bot",
  "committer_email": "tinker@nas.local"
}
```

### Multi-remote (GitHub + Gitea at the same time)

Set both GitHub and Gitea configs:

```json
{
  "repo_path": ".",
  "remotes": [
    {
      "name": "github",
      "platform": "github",
      "url": "https://github.com/yourname/yourrepo",
      "token": "ghp_..."
    },
    {
      "name": "gitea",
      "platform": "gitea",
      "url": "http://nas:3000/yourname/yourrepo",
      "token": "gitea-token"
    }
  ]
}
```

Fritz will `git push` to both remotes and open PRs on both platforms.

---

## Environment Variables

All `fritz_config.json` fields can be overridden via env vars:

```bash
FRITZ_REPO_PATH=.
FRITZ_PLATFORM=github             # github | gitea | none
FRITZ_REMOTE_URL=https://...
FRITZ_BOT_TOKEN=ghp_...
FRITZ_COMMITTER_NAME="Tinker Bot"
FRITZ_COMMITTER_EMAIL=tinker@...

# Push policy
FRITZ_ALLOW_PUSH_TO_MAIN=false
FRITZ_REQUIRE_PR=true
FRITZ_REQUIRE_CI_GREEN=true
FRITZ_CI_TIMEOUT=600
```

---

## Confirmation Gate (Human-in-the-Loop)

From the `TINKER_CONFIRM_BEFORE` feature: you can require a human to approve
every `git push` before Fritz executes it.

Set in `.env`:
```bash
TINKER_CONFIRM_BEFORE=git_push
TINKER_CONFIRM_TIMEOUT=300      # auto-approve after 5 minutes
```

When Fritz is about to push, it will either:
- **CLI mode**: print a prompt to your terminal and wait for `y/N`
- **Dashboard mode**: show the pending request in the webui at `GET /api/confirmations`

To respond via the API:
```bash
# Approve
curl -X POST http://localhost:8082/api/confirm/abc12345 \
     -H "Content-Type: application/json" \
     -d '{"approved": true}'

# Deny
curl -X POST http://localhost:8082/api/confirm/abc12345 \
     -d '{"approved": false}'
```

---

## Push Policy Reference

The push policy controls when and how Fritz ships code.

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `allow_push_to_main` | bool | `false` | Allow direct push to `main`/`master` |
| `require_pr` | bool | `true` | Always go via Pull Request |
| `require_ci_green` | bool | `true` | Block merge until CI passes |
| `auto_merge_method` | str | `"squash"` | How PRs merge: `squash`, `merge`, `rebase` |
| `protected_branches` | list | `["production", "release"]` | Never push here directly |
| `ci_timeout_seconds` | int | `600` | Give up waiting for CI after N seconds |

**Recommended settings for a home lab:**
```json
{
  "allow_push_to_main": false,
  "require_pr": true,
  "require_ci_green": false,
  "ci_timeout_seconds": 120
}
```
CI is often not configured on home lab repos, so `require_ci_green: false`
prevents Fritz from waiting forever for checks that will never run.

**Recommended settings for a real project:**
```json
{
  "allow_push_to_main": false,
  "require_pr": true,
  "require_ci_green": true,
  "ci_timeout_seconds": 600
}
```

---

## Audit Trail

Every Fritz action is written to `tinker_audit.sqlite` (or the path in
`TINKER_AUDIT_LOG_PATH`). You can query it:

```bash
sqlite3 tinker_audit.sqlite "
  SELECT created_at, actor, resource, outcome, details
  FROM audit_events
  WHERE actor LIKE 'fritz%'
  ORDER BY created_at DESC
  LIMIT 10;
"
```

This shows you exactly what Fritz committed, when, and whether it succeeded.

---

## Module Layout

```
fritz/
├── __init__.py       # Public API: FritzAgent, FritzConfig
├── agent.py          # FritzAgent — top-level orchestrator (calls git_ops + platform)
├── config.py         # FritzConfig dataclass + PushPolicyConfig
├── credentials.py    # Token loading (env vars, keyring, .netrc)
├── git_ops.py        # Local git operations (commit, push, branch, tag)
├── github_ops.py     # GitHub API (PRs, merges, CI polling, releases)
├── gitea_ops.py      # Gitea API (same surface as github_ops)
├── identity.py       # Git committer identity (name, email, gpg signing)
├── metrics.py        # Prometheus counters for Fritz operations
├── platform.py       # Platform detection and dispatch (github vs gitea vs none)
├── push_policy.py    # Push policy enforcement
└── retry.py          # HTTP retry logic for API calls
```

---

## Common Issues

**"Cannot connect to remote"**
: Check that your `FRITZ_REMOTE_URL` is reachable from this machine.
  For Gitea: make sure port 3000 is open on the NAS.

**"CI never becomes green"**
: Set `require_ci_green: false` if your repo has no CI configured.
  Or increase `ci_timeout_seconds`.

**"Push rejected: protected branch"**
: Fritz respects `protected_branches`. Either push to a feature branch or
  remove the branch from the list.

**"git push cancelled by operator"**
: You (or someone) denied the confirmation gate. Check `TINKER_CONFIRM_BEFORE`
  in your `.env` — remove `git_push` from the list to stop gating pushes.
