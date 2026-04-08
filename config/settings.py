from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def _load_bool(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes", "on")


def _load_int(val: str, default: int) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        logger.warning("Could not parse %r as int, using default %d", val, default)
        return default


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


@dataclass(frozen=True)
class LLMSettings:
    server_url: str
    server_model: str
    server_ctx: int
    server_max_out: int
    server_timeout: int
    server_keep_alive: str
    server_fallback_model: str
    secondary_url: str
    secondary_model: str
    secondary_ctx: int
    secondary_max_out: int
    secondary_timeout: int
    secondary_keep_alive: str
    secondary_fallback_model: str
    llm_backend: str

    @classmethod
    def from_env(cls) -> LLMSettings:
        return cls(
            server_url=_env("TINKER_SERVER_URL", "http://localhost:11434"),
            server_model=_env("TINKER_SERVER_MODEL", "qwen3:7b"),
            server_ctx=_load_int(_env("TINKER_SERVER_CTX", "8192"), 8192),
            server_max_out=_load_int(_env("TINKER_SERVER_MAX_OUT", "2048"), 2048),
            server_timeout=_load_int(_env("TINKER_SERVER_TIMEOUT", "120"), 120),
            server_keep_alive=_env("TINKER_SERVER_KEEP_ALIVE", "10m"),
            server_fallback_model=_env("TINKER_SERVER_FALLBACK_MODEL"),
            secondary_url=_env("TINKER_SECONDARY_URL", "http://secondary:11434"),
            secondary_model=_env("TINKER_SECONDARY_MODEL", "phi3:mini"),
            secondary_ctx=_load_int(_env("TINKER_SECONDARY_CTX", "4096"), 4096),
            secondary_max_out=_load_int(_env("TINKER_SECONDARY_MAX_OUT", "1024"), 1024),
            secondary_timeout=_load_int(_env("TINKER_SECONDARY_TIMEOUT", "60"), 60),
            secondary_keep_alive=_env("TINKER_SECONDARY_KEEP_ALIVE", "10m"),
            secondary_fallback_model=_env("TINKER_SECONDARY_FALLBACK_MODEL"),
            llm_backend=_env("TINKER_LLM_BACKEND", "ollama"),
        )


@dataclass(frozen=True)
class StorageSettings:
    redis_url: str
    redis_ttl: int
    duckdb_path: str
    sqlite_path: str
    chroma_path: str
    session_backend: str
    db_backend: str
    postgres_dsn: str
    trino_host: str
    trino_port: int
    trino_user: str
    trino_catalog: str
    trino_schema: str
    trino_table: str

    @classmethod
    def from_env(cls) -> StorageSettings:
        return cls(
            redis_url=_env("TINKER_REDIS_URL", "redis://localhost:6379"),
            redis_ttl=_load_int(_env("TINKER_REDIS_TTL", "3600"), 3600),
            duckdb_path=_env("TINKER_DUCKDB_PATH", "tinker_session.duckdb"),
            sqlite_path=_env("TINKER_SQLITE_PATH", "tinker_tasks.sqlite"),
            chroma_path=_env("TINKER_CHROMA_PATH", "./chroma_db"),
            session_backend=_env("TINKER_SESSION_BACKEND"),
            db_backend=_env("TINKER_DB_BACKEND", "sqlite"),
            postgres_dsn=_env("TINKER_POSTGRES_DSN"),
            trino_host=_env("TINKER_TRINO_HOST", "localhost"),
            trino_port=_load_int(_env("TINKER_TRINO_PORT", "8080"), 8080),
            trino_user=_env("TINKER_TRINO_USER", "tinker"),
            trino_catalog=_env("TINKER_TRINO_CATALOG", "memory"),
            trino_schema=_env("TINKER_TRINO_SCHEMA", "tinker"),
            trino_table=_env("TINKER_TRINO_TABLE", "session_artifacts"),
        )


@dataclass(frozen=True)
class PathSettings:
    base_dir: str
    workspace: str
    artifact_dir: str
    diagram_dir: str
    backup_dir: str
    dlq_path: str
    audit_log_path: str
    lineage_path: str
    task_db: str
    models_file: str
    presets_file: str
    active_preset_file: str
    state_path: str
    flags_file: str
    confirm_dir: str
    control_dir: str

    @classmethod
    def from_env(cls) -> PathSettings:
        return cls(
            base_dir=_env("TINKER_BASE_DIR", "."),
            workspace=_env("TINKER_WORKSPACE", "./tinker_workspace"),
            artifact_dir=_env("TINKER_ARTIFACT_DIR", "./tinker_artifacts"),
            diagram_dir=_env("TINKER_DIAGRAM_DIR", "./tinker_diagrams"),
            backup_dir=_env("TINKER_BACKUP_DIR", "./tinker_backups"),
            dlq_path=_env("TINKER_DLQ_PATH", "tinker_dlq.sqlite"),
            audit_log_path=_env("TINKER_AUDIT_LOG_PATH", "tinker_audit.sqlite"),
            lineage_path=_env("TINKER_LINEAGE_PATH", "tinker_lineage.sqlite"),
            task_db=_env("TINKER_TASK_DB", "tinker_tasks_engine.sqlite"),
            models_file=_env("TINKER_MODELS_FILE", "./tinker_models.json"),
            presets_file=_env("TINKER_PRESETS_FILE", "./tinker_presets.json"),
            active_preset_file=_env("TINKER_ACTIVE_PRESET_FILE", "./tinker_active_preset.json"),
            state_path=_env("TINKER_STATE_PATH", "./tinker_state.json"),
            flags_file=_env("TINKER_FLAGS_FILE", "tinker_flags.json"),
            confirm_dir=_env("TINKER_CONFIRM_DIR", "./tinker_confirmations"),
            control_dir=_env("TINKER_CONTROL_DIR", "./tinker_control"),
        )


@dataclass(frozen=True)
class WebUISettings:
    port: int
    rate_per_sec: float
    rate_burst: float
    config: str
    streamlit_port: int
    gradio_port: int

    @classmethod
    def from_env(cls) -> WebUISettings:
        return cls(
            port=_load_int(_env("TINKER_WEBUI_PORT", "8082"), 8082),
            rate_per_sec=float(_env("TINKER_WEBUI_RATE_PER_SEC", "2.0")),
            rate_burst=float(_env("TINKER_WEBUI_RATE_BURST", "30.0")),
            config=_env("TINKER_WEBUI_CONFIG", "tinker_webui_config.json"),
            streamlit_port=_load_int(_env("TINKER_STREAMLIT_PORT", "8501"), 8501),
            gradio_port=_load_int(_env("TINKER_GRADIO_PORT", "7860"), 7860),
        )


@dataclass(frozen=True)
class OrchestratorSettings:
    macro_interval: int
    meso_trigger: int
    architect_timeout: int
    critic_timeout: int
    temperature: float
    self_improve_enabled: bool
    self_improve_branch: str
    confirm_before: str
    confirm_timeout: int

    @classmethod
    def from_env(cls) -> OrchestratorSettings:
        return cls(
            macro_interval=_load_int(_env("TINKER_MACRO_INTERVAL", "14400"), 14400),
            meso_trigger=_load_int(_env("TINKER_MESO_TRIGGER", "5"), 5),
            architect_timeout=_load_int(_env("TINKER_ARCHITECT_TIMEOUT", "120"), 120),
            critic_timeout=_load_int(_env("TINKER_CRITIC_TIMEOUT", "60"), 60),
            temperature=float(_env("TINKER_TEMPERATURE", "0.7")),
            self_improve_enabled=_load_bool(_env("TINKER_SELF_IMPROVE_ENABLED", "false")),
            self_improve_branch=_env("TINKER_SELF_IMPROVE_BRANCH"),
            confirm_before=_env("TINKER_CONFIRM_BEFORE"),
            confirm_timeout=_load_int(_env("TINKER_CONFIRM_TIMEOUT", "300"), 300),
        )


@dataclass(frozen=True)
class ObservabilitySettings:
    log_level: str
    json_logs: bool
    metrics_enabled: bool
    metrics_port: int
    health_port: int
    health_enabled: bool
    otlp_service_name: str
    otlp_endpoint: str
    otlp_headers: str
    tracer_window: int

    @classmethod
    def from_env(cls) -> ObservabilitySettings:
        return cls(
            log_level=_env("TINKER_LOG_LEVEL", "INFO"),
            json_logs=_load_bool(_env("TINKER_JSON_LOGS", "false")),
            metrics_enabled=_load_bool(_env("TINKER_METRICS_ENABLED", "true")),
            metrics_port=_load_int(_env("TINKER_METRICS_PORT", "9090"), 9090),
            health_port=_load_int(_env("TINKER_HEALTH_PORT", "8080"), 8080),
            health_enabled=_load_bool(_env("TINKER_HEALTH_ENABLED", "true")),
            otlp_service_name=_env("TINKER_OTLP_SERVICE_NAME", "tinker"),
            otlp_endpoint=_env("TINKER_OTLP_ENDPOINT"),
            otlp_headers=_env("TINKER_OTLP_HEADERS"),
            tracer_window=_load_int(_env("TINKER_TRACER_WINDOW", "100"), 100),
        )


@dataclass(frozen=True)
class AlertingSettings:
    slack_webhook: str
    alert_webhook: str

    @classmethod
    def from_env(cls) -> AlertingSettings:
        return cls(
            slack_webhook=_env("TINKER_SLACK_WEBHOOK"),
            alert_webhook=_env("TINKER_ALERT_WEBHOOK"),
        )


@dataclass(frozen=True)
class SecuritySettings:
    artifact_key: str
    secret_backend: str
    secrets_file: str
    vault_url: str
    vault_token: str

    @classmethod
    def from_env(cls) -> SecuritySettings:
        return cls(
            artifact_key=_env("TINKER_ARTIFACT_KEY"),
            secret_backend=_env("TINKER_SECRET_BACKEND"),
            secrets_file=_env("TINKER_SECRETS_FILE"),
            vault_url=_env("TINKER_VAULT_URL"),
            vault_token=_env("TINKER_VAULT_TOKEN"),
        )


@dataclass(frozen=True)
class MCPSettings:
    enabled: bool
    server_path: str
    server_name: str
    server_version: str
    connect_timeout: int
    servers: str
    token: str
    rate_per_sec: float
    rate_burst: float

    @classmethod
    def from_env(cls) -> MCPSettings:
        return cls(
            enabled=_load_bool(_env("TINKER_MCP_ENABLED", "false")),
            server_path=_env("TINKER_MCP_SERVER_PATH", "/mcp"),
            server_name=_env("TINKER_MCP_SERVER_NAME", "tinker"),
            server_version=_env("TINKER_MCP_SERVER_VERSION", "1.0.0"),
            connect_timeout=_load_int(_env("TINKER_MCP_CONNECT_TIMEOUT", "10"), 10),
            servers=_env("TINKER_MCP_SERVERS"),
            token=_env("TINKER_MCP_TOKEN"),
            rate_per_sec=float(_env("TINKER_MCP_RATE_PER_SEC", "1.0")),
            rate_burst=float(_env("TINKER_MCP_RATE_BURST", "60.0")),
        )


@dataclass(frozen=True)
class GrubSettings:
    coder_model: str
    reviewer_model: str
    tester_model: str
    debugger_model: str
    refactorer_model: str
    ollama_url: str
    exec_mode: str
    quality_threshold: float
    max_iterations: int
    output_dir: str
    queue_db: str
    artifacts_dir: str
    queue_workers: int
    enable_git: bool
    request_timeout: float
    context_max_chars: int
    context_target_chars: int
    summarizer_model: str

    @classmethod
    def from_env(cls) -> GrubSettings:
        return cls(
            coder_model=_env("GRUB_CODER_MODEL", "qwen2.5-coder:32b"),
            reviewer_model=_env("GRUB_REVIEWER_MODEL", "qwen3:7b"),
            tester_model=_env("GRUB_TESTER_MODEL", "qwen3:7b"),
            debugger_model=_env("GRUB_DEBUGGER_MODEL", "qwen2.5-coder:32b"),
            refactorer_model=_env("GRUB_REFACTORER_MODEL", "qwen2.5-coder:7b"),
            ollama_url=_env("GRUB_OLLAMA_URL", "http://localhost:11434"),
            exec_mode=_env("GRUB_EXEC_MODE", "sequential"),
            quality_threshold=float(_env("GRUB_QUALITY_THRESHOLD", "0.75")),
            max_iterations=_load_int(_env("GRUB_MAX_ITERATIONS", "5"), 5),
            output_dir=_env("GRUB_OUTPUT_DIR", "./grub_output"),
            queue_db=_env("GRUB_QUEUE_DB", "grub_queue.sqlite"),
            artifacts_dir=_env("GRUB_ARTIFACTS_DIR", "./grub_artifacts"),
            queue_workers=_load_int(_env("GRUB_QUEUE_WORKERS", "2"), 2),
            enable_git=_load_bool(_env("GRUB_ENABLE_GIT", "false")),
            request_timeout=float(_env("GRUB_REQUEST_TIMEOUT", "120.0")),
            context_max_chars=_load_int(_env("GRUB_CONTEXT_MAX_CHARS", "6000"), 6000),
            context_target_chars=_load_int(_env("GRUB_CONTEXT_TARGET_CHARS", "3000"), 3000),
            summarizer_model=_env("GRUB_SUMMARIZER_MODEL"),
        )


@dataclass(frozen=True)
class FritzSettings:
    metrics_enabled: bool
    metrics_port: int
    auto_git: bool
    config_file: str

    @classmethod
    def from_env(cls) -> FritzSettings:
        return cls(
            metrics_enabled=_load_bool(_env("FRITZ_METRICS_ENABLED", "true")),
            metrics_port=_load_int(_env("FRITZ_METRICS_PORT", "9091"), 9091),
            auto_git=_load_bool(_env("TINKER_AUTO_GIT", "true")),
            config_file=_env("FRITZ_CONFIG_FILE", "fritz_config.json"),
        )


@dataclass(frozen=True)
class BackpressureSettings:
    bp_warn_depth: int
    bp_pause_depth: int
    idempotency_ttl: int
    backup_retention_days: int

    @classmethod
    def from_env(cls) -> BackpressureSettings:
        return cls(
            bp_warn_depth=_load_int(_env("TINKER_BP_WARN_DEPTH", "50"), 50),
            bp_pause_depth=_load_int(_env("TINKER_BP_PAUSE_DEPTH", "200"), 200),
            idempotency_ttl=_load_int(_env("TINKER_IDEMPOTENCY_TTL", "3600"), 3600),
            backup_retention_days=_load_int(_env("TINKER_BACKUP_RETENTION_DAYS", "7"), 7),
        )


@dataclass(frozen=True)
class SearchSettings:
    searxng_url: str
    scraper_timeout_ms: int

    @classmethod
    def from_env(cls) -> SearchSettings:
        return cls(
            searxng_url=_env("TINKER_SEARXNG_URL", "http://localhost:8080"),
            scraper_timeout_ms=_load_int(_env("SCRAPER_TIMEOUT_MS", "20000"), 20000),
        )


@dataclass(frozen=True)
class TinkerSettings:
    llm: LLMSettings
    storage: StorageSettings
    paths: PathSettings
    webui: WebUISettings
    orchestrator: OrchestratorSettings
    observability: ObservabilitySettings
    alerting: AlertingSettings
    security: SecuritySettings
    mcp: MCPSettings
    grub: GrubSettings
    fritz: FritzSettings
    backpressure: BackpressureSettings
    search: SearchSettings

    @classmethod
    def from_env(cls) -> TinkerSettings:
        return cls(
            llm=LLMSettings.from_env(),
            storage=StorageSettings.from_env(),
            paths=PathSettings.from_env(),
            webui=WebUISettings.from_env(),
            orchestrator=OrchestratorSettings.from_env(),
            observability=ObservabilitySettings.from_env(),
            alerting=AlertingSettings.from_env(),
            security=SecuritySettings.from_env(),
            mcp=MCPSettings.from_env(),
            grub=GrubSettings.from_env(),
            fritz=FritzSettings.from_env(),
            backpressure=BackpressureSettings.from_env(),
            search=SearchSettings.from_env(),
        )


_settings: TinkerSettings | None = None


def get_settings() -> TinkerSettings:
    global _settings
    if _settings is None:
        _settings = TinkerSettings.from_env()
    return _settings
