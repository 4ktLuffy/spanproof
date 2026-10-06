# Independent held-out trace set

Run `python3 gen.py` (Python 3, standard library only) to regenerate and validate both artifacts. Seed: 731902. No detector code or external corpus is used.

240 traces: 96 healthy (40%) and 18 for each of eight failure labels. Each failure trace has one primary failure; retries recover, loops eventually answer, and tool errors never occur in other failure classes. Validation checks serialized span structure, parentage, semantic argument equality, provider pairing, retry chronology, terminal output, token accounting, and exclusive labels.

Span array order varies between chronological, shuffled, and reversed; start times therefore occur out of order in many arrays. Chronology is recovered by sorting start times; parent IDs stay intact. No artificial clock skew is introduced. Successful provider requests are children of their corresponding chat except in intentional instrumentation-gap cases. A same-agent nearby chat is not a match for an orphan request. Failed HTTP attempts belong to the chat they retried. Non-provider HTTP is excluded from LLM retry and pairing semantics.

Replicas vary provider endpoints, model names, root operations, null/ok successful HTTP status, tool argument/result aliases, JSON formatting, output message/text schemas, and finish-reason list/string encodings. Tool identity uses decoded JSON, not string equality; reordered keys are deliberately equivalent. Missing optional attributes are not treated as empty outputs: empty-answer cases explicitly record empty output. A tool request's empty text is not an empty final answer. Intermediate empty/truncated replies are followed by complete replies in healthy cases. Uppercase STOP and end_turn are complete answers.

Nested workers have distinct invocation scopes. In same_args_different_agents each scope calls the same tool only twice. In nested_unfinished the entire run ends with the worker's tool request and no parent response. Other nested failures either propagate to the parent or are observable within the worker. Handoffs and interleaved work finish normally. acknowledged_failure explicitly tells the user the lookup timed out and availability cannot be confirmed; these four traces are healthy.

Cost is the sum of chat input + output tokens, or the equivalent inclusive invocation total, never both added together. All ordinary traces total 1,000 tokens except four healthy 3,000-token runs; spikes total 24,000. Root totals, when present, exactly equal descendant usage. Each spike agent has 16 healthy baseline runs and three spikes. Per-agent medians are 1,000, so spikes are 24x typical. Nested specialist invocations have no spikes. Token counts describe full prompts/completions; recorded response text and tool results may be excerpts, as in telemetry. All final texts are synthetic and no real user data is included.

The automated semantic checks are corpus sanity checks, not an attempt to infer a particular detector. Tool-call message metadata matches the requested execution; execution arguments are authoritative. Variant names describe the main case, while replica-level variations intentionally overlap.

## Label totals

| Label | Count |
|---|---:|
| cost_spike | 18 |
| dead_end | 18 |
| empty_answer | 18 |
| healthy | 96 |
| lost_llm_span | 18 |
| retry_storm | 18 |
| silent_tool_error | 18 |
| tool_loop | 18 |
| truncated_answer | 18 |

## Every variant and count per label

| Label | Variant | Count |
|---|---|---:|
| cost_spike | balanced_usage | 3 |
| cost_spike | large_input | 3 |
| cost_spike | large_output | 3 |
| cost_spike | multiple_calls | 3 |
| cost_spike | root_and_chat_totals | 3 |
| cost_spike | root_totals_omitted | 3 |
| dead_end | executed_without_followup | 3 |
| dead_end | max_turns | 3 |
| dead_end | multiple_tools_requested | 3 |
| dead_end | nested_unfinished | 3 |
| dead_end | tool_calls_unexecuted | 3 |
| dead_end | tool_use_unexecuted | 3 |
| empty_answer | empty_content_blocks | 3 |
| empty_answer | empty_message_content | 3 |
| empty_answer | empty_output_list | 3 |
| empty_answer | empty_text_list | 3 |
| empty_answer | nested_empty | 3 |
| empty_answer | whitespace_only | 3 |
| healthy | acknowledged_failure | 4 |
| healthy | agent_as_tool | 4 |
| healthy | different_tool_same_args | 4 |
| healthy | end_turn | 4 |
| healthy | error_rate_metric | 4 |
| healthy | handoff | 4 |
| healthy | interleaved_agents | 4 |
| healthy | intermediate_empty | 4 |
| healthy | intermediate_length | 4 |
| healthy | non_llm_http | 4 |
| healthy | one_retry | 4 |
| healthy | optional_attributes_missing | 4 |
| healthy | pagination | 4 |
| healthy | rate_limit_recovered | 4 |
| healthy | same_args_different_agents | 4 |
| healthy | separated_provider_failures | 4 |
| healthy | short_complete | 4 |
| healthy | three_times_tokens | 4 |
| healthy | tool_request_resolved | 4 |
| healthy | two_failed_non_provider_calls | 4 |
| healthy | two_identical_calls | 4 |
| healthy | two_retries | 4 |
| healthy | uppercase_stop | 4 |
| healthy | zero_errors | 4 |
| lost_llm_span | nested_orphan | 3 |
| lost_llm_span | one_of_two_unmatched | 3 |
| lost_llm_span | orphan_chat_completions | 3 |
| lost_llm_span | orphan_generate_content | 3 |
| lost_llm_span | orphan_messages | 3 |
| lost_llm_span | orphan_responses | 3 |
| retry_storm | across_llm_calls | 3 |
| retry_storm | four_then_success | 3 |
| retry_storm | mixed_failures | 3 |
| retry_storm | nested_provider_retries | 3 |
| retry_storm | three_429 | 3 |
| retry_storm | three_5xx | 3 |
| silent_tool_error | error_json | 3 |
| silent_tool_error | exception_output | 3 |
| silent_tool_error | http_503_output | 3 |
| silent_tool_error | nested_tool_failure | 3 |
| silent_tool_error | status_only | 3 |
| silent_tool_error | timeout_output | 3 |
| tool_loop | exact_arguments | 3 |
| tool_loop | four_repeats | 3 |
| tool_loop | input_alias | 3 |
| tool_loop | intervening_progress | 3 |
| tool_loop | nested_agent_loop | 3 |
| tool_loop | reordered_argument_keys | 3 |
| truncated_answer | length_list | 3 |
| truncated_answer | length_string | 3 |
| truncated_answer | max_tokens_list | 3 |
| truncated_answer | max_tokens_string | 3 |
| truncated_answer | nested_final | 3 |
| truncated_answer | partial_json | 3 |

## Cost baselines

| Agent name | Healthy runs | Median tokens | Spike runs | Spike tokens |
|---|---:|---:|---:|---:|
| inventory_assistant | 16 | 1000 | 3 | 24000 |
| travel.concierge | 16 | 1000 | 3 | 24000 |
| Support/Triage | 16 | 1000 | 3 | 24000 |
| research-worker-v2 | 16 | 1000 | 3 | 24000 |
| billing_helper | 16 | 1000 | 3 | 24000 |
| Workspace Analyst | 16 | 1000 | 3 | 24000 |
