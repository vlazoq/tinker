# Security Review Skill

Security checklist for code review. Flag any of these issues.

## Input Validation
- [ ] All user/external input is validated before use
- [ ] Numeric inputs have min/max bounds checks
- [ ] String inputs have length limits
- [ ] File paths are validated — no path traversal (`../../etc/passwd`)
- [ ] URLs are validated before fetching

```python
# Path traversal example — BAD
filepath = user_input   # user passes "../../etc/passwd"
content = open(filepath).read()   # DANGEROUS

# Safe version
base = Path("/safe/directory").resolve()
target = (base / user_input).resolve()
if not str(target).startswith(str(base)):
    raise ValueError("Path traversal attempt detected")
```

## SQL Injection
Never build SQL strings with string formatting:

```python
# DANGEROUS — SQL injection
query = f"SELECT * FROM users WHERE name = '{user_input}'"
cursor.execute(query)

# SAFE — parameterised query
cursor.execute("SELECT * FROM users WHERE name = ?", (user_input,))
```

## Command Injection
Never pass user input to shell commands:

```python
# DANGEROUS — shell injection
subprocess.run(f"ls {user_input}", shell=True)   # user passes "; rm -rf /"

# SAFE — use list form, never shell=True with user input
subprocess.run(["ls", user_input])   # shell metacharacters are NOT interpreted
```

## Secrets Management
- [ ] No hardcoded passwords, API keys, or tokens in source code
- [ ] Secrets come from environment variables or a secrets manager
- [ ] No secrets in log messages, error messages, or responses
- [ ] No secrets committed to git (check .gitignore)

```python
# BAD
API_KEY = "sk-abc123..."

# GOOD
API_KEY = os.getenv("MY_API_KEY")
if not API_KEY:
    raise ValueError("MY_API_KEY environment variable is not set")
```

## Authentication & Authorization
- [ ] Authenticated routes check the token/session on every request
- [ ] Authorization checks: can THIS user do THIS action on THIS resource?
- [ ] Sensitive actions require re-authentication
- [ ] Session tokens are not logged

## Dependency Security
- [ ] No known-vulnerable package versions
- [ ] Minimal dependencies (each dependency is a potential attack surface)
- [ ] Pinned versions in requirements.txt

## Logging Security
- [ ] No passwords, tokens, or PII in log messages
- [ ] Error messages don't leak internal paths, stack traces, or DB queries to users

```python
# BAD — leaks internal details to caller
except Exception as exc:
    return {"error": str(exc)}   # might include DB schema, file paths, etc.

# GOOD — log internally, return safe message to caller
except Exception as exc:
    logger.error("DB query failed: %s", exc)   # full detail in logs
    return {"error": "Internal error. Please try again."}
```

## File Operations
- [ ] File uploads: check file type (don't trust extension — check magic bytes)
- [ ] File downloads: sanitise the filename in Content-Disposition header
- [ ] Temporary files are cleaned up in finally blocks
- [ ] File permissions are set appropriately (don't make config files world-readable)

## Network Security
- [ ] HTTPS is used for all external API calls
- [ ] SSL certificates are verified (never `verify=False` in production)
- [ ] Timeouts are set on all HTTP calls (prevent slow-response DoS)
- [ ] Sensitive data is not sent in URL parameters (use POST body instead)
