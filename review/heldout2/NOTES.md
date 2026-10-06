# Independent Round 2 held-out corpus

Seed: `73190427`. Regenerate with `python3 gen.py` (standard library only).

240 traces: 96 healthy (40%) and 18 for each of eight failure classes. Round 2 overrides the earlier filename: the dataset is `heldout2.jsonl`.

Each variant has its own construction; replicas vary provider, agent name, attribute spelling, encoding, status nullability, and serialization order. Event chronology is given by start, not list position. Provider children occur after their chat span start. Unknown finish reasons are not inferred from text. Empty answers contain explicitly empty output, not missing output telemetry.

Successful provider requests are nested under their matching chat except in instrumentation-gap cases. Failed attempts are counted in timestamp order, across calls when applicable. Non-provider outages are excluded. Tool argument equality uses parsed JSON objects, preserving types and array order. Pending tool results in loops are successful pending responses, not tool errors. Silent-error final answers explicitly claim unsupported success. Recovery cases disclose the original failure.

Cost baselines use the same top-level agent name, summing chat usage once across its run. Each agent has 16 healthy runs. All spikes exceed eight times even the largest healthy run for their agent. Specialist names have no spikes. No invoke-agent usage totals duplicate chat totals.

## Counts and variants

| Label | Variant | Count |
|---|---|---:|
| cost_spike | duplicated_source_bundle | 3 |
| cost_spike | expensive_final_synthesis | 3 |
| cost_spike | oversized_archive_context | 3 |
| cost_spike | retrieval_context_flood | 3 |
| cost_spike | runaway_report_budget | 3 |
| cost_spike | unexpected_reasoning_bill | 3 |
| dead_end | fulfilled_tool_without_resume | 3 |
| dead_end | handoff_tool_request | 3 |
| dead_end | nested_request_abandoned | 3 |
| dead_end | tool_use_without_dispatch | 3 |
| dead_end | turn_budget_after_result | 3 |
| dead_end | unexecuted_dispatch | 3 |
| empty_answer | blank_final_after_evidence | 3 |
| empty_answer | empty_content_block | 3 |
| empty_answer | empty_message_content | 3 |
| empty_answer | empty_text_item | 3 |
| empty_answer | zero_output_messages | 3 |
| empty_answer | zero_text_items | 3 |
| healthy | alternating_retry_windows | 8 |
| healthy | converging_solver | 8 |
| healthy | delegated_subtask_return | 8 |
| healthy | error_budget_measurement | 8 |
| healthy | historical_limit_in_document | 8 |
| healthy | optional_finish_absent | 8 |
| healthy | parallel_specialist_join | 8 |
| healthy | quoted_error_manual | 8 |
| healthy | recovered_tool_fallback | 8 |
| healthy | rest_gateway_outage | 8 |
| healthy | terse_verdict | 8 |
| healthy | typed_argument_changes | 8 |
| lost_llm_span | custom_transport_wrapper | 3 |
| lost_llm_span | delegated_http_gap | 3 |
| lost_llm_span | direct_agent_responses | 3 |
| lost_llm_span | gemini_stream_gap | 3 |
| lost_llm_span | one_of_two_calls_missing | 3 |
| lost_llm_span | orphan_messages_request | 3 |
| retry_storm | cross_call_failure_chain | 3 |
| retry_storm | delegated_retry_burst | 3 |
| retry_storm | five_attempt_recovery | 3 |
| retry_storm | messages_gateway_chain | 3 |
| retry_storm | mixed_throttle_outage | 3 |
| retry_storm | overload_triplet | 3 |
| silent_tool_error | empty_success_claim_after_abort | 3 |
| silent_tool_error | error_status_ignored | 3 |
| silent_tool_error | service_503_ignored | 3 |
| silent_tool_error | structured_denial_ignored | 3 |
| silent_tool_error | timeout_text_ignored | 3 |
| silent_tool_error | traceback_ignored | 3 |
| tool_loop | array_payload_replay | 3 |
| tool_loop | canonical_object_reordering | 3 |
| tool_loop | delegated_index_lock | 3 |
| tool_loop | empty_parameter_poll | 3 |
| tool_loop | intervening_probe | 3 |
| tool_loop | nested_object_reordering | 3 |
| truncated_answer | capped_final_after_lookup | 3 |
| truncated_answer | cutoff_enumeration | 3 |
| truncated_answer | cutoff_json | 3 |
| truncated_answer | cutoff_sentence | 3 |
| truncated_answer | delegated_report_limit | 3 |
| truncated_answer | long_context_cutoff | 3 |

## Healthy case interpretation

- `converging_solver`: Repeated solver calls change tolerance and initial estimates; no identical arguments.
- `alternating_retry_windows`: Four provider failures total, separated into pairs by successful attempts.
- `rest_gateway_outage`: Three 503s from a non-LLM inventory endpoint; final answer explicitly reports unavailability.
- `quoted_error_manual`: Successful tool retrieves documentation quoting an error JSON example; answer explains the example.
- `error_budget_measurement`: Tool successfully reports an error budget metric, not an execution error.
- `recovered_tool_fallback`: Primary tool times out; distinct fallback succeeds; final answer discloses the recovery.
- `parallel_specialist_join`: Sibling specialists answer independently and coordinator synthesizes after both finish.
- `delegated_subtask_return`: Agent-as-tool specialist returns a complete result before coordinator final answer.
- `historical_limit_in_document`: Retrieved documentation mentions max_tokens and tool_calls; actual final reason is end_turn.
- `optional_finish_absent`: Final answer is explicit and complete although optional finish reason is absent.
- `terse_verdict`: One-word complete answer with uppercase STOP finish reason.
- `typed_argument_changes`: Tool arguments differ by JSON type, array order, or explicit null versus omission.

## Failure case interpretation

- Tool loops replay the same parsed arguments at least three times within one agent, including nested object key permutations and an unrelated interleaved probe.
- Retry storms contain three to five consecutive provider failures followed by recovery. A recovered storm remains a storm.
- Silent tool failures use explicit structured denial, timeout, HTTP failure, span error status, traceback, or aborted transaction; the final answer claims verification succeeded.
- Missing LLM spans include direct, orphaned, transport-wrapped, delegated, partial, and Gemini provider gaps. Delivery events document a completed answer without fabricating a chat span.
- Dead ends stop after a tool request, sometimes after a successful tool result, and never resume to a final answer. Delegation acknowledgements are not final answers.
- Truncations have visibly incomplete text and final length/max_tokens finish reasons.
- Empty final answers use empty text values or empty output arrays with a normal termination reason.
- Cost spikes are otherwise successful, complete runs with 24,000–30,000 tokens.

## Baseline audit

| Agent | Healthy runs | Healthy token range | Spike runs | Minimum spike / maximum healthy |
|---|---:|---:|---:|---:|
| archive-curator | 16 | 1000–1600 | 3 | 15.75× |
| harbor-planner | 16 | 1000–1600 | 3 | 15.00× |
| lab-coordinator | 16 | 1000–1600 | 3 | 15.75× |
| ledger-reader | 16 | 1000–1600 | 3 | 15.75× |
| roster-auditor | 16 | 1000–1600 | 3 | 15.00× |
| spec-reviewer | 16 | 1000–1600 | 3 | 15.00× |

## Validation and limits

Generation asserts schema keys, unique IDs, valid ancestry, class counts, semantic tool repetition thresholds, chronological retry thresholds, provider ancestry on non-gap traces, and same-name cost baseline coverage. Text-based labels additionally depend on the explicit narratives documented above; these checks are corpus consistency checks, not a proposed detector. No detector code or earlier corpus was consulted. Fixed synthetic epoch timestamps and fictional work-item identifiers make regeneration portable.
