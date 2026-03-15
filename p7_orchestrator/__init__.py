"""
Orchestrator — drives Tinker's micro/meso/macro reasoning loops.

    from p7_orchestrator.orchestrator import Orchestrator
    from p7_orchestrator.config import OrchestratorConfig
    from p7_orchestrator.stubs import build_stub_components
"""
from .orchestrator import Orchestrator
from .config import OrchestratorConfig

__all__ = ["Orchestrator", "OrchestratorConfig"]
