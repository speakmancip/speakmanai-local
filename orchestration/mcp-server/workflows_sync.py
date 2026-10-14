"""
Keep the shipped workflows and agents in the database in step with this build.

Startup seeding (seed_if_empty in server.py) only adds a workflow whose workflowId is not in the
database yet. It never changes one that is there, so a database created by an older version keeps
that version's prompts, review gates and agent lists. This module compares the database with the
workflow files shipped in this build and, when the user asks, replaces what differs.

Policy (owner decision, 2026-10-14): the shipped workflow IDs belong to the product. An update
replaces them and keeps a backup of what it replaced. A user who wants a customized workflow
copies it to a new workflowId, which an update never touches. The update is a button on the setup
page, never automatic, so replacing a changed workflow is always a conscious act.

What is compared, for each shipped workflow:
  - the workflow document's own fields,
  - each agent listed for it: every field the shipped file sets, and any managed field the shipped
    file no longer sets (a removed review gate, for example),
  - membership: an agent the database still lists under this workflow but the shipped files no
    longer do (a retired agent, such as the Technical Writer, which the planner would still offer).
Fields the shipped files never set (for example a per-agent executionMode a user chose) are left
alone, and so are workflows and agents that are not shipped.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Agent fields that come from the shipped files. If the database has one of these and the shipped
# agent does not, the shipped version removed it, so an update removes it too.
_AGENT_MANAGED = (
    "agentType", "dependencies", "description", "displayName", "grounded", "humanFeedbackConfig",
    "humanFeedbackId", "inputSchema", "mimeType", "minSubscriptionLevel", "model", "systemPrompt",
    "temperature", "validationConfig", "validatorAgentId",
)
_STRIPPED = ("_id", "createdAt", "updatedAt", "tenantId")  # same fields seed_if_empty strips


def _clean(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in _STRIPPED}


def load_shipped(wf_dir: Path) -> dict:
    """Read the shipped workflow files: workflow documents, agent definitions, and which agents
    belong to which workflow. An agent entry with nothing but an id (and a workflows list) is only
    a membership reference, for example the shared planner listed in several files."""
    workflows, agents, membership = {}, {}, {}
    for path in sorted(wf_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"Could not read {path.name} for the workflow check: {e}")
            continue
        wf = data.get("workflow")
        if not wf or "workflowId" not in wf:
            continue
        wid = wf["workflowId"]
        workflows[wid] = _clean(wf)
        membership.setdefault(wid, set())
        for entry in data.get("agents", []):
            if "agentId" not in entry:
                continue
            agent = _clean(entry)
            listed_in = agent.pop("workflows", None) or [wid]
            for w in listed_in:
                membership.setdefault(w, set()).add(agent["agentId"])
            if len(agent) > 1 and agent["agentId"] not in agents:
                agents[agent["agentId"]] = agent
    return {"workflows": workflows, "agents": agents, "membership": {w: sorted(a) for w, a in membership.items()}}


async def compare(db, shipped: dict) -> list:
    """One entry per shipped workflow: state 'up_to_date' or 'differs', and what differs."""
    all_agents = await db["agents"].find({}).to_list(length=None)
    by_id = {a["agentId"]: a for a in all_agents if "agentId" in a}
    report = []
    for wid, swf in shipped["workflows"].items():
        members = shipped["membership"].get(wid, [])
        dbw = await db["workflows"].find_one({"workflowId": wid})
        item = {
            "workflowId": wid,
            "displayName": swf.get("displayName") or wid,
            "workflow_missing": dbw is None,
            "workflow_fields": [] if dbw is None else sorted(k for k, v in swf.items() if dbw.get(k) != v),
            "agents": [],
            "retired": [],
        }
        for aid in members:
            sa = shipped["agents"].get(aid)
            if sa is None:
                continue  # listed here but defined in no shipped file: nothing to compare
            da = by_id.get(aid)
            if da is None:
                item["agents"].append({"agentId": aid, "change": "missing", "fields": []})
                continue
            fields = [k for k, v in sa.items() if da.get(k) != v]
            fields += [k for k in _AGENT_MANAGED if k in da and k not in sa]
            if fields:
                item["agents"].append({"agentId": aid, "change": "changed", "fields": sorted(set(fields))})
            elif wid not in (da.get("workflows") or []):
                item["agents"].append({"agentId": aid, "change": "unlinked", "fields": []})
        item["retired"] = sorted(a["agentId"] for a in all_agents
                                 if "agentId" in a and wid in (a.get("workflows") or []) and a["agentId"] not in members)
        differs = item["workflow_missing"] or item["workflow_fields"] or item["agents"] or item["retired"]
        item["state"] = "differs" if differs else "up_to_date"
        report.append(item)
    return report


def _summary(report: list) -> dict:
    differing = [w for w in report if w["state"] != "up_to_date"]
    return {
        "state": "differs" if differing else "up_to_date",
        "total": len(report),
        "differs": [
            {
                "workflowId": w["workflowId"],
                "displayName": w["displayName"],
                "agents_changed": len(w["agents"]),
                "agents_retired": w["retired"],
                "workflow_missing": w["workflow_missing"],
            }
            for w in differing
        ],
    }


async def status(db, wf_dir: Path) -> dict:
    shipped = load_shipped(wf_dir)
    if not shipped["workflows"]:
        return {"state": "unavailable", "total": 0, "differs": [], "message": "This build has no shipped workflow files."}
    return _summary(await compare(db, shipped))


async def apply(db, wf_dir: Path, backup_dir: Path, app_version: str = "") -> dict:
    """Replace every shipped workflow that differs. The documents being replaced are written to a
    backup file first; if that fails, nothing is changed."""
    shipped = load_shipped(wf_dir)
    if not shipped["workflows"]:
        return {"ok": False, "message": "This build has no shipped workflow files."}
    todo = [w for w in await compare(db, shipped) if w["state"] != "up_to_date"]
    if not todo:
        return {"ok": True, "updated": 0, "agents": 0, "retired": 0, "backup": None, "message": "Workflows are already up to date."}

    stamp = datetime.now(timezone.utc)
    backup = {"created": stamp.isoformat(), "app_version": app_version, "workflows": {}, "agents": {}}
    for w in todo:
        dbw = await db["workflows"].find_one({"workflowId": w["workflowId"]})
        if dbw:
            backup["workflows"][w["workflowId"]] = dbw
        for aid in [a["agentId"] for a in w["agents"]] + w["retired"]:
            da = await db["agents"].find_one({"agentId": aid})
            if da:
                backup["agents"][aid] = da
    backup_path = None
    if backup["workflows"] or backup["agents"]:  # nothing to back up on a first seed
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"workflows-{stamp.strftime('%Y%m%d-%H%M%S')}.json"
        backup_path.write_text(json.dumps(backup, indent=2, default=str), encoding="utf-8")

    agents_written = retired_count = 0
    for w in todo:
        wid = w["workflowId"]
        await db["workflows"].update_one({"workflowId": wid}, {"$set": shipped["workflows"][wid]}, upsert=True)
        for item in w["agents"]:
            sa = shipped["agents"][item["agentId"]]
            current = await db["agents"].find_one({"agentId": item["agentId"]}) or {}
            op = {"$set": dict(sa), "$addToSet": {"workflows": wid}}
            stale = [k for k in _AGENT_MANAGED if k in current and k not in sa]
            if stale:
                op["$unset"] = {k: "" for k in stale}
            await db["agents"].update_one({"agentId": item["agentId"]}, op, upsert=True)
            agents_written += 1
        for aid in w["retired"]:
            da = await db["agents"].find_one({"agentId": aid}) or {}
            keep = [x for x in (da.get("workflows") or []) if x != wid]
            await db["agents"].update_one({"agentId": aid}, {"$set": {"workflows": keep}})
            retired_count += 1

    names = ", ".join(w["displayName"] for w in todo)
    log.info(f"Workflows updated: {names}. {agents_written} agents written, {retired_count} retired. Backup: {backup_path or 'none needed'}")
    saved = f" The previous versions are saved in {backup_path}." if backup_path else ""
    return {
        "ok": True,
        "updated": len(todo),
        "agents": agents_written,
        "retired": retired_count,
        "backup": str(backup_path) if backup_path else None,
        "message": f"Updated {len(todo)} workflow{'s' if len(todo) != 1 else ''} ({agents_written} agents).{saved}",
    }
