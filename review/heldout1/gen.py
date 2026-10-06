#!/usr/bin/env python3
"""Generate an independent, deterministic adversarial trace corpus (stdlib only)."""
import json
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path

SEED = 731902
AGENTS = ['inventory_assistant', 'travel.concierge', 'Support/Triage',
          'research-worker-v2', 'billing_helper', 'Workspace Analyst']
PROVIDERS = [
    'https://api.openai.com/v1/chat/completions',
    'https://api.openai.com/v1/responses',
    'https://api.anthropic.com/v1/messages',
    'https://generativelanguage.googleapis.com/v1beta/models/gemini:generateContent',
]
HEALTHY = [
    'pagination', 'two_identical_calls', 'one_retry', 'two_retries',
    'rate_limit_recovered', 'zero_errors', 'error_rate_metric',
    'acknowledged_failure', 'three_times_tokens', 'short_complete',
    'non_llm_http', 'end_turn', 'uppercase_stop', 'handoff',
    'agent_as_tool', 'same_args_different_agents', 'intermediate_length',
    'intermediate_empty', 'tool_request_resolved', 'optional_attributes_missing',
    'interleaved_agents', 'different_tool_same_args', 'two_failed_non_provider_calls',
    'separated_provider_failures',
]
FAILURES = {
    'tool_loop': ['exact_arguments', 'reordered_argument_keys', 'input_alias',
                  'intervening_progress', 'nested_agent_loop', 'four_repeats'],
    'retry_storm': ['three_5xx', 'three_429', 'mixed_failures',
                    'across_llm_calls', 'nested_provider_retries', 'four_then_success'],
    'silent_tool_error': ['status_only', 'exception_output', 'error_json',
                          'timeout_output', 'http_503_output', 'nested_tool_failure'],
    'lost_llm_span': ['orphan_chat_completions', 'orphan_responses',
                      'orphan_messages', 'orphan_generate_content',
                      'one_of_two_unmatched', 'nested_orphan'],
    'dead_end': ['tool_calls_unexecuted', 'tool_use_unexecuted',
                 'executed_without_followup', 'max_turns',
                 'multiple_tools_requested', 'nested_unfinished'],
    'truncated_answer': ['length_string', 'length_list', 'max_tokens_string',
                         'max_tokens_list', 'nested_final', 'partial_json'],
    'empty_answer': ['empty_text_list', 'empty_message_content', 'whitespace_only',
                     'empty_output_list', 'empty_content_blocks', 'nested_empty'],
    'cost_spike': ['large_input', 'large_output', 'balanced_usage',
                   'multiple_calls', 'root_totals_omitted', 'root_and_chat_totals'],
}


def packed(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False)


class Trace:
    def __init__(self, index, label, variant, replica):
        self.index, self.label, self.variant, self.replica = index, label, variant, replica
        self.rng = random.Random(SEED + index)
        self.spans = []
        self.base = 1780272000.0 + index * 600
        self.root = self.add('http.server' if replica % 2 else 'workflow.run',
                             'POST /assistant/run', None, {})
        self.agent = self.new_agent(self.root, AGENTS[index % len(AGENTS)])
        self.focus = self.agent

    def add(self, op, description, parent, data, status='ok'):
        span = dict(trace_id=f'{self.index + 1:032x}', span_id=f'{len(self.spans)+1:016x}',
                    parent_span_id=parent, op=op, description=description, status=status,
                    start=round(self.base + len(self.spans) * .137, 6), data=data)
        self.spans.append(span)
        return span['span_id']

    def new_agent(self, parent, name):
        return self.add('gen_ai.invoke_agent', 'Execute assistant workflow', parent,
                        {'gen_ai.agent.name': name})

    def nested(self):
        self.chat('tool_calls', 'Delegating the lookup.', request=True)
        wrapper = self.tool('delegate_lookup', {'topic': 'stock'}, 'Worker finished.')
        self.focus = self.new_agent(wrapper, 'lookup.specialist')

    def http(self, parent, code=200, provider=None):
        url = PROVIDERS[self.replica % 4] if provider is None else provider
        return self.add('http.client', 'POST ' + url, parent,
                        {'http.response.status_code': code},
                        'internal_error' if code >= 400 else ('ok' if self.replica % 2 else None))

    def chat(self, reason='stop', answer='There are 12 units available.', request=False,
             attempts=None, omit=False):
        data = {'gen_ai.request.model': ['gpt-4.1-mini', 'gpt-4.1', 'claude-sonnet-4', 'gemini-2.5-flash'][self.replica % 4]}
        if not omit:
            data['gen_ai.response.finish_reasons'] = [reason] if self.replica % 2 else packed([reason])
        if self.replica % 2:
            data['gen_ai.output.messages'] = packed([{'role': 'assistant', 'content': answer}])
        else:
            data['gen_ai.response.text'] = packed([answer])
        if request:
            data['gen_ai.response.tool_calls'] = packed([{'name': 'lookup_stock', 'arguments': {'sku': 'A-17'}}])
        sid = self.add('gen_ai.chat', 'Generate assistant response', self.focus, data)
        for code in (attempts if attempts is not None else [200]):
            self.http(sid, code)
        return sid

    def tool(self, name='lookup_stock', args=None, output='{"available":12}', status='ok', alias=False):
        # Bind the preceding request to this actual execution, including delegation.
        for previous in reversed(self.spans):
            if previous['op'] == 'gen_ai.chat' and previous['parent_span_id'] == self.focus:
                if 'gen_ai.response.tool_calls' in previous['data']:
                    previous['data']['gen_ai.response.tool_calls'] = packed([
                        {'name': name, 'arguments': args or {}}])
                break
        data = {'gen_ai.tool.name': name}
        if args is not None:
            # Semantic JSON identity is intentional, including differently ordered nested keys.
            keys = list(args)
            if len(self.spans) % 2:
                keys.reverse()
            ordered = {k: args[k] for k in keys}
            data['gen_ai.tool.input' if alias or self.replica % 2 else 'gen_ai.tool.call.arguments'] = json.dumps(ordered, indent=None if len(self.spans) % 2 else 1)
        if output is not None:
            data['gen_ai.tool.output' if alias or self.replica % 2 else 'gen_ai.tool.call.result'] = output
        return self.add('gen_ai.execute_tool', 'Run ' + name, self.focus, data, status)

    def final(self, answer='There are 12 units available.', reason='stop'):
        return self.chat(reason, answer)

    def finish(self):
        # Every invocation has honest inclusive totals. Sum chats, never root + chats.
        chats = [s for s in self.spans if s['op'] == 'gen_ai.chat']
        total = 24000 if self.label == 'cost_spike' else (3000 if self.variant == 'three_times_tokens' else 1000)
        for i, s in enumerate(chats):
            allocation = total // len(chats) + (i < total % len(chats))
            ratio = .10 if self.variant == 'large_output' else (.5 if self.variant == 'balanced_usage' else .9)
            inp = int(allocation * ratio)
            s['data'].update({'gen_ai.usage.input_tokens': inp, 'gen_ai.usage.output_tokens': allocation-inp})
        by_id = {s['span_id']: s for s in self.spans}
        def under(s, ancestor):
            parent = s['parent_span_id']
            while parent:
                if parent == ancestor:
                    return True
                parent = by_id[parent]['parent_span_id']
            return False
        for s in self.spans:
            if s['op'] == 'gen_ai.invoke_agent' and self.variant not in ('root_totals_omitted', 'optional_attributes_missing'):
                for key in ('input_tokens', 'output_tokens'):
                    attr = 'gen_ai.usage.' + key
                    s['data'][attr] = sum(c['data'][attr] for c in chats if under(c, s['span_id']))
        # Export order does not define causality. Original timestamps and parents do.
        if self.replica % 3 == 1:
            self.rng.shuffle(self.spans)
        elif self.replica % 3 == 2:
            self.spans.reverse()
        return {'label': self.label, 'variant': self.variant, 'spans': self.spans}


def healthy(t):
    v = t.variant
    if v in ('pagination', 'two_identical_calls', 'zero_errors', 'error_rate_metric', 'acknowledged_failure', 'different_tool_same_args'):
        n = 4 if v == 'pagination' else (2 if v in ('two_identical_calls', 'different_tool_same_args') else 1)
        for i in range(n):
            t.chat('tool_calls', '', request=True)
            output = '0 errors; available=12' if v == 'zero_errors' else ('error rate is 0.1%; available=12' if v == 'error_rate_metric' else '{"available":12}')
            if v == 'acknowledged_failure':
                output = 'TimeoutError: inventory service did not respond'
            t.tool('lookup_stock' if v != 'different_tool_same_args' or i == 0 else 'lookup_reservations',
                   {'page': i+1} if v == 'pagination' else {'sku': 'A-17'}, output,
                   'internal_error' if v == 'acknowledged_failure' else 'ok')
        t.final('The inventory lookup timed out, so I cannot confirm availability. Please retry later.' if v == 'acknowledged_failure' else 'There are 12 units available.')
    elif v in ('one_retry', 'two_retries', 'rate_limit_recovered'):
        t.chat(attempts={'one_retry': [503, 200], 'two_retries': [502, 503, 200], 'rate_limit_recovered': [429, 200]}[v])
    elif v == 'separated_provider_failures':
        for _ in range(3):
            t.chat(answer='Checking one more source.', attempts=[503, 200])
        t.final()
    elif v in ('non_llm_http', 'two_failed_non_provider_calls'):
        for _ in range(2 if v == 'two_failed_non_provider_calls' else 1):
            t.http(t.focus, 503 if v == 'two_failed_non_provider_calls' else 200, 'https://weather.example.net/v1/forecast')
        t.final('The weather service is unavailable; I cannot provide a forecast.' if v == 'two_failed_non_provider_calls' else 'The forecast is sunny.')
    elif v in ('handoff', 'agent_as_tool', 'same_args_different_agents', 'interleaved_agents'):
        outer = t.focus
        if v == 'agent_as_tool':
            t.nested()
            t.final()
        else:
            handoff = t.add('gen_ai.handoff', 'Transfer to inventory specialist', outer, {'gen_ai.handoff.target': 'lookup.specialist'})
            child = t.new_agent(handoff, 'lookup.specialist')
            if v == 'same_args_different_agents':
                for owner in (outer, child):
                    t.focus = owner
                    for _ in range(2):
                        t.chat('tool_calls', '', request=True)
                        t.tool(args={'sku': 'A-17'})
                    t.final()
            elif v == 'interleaved_agents':
                for owner in (outer, child, outer, child):
                    t.focus = owner
                    t.chat(answer='Cross-checking stock.')
                t.final()
            else:
                t.focus = child
                t.final()
        t.focus = outer
        t.final()
    elif v in ('intermediate_length', 'intermediate_empty', 'tool_request_resolved'):
        t.chat('length' if v == 'intermediate_length' else ('tool_use' if v == 'tool_request_resolved' else 'stop'),
               'The available stock is' if v == 'intermediate_length' else '', request=v == 'tool_request_resolved')
        if v == 'tool_request_resolved':
            t.tool(args={'sku': 'A-17'})
        t.final()
    else:
        t.chat(reason={'end_turn': 'end_turn', 'uppercase_stop': 'STOP'}.get(v, 'stop'),
               answer='Yes.' if v == 'short_complete' else 'There are 12 units available.',
               omit=v == 'optional_attributes_missing')


def failing(t):
    v, label = t.variant, t.label
    if label == 'tool_loop':
        if v == 'nested_agent_loop':
            t.nested()
        for i in range(4 if v == 'four_repeats' else 3):
            t.chat('tool_use' if t.replica % 2 else 'tool_calls', '', request=True)
            t.tool(args={'sku': 'A-17', 'warehouse': 'east'}, alias=v == 'input_alias')
            if v == 'intervening_progress':
                t.chat(answer='I will recheck that same inventory entry.')
                t.tool('lookup_location', {'location_id': i}, 'East warehouse')
        t.final()
        if v == 'nested_agent_loop':
            t.focus = t.agent
            t.final()
    elif label == 'retry_storm':
        if v == 'nested_provider_retries':
            t.nested()
        codes = [429]*3 if v == 'three_429' else ([429, 502, 503] if v == 'mixed_failures' else [503]* (4 if v == 'four_then_success' else 3))
        if v == 'across_llm_calls':
            for code in codes:
                sid = t.chat(answer='', attempts=[code], omit=True)
                next(s for s in t.spans if s['span_id'] == sid)['status'] = 'internal_error'
            t.final()
        else:
            t.chat(attempts=codes+[200])
        if v == 'nested_provider_retries':
            t.focus = t.agent
            t.final()
    elif label == 'silent_tool_error':
        if v == 'nested_tool_failure':
            t.nested()
        t.chat('tool_calls', '', request=True)
        outputs = {'status_only': None, 'exception_output': 'RuntimeError: inventory database disconnected',
                   'error_json': '{"error":{"code":"DB_UNAVAILABLE","message":"lookup failed"}}',
                   'timeout_output': 'Request timed out after 30 seconds; no inventory data returned.',
                   'http_503_output': 'HTTP 503 Service Unavailable: inventory lookup failed',
                   'nested_tool_failure': 'ConnectionError: inventory backend unreachable'}
        t.tool(args={'sku': 'A-17'}, output=outputs[v], status='internal_error' if v == 'status_only' else 'ok')
        t.final('I checked the live inventory successfully: 12 units are available for purchase.')
        if v == 'nested_tool_failure':
            t.focus = t.agent
            t.final('The live inventory check succeeded. You can purchase 12 units.')
    elif label == 'lost_llm_span':
        if v == 'nested_orphan':
            t.nested()
        if v == 'one_of_two_unmatched':
            t.chat(answer='Checking a second inventory source.')
        endpoint = FAILURES[label].index(v) if v.startswith('orphan_') else t.replica
        t.http(t.focus, 200, PROVIDERS[endpoint % 4])
        t.final()
        if v == 'nested_orphan':
            t.focus = t.agent
            t.final()
    elif label == 'dead_end':
        if v == 'nested_unfinished':
            t.nested()
            # The delegating tool is still open: it has no completed result.
            wrapper = next(s for s in t.spans if s['op'] == 'gen_ai.execute_tool')
            wrapper['data'].pop('gen_ai.tool.call.result', None)
            wrapper['data'].pop('gen_ai.tool.output', None)
            wrapper['status'] = None
        sid = t.chat('tool_use' if v == 'tool_use_unexecuted' else 'tool_calls', '', request=True)
        if v == 'executed_without_followup':
            t.tool(args={'sku': 'A-17'})
        if v == 'max_turns':
            t.add('agent.lifecycle', 'Run stopped: maximum turns reached', t.focus, {'stop_reason': 'max_turns'})
        if v == 'multiple_tools_requested':
            next(s for s in t.spans if s['span_id'] == sid)['data']['gen_ai.response.tool_calls'] = packed([
                {'name': 'lookup_stock', 'arguments': {'sku': 'A-17'}},
                {'name': 'lookup_price', 'arguments': {'sku': 'A-17'}}])
    elif label == 'truncated_answer':
        if v == 'nested_final':
            t.nested()
            t.final()
            t.focus = t.agent
        reason = 'max_tokens' if v.startswith('max_tokens') else 'length'
        sid = t.final('{"available":12,"warehouses":[' if v == 'partial_json' else 'There are 12 units available, distributed among the following', reason)
        d = next(s for s in t.spans if s['span_id'] == sid)['data']
        d['gen_ai.response.finish_reasons'] = [reason] if v.endswith('_list') else packed([reason])
    elif label == 'empty_answer':
        if v == 'nested_empty':
            t.nested()
            t.final()
            t.focus = t.agent
        sid = t.final('')
        d = next(s for s in t.spans if s['span_id'] == sid)['data']
        d.pop('gen_ai.response.text', None)
        d.pop('gen_ai.output.messages', None)
        if v in ('empty_message_content', 'empty_content_blocks', 'nested_empty'):
            d['gen_ai.output.messages'] = packed([{'role': 'assistant', 'content': [] if v == 'empty_content_blocks' else ''}])
        else:
            d['gen_ai.response.text'] = packed([] if v == 'empty_output_list' else [' \n\t ' if v == 'whitespace_only' else ''])
    elif label == 'cost_spike':
        if v == 'multiple_calls':
            for page in range(3):
                t.chat('tool_calls', '', request=True)
                t.tool('read_document_page', {'page': page}, 'Long reference document excerpt.')
        t.final('The report is complete: 12 units are available.')


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


def inspect(row):
    """Semantic checks on serialized data, independent of scenario selection."""
    spans = sorted(row['spans'], key=lambda s: s['start'])
    by_id = {s['span_id']: s for s in spans}
    assert len(by_id) == len(spans)
    assert len({s['trace_id'] for s in spans}) == 1
    required = {'trace_id', 'span_id', 'parent_span_id', 'op', 'description', 'status', 'start', 'data'}
    for s in spans:
        assert set(s) == required and s['status'] in ('ok', 'internal_error', None)
        assert isinstance(s['start'], float) and isinstance(s['data'], dict)
        assert s['parent_span_id'] is None or s['parent_span_id'] in by_id
    def ancestors(s):
        seen = set()
        while s['parent_span_id']:
            s = by_id[s['parent_span_id']]
            assert s['span_id'] not in seen
            seen.add(s['span_id'])
            yield s
    def owner(s):
        return next(a['span_id'] for a in ancestors(s) if a['op'] == 'gen_ai.invoke_agent')
    chats = [s for s in spans if s['op'] == 'gen_ai.chat']
    flags = set()
    streak = 0
    for s in spans:
        if s['op'] == 'http.client' and any(url in s['description'] for url in PROVIDERS):
            code = s['data']['http.response.status_code']
            streak = streak + 1 if code == 429 or 500 <= code < 600 else 0
            if streak >= 3:
                flags.add('retry_storm')
            if 200 <= code < 300 and not any(a['op'] == 'gen_ai.chat' for a in ancestors(s)):
                flags.add('lost_llm_span')
    calls = Counter()
    bad_tools = []
    for s in spans:
        if s['op'] != 'gen_ai.execute_tool':
            continue
        d = s['data']
        args = d.get('gen_ai.tool.call.arguments', d.get('gen_ai.tool.input'))
        if args is not None:
            calls[(owner(s), d['gen_ai.tool.name'], json.dumps(decode(args), sort_keys=True))] += 1
        out = d.get('gen_ai.tool.call.result', d.get('gen_ai.tool.output', ''))
        if s['status'] == 'internal_error' or any(x in out for x in ('RuntimeError:', 'ConnectionError:', 'TimeoutError:', '"error":', 'timed out', 'HTTP 503')):
            bad_tools.append(s)
    if calls and max(calls.values()) >= 3:
        flags.add('tool_loop')
    last = chats[-1]['data']
    reason = decode(last.get('gen_ai.response.finish_reasons', '[]'))
    if any(r.lower() in ('tool_calls', 'tool_use') for r in reason):
        flags.add('dead_end')
    elif any(r.lower() in ('length', 'max_tokens') for r in reason):
        flags.add('truncated_answer')
    else:
        texts = decode(last['gen_ai.response.text']) if 'gen_ai.response.text' in last else [m['content'] for m in decode(last['gen_ai.output.messages'])]
        if not any(str(x).strip() for x in texts if x != []):
            flags.add('empty_answer')
    if bad_tools:
        answer = last.get('gen_ai.response.text', last.get('gen_ai.output.messages', ''))
        acknowledged = 'cannot confirm availability' in answer and 'timed out' in answer
        if not acknowledged:
            flags.add('silent_tool_error')
    total = sum(s['data']['gen_ai.usage.input_tokens'] + s['data']['gen_ai.usage.output_tokens'] for s in chats)
    primary = next(s for s in spans if s['op'] == 'gen_ai.invoke_agent')
    for s in spans:
        if s['op'] == 'gen_ai.invoke_agent' and 'gen_ai.usage.input_tokens' in s['data']:
            descendants = [c for c in chats if s in list(ancestors(c))]
            for key in ('input_tokens', 'output_tokens'):
                attr = 'gen_ai.usage.' + key
                assert s['data'][attr] == sum(c['data'][attr] for c in descendants)
    return flags, primary['data']['gen_ai.agent.name'], total


def validate(rows):
    assert len(rows) == 240
    assert Counter(r['label'] for r in rows) == Counter({'healthy': 96, **{k: 18 for k in FAILURES}})
    results = [inspect(r) for r in rows]
    normals = defaultdict(list)
    for row, (_, name, total) in zip(rows, results):
        if row['label'] == 'healthy':
            normals[name].append(total)
    for row, (flags, name, total) in zip(rows, results):
        assert len(normals[name]) >= 6
        typical = statistics.median(normals[name])
        if total >= 8 * typical:
            flags.add('cost_spike')
        expected = set() if row['label'] == 'healthy' else {row['label']}
        assert flags == expected, (row['label'], row['variant'], flags)
    return normals


def main():
    rows = []
    for label, variants, repetitions in [('healthy', HEALTHY, 4)] + [(k, v, 3) for k, v in FAILURES.items()]:
        for variant in variants:
            for replica in range(repetitions):
                t = Trace(len(rows), label, variant, replica)
                (healthy if label == 'healthy' else failing)(t)
                rows.append(t.finish())
    # Decouple cost cohorts from variant/index arithmetic: every agent gets 3 spikes.
    spikes = [r for r in rows if r['label'] == 'cost_spike']
    for i, row in enumerate(spikes):
        primary = min((s for s in row['spans'] if s['op'] == 'gen_ai.invoke_agent'), key=lambda s: s['start'])
        primary['data']['gen_ai.agent.name'] = AGENTS[i % 6]
    random.Random(SEED).shuffle(rows)
    normals = validate(rows)
    target = Path(__file__).resolve().parent
    serialized = ''.join(packed(r) + '\n' for r in rows)
    validate([json.loads(line) for line in serialized.splitlines()])
    (target / 'heldout.jsonl').write_text(serialized, encoding='utf-8')
    counts = Counter((r['label'], r['variant']) for r in rows)
    notes = '''# Independent held-out trace set

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
'''
    for label, count in sorted(Counter(r['label'] for r in rows).items()):
        notes += f'| {label} | {count} |\n'
    notes += '\n## Every variant and count per label\n\n| Label | Variant | Count |\n|---|---|---:|\n'
    for (label, variant), count in sorted(counts.items()):
        notes += f'| {label} | {variant} | {count} |\n'
    notes += '\n## Cost baselines\n\n| Agent name | Healthy runs | Median tokens | Spike runs | Spike tokens |\n|---|---:|---:|---:|---:|\n'
    for name in AGENTS:
        notes += f'| {name} | {len(normals[name])} | {statistics.median(normals[name]):.0f} | 3 | 24000 |\n'
    (target / 'NOTES.md').write_text(notes, encoding='utf-8')
    print('Validated and wrote 240 traces, 72 variants, and NOTES.md.')


if __name__ == '__main__':
    main()
