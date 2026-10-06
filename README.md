# SpanProof

Checks that Sentry's AI monitoring reports what the LLM provider actually returned, and turns agent
traces into issues.

SpanProof replays scripted provider responses (built to each provider's published wire format) through
Sentry's real AI integrations (sentry-python and @sentry/node), across the provider versions in sentry-python's own `tox.ini`, and compares every emitted
span with the provider's numbers. A second layer reads span trees and raises issues for common agent
failures. Nothing is mocked inside the SDK or the client library: each scenario runs in a fresh interpreter
against a local scripted server that speaks the provider's wire format.

## What it checks

| check | question it answers |
|---|---|
| usage | Do `gen_ai.usage.*` input, output, cached, cache-write and reasoning tokens equal the provider's `usage`? |
| lifecycle | Does every provider call produce one finished span, also when a stream is closed early or errors? |
| structure | Are agent, LLM and tool spans nested in one trace, with no orphaned parents? |
| aggregation | Does summing tokens over a trace equal what was billed? Does an agent's usage equal the calls inside it? |
| conventions | Is every attribute in `getsentry/sentry-conventions`, non-deprecated, correctly typed, and consistent (cached ≤ input, total = input + output)? |
| privacy | With data collection off, is all prompt and completion content absent? |
| identity | Are the response model, response id, finish reason and tool calls recorded as the provider returned them? |
| errors | Are provider failures captured, and does the app still see the same exception it would without Sentry? |
| output | With output collection on, does the recorded answer text match each choice the model returned? |
| billing | Are billed quantities beyond tokens (server and built-in tool calls, cache-write TTL, audio tokens, service tier) recorded, and is there a convention attribute for them? |

Each scenario runs in four transport modes: default, data collection off, legacy transport
(`stream_gen_ai_spans=False`) and span streaming (`trace_lifecycle="stream"`).

Python coverage: OpenAI (chat, Responses, embeddings, including structured outputs, background mode,
built-in tools, audio and n>1 streams), Anthropic (including extended thinking, server tools, cache TTLs,
`messages.parse` and `beta.messages`), LiteLLM, Google GenAI, Cohere, Mistral, Hugging Face Hub, OpenAI Agents,
Pydantic AI, LangChain and LangGraph, plus MCP servers (below). The tests parse each fixture builder's output
with the provider SDK's own types (`tests/test_fixtures.py`, `tests/test_fixtures_strict.py`), so a builder that
drifts from the real schema fails.

## Agent failure classes

`tool_loop`, `retry_storm`, `silent_tool_error`, `lost_llm_span`, `dead_end`, `truncated_answer`,
`empty_answer`, `cost_spike` (a token-count spike against the agent's own history; prices are not
applied). The detectors read only what Sentry already receives (gen_ai and http.client spans) and emit
events with stable fingerprints, so repeats group into one issue. They also read OTLP/JSON (tested with the
attribute names Sentry's own SDKs emit; other GenAI exporters are untested).

They are measured on a labelled corpus produced by running openai-agents, LangGraph and Pydantic AI with
Sentry's real instrumentation against a scripted model (`spanproof.corpus`), including hard negatives
(pagination, one retry, a tool output that merely mentions "error", a 2x larger run). Precision and recall
are reported with 95% Wilson intervals (`spanproof.evaluate`).

## Use

```bash
uv venv -p 3.12 && uv pip install -e . -e path/to/sentry-python ".[providers]"
python -m spanproof.runner --modes default,nodc,legacy,stream --out results/run.json
python -m spanproof.matrix --sdk path/to/sentry-python --envs ~/.spanproof-envs --out results/matrix.json
(cd js && npm install) && python -m spanproof.js_bridge     # js/package.json lists the JS packages
python -m spanproof.corpus --n 8 --out results/corpus.jsonl && python -m spanproof.evaluate results/corpus.jsonl
python -m spanproof.issues results/corpus.jsonl --dry-run
python -m spanproof.report            # writes report/index.html
```

Against a real Sentry account: put `SENTRY_DSN_PY`, `SENTRY_DSN_JS`, `SENTRY_ORG`, `SENTRY_REGION_URL` and a
read-only `SENTRY_AUTH_TOKEN` in `~/.spanproof.env` (never in the repo), run with `SPANPROOF_DSN=<dsn>` set so
every envelope is recorded locally and also sent, then `python -m spanproof.real_compare results/run.json`
compares what was sent, what Sentry stored and the truth, and `python -m spanproof.from_sentry` re-scores the
detectors on traces read back from Sentry.

Against a live model: `SPANPROOF_LIVE=1` forwards every request to an OpenAI-compatible provider instead of
the script, and the provider's own response becomes the truth. Set `SPANPROOF_UPSTREAM` (e.g.
`https://api.groq.com/openai`), `SPANPROOF_UPSTREAM_KEY`, `SPANPROOF_UPSTREAM_MODEL`, optionally
`SPANPROOF_CASSETTES=<dir>` to save every raw response and `SPANPROOF_PACE=<seconds>` for rate limits, then run
`python -m spanproof.runner --jobs 1 --only openai.chat.,litellm.completion.openai,langchain.`. Select only
OpenAI-compatible scenarios: in live mode every request goes upstream, so Anthropic and Google scenarios would
fail. `python -m spanproof.live_agents` runs real
openai-agents, LangGraph and Pydantic AI agents the same way, with faults injected into their tools and provider.

CI: `spanproof.gate` compares a run with a checked-in baseline of known findings, fails only on new ones,
and lists the known ones that disappeared. `ci/spanproof.yml` is an example nightly + pull-request workflow.

## MCP servers

`python -m spanproof.mcp_run --py <python> ...` checks Sentry's MCP integration against what an MCP server
really handled. One server (tools, prompts, resources; text, image, embedded-resource and structured
results; a tool that raises, one that returns `isError`, concurrent calls) runs as the low-level `Server`,
the SDK's high-level server (`FastMCP` in mcp 1.x, `MCPServer` in 2.x) and standalone `fastmcp`, over
in-memory streams, a real stdio subprocess, Streamable HTTP (stateful and stateless) and SSE, in seven
modes (default, `send_default_pii=False`, `include_prompts=False`, `data_collection` inputs and outputs
off, outputs off, legacy transport, span streaming). Pass one `--py` per installed mcp / fastmcp version.
The truth is what each handler received and returned, what the client got back, and every JSON-RPC
message as it reached the HTTP server (`spanproof/mcp_sc.py`); `spanproof/mcp_checks.py` holds the checks
and re-scores saved runs (`python -m spanproof.mcp_checks results/mcp_*.json.gz`). Attribute names, types and
deprecations come from a pinned `mcp.*` snapshot of sentry-conventions (`conventions_snapshot/mcp.json`).

## SpanProof Watch

`python -m spanproof.watch --interval 300` watches a Sentry organization and files agent failures back into
Sentry as issues: tool loops, retry storms, silent tool errors, LLM calls missing from the trace, dead ends,
truncated and empty answers, and per-agent token spikes. Each detection becomes an event with a stable
fingerprint (failure class, agent, tool), tags for filtering and a link to the trace, so repeats group into one
issue. It needs only a read-only auth token plus a DSN to file into, keeps local state so each failure is
filed once, and builds each agent's token baseline in time order. `--dry-run` shows what it would file and saves
no state; with `--once` it looks twice, 60 seconds apart, since a trace is only judged once it stops changing.

A trace is judged when its top-level agent span has arrived (it ends last, so it is sent last), no span is
waiting for its parent, and for 60 seconds no new span has started and the span list has not changed (300
seconds for traces without an agent span). After each judgment that saw new spans, the trace is read again
five minutes later for late tool and provider-call spans. Failed deliveries are retried, and each failure gets
a stable event id, so a retry is dropped by Sentry instead of counted twice (checked on a real project).

`python -m spanproof.watch_replay` replays 301 saved traces into Watch the way Sentry delivers them: late, out
of order, found through the same one-hour, 100-trace search, polled every 5 minutes, with 10% of deliveries
failing and the process dying mid-cycle in 2% of cycles. Over 10 arrival orders, out of 1,945 failures: 0 false issues,
1 never filed, 0 stored twice, none filed before the trace's top-level agent span arrived; the median correct
issue is filed 444 s after the trace's last span arrived. Judging a trace when it first appears filed 179 false issues and
stored 1,347 duplicates on the same arrivals (`results/watch_replay.json`).

Checked on 94 traces with known outcomes, read back from a real Sentry account (58 synthetic, plus 36 real
agent runs on a live model with injected faults): no false alarms on 35 problem-free traces, every tool loop,
retry storm, dead end, token spike, lost LLM call, silent tool error and empty answer caught; truncation is missed
where the integration does not record finish reasons (openai-agents, Pydantic AI before the fix in `patches/`).

## Layout

```
spanproof/fixtures.py     provider wire responses + ground truth (tests parse them with each provider SDK's types)
spanproof/mockserver.py   scripted JSON/SSE server (can cut a stream mid-way)
spanproof/scenarios/      one module per integration
spanproof/worker.py       runs one scenario in a fresh interpreter
spanproof/oracles.py      the eight checks
spanproof/matrix.py       version matrix derived from sentry-python's tox.ini
spanproof/js_bridge.py    the same fixtures through @sentry/node (js/worker.cjs)
spanproof/detectors.py    agent failure classes;  issues.py: Sentry events, OTLP and Sentry span input
spanproof/corpus.py, agent_worker.py, evaluate.py, fingerprint.py   detector measurement
spanproof/watch.py        SpanProof Watch: agent failures in a Sentry org filed back as Sentry issues
spanproof/watch_replay.py replays saved traces into Watch with late spans, failed deliveries and crashes
spanproof/live.py, live_agents.py, live_agent_worker.py   live provider truth (recorded responses) and real agents
spanproof/gate.py         CI baseline gate;  report.py: findings page;  catalog.py: curated findings
spanproof/mcp_sc.py       MCP server in three flavors + client operations, recording handler/client/wire truth
spanproof/mcp_worker.py   one MCP (flavor, transport, mode) run;  mcp_run.py: the matrix;  mcp_checks.py: the checks
tests/                    unit tests, each check with a positive case and a negative control
```

## Limits

- In the default runs, ground truth is built from each provider's published schema and parsed with the
  provider SDK's types; no API keys are needed. Live runs (above) use the provider's real responses; the ones
  in `results/` were made against Groq (`results/cassettes/groq/`).
- The detector corpus is synthetic. Results on production traces are the next step.
- JavaScript coverage is @sentry/node with OpenAI, Anthropic, Vercel AI, LangChain (OpenAI, Anthropic and Google chat
  models), LangGraph (`createReactAgent`, `langchain`'s `createAgent`) and Google GenAI (models and chats).
