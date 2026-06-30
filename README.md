<!--
---
name: Long-running MCP tools using Durable Functions (Python)
description: Run long-running MCP tools on Azure Functions by backing them with Durable Functions and a budgeted start/poll pattern, written in Python.
page_type: sample
languages:
- python
products:
- azure-functions
- azure
urlFragment: mcp-functions-long-running-tools-python
---
-->

# Long-running MCP tools using Durable Functions (Python)

A sample [Azure Functions](https://learn.microsoft.com/azure/azure-functions/) MCP server (Python)
that shows how to run **long-running MCP tools** (tool calls that take longer than an MCP client is
willing to wait) by backing them with [Durable Functions](https://learn.microsoft.com/azure/azure-functions/durable/durable-functions-overview)
and a **budgeted start + poll** pattern. Durable Functions lets you write stateful, long-running workflows as ordinary code, orchestrating multiple function calls while the platform handles checkpointing, scaling, and recovery.

> The MCP **Tasks extension**
> ([SEP-2663](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2663)) was introduced
> in the [`2026-07-28` release candidate](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/)
> for building long-running tools. However, until it's broadly supported in the ecosystem, we need a
> solution today — and **Durable Functions** is a good fit here.

## The problem

An MCP `tools/call` is request/response. If a tool kicks off work that takes minutes, the **client's
request timeout** fires long before the work finishes, and the agent sees a failed tool call, even
though the work may still be running. Client tool-call timeouts are **not standardized** by the MCP
spec; in practice they're often in the ~30–60s range and vary per client. So a single tool call must
not block for the full duration of a long workflow.

## The approach: budgeted single call + poll fallback

Two MCP tools are exposed:

1. **`start_mining`** - starts a Durable orchestration (which mines a short chain of proof-of-work blocks), then awaits completion up to a short budget (~20s, configurable).
   - If the workflow finishes within budget, the result is returned inline. The second tool is never needed. This is the common case and it removes any "did the agent remember to poll?" risk.
   - If the budget expires, a handle (`workflow_id`) is returned plus an explicit instruction to poll. The orchestration keeps running in Durable storage regardless of the client connection.

2. **`get_mining_result`** - takes the `workflow_id` (a _required_ parameter) and returns the current state: `completed` (with result), `failed` (with error), `running` (poll again), or `not_found` (unknown).

Ordering is made robust by design: `workflow_id` is a _required_ parameter of the poll tool (so the agent can't poll without first starting), the "running" response carries `poll_after_seconds` and a `next` instruction to guide the agent, and the budgeted wait means fast workflows never hit the second tool at all.

> **Known weakness.** Even so, the poll path still relies on the LLM correctly remembering, and
> not hallucinating, the `workflow_id` it was handed. If the model garbles or invents an id, the
> poll lands on the wrong instance or none at all (which is why `get_mining_result` returns
> `not_found` rather than guessing). The budgeted wait mitigates this by resolving calls that don't need
> a second hop, but it's the core reason the MCP Task extension, where the SDK (not the model)
> carries the handle, is the better long-term answer.

## The example workflow: a blockchain-style miner

The long-running work in this sample is a small **miner**, the same idea behind blockchain
_proof-of-work_, but with nothing crypto to learn. Here's the mechanic in plain terms: to "mine" a
block, the system runs an input through a one-way math function (SHA-256) and checks whether the
result matches a required pattern, i.e. starting with at least `difficulty` zeros. There's no
shortcut, so the miner just keeps trying different inputs (`0, 1, 2, …`) until one happens to produce
a result that fits. Lots of trial and error, which naturally takes time — a good stand-in for any
real long-running job.

The miner builds a short _chain_ of blocks, where each block's input includes the previous block's
answer. Because every step depends on the one before it, this is a natural example of Durable's
[**function-chaining pattern**](https://learn.microsoft.com/azure/durable-task/common/durable-task-sequence?tabs=python&pivots=durable-functions).

The `difficulty` knob controls the runtime: each extra required zero roughly doubles the expected
number of attempts, so higher difficulty takes longer. That single knob is what lets the sample
demonstrate both the quick _inline_ path and the slow _poll_ path.

## Prerequisites

- [Python 3.13](https://www.python.org/downloads/)
- [Azure Functions Core Tools v4](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
  for deploying to Azure.
- [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite) storage emulator
- [VS Code](https://code.visualstudio.com/) with [GitHub Copilot](https://code.visualstudio.com/docs/copilot/overview) (agent mode) to call the tools as an MCP client.

## Run it locally

**1. Start Azurite** (in its own terminal):

```bash
azurite --skipApiVersionCheck --silent --location ./.azurite
```

**2. Create a virtual environment and install dependencies** (from the `src` folder):

```bash
cd src
python -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**3. Start the Functions host** (from the `src` folder):

```bash
func start
```

> In **VS Code**, you can instead press **F5** to start the host (it creates the virtual
> environment, installs `requirements.txt`, and launches `func start` for you).

You should see both MCP tools register and the endpoint print:

```
MCP server endpoint: http://localhost:7071/runtime/webhooks/mcp

Functions:
    start_mining: mcpToolTrigger
    get_mining_result: mcpToolTrigger
    run_orchestrator: orchestrationTrigger
    mine_block: activityTrigger
    ...
```

> The MCP endpoint uses the Streamable HTTP transport at
> `http://localhost:7071/runtime/webhooks/mcp`.

**4. Connect from VS Code.** [`.vscode/mcp.json`](.vscode/mcp.json) registers the local server.
Open the repo in VS Code, open `.vscode/mcp.json`, and click **Start** on the `local-mining-mcp`
server. Then, in a Copilot **agent mode** chat, ask it to mine, e.g.:

> Mine some blocks for me.

The agent calls `start_mining`. With the default difficulty, the work outlives the wait budget, so the
tool returns status `running` with a `workflow_id` and a `next` instruction. The agent then calls
`get_mining_result` with that id, returning `running` until mining completes, then `completed` with the
mined chain. This exercises the **poll path** out of the box (so it's demonstrated even if you deploy
without testing locally first).

### Try the inline path

Ask the agent to mine at a lower difficulty so the work finishes within the wait budget:

> Mine some blocks at difficulty 18.

Now `start_mining` finishes before the budget expires and the result comes back **inline** (status
`completed`), with no polling needed.

> Mining time varies by machine. If difficulty 18
> still outlasts the budget, lower it by 1–2; the default difficulty is tuned to reliably trigger
> polling.

## Deploy to Azure

The repo includes [`azure.yaml`](azure.yaml) and Bicep under [`infra/`](infra), so it deploys with the
[Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/). Run this from
the **repository root** (the folder containing `azure.yaml`):

```bash
azd up
```

`azd` prompts for an environment name, subscription, and region, as well as the **mining difficulty**
(recommended value: 24) and **wait budget (seconds)** (recommended value: 20) for the deployed app.

Durable Functions requires a storage backend to checkpoint execution progress. For local
testing, the app uses Azure Storage. When deployed to Azure, it uses the [Durable Task Scheduler (DTS)](https://learn.microsoft.com/azure/durable-task/scheduler/durable-task-scheduler), which is the recommended backend.

During packaging, `azd` hooks automatically swap in the DTS [`src/host.dts.json`](src/host.dts.json)
so the deployed app uses the Durable Task Scheduler backend, then restore the local Azure Storage
`host.json`. You don't edit any files to deploy.

When it finishes, the MCP endpoint is at
`https://<function-app>.azurewebsites.net/runtime/webhooks/mcp`. The endpoint is **key-protected**, so
clients must send the MCP extension's system key in the `x-functions-key` header. Retrieve it with:

```bash
az functionapp keys list -g <resource-group> -n <function-app> \
  --query "systemKeys.mcp_extension" -o tsv
```

Tear everything down with `azd down`.

### Enable built-in MCP authentication

By default the deployed endpoint is _key-protected_ (the `x-functions-key` header above). For a more
secure, standards-based setup, you can enable _built-in MCP authentication_ so clients connect using
an OAuth flow backed by Microsoft Entra ID instead of a shared access key. This lets the agent
authenticate as a user/identity rather than passing a static system key.

Follow the steps in
[Enable built-in MCP authentication in the Azure portal](https://learn.microsoft.com/azure/azure-functions/functions-mcp-tutorial?tabs=mcp-extension&pivots=programming-language-python#enable-built-in-mcp-authentication-in-azure-portal).

## Configuration

| Setting | Default | Purpose |
|---------|---------|---------|
| `WaitBudgetSeconds` | `20` | How long `start_mining` blocks waiting for the workflow before returning a poll handle. Keep it **under the client's tool-call timeout**, not the Functions timeout. |
| `MiningDifficulty` | `24` | Default mining difficulty (leading zero bits) when the tool's `difficulty` argument is omitted. Tuned so the default run outlasts `WaitBudgetSeconds` and exercises the poll path. Higher = longer. |

Both are read from app settings / environment. **Locally**, set them in
[`src/local.settings.json`](src/local.settings.json) (defaults `24` / `20`). **In Azure**, `azd up`
prompts for them and sets them as Function App settings (see [`infra/main.bicep`](infra/main.bicep)).
The `difficulty` tool argument overrides `MiningDifficulty` per request.

## How the result is shaped

`status` is a **behavioral** signal that tells the agent what to do next; descriptive detail lives in
sibling fields so nothing is lost:

| `status` | Meaning | Agent's next move |
|----------|---------|-------------------|
| `completed` | Done; `result` holds the mined chain. | Use the result. |
| `running` | Still in flight (budget expired). | Wait `poll_after_seconds`, call `get_mining_result`. |
| `failed` | Terminal: errored or terminated. `reason` + `error` give detail. | Stop polling; surface the error; optionally start over. |
| `not_found` | No workflow for that id (bad/expired id). | Don't poll; start a new workflow. |

`Failed` and `Terminated` Durable states both map to `status: "failed"` because they drive the
**same** agent action; the precise cause is preserved in `reason` (`error` / `terminated`).

## Durable backend: Azure Storage locally, DTS in Azure

This sample uses two Durable Functions backends so local development stays lightweight while Azure
gets a managed, scalable backend:

- Locally, it uses the Azure Storage backend, served by the [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite)
  emulator. This is the default in [`src/host.json`](src/host.json), so all you need locally is Azurite.
- In Azure, it uses the [Durable Task Scheduler (DTS)](https://learn.microsoft.com/azure/durable-task/scheduler/durable-task-scheduler),
  a managed backend that `azd up` provisions and connects via managed identity.

The DTS configuration lives in [`src/host.dts.json`](src/host.dts.json). You don't switch backends by
hand: `azd` package [hooks](azure.yaml) swap `host.dts.json` in for the deployment package and restore
the local `host.json` afterward, so your working tree always runs on Azure Storage. The orchestration
and MCP tool code are identical for both backends.

## Q&A

**Q: Why is the wait budget ~20s, and what bounds it?**
The client tool-call timeout, not the Functions host timeout. The host timeout on Flex/Premium plans is
generous (minutes), but the client may give up in ~30s, and that's non-standard and varies per
client. So default the budget conservatively (~20s, as an app setting) to stay under the most
aggressive clients, and rely on the poll fallback for anything longer. 

**Q: While a long-running task is in flight, can the agent (or other agents) still call other tools?**
It's the agent's turn that's tied up, not the server. Any synchronous `tools/call` blocks the
agent until it returns; that's true for every tool, long or short. The long-running case just (a) can
consume the whole per-call budget before returning, and (b) puts the agent into a poll loop
(wait `poll_after_seconds`, call `get_mining_result`, repeat), which in practice is a dedicated loop
that monopolizes the agent's attention, so it feels blocking end to end. (The agent isn't strictly
_forced_ to poll back to back; `poll_after_seconds` is a hint, and a capable client could interleave
other work between polls, but most agents serialize.) The server is never blocked: the budgeted
wait is async (it doesn't hold a thread), the orchestration runs in the background keyed by
`workflow_id`, and the Functions host serves requests concurrently and scales out. So a _different_
agent on its own session can call the server's tools at the same time, and many long-running
workflows can run in parallel. This is the limitation the native MCP Task extension removes: the SDK
polls out of band, freeing the model from the loop.

## Contributing

This project welcomes contributions and suggestions. See the standard
[Microsoft CLA / Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/) notes.

## License

[MIT](LICENSE)
