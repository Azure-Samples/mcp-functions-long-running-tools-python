"""Long-running MCP tools using Durable Functions (Python).

Two MCP tools implement the "budgeted single call + poll fallback" pattern for long-running
work, backed by a Durable Functions orchestration (a proof-of-work miner):

* ``start_mining`` starts the orchestration and awaits it up to a short budget. Quick jobs
  return their result inline (the poll tool is never needed); long jobs return a ``workflow_id``
  handle and an instruction to poll.
* ``get_mining_result`` polls a previously started workflow by its ``workflow_id``.

The MCP Tasks extension (SEP-2663) was introduced in the 2026-07-28 release candidate for
building long-running tools. However, until it's broadly supported in the ecosystem, we need a
solution today -- and Durable Functions is a good fit here.
"""

import asyncio
import hashlib
import json
import logging
import os
import time

import azure.functions as func
import azure.durable_functions as df

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)

# The chain length is fixed; difficulty is the knob that controls how long mining takes.
BLOCK_COUNT = 4

# The wait budget is bounded by the *client* tool-call timeout (non-standard, often ~30-60s),
# not the Functions host timeout. Default conservatively to stay under aggressive clients.
WAIT_BUDGET_SECONDS = int(os.environ.get("WaitBudgetSeconds", "20"))

# Default mining difficulty (leading zero bits). Higher = longer. Callers can override it
# per request via the tool's optional "difficulty" argument.
DEFAULT_DIFFICULTY = int(os.environ.get("MiningDifficulty", "24"))


# ---------------------------------------------------------------------------
# Durable orchestration: a tiny, dependency-free proof-of-work "miner".
#
# It mines a short chain of blocks. Each block must have a SHA-256 hash with at least
# ``difficulty`` leading zero bits, found by trying nonces 0, 1, 2, ... until one works. Each
# block also includes the previous block's hash, so the blocks form a chain -- a natural example
# of Durable's *function-chaining* pattern: every step depends on the output of the step before.
#
# The work is real CPU work (no sleeps, no external services); its duration is controlled
# entirely by ``difficulty``: each extra bit roughly doubles the expected number of hashes. That
# single knob is what lets the sample demonstrate both the inline path (quick) and the poll path
# (slow) -- see the tools below and the README.
# ---------------------------------------------------------------------------


@app.orchestration_trigger(context_name="context")
def run_orchestrator(context: df.DurableOrchestrationContext):
    """Mines ``BLOCK_COUNT`` blocks by chaining one activity call per block.

    Each block is mined from the previous block's hash, so step N+1 depends on step N's output.
    The CPU-heavy mining happens in the activity, keeping the orchestrator deterministic (a
    requirement for Durable orchestrations).
    """
    difficulty = context.get_input()
    logger = logging.getLogger("MiningOrchestrator")
    if not context.is_replaying:
        logger.info("Mining %s blocks at difficulty %s", BLOCK_COUNT, difficulty)

    blocks = []
    previous_hash = "GENESIS"
    for index in range(1, BLOCK_COUNT + 1):
        block = yield context.call_activity(
            "mine_block",
            {"index": index, "previous_hash": previous_hash, "difficulty": difficulty},
        )
        blocks.append(block)
        previous_hash = block["hash"]

    lines = [f"# Mined {len(blocks)} blocks at difficulty {difficulty}", ""]
    for b in blocks:
        short = b["hash"][:16].lower()
        lines.append(
            f"- Block {b['index']}: nonce={b['nonce']}, "
            f"attempts={b['attempts']:,}, hash={short}\u2026"
        )
    return "\n".join(lines) + "\n"


@app.activity_trigger(input_name="block")
def mine_block(block: dict) -> dict:
    """Mines a single block.

    Scans nonces from 0 upward until the SHA-256 hash of ``"{index}:{previous_hash}:{nonce}"``
    has at least ``difficulty`` leading zero bits. Deterministic for a given input (so it is safe
    to replay), but how long it takes grows exponentially with difficulty.
    """
    index = block["index"]
    previous_hash = block["previous_hash"]
    difficulty = block["difficulty"]

    nonce = 0
    while True:
        digest = hashlib.sha256(
            f"{index}:{previous_hash}:{nonce}".encode("utf-8")
        ).digest()
        if _leading_zero_bits(digest) >= difficulty:
            return {
                "index": index,
                "nonce": nonce,
                "hash": digest.hex().upper(),
                "attempts": nonce + 1,
            }
        nonce += 1


def _leading_zero_bits(digest: bytes) -> int:
    bits = 0
    for byte in digest:
        if byte == 0:
            bits += 8
            continue
        mask = 0x80
        while mask > 0:
            if (byte & mask) == 0:
                bits += 1
            else:
                return bits
            mask >>= 1
    return bits


# ---------------------------------------------------------------------------
# MCP tools: budgeted single call + poll fallback.
# ---------------------------------------------------------------------------


@app.mcp_tool()
@app.mcp_tool_property(
    arg_name="difficulty",
    description=(
        "Optional mining difficulty (leading zero bits). Higher = longer. "
        "Omit to use the default."
    ),
    is_required=False,
)
@app.durable_client_input(client_name="client")
async def start_mining(client: df.DurableOrchestrationClient, difficulty: str = None) -> str:
    """Mines a short chain of proof-of-work blocks. Returns the result directly if it finishes \
quickly; otherwise returns a workflow_id to poll with get_mining_result. Higher difficulty takes \
longer."""
    # The argument is optional; when omitted (or not a positive int) fall back to the default.
    effective_difficulty = _parse_difficulty(difficulty)

    instance_id = await client.start_new(
        "run_orchestrator", client_input=effective_difficulty
    )
    logging.info(
        "Started mining orchestration %s at difficulty %s",
        instance_id,
        effective_difficulty,
    )

    # Budgeted wait: poll the orchestration until it reaches a terminal state OR the budget
    # expires. The orchestration keeps running in Durable storage regardless.
    deadline = time.monotonic() + WAIT_BUDGET_SECONDS
    while time.monotonic() < deadline:
        status = await client.get_status(instance_id)
        if status is not None and _is_terminal(status.runtime_status):
            # Finished within budget -> return the terminal result (completed or failed) inline.
            return _serialize(_to_result(status))
        await asyncio.sleep(1)

    # Budget expired. Hand back a poll handle plus explicit next-step guidance for the agent.
    logging.info(
        "Mining %s exceeded %ss budget; returning poll handle.",
        instance_id,
        WAIT_BUDGET_SECONDS,
    )
    return _serialize(
        {
            "status": "running",
            "workflow_id": instance_id,
            "poll_after_seconds": 5,
            "next": (
                f'Call get_mining_result with workflow_id "{instance_id}" '
                "in about 5 seconds."
            ),
        }
    )


@app.mcp_tool()
@app.mcp_tool_property(
    arg_name="workflow_id",
    description="The workflow_id returned by start_mining.",
    is_required=True,
)
@app.durable_client_input(client_name="client")
async def get_mining_result(
    client: df.DurableOrchestrationClient, workflow_id: str
) -> str:
    """Gets the status/result of a mining workflow started by start_mining. If status is \
'running', wait poll_after_seconds and call again."""
    status = await client.get_status(workflow_id)

    if status is None or status.runtime_status is None:
        # Distinct from "failed": the work didn't error, the handle is unknown (bad id, or the
        # instance history was purged after its retention window). The agent's right move is to
        # start a fresh workflow, not to keep polling -- so it gets its own status.
        return _serialize(
            {
                "status": "not_found",
                "workflow_id": workflow_id,
                "error": f'No workflow found with id "{workflow_id}".',
            }
        )

    return _serialize(_to_result(status))


# ---------------------------------------------------------------------------
# Result shaping: map a Durable runtime status to the closed
# { completed | failed | running | not_found } contract shared by both paths.
# ---------------------------------------------------------------------------


def _to_result(status: "df.models.DurableOrchestrationStatus.DurableOrchestrationStatus") -> dict:
    name = status.runtime_status.name if status.runtime_status is not None else None

    if name == "Completed":
        return {
            "status": "completed",
            "workflow_id": status.instance_id,
            "result": status.output,
        }

    if name in ("Failed", "Terminated", "Canceled"):
        return {
            "status": "failed",
            "workflow_id": status.instance_id,
            "reason": "error" if name == "Failed" else "terminated",
            "error": _describe_failure(status, name),
        }

    # Running / Pending / Suspended / ContinuedAsNew -> still in flight.
    return {
        "status": "running",
        "workflow_id": status.instance_id,
        "poll_after_seconds": 5,
        "next": (
            f'Call get_mining_result with workflow_id "{status.instance_id}" '
            "in about 5 seconds."
        ),
    }


def _describe_failure(status, name: str) -> str:
    detail = status.output
    if detail:
        return detail if isinstance(detail, str) else json.dumps(detail)
    return "Orchestration failed." if name == "Failed" else "Orchestration was terminated."


def _is_terminal(runtime_status) -> bool:
    return runtime_status is not None and runtime_status.name in (
        "Completed",
        "Failed",
        "Terminated",
        "Canceled",
    )


def _parse_difficulty(difficulty) -> int:
    try:
        value = int(difficulty)
    except (TypeError, ValueError):
        return DEFAULT_DIFFICULTY
    return value if value > 0 else DEFAULT_DIFFICULTY


def _serialize(result: dict) -> str:
    """Serialize the result, omitting null fields (matching the .NET WhenWritingNull policy)."""
    return json.dumps({k: v for k, v in result.items() if v is not None})
