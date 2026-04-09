"""
Human Judge review endpoints.

Provides the web API for human-in-the-loop quality control:

  GET  /api/reviews/pending       — list proposals awaiting human review
  GET  /api/reviews/{review_id}   — get a single pending review with full context
  POST /api/reviews/{review_id}   — submit a human review (score + feedback + directive)
  POST /api/judge-mode            — switch judge mode at runtime
  POST /api/request-review        — trigger on-demand human review for next loop
  GET  /api/directives            — list all active sticky directives
  DELETE /api/directives/{index}  — remove a sticky directive by index
  DELETE /api/directives          — clear all sticky directives
"""

import json
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ui.core import load_state

router = APIRouter()

# Directory where review responses are written (read by orchestrator).
_REVIEW_DIR = Path(os.getenv("TINKER_REVIEW_DIR", "./tinker_reviews"))


@router.get("/api/reviews/pending")
async def api_reviews_pending():
    """Return all pending human review requests from the state file."""
    state = load_state()
    reviews = state.get("pending_reviews", [])
    pending = [r for r in reviews if r.get("status") == "pending"]
    return {
        "pending": pending,
        "count": len(pending),
    }


@router.get("/api/reviews/{review_id}")
async def api_review_detail(review_id: str):
    """Return a single pending review with full context."""
    state = load_state()
    reviews = state.get("pending_reviews", [])
    for review in reviews:
        if review.get("id") == review_id:
            return {"ok": True, "review": review}
    return JSONResponse(
        {"ok": False, "error": f"Review {review_id} not found"},
        status_code=404,
    )


@router.post("/api/reviews/{review_id}")
async def api_submit_review(review_id: str, request: Request):
    """Submit a human review for a pending proposal.

    Body JSON:
        {
            "score": 0.0-1.0,       (required)
            "feedback": "...",       (optional, free-text review)
            "directive": "...",      (optional, steering instruction for Architect)
            "sticky": false          (optional, make directive persist across loops)
        }
    """
    # Sanitize review_id to prevent path traversal
    safe_name = Path(review_id).name
    if not safe_name or safe_name in (".", ".."):
        return JSONResponse({"ok": False, "error": "Invalid review_id"}, status_code=400)
    review_id = safe_name

    body = await request.json()

    # Validate score
    score = body.get("score")
    if score is None:
        return JSONResponse(
            {"ok": False, "error": "score is required (float 0.0-1.0)"},
            status_code=400,
        )
    try:
        score = max(0.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "score must be a number between 0.0 and 1.0"},
            status_code=400,
        )

    response_data = {
        "review_id": review_id,
        "score": score,
        "feedback": body.get("feedback", ""),
        "directive": body.get("directive"),
        "sticky": bool(body.get("sticky", False)),
    }

    # Write response file for the orchestrator to pick up.
    # The HumanJudge in the orchestrator process reads this via resolve().
    # For direct in-process wiring (when web UI and orchestrator share a
    # process), the app startup hook calls human_judge.resolve() directly.
    _REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    response_path = _REVIEW_DIR / f"{review_id}.json"

    fd, tmp = tempfile.mkstemp(dir=str(_REVIEW_DIR), suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(response_data, f)
        os.replace(tmp, str(response_path))
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # Try direct in-process resolution if orchestrator is available.
    # This is the fast path when web UI and orchestrator share a process.
    try:
        from ui.web.app import app

        orch = getattr(app.state, "orchestrator", None)
        if orch is not None and hasattr(orch, "human_judge") and orch.human_judge is not None:
            orch.human_judge.resolve(review_id, response_data)
    except Exception:
        pass  # Fall back to file-based resolution

    return {
        "ok": True,
        "review_id": review_id,
        "score": score,
        "has_directive": bool(response_data.get("directive")),
    }


@router.post("/api/judge-mode")
async def api_set_judge_mode(request: Request):
    """Switch the judge mode at runtime.

    Body JSON:
        {"mode": "llm" | "human" | "hybrid" | "on_demand"}
    """
    body = await request.json()
    mode = body.get("mode", "")

    valid_modes = ("llm", "human", "hybrid", "on_demand")
    if mode not in valid_modes:
        return JSONResponse(
            {"ok": False, "error": f"mode must be one of {valid_modes}"},
            status_code=400,
        )

    # Write control file for orchestrator to pick up
    control_dir = Path(os.getenv("TINKER_CONTROL_DIR", "./tinker_control"))
    control_dir.mkdir(parents=True, exist_ok=True)
    ctrl_path = control_dir / "judge_mode.json"

    fd, tmp = tempfile.mkstemp(dir=str(control_dir), suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"judge_mode": mode}, f)
        os.replace(tmp, str(ctrl_path))
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # Try direct in-process update
    try:
        from ui.web.app import app

        orch = getattr(app.state, "orchestrator", None)
        if orch is not None:
            orch.config.judge_mode = mode
            # Create or remove HumanJudge based on new mode
            if mode != "llm" and orch.human_judge is None:
                from agents.human_judge import HumanJudge

                orch.human_judge = HumanJudge(orch.config, orch.state, orch.event_bus)
            elif mode == "llm":
                orch.human_judge = None
    except Exception:
        pass

    return {"ok": True, "mode": mode, "message": f"Judge mode set to '{mode}'."}


@router.post("/api/request-review")
async def api_request_review():
    """Trigger a human review on the next micro loop (on_demand mode).

    When the orchestrator's judge_mode is "on_demand", this sets a flag
    that causes the next micro loop to pause for human review.  Also works
    in "hybrid" mode as an immediate override.
    """
    # Write control file
    control_dir = Path(os.getenv("TINKER_CONTROL_DIR", "./tinker_control"))
    control_dir.mkdir(parents=True, exist_ok=True)
    ctrl_path = control_dir / "request_review.json"

    fd, tmp = tempfile.mkstemp(dir=str(control_dir), suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"request_review": True}, f)
        os.replace(tmp, str(ctrl_path))
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # Try direct in-process update
    try:
        from ui.web.app import app

        orch = getattr(app.state, "orchestrator", None)
        if orch is not None:
            orch.state.human_review_requested = True
    except Exception:
        pass

    return {
        "ok": True,
        "message": "Human review requested for next micro loop.",
    }


@router.get("/api/directives")
async def api_list_directives():
    """List all active sticky human directives."""
    try:
        from ui.web.app import app

        orch = getattr(app.state, "orchestrator", None)
        if orch is not None and hasattr(orch, "human_judge") and orch.human_judge is not None:
            return {
                "directives": orch.human_judge.sticky_directives,
                "count": len(orch.human_judge.sticky_directives),
            }
    except Exception:
        pass

    return {"directives": [], "count": 0, "note": "Orchestrator not available"}


@router.delete("/api/directives/{index}")
async def api_delete_directive(index: int):
    """Remove a sticky directive by index."""
    try:
        from ui.web.app import app

        orch = getattr(app.state, "orchestrator", None)
        if orch is not None and hasattr(orch, "human_judge") and orch.human_judge is not None:
            if orch.human_judge.clear_sticky_directive(index):
                return {"ok": True, "message": f"Directive {index} removed."}
            return JSONResponse(
                {"ok": False, "error": f"Index {index} out of range"},
                status_code=404,
            )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return JSONResponse(
        {"ok": False, "error": "Orchestrator not available"},
        status_code=503,
    )


@router.delete("/api/directives")
async def api_clear_all_directives():
    """Clear all sticky directives."""
    try:
        from ui.web.app import app

        orch = getattr(app.state, "orchestrator", None)
        if orch is not None and hasattr(orch, "human_judge") and orch.human_judge is not None:
            count = orch.human_judge.clear_all_sticky_directives()
            return {"ok": True, "cleared": count}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return JSONResponse(
        {"ok": False, "error": "Orchestrator not available"},
        status_code=503,
    )
