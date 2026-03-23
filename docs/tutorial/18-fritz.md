# Chapter 18 — Fritz: Git and Remote Platform Integration

## The Problem

After Grub writes tested, reviewed code to disk, the files just sit there.
Someone still needs to:

1. Run `git add` and `git commit`
2. Create a feature branch
3. Open a Pull Request
4. Wait for CI to pass
5. Merge the PR
6. Clean up the branch

If you have 20 tasks per hour, doing that manually destroys the autonomy that
Tinker + Grub were designed to provide.

That's Fritz's job.

---

## What Fritz Is

Fritz is Tinker's Git and remote platform integration layer.

It gives the Tinker/Grub system the same version-control capabilities that
Claude Code has — committing, opening PRs, waiting for CI, merging — plus
extras that Claude Code's single-session model doesn't need:

- **Dedicated bot account**: keeps AI commits separated from human commits
- **Multi-remote push**: push to GitHub and a self-hosted Gitea simultaneously
- **Configurable push policy**: require CI, forbid pushing to protected branches
- **Structured audit trail**: SQLite log of every git and remote action

---

## Fritz in the Full Pipeline

```
Tinker (design)
    │  creates tinker_artifacts/billing.md
    ▼
Grub (code)
    │  writes billing/invoice_repository.py
    │  writes tests/test_invoice_repository.py
    │  ReviewerMinion score: 0.87 ✓
    ▼
Fritz (ship)
    │  git add billing/ tests/
    │  git commit -m "feat: implement InvoiceRepository [tinker: grub-abc123]"
    │  git push origin feature/grub-abc123
    │  Opens PR: "Implement InvoiceRepository"
    │  Polls CI...  ✓ green after 2 minutes
    │  Merges PR (squash)
    │  Deletes feature/grub-abc123
    ▼
Done.  Next Grub task starts.
```

---

## Module Tour

The Fritz package lives in `fritz/`:

```
fritz/
├── agent.py       ← The main class you call: FritzAgent
├── config.py      ← FritzConfig + PushPolicyConfig dataclasses
├── git_ops.py     ← Local git (commit, push, branch, tag)
├── github_ops.py  ← GitHub API (PRs, CI, merges, releases)
├── gitea_ops.py   ← Gitea API (identical surface, self-hosted)
├── platform.py    ← Detects platform and dispatches to the right ops module
├── credentials.py ← Loads tokens from env, keyring, or .netrc
├── identity.py    ← Configures git committer name and email
├── push_policy.py ← Decides whether a given push is allowed
├── retry.py       ← HTTP retry with exponential backoff
└── metrics.py     ← Prometheus counters for Fritz operations
```

The key class is `FritzAgent`. You only interact with two methods in normal use:

```python
from fritz import FritzAgent, FritzConfig

config = FritzConfig.from_file("fritz_config.json")
fritz  = FritzAgent(config)

# The main method: commit files and ship (PR, CI, merge)
await fritz.commit_and_ship(
    files=["billing/invoice_repository.py"],
    message="feat: implement InvoiceRepository",
    task_id="grub-abc123",
)

# Low-level: just commit locally without any remote action
await fritz.git_ops.commit("billing/invoice_repository.py", "feat: ...")
```

---

## Building FritzAgent

Let's build a minimal Fritz integration step by step.

### Step 1: The git ops layer

`FritzGitOps` wraps the `git` CLI via `subprocess` and runs everything in a
thread pool (via `asyncio.to_thread`) so it doesn't block the event loop.

```python
from fritz.config import FritzConfig
from fritz.identity import FritzIdentity
from fritz.git_ops import FritzGitOps

config   = FritzConfig(repo_path=".")
identity = FritzIdentity(name="Tinker Bot", email="tinker@localhost")
git_ops  = FritzGitOps(config, identity)

# Commit all staged changes
result = await git_ops.commit(
    message="feat: add invoice repository",
    files=["billing/invoice_repository.py"],
)
print(result.ok, result.stdout)

# Push to origin
result = await git_ops.push(branch="feature/billing", remote="origin")
print(result)  # [git:push origin feature/billing] OK — ...
```

Every method returns a `FritzGitResult`:
```python
@dataclass
class FritzGitResult:
    ok: bool          # True if git returned exit code 0
    operation: str    # e.g. "push origin feature/billing"
    stdout: str
    stderr: str
    returncode: int
```

### Step 2: The push policy

`PushPolicy` enforces rules before `FritzGitOps.push()` runs:

```python
from fritz.push_policy import PushPolicy
from fritz.config import PushPolicyConfig

policy = PushPolicy(PushPolicyConfig(
    allow_push_to_main=False,
    require_pr=True,
    require_ci_green=True,
    protected_branches=["production", "release"],
))

# Check before pushing
check = policy.check("feature/billing", "origin")
if check.allowed:
    await git_ops.push(branch="feature/billing")
else:
    print(f"Push blocked: {check.reason}")
```

### Step 3: The platform layer

`FritzPlatform` wraps the GitHub or Gitea API:

```python
from fritz.platform import create_platform

platform = create_platform(config)  # detects github/gitea from config.platform

pr = await platform.open_pr(
    title="Implement InvoiceRepository",
    body="Auto-generated by Grub. Task: grub-abc123.",
    head_branch="feature/billing",
    base_branch="main",
)
print(pr.url)   # https://github.com/yourname/repo/pull/42
print(pr.number)  # 42

# Poll CI
ci_ok = await platform.wait_for_ci(pr.number, timeout=600)

# Merge
await platform.merge_pr(pr.number, method="squash")
```

### Step 4: FritzAgent ties it all together

`FritzAgent` combines git_ops + push_policy + platform into a single call:

```python
agent = FritzAgent(config)

await agent.commit_and_ship(
    files=["billing/invoice_repository.py"],
    message="feat: implement InvoiceRepository",
    task_id="grub-abc123",
)
```

Internally, this does:

```python
async def commit_and_ship(self, files, message, task_id):
    # 1. Local commit
    await self.git_ops.commit(message=message, files=files)

    # 2. Create feature branch
    branch = f"feature/grub-{task_id[:8]}"
    await self.git_ops.create_branch(branch)

    # 3. Check push policy
    check = self.push_policy.check(branch)
    if not check.allowed:
        return FritzResult(ok=False, reason=check.reason)

    # 4. Push
    await self.git_ops.push(branch=branch)

    # 5. Open PR (if platform is configured)
    if self.platform:
        pr = await self.platform.open_pr(...)
        await self.platform.wait_for_ci(pr.number)
        await self.platform.merge_pr(pr.number)
```

---

## Configuration Deep Dive

### Local git only (no remote platform)

```json
{
  "repo_path": ".",
  "platform": "none",
  "committer_name": "Tinker Bot",
  "committer_email": "tinker@localhost"
}
```

Fritz will commit and push but won't try to open PRs. Good for a local
development loop where you just want automatic commits.

### GitHub with a bot account

Create a GitHub account called `tinker-bot` (or similar) and generate a
Personal Access Token with `repo` scope.

```json
{
  "repo_path": ".",
  "platform": "github",
  "remote_url": "https://github.com/yourname/yourrepo",
  "bot_token": "ghp_xxxxxxxxxxxxxxxxxxxx",
  "committer_name": "Tinker Bot",
  "committer_email": "tinker-bot@users.noreply.github.com",
  "push_policy": {
    "allow_push_to_main": false,
    "require_pr": true,
    "require_ci_green": true,
    "auto_merge_method": "squash",
    "ci_timeout_seconds": 600
  }
}
```

Set `FRITZ_BOT_TOKEN` in your `.env` instead of hardcoding it in JSON.

### Gitea (home lab, on the NAS)

Gitea is a lightweight self-hosted Git platform — think GitHub without the
cloud. It runs comfortably on a Raspberry Pi or NAS.

```json
{
  "repo_path": ".",
  "platform": "gitea",
  "remote_url": "http://nas:3000/yourname/yourrepo",
  "bot_token": "your-gitea-api-token",
  "committer_name": "Tinker Bot",
  "committer_email": "tinker@nas.local"
}
```

Generate a Gitea API token at: `http://nas:3000/user/settings/applications`

---

## Confirmation Gate Integration

Fritz has a slot for a `ConfirmationGate` that, when set, pauses before every
push and asks for human approval:

```python
from orchestrator.confirmation import ConfirmationGate

# Wire the gate into Fritz
fritz.git_ops.confirmation_gate = orchestrator.confirmation_gate
```

Then any push will go through the gate (if `git_push` is in your
`TINKER_CONFIRM_BEFORE` list). See Chapter 19 for the full confirmation gate
documentation.

---

## Audit Trail

Every Fritz action writes to the structured audit log. Query it:

```bash
sqlite3 tinker_audit.sqlite "
  SELECT created_at, outcome, details
  FROM audit_events
  WHERE resource LIKE '%fritz%' OR resource LIKE '%git%'
  ORDER BY created_at DESC LIMIT 20;
"
```

Example output:
```
2025-01-15 14:32:10|started|{"operation":"commit","files":["billing/invoice_repository.py"]}
2025-01-15 14:32:11|started|{"operation":"push","branch":"feature/billing","remote":"origin"}
2025-01-15 14:32:15|completed|{"pr_url":"https://github.com/.../pull/42","pr_number":42}
2025-01-15 14:34:22|completed|{"merged":true,"sha":"abc123","method":"squash"}
```

---

## What We Built

```
fritz/
├── FritzGitOps   — async git CLI wrapper (commit, push, branch, tag, status)
│   └── .confirmation_gate  — optional human approval before push
├── FritzPlatform — GitHub or Gitea API (PRs, CI polling, merges)
│   ├── GitHubOps  — github_ops.py
│   └── GiteaOps   — gitea_ops.py
├── PushPolicy    — enforces protected branches, PR requirement, CI gate
├── FritzAgent    — high-level orchestrator: commit_and_ship()
└── FritzConfig   — all settings in one dataclass, env-var-backed
```

### Next Steps

- `fritz/README.md` — full configuration reference + common issues
- Chapter 19 — Confirmation gates, checkpointing, MCP, and TINKER.md
- Chapter 17 — How Grub creates tasks that Fritz ships (the complete loop)
