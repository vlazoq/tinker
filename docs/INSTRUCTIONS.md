# Tinker — Installation & Deployment Guide

This guide walks you through everything needed to get Tinker running from scratch on a fresh machine (or two machines). Follow the steps in order.

---

## Prerequisites

You need:
- A Linux machine (Ubuntu 22.04+ recommended) or macOS
- Python 3.11 or newer
- At least 8 GB of RAM (16 GB+ recommended for running 7B models)
- A GPU is optional but makes models much faster (NVIDIA with 8GB+ VRAM ideal)
- Internet access for the initial setup (after that, Tinker runs offline)

---

## Step 1 — Install Ollama

Ollama is the local AI model server that runs your language models privately.

```bash
# Install Ollama (one command on Linux/macOS)
curl -fsSL https://ollama.ai/install.sh | sh

# Verify it's running
ollama --version
```

Ollama starts automatically as a background service on port `11434`.

**Pull the required models:**

```bash
# Architect model — the main "thinker" (7B parameters, ~4.5 GB)
ollama pull qwen3:7b

# Critic model — the reviewer (3.8B parameters, ~2.2 GB)
ollama pull phi3:mini
```

> **Two-machine setup:** If you have a secondary machine (e.g. a laptop) for the Critic model, install Ollama on it too and pull `phi3:mini` there. Then set `TINKER_SECONDARY_URL=http://<secondary-ip>:11434` in your `.env` file. On the secondary machine, make Ollama listen on all interfaces: `OLLAMA_HOST=0.0.0.0 ollama serve`

---

## Step 2 — Install Redis

Redis is used for Tinker's working memory (fast, temporary key-value storage).

```bash
# Ubuntu/Debian
sudo apt update && sudo apt install -y redis-server

# macOS (with Homebrew)
brew install redis

# Start Redis
sudo systemctl start redis-server   # Linux
brew services start redis            # macOS

# Verify it's running
redis-cli ping
# Should print: PONG
```

Redis listens on `localhost:6379` by default, which is what Tinker expects.

---

## Step 3 — Set Up SearXNG (Web Search)

SearXNG is a private, self-hosted meta-search engine. Tinker uses it to search the web without sending your queries to Google or Bing.

The easiest way is with Docker:

```bash
# Install Docker if you don't have it
curl -fsSL https://get.docker.com | sh

# Run SearXNG (one command, runs on port 8080)
docker run -d \
  --name searxng \
  --restart unless-stopped \
  -p 8080:8080 \
  -e SEARXNG_SECRET=$(openssl rand -hex 32) \
  searxng/searxng:latest

# Verify it's running
curl http://localhost:8080/search?q=test&format=json | head -c 200
```

> **Without Docker:** See the official SearXNG docs at https://docs.searxng.org/admin/installation.html for manual installation.

> **Skip web search:** If you don't want web search, Tinker works without it — the tool will just return empty results.

---

## Step 4 — Clone the Repo and Install Python Dependencies

```bash
# Clone the repository
git clone https://github.com/vlazoq/tinker.git
cd tinker

# Create a virtual environment (keeps dependencies isolated)
python3 -m venv .venv
source .venv/bin/activate      # Linux/macOS
# .venv\Scripts\activate       # Windows

# Install all dependencies
pip install -e ".[dev]"

# Install Playwright's browser (used for web scraping)
playwright install chromium
```

---

## Step 5 — Configure Environment Variables

Copy the template and fill in your values:

```bash
cp .env.example .env
nano .env    # or use any text editor
```

Minimum required settings for a single-machine setup:

```bash
# .env — Single machine setup (both models on same machine)
TINKER_SERVER_URL=http://localhost:11434
TINKER_SERVER_MODEL=qwen3:7b

TINKER_SECONDARY_URL=http://localhost:11434   # same machine for single-box setup
TINKER_SECONDARY_MODEL=phi3:mini

TINKER_REDIS_URL=redis://localhost:6379
TINKER_SEARXNG_URL=http://localhost:8080
```

Full two-machine setup (replace `192.168.1.x` with your secondary machine's IP):

```bash
# .env — Two machine setup
TINKER_SERVER_URL=http://localhost:11434
TINKER_SERVER_MODEL=qwen3:7b

TINKER_SECONDARY_URL=http://192.168.1.50:11434
TINKER_SECONDARY_MODEL=phi3:mini

TINKER_REDIS_URL=redis://localhost:6379
TINKER_SEARXNG_URL=http://localhost:8080

# Optional: change storage paths
TINKER_DUCKDB_PATH=./data/session.duckdb
TINKER_CHROMA_PATH=./data/chroma_db
TINKER_SQLITE_PATH=./data/tasks.sqlite
TINKER_WORKSPACE=./data/workspace
TINKER_ARTIFACT_DIR=./data/artifacts
TINKER_DIAGRAM_DIR=./data/diagrams
TINKER_STATE_PATH=./data/orchestrator_state.json
```

---

## Step 6 — First Run (Smoke Test)

Before using real AI models, do a smoke test with built-in stubs (no AI or Redis needed):

```bash
python main.py --problem "Design a distributed cache" --stubs
```

You should see log output like:
```
10:00:00  INFO      tinker.main  TINKER starting
10:00:00  INFO      tinker.main  Problem: Design a distributed cache
10:00:00  INFO      tinker.main  Mode   : STUBS
10:00:00  INFO      tinker.orchestrator  Orchestrator starting...
10:00:00  INFO      tinker.orchestrator.micro  Micro loop 1 starting...
10:00:00  INFO      tinker.orchestrator.micro  Task selected: initial-task
...
```

Press `Ctrl-C` to stop. If you see micro loops completing without errors, the wiring is correct.

---

## Step 7 — Run for Real

```bash
# Make sure your .env is configured, then:
python main.py --problem "Design a resilient message queue system"
```

Tinker will run indefinitely. Let it run — it improves over time. Results are saved to disk persistently.

**Watch what it's doing (in a second terminal):**

```bash
# Option A: separate dashboard terminal
python -m dashboard

# Option B: dashboard embedded in the same terminal
python main.py --problem "..." --dashboard
```

**Useful options:**

```bash
# Change log verbosity
python main.py --problem "..." --log-level DEBUG

# All options
python main.py --help
```

---

## Step 8 — Where Are the Results?

After running, Tinker produces:

| Location | Contents |
|----------|----------|
| `./tinker_workspace/` | Versioned architecture snapshot files (the main output) |
| `./tinker_artifacts/` | Written artifacts (markdown/JSON files per task) |
| `./tinker_diagrams/` | Generated architecture diagrams |
| `./tinker_session.duckdb` | Session database (artifacts, history) |
| `./chroma_db/` | Semantic research archive |
| `./tinker_tasks_engine.sqlite` | Task queue and registry |
| `/tmp/tinker_orchestrator_state.json` | Live state (overwritten each loop) |

The most useful output is in `tinker_workspace/` — look for `architecture_v*.json` files that show the evolving design.

---

## Running as a Service (Optional)

To keep Tinker running after you close your terminal, use `systemd`:

```bash
# Create the service file
sudo nano /etc/systemd/system/tinker.service
```

```ini
[Unit]
Description=Tinker Autonomous Architecture Engine
After=network.target redis.service

[Service]
Type=simple
User=your-username
WorkingDirectory=/path/to/tinker
Environment=PATH=/path/to/tinker/.venv/bin
ExecStart=/path/to/tinker/.venv/bin/python main.py --problem "Design a resilient microservices platform"
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable tinker
sudo systemctl start tinker

# Watch the logs
sudo journalctl -u tinker -f
```

---

## Troubleshooting

### "Connection refused" to Ollama
```bash
# Check if Ollama is running
systemctl status ollama   # Linux
ps aux | grep ollama      # macOS/Linux

# Restart it
sudo systemctl restart ollama   # Linux
ollama serve                    # manual start
```

### Redis connection error
```bash
redis-cli ping   # Should return PONG
sudo systemctl restart redis-server
```

### "Module not found" errors
```bash
# Make sure you're in the virtualenv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Models are very slow
- Make sure Ollama is using your GPU: `ollama run qwen3:7b` and look for `using gpu` in its output
- If CPU only: reduce model size (`qwen3:4b` or `qwen3:1.5b`) and update `.env`
- Set longer timeouts in `.env`: `TINKER_ARCHITECT_TIMEOUT=300`

### Dashboard shows "DISCONNECTED"
The dashboard connects to the orchestrator via a state snapshot file. Make sure:
1. `main.py` is running (it writes the state file)
2. Both use the same `TINKER_STATE_PATH` (default: `/tmp/tinker_orchestrator_state.json`)

### Web search returns no results
```bash
# Test SearXNG directly
curl "http://localhost:8080/search?q=event+sourcing&format=json" | python3 -m json.tool | head -20

# If SearXNG is down:
docker restart searxng
```

---

## Updating Tinker

```bash
cd tinker
git pull origin main
pip install -e ".[dev]"    # re-install in case dependencies changed
```

---

## Quick Reference

| Command | What it does |
|---------|-------------|
| `python main.py --problem "..."` | Run with real AI models |
| `python main.py --problem "..." --stubs` | Run with fake stubs (no AI needed) |
| `python main.py --problem "..." --dashboard` | Run with embedded TUI dashboard |
| `python -m dashboard` | Run dashboard only (in a separate terminal) |
| `python main.py --help` | Show all options |
| `ollama pull qwen3:7b` | Download/update the Architect model |
| `ollama pull phi3:mini` | Download/update the Critic model |
| `redis-cli ping` | Check Redis is alive |
