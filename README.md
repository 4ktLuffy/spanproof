# SpanProof

I wanted to know if Sentry's AI monitoring tells the truth. So I built a tool that sends the same LLM response through Sentry and compares what Sentry records with what the provider actually returned. Then I built a second one that reads agent traces and opens Sentry issues when an agent goes wrong.

It runs every night against the latest Sentry SDKs: **https://4ktluffy.github.io/spanproof/**

## What it found

Some of what turned up:

- **JS + Anthropic prompt caching:** Sentry shows 40 input tokens for a 2,600-token call, so cost comes out 57% low. ([sentry-javascript#25069](https://github.com/getsentry/sentry-javascript/issues/25069))
- **Hugging Face and Cohere streams:** if the provider fails mid-stream, Sentry swallows the error and the app gets a cut-off answer with no exception. ([#7889](https://github.com/getsentry/sentry-python/issues/7889))
- **MCP:** a tool call that fails is recorded as a success, so MCP error rates read 0%. ([#7890](https://github.com/getsentry/sentry-python/issues/7890))
- **Structured outputs:** `chat.completions.parse` and Anthropic `messages.parse` produce no span at all. ([#7891](https://github.com/getsentry/sentry-python/issues/7891))
- **LiteLLM** drops cached and reasoning tokens. My fix is up as [#7880](https://github.com/getsentry/sentry-python/pull/7880), tested on every LiteLLM version Sentry supports.

The full list (48 findings, each with the line of code that causes it and how to reproduce it) is in [`spanproof/catalog.py`](spanproof/catalog.py) and on the [report page](report/index.html). Other issues I filed: [#7870](https://github.com/getsentry/sentry-python/issues/7870), [#7871](https://github.com/getsentry/sentry-python/issues/7871), [#7872](https://github.com/getsentry/sentry-python/issues/7872), [#7873](https://github.com/getsentry/sentry-python/issues/7873), [#7876](https://github.com/getsentry/sentry-python/issues/7876).

## How it works

Each test sends a provider response through Sentry's real integrations, in a fresh Python or Node process, with nothing mocked inside Sentry or the provider's SDK. The responses follow each provider's published format, and the tests check them against the provider SDK's own types so they can't drift from reality. Then it compares every span Sentry produced with the provider's numbers: tokens (including cached, cache-write and reasoning), model, response id, finish reason, one span per call, privacy when data collection is off, and whether the app still gets the same errors it would without Sentry.

It covers OpenAI, Anthropic, LiteLLM, Google GenAI, Cohere, Mistral, Hugging Face, OpenAI Agents, Pydantic AI, LangChain, LangGraph and MCP in Python, plus OpenAI, Anthropic, Vercel AI, LangChain, LangGraph and Google GenAI in JavaScript. It runs on every provider version in Sentry's own test setup, and on the latest versions every scenario runs in all four of Sentry's transport modes.

I also ran it against a real Sentry account (what Sentry stored matched what was sent, and its cost math was right) and against a live model on Groq, using real agents with injected faults.

## SpanProof Watch

Watch reads agent traces from a Sentry org and files failures back as Sentry issues: tool loops, retry storms, silent tool errors, dead ends, cut-off and empty answers, LLM calls missing from the trace, and token spikes. Repeats of the same failure group into one issue, with a link to the trace.

The hard part is timing. Spans arrive late and out of order, so Watch waits until a trace has really finished, reads it again later for stragglers, and gives every failure a fixed id so a retry never files twice. I tested this by replaying 301 saved traces the way Sentry delivers them, with failed deliveries and crashes thrown in: out of 1,945 failures it filed 0 false issues and 0 duplicates, and missed 1. On traces read back from a real Sentry account, including real agents on a live model, it raised no false alarms on 35 healthy traces.

## Built on top of Sentry's own code

- **[A provider-truth check for Sentry's internal test harness](https://github.com/4ktLuffy/testing-ai-sdk-integrations/tree/provider-truth):** records what the provider really returned and flags spans that disagree. Their current check passes any positive token count.
- **[An agent-failure matrix](https://github.com/4ktLuffy/testing-ai-sdk-integrations/tree/failure-detectability):** injects tool loops, retry storms, dead ends and cut-off answers into real agents and shows which ones Sentry's data can detect, per integration. For example, a cut-off answer is invisible in OpenAI Agents and Pydantic AI because no finish reason is recorded.
- **[A tool-loop issue detector inside Sentry](https://github.com/4ktLuffy/sentry/tree/ai-agent-tool-loop-detector):** written the way Sentry's own performance detectors are. On 45 real agent runs it caught all 8 loops with no false alarms.

## Run it

```bash
uv venv -p 3.12 && uv pip install -e . -e path/to/sentry-python ".[providers]"
python -m spanproof.runner --modes default,nodc,legacy,stream     # all scenarios on the latest versions
python -m spanproof.matrix --sdk path/to/sentry-python --envs ~/.spanproof-envs   # every version in Sentry's tox.ini
(cd js && npm install) && python -m spanproof.js_bridge           # the JavaScript side
python -m spanproof.report                                        # writes report/index.html
```

More:

- **Real Sentry account:** put your DSNs, org and a read-only auth token in `~/.spanproof.env`, run with `SPANPROOF_DSN=<dsn>`, then `python -m spanproof.real_compare results/run.json` compares what was sent, what Sentry stored and the truth.
- **Live model:** `SPANPROOF_LIVE=1` with `SPANPROOF_UPSTREAM`, `SPANPROOF_UPSTREAM_KEY` and `SPANPROOF_UPSTREAM_MODEL` sends requests to any OpenAI-compatible provider, and its real response becomes the truth. `python -m spanproof.live_agents` does the same with real agents.
- **MCP:** `python -m spanproof.mcp_run --py <python>` runs an MCP server over every transport and checks Sentry's spans against what the server really handled.
- **Watch:** `python -m spanproof.watch --once --dry-run` shows what it would file without filing anything. `python -m spanproof.watch_replay` reruns the timing test.
- **CI:** `spanproof.gate` fails only on findings that aren't in the checked-in baseline. `ci/spanproof.yml` is an example workflow.

## Limits

- The default runs use scripted provider responses, so no API keys are needed. The live runs in `results/` were made against Groq.
- The detector test set is mostly synthetic, plus 45 real agent runs on one model. Production traces would be the real test.
- JavaScript coverage is @sentry/node only.

## Layout

```
spanproof/fixtures.py     provider responses and the truth they imply
spanproof/scenarios/      one module per integration
spanproof/oracles.py      the checks
spanproof/matrix.py       versions from sentry-python's tox.ini
spanproof/js_bridge.py    the JavaScript side (js/worker.cjs)
spanproof/detectors.py    agent failure classes
spanproof/watch.py        SpanProof Watch
spanproof/mcp_*.py        MCP checks
spanproof/catalog.py      every finding, with evidence
tests/                    each check has a positive case and a negative control
```
