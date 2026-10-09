# SPEAKMAN.AI — Local Multi-Agent Workflow Engine

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**SPEAKMAN.AI** is a local multi-agent workflow engine exposed entirely via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io). Connect it to your AI assistant — Claude Code, Claude Desktop, Cursor, or Microsoft Copilot Studio — and run sophisticated multi-step AI pipelines directly from your tools, against your own LLM provider, on your own machine.

Built-in workflows cover the whole idea-to-architecture arc — **Sizing**, **Solution Architecture**, **Requirements Engineering**, **UX Design** — plus a **Workflow Builder** for creating your own pipelines. Every workflow is a coordinated chain of AI agents that collaborate, validate each other's outputs, and (where the task calls for it) pause for a real human review before continuing.

**Latest release: [v1.4.0](https://github.com/speakmancip/speakmanai-local/releases/tag/v1.4.0)**: Gemini 2.x is gone (Google is retiring it this month) and the Gemini/Vertex defaults move to the 3.x line, with saved configs migrated automatically on startup. Also fixes `update_session` cascades that left downstream agents unchanged. See [release notes](RELEASE_NOTES.md#v140).

---

## Why SPEAKMAN.AI?

Most AI tools give you a single model responding to a single prompt. SPEAKMAN.AI gives you a **pipeline of specialist agents** — each with a focused role, its own system prompt, and awareness of what the agents before it produced. The result is dramatically better output quality for complex tasks that benefit from sequential reasoning, review, and synthesis.

- **No cloud dependency** — runs entirely on your machine against your own API keys
- **Provider agnostic** — Gemini, Claude, OpenAI, or Ollama (local models)
- **MCP native** — surfaces as tools in any MCP-compatible AI assistant
- **Build your own** — design custom workflows with the built-in Workflow Builder
- **Human-in-the-loop** — workflows can pause for your input mid-pipeline, or for a real review-and-revise gate on a specific agent's output
- **Execution flexibility** — run steps with your backend LLM, or hand them to your AI assistant to execute using its local tools

---

## Quickstart — Windows Desktop (Recommended)

No Python, no Docker, no terminal required.

1. Download `SpeakmanAI.exe` from the [releases page](https://github.com/speakmancip/speakmanai-local/releases)
2. Run it — a system tray icon appears and your browser opens automatically
3. Choose your AI provider and enter your API key on the setup page
4. Click **Add to Claude Code** (or Claude Desktop / Cursor) to connect your assistant
5. Ask your assistant: *"List the available SPEAKMAN.AI workflows and start a session"*

Config is saved to `~/.speakmanai/config.json`. Logs at `~/.speakmanai/speakmanai.log`.

---

## Other Deployment Options

| Mode | Best for | Details |
|---|---|---|
| **Windows exe** | End users, non-technical teams | No dependencies — download and run |
| **Docker + SQLite** | Developers, local testing | `USE_SQLITE=true docker-compose up --build` |
| **Docker + MongoDB** | Teams, persistent data | `docker-compose --profile mongo up --build` |
| **Dev / no Docker** | Contributors | `uvicorn server:app --port 8000` |

See [SETUP.md](SETUP.md) for full configuration details on each mode.

---

## Built-in Workflows

### MCP_CAPABILITY_GENERATOR_V1
Converts a plain-text description of your tech stack and business processes into a structured capabilities JSON array — the standard input format for the architecture workflow.

**Runtime:** ~2–5 minutes

### MCP_SIZING_ESTIMATE_V1
A lightweight pre-architecture gate. Clarifies a raw business idea, decomposes it into high-level functional components, and produces a relative T-shirt-sized (S/M/L/XL) estimate per component and overall — with a human review-and-revise gate — before anyone commits to a full architecture engagement. Deliberately outputs relative sizes only, never dollar figures; mapping size to cost stays a business decision, not a hardcoded prompt.

**Runtime:** ~5–10 minutes

### MCP_SOLUTION_ARCHITECTURE_V1
A full multi-agent Solution Architecture Document (SAD) pipeline. Specialist agents collaborate to produce business analysis, application architecture, infrastructure design, compliance assessment, Mermaid diagrams, and a complete written document, with human review gates at each major stage.

**Runtime:** ~10–20 minutes

### MCP_REQUIREMENTS_ENGINEERING_V1
Turns a completed architecture (or a business description alone) into implementation-ready technical contracts — domain model, use case catalog, field definitions, naming dictionary, and a full OpenAPI schema — with validation loops checking each stage against the one before it.

**Runtime:** ~10–15 minutes

### MCP_UX_DESIGN_V1
Maps architecture components and requirements into a UI screen inventory, checks that every requirement traces to a real screen, then proposes a design direction and produces clickable, self-contained HTML mockups for human review before any code gets written. The mockup step runs through your connected coding agent rather than a fixed background model.

**Runtime:** ~5–10 minutes

### WORKFLOW_CREATOR_V1 / CLAUDE_WORKFLOW_CREATOR_V1
Design and generate new SPEAKMAN.AI workflow definitions. Describe what you want to automate and the workflow builder produces a ready-to-import workflow JSON with agent system prompts, including `AI_VALIDATOR` and `HITL_VALIDATOR` gates where they fit.

**Runtime:** ~5–10 minutes

### CLAUDE_SKILL_WORKFLOW_CREATOR_V1
The delegate-first sibling of the workflow builder above. Produces workflows where any agent needing external data or real tool access runs directly through your connected coding agent (`executionMode: "delegate"`) instead of pausing for a human to paste data in.

**Runtime:** ~5–10 minutes

---

## LLM Provider Support

| Provider | Fast | Standard | Advanced |
|---|---|---|---|
| **Gemini** (recommended) | `gemini-3.5-flash-lite` | `gemini-3.8-flash` | `gemini-3.1-pro-preview` |
| **Claude** | `claude-haiku-4-5` | `claude-sonnet-4-6` | `claude-opus-4-8` |
| **OpenAI** | `gpt-4o-mini` | `gpt-4o` | `gpt-4o` |
| **Ollama** | Local inference — 14B+ model strongly recommended (see below) | | |

All workflow agents use abstract model tiers (`fast`, `standard`, `advanced`) — no workflow files need editing when you switch providers. Changes via the setup page take effect immediately.

### Ollama Requirements
SPEAKMAN.AI runs complex multi-step reasoning pipelines. Small models (under 14B parameters) frequently produce incomplete or invalid outputs for multi-agent workflows.

**Minimum recommended:** `qwen2.5:14b` with 16 GB RAM  
**Best results:** thinking/reasoning models such as `qwen3:14b` or `deepseek-r1:14b`

---

## Building Custom Workflows

Workflows are defined in JSON — a workflow definition and an array of agents. The Workflow Builder (`WORKFLOW_CREATOR_V1`) can generate these for you, or write them manually.

```json
{
  "workflow": {
    "workflowId": "MY_CUSTOM_WORKFLOW",
    "displayName": "Code Review Pipeline",
    "description": "Reviews code for security issues and proposes architecture changes.",
    "workflowType": "mcp"
  },
  "agents": [
    {
      "agentId": "SECURITY_REVIEWER",
      "agentType": "AI_WORKFLOW",
      "model": "advanced",
      "systemPrompt": "You are an expert security reviewer...",
      "dependencies": []
    },
    {
      "agentId": "HUMAN_APPROVAL",
      "agentType": "MCP_INPUT_REQUIRED",
      "systemPrompt": "Review the findings. Do you want to proceed with the refactor?",
      "dependencies": ["SECURITY_REVIEWER"]
    },
    {
      "agentId": "ARCHITECT",
      "agentType": "AI_WORKFLOW",
      "model": "advanced",
      "systemPrompt": "Based on the approved findings, propose architecture changes...",
      "dependencies": ["HUMAN_APPROVAL"]
    }
  ]
}
```

### Agent Types

| Type | Behaviour |
|---|---|
| `AI_WORKFLOW` | Executes silently using the configured backend LLM |
| `MCP_INPUT_REQUIRED` | Pauses the workflow and requests input from the user via the AI assistant |
| `MCP_LLM_DELEGATE` | Hands the step to the connected AI assistant — giving it access to local tools like file system, bash, and search |

### Execution Modes

| Mode | Behaviour |
|---|---|
| **Auto** | Each agent runs as configured — background LLM or delegate as defined |
| **Force Delegate** | Every AI step is handed to your connected assistant regardless of agent config |
| **Force Background** | Every step runs against the configured LLM — delegation overridden |

Switch modes at any time via the setup page or: *"Set execution mode to force_delegate"*

---

## MCP Tool Reference

| Tool | Description |
|---|---|
| `list_workflows` | List all available workflows |
| `start_session` | Start a new workflow pipeline — returns a session_id immediately |
| `poll_workflow` | Check status of a running session |
| `submit_response` | Provide input when a workflow reaches AWAITING_INPUT |
| `get_outputs` | Get the manifest of completed agent outputs |
| `get_output` | Fetch the full content of a specific agent's output |
| `list_sessions` | List recent sessions — use to recover a lost session_id |
| `cancel_session` | Cancel a stuck or failed session |
| `update_session` | Revise one agent's output in a completed session and cascade the change through everything downstream — no re-run from scratch |
| `set_execution_mode` | Switch between auto / force_delegate / force_background |
| `import_architecture_plan` | Import a workflow JSON into the database |
| `import_workflow` | Import a workflow definition only |
| `import_agent` | Import or update a single agent |
| `get_project` | Retrieve a completed project record |
| `list_projects` | List recent completed projects |

---

## Repository Structure

```
speakmanai-local/
├── launcher.py                          # Windows exe entry point
├── speakmanai.spec                      # PyInstaller build spec
├── docker-compose.yml                   # Docker deployment
├── .env.template                        # Environment config template
├── SETUP.md                             # Full deployment and config guide
├── WorkflowsAndAgents/                  # Built-in workflow definitions (JSON)
│   ├── MCP_SOLUTION_ARCHITECTURE_V1.json
│   ├── MCP_CAPABILITY_GENERATOR_V1.json
│   ├── WORKFLOW_CREATOR_V1.json
│   └── CLAUDE_WORKFLOW_CREATOR_V1.json
└── orchestration/mcp-server/
    ├── server.py                        # FastAPI + MCP server, setup UI
    ├── engine.py                        # Workflow execution engine
    ├── database.py                      # Database router (SQLite / MongoDB)
    ├── database_sqlite.py               # SQLite async adapter
    ├── requirements.txt                 # Docker/server dependencies
    └── requirements-desktop.txt        # Desktop exe dependencies
```

---

## Building the Exe

```bash
python -m venv venv-desktop
source venv-desktop/Scripts/activate    # Git Bash / Mac/Linux
# venv-desktop\Scripts\activate.bat    # Windows CMD

pip install -r orchestration/mcp-server/requirements-desktop.txt
python -m PyInstaller speakmanai.spec
```

Output: `dist/SpeakmanAI.exe`

---

## Health Check

```bash
curl http://localhost:8000/health
# {"status": "ok", "service": "speakmanai-mcp-server"}
```

---

## Skills & Document Generation

**[speakmanai-cc](https://github.com/speakmancip/speakmanai-cc)** is a companion repository of Claude Code skills that work with any SPEAKMAN.AI deployment — local, Docker, or cloud.

| Skill | Command | What it does |
|---|---|---|
| Project Brief | `/generate-project-brief` | Coaches a raw idea into a structured brief before it ever needs a SPEAKMAN.AI session |
| Solution Architecture | `/generate-sad` | Runs the architecture workflow and renders a branded HTML + PDF document locally |
| Requirements Engineer | `/generate-requirements` | Runs the requirements workflow and saves implementation-ready coding contracts |
| UX & Interface Design | `/generate-ux-design` | Runs the UX design workflow and saves the screen inventory, flow diagrams, and mockups |
| Compliance Report | `/generate-compliance-report` | Runs a compliance workflow and produces a risk assessment report |
| Workflow Creator | `/generate-speakmanai-workflow` | Designs and exports a new workflow definition ready for import |

This is a sample, not the full list — 17 skills in total, covering the whole SDLC from idea intake through code generation, infrastructure, and pentesting. Skills install globally into Claude Code and work from any project directory. See the [speakmanai-cc README](https://github.com/speakmancip/speakmanai-cc) for the complete list and installation instructions.

---

## Contributing

The workflow engine, MCP tools, and agent types are all designed to be extended. The most impactful contributions are new workflow definitions in `WorkflowsAndAgents/` — if you build something useful, share it.

For bugs and feature requests, open an issue.
