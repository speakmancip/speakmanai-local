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

Set `LLM_PROVIDER` to your chosen provider, fill in that provider's key, and set `DEFAULT_MODEL` (fast) / `STANDARD_MODEL` / `ADVANCED_MODEL` to match. Leave other provider blocks blank.

**Gemini**
```bash
USE_SQLITE=false
MONGO_ATLAS_URI=mongodb://mongodb:27017/
RAW_EVENTS_DB_NAME=speakmanai_db

LLM_PROVIDER=gemini
GEMINI_API_KEY=AIza...
DEFAULT_MODEL=gemini-3.5-flash-lite
STANDARD_MODEL=gemini-3.8-flash
ADVANCED_MODEL=gemini-3.1-pro-preview
```

**Claude**
```bash
USE_SQLITE=false
MONGO_ATLAS_URI=mongodb://mongodb:27017/
RAW_EVENTS_DB_NAME=speakmanai_db

LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
DEFAULT_MODEL=claude-haiku-5-5
STANDARD_MODEL=claude-sonnet-5-5
ADVANCED_MODEL=claude-opus-5-5
```

**OpenAI**
```bash
USE_SQLITE=false
MONGO_ATLAS_URI=mongodb://mongodb:27017/
RAW_EVENTS_DB_NAME=speakmanai_db

LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
DEFAULT_MODEL=gpt-6-luna
STANDARD_MODEL=gpt-6.1-sol
ADVANCED_MODEL=gpt-6-astra
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
STANDARD_MODEL=qwen2.5:14b
ADVANCED_MODEL=qwen2.5:14b
```

**Vertex AI** (no API key needed — auth via IAM when running on GCP)
```bash
USE_SQLITE=false
MONGO_ATLAS_URI=mongodb://mongodb:27017/
RAW_EVENTS_DB_NAME=speakmanai_db

LLM_PROVIDER=vertexai
GCP_PROJECT_ID=my-gcp-project
GCP_REGION=us-east1
DEFAULT_MODEL=gemini-3.5-flash-lite
STANDARD_MODEL=gemini-3.8-flash
ADVANCED_MODEL=gemini-3.1-pro-preview
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

> SQLite and Desktop modes seed automatically on startup — no manual import needed. Only workflows not already in the database are imported, so this also backfills any new bundled workflow after an update without touching ones you've customized.

### 4. Connect your AI assistant

**Claude Code.** Register the server once for all your projects:

```bash
claude mcp add --transport http --scope user speakmanai http://localhost:8000/mcp
```

The setup page at `http://localhost:8000/setup` has a **Connect Claude Code** button that runs this for you, and shows the command to copy if it cannot find the `claude` CLI. Restart Claude Code afterwards, then check with `claude mcp list`.

> Earlier versions wrote the server into `~/.claude/settings.json`. Claude Code does not read MCP servers from that file, so that entry never took effect. It keeps them in `~/.claude.json`, which the command above updates.

**Other assistants.** Setup is manual. Add the MCP server URL `http://localhost:8000/mcp` in the assistant's own settings (the setup page shows it with a copy button).

- The Claude app (chat side): the app's Add custom connector screen cannot take a local address, so the server goes into the app's config file through a bridge program, which needs Node installed for your user. The skills bundle upload is optional: the app can fetch the skill from the server. See [CLAUDE_APP.md](https://github.com/speakmancip/speakmanai-cc/blob/master/desktop-skills/CLAUDE_APP.md). Claude Code inside the Claude app (its Code tab) is simpler: install the Claude Code CLI, connect it from the setup page, and restart the app.
- Microsoft Copilot Studio: it cannot reach `localhost`, so use a public tunnel URL. A tunnel is refused until you turn on Remote access on the setup page, and then needs the token it shows (see [Remote access and tunnels](#remote-access-and-tunnels)). See `desktop-skills/COPILOT_STUDIO.md` in the `speakmanai-cc` repo.
- Cursor and Codex: not tested.

---

## Docker + SQLite Setup

### 1. Create `.env` in the repo root

```bash
USE_SQLITE=true

LLM_PROVIDER=gemini
GEMINI_API_KEY=AIza...
DEFAULT_MODEL=gemini-3.5-flash-lite
STANDARD_MODEL=gemini-3.8-flash
ADVANCED_MODEL=gemini-3.1-pro-preview
```

### 2. Start

```bash
docker-compose up --build
```

MongoDB is not started. The SQLite database is stored in the `speakmanai_data` Docker volume at `/data/speakmanai.db`. Workflows are seeded automatically from `WorkflowsAndAgents/` on startup — new workflow files are backfilled on later launches too, without touching ones already in the database.

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
DEFAULT_MODEL=gemini-3.5-flash-lite
STANDARD_MODEL=gemini-3.8-flash
ADVANCED_MODEL=gemini-3.1-pro-preview
```

Same provider options as the Docker section apply.

### 3. Start the server

```bash
uvicorn server:app --host 127.0.0.1 --port 8000 --reload
```

Workflows are seeded automatically on startup, including any new ones added since your last launch.

---

## Desktop Exe (Windows)

A standalone Windows exe with a system tray icon and no terminal window. No Python installation required on the target machine.

### Build

```bash
python -m venv venv-desktop
source venv-desktop/Scripts/activate       # Git Bash
# venv-desktop\Scripts\activate.bat        # CMD

pip install -r orchestration/mcp-server/requirements-desktop.txt

python tools/bundle_skills.py              # stage the skills (see below)
python -m PyInstaller speakmanai.spec
```

Output: `dist/SpeakmanAI.exe`

### Bundled skills

The exe carries the SPEAKMAN.AI skills from the [speakmanai-cc](https://github.com/speakmancip/speakmanai-cc) repo, so a user does not need to clone that repo or run its install script.

**At build time**, `tools/bundle_skills.py` reads a speakmanai-cc checkout and stages `build/skills_bundle`:

- the Claude Code skills (`.claude/skills`) with their synthesis `prompts`
- the upload bundles for other assistants, built by that repo's `desktop-skills/build.ps1` (needs PowerShell; pass `--skip-zips` to leave them out)
- `manifest.json` with the skills version (plugin version plus the checkout's commit) and a hash per file

Point it at the checkout with `--cc-dir PATH` or `SPEAKMANAI_CC_DIR`. By default it looks for a `speakmanai-cc` folder beside this repo's grandparent. Build from a clean checkout: uncommitted changes mark the version `.dirty`. `speakmanai.spec` includes the bundle when it exists and warns when it does not.

**At run time**, **Connect Claude Code** on the setup page registers the MCP server and installs the skills. It copies them to `~/.claude/skills` and the assets to `~/.claude/speakmanai-cc`, without prompting:

- a file you have not changed since the last install is replaced
- a file that differs from the last install (you edited it, or it came from `install.ps1`) is copied to `~/.claude/speakmanai-cc/backups/<timestamp>/` first, then replaced
- your own skills, and extra files you added inside a skill folder, are left alone

`~/.claude/speakmanai-cc/installed.json` records the installed version. The page shows whether the skills are installed, up to date, or have an update, and links the upload bundles for the Claude app and Copilot Studio. The speakmanai-cc `install.ps1` and `install.sh` still work for installing the skills without the exe.

### First run

1. Launch `SpeakmanAI.exe` — a tray icon appears and a browser window opens automatically
2. Click **Connect Claude Code** on the setup page (or pick another assistant for its manual steps)
3. Optional: add your own model API under **Model API**

A new install with no model API starts in Delegate Only mode, so workflows run through your connected assistant with no key.

**Model keys in your environment are ignored by the exe.** A key such as `ANTHROPIC_API_KEY` set for another tool does not count as a model API, so it is never billed without you entering it on the setup page. Only `~/.speakmanai/config.json` counts. Start the exe with `SPEAKMANAI_USE_ENV=1` to use environment settings instead. Docker and run-from-source read the environment and `.env` as before.

### Upgrading an existing install

- **Workflows.** The exe only imports a bundled workflow that is missing from your database, so an older database keeps the prompts, review gates and agent lists it was created with. The setup page shows an amber box with an **Update workflows** button when yours differ. It replaces the shipped workflows with this version's, adds missing agents, unlinks retired ones, and first writes what it replaced to `~/.speakmanai/backups/workflows-<timestamp>.json`. Your own workflows and agents are not touched. To keep an edited shipped workflow, copy it to a new workflow ID first. It is a button, never automatic.
- **Skills.** The Skills block on the setup page shows when an update is available. A file you edited is backed up under `~/.claude/speakmanai-cc/backups/` before it is replaced.
- **Models.** A saved config that pins an older Claude or OpenAI model keeps it. Save the setup page again to move to the new defaults.

### Remote access and tunnels

Copilot Studio cannot reach `localhost`, so it needs a public tunnel URL. The engine treats any request that is not local (a Host other than `localhost`, `127.0.0.1` or `::1`, or a forwarding header such as `X-Forwarded-For`) as remote:

- **Remote access off (default):** a remote request gets a 403 and nothing else happens. A tunnel does not work.
- **Remote access on:** only `/mcp` is reachable remotely, and it needs the token on every request, sent as the header `x-api-key` or as `Authorization: Bearer <token>` (401 otherwise).
- **Always local only:** `/setup` and `/api/*` never accept a remote request, so a tunnel cannot read the token, change settings or turn Remote access off.

Turn it on under **Remote access** (an Advanced fold) on the setup page. The token is created the first time and shown with a Copy button; **Make a new token** replaces it, which stops anything using the old one. Local use needs no token, and **Connect Claude Code** registers the plain `http://localhost:<port>/mcp` URL.

Separately, the local `/api` refuses state-changing requests that a web page on another site sends through your browser (a request with an `Origin` from another site, or `Sec-Fetch-Site: cross-site`). The setup page, curl and scripts send neither and are unaffected. `/mcp` is not covered by this check, because MCP clients are not browsers.

Limits to know about: the Host header is not proof of origin, so software on the same machine can claim to be local. The gate separates tunnels and proxies from local software, and a static key on a public URL narrows exposure but is not a secured cloud endpoint. Turn Remote access off and stop the tunnel when you are done. Not yet tested against a real Dev Tunnel or with Copilot Studio sending the key as a header.

### The run record

Each output records who produced it. `get_outputs` shows three fields per output:

- `executed_by`: for a step the engine ran on its own model, the provider, model and execution mode (recorded by the engine). For a delegated step, the name the assistant reported through the optional `executed_by` argument of `submit_response`, the client's user agent, whether the connection was local or remote, and the mode. The reported name is stored as given and is not checked.
- `validated_by`: the validator and its score, or why it was skipped (model API paused or not set up).
- `gate_answered_by`: for an approved human review gate, that it was answered through the connected client. The engine cannot tell a person's answer from the assistant answering for them, so it never records "human approved".

The record states facts and makes no claim of independent review: a run done by one assistant from start to finish reads that way.

### Finding an old session

A session can wait for weeks. `list_sessions` returns each session's title and a `request_preview` (the first 200 characters of the request it was started with), and `poll_workflow` and `get_outputs` return the same, so an assistant can find "the SAD about the bowling alley ecommerce tool" without a session ID. `get_output` on the Business Context Clarifier then gives the goal and scope. Nothing times out.

Config is stored at `~/.speakmanai/config.json`. The SQLite database is at `~/.speakmanai/speakmanai.db`. Logs are at `~/.speakmanai/speakmanai.log`.

---

## Setup Page

All modes expose a setup UI at `http://localhost:8000/setup`.

Use it to:
- Choose who runs the AI steps: your connected assistant (no API key) or your own model API. The choice appears once a model API is set up; before that your assistant runs them and there is nothing to choose. Adding a first model API switches to it
- Connect your assistant: pick it from the list to see its steps. Claude Code is one click; the Claude app, Copilot Studio, Cursor and Codex are manual
- Pick your AI provider (Gemini, Claude, OpenAI, Vertex AI, Ollama), under **Model API**, which is optional
- Enter your API key (or GCP project/region for Vertex AI, which uses Application Default Credentials instead) and choose fast / standard / advanced models
- Set the execution mode directly, under **Advanced** (shown once a model API is set up)
- Update an older database to this version's workflows (**Update workflows**, shown only when something differs)
- Turn **Remote access** on or off and copy the token, under **Advanced**
- Send feedback to SPEAKMAN.AI, under **Advanced**. It lists exactly what it sends (the type, your optional email, your message and the app version) and sends nothing until you press Send

The page footer shows the app version. Mention it in a bug report.

### Execution Modes

| Mode | Behaviour |
|---|---|
| **Auto** | Each agent runs with its own configured model tier. The provider and models chosen in setup determine what `fast` / `standard` / `advanced` resolve to. |
| **Force Delegate** | Every AI step pauses and is handed to your connected AI assistant (e.g. Claude Code) to execute instead of calling the LLM API. Useful when you have no API credits. |
| **Force Background** | Any steps configured to delegate to an AI assistant are overridden and run automatically using the provider above — fully hands-off. |
| **Delegate Only (API paused)** | Like Force Delegate, and the model API is paused. Nothing calls the provider, so there are no failures when you are out of credits, and your keys and provider settings are kept untouched. Switch back to Auto to resume. |

---

## LLM Provider Reference

| Provider | `LLM_PROVIDER` | Key variable | Fast (`DEFAULT_MODEL`) | Standard (`STANDARD_MODEL`) | Advanced (`ADVANCED_MODEL`) |
|---|---|---|---|---|---|
| Gemini | `gemini` | `GEMINI_API_KEY` | `gemini-3.5-flash-lite` | `gemini-3.8-flash` | `gemini-3.1-pro-preview` |
| Claude | `anthropic` | `ANTHROPIC_API_KEY` | `claude-haiku-5-5` | `claude-sonnet-5-5` | `claude-opus-5-5` |
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-6-luna` | `gpt-6.1-sol` | `gpt-6-astra` |
| Ollama | `ollama` | *(none)* | set via `DEFAULT_MODEL` | set via `STANDARD_MODEL` | set via `ADVANCED_MODEL` |
| Vertex AI | `vertexai` | `GCP_PROJECT_ID` + `GCP_REGION` | `gemini-3.5-flash-lite` | `gemini-3.8-flash` | `gemini-3.1-pro-preview` |

### Model tiers

All workflow agents use abstract tiers (`fast`, `standard`, `advanced`) rather than hardcoded model names. Your `DEFAULT_MODEL`, `STANDARD_MODEL`, and `ADVANCED_MODEL` settings are the authority — changing provider or model in setup takes effect immediately without editing any workflow files. Leaving `STANDARD_MODEL` or `ADVANCED_MODEL` blank falls back to each provider's built-in default for that tier (see table above).

| Tier | Usage |
|---|---|
| `fast` | Simple formatting, classification, validators — maps to `DEFAULT_MODEL` |
| `standard` | Content generation, summarisation, analysis — maps to `STANDARD_MODEL` |
| `advanced` | Complex reasoning, architecture, multi-step synthesis — maps to `ADVANCED_MODEL` |

### Optional: newer / higher-cost models

`DEFAULT_MODEL`, `STANDARD_MODEL`, and `ADVANCED_MODEL` accept any model name your provider account has access to — you are not limited to the table above. Newer, more capable models are also supported and selectable on the setup page for Gemini and Claude:

- **Gemini 3 family** — e.g. `gemini-3.1-flash-lite` (GA as of May 2026 — note: no `-preview` suffix, that alias has been retired), `gemini-3-flash-preview`, `gemini-3.1-pro-preview` (Vertex AI: global location only)
- **Earlier Claude and OpenAI models** — `claude-sonnet-4-6`, `claude-opus-4-8`, `claude-sonnet-5`, `claude-fable-5`, `gpt-4o`, `gpt-4o-mini` stay selectable on the setup page for Claude and OpenAI

These generally improve output quality but cost more per call than the defaults above — use them for `STANDARD_MODEL` / `ADVANCED_MODEL` on workflows where the extra quality is worth the spend. Preview model names change as Google/Anthropic promote them to general availability — if you get a 404/"model not found" on one of these, check whether the `-preview` suffix has been dropped.

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
Use Delegate Only mode — your connected AI assistant (Claude Code) executes all AI steps and the model API is paused, so your keys stay saved but are never used. Set it in the setup page or call `set_execution_mode("delegate_only")` via MCP, and switch back to Auto when you have credits again. Validators are not delegated: while the API is paused (or when no model API is configured at all) they auto-pass, and the run record carries a `validation_skipped` marker for each skipped step. `generate_document` needs the model and is unavailable in this mode. Plain Force Delegate also hands AI steps to your assistant, but it still calls a configured model for validators.

**Desktop exe logs**
If the exe starts but something isn't working, check `~/.speakmanai/speakmanai.log` for details.

**Poor or missing output when using Ollama**
SPEAKMAN.AI runs complex multi-agent workflows that require strong reasoning and instruction-following at every step. Small or general-purpose models frequently produce incomplete JSON, skip sections, or fail validation entirely — resulting in empty or degraded outputs. This is a model capability issue, not a bug.

Minimum recommended hardware and model for reliable results:
- **Model**: 14B parameter minimum — `qwen2.5:14b` or `llama3.3:70b` for best results
- **RAM**: 16 GB minimum for a 14B model; 32 GB+ recommended for 32B models
- **Thinking/reasoning models** (e.g. `qwen3:14b`, `deepseek-r1:14b`) produce significantly better results than standard instruction-tuned models of the same size

If you are seeing empty outputs or validation failures, try a larger model before troubleshooting anything else.
