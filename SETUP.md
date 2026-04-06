# SPEAKMAN.AI — Setup Guide

---

## Deployment Modes

| Mode | Command | Database |
|---|---|---|
| Docker + MongoDB | `docker-compose --profile mongo up --build` | MongoDB on port 27017 |
| Docker + SQLite | `USE_SQLITE=true docker-compose up --build` | File at `/data/speakmanai.db` in container |
| Dev / no Docker | `USE_SQLITE=true uvicorn server:app --port 8000` | `~/.speakmanai/speakmanai.db` |
| Desktop exe | Run `dist/SpeakmanAI.exe` | `~/.speakmanai/speakmanai.db` |

---

## Docker + MongoDB Setup

### 1. Create `.env` in the repo root

Set `LLM_PROVIDER` to your chosen provider, fill in that provider's key, and set `DEFAULT_MODEL` / `ADVANCED_MODEL` to match. Leave other provider blocks blank.

**Gemini**
```bash
USE_SQLITE=false
MONGO_ATLAS_URI=mongodb://mongodb:27017/
RAW_EVENTS_DB_NAME=speakmanai_db

LLM_PROVIDER=gemini
GEMINI_API_KEY=AIza...
DEFAULT_MODEL=gemini-2.5-flash
ADVANCED_MODEL=gemini-2.5-pro
```

**Claude**
```bash
USE_SQLITE=false
MONGO_ATLAS_URI=mongodb://mongodb:27017/
RAW_EVENTS_DB_NAME=speakmanai_db

LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
DEFAULT_MODEL=claude-sonnet-4-6
ADVANCED_MODEL=claude-opus-4-6
```

**OpenAI**
```bash
USE_SQLITE=false
MONGO_ATLAS_URI=mongodb://mongodb:27017/
RAW_EVENTS_DB_NAME=speakmanai_db

LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
DEFAULT_MODEL=gpt-4o-mini
ADVANCED_MODEL=gpt-4o
```

**Ollama**
```bash
USE_SQLITE=false
MONGO_ATLAS_URI=mongodb://mongodb:27017/
RAW_EVENTS_DB_NAME=speakmanai_db

LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_FALLBACK_MODEL=qwen2.5:14b
DEFAULT_MODEL=qwen2.5:14b
ADVANCED_MODEL=qwen2.5:14b
```

**Vertex AI** (no API key needed — auth via IAM when running on GCP)
```bash
USE_SQLITE=false
MONGO_ATLAS_URI=mongodb://mongodb:27017/
RAW_EVENTS_DB_NAME=speakmanai_db

LLM_PROVIDER=gemini
GCP_PROJECT_ID=my-gcp-project
GCP_REGION=us-east1
DEFAULT_MODEL=gemini-2.5-flash
ADVANCED_MODEL=gemini-2.5-pro
```

### 2. Start everything

```bash
# Foreground
docker-compose --profile mongo up --build

# Background
docker-compose --profile mongo up --build -d
```

### 3. Seed the database (first run only)

MongoDB mode requires a one-time import of the bundled workflows. Once the containers are up and your AI assistant is connected (see step 4), run `import_architecture_plan` for each workflow file:

```
import_architecture_plan('<contents of WorkflowsAndAgents/MCP_SOLUTION_ARCHITECTURE_V1.json>')
import_architecture_plan('<contents of WorkflowsAndAgents/MCP_CAPABILITY_GENERATOR_V1.json>')
import_architecture_plan('<contents of WorkflowsAndAgents/WORKFLOW_CREATOR_V1.json>')
import_architecture_plan('<contents of WorkflowsAndAgents/CLAUDE_WORKFLOW_CREATOR_V1.json>')
```

> SQLite and Desktop modes seed automatically on first launch — no manual import needed.

### 4. Connect your AI assistant

Add to `~/.claude/settings.json` (Claude Code):

```json
{
  "mcpServers": {
    "speakmanai": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

Restart your AI assistant after saving. The setup page at `http://localhost:8000/setup` provides one-click config buttons for Claude Desktop, Claude Code, and Cursor.

---

## Docker + SQLite Setup

### 1. Create `.env` in the repo root

```bash
USE_SQLITE=true

LLM_PROVIDER=gemini
GEMINI_API_KEY=AIza...
DEFAULT_MODEL=gemini-2.5-flash
ADVANCED_MODEL=gemini-2.5-pro
```

### 2. Start

```bash
docker-compose up --build
```

MongoDB is not started. The SQLite database is stored in the `speakmanai_data` Docker volume at `/data/speakmanai.db`. Workflows are seeded automatically from `WorkflowsAndAgents/` on first launch.

---

## Dev / No Docker Setup

Requires Python 3.11+.

### 1. Install dependencies

```bash
cd orchestration/mcp-server
pip install -r requirements.txt
```

### 2. Create `.env` in `orchestration/mcp-server/`

```bash
USE_SQLITE=true
LLM_PROVIDER=gemini
GEMINI_API_KEY=AIza...
DEFAULT_MODEL=gemini-2.5-flash
ADVANCED_MODEL=gemini-2.5-pro
```

Same provider options as the Docker section apply.

### 3. Start the server

```bash
uvicorn server:app --host 127.0.0.1 --port 8000 --reload
```

Workflows are seeded automatically on first launch.

---

## Desktop Exe (Windows)

A standalone Windows exe with a system tray icon and no terminal window. No Python installation required on the target machine.

### Build

```bash
python -m venv venv-desktop
source venv-desktop/Scripts/activate       # Git Bash
# venv-desktop\Scripts\activate.bat        # CMD

pip install -r orchestration/mcp-server/requirements-desktop.txt

python -m PyInstaller speakmanai.spec
```

Output: `dist/SpeakmanAI.exe`

### First run

1. Launch `SpeakmanAI.exe` — a tray icon appears and a browser window opens automatically
2. Choose your AI provider and enter your API key in the setup page
3. Connect your AI assistant from the setup page

Config is stored at `~/.speakmanai/config.json`. The SQLite database is at `~/.speakmanai/speakmanai.db`. Logs are at `~/.speakmanai/speakmanai.log`.

---

## Setup Page

All modes expose a setup UI at `http://localhost:8000/setup`.

Use it to:
- Pick your AI provider (Gemini, Claude, OpenAI, Ollama)
- Enter your API key and choose fast / advanced models
- Set the execution mode
- Connect your AI assistant (Claude Desktop, Claude Code, Cursor)

### Execution Modes

| Mode | Behaviour |
|---|---|
| **Auto** | Each agent runs with its own configured model tier. The provider and models chosen in setup determine what `fast` / `standard` / `advanced` resolve to. |
| **Force Delegate** | Every AI step pauses and is handed to your connected AI assistant (e.g. Claude Code) to execute instead of calling the LLM API. Useful when you have no API credits. |
| **Force Background** | Any steps configured to delegate to an AI assistant are overridden and run automatically using the provider above — fully hands-off. |

---

## LLM Provider Reference

| Provider | `LLM_PROVIDER` | Key variable | Default model | Advanced model |
|---|---|---|---|---|
| Gemini | `gemini` | `GEMINI_API_KEY` | `gemini-2.5-flash` | `gemini-2.5-pro` |
| Claude | `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` | `claude-opus-4-6` |
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-4o-mini` | `gpt-4o` |
| Ollama | `ollama` | *(none)* | set via `DEFAULT_MODEL` | set via `ADVANCED_MODEL` |
| Vertex AI | `gemini` | `GCP_PROJECT_ID` + `GCP_REGION` | `gemini-2.5-flash` | `gemini-2.5-pro` |

### Model tiers

All workflow agents use abstract tiers (`fast`, `standard`, `advanced`) rather than hardcoded model names. Your `DEFAULT_MODEL` and `ADVANCED_MODEL` settings are the authority — changing provider or model in setup takes effect immediately without editing any workflow files.

| Tier | Usage |
|---|---|
| `fast` | Simple formatting, classification, validators |
| `standard` | Content generation, summarisation, analysis |
| `advanced` | Complex reasoning, architecture, multi-step synthesis — maps to `ADVANCED_MODEL` |

### Ollama model sizing guidance

| Model size | Suitability |
|---|---|
| 3–7B | Fast but often produces incomplete outputs for multi-step workflows |
| 14B | Recommended minimum — good balance of quality and speed (~10 GB RAM) |
| 32B+ | Best quality (~20 GB+ RAM) |

A 14B+ model is strongly recommended for complex multi-agent workflows.

---

## Workflow Overview

| Workflow ID | Description |
|---|---|
| `MCP_CAPABILITY_GENERATOR_V1` | Converts a free-text environment description into a structured capabilities JSON array |
| `MCP_SOLUTION_ARCHITECTURE_V1` | Full multi-agent Solution Architecture Document pipeline |
| `WORKFLOW_CREATOR_V1` | Designs new workflow definitions and generates agent system prompts |
| `CLAUDE_WORKFLOW_CREATOR_V1` | Workflow creator variant tuned for high-performing models with concise prompting |

### Recommended order for first use

1. Run `MCP_CAPABILITY_GENERATOR_V1` with a plain-text description of your tech stack and business processes
2. Copy the structured capabilities JSON from the output
3. Run `MCP_SOLUTION_ARCHITECTURE_V1` — paste the capabilities when the workflow pauses and asks for them

---

## Health Check

```bash
curl http://localhost:8000/health
# {"status": "ok", "service": "speakmanai-mcp-server"}
```

---

## Troubleshooting

**Workflow stuck in IN_PROGRESS**
```
cancel_session('<session_id>')
```
Then start a new session.

**MongoDB connection refused**
Ensure you started with `--profile mongo`. Without it the MongoDB container does not start.

**Ollama not reachable from Docker**
The `extra_hosts: host.docker.internal:host-gateway` entry in `docker-compose.yml` handles this on Linux. On Mac and Windows, `host.docker.internal` resolves automatically.

**No API credits / want to test without LLM costs**
Use Force Delegate mode — your connected AI assistant (Claude Code) executes all AI steps instead of calling the LLM API. Set it in the setup page or call `set_execution_mode("force_delegate")` via MCP.

**Desktop exe logs**
If the exe starts but something isn't working, check `~/.speakmanai/speakmanai.log` for details.

**Poor or missing output when using Ollama**
SPEAKMAN.AI runs complex multi-agent workflows that require strong reasoning and instruction-following at every step. Small or general-purpose models frequently produce incomplete JSON, skip sections, or fail validation entirely — resulting in empty or degraded outputs. This is a model capability issue, not a bug.

Minimum recommended hardware and model for reliable results:
- **Model**: 14B parameter minimum — `qwen2.5:14b` or `llama3.3:70b` for best results
- **RAM**: 16 GB minimum for a 14B model; 32 GB+ recommended for 32B models
- **Thinking/reasoning models** (e.g. `qwen3:14b`, `deepseek-r1:14b`) produce significantly better results than standard instruction-tuned models of the same size

If you are seeing empty outputs or validation failures, try a larger model before troubleshooting anything else.
