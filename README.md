# SpanProof

Checks that Sentry's AI monitoring reports what the LLM provider actually returned, and turns agent
traces into issues.

SpanProof replays recorded provider responses through Sentry's real AI integrations (sentry-python and
@sentry/node), across the provider versions in sentry-python's own `tox.ini`, and compares every emitted
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
| identity | Are the response model, response id and finish reason recorded? |
| errors | Are provider failures captured, and does the client behave as the scenario expects? |

Each scenario runs in four transport modes: default, data collection off, legacy transport
(`stream_gen_ai_spans=False`) and span streaming (`trace_lifecycle="stream"`).

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
(cd js && npm install @sentry/node openai @anthropic-ai/sdk ai @ai-sdk/openai) && python -m spanproof.js_bridge
python -m spanproof.corpus --n 8 --out results/corpus.jsonl && python -m spanproof.evaluate results/corpus.jsonl
python -m spanproof.issues results/corpus.jsonl --dry-run
python -m spanproof.report            # writes report/index.html
```

Against a real Sentry account: put `SENTRY_DSN_PY`, `SENTRY_DSN_JS`, `SENTRY_ORG`, `SENTRY_REGION_URL` and a
read-only `SENTRY_AUTH_TOKEN` in `~/.spanproof.env` (never in the repo), run with `SPANPROOF_DSN=<dsn>` set so
every envelope is recorded locally and also sent, then `python -m spanproof.real_compare results/run.json`
compares what was sent, what Sentry stored and the truth, and `python -m spanproof.from_sentry` re-scores the
detectors on traces read back from Sentry.

CI: `spanproof.gate` compares a run with a checked-in baseline of known findings, fails only on new ones,
and lists the known ones that disappeared. `ci/spanproof.yml` is an example nightly + pull-request workflow.

## SpanProof Watch

`python -m spanproof.watch --interval 300` watches a Sentry organization and files agent failures back into
Sentry as issues: tool loops, retry storms, silent tool errors, LLM calls missing from the trace, dead ends,
truncated and empty answers, and per-agent token spikes. Each detection becomes an event with a stable
fingerprint (failure class, agent, tool), tags for filtering and a link to the trace, so repeats group into one
issue. It needs only a read-only auth token plus a DSN to file into. It waits until a trace has been quiet
for `--settle` seconds (default 300) before judging it, keeps local state so a trace is filed once, retries a
trace whose events could not be handed off, and builds each agent's token baseline in time order. `--dry-run`
shows what it would file and saves no state.

Checked on 94 traces with known outcomes, read back from a real Sentry account (58 synthetic, plus 36 real
agent runs on a live model with injected faults): no false alarms on 35 problem-free traces, every tool loop,
retry storm, dead end, token spike, lost LLM call, silent tool error and empty answer caught; truncation is missed
where the integration does not record finish reasons (openai-agents, Pydantic AI before the fix in `patches/`).

## Layout

```
spanproof/fixtures.py     provider wire responses + ground truth, validated with each provider SDK's own types
spanproof/mockserver.py   scripted JSON/SSE server (can cut a stream mid-way)
spanproof/scenarios/      one module per integration
spanproof/worker.py       runs one scenario in a fresh interpreter
spanproof/oracles.py      the eight checks
spanproof/matrix.py       version matrix derived from sentry-python's tox.ini
spanproof/js_bridge.py    the same fixtures through @sentry/node (js/worker.cjs)
spanproof/detectors.py    agent failure classes;  issues.py: Sentry events, OTLP and Sentry span input
spanproof/corpus.py, agent_worker.py, evaluate.py, fingerprint.py   detector measurement
spanproof/watch.py        SpanProof Watch: agent failures in a Sentry org filed back as Sentry issues
spanproof/live.py, live_agents.py, live_agent_worker.py   live provider truth (record/replay) and real agents
spanproof/gate.py         CI baseline gate;  report.py: findings page;  catalog.py: curated findings
tests/                    unit tests, each check with a positive case and a negative control
```

## Limits

- Ground truth is built from each provider's published schema and parsed with the provider SDK's types; it
  is not a recording of live calls (no API keys are used).
- The detector corpus is synthetic. Results on production traces are the next step.
- JavaScript coverage is @sentry/node with OpenAI, Anthropic and Vercel AI.
