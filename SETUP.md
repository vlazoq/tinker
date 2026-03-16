# Tinker Setup Guide

Tinker is an autonomous architecture-thinking engine that runs two Ollama instances across
two machines and designs software architectures through iterative AI reasoning loops.

---

## Hardware Requirements

| Machine | Role | Specs | Model |
|---------|------|-------|-------|
| Server | Architect, Researcher, Synthesizer | i7-7700, 64 GB RAM, RTX 3090 | `qwen3:7b` |
| Secondary | Critic | Any machine with ~8 GB RAM | `phi3:mini` |

Both machines need Ollama installed and reachable over the network.

---

## 1. Install Ollama

On **both machines**:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Start Ollama and configure it to listen on all interfaces (not just localhost):

```bash
# On each machine — expose Ollama on the network
OLLAMA_HOST=0.0.0.0 ollama serve
```

Or edit the Ollama systemd service to set `Environment=OLLAMA_HOST=0.0.0.0`.

---

## 2. Pull the Required Models

On the **server machine**:

```bash
ollama pull qwen3:7b
```

On the **secondary machine**:

```bash
ollama pull phi3:mini
```

Verify both are running:

```bash
curl http://localhost:11434/api/tags   # run on each machine
```

---

## 3. Start Infrastructure Services (Redis + SearXNG)

Tinker requires Redis (working memory) and SearXNG (web search).  A
`docker-compose.yml` in the repo root starts both with a single command:

```bash
# Start Redis and SearXNG in the background
docker compose up -d

# Verify both are healthy
docker compose ps
```

Redis will be available at `redis://localhost:6379`.
SearXNG will be available at `http://localhost:8080`.

**Manual alternative** (if you prefer not to use Docker Compose):

```bash
# Redis only
docker run -d --name tinker-redis -p 6379:6379 redis:7-alpine
# Or natively: sudo apt install redis-server && sudo systemctl start redis
```

---

## 5. Install Python Dependencies

```bash
# From the repo root
python -m pip install -e ".[dev]"

# Also install Playwright browser (for web scraping)
playwright install chromium
```

---

## 6. Configure Environment Variables

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```env
TINKER_SERVER_URL=http://<server-ip>:11434
TINKER_SECONDARY_URL=http://<secondary-ip>:11434
TINKER_REDIS_URL=redis://localhost:6379
TINKER_SEARXNG_URL=http://localhost:8080
```

---

## 7. Run Tinker

```bash
# Run with real Ollama models
python main.py --problem "Design a distributed job queue system"

# Run with in-process stubs (no Ollama or external services needed — for testing)
python main.py --problem "Design a distributed job queue system" --stubs
```

Tinker runs indefinitely. Press **Ctrl-C** to stop gracefully.

---

## 8. Run the Dashboard (in a separate terminal)

```bash
python -m dashboard
```

The Textual TUI dashboard shows:
- Current loop level (MICRO / MESO / MACRO)
- Active task and subsystem
- Architect / Critic token counts
- Live log stream
- Architecture state health

---

## 9. Prometheus Metrics (optional)

Tinker can expose Prometheus-compatible metrics on port 9090.  Install the
optional dependency to enable this:

```bash
pip install prometheus-client
```

Once installed, Tinker automatically starts a metrics HTTP server at startup:

```
http://localhost:9090/metrics
```

Add a scrape job to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: tinker
    static_configs:
      - targets: ['localhost:9090']
```

**Available metrics:**

| Metric | Type | Description |
|--------|------|-------------|
| `tinker_micro_loops_total` | Counter | Micro loops by status |
| `tinker_meso_loops_total` | Counter | Meso syntheses by status |
| `tinker_macro_loops_total` | Counter | Macro snapshots by status |
| `tinker_micro_loop_duration_seconds` | Histogram | Time per micro loop |
| `tinker_meso_loop_duration_seconds` | Histogram | Time per meso synthesis |
| `tinker_critic_score` | Gauge | Latest Critic quality score (0–1) |
| `tinker_task_queue_depth` | Gauge | Pending task count |
| `tinker_consecutive_failures` | Gauge | Current failure streak |
| `tinker_stagnation_events_total` | Counter | Stagnation detections by type |

**Environment variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `TINKER_METRICS_PORT` | `9090` | Prometheus scrape port |
| `TINKER_METRICS_ENABLED` | `true` | Set `false` to disable |

---

## Architecture Overview

```
main.py
  │
  ├── p1_model_client_n_ollama  ← async Ollama HTTP client + ModelRouter
  ├── p2_memory_manager         ← Redis + DuckDB + ChromaDB + SQLite
  ├── p3_tool_layer             ← SearXNG, web scraper, artifact writer
  ├── p4_agent_prompts          ← prompt templates + output schemas
  ├── p5_task_engine            ← task queue, generator, scorer
  ├── p6_context_assembler      ← token-budgeted context assembly
  ├── p7_orchestrator           ← micro/meso/macro loop controller
  │     └── agents.py           ← Architect / Critic / Synthesizer wrappers
  ├── p8_architecture_state_manager  ← versioned arch state + Git commits
  ├── p9_anti_stagnation_manager     ← loop detection + escape heuristics
  └── p10_observability_dashboard    ← Textual TUI
```

### Reasoning Loops

| Loop | Trigger | What happens |
|------|---------|--------------|
| **Micro** | Continuous | Select task → Architect → Critic → store artifact → generate tasks |
| **Meso** | Every N micro loops per subsystem | Synthesizer synthesises subsystem artifacts into a design doc |
| **Macro** | Every 4 hours | Full architectural snapshot → Git commit |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ConnectionRefusedError` on Ollama | Ensure `OLLAMA_HOST=0.0.0.0 ollama serve` is running on both machines |
| Redis connection error | Run `docker run -d -p 6379:6379 redis:7-alpine` |
| `No module named 'chromadb'` | Run `pip install -e .` from the repo root |
| Web search returns no results | Start SearXNG: `docker compose -f p3_tool_layer/docker-compose.searxng.yml up -d` |
| Import errors on startup | Run `pip install -e ".[dev]"` to install all dependencies |
| Dashboard blank | Check `./tinker_state.json` exists after Tinker starts (set `TINKER_STATE_PATH` if needed) |
