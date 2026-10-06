#!/usr/bin/env python3
"""Deterministic independent Round 2 trace corpus. Python standard library only."""
import collections
import json
import random
from pathlib import Path

SEED = 73190427
RNG = random.Random(SEED)
OUT = Path(__file__).resolve().parent
NAMES = ['archive-curator', 'harbor-planner', 'roster-auditor', 'spec-reviewer', 'ledger-reader', 'lab-coordinator']
PROVIDERS = [('https://api.openai.com/v1/responses', 'gpt-4.1'), ('https://api.anthropic.com/v1/messages', 'claude-sonnet-4'), ('https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent', 'gemini-2.5-flash'), ('https://api.openai.com/v1/chat/completions', 'gpt-4.1-mini')]

class Trace:
    def __init__(self, n, label, variant):
        self.n, self.label, self.variant = n, label, variant
        self.spans = []
        self.tick = 0
        self.url, self.model = PROVIDERS[n % len(PROVIDERS)]
        self.root = self.add('http.server' if n % 2 else 'workflow.dispatch', 'Process work item', None, {'work.item': f'W-{n:04d}'})
        self.agent = self.add('gen_ai.invoke_agent', 'Run assigned specialist', self.root, {'gen_ai.agent.name': NAMES[n % 6]})
    def add(self, op, description, parent, data=None, status='ok'):
        self.tick += 1
        sid = f'{self.n:08x}{self.tick:08x}'
        self.spans.append(dict(trace_id=f'{self.n + SEED:032x}', span_id=sid, parent_span_id=parent, op=op, description=description, status=status, start=1791200000.0 + self.n * 100 + self.tick * .61, data=data or {}))
        return sid
    def chat(self, text='The work item is complete.', finish='stop', parent=None, attempts=(200,), tokens=1000, encoding=None):
        data = {'gen_ai.request.model': self.model, 'gen_ai.usage.input_tokens': tokens * 4 // 5, 'gen_ai.usage.output_tokens': tokens - tokens * 4 // 5}
        mode = self.n % 3 if encoding is None else encoding
        if finish is not None:
            data['gen_ai.response.finish_reasons'] = [finish] if self.tick % 2 else json.dumps([finish])
        if text is not None:
            if mode == 0:
                data['gen_ai.response.text'] = json.dumps([text])
            elif mode == 1:
                data['gen_ai.output.messages'] = json.dumps([{'role': 'assistant', 'content': text}])
            else:
                data['gen_ai.output.messages'] = json.dumps([{'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}])
        sid = self.add('gen_ai.chat', 'Generate response', parent or self.agent, data)
        for code in attempts:
            self.add('http.client', 'POST ' + self.url, sid, {'http.response.status_code': code}, 'ok' if code == 200 else 'internal_error')
        return sid
    def tool(self, name, args, output='Completed successfully.', parent=None, status='ok', reverse=False):
        if reverse:
            args = dict(reversed(list(args.items())))
        key = 'gen_ai.tool.call.arguments' if self.tick % 2 else 'gen_ai.tool.input'
        result = 'gen_ai.tool.call.result' if self.tick % 3 else 'gen_ai.tool.output'
        return self.add('gen_ai.execute_tool', 'Execute ' + name, parent or self.agent, {'gen_ai.tool.name': name, key: json.dumps(args, separators=(',', ':') if reverse else None), result: output}, status)
    def child(self, name, parent=None):
        return self.add('gen_ai.invoke_agent', 'Delegate bounded subtask', parent or self.agent, {'gen_ai.agent.name': name})
    def finish(self):
        # Serialization order is deliberately independent of event time and ancestry.
        if self.n % 4 == 0:
            RNG.shuffle(self.spans)
        elif self.n % 4 == 1:
            self.spans = self.spans[::2] + self.spans[1::2]
        elif self.n % 4 == 2:
            self.spans.reverse()
        # Optional status can be absent in meaning (null), never erase actual errors.
        for span in self.spans:
            if span['status'] == 'ok' and RNG.random() < .13:
                span['status'] = None
        return {'label': self.label, 'variant': self.variant, 'spans': self.spans}

HEALTHY = {
 'converging_solver': 'Repeated solver calls change tolerance and initial estimates; no identical arguments.',
 'alternating_retry_windows': 'Four provider failures total, separated into pairs by successful attempts.',
 'rest_gateway_outage': 'Three 503s from a non-LLM inventory endpoint; final answer explicitly reports unavailability.',
 'quoted_error_manual': 'Successful tool retrieves documentation quoting an error JSON example; answer explains the example.',
 'error_budget_measurement': 'Tool successfully reports an error budget metric, not an execution error.',
 'recovered_tool_fallback': 'Primary tool times out; distinct fallback succeeds; final answer discloses the recovery.',
 'parallel_specialist_join': 'Sibling specialists answer independently and coordinator synthesizes after both finish.',
 'delegated_subtask_return': 'Agent-as-tool specialist returns a complete result before coordinator final answer.',
 'historical_limit_in_document': 'Retrieved documentation mentions max_tokens and tool_calls; actual final reason is end_turn.',
 'optional_finish_absent': 'Final answer is explicit and complete although optional finish reason is absent.',
 'terse_verdict': 'One-word complete answer with uppercase STOP finish reason.',
 'typed_argument_changes': 'Tool arguments differ by JSON type, array order, or explicit null versus omission.',
}
FAILURES = {
 'tool_loop': ['canonical_object_reordering', 'nested_object_reordering', 'intervening_probe', 'delegated_index_lock', 'array_payload_replay', 'empty_parameter_poll'],
 'retry_storm': ['overload_triplet', 'mixed_throttle_outage', 'messages_gateway_chain', 'delegated_retry_burst', 'cross_call_failure_chain', 'five_attempt_recovery'],
 'silent_tool_error': ['structured_denial_ignored', 'timeout_text_ignored', 'service_503_ignored', 'error_status_ignored', 'traceback_ignored', 'empty_success_claim_after_abort'],
 'lost_llm_span': ['direct_agent_responses', 'orphan_messages_request', 'custom_transport_wrapper', 'delegated_http_gap', 'one_of_two_calls_missing', 'gemini_stream_gap'],
 'dead_end': ['handoff_tool_request', 'fulfilled_tool_without_resume', 'unexecuted_dispatch', 'turn_budget_after_result', 'nested_request_abandoned', 'tool_use_without_dispatch'],
 'truncated_answer': ['cutoff_enumeration', 'cutoff_json', 'cutoff_sentence', 'delegated_report_limit', 'long_context_cutoff', 'capped_final_after_lookup'],
 'empty_answer': ['empty_text_item', 'empty_message_content', 'empty_content_block', 'zero_text_items', 'zero_output_messages', 'blank_final_after_evidence'],
 'cost_spike': ['oversized_archive_context', 'runaway_report_budget', 'retrieval_context_flood', 'unexpected_reasoning_bill', 'duplicated_source_bundle', 'expensive_final_synthesis'],
}

def healthy(t, v):
    if v == 'converging_solver':
        for k in range(4): t.tool('solve_equilibrium', {'tolerance': 10 ** (-k-2), 'estimate': 2 + k / 10}, 'Converged estimate available.')
    elif v == 'alternating_retry_windows':
        t.chat('First source checked.', attempts=(503, 502, 200), tokens=300)
        t.chat('Second source checked.', attempts=(500, 529, 200), tokens=300)
    elif v == 'rest_gateway_outage':
        for _ in range(3): t.add('http.client', 'GET https://inventory.example.net/v2/stock', t.agent, {'http.response.status_code': 503}, 'internal_error')
        t.chat('The inventory service is unavailable; I cannot confirm stock.'); return
    elif v == 'quoted_error_manual':
        t.tool('read_protocol_manual', {'chapter': 'negative-response-examples'}, 'Documentation excerpt (not a live result): {"error":"invalid signature"} is the documented rejection format.')
        t.chat('The manual uses an invalid-signature error as an example of rejection formatting.'); return
    elif v == 'error_budget_measurement':
        t.tool('fetch_slo', {'service': 'ingest'}, '{"query_success":true,"error_budget_remaining_percent":98.7}')
    elif v == 'recovered_tool_fallback':
        t.tool('read_replica', {'record': 'Q7'}, 'Timeout: replica did not respond.', status='internal_error')
        t.tool('read_primary', {'record': 'Q7'}, 'Record Q7: approved.')
        t.chat('The replica timed out. I verified Q7 against the primary instead: approved.'); return
    elif v == 'parallel_specialist_join':
        for name in ['format-checker', 'reference-checker']:
            a = t.child(name); t.chat('Checks passed.', parent=a, tokens=300)
    elif v == 'delegated_subtask_return':
        tool = t.tool('delegate_unit_check', {'document': 'D4'}, 'Unit check passed.')
        a = t.child('unit-checker', tool); t.chat('All units agree.', parent=a, tokens=300)
    elif v == 'historical_limit_in_document':
        t.tool('read_sdk_notes', {'section': 'finish reasons'}, 'Reference: max_tokens means a limit; tool_calls means a tool request.')
        t.chat('These are documented finish reasons.', finish='end_turn'); return
    elif v == 'optional_finish_absent':
        t.chat('All three checks passed.', finish=None); return
    elif v == 'terse_verdict':
        t.chat('Approved.', finish='STOP'); return
    elif v == 'typed_argument_changes':
        for args in [{'key': 7}, {'key': '7'}, {'key': 7, 'scope': None}, {'key': [1, 2]}, {'key': [2, 1]}]: t.tool('inspect_key', args)
    t.chat('The requested checks are complete and the results are ready.')

def failure(t, label, i):
    if label == 'tool_loop':
        p = t.child('index-repair-worker') if i == 3 else t.agent
        args = [{'collection': 'C9', 'mode': 'refresh'}, {'filter': {'owner': 'ops', 'active': True}, 'limit': 10}, {'lease': 'L2'}, {'index': 'I4'}, {'records': ['A', 'B'], 'commit': False}, {}][i]
        for k in range(4 if i == 5 else 3):
            t.chat(None, 'tool_calls', parent=p, tokens=150)
            a = dict(args)
            if i == 1 and k == 1: a['filter'] = {'active': True, 'owner': 'ops'}
            t.tool('refresh_index' if i != 5 else 'poll_default_queue', a, 'Pending; state unchanged.', parent=p, reverse=k % 2 == 1)
            if i == 2 and k == 1: t.tool('inspect_lease', {'lease': 'L2'}, 'Lease remains pending.', parent=p)
        t.chat('No progress was made; the same request kept returning pending.', parent=p, tokens=300)
        if p != t.agent: t.chat('The index repair made no progress.', tokens=300)
    elif label == 'retry_storm':
        if i == 2: t.url, t.model = PROVIDERS[1]
        p = t.child('request-worker') if i == 3 else t.agent
        if i == 4:
            t.chat(None, None, attempts=(502, 503), tokens=50)
            t.chat('Recovered response.', attempts=(429, 200), tokens=700)
        else: t.chat('The request eventually completed.', parent=p, attempts=[(503,503,503,200),(429,502,529,200),(500,502,503,200),(529,529,529,200),(),(429,503,502,500,529,200)][i])
        if p != t.agent: t.chat('The delegated request completed.', tokens=300)
    elif label == 'silent_tool_error':
        outputs = ['{"ok":false,"error":{"code":"ACCESS_DENIED","message":"Cannot read audit record"}}', 'Timeout while reading audit record; no data returned.', 'HTTP 503 Service Unavailable: audit backend offline.', 'Audit fetch did not complete.', 'Traceback (most recent call last):\n  audit.fetch()\nConnectionError: connection reset', '{"status":"aborted","error":"transaction cancelled","records":null}']
        t.chat(None, 'tool_use', tokens=150)
        t.tool('fetch_audit_record', {'record': 'A93'}, outputs[i], status='internal_error' if i in (3,5) else 'ok')
        t.chat('I verified audit record A93. It is approved and has no outstanding findings.', tokens=700)
    elif label == 'lost_llm_span':
        if i in (1,5): t.url, t.model = PROVIDERS[1 if i == 1 else 2]
        p = t.agent
        if i == 1: p = t.root
        if i == 2: p = t.add('provider.transport', 'Uninstrumented completion transport', t.agent)
        if i == 3: p = t.child('source-summarizer')
        if i == 4: t.chat('First source summarized.', tokens=300)
        t.add('http.client', 'POST ' + t.url, p, {'http.response.status_code': 200, 'response.body': '{"text":"Summary complete."}'})
        t.add('workflow.deliver', 'Deliver completed provider answer', t.agent, {'answer': 'Summary complete.'})
    elif label == 'dead_end':
        p = t.agent
        if i == 0:
            t.add('gen_ai.handoff', 'Transfer to dispatch worker', t.agent, {'gen_ai.agent.name': 'dispatch-worker'})
            p = t.child('dispatch-worker')
        if i == 4:
            wrapper = t.tool('delegate_manifest', {'manifest': 'M7'}, 'Delegation started; no answer yet.')
            p = t.child('manifest-worker', wrapper)
        t.chat(None, 'tool_use' if i in (0,5) else 'tool_calls', parent=p)
        if i in (1,3): t.tool('read_manifest', {'manifest': 'M7'}, 'Manifest contains 12 entries.', parent=p)
        t.add('workflow.stop', 'Turn budget exhausted before final answer', t.root, {'reason': 'max_turns'})
    elif label == 'truncated_answer':
        texts = ['1. Check the seal.\n2. Inspect the', '{"approved":true,"items":[', 'The remaining required action is to', 'The delegated report identifies three issues, namely', 'Based on the supplied reference material, the conclusion is', 'The lookup returned a record whose owner is']
        if i == 3:
            a = t.child('report-worker'); t.chat('Evidence reviewed.', parent=a, tokens=250)
        if i == 5: t.tool('lookup_owner', {'record': 'R8'}, 'Owner: Logistics')
        t.chat(texts[i], 'length' if i % 2 else 'max_tokens')
    elif label == 'empty_answer':
        if i == 5: t.tool('read_evidence', {'record': 'E2'}, 'Evidence: valid.')
        sid = t.chat('', 'end_turn', encoding=i % 3)
        d = next(s['data'] for s in t.spans if s['span_id'] == sid)
        if i in (3,4):
            d.pop('gen_ai.response.text', None); d.pop('gen_ai.output.messages', None)
            d['gen_ai.response.text' if i == 3 else 'gen_ai.output.messages'] = '[]'
    elif label == 'cost_spike':
        if i in (2,4): t.tool('load_reference_bundle', {'bundle': f'B{i}'}, 'Reference bundle retrieved successfully.')
        t.chat('The complete report is ready; all requested points are addressed.', tokens=24000 + i * 1200)


def validate(rows):
    assert len(rows) == 240
    counts = collections.Counter(r['label'] for r in rows)
    assert counts['healthy'] == 96 and all(counts[k] == 18 for k in FAILURES)
    totals = collections.defaultdict(list)
    for row in rows:
        spans = row['spans']; ids = {s['span_id'] for s in spans}
        assert len(ids) == len(spans)
        assert len({s['trace_id'] for s in spans}) == 1
        assert all(s['parent_span_id'] is None or s['parent_span_id'] in ids for s in spans)
        agents = {s['span_id']: s for s in spans if s['op'] == 'gen_ai.invoke_agent'}
        top = next(s for s in agents.values() if s['data']['gen_ai.agent.name'] in NAMES)
        usage = sum(s['data'].get('gen_ai.usage.input_tokens', 0) + s['data'].get('gen_ai.usage.output_tokens', 0) for s in spans if s['op'] == 'gen_ai.chat')
        totals[top['data']['gen_ai.agent.name']].append((row['label'], usage))
        calls = collections.Counter()
        for s in spans:
            assert set(s) == {'trace_id','span_id','parent_span_id','op','description','status','start','data'}
            if s['op'] == 'gen_ai.execute_tool':
                d = s['data']; args = json.loads(d.get('gen_ai.tool.call.arguments', d.get('gen_ai.tool.input')))
                calls[(s['parent_span_id'], d['gen_ai.tool.name'], json.dumps(args, sort_keys=True))] += 1
        assert (max(calls.values(), default=0) >= 3) == (row['label'] == 'tool_loop')
        streak = max_streak = gaps = 0
        for s in sorted(spans, key=lambda s:s['start']):
            if s['op'] == 'http.client' and any(url in s['description'] for url, _ in PROVIDERS):
                code = s['data']['http.response.status_code']
                streak = streak + 1 if code == 429 or 500 <= code < 600 else 0
                max_streak = max(max_streak, streak)
                p = s['parent_span_id']; ancestors = []
                while p:
                    ancestor = next(x for x in spans if x['span_id'] == p); ancestors.append(ancestor['op']); p = ancestor['parent_span_id']
                if code == 200 and 'gen_ai.chat' not in ancestors: gaps += 1
        assert (max_streak >= 3) == (row['label'] == 'retry_storm')
        assert (gaps > 0) == (row['label'] == 'lost_llm_span')
        chats = sorted((s for s in spans if s['op'] == 'gen_ai.chat'), key=lambda s:s['start'])
        if chats:
            data = chats[-1]['data']
            reasons = data.get('gen_ai.response.finish_reasons', [])
            if isinstance(reasons, str): reasons = json.loads(reasons)
            reasons = [r.lower() for r in reasons]
            assert any(r in ('length', 'max_tokens') for r in reasons) == (row['label'] == 'truncated_answer')
            assert any(r in ('tool_calls', 'tool_use') for r in reasons) == (row['label'] == 'dead_end')
            texts = None
            if 'gen_ai.response.text' in data:
                texts = json.loads(data['gen_ai.response.text'])
            elif 'gen_ai.output.messages' in data:
                texts = []
                for message in json.loads(data['gen_ai.output.messages']):
                    content = message['content']
                    texts.extend([content] if isinstance(content, str) else [b['text'] for b in content])
            if texts is not None:
                assert (not ''.join(texts)) == (row['label'] == 'empty_answer')
        if row['label'] == 'lost_llm_span':
            assert any(s['op'] == 'workflow.deliver' for s in spans)
    for name, entries in totals.items():
        normal = [u for label,u in entries if label == 'healthy']
        assert len(normal) >= 6
        assert all(u >= 8 * max(normal) for label,u in entries if label == 'cost_spike'), name
    return counts, totals


def main():
    RNG.seed(SEED)
    rows = []
    for v in HEALTHY:
        for _ in range(8):
            t = Trace(len(rows)+1, 'healthy', v); healthy(t,v); rows.append(t.finish())
    for label, variants in FAILURES.items():
        for i,v in enumerate(variants):
            for _ in range(3):
                t = Trace(len(rows)+1,label,v); failure(t,label,i); rows.append(t.finish())
    counts, totals = validate(rows)
    RNG.shuffle(rows)
    (OUT/'heldout2.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False, separators=(',',':'))+'\n' for r in rows))
    notes = ['# Independent Round 2 held-out corpus', '', f'Seed: `{SEED}`. Regenerate with `python3 gen.py` (standard library only).', '', '240 traces: 96 healthy (40%) and 18 for each of eight failure classes. Round 2 overrides the earlier filename: the dataset is `heldout2.jsonl`.', '', 'Each variant has its own construction; replicas vary provider, agent name, attribute spelling, encoding, status nullability, and serialization order. Event chronology is given by start, not list position. Provider children occur after their chat span start. Unknown finish reasons are not inferred from text. Empty answers contain explicitly empty output, not missing output telemetry.', '', 'Successful provider requests are nested under their matching chat except in instrumentation-gap cases. Failed attempts are counted in timestamp order, across calls when applicable. Non-provider outages are excluded. Tool argument equality uses parsed JSON objects, preserving types and array order. Pending tool results in loops are successful pending responses, not tool errors. Silent-error final answers explicitly claim unsupported success. Recovery cases disclose the original failure.', '', 'Cost baselines use the same top-level agent name, summing chat usage once across its run. Each agent has 16 healthy runs. All spikes exceed eight times even the largest healthy run for their agent. Specialist names have no spikes. No invoke-agent usage totals duplicate chat totals.', '', '## Counts and variants', '', '| Label | Variant | Count |', '|---|---|---:|']
    c = collections.Counter((r['label'],r['variant']) for r in rows)
    notes += [f'| {label} | {variant} | {count} |' for (label,variant),count in sorted(c.items())]
    notes += ['', '## Healthy case interpretation', ''] + [f'- `{v}`: {why}' for v,why in HEALTHY.items()]
    notes += ['', '## Failure case interpretation', '', '- Tool loops replay the same parsed arguments at least three times within one agent, including nested object key permutations and an unrelated interleaved probe.', '- Retry storms contain three to five consecutive provider failures followed by recovery. A recovered storm remains a storm.', '- Silent tool failures use explicit structured denial, timeout, HTTP failure, span error status, traceback, or aborted transaction; the final answer claims verification succeeded.', '- Missing LLM spans include direct, orphaned, transport-wrapped, delegated, partial, and Gemini provider gaps. Delivery events document a completed answer without fabricating a chat span.', '- Dead ends stop after a tool request, sometimes after a successful tool result, and never resume to a final answer. Delegation acknowledgements are not final answers.', '- Truncations have visibly incomplete text and final length/max_tokens finish reasons.', '- Empty final answers use empty text values or empty output arrays with a normal termination reason.', '- Cost spikes are otherwise successful, complete runs with 24,000–30,000 tokens.', '', '## Baseline audit', '', '| Agent | Healthy runs | Healthy token range | Spike runs | Minimum spike / maximum healthy |', '|---|---:|---:|---:|---:|']
    for name, entries in sorted(totals.items()):
        normal=[u for l,u in entries if l=='healthy']; spikes=[u for l,u in entries if l=='cost_spike']
        notes.append(f'| {name} | {len(normal)} | {min(normal)}–{max(normal)} | {len(spikes)} | {min(spikes)/max(normal):.2f}× |')
    notes += ['', '## Validation and limits', '', 'Generation asserts schema keys, unique IDs, valid ancestry, class counts, semantic tool repetition thresholds, chronological retry thresholds, provider ancestry on non-gap traces, and same-name cost baseline coverage. Text-based labels additionally depend on the explicit narratives documented above; these checks are corpus consistency checks, not a proposed detector. No detector code or earlier corpus was consulted. Fixed synthetic epoch timestamps and fictional work-item identifiers make regeneration portable.', '']
    (OUT/'NOTES.md').write_text('\n'.join(notes))
    print(json.dumps(dict(counts), sort_keys=True))

if __name__ == '__main__':
    main()
