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
               "findings for LiteLLM: 8 -> 0 on HEAD; sentry-python's LiteLLM suite 178/178 under tox, with five "
               "new tests, four of which fail on the original code.",
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

JS_SHA = "9ae9a3027daf12f31c7f848849dd1adbba732bc8"
JS = f"https://github.com/getsentry/sentry-javascript/blob/{JS_SHA}/packages/server-utils/src"
PYI = f"{PY}/sentry_sdk/integrations"


def _f(**kw):
    FINDINGS.append(kw)


# ------------------------------------------------- found by the 2026-10-06 exploration
_f(id="SP-19", title="Hugging Face and Cohere streams swallow provider errors: the app gets a shorter answer and no "
                     "exception",
   sdk="python", integration="huggingface_hub, cohere", severity="high", intent="unintended", status="new",
   evidence=[f"{PYI}/huggingface_hub.py#L352-L388 (the for/yield loop sits inside capture_internal_exceptions())",
             f"{PYI}/huggingface_hub.py#L286-L307 (text_generation stream, same pattern)",
             f"{PYI}/cohere.py#L223-L234 (chat_stream, same pattern)",
             "control: the same call without Sentry raises (huggingface_hub GenerationError, "
              "httpx.RemoteProtocolError)"],
   scenarios=["huggingface_hub.chat_completion.stream.server_error", "cohere.chat_stream.connection_drop"],
   impact="Sentry changes application behaviour: a stream that fails mid-way (TGI out of memory, dropped connection) "
          "ends early with no exception, so the app shows a truncated answer as if it were complete, and no error "
          "is reported anywhere. Also absorbs KeyboardInterrupt during a stream read (code reading, not tested).",
   fix="Keep only Sentry's own bookkeeping inside capture_internal_exceptions; let errors from the provider "
       "iterator propagate, mark the span as errored and end it in finally.")
_f(id="SP-20", title="Hugging Face chat stream: span lost on early close, break or mid-stream error",
   sdk="python", integration="huggingface_hub", severity="high", intent="unintended", status="new",
   evidence=[f"{PYI}/huggingface_hub.py#L454 (span.__exit__ runs only after the loop completes)"],
   scenarios=["huggingface_hub.chat_completion.stream.early_close", "huggingface_hub.chat_completion.stream.break",
              "huggingface_hub.chat_completion.stream.server_error"],
   impact="The gen_ai.chat span never finishes, so the call disappears from traces (same class as #7847 for OpenAI).",
   fix="End the span in a finally block of the wrapping generator.")
_f(id="SP-21", title="Hugging Face text_generation: generated tokens recorded as the total; input and output tokens "
                     "missing",
   sdk="python", integration="huggingface_hub", severity="high", intent="simplification", status="new",
   evidence=[f"{PYI}/huggingface_hub.py#L189-L194", f"{PYI}/huggingface_hub.py#L267-L271",
             "sentry-python's own tests assert total = generated tokens"],
   scenarios=["huggingface_hub.text_generation.details", "huggingface_hub.text_generation.stream"],
   impact="With details=True the provider reports input_length=8 and generated_tokens=3; Sentry records total=3 and "
          "nothing else, so input cost is invisible and the total is wrong.",
   fix="record_token_usage(input_tokens=details.input_length, output_tokens=details.generated_tokens).")
_f(id="SP-22", title="Mistral: chat.stream, chat.stream_async and embeddings.create produce no span",
   sdk="python", integration="mistral", severity="high", intent="unintended", status="new",
   evidence=[f"{PYI}/mistral.py#L50-L52 (only Chat.complete and complete_async are wrapped)"],
   scenarios=["mistral.chat.stream", "mistral.chat.stream.async", "mistral.embeddings.create"],
   impact="Streaming, the common chat UI path, and embeddings are invisible in Sentry.",
   fix="Wrap Chat.stream/stream_async (usage from the final chunk, span ended in finally) and Embeddings.create.")
_f(id="SP-23", title="OpenAI Responses stream that ends 'incomplete' records no tokens and no model",
   sdk="python", integration="openai", severity="high", intent="unintended", status="new",
   evidence=[f"{PYI}/openai.py#L1171 and #L1244 (only ResponseCompletedEvent is handled)",
             "control: the same response non-streamed is recorded correctly"],
   scenarios=["openai.adv.responses.incomplete.stream", "openai.adv.responses.incomplete.stream.async",
              "openai.adv.responses.incomplete_reasoning_only.stream"],
   impact="A call stopped by max_output_tokens (1,500 in + 4,096 out, 3,900 of it reasoning: 5,596 billed tokens) "
          "is recorded with 0 tokens. These are often the most expensive calls.",
   fix="Treat ResponseIncompleteEvent and ResponseFailedEvent like ResponseCompletedEvent (usage and model from "
       "x.response).")
_f(id="SP-24", title="OpenAI background responses: tokens never recorded (responses.retrieve is not instrumented)",
   sdk="python", integration="openai", severity="high", intent="unintended", status="new",
   evidence=[f"{PYI}/openai.py#L138-L149 (setup_once patches only .create)"],
   scenarios=["openai.adv.responses.background"],
   impact="create(background=True) returns 'queued' without usage; the billed result (1,500 in + 400 out) arrives "
          "through retrieve() and is never recorded.",
   fix="Wrap Responses.retrieve and record usage when the status is final, linked to the create span by response id.")
_f(id="SP-25", title="Structured-output parse() calls produce no span (OpenAI chat.completions.parse, Anthropic "
                     "messages.parse)",
   sdk="python", integration="openai, anthropic", severity="high", intent="unintended",
   status="new (responses.parse known: #5401)", issue="https://github.com/getsentry/sentry-python/issues/5401",
   evidence=[f"{PYI}/openai.py#L138-L149", f"{PYI}/anthropic.py#L245-L274",
             "openai and anthropic SDKs: parse() calls self._post directly, not create()"],
   scenarios=["openai.adv.chat.parse", "openai.adv.chat.parse.async", "openai.adv.responses.parse",
              "anthropic.adv.parse.sync", "anthropic.adv.parse.async"],
   impact="Every structured-output call (a common production pattern) is invisible: no span, no tokens, no cost.",
   fix="Wrap Completions.parse, Responses.parse and Messages.parse (sync and async) with the existing create handlers.")
_f(id="SP-26", title="OpenAI streamed n>1 chat: choices merged into one garbled string",
   sdk="python", integration="openai", severity="medium", intent="unintended", status="new",
   evidence=[f"{PYI}/openai.py#L1028-L1037 (position within the chunk used instead of choice.index)"],
   scenarios=["openai.adv.chat.n2.stream"],
   impact="'Paris is it.' and 'It is Paris.' are recorded as ['Paris It is is it.Paris.']. Tokens are correct.",
   fix="Index the buffers by choice.index.")
_f(id="SP-27", title="Anthropic stream: tool-call JSON glued into the answer text, tool calls not recorded",
   sdk="python", integration="anthropic", severity="medium", intent="unintended",
   status="related: #4242", issue="https://github.com/getsentry/sentry-python/issues/4242",
   evidence=[f"{PYI}/anthropic.py#L336-L342 (partial_json appended to the text buffer)",
             "control: the non-stream path is correct"],
   scenarios=["anthropic.adv.tool_round_trip.stream", "anthropic.adv.server_tools.stream"],
   impact="Recorded answer 'Let me check.{\"city\": \"Paris\"}'; with web search the query JSON is prepended to the "
          "answer.",
   fix="Collect content per block; tool_use input goes to tool calls, server_tool_use input is skipped.")
_f(id="SP-28", title="Hugging Face streamed tool call: only the last delta is kept",
   sdk="python", integration="huggingface_hub", severity="medium", intent="unintended", status="new",
   evidence=[f"{PYI}/huggingface_hub.py#L381-L386"],
   scenarios=["huggingface_hub.chat_completion.stream.tool_call"],
   impact="Recorded tool call has name None and arguments 'ris\"}' instead of get_weather({\"city\": \"Paris\"}).",
   fix="Accumulate tool-call deltas per index.")
_f(id="SP-29", title="Mistral: response model, id and finish reason not recorded; tool calls dropped",
   sdk="python", integration="mistral", severity="medium", intent="unintended", status="new",
   evidence=[f"{PYI}/mistral.py#L321-L361", f"{PYI}/mistral.py#L152-L189"],
   scenarios=["mistral.chat.complete", "mistral.chat.complete.tool_call"],
   impact="Truncation and tool-stops are invisible and calls cannot be joined with provider logs.",
   fix="Set response model, id and finish reasons; add tool_call parts to the output messages.")
_f(id="SP-30", title="Anthropic thinking tokens not recorded",
   sdk="python", integration="anthropic", severity="medium", intent="unintended",
   status="known", issue="https://github.com/getsentry/sentry-python/issues/5802",
   evidence=[f"{PYI}/anthropic.py#L286-L320 (usage.output_tokens_details.thinking_tokens is not read)"],
   scenarios=["anthropic.adv.thinking.sync", "anthropic.adv.thinking.stream", "anthropic.adv.thinking.stream_helper"],
   impact="900 of 1,100 output tokens were reasoning; none recorded. Output totals and cost stay right.",
   fix="Read thinking_tokens as output_tokens.reasoning (sync and message_delta paths).")
_f(id="SP-31", title="Billing facts with no attribute: server and built-in tool calls, cache-write TTL, audio tokens, "
                     "service tier",
   sdk="python", integration="openai, anthropic", severity="medium", intent="needs decision",
   status="convention gap (partly getsentry/sentry-conventions#64)",
   issue="https://github.com/getsentry/sentry-conventions/issues/64",
   evidence=[f"{PYI}/anthropic.py#L391 (# TODO: Record event.usage.server_tool_use)",
             "sentry-conventions at 6f0307c and ff34ba1: no attribute for any of these"],
   scenarios=["anthropic.adv.server_tools.sync", "anthropic.adv.cache_ttl.sync",
   "openai.adv.responses.builtin_tools.sync",
              "openai.adv.chat.audio_tokens", "openai.adv.chat.service_tier"],
   impact="Per-request tool fees (web search), the 1-hour cache-write price (2x input vs 1.25x for 5 minutes), "
          "audio tokens and flex/priority tiers change the bill but cannot be represented, so cost is understated.",
   fix="Conventions first (attributes for these), then the integrations.")
_f(id="SP-32", title="MCP: tool errors are recorded as successes",
   sdk="python", integration="mcp", severity="high", intent="unintended", status="new",
   evidence=[f"{PYI}/mcp.py#L457-L501 (mcp 2.x middleware never reads result['isError'])",
             f"{PYI}/mcp.py#L357-L395 (mcp 1.x wrapper ignores a returned CallToolResult.isError)",
             "the client receives isError=true; the span status stays ok and the MCP integration captures nothing"],
   scenarios=["mcp: tool_raise, tool_is_error, tool_bad_args, tool_unknown (python -m spanproof.mcp_run)"],
   impact="On mcp 2.x and fastmcp 4 every tool exception, bad-argument call and unknown-tool call ends this way, "
          "so MCP tool error rates read 0%.",
   fix="After call_next, if result.get('isError') set the span status to error and error.type; same for mcp 1.x.")
_f(id="SP-33", title="MCP: concurrent requests on one connection nest under each other, and on stdio most are dropped",
   sdk="python", integration="mcp", severity="high", intent="unintended", status="new",
   evidence=[f"{PYI}/mcp.py#L330-L340 (spans started on the shared scope; no new_scope/isolation_scope per request)",
             "control: with AsyncioIntegration enabled, 0 nested and 0 lost"],
   scenarios=["mcp: concurrent_0..9 on stdio, memory and SSE"],
   impact="Three concurrent tool calls on stdio produce one transaction: 4 of 5 spans are lost in every stdio cell "
          "(288 of 288 expected), and the rest form a false parent chain.",
   fix="Enter each handler span inside sentry_sdk.new_scope() (or isolation_scope) per request.")
_f(id="SP-34", title="MCP small gaps: structuredContent ignored on mcp 1.x, content_count counts dict keys, request "
                     "id 0 dropped",
   sdk="python", integration="mcp", severity="low", intent="unintended", status="new",
   evidence=[f"{PYI}/mcp.py#L239 (getattr(result, 'structured_content'); the 1.x field is structuredContent)",
             f"{PYI}/mcp.py#L388-L392", f"{PYI}/mcp.py#L209 (if request_id: skips id 0)"],
   scenarios=["mcp: tool_structured, tool_ok"],
   impact="Recorded results and counts disagree with the registry's definitions; one JSON-RPC id is never recorded.",
   fix="Read structuredContent, count content items, test request_id is not None.")
_f(id="SP-35", title="Known gaps reproduced: uninstrumented client methods",
   sdk="python", integration="anthropic, cohere, huggingface_hub", severity="medium", intent="unintended",
   status="known", issue="https://github.com/getsentry/sentry-python/issues/5806",
   evidence=["anthropic beta.messages (#5806), cohere async (#5845) and ClientV2 (#5844), "
             "huggingface_hub AsyncInferenceClient (#5846): no span in each case"],
   scenarios=["anthropic.adv.beta.sync", "cohere.chat.async", "cohere.v2.chat",
              "huggingface_hub.chat_completion.async"],
   impact="Confirms the open issues still apply at HEAD.", fix="Tracked in the linked issues.")
_f(id="SP-36", title="Cohere: legacy ai.* span ops and attributes",
   sdk="python", integration="cohere", severity="medium", intent="unintended",
   status="known", issue="https://github.com/getsentry/sentry-python/issues/4760",
   evidence=[f"{PYI}/cohere.py#L173-L182"], scenarios=["cohere.chat.sync"],
   impact="Cohere calls do not appear in the Agents views that filter on gen_ai.* ops.", fix="Tracked in #4760.")
_f(id="SP-37", title="JS LangChain createAgent (LangChain v1's recommended agent API): no agent span, no tool spans",
   sdk="js", integration="langgraph", severity="high", intent="unintended", status="new",
   evidence=[f"{JS}/integrations/langgraph.ts#L115-L120 (invoke is patched on the compiled graph instance)",
             "langchain createAgent then calls .withConfig(), which builds a new Pregel object without the patch"],
   scenarios=["js.langgraph.create_agent"],
   impact="Agents built the recommended way show up as flat LLM calls: no agent name, no tools, no Agents view.",
   fix="Instrument Pregel.prototype.invoke/stream (guarded) instead of the instance, or carry the wrapper across "
       "withConfig.")
_f(id="SP-38", title="JS LangGraph with a checkpointer: agent span re-counts earlier turns",
   sdk="js", integration="langgraph", severity="high", intent="unintended", status="new",
   evidence=[f"{JS}/ai/langgraph/utils.ts#L283-L285 (new messages = output.slice(input length); with thread memory the "
             "output also holds earlier turns)"],
   scenarios=["js.langgraph.react_agent.thread"],
   impact="Turn 2 reports 2,700 input / 62 output when its only LLM call was 1,000 / 12; over the conversation agent "
          "spans say 4,400 against 2,700 billed, and turn 1's tool calls are reported again.",
   fix="Take agent usage from the LLM runs under this invoke, not from a slice of the message list.")
_f(id="SP-39", title="JS Google GenAI: thinking tokens dropped from output, cached and reasoning never emitted",
   sdk="js", integration="google_genai", severity="high", intent="unintended", status="new",
   evidence=[f"{JS}/ai/google-genai/index.ts#L208-L220 (output = candidatesTokenCount only)",
             "Python reports the same response correctly; only the Vercel AI path was fixed (#23433/#24066)"],
   scenarios=["js.google_genai.generate_content", "js.google_genai.generate_content_stream",
              "js.google_genai.chat.send_message"],
   impact="Output 200 instead of 350 (43% low); 512 cached and 150 reasoning tokens missing; total != input + output.",
   fix="output = candidatesTokenCount + thoughtsTokenCount; emit cache_read and reasoning (index.ts and streaming.ts).")
_f(id="SP-40", title="JS LangChain with Anthropic: input excludes cache, cache attributes missing",
   sdk="js", integration="langchain", severity="high", intent="unintended", status="new (same class as js#25069)",
   issue="https://github.com/getsentry/sentry-javascript/issues/25069",
   evidence=[f"{JS}/ai/langchain/utils.ts#L358-L360 (anthropicUsage.input_tokens used as input)"],
   scenarios=["js.langchain.chat_anthropic.invoke", "js.langchain.chat_anthropic.stream",
              "js.langgraph.react_agent.anthropic"],
   impact="invoke reports input 40 with cache_read 2048 > input; stream reports 2,600 with no cache attributes; in "
          "LangGraph the agent span says 2,138 while its LLM spans say 30 + 60.",
   fix="Prefer message.usage_metadata (provider-neutral, cache included, carries cache and reasoning details).")
_f(id="SP-41", title="JS LangChain: cached and reasoning tokens never emitted",
   sdk="js", integration="langchain", severity="medium", intent="unintended", status="new",
   evidence=[f"{JS}/ai/langchain/utils.ts#L355-L357 (only llmOutput.tokenUsage is read)",
             "the data is on message.usage_metadata.input_token_details / output_token_details "
              "(checked without Sentry)"],
   scenarios=["js.langchain.chat_openai.invoke", "js.langchain.chat_openai.stream", "js.langgraph.react_agent"],
   impact="1,024 cached and 256 reasoning tokens missing on OpenAI; Python reports both.", fix="Same as SP-40.")
_f(id="SP-42", title="JS LangChain: tool-call arguments recorded with recordOutputs off",
   sdk="js", integration="langchain", severity="high", intent="unintended",
   status="new (#19812 closed, this part left)",
   evidence=[f"{JS}/ai/langchain/utils.ts#L413-L414 (comment: names and IDs are not PII, so captured regardless; "
             "the arguments are captured too)"],
   scenarios=["js.langchain.chat_openai.tool_call (nodc)", "js.langgraph.react_agent (nodc)"],
   impact="With sendDefaultPii off, gen_ai.response.tool_calls still carries {\"city\": \"Paris\"}. The OpenAI and "
          "Google integrations, and the LangGraph tool span, drop the same arguments.",
   fix="Record names and IDs only, or move arguments under recordOutputs.")
_f(id="SP-43", title="JS LangGraph without the LangChain integration: every LLM call reported twice",
   sdk="js", integration="langgraph", severity="medium", intent="unintended", status="new",
   evidence=["integrations/langgraph injects the callback handler but never marks providers as skipped"],
   scenarios=["js.langgraph.react_agent.without_langchain"],
   impact="Only with an explicit integration list (the default list includes LangChain): 3,400 tokens "
           "for 1,700 billed.",
   fix="Mark providers skipped inside LangGraph runs, as the LangChain integration does.")
_f(id="SP-44", title="JS: after the first LangChain call, direct provider calls lose their spans process-wide",
   sdk="js", integration="langchain", severity="high", intent="simplification",
   status="known", issue="https://github.com/getsentry/sentry-javascript/issues/19687",
   evidence=["core/src/utils/ai/providerSkip.ts (a module-level Set); Sentry's own test pins the behaviour"],
   scenarios=["js.langchain.then_direct_openai", "js.langchain.then_direct_anthropic",
              "js.langchain.then_direct_google"],
   impact="Reproduced on 11.4.0; also hides Anthropic and Google calls after a LangChain call that used OpenAI.",
   fix="Scope the skip to LangChain runs (AsyncLocalStorage on the chat model channels).")

_f(id="SP-45", title="Cohere: meta.cached_tokens ignored",
   sdk="python", integration="cohere", severity="low", intent="needs decision", status="new",
   evidence=[f"{PYI}/cohere.py#L136-L148"], scenarios=["cohere.chat.cached"],
   impact="Whether Cohere counts cached tokens inside billed input is not confirmed, so the cost effect is unknown.",
   fix="Record as input_tokens.cached once the billing semantics are confirmed.")
_f(id="SP-46", title="JS identity gaps: LangGraph finish_reasons sent as an array; LangChain + Google records a run "
                     "id as response id",
   sdk="js", integration="langgraph, langchain", severity="low", intent="unintended", status="new",
   evidence=[f"{JS}/ai/langgraph/utils.ts (finish reasons set as a list, same class as sentry-python#7873)",
             "LangChain records LangChain's run-<uuid> as gen_ai.response.id for Google models"],
   scenarios=["js.langgraph.react_agent", "js.langchain.chat_google.invoke"],
   impact="List values are not searchable with the default transport; response ids cannot be joined "
           "with provider logs.",
   fix="Send finish_reasons as a JSON string; read the provider's response id.")
_f(id="SP-47", title="JS LangGraph stream(): no invoke_agent span",
   sdk="js", integration="langgraph", severity="high", intent="unintended",
   status="known", issue="https://github.com/getsentry/sentry-javascript/issues/19626",
   evidence=[f"{JS}/integrations/langgraph.ts#L115-L120 (only invoke is wrapped)"],
   scenarios=["js.langgraph.react_agent.stream"],
   impact="Reproduced on 11.4.0: the tool span sits outside any agent (the Python side is SP-08).",
   fix="Tracked in #19626.")

_f(id="SP-48", title="Cohere 5.4.0: tool calls recorded as a Python repr string, not JSON",
   sdk="python", integration="cohere", severity="low", intent="unintended", status="new",
   evidence=["ai.tool_calls is \"name='get_weather' parameters={'city': 'Paris'}\" on cohere 5.4.0; JSON on 5.21.1 and later"],
   scenarios=["cohere.chat.tool_call (cohere 5.4.0)"],
   impact="On the oldest supported Cohere version the recorded tool calls cannot be parsed.",
   fix="Serialize the tool-call objects explicitly (model_dump / dict) instead of relying on str().")


NEGATIVE_RESULTS = [
    "LangChain JS + Google reports 200 output tokens because @langchain/google-genai leaves thoughts out of its own "
    "usage, and LangChain JS + Anthropic streams report 121 instead of 120 because @langchain/anthropic adds "
    "message_start's 1; both reproduce without Sentry loaded. Not Sentry faults.",
    "Hugging Face chat.completions.create (the OpenAI-compatible alias) is instrumented on every version from 0.24.7 "
    "to the pre-release, although sentry-python#5848 says otherwise.",
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
