# Tinker — Setup Guide

Tinker runs two Ollama instances across one or two machines and designs software
architectures through iterative AI reasoning loops.

> **Quick summary by OS**
> - **Linux / macOS** — install natively, all features available
> - **Windows** — install natively; Redis requires Docker Desktop (or WSL2);
>   everything else works without modification

---

## Hardware Requirements

| Machine | Role | Minimum Specs | Recommended Model |
|---------|------|---------------|-------------------|
| Primary (server) | Architect, Researcher, Synthesizer | 16 GB RAM, GPU with ≥8 GB VRAM | `qwen3:7b` |
| Secondary | Critic | 8 GB RAM, CPU is fine | `phi3:mini` |

Both machines need Ollama reachable over the network.  A single machine works
too — just run both Ollama instances on different ports or point both URLs at
the same host.

---

## Port Reference

| Service | Default Port | Env var to change it |
|---------|-------------|----------------------|
| Ollama (primary) | 11434 | `TINKER_SERVER_URL` |
| Ollama (secondary) | 11434 | `TINKER_SECONDARY_URL` |
| Redis | 6379 | `TINKER_REDIS_URL` |
| SearXNG (Docker) | **8888** | `TINKER_SEARXNG_URL` |
| Health endpoint | **8081** | `TINKER_HEALTH_PORT` |
| Web UI (FastAPI) | 8082 | `TINKER_WEBUI_PORT` |
| Gradio UI | 7860 | `TINKER_GRADIO_PORT` |
| Streamlit UI | 8501 | `TINKER_STREAMLIT_PORT` |
| Prometheus metrics | 9090 | `TINKER_METRICS_PORT` |

---

## Step 1 — Install Python 3.11+

### Linux (Ubuntu / Debian)

```bash
sudo apt update
sudo apt install python3.11 python3.11-venv python3-pip git -y
python3.11 --version   # should print 3.11.x
```

### macOS

```bash
# Install Homebrew first if you don't have it:
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

brew install python@3.11 git
python3.11 --version
```

### Windows

1. Download the Python 3.11 installer from https://www.python.org/downloads/
2. **Important:** tick "Add Python to PATH" during installation
3. Open **Command Prompt** (or **PowerShell**) and verify:

```cmd
python --version
```

> If you see `Python 3.11.x` you are ready.  If you see 3.10 or older,
> re-run the installer and make sure "Add to PATH" is checked.

---

## Step 2 — Install Ollama

### Linux

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Configure Ollama to listen on all network interfaces (needed if the secondary
machine is separate from the primary):

```bash
# Edit the systemd service, or run with the env var:
OLLAMA_HOST=0.0.0.0 ollama serve
```

### macOS

Download the macOS app from https://ollama.com/download or:

```bash
brew install ollama
```

To expose Ollama on the network (for a separate secondary machine):

```bash
OLLAMA_HOST=0.0.0.0 ollama serve
```

### Windows

1. Download the Windows installer from https://ollama.com/download
2. Run the `.exe` installer — Ollama will start as a background service
3. To expose Ollama on the network, open **Settings → Advanced** in the
   Ollama system tray icon and set "OLLAMA_HOST" to `0.0.0.0`
4. Verify Ollama is running:

```cmd
curl http://localhost:11434/api/tags
```

---

## Step 3 — Pull AI Models

Run these on the **primary machine**:

```bash
ollama pull qwen3:7b
```

Run this on the **secondary machine** (or the same machine if using one):

```bash
ollama pull phi3:mini
```

Verify both are loaded:

```bash
curl http://localhost:11434/api/tags
```

---

## Step 4 — Install Infrastructure Services (Redis + SearXNG)

Tinker needs Redis (working memory) and SearXNG (self-hosted web search).
The easiest way is Docker Compose, which works on all three OSes.

### Linux

```bash
# Install Docker if not already installed:
sudo apt install docker.io docker-compose-plugin -y
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # re-login after this

# Start Redis and SearXNG:
docker compose up -d
docker compose ps   # both should show "healthy"
```

### macOS

1. Install Docker Desktop from https://www.docker.com/products/docker-desktop
2. Open Docker Desktop and wait for it to finish starting
3. In the repo directory:

```bash
docker compose up -d
docker compose ps
```

### Windows

1. Install **Docker Desktop for Windows** from https://www.docker.com/products/docker-desktop
   - Requires Windows 10/11 with WSL2 enabled (Docker Desktop will prompt you)
   - Or use Hyper-V backend (Windows Pro/Enterprise only)
2. After Docker Desktop is running (whale icon in system tray):

```cmd
docker compose up -d
docker compose ps
```

Redis will be available at `redis://localhost:6379`.
SearXNG will be available at `http://localhost:8888`.

#### Windows without Docker (reduced-feature mode)

If you cannot install Docker, Tinker still runs — Redis-backed working memory
is disabled, but all durable stores (DuckDB, ChromaDB, SQLite) work normally.
You will see this warning on startup:

```
WARNING RedisAdapter: Redis not reachable at redis://localhost:6379 — working memory disabled.
On Windows: start Redis with 'docker compose up -d'
```

To explicitly disable the Redis connection attempt:

```cmd
set TINKER_REDIS_URL=
python main.py --stubs
```

---

## Step 5 — Clone the Repository and Install Dependencies

### Linux / macOS

```bash
git clone <your-repo-url> tinker
cd tinker

python3.11 -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"

# Install Playwright browser (used by the web scraper tool):
playwright install chromium
```

### Windows

```cmd
git clone <your-repo-url> tinker
cd tinker

python -m venv .venv
.venv\Scripts\activate

pip install -e ".[dev]"

playwright install chromium
```

> **Tip:** If `pip install -e ".[dev]"` fails with a build error for a package,
> try upgrading pip first: `python -m pip install --upgrade pip setuptools wheel`

---

## Step 6 — Configure Environment Variables

### All platforms

```bash
cp .env.example .env
```

Then open `.env` in a text editor and set at minimum:

```env
# Primary Ollama (Architect / Researcher / Synthesizer)
TINKER_SERVER_URL=http://<primary-machine-ip>:11434

# Secondary Ollama (Critic) — use same IP if one machine
TINKER_SECONDARY_URL=http://<secondary-machine-ip>:11434

# Redis (skip or leave blank if running without Docker)
TINKER_REDIS_URL=redis://localhost:6379

# SearXNG (started by docker compose up -d)
TINKER_SEARXNG_URL=http://localhost:8888
```

### Windows-specific `.env` notes

- **Paths:** use forward slashes or double backslashes:
  ```env
  TINKER_STATE_PATH=./tinker_state.json       # ✅ works on all OSes
  TINKER_ARTIFACT_DIR=./tinker_artifacts      # ✅ works on all OSes
  # TINKER_STATE_PATH=C:\Users\you\tinker_state.json  # also fine
  ```
- **`/tmp` paths:** do not use `/tmp/` — it does not exist on Windows.
  The defaults in `.env.example` have been updated to use `./` paths.

---

## Step 7 — Run Tinker

### All platforms

```bash
# Real models (requires Ollama + Docker services running):
python main.py --problem "Design a distributed task queue"

# Stub mode — no Ollama, Redis, or external services needed:
python main.py --problem "Design a distributed task queue" --stubs
```

Press **Ctrl-C** to stop gracefully.  Tinker will finish the current micro loop
and write a final state snapshot before exiting.

### Windows note on graceful shutdown

On Windows, `Ctrl-C` is handled via `signal.signal(SIGINT)` (installed at
startup) which requests a graceful shutdown.  If the shutdown takes more than
a few seconds (e.g., the current AI call is still in progress), pressing
`Ctrl-C` a second time will force-terminate immediately.

---

## Step 8 — Run the Web UI (optional, separate terminal)

```bash
# FastAPI / React dashboard:
python -m webui

# Gradio UI:
python -m gradio_ui

# Streamlit UI:
python -m streamlit_ui
```

Open your browser at:
- Web UI: http://localhost:8082
- Gradio UI: http://localhost:7860
- Streamlit UI: http://localhost:8501

---

## Step 9 — Run the TUI Dashboard (optional, separate terminal)

```bash
python -m dashboard
```

The Textual TUI shows the current loop level, active task, critic scores, and
a live log stream.

> **Windows note:** the TUI uses `textual`, which requires a terminal with
> Unicode and colour support.  Use **Windows Terminal** (available from the
> Microsoft Store) rather than the legacy `cmd.exe` for the best experience.

---

## Step 10 — Prometheus Metrics (optional)

Install the optional dependency:

```bash
pip install prometheus-client
```

Tinker then starts a metrics server at `http://localhost:9090/metrics`.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ConnectionRefusedError` on Ollama | Make sure `ollama serve` is running with `OLLAMA_HOST=0.0.0.0` |
| `Redis not reachable` warning | Start Redis: `docker compose up -d` (or ignore on Windows without Docker) |
| `No module named 'chromadb'` | Run `pip install -e .` from the repo root inside your venv |
| SearXNG returns no results | Check Docker: `docker compose ps` — SearXNG should be healthy |
| Web search fails | Verify `TINKER_SEARXNG_URL=http://localhost:8888` in your `.env` |
| Dashboard shows blank page | Check that `tinker_state.json` exists: run Tinker first, then open the UI |
| Import errors on startup | Activate your venv first: `source .venv/bin/activate` (Linux/macOS) or `.venv\Scripts\activate` (Windows) |
| Windows: `playwright install` fails | Run `pip install playwright` then `python -m playwright install chromium` |
| Windows: port in use | Check nothing else is on ports 8081, 8082, 8888. Change ports via env vars. |
| Windows: `'python' not found` | Reinstall Python 3.11 with "Add to PATH" ticked |

---

## Running on a Single Machine

You do not need two machines.  Set both Ollama URLs to the same host:

```env
TINKER_SERVER_URL=http://localhost:11434
TINKER_SECONDARY_URL=http://localhost:11434
```

Ollama will serve both models from the same process.

---

## Full Port Map (for firewall / router configuration)

| Port | Service | Direction |
|------|---------|-----------|
| 11434 | Ollama API | inbound (if secondary machine is remote) |
| 6379 | Redis | loopback only (do not expose externally) |
| 8081 | Orchestrator health endpoint | loopback / internal |
| 8082 | Tinker Web UI (FastAPI) | inbound (browser access) |
| 7860 | Gradio UI | inbound (browser access) |
| 8501 | Streamlit UI | inbound (browser access) |
| 8888 | SearXNG (Docker) | loopback only |
| 9090 | Prometheus metrics | internal / monitoring system |
