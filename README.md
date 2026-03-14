# tinker

# Tinker: High-Level Architecture Design

## System Overview

Tinker is best understood as a **self-directed architectural research engine** — not a single agent, but a coordinated system of reasoning loops, memory layers, and specialized roles that collectively simulate how a senior engineering team might iteratively design and refine a complex system over days of focused work.

The core insight driving the design: *stateless models + stateful orchestration = persistent cognition*. The models themselves remember nothing, but the controller and memory systems create the illusion and function of continuous, evolving thought.

---

## Core System Components

### 1. The Orchestrator (The Brain Stem)

The central controller is the heartbeat of Tinker. It is not an AI — it is deterministic Python logic that manages the lifecycle of every reasoning cycle. Its responsibilities are:

**Loop management** — it drives the three nested loops (micro, meso, macro), deciding when to escalate from task-level thinking to subsystem-level thinking to architectural-level synthesis.

**Context assembly** — before every model call, the orchestrator composes a context window by pulling from memory systems, selecting relevant artifacts, injecting current task state, and formatting a structured prompt. This is the critical translation layer between stateful memory and stateless models.

**Task queue management** — it maintains a priority queue of pending reasoning tasks, manages their lifecycle (pending → active → critique → complete → archived), and routes outputs to the right memory stores.

**Anti-stagnation monitoring** — it tracks metrics like semantic similarity between consecutive outputs, time spent on a design branch, and task completion rates, triggering intervention when the system shows signs of circular reasoning or dead ends.

**Loop escalation logic** — it decides when a micro loop has produced enough to synthesize at the meso level, and when meso-level work warrants a macro architectural revision.

---

### 2. Agent Roles

Tinker has four distinct agent roles. These are not separate processes — they are **prompt personas** injected into the same or different model instances, giving the system specialized cognitive modes.

**The Architect (Primary — 7B on server)**
This is Tinker's main reasoning voice. It receives structured problem states and produces design artifacts: architecture proposals, component decompositions, interface definitions, trade-off analyses, and decision rationales. It operates across all three loop levels, but its most important work happens at the meso and macro levels.

**The Critic (Secondary — 2-3B on secondary machine)**
This is the adversarial judge. Every significant output from the Architect gets routed to the Critic, which is prompted with a different persona: skeptical, focused on failure modes, questioning assumptions, probing for inconsistencies. The Critic does not propose alternatives — it identifies weaknesses, rates confidence, and generates specific objection statements that feed back into the Architect's next cycle.

**The Researcher (Tool-calling mode — 7B)**
When the Architect identifies knowledge gaps, the Orchestrator spawns Researcher tasks. The Researcher is the Architect model operating in a different mode: given a specific question or topic, it uses web tools to gather information, synthesizes findings into structured research notes, and stores them in the research archive. It is deliberately constrained to not make design decisions — only to gather and summarize.

**The Synthesizer (Periodic — 7B)**
Triggered by the Orchestrator at macro loop intervals, the Synthesizer receives a compressed view of recent design work and produces architectural synthesis documents: updated system diagrams, revised decision logs, version summaries, and the next generation's starting context. This is the role that prevents context drift — it re-anchors the system to its accumulated understanding.

---

### 3. Reasoning Loops

The three loops are nested and operate on different timescales.

**Micro Loop (seconds to minutes)**
This is a single reasoning cycle: one prompt, one model response, one critique, one refinement. It operates at the task level. A micro loop might produce a component interface definition, a data flow analysis, or a critique of a specific design decision. Micro loops are cheap, fast, and numerous. The Orchestrator runs them continuously.

```
task selected → context assembled → Architect reasons → 
Critic evaluates → refinement applied → artifact stored → 
task marked complete → new tasks generated → repeat
```

**Meso Loop (minutes to hours)**
The meso loop operates across a cluster of related micro loops. When the Orchestrator detects that enough micro-level work has accumulated around a subsystem (e.g., the storage layer, the API gateway, the event bus), it triggers a meso synthesis. The Architect receives all recent artifacts from that subsystem domain and produces a cohesive subsystem design document. The Critic reviews it holistically. New exploration tasks are generated from gaps identified.

**Macro Loop (hours)**
The macro loop is architectural evolution. At configured intervals (e.g., every 4-6 hours), the Synthesizer produces a full architectural snapshot. This becomes the new "ground truth" that all subsequent loops build upon. The macro loop also manages **versioning** — each synthesis produces a named architecture version (v1.0, v1.1, etc.) stored permanently. This creates a timeline of how Tinker's understanding of the target system has evolved.

---

### 4. Memory Systems

Memory is what makes Tinker feel like a continuous thinker rather than a series of disconnected model calls. It is structured in layers with different retention, access patterns, and purposes.

**Working Memory (Redis or in-process)**
This is the current loop's active context. It holds the immediate task, recent outputs (last 3-5 turns), and assembled prompt components. It is ephemeral — wiped at the start of each new task cycle. Fast access, small capacity. Think of this as the model's "attention window extension."

**Session Memory (SQLite or DuckDB)**
This stores all outputs from the current run session: completed tasks, generated artifacts, critique records, research notes, and decision logs. It persists across micro and meso loops but is considered "active" rather than archived. The Orchestrator queries this constantly when assembling context. This is the medium-term memory of the current architectural reasoning session.

**Architecture State Store (JSON + Git)**
This is the canonical representation of what Tinker currently believes about the target system's architecture. It is structured as a versioned document containing: system purpose, identified components, component relationships, interface definitions, open questions, design decisions (with rationale), and confidence scores. Every macro loop produces a new version committed to a local Git repository. This gives Tinker a full history of its own thinking.

**Research Archive (Vector DB — ChromaDB or Qdrant locally)**
All research gathered by the Researcher role is embedded and stored in a vector database. When the Orchestrator assembles context for the Architect, it performs semantic search against this archive to surface relevant prior research. This prevents Tinker from re-researching topics it has already explored and allows knowledge to compound across sessions.

**Task Registry (SQLite)**
A persistent log of every task ever generated, its status, priority, parent task, generated subtasks, and associated outputs. This enables the Orchestrator to understand the reasoning lineage — why a task was created, what it produced, what it led to — and to avoid redundant exploration.

---

### 5. Tool Integrations

Tools are external capabilities the Researcher role can invoke. They are wrapped in a simple tool-calling interface the Orchestrator manages.

**Web Search** — query a search engine (SearXNG self-hosted, or direct scraping) to find relevant papers, documentation, architectural case studies, and technical discussions.

**Web Scraper** — fetch and extract content from specific URLs. Clean HTML to markdown for model consumption. Used to read documentation, GitHub READMEs, architecture blog posts, and academic papers.

**ArXiv/Papers fetcher** — specialized tool for pulling relevant computer science papers. The Researcher can query by topic and get structured summaries.

**Diagram Generator** — takes structured component/relationship descriptions and produces architecture diagrams (via Mermaid or Graphviz). Stored as artifacts.

**Artifact Writer** — writes structured outputs (markdown, JSON) to the artifact store with metadata tagging.

**Memory Query** — allows model prompts to explicitly request retrieval from the research archive or session memory via semantic search.

---

### 6. Controller / Orchestration Logic

The Orchestrator implements a decision engine with the following core logic:

**Task Selection** uses a priority scoring function combining: recency of related work, confidence gap (how uncertain is the current understanding of this area), dependency satisfaction (are prerequisite tasks complete), exploration diversity (avoiding over-focus on one subsystem), and staleness penalty (tasks that have been waiting too long get boosted).

**Context Assembly** is template-driven. For each agent role and loop level, there is a prompt template. The Orchestrator fills it with: system identity and current goal, the compressed architecture state (current version summary), relevant recent artifacts (top-k from semantic search), the specific task, any prior critique on related work, and output format instructions.

**Anti-Stagnation Mechanisms** are critical for a long-running system:

- *Semantic drift detection*: embed recent outputs and compare cosine similarity across a sliding window. If consecutive outputs are too similar, the Orchestrator injects a "challenge" prompt forcing the Architect to explore an alternative approach.
- *Branch forcing*: if the system has been refining the same component for too long, the Orchestrator forces exploration of a different subsystem.
- *Contradiction injection*: the Critic is periodically prompted to generate strong objections to the current architectural consensus, forcing the Architect to defend or revise.
- *Random exploration tasks*: a small percentage of task slots are reserved for randomly selected architectural questions unrelated to current focus — serendipitous exploration that can surface unexpected insights.

**Confidence Tracking** — each design decision in the architecture state carries a confidence score (0-1), estimated by the Critic. Low-confidence decisions become high-priority tasks for deeper investigation. As evidence accumulates, confidence scores are updated. This gives the Macro Loop a map of where the architecture is solid and where it is still speculative.

---

## Component Interaction Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                        ORCHESTRATOR                         │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────┐ │
│  │ Task Queue  │  │ Loop Manager │  │ Anti-Stagnation    │ │
│  │ & Scheduler │  │ (M/Me/Ma)    │  │ Monitor            │ │
│  └──────┬──────┘  └──────┬───────┘  └────────────────────┘ │
└─────────┼────────────────┼───────────────────────────────────┘
          │                │
          ▼                ▼
┌─────────────────────────────────────────────────────────────┐
│                   CONTEXT ASSEMBLER                         │
│         pulls from all memory layers, fills templates       │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
   ┌─────────────┐ ┌──────────┐ ┌────────────────┐
   │  ARCHITECT  │ │  CRITIC  │ │  RESEARCHER    │
   │  (7B main)  │ │  (2-3B)  │ │  (7B + tools)  │
   └──────┬──────┘ └────┬─────┘ └───────┬────────┘
          │             │               │
          └──────────────┴───────────────┘
                         │
          ┌──────────────▼──────────────────────────┐
          │              MEMORY SYSTEMS             │
          │  ┌─────────┐ ┌──────────┐ ┌──────────┐ │
          │  │ Working │ │ Session  │ │ Arch     │ │
          │  │ Memory  │ │ Memory   │ │ State    │ │
          │  │ (Redis) │ │(DuckDB)  │ │ (Git)    │ │
          │  └─────────┘ └──────────┘ └──────────┘ │
          │  ┌──────────────────┐ ┌──────────────┐  │
          │  │  Research Archive│ │ Task Registry│  │
          │  │  (ChromaDB)      │ │  (SQLite)    │  │
          │  └──────────────────┘ └──────────────┘  │
          └─────────────────────────────────────────┘
```

---

## Failure Modes and Mitigations

**Reasoning Loops** — the Architect gets stuck refining the same design without progress. *Mitigation*: semantic similarity monitoring, forced branch exploration, time-boxed task slots.

**Context Drift** — after many loops, the model's understanding diverges from the architecture state document because the assembled context is stale or incomplete. *Mitigation*: Synthesizer role re-anchors context at macro loop boundaries; architecture state is always injected fresh.

**Critique Collapse** — the Critic becomes too agreeable (small models tend toward agreement when prompted subtly). *Mitigation*: the Critic prompt explicitly instructs it to always find at least 2-3 weaknesses and rate outputs against strict criteria. Scores that trend too high trigger a "harder critic" prompt variant.

**Research Saturation** — the Researcher keeps finding the same sources and adding redundant knowledge. *Mitigation*: before research tasks, semantic search checks the archive. If sufficient coverage exists, the task is skipped or redirected to a more specific question.

**Hallucinated Architecture** — the Architect invents plausible-sounding but technically invalid designs. *Mitigation*: the Critic is explicitly prompted to check for known technical impossibilities; research tasks are generated to validate novel or unusual design claims.

**Memory Bloat** — session memory grows too large for effective context assembly. *Mitigation*: session memory uses recency + relevance scoring for retrieval (not full dumps); old artifacts are summarized and compressed by the Synthesizer.

---

## How Continuous Improvement Works

The system improves along three axes simultaneously:

**Breadth** — each research cycle adds to the archive, expanding the domain knowledge base. The task generator ensures coverage of unexplored subsystem areas.

**Depth** — each meso loop revisits a subsystem with accumulated context from prior micro loops, producing increasingly detailed and internally consistent designs.

**Coherence** — each macro loop forces the Synthesizer to reconcile all current design work into a unified architectural view, catching contradictions and gaps that only become visible at the system level.

Over 24-48 hours, Tinker would be expected to progress from a rough initial problem statement to a multi-version architecture document with detailed subsystem designs, a rich research archive, a log of considered-and-rejected alternatives, and a confidence map showing which parts of the design are well-supported versus speculative — the kind of artifact that would take a human architecture team weeks of focused effort to produce.

---

## Recommended Next Design Steps

With this high-level architecture established, the natural next areas to specify in detail are:

1. **The prompt architecture** — what exactly goes into each agent role's context, and how structured outputs are formatted to make them machine-parseable for the Orchestrator
2. **The task generation schema** — how completed work reliably spawns well-formed next tasks, avoiding vagueness
3. **The architecture state schema** — the exact structure of the versioned document that represents Tinker's current understanding
4. **The anti-stagnation algorithms** — the specific mathematical and heuristic mechanisms for detecting and breaking reasoning loops

# Tinker: Build Inventory

Let me break this into three clear categories.

---

## 1. Download (Models)

**Server machine (i7-7700, 64GB RAM, RTX 3090)**

You want a 7B reasoning model that runs well on a 3090 (24GB VRAM). Best options:

- **Qwen3-7B** — currently the strongest 7B reasoning model, excellent for structured output and multi-step thinking. Recommended.
- **DeepSeek-R1-Distill-Qwen-7B** — strong reasoning, good at following structured formats
- **Mistral-7B-Instruct-v0.3** — reliable fallback, very instruction-following

**Secondary machine (2-3B critic)**

- **Qwen3-1.7B or Qwen3-4B** — good reasoning for the size
- **Phi-3-mini (3.8B)** — Microsoft's small model, surprisingly capable at critique tasks
- **Gemma-3-2B** — solid small critic

Download these via **Hugging Face Hub** or grab GGUF quantized versions from **Bartowski or TheBloke repos** on HuggingFace.

---

## 2. Install (Existing Tools & Infrastructure)

### Model Serving
```
Ollama                  # easiest local model server, runs on both machines
                        # OR
llama.cpp               # if you want more control over GPU layers
vLLM                    # if you want OpenAI-compatible API with better batching
```
Ollama is the practical choice to start. It exposes an OpenAI-compatible REST API on both machines.

### Orchestration Runtime
```
Python 3.11+
uv                      # fast Python package manager (replaces pip/venv)
```

### Memory & Storage
```
Redis                   # working memory (apt install redis)
SQLite                  # comes with Python, no install needed
DuckDB                  # pip install duckdb (session memory, fast analytics)
ChromaDB                # pip install chromadb (vector DB for research archive)
Git                     # already on your system most likely
```

### Web / Research Tools
```
SearXNG                 # self-hosted search engine (Docker)
Playwright              # pip install playwright (web scraping)
trafilatura             # pip install trafilatura (HTML → clean text extraction)
httpx                   # pip install httpx (async HTTP)
```

### Embeddings (for vector DB)
```
sentence-transformers   # pip install sentence-transformers
                        # use nomic-embed-text or all-MiniLM-L6-v2 locally
```

### Diagram Generation
```
Graphviz                # apt install graphviz
                        # pip install graphviz
```

### Observability (so you can watch Tinker think)
```
Loguru                  # pip install loguru (structured logging)
Rich                    # pip install rich (pretty terminal output)
Textual                 # pip install textual (TUI dashboard, optional but very useful)
```

### Docker (for SearXNG)
```
Docker + Docker Compose # apt install docker.io
```

**SearXNG docker-compose** — one YAML file, gives you a private search engine Tinker can query freely without rate limits or API keys.

---

## 3. Build From Scratch

This is the actual Tinker system. Nothing off-the-shelf does what you need here.

### 3a. Orchestrator
The core engine. You build this as a Python async event loop.

- Loop manager (drives micro/meso/macro transitions)
- Task queue with priority scoring
- Loop escalation logic (when does micro → meso → macro trigger?)
- Anti-stagnation monitor (semantic similarity checks, branch forcing)
- Main run loop with graceful shutdown handling

### 3b. Context Assembler
The layer that turns memory into model prompts.

- Prompt template system (one template per agent role × loop level)
- Memory retrieval pipeline (pulls from ChromaDB + DuckDB + Redis)
- Context window budget manager (fits everything within token limits)
- Structured output formatters (ensures model responses are parseable)

### 3c. Agent Role Prompts
Not just prompts — a full prompt engineering system.

- Architect prompt (with structured XML/JSON output schema)
- Critic prompt (adversarial, strict scoring rubric, minimum objection count)
- Researcher prompt (tool-use instructions, synthesis format)
- Synthesizer prompt (compression + reconciliation instructions)
- Prompt variant library (harder critic, alternative forcing, contradiction injection)

### 3d. Task Engine
How Tinker generates, manages, and prioritizes its own work.

- Task schema (id, type, priority, parent, status, dependencies, outputs)
- Task generator (extracts next tasks from Architect outputs)
- Priority scorer (recency + confidence gap + staleness + diversity)
- Task registry CRUD (SQLite-backed)
- Dependency resolver (don't run task B until task A completes)

### 3e. Architecture State Manager
The versioned document that represents what Tinker currently believes.

- Architecture state schema (JSON structure for components, relationships, decisions, confidence scores)
- State diff and merge logic (how new findings update the existing state)
- Git commit wrapper (auto-commits each macro loop as a new version)
- Confidence score tracker and updater

### 3f. Memory Manager
Unified interface over all the memory layers.

- Working memory adapter (Redis)
- Session memory adapter (DuckDB)
- Research archive adapter (ChromaDB + sentence-transformers)
- Retrieval router (given a query, pulls from the right layer)
- Memory compression / summarization (for old session data)

### 3g. Tool Layer
Wrappers around external tools the Researcher can call.

- Web search tool (SearXNG API wrapper)
- Web scraper tool (Playwright + trafilatura)
- Artifact writer tool (structured markdown/JSON to disk)
- Diagram generator tool (Graphviz wrapper)
- Memory query tool (semantic search into research archive)

### 3h. Model Client
Abstraction over Ollama on both machines.

- Async client for server machine (Architect, Researcher, Synthesizer)
- Async client for secondary machine (Critic)
- Retry logic, timeout handling
- Token counting / context window enforcement
- Response parser (extract structured JSON from model output)

### 3i. Observability Dashboard
So you can actually watch Tinker think in real time.

- Live TUI (Textual) showing: current loop level, active task, last Architect output, last Critic score, task queue depth, memory stats
- Log streaming to file
- Architecture state viewer (renders current version as readable summary)

---

## Summary Table

| Thing | Source | Effort |
|---|---|---|
| 7B model (Qwen3-7B) | Download from HuggingFace | Low |
| 2-3B model (Phi-3-mini) | Download from HuggingFace | Low |
| Ollama | Install on both machines | Low |
| Redis, SQLite, Git | Install via apt/brew | Trivial |
| DuckDB, ChromaDB | pip install | Trivial |
| SearXNG | Docker Compose | Low |
| Playwright, trafilatura | pip install | Low |
| sentence-transformers | pip install | Low |
| Orchestrator | **Build** | High |
| Context Assembler | **Build** | High |
| Agent Prompts | **Build** | Medium |
| Task Engine | **Build** | High |
| Architecture State Manager | **Build** | Medium |
| Memory Manager | **Build** | Medium |
| Tool Layer | **Build** | Medium |
| Model Client | **Build** | Low |
| TUI Dashboard | **Build** | Medium |

---

## Suggested Build Order

1. **Model client + Ollama** — get both models responding to structured prompts
2. **Memory Manager** — stand up Redis, DuckDB, ChromaDB with a unified interface
3. **Tool Layer** — get web search and scraping working
4. **Agent prompts** — validate that Architect, Critic, Researcher produce parseable outputs
5. **Task Engine** — build the task schema and registry
6. **Context Assembler** — wire memory retrieval into prompt construction
7. **Orchestrator** — build the micro loop first, validate end-to-end, then add meso/macro
8. **Architecture State Manager** — add versioning once loops are stable
9. **Anti-stagnation** — add monitoring once you have enough runs to observe drift
10. **Dashboard** — add observability throughout but polish at the end

