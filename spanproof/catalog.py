"""Curated findings: what SpanProof found in Sentry's AI monitoring, with evidence.

Every entry was reproduced by running the listed scenarios, and its root cause read
in source at the pinned versions below. Intent labels:
  unintended      a defect: the code does not do what its own design says
  simplification  a deliberate shortcut with a real cost worth knowing
  not a fault     expected behaviour; listed so nobody chases it
  needs decision  SDK and conventions disagree; a maintainer has to pick a side
Status: "new" means no matching issue or PR was found on 2026-10-06 (searches listed in
the report); otherwise the existing issue is linked.
"""

PY_SHA = "6a4eb20f1db3de7b025bda6eedb73b9073cfbc91"
PY = f"https://github.com/getsentry/sentry-python/blob/{PY_SHA}"
JS_VER = "@sentry/server-utils 11.4.0 (npm)"

FINDINGS = [
    {
        "id": "SP-01",
        "title": "LiteLLM drops cached, reasoning and cache-write tokens",
        "sdk": "python", "integration": "litellm", "severity": "high", "intent": "unintended",
        "status": "known", "issue": "https://github.com/getsentry/sentry-python/issues/5455",
        "evidence": [f"{PY}/sentry_sdk/integrations/litellm.py#L258-L265",
                     "litellm/types/utils.py:1762-1910 (litellm 1.104.0): Usage carries prompt_tokens_details "
                     "and completion_tokens_details, so the data is available to the callback"],
        "scenarios": ["litellm.completion.openai", "litellm.acompletion.openai", "litellm.completion.openai.stream",
                      "litellm.completion.anthropic"],
        "impact": "Every cached or reasoning token through LiteLLM is invisible; for an Anthropic model behind "
                  "LiteLLM the cache-write tokens (billed at 1.25x) are missing too.",
        "fixed": "python",
        "fix": "Patched in this work (draft PR getsentry/sentry-python#7880, not merged): read "
               "prompt_tokens_details.cached_tokens / cache_write_tokens and "
               "completion_tokens_details.reasoning_tokens (with LiteLLM's private fallbacks). SpanProof usage "
               "findings for LiteLLM: 8 -> 0 on HEAD; sentry-python's LiteLLM suite 173/173 under tox, with two "
               "new regression tests that fail on the original code.",
    },
    {
        "id": "SP-02",
        "title": "OpenAI stream closed early: the gen_ai span is never sent (Chat and Responses)",
        "sdk": "python", "integration": "openai", "severity": "high", "intent": "unintended",
        "status": "known", "issue": "https://github.com/getsentry/sentry-python/issues/7847",
        "evidence": [f"{PY}/sentry_sdk/integrations/openai.py#L1001-L1286 (span exits only after the for loop; "
                     "no finally, no Stream.close hook)"],
        "scenarios": ["openai.chat.stream.early_close", "openai.responses.stream.early_close"],
        "impact": "A user who stops reading a stream (UI cancel, first-token routing) makes the LLM call disappear "
                  "from Sentry: no latency, no tokens, no cost. The JS SDK handles the same case correctly "
                  "(js.openai.chat.stream.early_close is green), as does Python Anthropic.",
        "fix": "Mirror anthropic.py's _StreamSpanContext/_wrap_close: finish the span exactly once in a finally "
               "and on close()/aclose(); only mark an error for real exceptions, not GeneratorExit.",
    },
    {
        "id": "SP-03",
        "title": "LiteLLM stream closed early: the gen_ai span is never sent",
        "sdk": "python", "integration": "litellm", "severity": "high", "intent": "unintended", "status": "new",
        "evidence": [f"{PY}/sentry_sdk/integrations/litellm.py#L298-L312 (span finished only when "
                     "complete_streaming_response arrives; LiteLLM fires no callback for an abandoned stream)"],
        "scenarios": ["litellm.completion.openai.stream.early_close"],
        "impact": "Same blind spot as SP-02 for every provider routed through LiteLLM.",
        "fix": "Finish the stored span from a wrapper on CustomStreamWrapper iteration/close when the final "
               "callback never fires.",
    },
    {
        "id": "SP-04",
        "title": "OpenAI Responses cache_write_tokens never recorded (Python and JavaScript)",
        "sdk": "python+js", "integration": "openai", "severity": "medium", "intent": "unintended",
        "status": "reported: sentry-python#7870", "issue": "https://github.com/getsentry/sentry-python/issues/7870",
        "evidence": [f"{PY}/sentry_sdk/integrations/openai.py#L283-L289",
                     f"{JS_VER}: build/cjs/ai/openai/utils.js:25-37",
                     "openai-python 2.54.0 openai/types/responses/response_usage.py:11 (InputTokensDetails."
                     "cache_write_tokens is a required field)"],
        "scenarios": ["openai.responses.sync", "openai.responses.stream", "js.openai.responses.sync"],
        "impact": "Prompt-cache writes are billed above the normal input rate and are not visible in Sentry.",
        "fix": "Patched in this work for Python, not merged (Chat Completions and Responses): read cache_write_tokens "
               "and pass "
               "input_tokens_cache_write to record_token_usage. SpanProof OpenAI usage findings 8 -> 0; "
               "sentry-python OpenAI suite 687/687 (openai 2.54.0) and 683 + 4 skipped (openai 1.109.1); the new "
               "test fails on the original code. JS still open.",
        "fixed": "python",
    },
    {
        "id": "SP-05",
        "title": "Anthropic with_raw_response: span closes before parse(), so no usage, model or id",
        "sdk": "python", "integration": "anthropic", "severity": "high", "intent": "unintended", "status": "new",
        "evidence": [f"{PY}/sentry_sdk/integrations/anthropic.py#L790-L817 (APIResponse has no .usage, falls to "
                     "the unknown-response branch and exits the span)"],
        "scenarios": ["anthropic.messages.raw_response"],
        "impact": "Apps that read headers (rate limits, request ids) via with_raw_response lose all token data.",
        "fix": "Detect APIResponse/AsyncAPIResponse and finish the span after parse() (wrap parse on the "
               "instance), running the existing output handling on the parsed Message.",
    },
    {
        "id": "SP-06",
        "title": "Summing tokens across span levels double counts agent runs (2.00x to 3.00x billed)",
        "sdk": "python+js", "integration": "openai_agents, langgraph, vercel_ai", "severity": "high",
        "intent": "unintended", "status": "known",
        "issue": "https://github.com/getsentry/sentry/issues/114914",
        "evidence": ["invoke_agent spans carry the run's total usage and the LLM spans inside carry the same "
                     "tokens again; the conventions text asks queries to filter operation.type=ai_client",
                     "sentry-python PR #7515 (merged to major/3.0, not master) stops setting tokens on "
                     "invoke_agent spans"],
        "scenarios": ["openai_agents.run.chat", "openai_agents.run.responses", "openai_agents.agent_as_tool",
                      "langgraph.react_agent", "js.vercel_ai.generate_text"],
        "impact": "Measured: a naive sum(input_tokens) over all spans = 2.00x billed (openai-agents, langgraph, "
                  "Vercel generateText), 2.72x for an agent used as a tool, cached tokens 3.00x. The agent "
                  "aggregate is itself correct (except SP-07); the inflation comes from summing both levels, "
                  "which is what a dashboard sum() does unless it filters to LLM-call spans.",
        "fix": "SpanProof gives #114914 a regression test: whichever fix lands (Relay-side filter or SDK-side "
               "removal as in #7515), the aggregation check must go green on these five scenarios.",
    },
    {
        "id": "SP-07",
        "title": "Nested agent (Agent.as_tool) reports usage that includes its parent's tokens",
        "sdk": "python", "integration": "openai_agents", "severity": "high", "intent": "unintended",
        "status": "new",
        "evidence": ["sentry_sdk/integrations/openai_agents/spans/invoke_agent.py:117-119 reads "
                     "context.usage at the end of the run",
                     "openai-agents shares one Usage object into nested runs (agents/agent.py as_tool, "
                     "run_context.py:1330,1339); openai-agents' own spans use snapshot/usage_delta"],
        "scenarios": ["openai_agents.agent_as_tool"],
        "impact": "Inner agent reports input 2400 / output 75; its own calls sum to 1700 / 50. The extra 700 / 25 "
                  "are the outer agent's first call, so per-agent cost is wrong in the Agents dashboard.",
        "fix": "Snapshot usage when the invoke_agent span starts and report the delta, as openai-agents does.",
    },
    {
        "id": "SP-08",
        "title": "LangGraph graph.stream()/astream(): no invoke_agent span",
        "sdk": "python", "integration": "langgraph", "severity": "high", "intent": "unintended",
        "status": "reported: sentry-python#7871", "issue": "https://github.com/getsentry/sentry-python/issues/7871",
        "evidence": [f"{PY}/sentry_sdk/integrations/langgraph.py#L44-L53 (only Pregel.invoke/ainvoke are "
                     "wrapped; the comment assumes stream runs through invoke, but in langgraph 1.2.13 invoke "
                     "calls stream, not the reverse)"],
        "scenarios": ["langgraph.react_agent.stream"],
        "impact": "Streaming LangGraph agents (the common UI case) have no agent span: no agent name, no "
                  "grouping in the Agents dashboard. JS has a matching report (sentry-javascript#19626).",
        "fix": "Patched in this work (not merged): wrap Pregel.stream/astream; a context variable set inside "
               "invoke/ainvoke "
               "stops the inner stream call from opening a second span; the span ends in a finally, so early "
               "close is covered. SpanProof: invoke_agent spans for .stream() 0 -> 1, .invoke() stays at 1, "
               "structure findings 8 -> 0 (plus astream and early-close scenarios). LangGraph suite 150/150 on "
               "langgraph 1.2.12 and 0.6.11 with four new tests.",
        "fixed": "python",
    },
    {
        "id": "SP-09",
        "title": "Pydantic AI Agent.iter(): no agent or tool spans",
        "sdk": "python", "integration": "pydantic_ai", "severity": "high", "intent": "unintended",
        "status": "known", "issue": "https://github.com/getsentry/sentry-python/issues/5606",
        "evidence": ["scenario pydantic_ai.iter vs pydantic_ai.run_sync on the same script"],
        "scenarios": ["pydantic_ai.iter"],
        "impact": "Reproduced on pydantic-ai 2.54 at HEAD; the issue has no staff reply yet.",
        "fix": "Instrument the AgentRun context manager returned by iter().",
    },
    {
        "id": "SP-10",
        "title": "Finish reasons not recorded by OpenAI, OpenAI Agents, Pydantic AI (and LiteLLM)",
        "fixed": "partial",
        "sdk": "python", "integration": "openai, openai_agents, pydantic_ai, litellm", "severity": "medium",
        "intent": "unintended",
        "status": "new (LiteLLM part known: #5808; Pydantic AI part reported: sentry-python#7872)",
        "issue": "https://github.com/getsentry/sentry-python/issues/5808",
        "evidence": ["grep FINISH_REASON: set in anthropic.py:683, langchain.py:660, google_genai, huggingface_hub; "
                     "absent from openai.py, litellm.py, openai_agents/, pydantic_ai/",
                     "@sentry/node records finish reasons for OpenAI (JS openai/utils.js:39-43): a parity gap"],
        "scenarios": ["openai.chat.sync", "openai_agents.run.chat", "pydantic_ai.run_sync", "litellm.completion.openai"],
        "impact": "Truncated answers (finish_reason=length) and tool-stops are invisible. The failure-class "
                  "detector for truncation reaches recall 1.0 where finish reasons exist and cannot work where "
                  "they do not (see detector results).",
        "fix": "Pydantic AI part patched in this work (9 lines, not merged): truncation detector recall 0.33 -> "
               "0.67, Pydantic AI "
               "suite 332/332, new test fails on the original code. OpenAI and openai-agents still open "
               "(openai-agents' ModelResponse carries no finish reason, so that one needs upstream support).",
    },
    {
        "id": "SP-11",
        "title": "JS OpenAI ignores cached and reasoning tokens",
        "sdk": "js", "integration": "openai", "severity": "medium", "intent": "unintended", "status": "new",
        "issue": "https://github.com/getsentry/sentry-javascript/issues/20578",
        "evidence": [f"{JS_VER}: build/cjs/ai/openai/utils.js:25-37 reads prompt/completion/total only"],
        "scenarios": ["js.openai.chat.sync", "js.openai.chat.stream", "js.openai.responses.sync"],
        "impact": "The Python SDK reports 1024 cached / 256 reasoning tokens for the same response; the JS SDK "
                  "reports none. The open tracker #20578 lists attribute renames, not this gap.",
        "fix": "Read prompt_tokens_details / input_tokens_details and the output details.",
    },
    {
        "id": "SP-12",
        "title": "JS Anthropic: input_tokens excludes cache; cache attributes never emitted",
        "sdk": "js", "integration": "anthropic", "severity": "high", "intent": "unintended",
        "status": "reported: sentry-javascript#25069",
        "issue": "https://github.com/getsentry/sentry-javascript/issues/25069",
        "evidence": [f"{JS_VER}: build/cjs/ai/core/utils.js:25-42 setTokenUsageAttributes adds the two cache "
                     "counts into total_tokens only; anthropic-ai/index.js:68-74 passes cache_creation and "
                     "cache_read into slots named cachedInputTokens/cachedOutputTokens"],
        "scenarios": ["js.anthropic.messages.sync", "js.anthropic.messages.stream", "js.anthropic.messages.stream_helper"],
        "impact": "Same response: Python reports input 2600 (inclusive, per the conventions) with cached 2048 and "
                  "cache-write 512; JS reports input 40, no cache attributes, and total 2720 that contradicts "
                  "input+output. On a real Sentry account the trace view shows '40 in + 120 out = 160 total' for this "
                  "call (Python shows 2.6K in) and the stored cost is $0.00128 instead of $0.00297 (57% low).",
        "fix": "Emit cache_read/cache_creation attributes and make input_tokens inclusive, as Python does.",
    },
    {
        "id": "SP-13",
        "title": "JS Vercel AI streamText stopped early: parent span never sent, child orphaned",
        "sdk": "js", "integration": "vercel_ai", "severity": "medium", "intent": "unintended", "status": "new",
        "evidence": ["scenario js.vercel_ai.stream_text.early_close: gen_ai.generate_content arrives with a "
                     "parent_span_id that is not delivered, checked after flush and SDK close; a fully consumed "
                     "stream delivers the parent"],
        "scenarios": ["js.vercel_ai.stream_text.early_close"],
        "impact": "Trace view shows a detached model call; agent-level grouping is lost.",
        "fix": "End the streamText span when the consumer stops reading (return() on the async iterator).",
    },
    {
        "id": "SP-14",
        "title": "List-valued finish_reasons cannot be searched in Sentry with the default gen_ai transport",
        "sdk": "python", "integration": "anthropic, langchain, langgraph", "severity": "medium",
        "intent": "unintended", "status": "reported: sentry-python#7873",
        "issue": "https://github.com/getsentry/sentry-python/issues/7873",
        "evidence": [f"{PY}/sentry_sdk/integrations/anthropic.py#L683", f"{PY}/sentry_sdk/integrations/langgraph.py#L178",
                     "sentry-conventions gen_ai__response__finish_reasons.json: type string; OTel defines an array"],
        "scenarios": ["anthropic.messages.sync", "langgraph.react_agent"],
        "impact": "Measured on a real Sentry account: with stream_gen_ai_spans (the default) the list is stored "
                  "but has:gen_ai.response.finish_reasons matches 0 spans; with the legacy transport, or the JS "
                  "SDK's JSON string, it matches. Python users cannot filter for truncated answers.",
        "fix": "Send a JSON string, as the conventions type it (the Pydantic AI fix does this and is searchable "
               "in both modes on a real account).",
    },
    {
        "id": "SP-15",
        "title": "LiteLLM streams report the requested model instead of the provider's model",
        "sdk": "python", "integration": "litellm", "severity": "low", "intent": "not a fault",
        "status": "new",
        "evidence": ["litellm streaming_handler.py:1421 sets model_response.model = self.model on every chunk; "
                     "the provider model survives in _hidden_params['provider_response_model']"],
        "scenarios": ["litellm.completion.openai.stream"],
        "impact": "gen_ai.response.model shows 'gpt-5-mini' instead of 'gpt-5-mini-2026-08-07' for streams "
                  "only, so model-version dashboards split.",
        "fix": "LiteLLM normalizes the chunk model itself (reproduced without Sentry), so this is upstream "
               "behaviour. Sentry could still prefer _hidden_params['provider_response_model'] when present.",
    },
    {
        "id": "SP-16",
        "title": "OpenAI Agents as_tool split traces (#4786): not reproducible at HEAD",
        "sdk": "python", "integration": "openai_agents", "severity": "info", "intent": "not a fault",
        "status": "known", "issue": "https://github.com/getsentry/sentry-python/issues/4786",
        "evidence": ["openai_agents.agent_as_tool: 11 spans, one trace, correct nesting (router > ask_weather > "
                     "weather_agent > get_weather)"],
        "scenarios": ["openai_agents.agent_as_tool"],
        "impact": "Negative result: matches the maintainer's own 2025-10-02 attempt. Candidate to close or to "
                  "ask the reporter for a repro.",
        "fix": "None needed.",
    },
]

FINDINGS.append({
    "id": "SP-17",
    "title": "Anthropic stream via .asResponse(): span never ends on @sentry/node 10.76.0, fixed in 11.x",
    "sdk": "js", "integration": "anthropic", "severity": "medium", "intent": "unintended",
    "status": "known", "issue": "https://github.com/getsentry/sentry-javascript/issues/24258",
    "evidence": ["JS matrix: lifecycle finding on @sentry/node 10.76.0 (with openai 5 and 7), absent on 11.4.0"],
    "scenarios": ["js.anthropic.messages.stream.as_response"],
    "impact": "The issue is still open. SpanProof shows it is fixed on the 11.x line and still present on the "
              "latest 10.x release, which helps decide whether to backport or close.",
    "fix": "Backport to v10 or close for 11.x.",
})

FINDINGS.append({
    "id": "SP-18",
    "title": "Pydantic AI drops reasoning tokens",
    "sdk": "python", "integration": "pydantic_ai", "severity": "medium", "intent": "unintended",
    "status": "reported: sentry-python#7876", "issue": "https://github.com/getsentry/sentry-python/issues/7876",
    "evidence": [f"{PY}/sentry_sdk/integrations/pydantic_ai/spans/utils.py#L49-L87 (_set_usage_data reads input, "
                 "cache, output and total tokens but never usage.details['reasoning_tokens'], where pydantic-ai "
                 "puts them)"],
    "scenarios": ["pydantic_ai.run_sync (live, Groq gpt-oss-20b)"],
    "impact": "Found only with live traffic: Groq reported 9, 14 and 16 reasoning tokens on real calls and Sentry "
              "recorded none. Scripted fixtures never exercised it.",
    "fix": "Read usage.details.get('reasoning_tokens') and record it as output_tokens.reasoning (7 lines + test; "
           "Pydantic AI suite 332/332, the new test fails on the original code).",
})

NEGATIVE_RESULTS = [
    "Live Groq streams repeat the usage block on two chunks; Sentry's OpenAI integration takes the final report "
    "and records the right numbers. LangChain, however, adds the two reports together (75 + 75 = 150 input "
    "tokens), with or without Sentry, so LangChain-on-Groq users see doubled tokens in Sentry. Not a Sentry fault.",
    "On a real Sentry account, the built-in AI Agents dashboard does not double count: its per-model token "
    "totals equal the LLM-call-only sums exactly (486,010 / 79,906 / 19,500). The double counting (SP-06) affects "
    "custom queries, dashboards and alerts that sum across all gen_ai spans (+27% cost, +50% input tokens over "
    "the whole test run).",
    "On a real Sentry account, every span that was sent was stored with the same token values, and Sentry's cost "
    "calculation matched hand-computed prices exactly whenever the SDK sent correct numbers.",
    "Google GenAI (generate_content, stream, early close): usage, cached and thinking tokens all match the "
    "provider, on google-genai 1.29.0 through 2.28.0.",
    "Privacy gating: with data collection off, no prompt or completion content appeared on any span, in any "
    "integration, in any of the four transport modes.",
    "Anthropic Python streams closed early and the JS OpenAI stream closed early finish their spans correctly.",
    "Duplicate chat spans appear only if both a framework integration and OpenAIIntegration are listed "
    "explicitly; Sentry's default auto-enabling deactivates the plain integration, so this is not an SDK bug.",
    "openai-agents 0.8.4/0.16.1 crash with openai>=2.5x (InputTokensDetails.cache_write_tokens became required); "
    "that is dependency skew in openai-agents, not Sentry. SpanProof runs those cells with tox's pins.",
]
