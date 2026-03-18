# requirements/ — Dependency Management

Tinker uses a **three-tier dependency strategy** that is standard at
production-grade Python shops (Google, Stripe, Shopify, etc.).

---

## File Overview

| File | Format | Purpose |
|------|--------|---------|
| `pyproject.toml` | `>=` ranges | Developer-facing: describes what Tinker *needs* |
| `requirements/*.in` | `>=` ranges | Source files compiled into `.txt` lock files |
| `requirements/base.txt` | `==` pins + SHA-256 hashes | Production / CI: exact reproducible install with hash verification |
| `requirements/dev.txt` | `==` pins + SHA-256 hashes | Development: same as base + test runners and tooling |
| `requirements/metrics.txt` | `==` pins + SHA-256 hashes | Optional Prometheus metrics endpoint |

The `.in` files are the **source of truth** for what Tinker needs.
The `.txt` files are **generated** by `pip-compile --generate-hashes`;
never edit them by hand.

---

## The Three-Tier Strategy

### Tier 1 — `pyproject.toml` (loose ranges, intent)

```toml
dependencies = [
    "aiohttp>=3.9.0",
    "redis>=5.0.0",
]
```

Expresses what Tinker *needs*.  Allows `pip install -e ".[dev]"` to work
on any developer's machine without a lock file.

### Tier 2 — `requirements/*.txt` (exact pins, reproducibility)

```
aiohttp==3.13.3
redis==7.3.0
```

Every transitive dependency is pinned to an exact version.
CI, staging, and production all run byte-for-byte identical code.

### Tier 3 — SHA-256 hashes (supply-chain security)

```
aiohttp==3.13.3 \
    --hash=sha256:abc123def456... \
    --hash=sha256:789abcdef012...
```

When a lock file contains `--hash=sha256:…` lines, **pip enforces those
hashes automatically** — no extra flag needed at install time.  If the
downloaded wheel or sdist doesn't match the committed hash, pip refuses
to install it.

This protects against:
- A compromised PyPI account uploading a backdoored version
- A CDN serving a tampered wheel
- A typosquatting package (`aiohttp` vs `aiohttpx`)

---

## SHA-256 Hash Verification in Practice

```bash
# Enforces hashes automatically (they are in the lock file):
pip install -r requirements/base.txt

# pip-audit also enforces hashes when auditing:
pip-audit --requirement requirements/base.txt --require-hashes
```

If a hash doesn't match, pip shows:

```
ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.
aiohttp from https://…:
    Expected sha256 abc123...
    Got        sha256 DIFFERENT...
```

This is the final defence against a supply-chain attack sneaking a
malicious package into the environment.

---

## Generating / Updating Lock Files

```bash
# Regenerate all three lock files at once (recommended):
make deps-all

# Or individually:
make deps          # → requirements/base.txt
make deps-dev      # → requirements/dev.txt
make deps-metrics  # → requirements/metrics.txt
```

All `make deps*` targets run `pip-compile --generate-hashes --upgrade`,
which:
1. Resolves the latest versions compatible with the `>=` constraints in the `.in` file
2. Downloads metadata to compute SHA-256 hashes for every wheel and sdist
3. Writes the new pinned + hashed lock file

**Always commit the updated lock files immediately after regenerating:**

```bash
make deps-all
git add requirements/
git commit -m "chore: update dependency lock files"
```

CI rejects any PR where the committed `.txt` files differ from what
`pip-compile --generate-hashes` would produce for the same `.in` sources.

---

## CVE Scanning with pip-audit

```bash
make audit          # scan all lock files for known CVEs; exit 1 on any finding
make audit-fix      # automatically upgrade vulnerable packages
```

`pip-audit` queries the [OSV database](https://osv.dev/) and the
[GitHub Advisory Database](https://github.com/advisories).

Example — clean output:

```
Name  Version  ID  Fix Versions
----- -------- --- ------------
No known vulnerabilities found
✓ no CVEs found in any lock file
```

Example — vulnerable package found:

```
Name     Version  ID                  Fix Versions
-------- -------- ------------------- ------------
requests 2.25.1   GHSA-j8r2-6x86-q33q 2.31.0
```

Fix: update `requests>=2.31.0` in `requirements/base.in`, then `make deps`.

**CI policy:** the `security-audit` job blocks PR merges on any CVE finding.

---

## Automated Updates — Dependabot

`.github/dependabot.yml` opens pull requests weekly for dependency updates:

- **Patch and minor** updates are batched into one PR per week.
- **Security patches** trigger an immediate PR regardless of schedule.
- **Major** version bumps come as separate PRs (they may contain breaking changes).

Each Dependabot PR updates both the `.in` source and the `.txt` lock file
so CI passes without manual intervention.

---

## First-Time Hash Migration

If the committed lock files were generated **without** `--generate-hashes`
(missing `--hash=sha256:…` lines), CI will fail with:

```
❌  requirements/base.txt is STALE or MISSING HASHES
```

One-time fix:

```bash
make deps-all                  # adds hashes to all three lock files
make audit                     # verify no CVEs before committing
git add requirements/
git commit -m "chore: add SHA-256 hash pins to all lock files (supply-chain security)"
```

After this commit, every future install and CI run has cryptographic
supply-chain verification.

---

## File Map

```
requirements/
├── base.in        ← EDIT THIS to add/remove/update production deps
├── base.txt       ← AUTO-GENERATED (commit; contains SHA-256 hashes)
├── dev.in         ← EDIT THIS to add/remove/update dev-only deps
├── dev.txt        ← AUTO-GENERATED (commit; contains SHA-256 hashes)
├── metrics.in     ← EDIT THIS to add/remove metrics deps
├── metrics.txt    ← AUTO-GENERATED (commit; contains SHA-256 hashes)
└── README.md      ← you are here
```

---

## Decision Log

| Decision | Rationale |
|----------|-----------|
| `pip-compile` not `pip freeze` | `pip freeze` captures the *current env*; pip-compile resolves from intent (`.in`) so fresh environments don't need a prior install |
| `--generate-hashes` | Protects against compromised wheels on PyPI or CDN; required by the `security-audit` CI job and `make audit` |
| `--strip-extras` | Keeps lock files portable across pip-tools versions; extras are re-applied at install time |
| `--upgrade` in `make deps*` | Always resolves the newest compatible versions when a developer regenerates; Dependabot handles week-to-week automation |
| `pip-audit` not `safety` | pip-audit is OSV-backed, free for commercial use, exits non-zero on findings; `safety` requires a paid subscription for the full database |
| Dependabot not Renovate | Native GitHub integration; no additional service credentials or configuration service needed |
