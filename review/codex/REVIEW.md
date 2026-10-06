# Independent reproduction review

All 15 listed claims were exercised with fresh scripts against the installed SDKs. **The stated observable behaviors reproduced**, including SP-16's claim that the trace-splitting issue does **not** reproduce. Several interpretations need qualification, especially SP-06, SP-13, and SP-15.

No `spanproof` package was read or imported. Provider traffic uses a loopback HTTP server returning JSON or SSE. No real API keys or external provider services are used; `local-placeholder` only satisfies client constructors. Sentry envelopes are captured in memory, with sampling at 100%. Python explicitly uses `stream_gen_ai_spans=False` and captures transactions; JavaScript captures the default streamed `span` envelopes, including their `items` arrays. Assertions inspect delivered transport output, not merely active spans.

Versions verified: Python sentry-sdk 2.71.0, openai 2.54.0, anthropic 1.11.0, litellm 1.104.0, openai-agents 0.20.0, langchain 1.4.3, langchain-openai 1.6.7, langgraph 1.2.13, pydantic-ai-slim 2.31.1; Node v25.8.0, @sentry/node 11.4.0, openai 7.28.0, @anthropic-ai/sdk 0.131.0, ai 7.0.128, @ai-sdk/openai 4.0.84. The JavaScript runs use `--import @sentry/node/import`, required to instrument these OpenAI/Anthropic versions. Explicit integrations and disabled defaults prevent accidental overlapping instrumentation.

In the table, token fields abbreviate the `gen_ai.usage.` prefix. **Absent** means the attribute key is missing, not zero.

| Claim | Verdict | Exact observation | Reproduction | Qualification / control |
| --- | --- | --- | --- | --- |
| SP-01 | CONFIRMED | One LiteLLM chat span: `input_tokens=1200`, `output_tokens=300`, `total_tokens=1500`; cached and reasoning attributes absent despite response details of 1024 and 256. | [repro_SP-01.py](repro_SP-01.py) | Only LiteLLM integration enabled. Ordinary OpenAI integration records both details with the same response fixture (SP-10). |
| SP-02 | CONFIRMED | Chat and Responses streams each read two chunks, break, and call `.close()`; **0 delivered gen_ai spans** across the two calls. | [repro_SP-02.py](repro_SP-02.py) | `FULL=1` consumes both to exhaustion and delivers **2** spans: `gen_ai.chat` and `gen_ai.responses`. Early exit may lack final usage, but here the entire span is absent. |
| SP-03 | CONFIRMED | LiteLLM stream read for two chunks then abandoned: **0 delivered gen_ai spans**, including after a two-second callback grace period. | [repro_SP-03.py](repro_SP-03.py) | `FULL=1` delivers **1** LiteLLM chat span. Enabling OpenAI integration too can produce an additional underlying span; that is not evidence that the LiteLLM span finished. |
| SP-04 | CONFIRMED | Python and JS parsed usage retains `input_tokens_details.cache_write_tokens=96`; neither emitted span has a cache-write attribute. Both record input/output/total **1200/300/1500**. Python also records cached **1024** and reasoning **256**; JS records neither. | [repro_SP-04.py](repro_SP-04.py), [repro_SP-04.cjs](repro_SP-04.cjs) | The installed Python OpenAI `InputTokensDetails` declares `cache_write_tokens`; this is not just a misspelled extra JSON field. Both scripts print parsed usage. |
| SP-05 | CONFIRMED | `.messages.with_raw_response.create(...).parse()` delivers a chat span with **no `gen_ai.usage.*` keys**. | [repro_SP-05.py](repro_SP-05.py) | Parsed response has input **40**, cached **2048**, cache creation **512**, output **120**. Ordinary `.messages.create()` against the identical response records input **2600**, cached **2048**, cache_write **512**, output **120**, total **2720**. |
| SP-06 | CONFIRMED | One tool invocation, two chat spans: input **100** and **200**; invoke_agent input **300**. Sum across gen_ai spans = **600**, versus provider input **300**. Output similarly: **300+300+600=1200**, versus provider output **600**. | [repro_SP-06.py](repro_SP-06.py) | This is aggregation across parent and child spans, not proof of actual double billing. The agent aggregate itself is correct; a naive sum of all span levels double counts. OpenAI integration is disabled, ruling out overlapping OpenAI instrumentation. |
| SP-07 | CONFIRMED | Outer first chat input **100**, inner chat **200**, outer final chat **300**. Inner invoke_agent input **300**, although its own chat totals **200**. Outer invoke_agent input **600**. Inner output **600**, versus its own chat output **300**. | [repro_SP-07.py](repro_SP-07.py) | Inner aggregate includes the preceding outer call. The trace nesting is correct, so the usage anomaly is separate from trace splitting. |
| SP-08 | CONFIRMED | `.invoke()` delivers **1 invoke_agent + 1 chat** span; fully exhausted `.stream()` delivers **0 invoke_agent + 1 chat** span. | [repro_SP-08.py](repro_SP-08.py) | Same graph, model, and HTTP fixture for both. LangGraph and LangChain integrations explicitly enabled. The installed LangGraph warns that `create_react_agent` is deprecated; the test nevertheless uses precisely the claimed API. |
| SP-09 | CONFIRMED | Exhausted `Agent.iter()` delivers **2 chat**, **0 invoke_agent**, **0 execute_tool** spans for a run that calls `ping`. | [repro_SP-09.py](repro_SP-09.py) | `Agent.run()` on the same agent produces **1 invoke_agent, 1 execute_tool, 2 chat** spans. Successful second model call follows the tool response. No partial-consumption explanation applies here. |
| SP-10 | CONFIRMED | Python OpenAI chat response has `finish_reason="stop"`; span lacks `gen_ai.response.finish_reasons`. Anthropic span records **`["end_turn"]`**. | [repro_SP-10.py](repro_SP-10.py) | This is scoped to Python. The JS OpenAI control records a serialized `["stop"]` finish-reasons value. |
| SP-11 | CONFIRMED | JS OpenAI chat span records input/output/total **1200/300/1500**, but no cached or reasoning attribute for supplied **1024/256**. | [repro_SP-11.cjs](repro_SP-11.cjs) | Runtime instrumentation hook enabled; presence of the chat span and other response fields rules out a wholly uninstrumented client. |
| SP-12 | CONFIRMED | JS Anthropic span: `input_tokens=40`, `output_tokens=120`, `total_tokens=2720`; cache-read/cache-creation attributes absent. | [repro_SP-12.cjs](repro_SP-12.cjs) | Total includes **40+2048+512+120**, while recorded input excludes both cache components. Python's ordinary Anthropic control records input **2600** and both cache fields. |
| SP-13 | CONFIRMED | Breaking `streamText(...).textStream` after two text parts delivers **1 gen_ai.generate_content child + 1 manual root**. The child's `parent_span_id` matches neither delivered span; **0 invoke_agent parent spans** delivered. | [repro_SP-13.cjs](repro_SP-13.cjs) | The fake provider completes; child has full **1200/300/1500** usage. Checked after a 500 ms grace period, another 1 s wait, flush, and SDK close. `FULL=1` delivers **3 spans**, including invoke_agent, with every parent present. “Never” is bounded to this completed test/flush/close lifecycle, not an infinite observation. |
| SP-15 | CONFIRMED | Requested `gpt-5-mini`; every HTTP SSE chunk identifies `gpt-5-mini-2026-08-07`; delivered LiteLLM span's `gen_ai.response.model` is **`gpt-5-mini`**. | [repro_SP-15.py](repro_SP-15.py) | **Attribution caveat:** all **5** chunks exposed by LiteLLM already report `gpt-5-mini`. `NO_SENTRY=1` reproduces that normalization too. This confirms the span value but does not establish that Sentry itself discarded an available provider model name. |
| SP-16 | CONFIRMED | Nested `Agent.as_tool()` run: **1 trace ID**, **8 child spans + 1 transaction root**; every child parent ID resolves. Outer/inner workflow, invoke_agent, tool, and chat nesting is correct. | [repro_SP-16.py](repro_SP-16.py) | CONFIRMED refers to the claim of **non-reproduction**. Outer workflow → outer invoke_agent → inner_tool → inner workflow → inner invoke_agent → inner chat; outer chats parent to outer invoke_agent. This is evidence for this tested path, not proof that every configuration avoids issue #4786. |

Run everything from this directory with:

```sh
sh run_all.sh
```

For an individual case:

```sh
python repro_SP-07.py
node --import @sentry/node/import repro_SP-12.cjs
```

The short per-claim entry points share independently written `fixture.py`, `repro_python.py`, `repro_agents.py`, and `repro_js.cjs`. `result_*.json` and `control_*.json` retain stdout containing labeled JSON blocks and, in some LiteLLM runs, diagnostic text; they are not single JSON documents. `err_*.txt` retain stderr. `verify.py` parses captured output and asserts every table's central finding plus the full-consumption, no-Sentry model, and trace-parent controls. `verification.txt` records the final assertion result.

Two setup traps were corrected before the final run: simultaneous LiteLLM/OpenAI integrations produced extra lower-level spans, and ending the transaction immediately after exhausting LiteLLM could race its background success callback. The final LiteLLM tests isolate its integration and wait two seconds inside the transaction. The nested-agent fixture uses zero cached tokens, avoiding impossible cached counts greater than its deliberately small input totals.

Final-run identity evidence: SP-13 child `bc378887a68025df` names missing parent `98b3faa6e5e8ab5b`; the delivered manual root is `b3c773f792ae7179`. SP-16 uses trace `6a84ca9a608445c1a1309905f3b1907b` throughout. IDs are regenerated on rerun.

Validation: `sh run_all.sh` completed successfully; `verify.py` reported **PASS for all 15 claims and controls**.
