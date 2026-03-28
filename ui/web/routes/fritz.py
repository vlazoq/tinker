"""Fritz (Git/VCS agent) endpoints: status, ship, push, PR, verify, diffs."""

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ui.core import BASE_DIR, FRITZ_CONFIG_FILE, fetch_fritz_status

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/fritz/status")
async def api_fritz_status():
    """Return Fritz config + live git state (branch, SHA, dirty files, remotes)."""
    return await fetch_fritz_status()


@router.post("/api/fritz/ship")
async def api_fritz_ship(request: Request):
    """Run Fritz commit-and-ship pipeline."""
    body = await request.json()
    try:
        from agents.fritz.config import FritzConfig
        from agents.fritz.agent import FritzAgent

        config = (
            FritzConfig.from_file(FRITZ_CONFIG_FILE)
            if FRITZ_CONFIG_FILE.exists()
            else FritzConfig()
        )
        agent = FritzAgent(config)
        await agent.setup()
        result = await agent.commit_and_ship(
            message=body.get("message", "chore: automated commit by Fritz"),
            task_id=body.get("task_id", "webui"),
            task_description=body.get("task_description", ""),
            auto_merge=bool(body.get("auto_merge", False)),
        )
        return {
            "ok": result.ok,
            "branch": result.branch,
            "commit_sha": result.commit_sha,
            "pr_url": result.pr_url,
            "pr_number": result.pr_number,
            "merged": result.merged,
            "direct_push": result.direct_push,
            "errors": result.errors,
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/api/fritz/push")
async def api_fritz_push(request: Request):
    """Push the current (or specified) branch."""
    body = await request.json()
    try:
        from agents.fritz.config import FritzConfig
        from agents.fritz.agent import FritzAgent

        config = (
            FritzConfig.from_file(FRITZ_CONFIG_FILE)
            if FRITZ_CONFIG_FILE.exists()
            else FritzConfig()
        )
        agent = FritzAgent(config)
        await agent.setup()
        branch = body.get("branch") or await agent.git.current_branch()
        result = await agent.push(branch=branch)
        return {"ok": result.ok, "branch": branch, "error": result.stderr}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/api/fritz/pr")
async def api_fritz_create_pr(request: Request):
    """Create a pull request on GitHub or Gitea."""
    body = await request.json()
    try:
        from agents.fritz.config import FritzConfig
        from agents.fritz.agent import FritzAgent

        config = (
            FritzConfig.from_file(FRITZ_CONFIG_FILE)
            if FRITZ_CONFIG_FILE.exists()
            else FritzConfig()
        )
        agent = FritzAgent(config)
        await agent.setup()
        result = await agent.create_pr(
            title=body.get("title", ""),
            body=body.get("body", ""),
            head=body.get("head", ""),
            base=body.get("base"),
            platform=body.get("platform", "auto"),
        )
        return {"ok": result.ok, "url": result.url, "error": result.error, "data": result.data}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.get("/api/fritz/verify")
async def api_fritz_verify():
    """Test GitHub and Gitea credentials."""
    try:
        from agents.fritz.config import FritzConfig
        from agents.fritz.agent import FritzAgent

        config = (
            FritzConfig.from_file(FRITZ_CONFIG_FILE)
            if FRITZ_CONFIG_FILE.exists()
            else FritzConfig()
        )
        agent = FritzAgent(config)
        await agent.setup()
        results = await agent.verify_connections()
        return {"ok": True, "connections": results}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.get("/api/fritz/recent-diffs")
async def api_fritz_recent_diffs(limit: int = 5):
    """Return the last N commits with their diffs."""
    limit = max(1, min(limit, 25))

    workspace = str(BASE_DIR)
    try:
        cfg_path = FRITZ_CONFIG_FILE
        if cfg_path.exists():
            with open(cfg_path, "r") as f:
                fritz_cfg = json.load(f)
            workspace = fritz_cfg.get("repo_path", workspace)
    except Exception:
        pass

    # Verify git repo
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--is-inside-work-tree",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return JSONResponse(
                {"error": "Not a git repository", "detail": f"'{workspace}' is not inside a git work tree."},
                status_code=400,
            )
    except (asyncio.TimeoutError, FileNotFoundError) as exc:
        return JSONResponse(
            {"error": "Git not available", "detail": str(exc)},
            status_code=500,
        )

    # Fetch last N commits
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "log", f"-{limit}", "--format=%H|%aI|%s",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (asyncio.TimeoutError, Exception) as exc:
        return JSONResponse(
            {"error": "git log failed", "detail": str(exc)},
            status_code=500,
        )

    lines = stdout.decode().strip().splitlines()
    if not lines or not lines[0]:
        return {"commits": [], "workspace": workspace}

    commits: list[dict[str, str]] = []
    for line in lines:
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        sha, timestamp, message = parts

        try:
            diff_proc = await asyncio.create_subprocess_exec(
                "git", "diff", f"{sha}~1..{sha}", "--stat", "--patch",
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            diff_out, _ = await asyncio.wait_for(diff_proc.communicate(), timeout=15)
            diff_text = diff_out.decode(errors="replace")
        except Exception:
            try:
                show_proc = await asyncio.create_subprocess_exec(
                    "git", "show", sha, "--format=", "--stat", "--patch",
                    cwd=workspace,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                show_out, _ = await asyncio.wait_for(show_proc.communicate(), timeout=15)
                diff_text = show_out.decode(errors="replace")
            except Exception as exc:
                logger.debug("git show fallback failed for %s: %s", sha, exc)
                diff_text = "(diff unavailable)"

        max_diff_chars = 50_000
        if len(diff_text) > max_diff_chars:
            diff_text = diff_text[:max_diff_chars] + "\n\n... (diff truncated) ..."

        commits.append({
            "sha": sha,
            "message": message,
            "diff": diff_text,
            "timestamp": timestamp,
        })

    return {"commits": commits, "workspace": workspace}
