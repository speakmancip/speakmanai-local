"""
SPEAKMAN.AI MCP Server
Exposes SPEAKMAN.AI workflows to remote agents via the Model Context Protocol.
Transport: Streamable HTTP (stateless, per-request auth)
"""
import asyncio
import os
import re
import sys
import json
import shutil
import subprocess
import logging
import uuid
import httpx
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote
import json as _json

from dotenv import load_dotenv
if not getattr(sys, "_MEIPASS", None):
    # Desktop exe is fully driven by ~/.speakmanai/config.json (see launcher.py) —
    # skip .env entirely so it never inherits Docker-only settings (e.g. a container
    # GOOGLE_APPLICATION_CREDENTIALS path) from an unrelated .env in/above the CWD.
    load_dotenv()

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

import skills_installer
import workflows_sync
from database import get_db, get_user_db, close as close_db
from engine import (
    process_local_event,
    list_projects_local,
    get_project_local,
    generate_document_local,
    render_document_local,
    resolve_agent_step_index,
    transition_completed_or_cancelled_to_draft,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# --- Integrated Workflow Queue ---
workflow_queue = asyncio.Queue()

async def local_workflow_consumer():
    """Background task that queues events"""
    while True:
        event = await workflow_queue.get()
        log.info(f"Local Workflow Engine picked up event: {event['action']} for session {event.get('session_id')}")
        try:
            await process_local_event(event, workflow_queue)
        except Exception as e:
            log.error(f"Error processing event {event['action']}: {e}", exc_info=True)
        finally:
            workflow_queue.task_done()

# --- PII Scrubber ---
# Applied to all user-supplied text before forwarding to workflow pipeline.
_PII_PATTERNS = {
    "AWS_KEY":      (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    "PRIVATE_KEY":  (re.compile(r"-----BEGIN [A-Z]+ PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY_BLOCK]"),
    "CREDIT_CARD":  (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "[REDACTED_CREDIT_CARD]"),
    "SSN_SIN":      (re.compile(r"\b(?:\d{3}[-\s]\d{2}[-\s]\d{4}|\d{3}[-\s]\d{3}[-\s]\d{3})\b"), "[REDACTED_GOV_ID]"),
    "EMAIL":        (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    "PHONE":        (re.compile(r"(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}\b"), "[REDACTED_PHONE]"),
}

def scrub_pii(text: str) -> str:
    """Redact PII and secrets from a string before sending to the workflow pipeline."""
    if not text or not isinstance(text, str):
        return text
    for pattern, replacement in _PII_PATTERNS.values():
        text = pattern.sub(replacement, text)
    return text



# --- MCP Server ---
# Explicitly disable DNS rebinding protection — FastMCP enables it by default for
# localhost hosts, which rejects external hostnames with 421. 
mcp = FastMCP(
    name="speakmanai",
    stateless_http=True,  # Each request is independent — no in-memory session state.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    instructions=(
        "SPEAKMAN.AI is a multi-agent workflow engine. You can run built-in workflows (e.g. Solution "
        "Architecture, Capability Generator) or any custom workflow built with the Workflow Builder. "
        "Each workflow is a pipeline of AI agents that collaborate to produce structured outputs.\n\n"
        "WORKFLOW CALL CHAINS - follow exactly for the workflow type you are running:\n\n"
        "Standard workflow (most workflows, including MCP_SOLUTION_ARCHITECTURE_V1 and custom workflows):\n"
        "  1. list_workflows()  →  choose a workflow_id\n"
        "  2. start_session(workflow_id, title, description)  →  returns session_id immediately\n"
        "  3. poll_workflow(session_id) every 30s  →  wait for status AWAITING_INPUT or COMPLETED\n"
        "  4. When AWAITING_INPUT: review input_required.prompt and input_required.schema\n"
        "     Build the requested data matching the schema\n"
        "  5. submit_response(session_id, response, pause_id=input_required.pause_id)  →  pipeline continues\n"
        "  6. poll_workflow(session_id) every 30-60s  →  wait for status COMPLETED\n"
        "  7. get_outputs(session_id)  →  returns manifest: available_outputs[], document_capabilities[], suggested_next_steps[]\n"
        "  8. get_output(session_id, agent_id)  →  full content of one agent (call once per agent you need)\n\n"
        "Capability Generator workflow (MCP_CAPABILITY_GENERATOR_V1):\n"
        "  1. list_workflows()  →  find MCP_CAPABILITY_GENERATOR_V1\n"
        "  2. start_session(workflow_id, title=<group name>, description=<capabilities JSON array as string>)\n"
        "     → returns session_id immediately (description IS the capabilities payload)\n"
        "  3. poll_workflow(session_id)  →  wait for COMPLETED (~2-5 min)\n"
        "  !! DO NOT call submit_response for Capability Generator - no AWAITING_INPUT step !!\n\n"
        "poll_workflow returns status and completed_steps ONLY - never full content. "
        "Always call get_outputs then get_output to retrieve content.\n\n"
        "When poll_workflow returns status=AWAITING_INPUT, it includes an input_required block "
        "with prompt, schema, context, and pause_id. Build your response and call submit_response, "
        "always passing pause_id so the response can only apply to the pause you actually read.\n\n"
        "Agent outputs are fetched individually to keep each response within context limits. "
        "get_outputs returns the manifest only; get_output returns one agent's full content.\n\n"
        "get_outputs works on any past session_id - use it to resume downstream work across conversations.\n\n"
        "If a session is stuck or failed, call cancel_session(session_id) to free your concurrency slot.\n\n"
        "If you lose a session_id (context reset, new conversation), call list_sessions() to recover it.\n\n"
        "SKILLS: this server can also deliver skills, which are complete step-by-step instructions for "
        "producing a finished deliverable from a workflow (for example a Solution Architecture Document, "
        "a compliance report, requirements, a UX design, generated code). When the user asks for a "
        "deliverable like that and your client does not already have a SPEAKMAN.AI skill installed for it, "
        "call list_skills(), then get_skill(name), and follow the returned instructions exactly instead of "
        "improvising the steps. If your client already has the skill installed (for example Claude Code "
        "with /generate-sad), use the installed skill."
    )
)

# ─────────────────────────────────────────────
# Session id handling
# ─────────────────────────────────────────────
def _need_session_id(session_id: str = "", sid: str = "") -> str:
    """Accept the session id under either name. Some clients drop an argument named session_id
    before the call reaches this server, so every tool that takes one also accepts sid."""
    value = (session_id or sid or "").strip()
    if not value:
        log.warning("Tool call arrived with no session id under either session_id or sid.")
        raise ValueError(
            "No session id received. Pass the session id as session_id. If your client drops "
            "arguments named session_id, pass the same value as sid instead."
        )
    return value


# ─────────────────────────────────────────────
# Tool 1: list_workflows
# ─────────────────────────────────────────────
@mcp.tool(name="list_workflows", annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def list_workflows() -> str:
    """
    List all available SPEAKMAN.AI workflows.
    Returns workflow IDs, names, descriptions, and credit cost.

    Returns all available workflows including built-in and any custom workflows you have created.
    Use the returned workflow_id to start a session.
    """
    db = get_db()

    workflows = await db["workflows"].find(
        {"workflowType": "mcp"},
        {"_id": 0, "workflowId": 1, "displayName": 1, "description": 1}
    ).to_list(length=50)

    return json.dumps(workflows)



# ─────────────────────────────────────────────
# Tool 3: start_session
# ─────────────────────────────────────────────
@mcp.tool(name="start_session", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
async def start_session(
    workflow_id: str,
    title: str,
    description: str,
) -> str:
    """
    Start a new workflow session and return a session_id immediately.
    The workflow runs asynchronously - poll_workflow to track progress.

    Args:
        workflow_id: The workflow ID from list_workflows.
        title: Short title for this session (max 100 chars).
            For Capability Generator: use this as the capability group name.
        description: Context or payload for the workflow (max 10000 chars).
            - Most workflows: a plain-text description of what you want to accomplish.
            - Capability Generator (MCP_CAPABILITY_GENERATOR_V1): pass the capabilities JSON array
              as a string - this IS the payload, not a description.

    Returns:
        session_id, credit_balance, and next_step instructions.
        Some workflows will reach AWAITING_INPUT status and require a submit_response call before completing.
    """
    desc_limit = 10000
    if len(description) > desc_limit:
        raise ValueError(f"description must be {desc_limit} characters or less.")
    if len(title) > 100:
        raise ValueError("title must be 100 characters or less.")

    session_id = f"session-{uuid.uuid4()}"

    # Scrub PII from user-supplied text before it enters the workflow pipeline.
    clean_description = scrub_pii(description)
    clean_title = scrub_pii(title)

    # Enqueue workflow for async processing
    await workflow_queue.put({
        "action": "start",
        "prompt": clean_description,
        "workflowId": workflow_id,
        "session_id": session_id,
        "session_title": clean_title,
        "user_id": "local_user"
    })

    is_capability_generator = "CAPABILITY_GENERATOR" in workflow_id.upper()

    if is_capability_generator:
        next_step = (
            "Capability Generator is running. Call poll_workflow(session_id) to check status. "
            "Typical runtime: 2-5 minutes. DO NOT call submit_response - no AWAITING_INPUT step."
        )
    else:
        next_step = (
            "Workflow started. Call poll_workflow(session_id) every 15-30 seconds. "
            "When status is AWAITING_INPUT, read input_required.prompt and input_required.schema, "
            "build your response, then call submit_response(session_id, response, pause_id=input_required.pause_id). "
            "After submission, poll again until COMPLETED."
        )

    return json.dumps({
        "session_id": session_id,
        "next_step": next_step,
    }, indent=2)


# ─────────────────────────────────────────────
# Tool 3: submit_response  (generic HITL resume)
# ─────────────────────────────────────────────
@mcp.tool(name="submit_response", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
async def submit_response(
    response: str,
    session_id: str = "",
    sid: str = "",
    pause_id: str = "",
) -> str:
    """
    Submit your response to a workflow that is waiting for input (status=AWAITING_INPUT).

    Call poll_workflow first - when it returns status=AWAITING_INPUT, read the input_required
    block (prompt, schema, context) to understand what to provide, then call this tool.

    For architecture workflows the response is a JSON array of capabilities.
    For other workflows the response is whatever input_required.prompt asks for.

    !! Only call this when poll_workflow returns status=AWAITING_INPUT !!
    !! DO NOT call for Capability Generator!!

    Args:
        session_id: The session_id from start_session
        sid:        Same as session_id. Use it only if your client drops arguments named session_id.
        response:   Your response as a string (JSON-encoded array/object for structured inputs)
        pause_id:   input_required.pause_id from the poll_workflow call you're answering.
                    Optional, but always pass it: it guarantees your response is applied to the
                    pause you actually read, and is rejected if the workflow has since moved on
                    (e.g. a repeated submit would otherwise land on the NEXT pause).
    """
    session_id = _need_session_id(session_id, sid)
    if not response or not response.strip():
        raise ValueError("response must be a non-empty string.")
    if len(response) > 50000:
        raise ValueError("response payload exceeds 50,000 character limit.")

    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    COLLECTION = os.environ.get("RAW_EVENTS_COLLECTION", "events_raw")

    doc = await db[COLLECTION].find_one({"session_id": session_id})
    if not doc:
        raise RuntimeError(f"Session {session_id} not found.")

    mcp_input_config = await _get_mcp_pause_config(doc)
    if not mcp_input_config:
        current_status = doc.get("current_status", "UNKNOWN")
        raise RuntimeError(
            f"Session {session_id} is not waiting for input (status: {current_status}). "
            "Only call submit_response when poll_workflow returns status=AWAITING_INPUT."
        )

    current_pause_id = mcp_input_config.get("pause_id", "")
    if pause_id and pause_id != current_pause_id:
        raise RuntimeError(
            f"Stale pause_id for session {session_id}: this response was for pause {pause_id}, but the "
            f"workflow is now waiting on pause {current_pause_id} ({mcp_input_config.get('agent_id', '')}). "
            "It has probably already been answered. Call poll_workflow to see what's needed now."
        )

    # Atomically claim the pause (AWAITING_INPUT -> IN_PROGRESS) BEFORE queueing the resume.
    # Writing it afterwards could stomp a status the engine had already moved on — e.g. straight
    # to the NEXT delegate pause, which is AWAITING_INPUT again, leaving poll_workflow showing
    # IN_PROGRESS forever. Claiming first also rejects a duplicate submit for the same pause.
    coll = db[COLLECTION]
    if hasattr(coll, "update_one_if"):
        claimed = await coll.update_one_if(
            {"session_id": session_id}, {"$set": {"current_status": "IN_PROGRESS"}},
            expected_field="current_status", expected_values=["AWAITING_INPUT"],
        )
    else:
        result = await coll.update_one(
            {"session_id": session_id, "current_status": "AWAITING_INPUT"},
            {"$set": {"current_status": "IN_PROGRESS"}},
        )
        claimed = getattr(result, "matched_count", 0) > 0
    if not claimed:
        current_status = doc.get("current_status", "UNKNOWN")
        raise RuntimeError(
            f"Session {session_id} is not waiting for input (status: {current_status}) - "
            "a response may already have been submitted for this step. Call poll_workflow first."
        )

    # The claim only checks status, not WHICH pause. If a previous submit's resume wrote a new
    # pause between our read above and the claim, we'd now hold that new pause while having
    # resolved routing for the old one. Re-read and confirm; if it moved, release the claim
    # (safe: nothing has been queued yet, so nothing else can touch the session) and reject.
    fresh_doc = await coll.find_one({"session_id": session_id})
    fresh_pause = _latest_pause_event((fresh_doc or {}).get("events") or [])
    if (fresh_pause or {}).get("event_id", "") != current_pause_id:
        await coll.update_one({"session_id": session_id}, {"$set": {"current_status": "AWAITING_INPUT"}})
        raise RuntimeError(
            f"Session {session_id} moved on to a new pause while this response was being submitted. "
            "Call poll_workflow to see what's needed now."
        )

    step_index = mcp_input_config.get("step_index", 1)
    agent_id = mcp_input_config.get("agent_id", "MCP_INPUT_REQUIRED")

    if mcp_input_config.get("pause_kind") == "hitl_feedback":
        # Human-review gate — routes to the origin agent's revise-or-advance loop, not a
        # normal DAG step resume.
        await workflow_queue.put({
            "action": "hitl_response",
            "session_id": session_id,
            "response": response,
            "current_step_index": step_index,
            "hitl_agent_id": agent_id,
            "origin_agent_id": mcp_input_config.get("origin_agent_id", ""),
            "user_id": "local_user",
        })
    else:
        # Push resume event to internal local queue
        await workflow_queue.put({
            "action": "resume",
            "session_id": session_id,
            "content": response,
            "current_step_index": step_index,
            "agentId": agent_id,
            "user_id": "local_user"
        })

    return json.dumps({
        "ok": True,
        "session_id": session_id,
        "message": (
            "Response accepted. The workflow is now continuing. "
            "Call poll_workflow(session_id) every 15-30 seconds until status is COMPLETED."
        ),
    })



# ─────────────────────────────────────────────
# Tool 5: poll_workflow
# ─────────────────────────────────────────────
@mcp.tool(name="poll_workflow", annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def poll_workflow(mcp_ctx: Context, session_id: str = "", sid: str = "") -> str:
    """
    Check the status of a running workflow and retrieve outputs when complete.

    Args:
        session_id: The session_id returned by start_session
        sid:        Same as session_id. Use it only if your client drops arguments named session_id.

    Returns:
        status (IN_PROGRESS | AWAITING_CAPABILITIES | COMPLETED | FAILED),
        completed_steps list, and outputs dict keyed by agent_id when COMPLETED.
    """
    session_id = _need_session_id(session_id, sid)
    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    COLLECTION = os.environ.get("RAW_EVENTS_COLLECTION", "events_raw")

    doc = await db[COLLECTION].find_one({"session_id": session_id})
    if not doc:
        return json.dumps({
            "session_id": session_id,
            "status": "IN_PROGRESS",
            "completed_steps": [],
            "message": "Session is starting - wait 30 seconds and poll again.",
        }, indent=2)

    current_status = doc.get("current_status", "UNKNOWN")
    events = doc.get("events") or []
    mcp_input_config = await _get_mcp_pause_config(doc) if current_status == "AWAITING_INPUT" else None

    additional_exclusions = set()
    if mcp_input_config:
        additional_exclusions.add(mcp_input_config.get("agent_id", ""))

    human_agents = await _build_human_exclusion_set(events, additional_exclusions)
    agent_index = _aggregate_agent_index(events, human_agents)
    # completed_steps ordered by each agent's most recent output, so agents re-run by an
    # update_session cascade move to the end, reflecting what just ran
    # rather than workflow step order.
    completed_steps = [
        aid for aid, _ in sorted(agent_index.items(), key=lambda x: x[1]["index"])
    ]

    # Emit progress notifications — one per completed agent
    wf_def = next((e.get("data", {}).get("workflow_definition", {}) for e in events if e.get("data", {}).get("workflow_definition")), {})
    total_agents = len(wf_def.get("steps", [])) or len(completed_steps)
    for i, agent_id in enumerate(completed_steps):
        await mcp_ctx.report_progress(
            progress=i + 1,
            total=total_agents,
            message=f"[{i + 1}/{total_agents}] {agent_id}",
        )

    is_completed = current_status == "COMPLETED"
    is_failed = current_status in ("FAILED", "ERROR")

    # Determine whether this session is a system workflow (never incremented, so don't decrement).
    session_workflow_type = next(
        (e.get("data", {}).get("workflow_definition", {}).get("workflow_type")
         for e in events if e.get("data", {}).get("workflow_definition")),
        None
    )
    is_system_session = session_workflow_type == "system"

    if is_failed:
        error_msg = doc.get("error_message", "Workflow failed - check agent configuration.")
        raise RuntimeError(f"Workflow {session_id} failed: {error_msg}")

    is_awaiting_input = current_status == "AWAITING_INPUT"

    result = {
        "session_id": session_id,
        "status": current_status,
        "completed_steps": completed_steps,
    }

    if is_awaiting_input and mcp_input_config:
        agent_type = mcp_input_config.get("agent_type", "MCP_INPUT_REQUIRED")
        result["input_required"] = {
            "prompt": mcp_input_config.get("prompt", ""),
            "schema": mcp_input_config.get("schema", {}),
            "context": mcp_input_config.get("context", ""),
            "agent_id": mcp_input_config.get("agent_id", ""),
            "agent_type": agent_type,
            "pause_id": mcp_input_config.get("pause_id", ""),
        }
        if mcp_input_config.get("origin_agent_id"):
            result["input_required"]["origin_agent_id"] = mcp_input_config["origin_agent_id"]
        if mcp_input_config.get("context_refs"):
            result["input_required"]["context_refs"] = mcp_input_config["context_refs"]

        if agent_type == "MCP_LLM_DELEGATE":
            result["message"] = (
                "The workflow has paused and is delegating this execution step to YOU (the connected AI assistant). "
                "Do NOT ask the user for this information. Read input_required.prompt, input_required.context, "
                "and input_required.schema. Execute the task yourself using your own capabilities, "
                "then immediately call submit_response(session_id, response, pause_id=input_required.pause_id) with the result. "
                "If input_required.context_refs is present, the context lists large earlier outputs by agent_id instead of repeating them: use your own copy only if you hold the complete text, otherwise call get_output for each."
            )
        elif agent_type == "HITL_VALIDATOR":
            result["message"] = (
                "A human review checkpoint has been reached for the output of "
                f"{mcp_input_config.get('origin_agent_id', 'the prior step')}. Read input_required.context "
                "(the output under review, plus any named source-of-truth material) and input_required.prompt "
                "(what to look for). Do NOT silently approve on your own judgment - surface the output to the "
                "actual user, ask clarifying questions if needed, and proactively point out any gaps or risks "
                "you notice. Once the user has responded, call submit_response(session_id, response, "
                "pause_id=input_required.pause_id) with "
                '{"status": "Approved"} or {"status": "Update", "feedback": "<what to change>"} per '
                "input_required.schema."
            )
        else:
            result["message"] = (
                "Workflow is waiting for human input. Ask the user to provide the information requested in "
                "input_required.prompt. Once they answer, format it according to input_required.schema "
                "and call submit_response(session_id, response, pause_id=input_required.pause_id)."
            )
    elif is_completed:
        result["message"] = (
            "Workflow complete. Call get_outputs(session_id) to get the manifest of available agent outputs, "
            "then call get_output(session_id, agent_id) for each output you need. "
        )
    else:
        result["message"] = f"Workflow is {current_status}. Poll again in 30-60 seconds."

    return json.dumps(result, indent=2)



# A delegated pause normally carries the full text of every output the step depends on. Past this
# size the pause lists the large outputs by agent_id instead, and the client uses what it already
# holds or fetches them with get_output. Keeps one pause under client result limits (73K characters
# hit one in the Claude app) and stops the same output being re-sent in every later pause.
PAUSE_INLINE_LIMIT = int(os.environ.get("SPEAKMANAI_PAUSE_INLINE_LIMIT", "20000"))
PAUSE_INLINE_SMALL = 2000  # outputs under this many characters stay inline even in reference mode


def _dependency_blocks(agent_doc: dict, events: list) -> tuple[list, str]:
    """The (agent_id, content) pairs an agent depends on, last write wins. With no declared
    dependencies there are no pairs and the second value is the session's first output."""
    dependencies = agent_doc.get("dependencies", []) if agent_doc else []
    latest_outputs = {}
    first_output = ""
    for event in events:
        attrs = event.get("attributes", {})
        if attrs.get("event_type") == "VALIDATION" or attrs.get("validation_retry") == "true" or attrs.get("hitl_retry") == "true" or attrs.get("update_retry") == "true":
            continue
        src = event.get("data", {}).get("execution_context", {}).get("source_outputs", {})
        src_agent = src.get("source_agent_id")
        content = src.get("content")
        if src_agent and content:
            latest_outputs[src_agent] = content
            if not first_output:
                first_output = content

    if not dependencies:
        return [], first_output

    if "*" in dependencies:
        return [(a, c) for a, c in latest_outputs.items() if not a.startswith("root_planner")], ""
    return [(d, latest_outputs[d]) for d in dependencies if d in latest_outputs], ""


async def _compile_pause_context(agent_doc: dict, events: list, session_id: str) -> tuple[str, list]:
    """Context for a pause: the dependency outputs inline when they are small, otherwise the
    large ones listed by agent_id. Returns (context text, context_refs)."""
    blocks, plain = _dependency_blocks(agent_doc, events)
    if plain or not blocks:
        return plain, []
    inline = "\n\n".join(f"# CONTEXT FROM {a}:\n{c}" for a, c in blocks)
    if len(inline) <= PAUSE_INLINE_LIMIT:
        return inline, []

    # get_output serves an agent's output only for agents in the aggregate index, so only those can be referenced.
    fetchable = set(_aggregate_agent_index(events, await _build_human_exclusion_set(events)))
    parts, refs = [], []
    for agent, content in blocks:
        if agent in fetchable and len(content) > PAUSE_INLINE_SMALL:
            refs.append({"agent_id": agent, "chars": len(content)})
        else:
            parts.append(f"# CONTEXT FROM {agent}:\n{content}")
    if not refs:
        return inline, []

    listing = "\n".join(f"- {r['agent_id']}: {r['chars']} characters" for r in refs)
    note = (
        "# CONTEXT NOT INCLUDED\n"
        "This step also depends on the outputs below. They are large, so they are not repeated here:\n"
        f"{listing}\n"
        "For each one: if its complete text is still in this conversation, exactly as it was written, use that. "
        f"Otherwise call get_output(agent_id, session_id='{session_id}') and read the returned content. "
        "Do not work from memory, a summary or a partial copy of an output."
    )
    return "\n\n".join([note] + parts), refs

def _latest_pause_event(events: list) -> dict | None:
    """The most recent MCP_PAUSE event - the pause a session in AWAITING_INPUT is waiting on.
    Its event_id is the pause_id handed out by poll_workflow and checked by submit_response."""
    return next((e for e in reversed(events) if e.get("attributes", {}).get("event_type") == "MCP_PAUSE"), None)


async def _get_mcp_pause_config(doc: dict) -> dict | None:
    """
    Resolve input_required config for a session in AWAITING_INPUT state.

    Reads step_type from the workflow plan and fetches prompt/schema directly
    from the agent doc in the database - no dependency on embedded mcp_input_config.
    """
    events = doc.get("events") or []
    # workflow_definition is carried in every event — read from the first.
    workflow_definition = events[0].get("data", {}).get("workflow_definition", {}) if events else {}
    steps = workflow_definition.get("steps", [])

    # Find the MCP_PAUSE event to get the active step index and the prior output as context.
    mcp_pause_step_index = None
    mcp_pause_event = _latest_pause_event(events)
    if mcp_pause_event:
        try:
            mcp_pause_step_index = int(mcp_pause_event.get("attributes", {}).get("current_step_index", -1))
        except (TypeError, ValueError):
            pass

    if mcp_pause_step_index is None:
        return None

    # --- HITL_VALIDATOR gate ---
    # This pause isn't a real DAG step — it's an attached human-review gate on the origin
    # agent (mirrors how AI_VALIDATOR attaches, but must actually pause). Resolved directly
    # from the pause event's own attributes rather than via workflow_definition.steps.
    hitl_agent_id = mcp_pause_event.get("attributes", {}).get("hitl_pending_agent_id")
    if hitl_agent_id:
        ops_db = get_db()
        hitl_agent_doc = await ops_db["agents"].find_one({"agentId": hitl_agent_id})
        origin_source = mcp_pause_event.get("data", {}).get("execution_context", {}).get("source_outputs", {})
        origin_agent_id = origin_source.get("source_agent_id", "")
        origin_content = origin_source.get("content", "")

        dep_context, context_refs = await _compile_pause_context(hitl_agent_doc, events, doc.get("session_id", ""))
        hitl_context = f"# OUTPUT UNDER REVIEW ({origin_agent_id})\n{origin_content}"
        if dep_context:
            hitl_context += f"\n\n{dep_context}"

        hitl_loop = int(mcp_pause_event.get("attributes", {}).get("hitl_loop", "0"))
        if hitl_loop > 0:
            # Re-pause after an "Update" — the feedback lives on the most recent hitl_retry
            # bookkeeping event (logged by _handle_hitl_response), not on this fresh pause event.
            # Scoped to this pause's own step so a different agent's retry can never be picked up.
            last_retry = next(
                (e for e in reversed(events) if e.get("attributes", {}).get("hitl_retry") == "true"
                 and e.get("attributes", {}).get("current_step_index") == str(mcp_pause_step_index)),
                None
            )
            prior_feedback = last_retry.get("data", {}).get("execution_context", {}).get("hitl_feedback", "") if last_retry else ""
            if prior_feedback:
                hitl_context += f"\n\n# YOUR PRIOR FEEDBACK (loop {hitl_loop})\n{prior_feedback}"

        return {
            "prompt": hitl_agent_doc.get("systemPrompt", "") if hitl_agent_doc else "",
            "schema": hitl_agent_doc.get("inputSchema", {}) if hitl_agent_doc else {},
            "context": hitl_context,
            "context_refs": context_refs,
            "step_index": mcp_pause_step_index,
            "pause_id": mcp_pause_event.get("event_id", ""),
            "agent_id": hitl_agent_id,
            "agent_type": "HITL_VALIDATOR",
            "pause_kind": "hitl_feedback",
            "origin_agent_id": origin_agent_id,
        }

    # Find the matching step in the plan and confirm it is a MCP_PAUSE step.
    mcp_pause_step = next(
        (s for s in steps if s.get("step_index") == mcp_pause_step_index),
        None
    )
    if not mcp_pause_step or mcp_pause_step.get("step_type") != "MCP_PAUSE":
        return None
    if not mcp_pause_step.get("agents"):
        return None

    agent_id = mcp_pause_step["agents"][0]

    # Fetch prompt and schema directly from the agent document.
    ops_db = get_db()
    agent_doc = await ops_db["agents"].find_one({"agentId": agent_id})

    agent_type = agent_doc.get("agentType", "MCP_INPUT_REQUIRED") if agent_doc else "MCP_INPUT_REQUIRED"

    # Apply global and agent-level execution mode overrides to the reported agent_type
    settings = await ops_db["settings"].find_one({"_id": "global_config"}) or {}
    exec_mode = settings.get("execution_mode", "auto")
    a_exec_mode = agent_doc.get("executionMode", "auto") if agent_doc else "auto"

    if exec_mode in ("force_delegate", "delegate_only") and agent_type.startswith("AI_"):
        agent_type = "MCP_LLM_DELEGATE"
    elif exec_mode == "force_background" and agent_type == "MCP_LLM_DELEGATE":
        agent_type = "AI_WORKFLOW"
    elif exec_mode == "auto" and a_exec_mode == "delegate" and agent_type.startswith("AI_"):
        agent_type = "MCP_LLM_DELEGATE"

    pause_exec_ctx = mcp_pause_event.get("data", {}).get("execution_context", {}) if mcp_pause_event else {}
    # A delegated planner's pause carries its own input (workflow description, agent list and
    # the user's request). Every other pause gets its declared dependencies.
    context_refs = []
    context = pause_exec_ctx.get("planner_input")
    if not context:
        context, context_refs = await _compile_pause_context(agent_doc, events, doc.get("session_id", ""))

    # If this pause is a re-prompt after a failed validation or a human "Update" decision,
    # surface the prior attempt and the reviewer's feedback so the delegate can revise its answer.
    validation_feedback = pause_exec_ctx.get("validation_feedback")
    hitl_feedback = pause_exec_ctx.get("hitl_feedback")
    update_feedback = pause_exec_ctx.get("update_feedback")
    if validation_feedback:
        previous_output = pause_exec_ctx.get("previous_output", "")
        prior_block = f"\n\n# YOUR PREVIOUS OUTPUT\n{previous_output}" if previous_output else ""
        context += (
            f"{prior_block}\n\n# VALIDATOR RESPONSE\n"
            f"Revise your previous output to address every issue raised below:\n{validation_feedback}"
        )
    if hitl_feedback:
        previous_output = pause_exec_ctx.get("previous_output", "")
        prior_block = f"\n\n# YOUR PREVIOUS OUTPUT\n{previous_output}" if previous_output else ""
        context += (
            f"{prior_block}\n\n# HUMAN REVIEWER RESPONSE\n"
            f"Revise your previous output to address the feedback below:\n{hitl_feedback}"
        )
    if update_feedback:
        # Either the directly-targeted agent of an update_session cascade, or a downstream
        # agent reached mid-cascade whose upstream dependency just changed (see
        # _emit_advance_event's cascade branch, which seeds these two fields).
        previous_output = pause_exec_ctx.get("previous_output", "")
        prior_block = f"\n\n# YOUR PREVIOUS OUTPUT\n{previous_output}" if previous_output else ""
        context += (
            f"{prior_block}\n\n# PROJECT UPDATE REQUEST\n"
            f"Revise your previous output to address the requested change below:\n{update_feedback}"
        )

    schema = agent_doc.get("inputSchema", {}) if agent_doc else {}

    return {
        "prompt": agent_doc.get("systemPrompt", "") if agent_doc else "",
        "schema": schema,
        "context": context,
        "context_refs": context_refs,
        "step_index": mcp_pause_step_index,
        "pause_id": mcp_pause_event.get("event_id", ""),
        "agent_id": agent_id,
        "agent_type": agent_type,
    }


async def _build_human_exclusion_set(events: list, additional_exclusions: set = None) -> set:
    """
    Return the set of agent IDs to exclude from output aggregation.
    Excludes HITL pause steps based on their true database agentType.
    """
    human_agents = set()
    if additional_exclusions:
        human_agents.update(a for a in additional_exclusions if a)
        
    if not events:
        return human_agents
        
    agent_ids = list(set(
        e.get("data", {}).get("execution_context", {}).get("source_outputs", {}).get("source_agent_id", "")
        for e in events
    ))
    agent_ids = [a for a in agent_ids if a and not a.startswith("root_planner")]
    
    if agent_ids:
        ops_db = get_db()
        agent_docs = await ops_db["agents"].find({"agentId": {"$in": agent_ids}}).to_list(length=None)
        for a in agent_docs:
            a_type = a.get("agentType", "")
            if (a_type.startswith("MCP_") and a_type != "MCP_LLM_DELEGATE") or a_type == "AI_PLANNER":
                human_agents.add(a["agentId"])
                
    return human_agents


def _aggregate_agent_index(events: list, human_agents: set) -> dict:
    """
    Aggregate agent outputs from events, skipping VALIDATION and validation_retry events.
    Returns a dict keyed by agent_id with metadata and content.
    """
    agent_index = {}
    step_index = 0
    for event in events:
        attrs = event.get("attributes", {})
        if attrs.get("event_type") == "VALIDATION":
            continue
        if attrs.get("validation_retry") == "true":
            continue
        if attrs.get("hitl_retry") == "true":
            continue
        if attrs.get("update_retry") == "true":
            continue
        src = event.get("data", {}).get("execution_context", {}).get("source_outputs", {})
        agent_id = src.get("source_agent_id")
        if agent_id:
            # Parallel agents can emit a single event with a comma-separated agent ID string.
            # Split so each agent gets its own entry in the index.
            for aid in [a.strip() for a in agent_id.split(",")]:
                if aid and aid not in human_agents:
                    agent_index[aid] = {
                        "content": src.get("content", ""),
                        "mime_type": src.get("mime_type", "text/plain"),
                        "index": step_index,
                    }
            step_index += 1
    return agent_index


# ─────────────────────────────────────────────
# Tool 6: get_outputs  (manifest — no content)
# ─────────────────────────────────────────────
@mcp.tool(name="get_outputs", annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def get_outputs(mcp_ctx: Context, session_id: str = "", sid: str = "") -> str:
    """
    Return the manifest of available agent outputs for a completed workflow session.
    Does NOT return content - call get_output(session_id, agent_id) for each output you need.

    Works on any past session_id so you can resume downstream work across conversations.

    Args:
        session_id: The session_id from start_session or poll_workflow
        sid:        Same as session_id. Use it only if your client drops arguments named session_id.

    Returns:
        Session metadata, list of available outputs (agent_id, description, mime_type, index),
        document_capabilities, and suggested next steps.
        Use get_output(session_id, agent_id) to fetch the full content of each output.
    """
    session_id = _need_session_id(session_id, sid)
    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    COLLECTION = os.environ.get("RAW_EVENTS_COLLECTION", "events_raw")

    doc = await db[COLLECTION].find_one({"session_id": session_id})
    if not doc:
        raise RuntimeError(f"Session {session_id} not found.")

    current_status = doc.get("current_status", "UNKNOWN")
    if current_status == "CANCELLED":
        raise RuntimeError(
            f"Session {session_id} was cancelled - no outputs are available. "
            "Start a new session to generate a complete workflow."
        )

    events = doc.get("events") or []

    human_agents = await _build_human_exclusion_set(events)
    agent_index = _aggregate_agent_index(events, human_agents)

    # Build manifest — metadata only, no content
    available_outputs = [
        {
            "agent_id": agent_id,
            "description": f"Output from {agent_id}",
            "mime_type": meta["mime_type"],
            "index": meta["index"],
        }
        for agent_id, meta in sorted(agent_index.items(), key=lambda x: x[1]["index"])
    ]

    # Session metadata: title is stored top-level; workflow_id is in event attributes
    title = doc.get("session_title", "")
    workflow_id = (events[0].get("attributes") or {}).get("workflow_id", "") if events else ""

    # Extract document_capabilities from the COMPLETED event (added by capabilities_processor)
    document_capabilities = []
    for event in events:
        if event.get("attributes", {}).get("status") == "COMPLETED":
            dc = event.get("data", {}).get("execution_context", {}).get("source_outputs", {}).get("document_capabilities")
            if dc:
                document_capabilities = dc
            break

    return json.dumps({
        "session_id": session_id,
        "title": title,
        "workflow_id": workflow_id,
        "status": current_status,
        "agent_count": len(available_outputs),
        "available_outputs": available_outputs,
        "document_capabilities": document_capabilities,
        "suggested_next_steps": (
            [
                "Capabilities have been extracted and saved to your local library - no get_output call needed.",
                f"A capability group was {'created' if len(document_capabilities) >= 10 else 'not created (fewer than 10 capabilities saved)'} for this session.",
                "To run a Solution Architecture workflow using these capabilities: call list_workflows, choose an architecture workflow_id, then call start_session with a title and business description.",
            ]
            if "CAPABILITY_GENERATOR" in workflow_id.upper()
            else [
                "Call get_output(session_id, agent_id) for each output you need - fetch only what is relevant to your task.",
                "For the full solution architecture: fetch MCP_BUSINESS_CONTEXT_CLARIFIER_V1, MCP_BUSINESS_ANALYST_V1, MCP_BUSINESS_APPLICATION_ARCHITECT_V1, MCP_TECHNICAL_SOLUTION_ARCHITECT_V1 - each agent is a self-contained deliverable.",
                "For PowerPoint: fetch MCP_TECHNICAL_SOLUTION_ARCHITECT_V1 and MCP_TECHNICAL_VISUALIZATION_SPECIALIST_V1, then use python-pptx.",
                "For dev planning / ADR register: fetch MCP_TECHNICAL_SOLUTION_ARCHITECT_V1 and MCP_BUSINESS_APPLICATION_ARCHITECT_V1.",
                "For compliance report: fetch MCP_COMPLIANCE_OFFICER_V2.",
                "For stakeholder briefing: fetch MCP_BUSINESS_ANALYST_V1.",
                "To save capabilities to your SPEAKMAN.AI library: if document_capabilities is non-empty, call list_workflows to find the Capability Generator, then start_session with a group name as title and the document_capabilities JSON as description.",
            ]
        ),
    }, indent=2)


# ─────────────────────────────────────────────
# Tool 7: get_output  (single agent content)
# ─────────────────────────────────────────────
@mcp.tool(name="get_output", annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def get_output(agent_id: str, session_id: str = "", sid: str = "") -> str:
    """
    Retrieve the full content of one agent output from a workflow session.
    Call get_outputs(session_id) first to get the list of available agent_ids.

    Args:
        session_id: The session_id from start_session or poll_workflow
        sid:        Same as session_id. Use it only if your client drops arguments named session_id.
        agent_id:   The agent_id from get_outputs available_outputs list
                    (e.g. 'MCP_TECHNICAL_WRITER_V2', 'MCP_COMPLIANCE_OFFICER_V2')

    Returns:
        agent_id, description, mime_type, and the full content string for that agent.
    """
    session_id = _need_session_id(session_id, sid)
    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    COLLECTION = os.environ.get("RAW_EVENTS_COLLECTION", "events_raw")

    doc = await db[COLLECTION].find_one({"session_id": session_id})
    if not doc:
        raise RuntimeError(f"Session {session_id} not found.")

    if doc.get("current_status") == "CANCELLED":
        raise RuntimeError(
            f"Session {session_id} was cancelled - no outputs are available. "
            "Start a new session to generate a complete workflow."
        )

    events = doc.get("events") or []
    human_agents = await _build_human_exclusion_set(events)
    agent_index = _aggregate_agent_index(events, human_agents)

    if agent_id not in agent_index:
        available = sorted(agent_index.keys())
        raise RuntimeError(
            f"Agent '{agent_id}' not found in session {session_id}. "
            f"Available agent_ids: {available}"
        )

    meta = agent_index[agent_id]
    return json.dumps({
        "session_id": session_id,
        "agent_id": agent_id,
        "description": f"Output from {agent_id}",
        "mime_type": meta["mime_type"],
        "content": meta["content"],
    }, indent=2)


# ─────────────────────────────────────────────
# Tool 8: list_sessions
# ─────────────────────────────────────────────
@mcp.tool(name="list_sessions", annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def list_sessions(limit: int = 10) -> str:
    """
    List your recent MCP workflow sessions with their current status and session_ids.

    Use this to recover a session_id after a context reset, crash, or new conversation,
    or to check the status of sessions you have started previously.

    Args:
        limit: Number of recent sessions to return (default 10, max 25)

    Returns:
        List of sessions ordered by most recent first, each with:
          session_id - pass to poll_workflow, get_outputs, get_output, cancel_session
          title - the title supplied to start_session
          workflow_id - identifies the workflow type and therefore the correct call chain
          status - one of:
                           COMPLETED          → call get_outputs then get_output to retrieve content
                           AWAITING_INPUT      → call submit_response to continue the workflow
                           IN_PROGRESS        → call poll_workflow to check progress (poll every 30s)
                           FAILED             → call cancel_session to free your slot, then start_session
                           CANCELLED          → session is closed; start a new session
          awaiting_input - true when status is AWAITING_INPUT (submit_response required)
          created_at - ISO 8601 timestamp
          completed_steps - number of agent steps that have finished
    """
    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    COLLECTION = os.environ.get("RAW_EVENTS_COLLECTION", "events_raw")

    limit = min(max(1, limit), 25)

    # Restrict to MCP-originated sessions only — exclude UI workflow sessions.
    mcp_workflow_docs = await db["workflows"].find(
        {"workflowType": "mcp"},
        {"workflowId": 1, "_id": 0}
    ).to_list(length=100)
    mcp_workflow_ids = [w["workflowId"] for w in mcp_workflow_docs]

    # Hardcoded local user for single-container installation
    query = {"owner_id": "local_user"}

    # Filter to MCP sessions only
    if mcp_workflow_ids:
        query["events.0.attributes.workflow_id"] = {"$in": mcp_workflow_ids}

    cursor = db[COLLECTION].find(
        query,
        {"session_id": 1, "session_title": 1, "current_status": 1, "created_at": 1, "events": 1}
    ).sort("created_at", -1).limit(limit)

    sessions = []
    async for doc in cursor:
        events = doc.get("events") or []
        workflow_id_val = ""
        if events:
            workflow_id_val = (events[0].get("attributes") or {}).get("workflow_id", "")
        human_agents = await _build_human_exclusion_set(events)
        agent_index = _aggregate_agent_index(events, human_agents)
        status = doc.get("current_status", "UNKNOWN")
        sessions.append({
            "session_id": doc["session_id"],
            "title": doc.get("session_title", ""),
            "workflow_id": workflow_id_val,
            "status": status,
            "awaiting_input": status in ("AWAITING_INPUT", "AWAITING_CAPABILITIES"),
            "created_at": doc["created_at"].isoformat() if doc.get("created_at") else None,
            "completed_steps": len(agent_index),
        })

    return json.dumps({"sessions": sessions, "count": len(sessions)}, indent=2)


# ─────────────────────────────────────────────
# Tool 9: cancel_session
# ─────────────────────────────────────────────
@mcp.tool(name="cancel_session", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True))
async def cancel_session(session_id: str = "", sid: str = "") -> str:
    """
    Cancel a running or stalled workflow session and free your concurrency slot.

    Use this if a session is stuck in IN_PROGRESS, failed without updating status,
    or you want to abandon a session without waiting for a timeout.

    Args:
        session_id: The session_id to cancel
        sid:        Same as session_id. Use it only if your client drops arguments named session_id.

    Returns:
        Confirmation that the session was marked CANCELLED and the concurrency slot freed.
    """
    session_id = _need_session_id(session_id, sid)
    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    COLLECTION = os.environ.get("RAW_EVENTS_COLLECTION", "events_raw")

    doc = await db[COLLECTION].find_one({"session_id": session_id})
    if not doc:
        raise RuntimeError(f"Session {session_id} not found.")

    current_status = doc.get("current_status", "UNKNOWN")
    if current_status in ("COMPLETED", "CANCELLED"):
        return json.dumps({
            "ok": True,
            "session_id": session_id,
            "message": f"Session already in terminal state: {current_status}. No action taken."
        })

    await db[COLLECTION].update_one(
        {"session_id": session_id},
        {"$set": {"current_status": "CANCELLED"}}
    )

    return json.dumps({
        "ok": True,
        "session_id": session_id,
        "message": "Session cancelled."
    })


# ─────────────────────────────────────────────
# Tool: update_session
# ─────────────────────────────────────────────
@mcp.tool(name="update_session", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def update_session(agent_id: str, update_content: str, session_id: str = "", sid: str = "") -> str:
    """
    Revise one agent's output in an already-finished workflow session and cascade the change
    through every downstream agent that depends on it.

    Only works on a session whose status is COMPLETED or CANCELLED. If it is IN_PROGRESS or
    AWAITING_INPUT, call cancel_session(session_id) first, then retry this call.

    Args:
        session_id: The session_id of a COMPLETED or CANCELLED session (from list_sessions).
        sid:        Same as session_id. Use it only if your client drops arguments named session_id.
        agent_id:   The agent whose output to revise - an agent_id from get_outputs' available_outputs.
        update_content: What should change, in your own words.

    Returns:
        Confirmation the cascade has started - poll_workflow(session_id) exactly like a fresh run.
    """
    session_id = _need_session_id(session_id, sid)
    if not agent_id or not agent_id.strip():
        raise ValueError("agent_id must be a non-empty string.")
    if not update_content or not update_content.strip():
        raise ValueError("update_content must be a non-empty string.")
    if len(update_content) > 20000:
        raise ValueError("update_content exceeds 20,000 character limit.")

    db = get_db(os.environ.get("RAW_EVENTS_DB_NAME", "speakmanai_db"))
    COLLECTION = os.environ.get("RAW_EVENTS_COLLECTION", "events_raw")

    doc = await db[COLLECTION].find_one({"session_id": session_id})
    if not doc:
        raise RuntimeError(f"Session {session_id} not found.")

    events = doc.get("events") or []
    if not events:
        raise RuntimeError(f"Session {session_id} has no recorded steps to update.")
    workflow_def = events[0].get("data", {}).get("workflow_definition", {})

    entry_step_idx = resolve_agent_step_index(workflow_def, agent_id)
    if entry_step_idx is None:
        human_agents = await _build_human_exclusion_set(events)
        available = sorted(_aggregate_agent_index(events, human_agents).keys())
        raise RuntimeError(
            f"Agent '{agent_id}' not found in session {session_id}'s workflow. "
            f"Available agent_ids: {available}"
        )

    clean_update_content = scrub_pii(update_content)

    # This single atomic conditional transition IS the guard AND the idempotency mechanism —
    # only a COMPLETED/CANCELLED session can win it, and only one of two racing callers ever
    # can. No new state is touched before this call succeeds.
    won = await transition_completed_or_cancelled_to_draft(session_id, db)
    if not won:
        refreshed = await db[COLLECTION].find_one({"session_id": session_id}) or {}
        current_status = refreshed.get("current_status", "UNKNOWN")
        raise RuntimeError(
            f"Session {session_id} cannot be updated right now (status: {current_status}). "
            "update_session only works on a COMPLETED or CANCELLED session. If it is IN_PROGRESS "
            "or AWAITING_INPUT, call cancel_session(session_id) first, then retry update_session. "
            "If it is already DRAFT, an update is already in progress - poll_workflow instead."
        )

    await workflow_queue.put({
        "action": "update_session",
        "session_id": session_id,
        "agent_id": agent_id,
        "update_content": clean_update_content,
    })

    return json.dumps({
        "ok": True,
        "session_id": session_id,
        "agent_id": agent_id,
        "message": (
            f"Update accepted for {agent_id}. The workflow is regenerating its output and every "
            "downstream agent that depends on it. Call poll_workflow(session_id) every 15-30 "
            "seconds until COMPLETED, then get_outputs/get_output for the refreshed content."
        ),
    }, indent=2)


# ─────────────────────────────────────────────
# Tool 11: list_projects
# ─────────────────────────────────────────────
@mcp.tool(name="list_projects", annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def list_projects() -> str:
    """
    List your completed workflow projects from the projects cache.

    Returns projects most recent first, each with session_id, title, status,
    workflow_type, and output summary (section count, has_document, has_items).

    Use get_project(session_id) to fetch a specific project with its sections
    and available on-demand operations.
    """
    return await list_projects_local()


# ─────────────────────────────────────────────
# Tool 12: get_project
# ─────────────────────────────────────────────
@mcp.tool(name="get_project", annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def get_project(session_id: str = "", sid: str = "") -> str:
    """
    Get a specific project with its section manifest and available on-demand operations.

    The response includes a capabilities block that tells you exactly what operations
    are available for this project:
      can_generate_document - true if sections exist; call generate_document to assemble

    Sections are listed by title/agent - each is a panel of the workflow output.
    Call generate_document to produce derived artefacts on demand.

    Args:
        session_id: The session_id from list_projects or start_session
        sid:        Same as session_id. Use it only if your client drops arguments named session_id.
    """
    session_id = _need_session_id(session_id, sid)
    try:
        return await get_project_local(session_id)
    except ValueError as e:
        raise RuntimeError(str(e))


# ─────────────────────────────────────────────
# Tool 17: set_execution_mode
# ─────────────────────────────────────────────
@mcp.tool(name="set_execution_mode", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def set_execution_mode(mode: str) -> str:
    """
    Set the global execution mode for all workflows.
    Allows you to override the agent configurations dynamically.
    
    Args:
        mode: One of "auto" (default JSON config), "force_delegate" (Claude Code executes all AI steps), "force_background" (Ollama/Vertex executes all delegated steps), or "delegate_only" (like force_delegate, and the model API is paused: saved keys and settings are kept but never used, and AI validators auto-pass).
    """
    if mode not in ("auto", "force_delegate", "force_background", "delegate_only"):
        raise ValueError('mode must be "auto", "force_delegate", "force_background", or "delegate_only"')
        
    db = get_db()
    await db["settings"].update_one(
        {"_id": "global_config"},
        {"$set": {"execution_mode": mode}},
        upsert=True
    )
    return json.dumps({"ok": True, "message": f"Global execution mode set to '{mode}'."})


def _strip_mongo_export_fields(doc: dict) -> None:
    """Removes MongoDB-specific extended JSON fields and enterprise artifacts from exports."""
    doc.pop("_id", None)
    doc.pop("createdAt", None)
    doc.pop("updatedAt", None)
    doc.pop("tenantId", None)


# ─────────────────────────────────────────────
# Tool 15: import_architecture_plan
# ─────────────────────────────────────────────
@mcp.tool(name="import_architecture_plan", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def import_architecture_plan(plan_json: str) -> str:
    """
    Import a complete workflow and its associated agents into the database.
    Accepts the exact JSON output generated by the Workflow Analyst prompt.

    Args:
        plan_json: JSON string containing {"workflow": {...}, "agents": [...]}
    """
    try:
        data = json.loads(plan_json)
        workflow = data.get("workflow")
        agents = data.get("agents", [])

        db = get_db()
        msg_parts = []
        
        if workflow and "workflowId" in workflow:
            _strip_mongo_export_fields(workflow)
            await db["workflows"].update_one(
                {"workflowId": workflow["workflowId"]},
                {"$set": workflow},
                upsert=True
            )
            msg_parts.append(f"workflow '{workflow['workflowId']}'")

        agent_names = []
        for agent in agents:
            if "agentId" not in agent:
                continue

            _strip_mongo_export_fields(agent)

            # Safely append to the workflows array instead of overwriting it
            workflows_list = agent.pop("workflows", [])
            update_op = {"$set": agent}
            if workflows_list:
                update_op["$addToSet"] = {"workflows": {"$each": workflows_list}}

            await db["agents"].update_one(
                {"agentId": agent["agentId"]},
                update_op,
                upsert=True
            )
            agent_names.append(agent["agentId"])
            
        if agent_names:
            msg_parts.append(f"{len(agent_names)} agents")
            
        if not msg_parts:
            raise ValueError("Payload must contain a valid 'workflow' object or 'agents' array.")

        return json.dumps({
            "ok": True,
            "message": f"Successfully imported {' and '.join(msg_parts)}."
        })
    except Exception as e:
        raise RuntimeError(f"import_architecture_plan failed: {str(e)}")


# ─────────────────────────────────────────────
# Tool 18: import_workflow
# ─────────────────────────────────────────────
@mcp.tool(name="import_workflow", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def import_workflow(workflow_json: str) -> str:
    """
    Import or update a workflow definition independently (without agents).
    
    Args:
        workflow_json: JSON string of the workflow document. Must contain 'workflowId'.
    """
    try:
        workflow = json.loads(workflow_json)
        if "workflowId" not in workflow:
            raise ValueError("Workflow document must contain 'workflowId'.")
            
        _strip_mongo_export_fields(workflow)
        db = get_db()
        await db["workflows"].update_one(
            {"workflowId": workflow["workflowId"]},
            {"$set": workflow},
            upsert=True
        )
        return json.dumps({"ok": True, "message": f"Workflow '{workflow['workflowId']}' imported successfully."})
    except Exception as e:
        raise RuntimeError(f"import_workflow failed: {str(e)}")


# ─────────────────────────────────────────────
# Tool 19: import_agent
# ─────────────────────────────────────────────
@mcp.tool(name="import_agent", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def import_agent(agent_json: str) -> str:
    """
    Import or update a single agent independently. Safely appends to the agent's workflow list.
    
    Args:
        agent_json: JSON string of the agent document. Must contain 'agentId'.
    """
    try:
        agent = json.loads(agent_json)
        if "agentId" not in agent:
            raise ValueError("Agent document must contain 'agentId'.")
            
        _strip_mongo_export_fields(agent)
        workflows_list = agent.pop("workflows", [])
        
        update_op = {"$set": agent}
        if workflows_list:
            update_op["$addToSet"] = {"workflows": {"$each": workflows_list}}
            
        db = get_db()
        await db["agents"].update_one(
            {"agentId": agent["agentId"]},
            update_op,
            upsert=True
        )
        return json.dumps({"ok": True, "message": f"Agent '{agent['agentId']}' imported successfully."})
    except Exception as e:
        raise RuntimeError(f"import_agent failed: {str(e)}")


# ─────────────────────────────────────────────
# Tool 13: generate_document
# ─────────────────────────────────────────────
@mcp.tool(name="generate_document", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
async def generate_document(session_id: str = "", sid: str = "", format: str = "document") -> str:
    """
    Assemble workflow sections into a formatted document via AI.
    The result is cached in the project record - subsequent calls regenerate it.

    Check get_project(session_id).capabilities.can_generate_document before calling.
    Returns 400 if the project has no sections.

    Args:
        session_id: The session_id of the completed project
        sid:        Same as session_id. Use it only if your client drops arguments named session_id.
        format:     Document format hint for the assembler, e.g. "document",
                    "executive_summary", "sad", "report" (default: "document")

    Returns:
        The assembled document as a Markdown string, plus session_id and format.
    """
    session_id = _need_session_id(session_id, sid)
    try:
        return await generate_document_local(session_id, format)
    except Exception as e:
        raise RuntimeError(f"generate_document failed: {str(e)}")


# ─────────────────────────────────────────────
# Tool 14: render_document
# ─────────────────────────────────────────────
@mcp.tool(name="render_document", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def render_document(markdown: str, title: str = "", subtitle: str = "") -> str:
    """
    Convert a Markdown document into a fully self-contained, SPEAKMAN.AI-branded HTML document
    (inline CSS, base64 logo - no external files, no local Python/Node required).

    To rebrand: edit the returned HTML directly (swap the logo <img> src and the header/accent
    CSS custom properties) - there is no template file system.

    Args:
        markdown: The document body as Markdown (already synthesized by the caller).
        title:    Document title (default: first H1 in markdown).
        subtitle: Document subtitle (default: "Generated {today}").

    Returns:
        JSON: {"html": "<full HTML document string>"}
    """
    try:
        return await render_document_local(markdown, title or None, subtitle or None)
    except Exception as e:
        raise RuntimeError(f"render_document failed: {str(e)}")


# ─────────────────────────────────────────────
# Tool 18: list_skills
# ─────────────────────────────────────────────
@mcp.tool(name="list_skills", annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def list_skills() -> str:
    """
    List the SPEAKMAN.AI skills this server can deliver.

    A skill is a complete set of step-by-step instructions for producing a finished deliverable,
    for example running the architecture workflow and writing a Solution Architecture Document.
    Call this when the user asks for a SPEAKMAN.AI deliverable and your client does not already
    have a matching skill installed. Then call get_skill(name) for the one you need.

    Returns:
        JSON: skills_version and a list of {name, description}.
    """
    skills = await asyncio.to_thread(skills_installer.list_mcp_skills)
    manifest = skills_installer.load_manifest() or {}
    if not skills:
        return json.dumps({"skills": [], "message": "This build of the server does not include any skills."})
    return json.dumps({
        "skills_version": manifest.get("version"),
        "skills": skills,
        "next_step": "Call get_skill(name) and follow the returned instructions exactly.",
    }, indent=2)


# ─────────────────────────────────────────────
# Tool 19: get_skill
# ─────────────────────────────────────────────
@mcp.tool(name="get_skill", annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
async def get_skill(name: str) -> str:
    """
    Get the full instructions for one SPEAKMAN.AI skill, with every file it bundles included.

    Follow the returned instructions exactly, as you would a skill installed in your client.
    Files the skill refers to (for example prompts/sad_agent_prompt.md) are included in the
    same response under BUNDLED FILES, so nothing needs to be read from disk.

    Args:
        name: A skill name from list_skills (for example 'generate-sad').

    Returns:
        The skill instructions as plain text.
    """
    text = await asyncio.to_thread(skills_installer.get_mcp_skill, name)
    if text is None:
        available = [s["name"] for s in await asyncio.to_thread(skills_installer.list_mcp_skills)]
        raise ValueError(f"Unknown skill '{name}'. Available skills: {', '.join(available) or 'none in this build'}.")
    return text


# ─────────────────────────────────────────────
# Prompt 1: architecture_brief
# ─────────────────────────────────────────────
@mcp.prompt()
async def architecture_brief(system_name: str, business_context: str) -> str:
    """
    Generate a structured prompt to kick off a SPEAKMAN.AI architecture workflow.

    Args:
        system_name: Name of the system or project to architect (e.g. 'Payment Platform', 'Customer Data Hub')
        business_context: Description of the business problem, opportunity, or goal to be addressed
    """
    return (
        f"I need to create a Solution Architecture Document for: {system_name}\n\n"
        f"Business context: {business_context}\n\n"
        "Please complete the full SPEAKMAN.AI architecture workflow:\n"
        "1. Call list_workflows() to see available workflows\n"
        "2. Call start_session() with the architecture workflow_id, "
        f"title='{system_name}', and the business context as the description\n"
        "3. Poll poll_workflow(session_id) every 30 seconds until status is AWAITING_INPUT\n"
        "4. When status is AWAITING_INPUT: read input_required.prompt and input_required.schema\n"
        "5. Build the requested capabilities list matching the schema\n"
        "6. Call submit_response(session_id, json_capabilities_string, pause_id=input_required.pause_id) to continue the pipeline\n"
        "7. Poll with poll_workflow() every 30-60 seconds until status is COMPLETED\n"
        "8. Call get_outputs() to get the manifest, then get_output() for each agent deliverable\n\n"
        "Individual agent outputs are the deliverables - fetch each with get_output(session_id, agent_id). "
        "Key outputs: MCP_BUSINESS_CONTEXT_CLARIFIER_V1 (business context), MCP_BUSINESS_ANALYST_V1 (requirements + personas), "
        "MCP_BUSINESS_APPLICATION_ARCHITECT_V1 (component catalog + data flows), MCP_TECHNICAL_SOLUTION_ARCHITECT_V1 (tech arch + ADRs), "
        "MCP_COMPLIANCE_OFFICER_V2 (compliance + RTM), MCP_TECHNICAL_VISUALIZATION_SPECIALIST_V1 (Mermaid diagram)."
    )


# ─────────────────────────────────────────────
# Prompt 2: resume_session
# ─────────────────────────────────────────────
@mcp.prompt()
async def resume_session() -> str:
    """
    Resume work on a previous SPEAKMAN.AI workflow after a context reset or new conversation.
    Use this when you need to retrieve outputs or continue a session started in a prior conversation.
    """
    return (
        "I need to resume a previous SPEAKMAN.AI architecture workflow.\n\n"
        "Please:\n"
        "1. Call list_sessions() to see my recent sessions with their current status\n"
        "2. Identify the session to continue based on title and status\n"
        "3. Based on the session status:\n"
        "   - COMPLETED: call get_outputs(session_id) to get the manifest, "
        "then get_output(session_id, agent_id) for each agent you need\n"
        "   - AWAITING_INPUT: call poll_workflow to get input_required, then submit_response(session_id, response, pause_id=input_required.pause_id)\n"
        "   - IN_PROGRESS: call poll_workflow(session_id) every 30-60 seconds until COMPLETED\n"
        "   - FAILED or stuck: call cancel_session(session_id) to free your concurrency slot, "
        "then start a new session with start_session()"
    )




# ─────────────────────────────────────────────
# FastAPI app — Streamable HTTP transport
# ─────────────────────────────────────────────

# Streamable HTTP transport — returns a Starlette app with a single route at /mcp
mcp_asgi = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Seed database on first launch if empty
    if os.environ.get("USE_SQLITE", "").lower() in ("true", "1", "yes"):
        await seed_if_empty()
        try:
            st = await workflows_sync.status(get_db(), _workflows_and_agents_dir())
            if st["state"] == "differs":
                names = ", ".join(d["workflowId"] for d in st["differs"])
                log.warning(f"{len(st['differs'])} shipped workflow(s) differ from this version: {names}. "
                            "Open the setup page to update them.")
        except Exception as e:
            log.warning(f"Could not check the workflows against this version: {e}")
    try:
        await _apply_fresh_install_mode()
    except Exception as e:
        log.warning(f"Could not set the fresh-install execution mode: {e}")

    # Start the integrated local worker queue
    asyncio.create_task(local_workflow_consumer())
    async with mcp_asgi.router.lifespan_context(app):
        try:
            yield
        finally:
            await close_db()


app = FastAPI(title="SPEAKMAN.AI MCP Server", lifespan=lifespan)


# ─────────────────────────────────────────────
# First-launch database seeding
# ─────────────────────────────────────────────

def _workflows_and_agents_dir() -> Path:
    """Locate the WorkflowsAndAgents directory relative to this file or the PyInstaller bundle."""
    # PyInstaller sets sys._MEIPASS; fall back to relative path for dev
    import sys
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent.parent))
    return base / "WorkflowsAndAgents"


async def seed_if_empty() -> None:
    """Import any bundled workflow JSON files whose workflowId isn't in the database yet.

    Runs on every startup rather than a one-time empty-database check, so workflow files
    added to WorkflowsAndAgents/ after a user's first launch (e.g. a new bundled workflow
    shipped in an update) still get imported — without touching workflows/agents that are
    already present, which may have been hand-edited via import_agent/import_workflow.
    """
    db = get_db()

    wf_dir = _workflows_and_agents_dir()
    if not wf_dir.is_dir():
        log.warning(f"WorkflowsAndAgents directory not found at {wf_dir} — skipping seed.")
        return

    count = 0
    for json_path in sorted(wf_dir.glob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            workflow = data.get("workflow")
            agents = data.get("agents", [])

            if not workflow or "workflowId" not in workflow:
                continue

            already_seeded = await db["workflows"].find_one({"workflowId": workflow["workflowId"]})
            if already_seeded:
                continue

            _strip_mongo_export_fields(workflow)
            await db["workflows"].update_one(
                {"workflowId": workflow["workflowId"]},
                {"$set": workflow},
                upsert=True,
            )

            for agent in agents:
                if "agentId" not in agent:
                    continue
                _strip_mongo_export_fields(agent)
                workflows_list = agent.pop("workflows", [])
                update_op = {"$set": agent}
                if workflows_list:
                    update_op["$addToSet"] = {"workflows": {"$each": workflows_list}}
                await db["agents"].update_one(
                    {"agentId": agent["agentId"]}, update_op, upsert=True
                )
            count += 1
            log.info(f"Seeded {json_path.name}")
        except Exception as e:
            log.warning(f"Failed to seed {json_path.name}: {e}")

    if count:
        log.info(f"Database seeding complete — {count} new workflow file(s) imported.")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "speakmanai-mcp-server"}


@app.get("/quit")
async def quit_server():
    import threading, os
    threading.Thread(target=lambda: os._exit(0), daemon=True).start()
    return {"status": "ok", "message": "Shutting down."}


# ─────────────────────────────────────────────
# Setup / configuration endpoints
# ─────────────────────────────────────────────

_CONFIG_PATH = Path.home() / ".speakmanai" / "config.json"

_PROVIDER_MODELS = {
    "gemini":    {"fast": "gemini-3.5-flash-lite", "standard": "gemini-3.8-flash",  "advanced": "gemini-3.1-pro-preview"},
    "anthropic": {"fast": "claude-haiku-5-5", "standard": "claude-sonnet-5-5", "advanced": "claude-opus-5-5"},
    "openai":    {"fast": "gpt-6-luna",       "standard": "gpt-6.1-sol",       "advanced": "gpt-6-astra"},
    "ollama":    {"fast": "",                       "standard": "",                  "advanced": ""},
}

_MCP_SERVER_NAME = "speakmanai"


def _find_claude_cli() -> str | None:
    """Locate the Claude Code CLI. The exe may be started with a shorter PATH than the user's
    shell, so also check the native installer's default location."""
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "claude.exe", Path.home() / ".local" / "bin" / "claude"):
        if candidate.is_file():
            return str(candidate)
    return None


def _claude_mcp_add_args(url: str) -> list[str]:
    # User scope: one registration covers every project. Claude Code keeps MCP servers in
    # ~/.claude.json and owns that file, so register through its CLI rather than editing JSON.
    return ["mcp", "add", "--transport", "http", "--scope", "user", _MCP_SERVER_NAME, url]


def _run_claude(cli: str, args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [cli, *args], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _read_config() -> dict:
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _write_config(cfg: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _apply_config_to_env(cfg: dict) -> None:
    mapping = {
        "llm_provider":      "LLM_PROVIDER",
        "gemini_api_key":    "GEMINI_API_KEY",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "openai_api_key":    "OPENAI_API_KEY",
        "ollama_base_url":   "OLLAMA_BASE_URL",
        "default_model":     "DEFAULT_MODEL",
        "standard_model":    "STANDARD_MODEL",
        "advanced_model":    "ADVANCED_MODEL",
        "execution_mode":    "EXECUTION_MODE",
        "gcp_project_id":    "GCP_PROJECT_ID",
        "gcp_region":        "GCP_REGION",
    }
    for cfg_key, env_key in mapping.items():
        val = cfg.get(cfg_key)
        if val:
            os.environ[env_key] = str(val)
        elif cfg_key in cfg:
            # Explicitly cleared (e.g. switching Gemini API key → Vertex AI) — drop
            # the stale env var so it doesn't keep winning in the same process.
            os.environ.pop(env_key, None)


_EXECUTION_MODES = ("auto", "force_delegate", "force_background", "delegate_only")


async def _get_execution_mode() -> str:
    """The engine's effective execution mode: the global_config row in the settings collection."""
    settings = await get_db()["settings"].find_one({"_id": "global_config"}) or {}
    mode = settings.get("execution_mode", "auto")
    return mode if mode in _EXECUTION_MODES else "auto"


def _model_api_configured() -> bool:
    """True if a model API is set up at all: any provider key, a GCP project, or Ollama chosen."""
    env = os.environ
    if env.get("LLM_PROVIDER", "").lower() == "ollama":
        return True
    return any(env.get(k) for k in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GCP_PROJECT_ID"))


async def _set_execution_mode(mode: str) -> None:
    await get_db()["settings"].update_one(
        {"_id": "global_config"}, {"$set": {"execution_mode": mode}}, upsert=True
    )


async def _apply_fresh_install_mode() -> None:
    """A new install with no model API starts in delegate_only, so workflows run through the
    connected assistant without a key. An install that has ever chosen a mode is left alone."""
    row = await get_db()["settings"].find_one({"_id": "global_config"}) or {}
    if "execution_mode" not in row and not _model_api_configured():
        await _set_execution_mode("delegate_only")
        log.info("No model API configured and no execution mode chosen: defaulting to delegate_only.")


def _claude_code_status(expected_url: str) -> dict:
    """What the setup page needs to know about Claude Code on this machine. Read-only."""
    home = Path.home()
    cli = _find_claude_cli()
    registered_url = None
    try:
        data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        registered_url = ((data.get("mcpServers") or {}).get(_MCP_SERVER_NAME) or {}).get("url")
    except Exception:
        pass
    return {
        "cli_found": bool(cli),
        # The native installer puts claude.exe in ~/.local/bin and does not always add it to PATH.
        "cli_on_path": bool(shutil.which("claude")),
        "windows": os.name == "nt",
        "detected": bool(cli) or (home / ".claude").is_dir(),
        "registered": registered_url == expected_url,
        "registered_url": registered_url,
    }


@app.get("/api/assistant-status")
async def assistant_status(request: Request):
    port = request.url.port or 8000
    return JSONResponse({
        "claude_code": await asyncio.to_thread(_claude_code_status, f"http://localhost:{port}/mcp"),
        "model_api_configured": _model_api_configured(),
        "execution_mode": await _get_execution_mode(),
    })


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    """Self-contained onboarding page — no external JS/CSS dependencies."""
    cfg = _read_config()
    # The engine reads the execution mode from the database (also written by the
    # set_execution_mode tool), so show that value rather than whatever config.json holds.
    cfg["execution_mode"] = await _get_execution_mode()
    return HTMLResponse(
        _build_setup_html(cfg),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/api/config")
async def get_config():
    cfg = _read_config()
    # Mask API keys
    for key in ("gemini_api_key", "anthropic_api_key", "openai_api_key"):
        if cfg.get(key):
            cfg[key] = cfg[key][:6] + "••••••••"
    return JSONResponse(cfg)


@app.post("/api/config")
async def post_config(request: Request):
    body = await request.json()
    # "use_model_api" is the setup page asking to run AI steps on the user's own model API.
    # It is a request, not a setting: the mode only becomes auto when an API is really set up.
    use_model_api = bool(body.pop("use_model_api", False))
    mode = body.get("execution_mode")
    if mode is not None and mode not in _EXECUTION_MODES:
        return JSONResponse({"ok": False, "message": f"Invalid execution_mode: {mode!r}."}, status_code=400)
    cfg = _read_config()
    cfg.update({k: v for k, v in body.items() if v is not None})
    _write_config(cfg)
    _apply_config_to_env(cfg)
    configured = _model_api_configured()
    message = "Configuration saved."
    if use_model_api:
        if configured:
            mode = "auto"
            message = "Saved. AI steps now run on your model API."
        else:
            mode = None
            message = "Saved. Add a model API to switch: until then your assistant keeps running the AI steps."
    if mode is not None:
        # The engine reads the mode from the database, not from config.json or the environment.
        cfg["execution_mode"] = mode
        _write_config(cfg)
        await _set_execution_mode(mode)
    return JSONResponse({"ok": True, "message": message, "execution_mode": await _get_execution_mode(),
                         "model_api_configured": configured})


@app.get("/api/check-ollama")
async def check_ollama():
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{base}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            models = [
                {
                    "name": m["name"],
                    "parameter_size": m.get("details", {}).get("parameter_size", ""),
                }
                for m in data.get("models", [])
            ]
            return JSONResponse({"available": True, "models": models})
    except Exception:
        return JSONResponse({"available": False, "models": []})


@app.post("/api/setup-mcp-client")
async def setup_mcp_client(request: Request):
    body = await request.json()
    client_name = body.get("client")
    # Only Claude Code can be registered automatically. Other clients are set up by hand
    # from the MCP URL shown on the setup page.
    if client_name != "claude_code":
        raise HTTPException(status_code=400, detail=f"Unknown client: {client_name}")

    # Connecting an assistant with no model API set up means the assistant does the work.
    if not _model_api_configured() and await _get_execution_mode() == "auto":
        await _set_execution_mode("delegate_only")

    port = body.get("port") or request.url.port or 8000
    url = f"http://localhost:{port}/mcp"
    add_args = _claude_mcp_add_args(url)
    command = "claude " + " ".join(add_args)

    cli = _find_claude_cli()
    if not cli:
        return JSONResponse({"ok": False, "command": command,
                             "message": "Claude Code CLI not found. Copy the command and run it in a terminal."})

    try:
        code, out = await asyncio.to_thread(_run_claude, cli, add_args)
        if code != 0 and "already exists" in out.lower():
            # Re-register so a changed port takes effect.
            await asyncio.to_thread(_run_claude, cli, ["mcp", "remove", "--scope", "user", _MCP_SERVER_NAME])
            code, out = await asyncio.to_thread(_run_claude, cli, add_args)
        if code != 0:
            log.warning(f"claude mcp add failed ({code}): {out}")
            return JSONResponse({"ok": False, "command": command,
                                 "message": f"Claude Code did not accept the registration: {out[:200] or 'no output'}"})
        return JSONResponse({"ok": True, "command": command,
                             "message": "Added to Claude Code for all projects. Restart Claude Code to activate."})
    except Exception as e:
        log.warning(f"claude mcp add could not run: {e}")
        return JSONResponse({"ok": False, "command": command,
                             "message": f"Could not run the Claude Code CLI: {e}. Copy the command and run it in a terminal."})


@app.get("/api/skills-status")
async def skills_status():
    return JSONResponse(await asyncio.to_thread(skills_installer.status))


@app.post("/api/install-skills")
async def install_skills():
    """Copy the bundled skills into Claude Code (user level). Unattended; changed files are backed up."""
    try:
        result = await asyncio.to_thread(skills_installer.install)
    except Exception as e:
        log.warning(f"Skills install failed: {e}")
        return JSONResponse({"ok": False, "message": f"Skills install failed: {e}"})
    log.info(f"Skills install: {result.get('message')}")
    return JSONResponse(result)


@app.get("/api/workflows-status")
async def workflows_status():
    """Do the workflows in this database match the ones shipped in this build?"""
    try:
        return JSONResponse(await workflows_sync.status(get_db(), _workflows_and_agents_dir()))
    except Exception as e:
        log.warning(f"Workflow check failed: {e}")
        return JSONResponse({"state": "unavailable", "total": 0, "differs": [], "message": f"Could not check the workflows: {e}"})


# Feedback goes to a public HubSpot form endpoint. No key is involved; the ids are not secrets.
FEEDBACK_ENDPOINT = ("https://api.hsforms.com/submissions/v3/integration/submit/"
                     "342296015/ec0616e6-8af0-4be5-84c4-0e31dfd4e457")
FEEDBACK_TYPES = {"bug": "Bug report", "request": "Feature request",
                  "question": "Question", "updates": "Keep me updated"}


@app.post("/api/feedback")
async def send_feedback(request: Request):
    """Send one note the user wrote on the setup page. Only the type, the optional email and the message leave this computer."""
    # The form has three fields: feedback_type (one of FEEDBACK_TYPES), email and message.
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "That request was not valid."})
    kind = FEEDBACK_TYPES.get(str(body.get("type", "")))
    email = str(body.get("email", "")).strip()
    message = str(body.get("message", "")).strip()
    if not kind:
        return JSONResponse({"ok": False, "message": "Choose what this is about."})
    if kind == "Keep me updated" and not email:
        return JSONResponse({"ok": False, "message": "Add your email address so we can send you updates."})
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return JSONResponse({"ok": False, "message": "That email address does not look right."})
    if not message and kind != "Keep me updated":
        return JSONResponse({"ok": False, "message": "Write a short message first."})
    if len(message) > 5000:
        return JSONResponse({"ok": False, "message": "Please keep the message under 5000 characters."})
    fields = [{"name": "feedback_type", "value": kind},
              {"name": "message", "value": message or "(no message)"}]
    if email:
        fields.append({"name": "email", "value": email})
    payload = {"fields": fields,
               "context": {"pageUri": "speakmanai-exe://setup", "pageName": "SpeakmanAI exe setup page"}}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(FEEDBACK_ENDPOINT, json=payload)
    except Exception as e:
        log.warning(f"Feedback not sent: {e}")
        return JSONResponse({"ok": False, "message": "Could not reach the server. Check your internet connection and try again. Nothing was sent."})
    if r.status_code != 200:
        log.warning(f"Feedback rejected: HTTP {r.status_code}")
        return JSONResponse({"ok": False, "message": f"The server did not accept it (HTTP {r.status_code}). Nothing was saved. Try again later."})
    log.info(f"Feedback sent: {kind}")
    return JSONResponse({"ok": True, "message": "Sent. Thank you."})


@app.post("/api/update-workflows")
async def update_workflows():
    """Replace the shipped workflows that differ from this build. The replaced versions are backed up first."""
    try:
        result = await workflows_sync.apply(get_db(), _workflows_and_agents_dir(), Path.home() / ".speakmanai" / "backups")
    except Exception as e:
        log.warning(f"Workflow update failed: {e}")
        return JSONResponse({"ok": False, "message": f"Workflow update failed: {e}"})
    log.info(f"Workflow update: {result.get('message')}")
    return JSONResponse(result)


@app.get("/downloads/skills/{name}")
async def download_skills_bundle(name: str):
    manifest = skills_installer.load_manifest() or {}
    if name not in manifest.get("downloads", {}):
        raise HTTPException(status_code=404, detail="Unknown download.")
    return FileResponse(skills_installer.bundle_dir() / "downloads" / name, filename=name, media_type="application/zip")


def _build_setup_html(cfg: dict) -> str:
    """Generate the setup page HTML."""
    provider       = cfg.get("llm_provider", "gemini")
    saved_exec     = cfg.get("execution_mode", "auto")
    if saved_exec not in _EXECUTION_MODES:
        saved_exec = "auto"
    saved_fast     = cfg.get("default_model", "")
    saved_standard = cfg.get("standard_model", "")
    saved_advanced = cfg.get("advanced_model", "")
    saved_gcp_project = cfg.get("gcp_project_id", "")
    saved_gcp_region  = cfg.get("gcp_region", "us-east1")
    configured = _model_api_configured()
    configured_js = "true" if configured else "false"
    has_key_js = json.dumps({p: bool(cfg.get(f"{p}_api_key")) for p in ("gemini", "anthropic", "openai")})
    # The model section starts open only for someone who already uses a model API.
    model_open = "open" if configured else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SPEAKMAN.AI Setup</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; background: #0a0f1a; color: #e2e8f0; min-height: 100vh; display: flex; align-items: flex-start; justify-content: center; padding: 40px 24px; }}
  .card {{ background: #111827; border-radius: 12px; padding: 32px; width: 100%; max-width: 560px; box-shadow: 0 4px 32px rgba(0,0,0,0.6), 0 0 0 1px rgba(0,255,127,0.08); }}
  .brand {{ font-size: 1.5rem; font-weight: 800; color: #00ff7f; letter-spacing: 0.04em; margin-bottom: 4px; }}
  .subtitle {{ color: #64748b; font-size: 0.875rem; margin-bottom: 28px; }}
  h2 {{ font-size: 0.75rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: #475569; margin-bottom: 12px; }}
  .section {{ margin-bottom: 24px; }}
  .divider {{ border: none; border-top: 1px solid #1e293b; margin: 24px 0; }}
  .radio-group {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .radio-group label {{ cursor: pointer; padding: 8px 16px; border-radius: 8px; border: 2px solid #1e293b; font-size: 0.875rem; transition: all 0.15s; color: #94a3b8; }}
  .radio-group label.active {{ border-color: #00ff7f; background: #021a0d; color: #00ff7f; }}
  .radio-group label:hover:not(.active) {{ border-color: #334155; color: #e2e8f0; }}
  label.field-label {{ display: block; font-size: 0.8rem; color: #64748b; margin-bottom: 4px; margin-top: 12px; }}
  textarea {{ width: 100%; background: #0a0f1a; border: 1px solid #1e293b; border-radius: 6px; padding: 8px 12px; color: #e2e8f0; font-size: 0.875rem; font-family: inherit; resize: vertical; }}
  input[type=text], input[type=email], input[type=password], select {{ width: 100%; background: #0a0f1a; border: 1px solid #1e293b; border-radius: 6px; padding: 8px 12px; color: #e2e8f0; font-size: 0.875rem; }}
  input:focus, select:focus {{ outline: none; border-color: #00ff7f; box-shadow: 0 0 0 2px rgba(0,255,127,0.12); }}
  select option {{ background: #111827; }}
  .btn {{ display: inline-flex; align-items: center; gap: 8px; padding: 10px 20px; border-radius: 8px; font-size: 0.875rem; font-weight: 600; cursor: pointer; border: none; transition: all 0.15s; }}
  .btn-primary {{ background: #00ff7f; color: #0a0f1a; width: 100%; justify-content: center; margin-top: 16px; font-weight: 700; }}
  .btn-primary:hover {{ background: #00e070; box-shadow: 0 0 16px rgba(0,255,127,0.3); }}
  .btn-outline {{ background: transparent; border: 2px solid #1e293b; color: #94a3b8; }}
  .btn-outline:hover {{ border-color: #00ff7f; color: #00ff7f; background: #021a0d; }}
  .copy-row {{ display: flex; align-items: center; gap: 8px; margin-top: 6px; }}
  .copy-row code {{ flex: 1; background: #0a0f1a; border: 1px solid #1e293b; border-radius: 8px; padding: 8px 12px; font-size: 0.75rem; color: #94a3b8; overflow-x: auto; white-space: nowrap; }}
  .btn-copy {{ padding: 6px 12px; font-size: 0.75rem; }}
  .status-dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; background: #475569; flex: none; }}
  .status-dot.up {{ background: #00ff7f; }}
  .status-dot.warn {{ background: #f59e0b; }}
  .status-line {{ display: flex; align-items: center; gap: 8px; font-size: 0.875rem; color: #cbd5e1; margin-bottom: 4px; }}
  .note {{ background: #0f172a; border: 1px solid #1e293b; border-radius: 6px; padding: 10px 12px; font-size: 0.8rem; color: #94a3b8; margin-top: 8px; }}
  .choice-group {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  .choice {{ cursor: pointer; border: 2px solid #1e293b; border-radius: 10px; padding: 14px; transition: all 0.15s; }}
  .choice:hover:not(.active) {{ border-color: #334155; }}
  .choice.active {{ border-color: #00ff7f; background: #021a0d; }}
  .choice-title {{ font-weight: 700; font-size: 0.95rem; color: #e2e8f0; }}
  .choice.active .choice-title {{ color: #00ff7f; }}
  .choice-sub {{ font-size: 0.75rem; color: #64748b; margin-top: 4px; }}
  .panel {{ margin-top: 14px; padding: 14px; border: 1px solid #1e293b; border-radius: 10px; background: #0d1422; }}
  .panel .btn-primary {{ margin-top: 8px; }}
  .btn-row {{ display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }}
  a.btn {{ text-decoration: none; }}
  .btn:disabled {{ opacity: 0.6; cursor: default; }}
  .warn-box {{ margin-top: 14px; padding: 10px 12px; border-radius: 8px; background: #1c1405; border: 1px solid #78350f; font-size: 0.8rem; color: #fcd34d; }}
  .warn-box .btn {{ margin-top: 8px; }}
  .next-box {{ margin-top: 10px; padding: 10px 12px; border-radius: 8px; background: #021a0d; border: 1px solid #14532d; font-size: 0.8rem; color: #a7f3d0; }}
  .steps {{ margin: 8px 0 4px 18px; font-size: 0.8rem; color: #94a3b8; }}
  .steps li {{ margin-bottom: 8px; }}
  .panel a:not(.btn) {{ color: #00ff7f; }}
  #assistant-group label {{ padding: 8px 11px; font-size: 0.82rem; }}
  code {{ font-size: 0.78rem; color: #cbd5e1; }}
  details.fold {{ border-top: 1px solid #1e293b; padding: 14px 0 4px; }}
  details.fold > summary {{ cursor: pointer; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: #64748b; list-style-position: inside; }}
  details.fold[open] > summary {{ margin-bottom: 8px; }}
  details.sub {{ margin-top: 10px; }}
  details.sub > summary {{ cursor: pointer; font-size: 0.75rem; color: #64748b; }}
  .opt {{ text-transform: none; letter-spacing: 0; font-weight: 400; color: #475569; margin-left: 6px; }}
  .fold-state {{ float: right; text-transform: none; letter-spacing: 0; font-weight: 400; color: #94a3b8; }}
  .exec-hint {{ font-size: 0.75rem; color: #475569; margin-top: 6px; min-height: 1.5em; }}
  .toast {{ position: fixed; bottom: 24px; right: 24px; background: #00ff7f; color: #0a0f1a; padding: 12px 20px; border-radius: 8px; font-weight: 700; font-size: 0.875rem; opacity: 0; transition: opacity 0.3s; pointer-events: none; }}
  .toast.show {{ opacity: 1; }}
  .toast.error {{ background: #ef4444; color: #fff; }}
  #ollama-section {{ display: none; }}
  #cloud-section {{ display: none; }}
</style>
</head>
<body>
<div class="card">
  <div class="brand">SPEAKMAN.AI</div>
  <p class="subtitle">Run multi-agent workflows through the AI assistant you already use. No API key needed to start.</p>

  <div class="note" id="mode-warning" style="display:none; margin-bottom:20px">
    No model API is set up, but this install is set to run AI steps on one, so those steps will fail.
    <div class="btn-row"><button class="btn btn-outline" onclick="chooseRunner('assistant')">Let my assistant run the steps</button></div>
  </div>

  <div class="section" id="runner-section" style="display:none">
    <h2>Who runs the AI steps?</h2>
    <div class="choice-group">
      <div class="choice" id="choice-assistant" onclick="chooseRunner('assistant')">
        <div class="choice-title">My AI assistant</div>
        <div class="choice-sub">No API key. Your connected assistant does the work.</div>
      </div>
      <div class="choice" id="choice-model" onclick="chooseRunner('model')">
        <div class="choice-title">My own model API</div>
        <div class="choice-sub">Steps run in the background on your provider.</div>
      </div>
    </div>
    <div class="exec-hint" id="runner-hint"></div>
  </div>

  <div class="section">
    <h2>Connect your AI assistant</h2>
    <div class="radio-group" id="assistant-group">
      <label data-a="claude_code" onclick="setAssistant('claude_code')">Claude Code</label>
      <label data-a="claude_app" onclick="setAssistant('claude_app')">Claude app</label>
      <label data-a="copilot" onclick="setAssistant('copilot')">Copilot Studio</label>
      <label data-a="cursor" onclick="setAssistant('cursor')">Cursor</label>
      <label data-a="codex" onclick="setAssistant('codex')">Codex</label>
    </div>

    <div class="panel" id="panel-claude_code">
      <div class="status-line"><span class="status-dot" id="cc-dot"></span><span id="cc-status">Checking for Claude Code...</span></div>
      <div id="cc-ready" style="display:none">
        <button class="btn btn-primary" id="cc-connect" onclick="connectClient('claude_code')">Connect Claude Code</button>
        <div class="exec-hint" id="cc-hint">One click: registers this server for all your projects and installs the skills.</div>
      </div>
      <div id="cc-missing" style="display:none">
        <div class="btn-row" style="margin-top:0">
          <button class="btn btn-primary" style="width:auto" onclick="refreshAssistantStatus(true)">Check again</button>
          <a class="btn btn-outline" href="https://code.claude.com/docs/en/overview" target="_blank" rel="noopener">Official install steps</a>
        </div>
        <ol class="steps">
          <li>Paste this into PowerShell:
            <div class="copy-row"><code id="cc-install-cmd">irm https://claude.ai/install.ps1 | iex</code><button class="btn btn-outline btn-copy" onclick="copyText('cc-install-cmd')">Copy</button></div></li>
          <li>Run <code>claude</code> once and sign in when your browser opens.</li>
          <li>Press Check again.</li>
        </ol>
      </div>
      <div class="next-box" id="cc-next" style="display:none">Next: open Claude Code in any folder and type <code>/generate-sad</code>. Restart it first if it was already open.<br>Using the chat side of the Claude app? Use the Claude app tab above.</div>
      <div class="exec-hint" id="skills-status"></div>
      <button class="btn btn-outline" id="skills-btn" onclick="installSkills()" style="display:none; margin-top:6px;">Install skills for Claude Code</button>
      <details class="sub" id="cc-path" style="display:none">
        <summary>Can't start <code>claude</code> in a terminal?</summary>
        <div class="exec-hint">The installer did not add it to your PATH. Connecting from this page works either way, but the first run of <code>claude</code> is where you sign in. To run it by its full path:</div>
        <div class="copy-row"><code id="cc-path-run">&amp; "$env:USERPROFILE\\.local\\bin\\claude.exe"</code><button class="btn btn-outline btn-copy" onclick="copyText('cc-path-run')">Copy</button></div>
        <div class="exec-hint">Or add it to PATH once, then open a new terminal:</div>
        <div class="copy-row"><code id="cc-path-add">[Environment]::SetEnvironmentVariable("Path", $env:Path + ";$env:USERPROFILE\\.local\\bin", "User")</code><button class="btn btn-outline btn-copy" onclick="copyText('cc-path-add')">Copy</button></div>
        <div class="exec-hint"><a href="https://code.claude.com/docs/en/troubleshoot-install#command-not-found-claude-after-installation" target="_blank" rel="noopener">Anthropic's PATH help</a></div>
      </details>
      <details class="sub" id="cc-manual">
        <summary>Do it by hand</summary>
        <div class="exec-hint">Run this in a terminal, then restart Claude Code:</div>
        <div class="copy-row"><code id="cc-cmd"></code><button class="btn btn-outline btn-copy" onclick="copyText('cc-cmd')">Copy</button></div>
      </details>
    </div>

    <div class="panel" id="panel-claude_app" style="display:none">
      <div class="exec-hint">Manual setup. The Claude app covers Claude Desktop and Cowork.</div>
      <ol class="steps">
        <li>In the app's settings, add a custom connector (MCP server) with this URL:
          <div class="copy-row"><code class="mcp-url" id="mcp-url-app"></code><button class="btn btn-outline btn-copy" onclick="copyText('mcp-url-app')">Copy</button></div></li>
        <li>Download the skills plugin and upload it in the app's settings: <span class="dl" data-name="speakmanai-sdlc.zip"></span> (all 17 skills in one upload).</li>
        <li>Ask the app to list the SPEAKMAN.AI workflows.</li>
      </ol>
      <div class="exec-hint">Single skills, if you cannot upload a plugin: <span class="dl" data-name="generate-sad.zip"></span>, <span class="dl" data-name="generate-compliance-report.zip"></span>, <span class="dl" data-name="generate-speakmanai-code.zip"></span>.</div>
      <div class="exec-hint">Experimental: with only the connector added, ask for a SPEAKMAN.AI deliverable and the app can fetch the skill from this server.</div>
    </div>

    <div class="panel" id="panel-copilot" style="display:none">
      <div class="exec-hint">Manual setup. Copilot Studio runs in the cloud, so it cannot reach this computer directly.</div>
      <ol class="steps">
        <li>Forward port <span class="port"></span> to a public URL (for example VS Code port forwarding), and open <code>/setup</code> through that URL once.</li>
        <li>In your agent, add an MCP connector using the public URL followed by <code>/mcp</code>.</li>
        <li>Download <span class="dl" data-name="copilot-studio-skills.zip"></span>, unzip it, and upload each item in the agent's Skills tab (17 uploads).</li>
      </ol>
      <div class="exec-hint">Full guide: <a href="https://github.com/speakmancip/speakmanai-cc/blob/master/desktop-skills/COPILOT_STUDIO.md" target="_blank" rel="noopener">COPILOT_STUDIO.md</a></div>
    </div>

    <div class="panel" id="panel-cursor" style="display:none">
      <div class="exec-hint">Not tested yet. Add this MCP server URL in Cursor's MCP settings:</div>
      <div class="copy-row"><code class="mcp-url" id="mcp-url-cursor"></code><button class="btn btn-outline btn-copy" onclick="copyText('mcp-url-cursor')">Copy</button></div>
    </div>

    <div class="panel" id="panel-codex" style="display:none">
      <div class="exec-hint">Not tested yet. Add this MCP server URL in Codex's MCP settings:</div>
      <div class="copy-row"><code class="mcp-url" id="mcp-url-codex"></code><button class="btn btn-outline btn-copy" onclick="copyText('mcp-url-codex')">Copy</button></div>
    </div>
  </div>

  <div class="warn-box" id="wf-box" style="display:none">
    <div id="wf-text"></div>
    <button class="btn btn-outline" id="wf-btn" onclick="updateWorkflows()">Update workflows</button>
  </div>

  <details class="fold" id="model-fold" {model_open}>
    <summary>Model API <span class="opt">optional</span><span class="fold-state" id="model-state"></span></summary>
    <div class="exec-hint" id="model-hint"></div>

    <div class="section" style="margin-top:14px">
      <div class="radio-group" id="provider-group">
        <label class="{'active' if provider == 'gemini' else ''}" onclick="setProvider('gemini')"><span>Gemini</span></label>
        <label class="{'active' if provider == 'anthropic' else ''}" onclick="setProvider('anthropic')"><span>Claude</span></label>
        <label class="{'active' if provider == 'openai' else ''}" onclick="setProvider('openai')"><span>OpenAI</span></label>
        <label class="{'active' if provider == 'vertexai' else ''}" onclick="setProvider('vertexai')"><span>Vertex AI</span></label>
        <label class="{'active' if provider == 'ollama' else ''}" onclick="setProvider('ollama')"><span>Ollama</span></label>
      </div>
    </div>

    <div id="cloud-section" class="section">
      <div id="api-key-field">
        <label class="field-label">API Key</label>
        <input type="password" id="api-key" placeholder="Paste your API key">
      </div>
      <div id="vertex-fields" style="display:none">
        <label class="field-label">GCP Project ID</label>
        <input type="text" id="gcp-project-id" placeholder="my-gcp-project" value="{saved_gcp_project}">
        <label class="field-label">GCP Region</label>
        <input type="text" id="gcp-region" placeholder="us-east1" value="{saved_gcp_region}">
        <div class="note">Vertex AI signs in with your Google account, so there is no API key. Run <code>gcloud auth application-default login</code> on this computer once.</div>
      </div>
      <label class="field-label">Fast model (quick, lightweight steps)</label>
      <select id="default-model"></select>
      <label class="field-label">Standard model (general reasoning)</label>
      <select id="standard-model"></select>
      <label class="field-label">Advanced model (complex, multi-step reasoning)</label>
      <select id="advanced-model"></select>
    </div>

    <div id="ollama-section" class="section">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
        <span id="ollama-dot" class="status-dot"></span>
        <span id="ollama-status" style="font-size:0.875rem;color:#64748b;">Checking...</span>
      </div>
      <label class="field-label">Base URL</label>
      <input type="text" id="ollama-url" value="http://localhost:11434">
      <label class="field-label">Model</label>
      <select id="ollama-model" onchange="checkModelSize()"><option>Loading...</option></select>
      <div id="ollama-warning" class="note" style="display:none">
        Models under 14B often produce incomplete outputs for multi-step workflows.
        A 14B+ model (~10 GB RAM) is recommended. 32B+ for best quality.
      </div>
    </div>

    <button class="btn btn-primary" onclick="saveConfig()">Save model settings</button>
  </details>

  <details class="fold" id="adv-fold">
    <summary>Advanced</summary>
    <label class="field-label">Execution mode</label>
    <select id="exec-mode" onchange="updateExecHint()">
      <option value="delegate_only">Delegate Only: your assistant runs every AI step, model API paused</option>
      <option value="auto">Auto: each agent uses its configured model</option>
      <option value="force_delegate">Force Delegate: your assistant runs every AI step, validators use the model API</option>
      <option value="force_background">Force Background: every step runs on the model API</option>
    </select>
    <div class="exec-hint" id="exec-hint"></div>
    <button class="btn btn-outline" style="margin-top:10px" onclick="saveMode()">Apply mode</button>
  </details>

  <details class="fold" id="fb-fold">
    <summary>Send feedback <span class="opt">optional</span></summary>
    <label class="field-label">What is this about?</label>
    <select id="fb-type" onchange="fbChange()">
      <option value="bug">Bug report</option>
      <option value="request">Feature request</option>
      <option value="question">Question</option>
      <option value="updates">Keep me updated (product news by email)</option>
    </select>
    <label class="field-label" id="fb-email-label">Your email (optional)</label>
    <input type="email" id="fb-email" autocomplete="email" oninput="fbPreview()">
    <label class="field-label">Message</label>
    <textarea id="fb-message" rows="4" maxlength="5000" oninput="fbPreview()"></textarea>
    <div class="exec-hint" id="fb-preview"></div>
    <div class="exec-hint" id="fb-consent" style="display:none">By sending this you agree to receive product emails from SPEAKMAN.AI. You can unsubscribe at any time.</div>
    <button class="btn btn-outline" style="margin-top:10px" onclick="sendFeedback()">Send</button>
  </details>
</div>

<div class="toast" id="toast"></div>

<script>
const MODELS = {{
  gemini:    {{ fast: ['gemini-3.5-flash-lite','gemini-3.1-flash-lite','gemini-3.8-flash'], standard: ['gemini-3.8-flash','gemini-3-flash-preview','gemini-3.5-flash-lite'], advanced: ['gemini-3.1-pro-preview','gemini-3.8-flash'] }},
  anthropic: {{ fast: ['claude-haiku-5-5','claude-sonnet-5-5'], standard: ['claude-sonnet-5-5','claude-haiku-5-5','claude-sonnet-4-6','claude-sonnet-5'], advanced: ['claude-opus-5-5','claude-sonnet-5-5','claude-opus-4-8','claude-sonnet-4-6','claude-sonnet-5','claude-fable-5'] }},
  openai:    {{ fast: ['gpt-6-luna','gpt-6.1-sol','gpt-4o-mini'], standard: ['gpt-6.1-sol','gpt-6-luna','gpt-4o'], advanced: ['gpt-6-astra','gpt-6.1-sol','gpt-4o'] }},
  vertexai:  {{ fast: ['gemini-3.5-flash-lite','gemini-3.1-flash-lite','gemini-3.8-flash'], standard: ['gemini-3.8-flash','gemini-3-flash-preview','gemini-3.5-flash-lite'], advanced: ['gemini-3.1-pro-preview','gemini-3.8-flash'] }},
  ollama:    {{ fast: [], standard: [], advanced: [] }},
}};
const PROVIDER_NAMES = {{ gemini: 'Gemini', anthropic: 'Claude', openai: 'OpenAI', vertexai: 'Vertex AI', ollama: 'Ollama' }};

const SAVED = {{ fast: '{saved_fast}', standard: '{saved_standard}', advanced: '{saved_advanced}' }};
const HAS_KEY = {has_key_js};
const SAVED_PROVIDER = '{provider}';
const MCP_PORT = Number(location.port) || 8000;
const MCP_URL = 'http://localhost:' + MCP_PORT + '/mcp';

let currentProvider = '{provider}';
let modelConfigured = {configured_js};
let execMode = '{saved_exec}';
// Adding a first model API is a request to use it. Editing an existing one is not.
const CONFIGURED_AT_LOAD = modelConfigured;

const $ = id => document.getElementById(id);

// ---- Who runs the AI steps ----
function renderRunner() {{
  const assistant = execMode === 'delegate_only' || execMode === 'force_delegate';
  // The choice, and the mode list under Advanced, only appear when there is something to
  // choose between. With no model API the assistant runs the steps and that is all.
  $('runner-section').style.display = modelConfigured ? 'block' : 'none';
  $('adv-fold').style.display = (modelConfigured || !assistant) ? '' : 'none';
  $('mode-warning').style.display = (!modelConfigured && !assistant) ? 'block' : 'none';
  $('choice-assistant').classList.toggle('active', assistant);
  $('choice-model').classList.toggle('active', !assistant);
  let hint;
  if (execMode === 'delegate_only') hint = 'Your connected assistant runs every AI step. No model API is called, and your saved model settings are kept.';
  else if (execMode === 'force_delegate') hint = 'Your connected assistant runs every AI step. Validators still use your model API. (Force Delegate, set under Advanced.)';
  else if (execMode === 'force_background') hint = 'Every step runs on your model API. (Force Background, set under Advanced.)';
  else hint = 'AI steps run on your model API (' + (PROVIDER_NAMES[SAVED_PROVIDER] || SAVED_PROVIDER) + '). Steps a workflow marks for your assistant still go to your assistant.';
  $('runner-hint').textContent = hint;
  $('model-state').textContent = modelConfigured ? (PROVIDER_NAMES[SAVED_PROVIDER] || SAVED_PROVIDER) : 'not set';
  $('model-hint').textContent = modelConfigured
    ? 'Used when AI steps run on your own model API, and by validators.'
    : 'Not needed: your assistant runs the AI steps. Add one if you want steps to run in the background on your own provider.';
  $('exec-mode').value = execMode;
  updateExecHint();
}}

async function postConfig(body) {{
  const r = await fetch('/api/config', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(body) }});
  const d = await r.json();
  if (d.execution_mode) execMode = d.execution_mode;
  if (typeof d.model_api_configured === 'boolean') modelConfigured = d.model_api_configured;
  return d;
}}

async function chooseRunner(which) {{
  const d = await postConfig(which === 'assistant' ? {{ execution_mode: 'delegate_only' }} : {{ use_model_api: true }});
  showToast(d.ok ? (which === 'assistant' ? 'Your assistant now runs the AI steps.' : d.message) : d.message, !d.ok);
  renderRunner();
}}

// ---- Assistant picker ----
function setAssistant(a) {{
  document.querySelectorAll('#assistant-group label').forEach(l => l.classList.toggle('active', l.dataset.a === a));
  document.querySelectorAll('.panel').forEach(p => p.style.display = p.id === 'panel-' + a ? 'block' : 'none');
  try {{ localStorage.setItem('speakmanai-assistant', a); }} catch (e) {{}}
}}

async function refreshAssistantStatus(announce) {{
  let st;
  try {{ st = await (await fetch('/api/assistant-status')).json(); }} catch (e) {{ $('cc-status').textContent = 'Could not check for Claude Code.'; return; }}
  const cc = st.claude_code;
  $('cc-ready').style.display = cc.detected ? 'block' : 'none';
  $('cc-missing').style.display = cc.cli_found ? 'none' : 'block';
  $('cc-path').style.display = cc.windows && cc.cli_found && !cc.cli_on_path ? 'block' : 'none';
  $('cc-next').style.display = cc.registered ? 'block' : 'none';
  $('cc-hint').style.display = cc.registered ? 'none' : 'block';
  const btn = $('cc-connect');
  if (cc.registered) {{
    $('cc-dot').className = 'status-dot up';
    $('cc-status').textContent = 'Connected to Claude Code.';
    btn.textContent = 'Reconnect Claude Code';
    btn.className = 'btn btn-outline';
  }} else if (cc.cli_found) {{
    $('cc-dot').className = 'status-dot';
    $('cc-status').textContent = 'Claude Code found. Not connected yet.';
    btn.textContent = 'Connect Claude Code';
    btn.className = 'btn btn-primary';
  }} else if (cc.detected) {{
    $('cc-dot').className = 'status-dot warn';
    $('cc-status').textContent = 'Claude Code CLI was not found on this computer.';
  }} else {{
    $('cc-dot').className = 'status-dot warn';
    $('cc-status').textContent = 'Claude Code CLI was not found on this computer.';
  }}
  modelConfigured = st.model_api_configured;
  execMode = st.execution_mode;
  renderRunner();
  if (announce) showToast(cc.cli_found ? 'Claude Code found.' : 'Still not found. Install Claude Code and run it once.', !cc.cli_found);
}}

async function connectClient(name) {{
  const btn = $('cc-connect');
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = 'Connecting...';
  try {{
    const r = await fetch('/api/setup-mcp-client', {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{ client: name, port: MCP_PORT }})
    }});
    const d = await r.json();
    // The skills are useful even when registration has to be finished by hand.
    const s = await postInstallSkills();
    const skillsMsg = s.bundled === false ? '' : ' ' + s.message;
    showToast(d.message + skillsMsg, !d.ok || s.ok === false && s.bundled !== false);
    if (!d.ok) $('cc-manual').open = true;
  }} finally {{
    btn.disabled = false; btn.textContent = label;
    await refreshAssistantStatus();
    refreshSkillsStatus();
  }}
}}

// ---- Skills ----
async function postInstallSkills() {{
  const st = await (await fetch('/api/skills-status')).json();
  if (!st.bundled) return {{ ok: false, bundled: false, message: st.message }};
  const r = await fetch('/api/install-skills', {{ method: 'POST' }});
  return await r.json();
}}

async function installSkills() {{
  const s = await postInstallSkills();
  showToast(s.message, !s.ok);
  refreshSkillsStatus();
}}

const SKILLS_STATE_TEXT = {{
  not_installed:    'not installed yet',
  up_to_date:       'installed and up to date',
  update_available: 'update available',
}};

async function refreshSkillsStatus() {{
  const el = $('skills-status');
  const btn = $('skills-btn');
  let st;
  try {{ st = await (await fetch('/api/skills-status')).json(); }} catch (e) {{ el.textContent = ''; return; }}
  const downloads = st.bundled ? st.downloads : [];
  document.querySelectorAll('.dl').forEach(span => {{
    const name = span.dataset.name;
    span.textContent = '';
    if (downloads.includes(name)) {{
      const a = document.createElement('a');
      a.href = '/downloads/skills/' + encodeURIComponent(name);
      a.textContent = name;
      span.appendChild(a);
    }} else {{
      span.textContent = name + ' (not included in this build)';
    }}
  }});
  if (!st.bundled) {{ el.textContent = 'This build does not include the skills.'; btn.style.display = 'none'; return; }}
  let text = st.skills + ' skills: ' + (SKILLS_STATE_TEXT[st.state] || st.state) + '.';
  if (st.counts.modified) text += ' ' + st.counts.modified + ' files you changed will be backed up before they are replaced.';
  el.textContent = text;
  // A first install happens as part of Connect; this button is for updates.
  btn.style.display = st.state === 'update_available' && st.claude_code_detected ? 'inline-flex' : 'none';
  btn.textContent = 'Update skills';
}}

// ---- Workflows ----
async function refreshWorkflowsStatus() {{
  const box = $('wf-box');
  let st;
  try {{ st = await (await fetch('/api/workflows-status')).json(); }} catch (e) {{ box.style.display = 'none'; return; }}
  if (st.state !== 'differs') {{ box.style.display = 'none'; return; }}
  const n = st.differs.length;
  const retired = st.differs.reduce((s, d) => s + d.agents_retired.length, 0);
  let text = n + ' of ' + st.total + ' workflows on this computer differ from this version. They may be older, ' +
    'missing review gates, or edited. Updating replaces them with this version. The versions it replaces are saved ' +
    'first in your .speakmanai/backups folder. Workflows you created under your own name are not touched.';
  if (retired) text += ' ' + retired + ' agent link' + (retired === 1 ? '' : 's') + ' that this version no longer uses will be removed from the workflows.';
  $('wf-text').textContent = text;
  box.style.display = 'block';
}}

async function updateWorkflows() {{
  const btn = $('wf-btn');
  btn.disabled = true; btn.textContent = 'Updating...';
  try {{
    const r = await (await fetch('/api/update-workflows', {{ method: 'POST' }})).json();
    showToast(r.message, !r.ok);
  }} catch (e) {{
    showToast('Could not update the workflows: ' + e, true);
  }} finally {{
    btn.disabled = false; btn.textContent = 'Update workflows';
    refreshWorkflowsStatus();
  }}
}}

// ---- Model API ----
function setProvider(p) {{
  currentProvider = p;
  document.querySelectorAll('#provider-group label').forEach((l,i) => {{
    l.classList.toggle('active', ['gemini','anthropic','openai','vertexai','ollama'][i] === p);
  }});
  $('cloud-section').style.display = p === 'ollama' ? 'none' : 'block';
  $('ollama-section').style.display = p === 'ollama' ? 'block' : 'none';
  $('api-key-field').style.display = p === 'vertexai' ? 'none' : 'block';
  $('vertex-fields').style.display = p === 'vertexai' ? 'block' : 'none';
  $('api-key').placeholder = HAS_KEY[p] ? 'Key saved. Paste a new one to replace it' : 'Paste your API key';
  if (p !== 'ollama') populateModelDropdowns(p);
  if (p === 'ollama') checkOllama();
}}

function populateModelDropdowns(p) {{
  const m = MODELS[p] || {{ fast: [], standard: [], advanced: [] }};
  const fill = (id, opts, saved) => {{
    const sel = $(id);
    sel.innerHTML = opts.map(o => `<option value="${{o}}">${{o}}</option>`).join('');
    if (saved && opts.includes(saved)) sel.value = saved;
  }};
  fill('default-model',  m.fast,     SAVED.fast);
  fill('standard-model', m.standard, SAVED.standard);
  fill('advanced-model', m.advanced, SAVED.advanced);
}}

async function checkOllama() {{
  const dot = $('ollama-dot');
  const status = $('ollama-status');
  const sel = $('ollama-model');
  try {{
    const r = await fetch('/api/check-ollama');
    const d = await r.json();
    if (d.available) {{
      dot.className = 'status-dot up';
      status.textContent = 'Running';
      sel.innerHTML = d.models.length
        ? d.models.map(m => `<option value="${{m.name}}" data-param-size="${{m.parameter_size}}">${{m.name}}${{m.parameter_size ? ' (' + m.parameter_size + ')' : ''}}</option>`).join('')
        : '<option>No models found. Run: ollama pull qwen2.5:14b</option>';
      checkModelSize();
    }} else {{
      dot.className = 'status-dot warn';
      status.textContent = 'Ollama is not running. Start it, then pick Ollama again.';
      sel.innerHTML = '<option>Ollama offline</option>';
    }}
  }} catch(e) {{
    dot.className = 'status-dot warn';
    status.textContent = 'Cannot reach Ollama';
  }}
}}

function checkModelSize() {{
  const sel = $('ollama-model');
  const warn = $('ollama-warning');
  const opt = sel.options[sel.selectedIndex];
  const paramSize = opt ? opt.getAttribute('data-param-size') || '' : '';
  // Use parameter_size from API (e.g. "8B", "14B"), falling back to parsing the name
  const match = paramSize.match(/(\\d+(?:\\.\\d+)?)/i) || (sel.value || '').match(/(\\d+)b/i);
  const size = match ? parseFloat(match[1]) : 99;
  warn.style.display = size < 14 ? 'block' : 'none';
}}

async function saveConfig() {{
  const p = currentProvider;
  const body = {{ llm_provider: p }};
  if (p === 'ollama') {{
    body.ollama_base_url = $('ollama-url').value;
    const ollamaModel = $('ollama-model').value;
    body.default_model   = ollamaModel;
    body.standard_model  = ollamaModel;
    body.advanced_model  = ollamaModel;
  }} else if (p === 'vertexai') {{
    if (!$('gcp-project-id').value.trim()) {{ showToast('Enter a GCP project ID first.', true); return; }}
    body.gcp_project_id = $('gcp-project-id').value.trim();
    body.gcp_region     = $('gcp-region').value.trim();
    body.gemini_api_key = '';  // Vertex AI uses ADC, not an API key, so clear any saved Gemini key
    body.default_model   = $('default-model').value;
    body.standard_model  = $('standard-model').value;
    body.advanced_model  = $('advanced-model').value;
  }} else {{
    const key = $('api-key').value;
    if (!key && !HAS_KEY[p]) {{ showToast('Paste an API key first.', true); return; }}
    if (key) body[p + '_api_key'] = key;
    if (p === 'gemini') {{ body.gcp_project_id = ''; body.gcp_region = ''; }}
    body.default_model   = $('default-model').value;
    body.standard_model  = $('standard-model').value;
    body.advanced_model  = $('advanced-model').value;
  }}
  // Setting up a first model API switches AI steps to it (the server only does so once an
  // API is really set up). Editing an existing one leaves who-runs-the-steps alone.
  if (!CONFIGURED_AT_LOAD) body.use_model_api = true;
  const d = await postConfig(body);
  if (d.ok) {{
    if ($('api-key').value) {{ HAS_KEY[p] = true; $('api-key').value = ''; }}
    let msg = d.message;
    if (!body.use_model_api && (execMode === 'delegate_only') && modelConfigured) msg = 'Model settings saved. Your assistant still runs the AI steps. Choose "My own model API" at the top to use this model.';
    showToast(msg);
    setTimeout(() => location.reload(), 1600);
  }} else {{
    showToast(d.message, true);
  }}
}}

// ---- Advanced ----
const EXEC_HINTS = {{
  auto:             'Each agent runs with its own configured model. Steps a workflow marks for your assistant are handed to it.',
  force_delegate:   'Every AI step pauses and is handed to your connected assistant. Validators still call your model API when one is set up.',
  delegate_only:    'Every AI step is handed to your connected assistant and the model API is paused: no calls, no failures when you are out of credits, and your saved keys are kept. Validators are skipped and marked as such.',
  force_background: 'Steps set to be handed to an assistant run on your model API instead. Fully hands-off, and needs a model API.',
}};

function updateExecHint() {{
  $('exec-hint').textContent = EXEC_HINTS[$('exec-mode').value] || '';
}}

async function saveMode() {{
  const mode = $('exec-mode').value;
  const d = await postConfig({{ execution_mode: mode }});
  const needsApi = (mode === 'auto' || mode === 'force_background') && !modelConfigured;
  showToast(d.ok ? (needsApi ? 'Mode applied, but no model API is set up, so those steps will fail.' : 'Mode applied.') : d.message, !d.ok || needsApi);
  renderRunner();
}}

// ---- Feedback ----
const FB_LABELS = {{ bug: 'Bug report', request: 'Feature request', question: 'Question', updates: 'Keep me updated' }};
function fbPreview() {{
  const t = $('fb-type').value, email = $('fb-email').value.trim();
  $('fb-preview').textContent = 'This sends only: the type (' + FB_LABELS[t] + '), '
    + (email ? 'your email (' + email + '), ' : 'no email, ')
    + 'and the message above, to SPEAKMAN.AI through HubSpot. Nothing else leaves this computer: no workflow data, documents or settings.';
}}
function fbChange() {{
  const updates = $('fb-type').value === 'updates';
  $('fb-email-label').textContent = updates ? 'Your email (required)' : 'Your email (optional)';
  $('fb-consent').style.display = updates ? 'block' : 'none';
  fbPreview();
}}
async function sendFeedback() {{
  const body = {{ type: $('fb-type').value, email: $('fb-email').value, message: $('fb-message').value }};
  let d;
  try {{
    const r = await fetch('/api/feedback', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(body) }});
    d = await r.json();
  }} catch (e) {{
    d = {{ ok: false, message: 'Could not send. Nothing was sent.' }};
  }}
  showToast(d.message, !d.ok);
  if (d.ok) {{ $('fb-message').value = ''; fbPreview(); }}
}}
fbChange();

// ---- Shared ----
async function copyText(id) {{
  const text = $(id).textContent;
  try {{
    await navigator.clipboard.writeText(text);
    showToast('Copied');
  }} catch (e) {{
    showToast('Copy failed. Select the text and copy it manually.', true);
  }}
}}

function showToast(msg, isError=false) {{
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show' + (isError ? ' error' : '');
  setTimeout(() => t.className = 'toast', isError ? 6000 : 3500);
}}

// Initialise on load
document.querySelectorAll('.mcp-url').forEach(el => el.textContent = MCP_URL);
document.querySelectorAll('.port').forEach(el => el.textContent = MCP_PORT);
$('cc-cmd').textContent = 'claude mcp add --transport http --scope user {_MCP_SERVER_NAME} ' + MCP_URL;
let startAssistant = 'claude_code';
try {{ startAssistant = new URLSearchParams(location.search).get('assistant') || localStorage.getItem('speakmanai-assistant') || 'claude_code'; }} catch (e) {{}}
if (!$('panel-' + startAssistant)) startAssistant = 'claude_code';
setAssistant(startAssistant);
setProvider(currentProvider);
renderRunner();
refreshAssistantStatus();
refreshSkillsStatus();
refreshWorkflowsStatus();
</script>
</body>
</html>"""


# Mount MCP handler at root.
# The API gateway preserves /mcp prefix (rewritePrefix: '/mcp'), so upstream receives /mcp/*.
# FastMCP's internal Starlette route is at /mcp, which matches.
app.mount("/", mcp_asgi)
