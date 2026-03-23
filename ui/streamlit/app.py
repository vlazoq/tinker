"""
ui/streamlit/app.py  —  Tinker Streamlit Control Panel
───────────────────────────────────────────────────────
Run:  python -m tinker.ui.streamlit   (or: streamlit run tinker/ui/streamlit/app.py)

All tabs are in this single file using st.tabs(). Streamlit re-runs on every
widget interaction, which is fine here because all reads are fast local ops.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui.core import (  # noqa: E402
    ORCH_CONFIG_SCHEMA,
    STAGNATION_CONFIG_SCHEMA,
    FLAG_DEFAULTS,
    FLAG_DESCRIPTIONS,
    FLAG_GROUPS,
    TASK_TYPES,
    SUBSYSTEMS,
    AUDIT_DB,
    BACKUP_DIR,
    DLQ_DB,
    FLAGS_FILE,
    FRITZ_CONFIG_FILE,
    TASKS_DB,
    db_query_sync as dbq,
    db_execute_sync as dbe,
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

st.set_page_config(
    page_title="Tinker Web UI",
    page_icon="🔧",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
  #MainMenu, footer, header { visibility: hidden; }
  .block-container { padding-top: 1rem; }
  .stAlert { font-size: 0.85rem; }
</style>
""",
    unsafe_allow_html=True,
)

st.title("🔧 TINKER — Control Panel")


# ── Fritz async helpers (defined before use) ──────────────────────────────────

async def _async_ship(agent, message, task_id, auto_merge):
    await agent.setup()
    return await agent.commit_and_ship(
        message=message, task_id=task_id or "webui", auto_merge=auto_merge
    )


async def _async_push(agent, branch):
    await agent.setup()
    if branch:
        return await agent.git.push(branch)
    return await agent.git.push()


async def _async_pr(agent, title, body, head, base):
    await agent.setup()
    return await agent.create_pr(title=title, body=body, head=head, base=base)


# ─────────────────────────────────────────────────────────────────────────────

tabs = st.tabs(
    [
        "📊 Dashboard",
        "⚙️ Config",
        "🚩 Feature Flags",
        "📋 Task Queue",
        "💀 DLQ",
        "💾 Backups",
        "🤖 Grub",
        "🔀 Fritz",
        "📜 Audit Log",
    ]
)

# ── Dashboard ─────────────────────────────────────────────────────────────────
with tabs[0]:
    if st.button("↻ Refresh", key="dash_refresh"):
        st.rerun()

    state = load_state()
    if not state:
        st.warning("Orchestrator offline — `tinker_state.json` not found.")
    else:
        totals = state.get("totals", {})
        micro_hist = state.get("micro_history", [])
        last_critic = micro_hist[-1].get("critic_score") if micro_hist else None

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Micro Loops", totals.get("micro", "—"))
        c2.metric("Meso Loops", totals.get("meso", "—"))
        c3.metric("Macro Loops", totals.get("macro", "—"))
        c4.metric("Consecutive Failures", totals.get("consecutive_failures", 0))

        col_left, col_right = st.columns(2)
        with col_left:
            st.subheader("Loop Status")
            st.json(
                {
                    "status": state.get("status", "—"),
                    "current_level": state.get("current_level", "—"),
                    "current_task_id": state.get("current_task_id", "—"),
                    "current_subsystem": state.get("current_subsystem", "—"),
                    "last_critic_score": last_critic,
                    "uptime_min": round(state.get("uptime_seconds", 0) / 60, 1),
                }
            )

        with col_right:
            st.subheader("Subsystem Micro Counts")
            counts = state.get("subsystem_micro_counts", {})
            if counts:
                import pandas as pd

                st.bar_chart(pd.Series(counts))

# ── Config ────────────────────────────────────────────────────────────────────
with tabs[1]:
    st.info(
        "Saved to `tinker_webui_config.json`. **Restart the orchestrator to apply.**"
    )
    saved = load_config()
    if saved.get("_saved_at"):
        st.caption(f"Last saved: {saved['_saved_at']}")

    new_vals: dict = {}
    new_stag: dict = {}

    st.subheader("Orchestrator Config")
    for section_key, section in ORCH_CONFIG_SCHEMA.items():
        st.markdown(f"**{section['label']}**")
        cols = st.columns(min(len(section["fields"]), 3))
        for i, (field_name, meta) in enumerate(section["fields"].items()):
            with cols[i % len(cols)]:
                default = saved.get(field_name, meta["default"])
                help_txt = meta.get("help", "")
                if meta["type"] == "int":
                    new_vals[field_name] = st.number_input(
                        meta["label"],
                        value=int(default),
                        min_value=int(meta["min"]),
                        step=1,
                        help=help_txt,
                        key=f"cfg_{field_name}",
                    )
                else:
                    new_vals[field_name] = st.number_input(
                        meta["label"],
                        value=float(default),
                        min_value=float(meta["min"]),
                        step=0.1,
                        format="%.2f",
                        help=help_txt,
                        key=f"cfg_{field_name}",
                    )

    st.subheader("Anti-Stagnation Config")
    for section_key, section in STAGNATION_CONFIG_SCHEMA.items():
        st.markdown(f"**{section['label']}**")
        stag_saved = saved.get("stagnation", {}).get(section_key, {})
        new_stag[section_key] = {}
        cols = st.columns(min(len(section["fields"]), 3))
        for i, (field_name, meta) in enumerate(section["fields"].items()):
            with cols[i % len(cols)]:
                default = stag_saved.get(field_name, meta["default"])
                if meta["type"] == "int":
                    new_stag[section_key][field_name] = st.number_input(
                        meta["label"],
                        value=int(default),
                        min_value=int(meta["min"]),
                        step=1,
                        key=f"stag_{section_key}_{field_name}",
                    )
                else:
                    new_stag[section_key][field_name] = st.number_input(
                        meta["label"],
                        value=float(default),
                        min_value=float(meta["min"]),
                        step=0.1,
                        format="%.2f",
                        key=f"stag_{section_key}_{field_name}",
                    )

    if st.button("💾 Save Config", type="primary", key="cfg_save"):
        new_vals["stagnation"] = new_stag
        save_config(new_vals)
        st.success("Config saved. Restart the orchestrator to apply changes.")

# ── Feature Flags ─────────────────────────────────────────────────────────────
with tabs[2]:
    st.info(
        f"Writing to `{FLAGS_FILE}`. Orchestrator picks up changes within **30 seconds**."
    )
    current_flags = load_flags()
    new_flags: dict[str, bool] = {}

    for group_name, flag_names in FLAG_GROUPS.items():
        st.subheader(group_name)
        cols = st.columns(min(len(flag_names), 3))
        for i, flag in enumerate(flag_names):
            with cols[i % len(cols)]:
                new_flags[flag] = st.toggle(
                    flag,
                    value=current_flags.get(flag, FLAG_DEFAULTS.get(flag, False)),
                    help=FLAG_DESCRIPTIONS.get(flag, ""),
                    key=f"flag_{flag}",
                )

    if st.button("💾 Save Flags", type="primary", key="flags_save"):
        save_flags(new_flags)
        st.success("Flags saved. Orchestrator will pick up changes within 30 seconds.")

# ── Task Queue ────────────────────────────────────────────────────────────────
with tabs[3]:
    import pandas as pd

    stats_rows = (
        dbq(TASKS_DB, "SELECT status, COUNT(*) n FROM tasks GROUP BY status") or []
    )
    stats = {r["status"]: r["n"] for r in stats_rows}
    if stats:
        scols = st.columns(len(stats))
        for i, (s, n) in enumerate(stats.items()):
            scols[i].metric(s.upper(), n)

    if st.button("↻ Refresh", key="tasks_refresh"):
        st.rerun()

    rows = (
        dbq(
            TASKS_DB,
            "SELECT id, title, type, subsystem, status, priority_score, attempt_count, created_at "
            "FROM tasks ORDER BY priority_score DESC LIMIT 200",
        )
        or []
    )
    if rows:
        df = pd.DataFrame(rows)
        df["id"] = df["id"].str[:8] + "…"
        df["priority_score"] = df["priority_score"].round(3)
        st.dataframe(df, use_container_width=True)
    else:
        st.info("No tasks in database.")

    with st.expander("➕ Inject New Task"):
        inj_title = st.text_input("Title *", key="inj_title")
        inj_desc = st.text_area("Description", key="inj_desc")
        icol1, icol2 = st.columns(2)
        with icol1:
            inj_type = st.selectbox("Type", TASK_TYPES, key="inj_type")
            inj_gap = st.slider("Confidence Gap", 0.0, 1.0, 0.5, 0.05, key="inj_gap")
        with icol2:
            inj_sub = st.selectbox("Subsystem", SUBSYSTEMS, key="inj_sub")
            inj_explore = st.checkbox("Exploration task", key="inj_explore")

        if st.button("Inject Task", type="primary", key="inj_btn"):
            if not inj_title.strip():
                st.error("Title is required.")
            else:
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
                        inj_title.strip(),
                        inj_desc,
                        inj_type,
                        inj_sub,
                        inj_gap,
                        1 if inj_explore else 0,
                        ts,
                        ts,
                    ),
                )
                if ok:
                    st.success(f"Task `{tid[:8]}…` injected.")
                    st.rerun()
                else:
                    st.error("Injection failed — database not found.")

# ── DLQ ───────────────────────────────────────────────────────────────────────
with tabs[4]:
    if st.button("↻ Refresh", key="dlq_refresh"):
        st.rerun()

    dlq_rows = (
        dbq(
            DLQ_DB,
            "SELECT id, operation, error, status, retry_count, created_at, notes "
            "FROM dlq_items ORDER BY created_at DESC LIMIT 100",
        )
        or []
    )

    if dlq_rows:
        stats_rows2 = (
            dbq(DLQ_DB, "SELECT status, COUNT(*) n FROM dlq_items GROUP BY status")
            or []
        )
        s2 = {r["status"]: r["n"] for r in stats_rows2}
        sc1, sc2, sc3 = st.columns(3)
        sc1.metric("Pending", s2.get("pending", 0))
        sc2.metric("Resolved", s2.get("resolved", 0))
        sc3.metric("Discarded", s2.get("discarded", 0))

        dlq_df = pd.DataFrame(dlq_rows)
        dlq_df["id"] = dlq_df["id"].str[:8] + "…"
        dlq_df["error"] = dlq_df["error"].str[:80]
        st.dataframe(dlq_df, use_container_width=True)
    else:
        st.success("Queue is empty.")

    with st.expander("🔧 Mark Item"):
        dlq_id = st.text_input("Item ID (full UUID)", key="dlq_id")
        dlq_notes = st.text_input("Notes", key="dlq_notes")
        da1, da2 = st.columns(2)
        with da1:
            if st.button("✅ Mark Resolved", type="primary", key="dlq_resolve"):
                if dlq_id.strip():
                    ts = now_iso()
                    dbe(
                        DLQ_DB,
                        "UPDATE dlq_items SET status='resolved',resolved_at=?,updated_at=?,notes=? WHERE id=?",
                        (
                            ts,
                            ts,
                            dlq_notes or "Resolved via Streamlit UI",
                            dlq_id.strip(),
                        ),
                    )
                    st.success("Marked resolved.")
                    st.rerun()
        with da2:
            if st.button("🗑 Mark Discarded", key="dlq_discard"):
                if dlq_id.strip():
                    ts = now_iso()
                    dbe(
                        DLQ_DB,
                        "UPDATE dlq_items SET status='discarded',resolved_at=?,updated_at=?,notes=? WHERE id=?",
                        (
                            ts,
                            ts,
                            dlq_notes or "Discarded via Streamlit UI",
                            dlq_id.strip(),
                        ),
                    )
                    st.success("Marked discarded.")
                    st.rerun()

# ── Backups ───────────────────────────────────────────────────────────────────
with tabs[5]:
    if st.button("↻ Refresh", key="bk_refresh"):
        st.rerun()

    if st.button("➕ Trigger Backup", type="primary", key="bk_trigger"):
        trigger = BACKUP_DIR.parent / "tinker_backup_trigger"
        trigger.write_text(now_iso())
        st.success("Backup trigger written. BackupManager will pick it up shortly.")

    backups = list_backups()
    if backups:
        st.dataframe(pd.DataFrame(backups), use_container_width=True)
    else:
        st.info(f"No backups found in `{BACKUP_DIR}`.")

    st.caption(
        "Tinker backs up: DuckDB (artifacts), SQLite (tasks), ChromaDB (vectors)."
    )

# ── Grub ──────────────────────────────────────────────────────────────────────
with tabs[6]:
    if st.button("↻ Refresh", key="grub_refresh"):
        st.rerun()

    status = fetch_grub_status_sync()
    # task_counts: {type_str: {status_str: count}}
    task_counts = status.get("task_counts", {})
    queue_counts = status.get("queue_counts", {})
    artifacts = status.get("artifacts", [])

    st.subheader("Tinker tasks (implementation + review)")
    # Flatten nested {type: {status: count}} into a list of metrics
    flat_counts = [
        (f"{t}/{s}", n) for t, sm in task_counts.items() for s, n in sm.items()
    ]
    if flat_counts:
        tcols = st.columns(min(len(flat_counts), 4))
        for i, (label, count) in enumerate(flat_counts):
            tcols[i % 4].metric(label, count)
    else:
        st.info("No implementation or review tasks found.")

    st.subheader("Grub queue")
    if queue_counts:
        qcols = st.columns(min(len(queue_counts), 4))
        for i, (label, count) in enumerate(queue_counts.items()):
            qcols[i % 4].metric(label, count)
    elif not status.get("queue_db_exists"):
        st.info("Grub has not started yet — `grub_queue.sqlite` not found.")

    impl_rows = (
        dbq(
            TASKS_DB,
            "SELECT id, title, type, subsystem, status, priority_score, attempt_count, updated_at "
            "FROM tasks WHERE type IN ('implementation','review') "
            "ORDER BY updated_at DESC LIMIT 100",
        )
        or []
    )
    if impl_rows:
        import pandas as pd

        idf = pd.DataFrame(impl_rows)
        idf["id"] = idf["id"].str[:8] + "…"
        idf["priority_score"] = idf["priority_score"].round(3)
        st.dataframe(idf, use_container_width=True)

    st.subheader("Recent implementation artifacts")
    arts_dir = status.get("artifacts_dir", "./grub_artifacts")
    if artifacts:
        for a in artifacts:
            size_kb = round(a.get("size_bytes", 0) / 1024, 1)
            score = f"  score={a['score']:.2f}" if a.get("score") is not None else ""
            label = f"{a['name']} ({size_kb} KB){score}"
            with st.expander(label):
                st.caption(f"Modified: {a.get('mtime', '')[:19]}")
                if a.get("subsystem"):
                    st.caption(f"Subsystem: {a['subsystem']}")
    else:
        st.info(f"No artifacts yet. Grub writes to `{arts_dir}` when tasks complete.")


# ── Fritz ─────────────────────────────────────────────────────────────────────
with tabs[7]:
    import asyncio as _asyncio

    fritz_col1, fritz_col2 = st.columns([1, 1])

    with fritz_col1:
        if st.button("↻ Refresh", key="fritz_refresh"):
            st.rerun()

    fritz_status = fetch_fritz_status_sync()
    git_info = fritz_status.get("git", {})
    pp = fritz_status.get("push_policy", {})

    if not fritz_status.get("config_exists"):
        st.warning("`fritz_config.json` not found — showing defaults. Run Fritz once to generate a config file.")

    # ── Status summary ────────────────────────────────────────────────────────
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Branch", git_info.get("branch") or "—")
    m2.metric("HEAD SHA", git_info.get("sha") or "—")
    m3.metric("Working tree", "✓ clean" if git_info.get("clean") else "⚠ dirty")
    m4.metric("Identity", fritz_status.get("identity_mode", "—"))

    pc1, pc2 = st.columns(2)
    with pc1:
        st.markdown("**Platforms**")
        gh_enabled = fritz_status.get("github_enabled", False)
        gt_enabled = fritz_status.get("gitea_enabled", False)
        gh_target = fritz_status.get("github_owner", "") + "/" + fritz_status.get("github_repo", "")
        gt_target = fritz_status.get("gitea_base_url", "")
        st.markdown(
            f"- GitHub: {'✅' if gh_enabled else '❌'} {gh_target.strip('/')}\n"
            f"- Gitea:  {'✅' if gt_enabled else '❌'} {gt_target}"
        )
    with pc2:
        st.markdown("**Push Policy**")
        st.markdown(
            f"- push to main: `{pp.get('allow_push_to_main', False)}`\n"
            f"- require PR: `{pp.get('require_pr', True)}`\n"
            f"- require CI: `{pp.get('require_ci_green', True)}`\n"
            f"- merge method: `{pp.get('auto_merge_method', 'squash')}`"
        )

    remotes = git_info.get("remotes", [])
    if remotes:
        with st.expander("Remotes"):
            for r in remotes:
                st.code(r, language=None)

    git_changes = git_info.get("status", "")
    if git_changes:
        with st.expander("Uncommitted changes"):
            st.code(git_changes, language=None)

    st.divider()

    # ── Commit & Ship ─────────────────────────────────────────────────────────
    with st.expander("🚀 Commit & Ship", expanded=True):
        ship_msg = st.text_input(
            "Commit message",
            placeholder="fix: correct off-by-one in parser",
            key="fritz_ship_msg",
        )
        ship_task = st.text_input("Task ID (optional)", placeholder="grub-abc123", key="fritz_ship_task")
        ship_auto_merge = st.checkbox("Auto-merge PR (if policy allows)", key="fritz_auto_merge")

        if st.button("⚡ Commit & Ship", key="fritz_ship_btn", disabled=not ship_msg.strip()):
            try:
                from fritz.config import FritzConfig
                from fritz.agent import FritzAgent

                config = (
                    FritzConfig.from_file(FRITZ_CONFIG_FILE)
                    if FRITZ_CONFIG_FILE.exists()
                    else FritzConfig()
                )
                agent = FritzAgent(config)
                result = _asyncio.run(_async_ship(agent, ship_msg, ship_task, ship_auto_merge))
                if result.ok:
                    st.success(
                        f"✓ {'Merged' if result.merged else 'PR created' if result.pr_url else 'Pushed'}"
                        + (f" — branch: `{result.branch}`" if result.branch else "")
                        + (f" — SHA: `{result.commit_sha}`" if result.commit_sha else "")
                        + (f"\n\nPR: {result.pr_url}" if result.pr_url else "")
                    )
                else:
                    st.error("✗ " + "; ".join(result.errors or ["Unknown error"]))
            except Exception as exc:
                st.error(f"Fritz error: {exc}")

    # ── Push branch ───────────────────────────────────────────────────────────
    with st.expander("⬆ Push Branch"):
        push_br = st.text_input(
            "Branch (leave blank for current)",
            placeholder=git_info.get("branch", "main"),
            key="fritz_push_branch",
        )
        if st.button("⬆ Push", key="fritz_push_btn"):
            try:
                from fritz.config import FritzConfig
                from fritz.agent import FritzAgent

                config = (
                    FritzConfig.from_file(FRITZ_CONFIG_FILE)
                    if FRITZ_CONFIG_FILE.exists()
                    else FritzConfig()
                )
                agent = FritzAgent(config)
                res = _asyncio.run(_async_push(agent, push_br or None))
                if res.ok:
                    st.success(f"Pushed {push_br or 'current branch'} successfully.")
                else:
                    st.error(f"Push failed: {res.stderr}")
            except Exception as exc:
                st.error(f"Fritz error: {exc}")

    # ── Create PR ─────────────────────────────────────────────────────────────
    with st.expander("🔀 Create Pull Request"):
        pr_c1, pr_c2 = st.columns(2)
        with pr_c1:
            pr_title  = st.text_input("Title", placeholder="fix: correct off-by-one", key="fritz_pr_title")
            pr_head   = st.text_input("Head branch", placeholder=git_info.get("branch", "feature/xyz"), key="fritz_pr_head")
        with pr_c2:
            pr_base   = st.text_input("Base branch", placeholder="main", key="fritz_pr_base")
            pr_body   = st.text_area("Description", placeholder="Describe what this PR does…", key="fritz_pr_body", height=80)
        if st.button("🔀 Create PR", key="fritz_pr_btn", disabled=not (pr_title.strip() and pr_head.strip())):
            try:
                from fritz.config import FritzConfig
                from fritz.agent import FritzAgent

                config = (
                    FritzConfig.from_file(FRITZ_CONFIG_FILE)
                    if FRITZ_CONFIG_FILE.exists()
                    else FritzConfig()
                )
                agent = FritzAgent(config)
                res = _asyncio.run(_async_pr(agent, pr_title, pr_body, pr_head, pr_base or None))
                if res.ok:
                    st.success(f"PR created: {res.url or '(no URL returned)'}")
                else:
                    st.error(f"PR failed: {res.error}")
            except Exception as exc:
                st.error(f"Fritz error: {exc}")


# ── Audit Log ─────────────────────────────────────────────────────────────────
with tabs[8]:
    evt_types = [
        r["event_type"]
        for r in dbq(
            AUDIT_DB, "SELECT DISTINCT event_type FROM audit_events ORDER BY event_type"
        )
        or []
    ]

    fcol1, fcol2, fcol3 = st.columns([2, 2, 1])
    with fcol1:
        filter_evt = st.selectbox("Event Type", [""] + evt_types, key="aud_evt")
    with fcol2:
        filter_actor = st.text_input(
            "Actor", placeholder="e.g. micro_loop", key="aud_actor"
        )
    with fcol3:
        audit_limit = st.number_input("Limit", 10, 200, 50, 10, key="aud_limit")

    conds, params = [], []
    if filter_evt:
        conds.append("event_type = ?")
        params.append(filter_evt)
    if filter_actor:
        conds.append("actor = ?")
        params.append(filter_actor)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    audit_rows = (
        dbq(
            AUDIT_DB,
            f"SELECT event_type, actor, resource, outcome, trace_id, created_at "
            f"FROM audit_events {where} ORDER BY created_at DESC LIMIT ?",
            tuple(params) + (int(audit_limit),),
        )
        or []
    )

    if audit_rows:
        st.dataframe(pd.DataFrame(audit_rows), use_container_width=True)
    else:
        st.info("No audit events found.")

    if st.button("↻ Refresh", key="aud_refresh"):
        st.rerun()
