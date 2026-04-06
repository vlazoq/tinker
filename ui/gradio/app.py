"""
ui/gradio/app.py
────────────────
Gradio UI for Tinker. Run with:  python -m tinker.ui.gradio

Tabs: Dashboard · Config · Feature Flags · Task Queue · DLQ · Backups · Audit Log

NOTE: Gradio re-runs the entire block on each interaction, so all reads
are done inside event handlers (not at module import time).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make tinker root importable
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gradio as gr

from ui.core import (
    AUDIT_DB,
    BACKUP_DIR,
    DLQ_DB,
    FLAG_DEFAULTS,
    FLAG_DESCRIPTIONS,
    FLAG_GROUPS,
    FLAGS_FILE,
    FRITZ_CONFIG_FILE,
    ORCH_CONFIG_SCHEMA,
    STAGNATION_CONFIG_SCHEMA,
    SUBSYSTEMS,
    TASK_TYPES,
    TASKS_DB,
    fetch_fritz_status_sync,
    fetch_grub_status_sync,
    list_backups,
    load_config,
    load_flags,
    load_state,
    new_id,
    now_iso,
    save_config,
    save_flags,
)
from ui.core import (
    db_execute_sync as dbe,
)
from ui.core import (
    db_query_sync as dbq,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _health_md() -> str:
    state = load_state()
    if not state:
        return "**Orchestrator offline** — `tinker_state.json` not found."
    totals = state.get("totals", {})
    micro_hist = state.get("micro_history", [])
    last_critic = micro_hist[-1].get("critic_score") if micro_hist else "—"
    status_str = state.get("status", "—")
    uptime_min = round(state.get("uptime_seconds", 0) / 60, 1)
    lines = [
        "### System Health",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Status | **{status_str}** |",
        f"| Current Level | {state.get('current_level', '—')} |",
        f"| Micro Loops | **{totals.get('micro', '—')}** |",
        f"| Meso Loops  | **{totals.get('meso', '—')}** |",
        f"| Macro Loops | **{totals.get('macro', '—')}** |",
        f"| Stagnation Events | {totals.get('stagnation_events', '—')} |",
        f"| Consecutive Failures | {totals.get('consecutive_failures', '—')} |",
        f"| Current Task | `{state.get('current_task_id', '—')}` |",
        f"| Current Subsystem | {state.get('current_subsystem', '—')} |",
        f"| Last Critic Score | {last_critic} |",
        f"| Uptime | {uptime_min} min |",
    ]
    return "\n".join(lines)


def _tasks_df():
    import pandas as pd

    rows = dbq(
        TASKS_DB,
        "SELECT id, title, type, subsystem, status, priority_score, attempt_count, created_at "
        "FROM tasks ORDER BY priority_score DESC LIMIT 200",
    )
    if not rows:
        return pd.DataFrame(
            columns=[
                "id",
                "title",
                "type",
                "subsystem",
                "status",
                "priority",
                "attempts",
                "created_at",
            ]
        )
    df = pd.DataFrame(rows)
    df["id"] = df["id"].str[:8] + "…"
    df["priority_score"] = df["priority_score"].round(3)
    return df


def _tasks_stats() -> str:
    rows = dbq(TASKS_DB, "SELECT status, COUNT(*) as n FROM tasks GROUP BY status")
    if not rows:
        return "No tasks found."
    return "  ".join(f"**{r['status']}**: {r['n']}" for r in rows)


def _dlq_df():
    import pandas as pd

    rows = dbq(
        DLQ_DB,
        "SELECT id, operation, error, status, retry_count, created_at, notes "
        "FROM dlq_items ORDER BY created_at DESC LIMIT 100",
    )
    if not rows:
        return pd.DataFrame(
            columns=[
                "id",
                "operation",
                "error",
                "status",
                "retry_count",
                "created_at",
                "notes",
            ]
        )
    df = pd.DataFrame(rows)
    df["id"] = df["id"].str[:8] + "…"
    df["error"] = df["error"].str[:80]
    return df


def _audit_df(event_type="", actor="", limit=50):
    import pandas as pd

    conds, params = [], []
    if event_type:
        conds.append("event_type = ?")
        params.append(event_type)
    if actor:
        conds.append("actor = ?")
        params.append(actor)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = dbq(
        AUDIT_DB,
        f"SELECT event_type, actor, resource, outcome, trace_id, created_at "
        f"FROM audit_events {where} ORDER BY created_at DESC LIMIT ?",
        (*tuple(params), limit),
    )
    if not rows:
        return pd.DataFrame(
            columns=[
                "event_type",
                "actor",
                "resource",
                "outcome",
                "trace_id",
                "created_at",
            ]
        )
    return pd.DataFrame(rows)


def _backup_df():
    import pandas as pd

    bs = list_backups()
    if not bs:
        return pd.DataFrame(columns=["id", "created_at", "size_mb", "file_count", "errors"])
    return pd.DataFrame(bs)


def _grub_md() -> str:
    status = fetch_grub_status_sync()
    # task_counts: {type_str: {status_str: count}}
    task_counts = status.get("task_counts", {})
    queue_counts = status.get("queue_counts", {})
    artifacts = status.get("artifacts", [])

    lines = [
        "### Grub — Implementation Pipeline",
        "",
        "**Task Counts (implementation + review)**",
        "| Type | Status | Count |",
        "|------|--------|-------|",
    ]
    for task_type, status_map in task_counts.items():
        for task_status, count in status_map.items():
            lines.append(f"| {task_type} | {task_status} | {count} |")
    if not task_counts:
        lines.append("| — | — | 0 |")

    lines += [
        "",
        "**Queue Counts**",
        "| Status | Count |",
        "|--------|-------|",
    ]
    for k, v in queue_counts.items():
        lines.append(f"| {k} | {v} |")
    if not queue_counts and not status.get("queue_db_exists"):
        lines.append("| — | Grub not yet started |")

    lines += ["", "**Recent Artifacts**"]
    if artifacts:
        for a in artifacts:
            size_kb = round(a.get("size_bytes", 0) / 1024, 1)
            score = f"  score={a['score']:.2f}" if a.get("score") is not None else ""
            lines.append(f"- **{a['name']}** ({size_kb} KB){score} — {a.get('mtime', '')[:19]}")
    else:
        arts_dir = status.get("artifacts_dir", "./grub_artifacts")
        lines.append(f"_No artifacts yet. Grub writes to `{arts_dir}` when tasks complete._")

    return "\n".join(lines)


def _grub_impl_df():
    import pandas as pd

    rows = dbq(
        TASKS_DB,
        "SELECT id, title, type, subsystem, status, priority_score, attempt_count, updated_at "
        "FROM tasks WHERE type IN ('implementation','review') "
        "ORDER BY updated_at DESC LIMIT 100",
    )
    if not rows:
        return pd.DataFrame(
            columns=[
                "id",
                "title",
                "type",
                "subsystem",
                "status",
                "priority",
                "attempts",
                "updated_at",
            ]
        )
    df = pd.DataFrame(rows)
    df["id"] = df["id"].str[:8] + "…"
    df["priority_score"] = df["priority_score"].round(3)
    return df


def _fritz_md() -> str:
    """Return a Markdown summary of Fritz config + live git state."""
    s = fetch_fritz_status_sync()
    git = s.get("git", {})
    pp = s.get("push_policy", {})
    lines = [
        "### Fritz — Git Integration",
        "",
    ]
    if not s.get("config_exists"):
        lines.append("> `fritz_config.json` not found — showing defaults.")
        lines.append("")

    lines += [
        f"**Branch:** `{git.get('branch') or '—'}`  "
        f"**SHA:** `{git.get('sha') or '—'}`  "
        f"**Tree:** {'Clean' if git.get('clean') else 'Dirty'}",
        f"**Identity:** {s.get('identity_mode', '—')}  "
        f"({s.get('git_name', '')} `{s.get('git_email', '')}`)",
        "",
        "### Platforms",
    ]
    gh = "enabled" if s.get("github_enabled") else "disabled"
    gt = "enabled" if s.get("gitea_enabled") else "disabled"
    gh_target = f"{s.get('github_owner', '')}/{s.get('github_repo', '')}".strip("/")
    gt_url = s.get("gitea_base_url", "")
    lines += [
        f"- GitHub {gh} {gh_target}",
        f"- Gitea  {gt} {gt_url}",
        "",
        "### Push Policy",
        "| Setting | Value |",
        "|---------|-------|",
        f"| allow_push_to_main | `{pp.get('allow_push_to_main', False)}` |",
        f"| require_pr | `{pp.get('require_pr', True)}` |",
        f"| require_ci_green | `{pp.get('require_ci_green', True)}` |",
        f"| auto_merge_method | `{pp.get('auto_merge_method', 'squash')}` |",
    ]

    remotes = git.get("remotes", [])
    if remotes:
        lines += ["", "### Remotes"]
        for r in remotes:
            lines.append(f"- `{r}`")

    git_changes = git.get("status", "")
    if git_changes:
        lines += ["", "### Uncommitted changes", f"```\n{git_changes}\n```"]

    return "\n".join(lines)


async def _fritz_ship_async(message: str, task_id: str, auto_merge: bool) -> str:
    """Async helper for commit-and-ship; returns a status string."""
    from agents.fritz.agent import FritzAgent
    from agents.fritz.config import FritzConfig

    config = (
        FritzConfig.from_file(FRITZ_CONFIG_FILE) if FRITZ_CONFIG_FILE.exists() else FritzConfig()
    )
    agent = FritzAgent(config)
    await agent.setup()
    result = await agent.commit_and_ship(
        message=message, task_id=task_id or "gradio", auto_merge=auto_merge
    )
    if result.ok:
        parts = ["✓ Success"]
        if result.branch:
            parts.append(f"branch={result.branch}")
        if result.commit_sha:
            parts.append(f"sha={result.commit_sha}")
        if result.pr_url:
            parts.append(f"pr={result.pr_url}")
        if result.merged:
            parts.append("merged=true")
        return "  ".join(parts)
    return "✗ " + "; ".join(result.errors or ["Unknown error"])


async def _fritz_push_async(branch: str) -> str:
    from agents.fritz.agent import FritzAgent
    from agents.fritz.config import FritzConfig

    config = (
        FritzConfig.from_file(FRITZ_CONFIG_FILE) if FRITZ_CONFIG_FILE.exists() else FritzConfig()
    )
    agent = FritzAgent(config)
    await agent.setup()
    res = await agent.git.push(branch or None)
    return "✓ Pushed successfully." if res.ok else f"✗ {res.stderr}"


async def _fritz_pr_async(title: str, body: str, head: str, base: str) -> str:
    from agents.fritz.agent import FritzAgent
    from agents.fritz.config import FritzConfig

    config = (
        FritzConfig.from_file(FRITZ_CONFIG_FILE) if FRITZ_CONFIG_FILE.exists() else FritzConfig()
    )
    agent = FritzAgent(config)
    await agent.setup()
    res = await agent.create_pr(title=title, body=body, head=head, base=base or None)
    return f"✓ PR created: {res.url}" if res.ok else f"✗ {res.error}"


async def _fritz_verify_async() -> str:
    from agents.fritz.agent import FritzAgent
    from agents.fritz.config import FritzConfig

    config = (
        FritzConfig.from_file(FRITZ_CONFIG_FILE) if FRITZ_CONFIG_FILE.exists() else FritzConfig()
    )
    agent = FritzAgent(config)
    await agent.setup()
    results = await agent.verify_connections()
    parts = [f"{p}: {'✓' if ok else '✗'}" for p, ok in results.items()]
    return "  |  ".join(parts) if parts else "No platforms enabled."


def _run_async(coro):
    """
    Run an async coroutine from a synchronous Gradio callback.

    Gradio runs callbacks in a thread pool where there is no running event loop,
    so asyncio.run() is always safe here.  We avoid the ThreadPoolExecutor
    approach (running asyncio.run inside a thread) because that pattern can
    deadlock when the outer loop holds resources the inner loop needs.
    """
    import asyncio

    return asyncio.run(coro)


# ── App builder ───────────────────────────────────────────────────────────────


def build_app() -> gr.Blocks:
    with gr.Blocks(
        title="Tinker Control Panel",
        theme=gr.themes.Base(
            primary_hue="blue",
            secondary_hue="blue",
            neutral_hue="slate",
            font=("Inter", "system-ui", "sans-serif"),
            font_mono=("JetBrains Mono", "SF Mono", "monospace"),
        ),
        css="""
        .gradio-container {
            max-width: 1280px !important;
            margin: 0 auto !important;
        }
        footer { display: none !important; }
        .dark .gr-button-secondary {
            border-color: #30363d !important;
        }
        .dark .gr-panel {
            border-color: #30363d !important;
        }
        .dark .gr-form {
            border-color: #30363d !important;
        }
        .dark .gr-input, .dark .gr-text-input {
            border-color: #30363d !important;
        }
        .dark .gr-input:focus, .dark .gr-text-input:focus {
            border-color: #58a6ff !important;
            box-shadow: 0 0 0 3px rgba(88,166,255,.12) !important;
        }
        .dark .gr-box {
            border-color: #30363d !important;
        }
        .gr-group {
            margin-bottom: 1rem !important;
        }
        """,
    ) as demo:
        gr.Markdown("# TINKER — Control Panel")

        with gr.Tabs():
            # ── Dashboard ────────────────────────────────────────────────────
            with gr.Tab("Dashboard"):
                dash_md = gr.Markdown(_health_md())
                dash_btn = gr.Button("Refresh", variant="secondary", size="sm")
                dash_btn.click(fn=_health_md, outputs=dash_md)
                # Auto-refresh: Gradio 4.x supports `every` on event handlers
                # to poll the dashboard status periodically.
                try:
                    dash_md.change(fn=_health_md, outputs=dash_md, every=10)
                except TypeError:
                    pass  # Older Gradio versions don't support `every`

            # ── Config ───────────────────────────────────────────────────────
            with gr.Tab("Config"):
                gr.Markdown(
                    "Changes are saved to `tinker_webui_config.json`. Restart the orchestrator to apply."
                )
                saved = load_config()

                orch_inputs: dict[str, gr.Number] = {}
                with gr.Group():
                    gr.Markdown("### Orchestrator Config")
                    for _section_key, section in ORCH_CONFIG_SCHEMA.items():
                        gr.Markdown(f"**{section['label']}**")
                        with gr.Row():
                            for field_name, meta in section["fields"].items():
                                orch_inputs[field_name] = gr.Number(
                                    label=meta["label"],
                                    value=saved.get(field_name, meta["default"]),
                                    info=meta.get("help", ""),
                                    precision=0 if meta["type"] == "int" else 2,
                                )

                stag_inputs: dict[str, dict[str, gr.Number]] = {}
                with gr.Group():
                    gr.Markdown("### Anti-Stagnation Config")
                    for section_key, section in STAGNATION_CONFIG_SCHEMA.items():
                        gr.Markdown(f"**{section['label']}**")
                        stag_inputs[section_key] = {}
                        stag_saved = saved.get("stagnation", {}).get(section_key, {})
                        with gr.Row():
                            for field_name, meta in section["fields"].items():
                                stag_inputs[section_key][field_name] = gr.Number(
                                    label=meta["label"],
                                    value=stag_saved.get(field_name, meta["default"]),
                                    precision=0 if meta["type"] == "int" else 2,
                                )

                cfg_save_btn = gr.Button("Save Config", variant="primary")
                cfg_msg = gr.Markdown("")

                def save_all_config(*args):
                    """Collect all Number widget values and save."""
                    # args order: orch fields (in schema order), then stagnation fields
                    orch_field_order = [
                        fn for s in ORCH_CONFIG_SCHEMA.values() for fn in s["fields"]
                    ]
                    stag_field_order = [
                        (sk, fn)
                        for sk, s in STAGNATION_CONFIG_SCHEMA.items()
                        for fn in s["fields"]
                    ]

                    idx = 0
                    data: dict = {}
                    for fn in orch_field_order:
                        meta = next(
                            m
                            for s in ORCH_CONFIG_SCHEMA.values()
                            for k, m in s["fields"].items()
                            if k == fn
                        )
                        val = args[idx]
                        idx += 1
                        data[fn] = int(val) if meta["type"] == "int" else float(val)

                    stag_data: dict = {}
                    for sk, fn in stag_field_order:
                        meta = STAGNATION_CONFIG_SCHEMA[sk]["fields"][fn]
                        val = args[idx]
                        idx += 1
                        stag_data.setdefault(sk, {})[fn] = (
                            int(val) if meta["type"] == "int" else float(val)
                        )

                    data["stagnation"] = stag_data
                    save_config(data)
                    return "**Config saved.** Restart the orchestrator to apply changes."

                all_inputs = list(orch_inputs.values()) + [
                    w for s in stag_inputs.values() for w in s.values()
                ]
                cfg_save_btn.click(fn=save_all_config, inputs=all_inputs, outputs=cfg_msg)

            # ── Feature Flags ─────────────────────────────────────────────────
            with gr.Tab("Flags"):
                gr.Markdown(
                    f"Writes to `{FLAGS_FILE}`. Orchestrator picks up changes within **30 seconds**."
                )
                flags_msg = gr.Markdown("")

                flag_widgets: dict[str, gr.Checkbox] = {}
                current_flags = load_flags()

                for group_name, flag_names in FLAG_GROUPS.items():
                    with gr.Group():
                        gr.Markdown(f"**{group_name}**")
                        with gr.Row():
                            for flag in flag_names:
                                flag_widgets[flag] = gr.Checkbox(
                                    label=f"{flag}",
                                    value=current_flags.get(flag, FLAG_DEFAULTS.get(flag, False)),
                                    info=FLAG_DESCRIPTIONS.get(flag, ""),
                                )

                flags_save_btn = gr.Button("Save Flags", variant="primary")

                def save_all_flags(*args):
                    flag_names_ordered = [f for g in FLAG_GROUPS.values() for f in g]
                    flags = {fn: bool(args[i]) for i, fn in enumerate(flag_names_ordered)}
                    save_flags(flags)
                    return (
                        "**Flags saved.** Orchestrator will pick up changes within 30 seconds."
                    )

                all_flag_widgets = [flag_widgets[f] for g in FLAG_GROUPS.values() for f in g]
                flags_save_btn.click(fn=save_all_flags, inputs=all_flag_widgets, outputs=flags_msg)

            # ── Task Queue ────────────────────────────────────────────────────
            with gr.Tab("Tasks"):
                tasks_stats_md = gr.Markdown(_tasks_stats())
                tasks_df_out = gr.DataFrame(
                    _tasks_df(), label="Tasks (top 200 by priority)", interactive=False
                )

                with gr.Row():
                    tasks_refresh_btn = gr.Button("Refresh", variant="secondary", size="sm")

                with gr.Accordion("Inject New Task", open=False):
                    inj_title = gr.Textbox(
                        label="Title *", placeholder="e.g. Research caching strategies"
                    )
                    inj_desc = gr.Textbox(label="Description", lines=3)
                    inj_type = gr.Dropdown(TASK_TYPES, value="design", label="Type")
                    inj_sub = gr.Dropdown(SUBSYSTEMS, value="cross_cutting", label="Subsystem")
                    inj_gap = gr.Slider(0, 1, value=0.5, step=0.05, label="Confidence Gap")
                    inj_explore = gr.Checkbox(label="Exploration task", value=False)
                    inj_btn = gr.Button("Inject", variant="primary")
                    inj_msg = gr.Markdown("")

                def inject_task(title, desc, typ, sub, gap, explore):
                    if not title.strip():
                        return "**Error:** Title is required.", _tasks_df(), _tasks_stats()
                    tid = new_id()
                    ts = now_iso()
                    ok = dbe(
                        TASKS_DB,
                        "INSERT INTO tasks "
                        "(id,title,description,type,subsystem,status,confidence_gap,"
                        "is_exploration,created_at,updated_at,priority_score,"
                        "staleness_hours,dependency_depth,last_subsystem_work_hours,"
                        "attempt_count,dependencies,outputs,tags,metadata) "
                        "VALUES (?,?,?,?,?,'pending',?,?,?,?,0.5,0.0,0,0.0,0,'[]','[]','[]','{}')",
                        (
                            tid,
                            title.strip(),
                            desc,
                            typ,
                            sub,
                            gap,
                            1 if explore else 0,
                            ts,
                            ts,
                        ),
                    )
                    msg = (
                        f"Task `{tid[:8]}...` injected."
                        if ok
                        else "**Error:** Injection failed (DB not found)."
                    )
                    return msg, _tasks_df(), _tasks_stats()

                inj_btn.click(
                    inject_task,
                    inputs=[
                        inj_title,
                        inj_desc,
                        inj_type,
                        inj_sub,
                        inj_gap,
                        inj_explore,
                    ],
                    outputs=[inj_msg, tasks_df_out, tasks_stats_md],
                )

                tasks_refresh_btn.click(
                    fn=lambda: (_tasks_df(), _tasks_stats()),
                    outputs=[tasks_df_out, tasks_stats_md],
                )

            # ── DLQ ──────────────────────────────────────────────────────────
            with gr.Tab("DLQ"):
                dlq_df_out = gr.DataFrame(
                    _dlq_df(), label="DLQ Items (pending first)", interactive=False
                )
                dlq_refresh = gr.Button("Refresh", variant="secondary", size="sm")
                dlq_msg = gr.Markdown("")

                with gr.Row():
                    dlq_item_id = gr.Textbox(
                        label="Item ID (full UUID)", placeholder="Paste full item ID"
                    )
                    dlq_notes = gr.Textbox(label="Notes", placeholder="Resolution reason…")
                with gr.Row():
                    dlq_resolve = gr.Button("Mark Resolved", variant="primary")
                    dlq_discard = gr.Button("Mark Discarded", variant="stop")

                def dlq_action(action, item_id, notes):
                    if not item_id.strip():
                        return "**Error:** Paste the full item ID first.", _dlq_df()
                    ts = now_iso()
                    ok = dbe(
                        DLQ_DB,
                        f"UPDATE dlq_items SET status='{action}', resolved_at=?, updated_at=?, notes=? WHERE id=?",
                        (
                            ts,
                            ts,
                            notes or f"{action.title()} via Gradio UI",
                            item_id.strip(),
                        ),
                    )
                    msg = (
                        f"Item `{item_id[:8]}...` marked **{action}**."
                        if ok
                        else "**Error:** Update failed."
                    )
                    return msg, _dlq_df()

                dlq_resolve.click(
                    lambda id, n: dlq_action("resolved", id, n),
                    inputs=[dlq_item_id, dlq_notes],
                    outputs=[dlq_msg, dlq_df_out],
                )
                dlq_discard.click(
                    lambda id, n: dlq_action("discarded", id, n),
                    inputs=[dlq_item_id, dlq_notes],
                    outputs=[dlq_msg, dlq_df_out],
                )
                dlq_refresh.click(fn=_dlq_df, outputs=dlq_df_out)

            # ── Backups ───────────────────────────────────────────────────────
            with gr.Tab("Backups"):
                backup_df_out = gr.DataFrame(
                    _backup_df(), label="Available Backups", interactive=False
                )
                backup_refresh = gr.Button("Refresh", variant="secondary", size="sm")
                backup_trigger = gr.Button("+ Create Backup", variant="primary")
                backup_msg = gr.Markdown("")

                def trigger_backup():
                    trigger = BACKUP_DIR.parent / "tinker_backup_trigger"
                    trigger.write_text(now_iso())
                    return (
                        "**Backup trigger written.** BackupManager will pick it up shortly.",
                        _backup_df(),
                    )

                backup_trigger.click(fn=trigger_backup, outputs=[backup_msg, backup_df_out])
                backup_refresh.click(fn=_backup_df, outputs=backup_df_out)

            # ── Grub ──────────────────────────────────────────────────────────
            with gr.Tab("Grub"):
                grub_md_out = gr.Markdown(_grub_md())
                grub_df_out = gr.DataFrame(
                    _grub_impl_df(),
                    label="Implementation & Review tasks (last 100)",
                    interactive=False,
                )
                grub_refresh = gr.Button("Refresh", variant="secondary", size="sm")
                grub_refresh.click(
                    fn=lambda: (_grub_md(), _grub_impl_df()),
                    outputs=[grub_md_out, grub_df_out],
                )

            # ── Fritz ─────────────────────────────────────────────────────────
            with gr.Tab("Fritz"):
                fritz_md_out = gr.Markdown(_fritz_md())
                fritz_result_out = gr.Textbox(label="Result", interactive=False, lines=2)
                fritz_refresh_btn = gr.Button("Refresh", variant="secondary", size="sm")
                fritz_refresh_btn.click(fn=_fritz_md, outputs=fritz_md_out)

                with gr.Row():
                    fritz_verify_btn = gr.Button(
                        "Verify Connections", variant="secondary", size="sm"
                    )
                fritz_verify_btn.click(
                    fn=lambda: _run_async(_fritz_verify_async()),
                    outputs=fritz_result_out,
                )

                gr.Markdown("### Commit & Ship")
                with gr.Row():
                    fritz_msg = gr.Textbox(
                        label="Commit message",
                        placeholder="fix: correct off-by-one in parser",
                        scale=3,
                    )
                    fritz_task = gr.Textbox(
                        label="Task ID (optional)", placeholder="grub-abc123", scale=1
                    )
                fritz_auto_merge = gr.Checkbox(
                    label="Auto-merge PR (if policy allows)", value=False
                )
                fritz_ship_btn = gr.Button("Commit & Ship", variant="primary")
                fritz_ship_btn.click(
                    fn=lambda msg, tid, am: _run_async(_fritz_ship_async(msg, tid, am)),
                    inputs=[fritz_msg, fritz_task, fritz_auto_merge],
                    outputs=fritz_result_out,
                )

                gr.Markdown("### Push Branch")
                with gr.Row():
                    fritz_push_branch = gr.Textbox(
                        label="Branch (leave blank for current)",
                        placeholder="main",
                        scale=2,
                    )
                    fritz_push_btn = gr.Button("Push", variant="primary", scale=1)
                fritz_push_btn.click(
                    fn=lambda b: _run_async(_fritz_push_async(b)),
                    inputs=fritz_push_branch,
                    outputs=fritz_result_out,
                )

                gr.Markdown("### Create Pull Request")
                with gr.Row():
                    fritz_pr_title = gr.Textbox(label="Title", placeholder="fix: …", scale=3)
                    fritz_pr_head = gr.Textbox(
                        label="Head branch", placeholder="feature/xyz", scale=1
                    )
                    fritz_pr_base = gr.Textbox(label="Base branch", placeholder="main", scale=1)
                fritz_pr_body = gr.Textbox(
                    label="Description", placeholder="Describe what this PR does…", lines=3
                )
                fritz_pr_btn = gr.Button("Create PR", variant="primary")
                fritz_pr_btn.click(
                    fn=lambda t, b, h, bs: _run_async(_fritz_pr_async(t, b, h, bs)),
                    inputs=[fritz_pr_title, fritz_pr_body, fritz_pr_head, fritz_pr_base],
                    outputs=fritz_result_out,
                )

            # ── Audit Log ─────────────────────────────────────────────────────
            with gr.Tab("Audit"):
                audit_event_types = [
                    r["event_type"]
                    for r in dbq(
                        AUDIT_DB,
                        "SELECT DISTINCT event_type FROM audit_events ORDER BY event_type",
                    )
                    or []
                ]

                with gr.Row():
                    audit_evt_filter = gr.Dropdown(
                        ["", *audit_event_types], value="", label="Event Type"
                    )
                    audit_actor_filter = gr.Textbox(label="Actor", placeholder="e.g. micro_loop")
                    audit_limit = gr.Slider(10, 200, value=50, step=10, label="Limit")
                audit_search_btn = gr.Button("Search", variant="primary")
                audit_df_out = gr.DataFrame(_audit_df(), label="Audit Events", interactive=False)

                audit_search_btn.click(
                    fn=_audit_df,
                    inputs=[audit_evt_filter, audit_actor_filter, audit_limit],
                    outputs=audit_df_out,
                )

    return demo
