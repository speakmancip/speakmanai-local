# Project notes for Claude Code

## Rule: no em dashes or en dashes in prompt text

Agent prompts must not contain em dashes (`—`) or en dashes (`–`). This covers every `systemPrompt` and `description` in `WorkflowsAndAgents/*.json`, plus any string in the code that a model reads:

- in `orchestration/mcp-server/engine.py`, the planner prompt, `PRESERVE_INSTRUCTION`, and the injected validator, HITL and update-cascade feedback
- in `orchestration/mcp-server/server.py`, the MCP server instructions, tool docstrings (clients see them as tool descriptions), tool result messages and `@mcp.prompt` text

Use a spaced hyphen (` - `), a colon, or a comma instead. Write number ranges as `2-5`. Prefer plain ASCII punctuation generally, e.g. `->` rather than `→`.

**Why:** set by the project owner on 2026-10-09. Punctuation in a prompt leaks into what the model writes, and em dashes are an AI-writing tell the generated documents shouldn't carry. Non-ASCII punctuation has also already broken once. From v1.2.0 to v1.4.0, `MCP_SOLUTION_ARCHITECTURE_V1.json` and `MCP_CAPABILITY_GENERATOR_V1.json` shipped with every dash and arrow garbled (`â€”`, `â†’`) after being saved through a cp1252 round trip. Fixed in v1.4.1.

**Applies to:** new agents, edits to existing prompts, imported workflows, and any cleanup or refactor pass. A tidy-up must not "restore" em dashes for style. Comments, docstrings that aren't tool-facing, log lines and the `/setup` page UI aren't prompts, so they're out of scope.

**Check before committing** (Git Bash; must print nothing). It matches the UTF-8 bytes of em/en dashes and curly quotes (`E2 80 ..`) and the cp1252-garbled form (`C3 A2 ..`):

```bash
LC_ALL=C grep -l -e $'\xe2\x80' -e $'\xc3\xa2' WorkflowsAndAgents/*.json
```

Shipped workflow files only reach **new** installs. `seed_if_empty` never overwrites a workflow already in a user's database, so a prompt fix also has to be re-imported (`import_workflow` / `import_agent`) into any existing local database you test against.
