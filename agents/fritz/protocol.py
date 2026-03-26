"""
agents/fritz/protocol.py
========================

Structural protocol (interface) for VCS integration agents.

Why this protocol exists
------------------------
The three Tinker UI layers (Textual TUI, FastAPI web, Gradio, Streamlit) all
need to trigger git/GitHub operations, but they currently import FritzAgent
directly::

    from agents.fritz.agent import FritzAgent   # concrete — hard to swap

That tight coupling means:

  * Swapping Fritz for a different backend (GitLab, bare git, Gitea-only) would
    require touching every UI file.
  * Unit-testing UI code that calls Fritz requires a full FritzAgent instance
    (or extensive mocking of its internals).

``VCSAgentProtocol`` breaks that coupling.  UI code should type-hint against
the protocol and instantiate the concrete class only in bootstrap/factory code::

    # In UI handlers — depend on the protocol:
    async def _run_ship(vcs: VCSAgentProtocol, ...) -> dict: ...

    # In bootstrap/factory — create the concrete instance:
    from agents.fritz.agent import FritzAgent
    vcs_agent: VCSAgentProtocol = FritzAgent(config)

Runtime verification
--------------------
Because the protocol is decorated with ``@runtime_checkable``, you can verify
that a concrete class satisfies it without running any methods::

    assert isinstance(fritz_instance, VCSAgentProtocol)   # True

Implementing classes
--------------------
* ``agents/fritz/agent.py :: FritzAgent``  — GitHub + Gitea + bare git
* Any future GitLab, Bitbucket, or plain-git-only implementation.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class VCSAgentProtocol(Protocol):
    """
    Protocol every VCS integration agent must satisfy.

    The four core operations mirror what Fritz (and any successor) must
    support to serve the Tinker workflow:

    1. ``setup()``           — load credentials, apply git identity.
    2. ``commit_and_ship()`` — stage, commit, push, optionally create PR
                               and auto-merge.
    3. ``push()``            — push a branch to the remote.
    4. ``create_pr()``       — open a pull request on the configured platform.
    5. ``verify_connections()`` — smoke-test credentials and reachability.

    All methods are async to avoid blocking the event loop during network I/O.
    """

    async def setup(self) -> None:
        """
        Initialise credentials, git identity, and platform connections.

        Must be called before any other method.  Should be idempotent —
        calling twice should not raise or duplicate work.
        """
        ...

    async def commit_and_ship(
        self,
        message: str,
        task_id: str = "",
        task_description: str = "",
        auto_merge: bool = False,
    ) -> Any:
        """
        Stage all changes, commit, push, and optionally open and merge a PR.

        Parameters
        ----------
        message          : Git commit message.
        task_id          : Tinker task ID for the commit trailer (traceability).
        task_description : Human-readable description for the PR body.
        auto_merge       : If True, merge the PR after creation when checks pass.

        Returns
        -------
        A result object with at minimum the fields:
            ok          (bool)  — True if the operation succeeded end-to-end.
            branch      (str)   — The branch that was pushed.
            commit_sha  (str)   — The commit SHA created.
            pr_url      (str)   — URL of the created PR, or "" if none.
            merged      (bool)  — True if the PR was auto-merged.
            errors      (list)  — Any non-fatal errors encountered.
        """
        ...

    async def push(
        self,
        branch: str | None = None,
        force: bool = False,
    ) -> Any:
        """
        Push a branch to the remote.

        Parameters
        ----------
        branch : Branch to push.  Defaults to the current branch.
        force  : If True, force-push (use with care; requires explicit auth
                 in push_policy when targeting protected branches).

        Returns
        -------
        A result object with at minimum:
            ok      (bool) — True if the push succeeded.
            stderr  (str)  — Git stderr output for diagnostics.
        """
        ...

    async def create_pr(
        self,
        title: str,
        body: str = "",
        head: str = "",
        base: str | None = None,
        platform: str = "auto",
    ) -> Any:
        """
        Create a pull request on GitHub or Gitea.

        Parameters
        ----------
        title    : PR title.
        body     : PR description (Markdown).
        head     : Source branch (defaults to current branch).
        base     : Target branch (defaults to ``config.default_branch``).
        platform : ``"github"``, ``"gitea"``, or ``"auto"`` (detect from remote URL).

        Returns
        -------
        A result object with at minimum:
            ok    (bool) — True if the PR was created successfully.
            url   (str)  — URL of the created PR.
            error (str)  — Error message if ok=False, else "".
        """
        ...

    async def verify_connections(self) -> dict[str, bool]:
        """
        Test credentials and reachability for all configured platforms.

        Returns
        -------
        dict mapping platform name to reachability:
            {"github": True, "gitea": False}  # example
        """
        ...
