"""Assert only properties observed in captured transport output, not SDK internals."""
import json
from pathlib import Path
D=json.JSONDecoder()
def py(id,control=False):
 s=Path(('control_' if control else 'result_')+'SP-'+id+'.json').read_text()
 return [D.raw_decode(part)[0] for part in s.split('SPANS ')[1:]]
def js(id,control=False):
 s=Path(('control_' if control else 'result_')+'JS-SP-'+id+'.json').read_text()
 env=json.loads(s.split('ENVELOPES ')[1])
 return [x for e in env for h,v in e[1] if h['type']=='span' for x in v['items']]
def data(x):return x['data'] if 'data' in x else {k:v['value'] for k,v in x['attributes'].items()}
def usage(x):return {k:v for k,v in data(x).items() if k.startswith('gen_ai.usage.')}
def inp(x):return data(x).get('gen_ai.usage.input_tokens',0)
def op(x):return x.get('op') or data(x).get('sentry.op')
def absent_details(x):return not any('cached' in k or 'cache' in k or 'reasoning' in k for k in usage(x))
a=py('01')[0];assert len(a)==1 and absent_details(a[0]) and inp(a[0])==1200
assert py('02')[0]==[] and len(py('02',True)[0])==2
assert py('03')[0]==[] and len(py('03',True)[0])==1
for x in [py('04')[0][0],js('04')[0]]:
 assert not any('cache_write' in k or 'cache_creation' in k for k in usage(x))
a=py('05')[0];assert usage(a[0])=={} and inp(a[1])==2600
a=py('06')[0];assert sum(map(inp,a))==600
assert [inp(x) for x in a if op(x)=='gen_ai.invoke_agent']==[300]
a=py('07')[0];assert [inp(x) for x in a if op(x)=='gen_ai.invoke_agent']==[600,300]
assert [inp(x) for x in a if op(x)=='gen_ai.chat']==[100,200,300]
a,b=py('08');assert sum(op(x)=='gen_ai.invoke_agent' for x in a)==1 and not any(op(x)=='gen_ai.invoke_agent' for x in b)
a,b=py('09');assert [op(x) for x in a]==['gen_ai.invoke_agent','gen_ai.chat','gen_ai.execute_tool','gen_ai.chat'] and [op(x) for x in b]==['gen_ai.chat','gen_ai.chat']
a=py('10')[0];assert data(a[0])['gen_ai.response.finish_reasons']==['end_turn'] and 'gen_ai.response.finish_reasons' not in data(a[1])
a=js('11');assert absent_details(a[0]) and inp(a[0])==1200
a=js('12');assert inp(a[0])==40 and data(a[0])['gen_ai.usage.total_tokens']==2720 and absent_details(a[0])
a=js('13');ids={x['span_id'] for x in a};missing=[x for x in a if x.get('parent_span_id') and x['parent_span_id'] not in ids];assert len(a)==2 and len(missing)==1
a=js('13',True);ids={x['span_id'] for x in a};assert len(a)==3 and all(not x.get('parent_span_id') or x['parent_span_id'] in ids for x in a)
a=py('15')[0];assert data(a[0])['gen_ai.response.model']=='gpt-5-mini'
assert "CHUNK_MODELS ['gpt-5-mini'," in Path('control_SP-15.json').read_text()
a=py('16')[0];s=Path('result_SP-16.json').read_text();roots=D.raw_decode(s.split('ROOTS ')[1])[0]
assert len({x['trace_id'] for x in a+roots})==1
ids={x['span_id'] for x in a+roots};assert all(x['parent_span_id'] in ids for x in a)
names={x['span_id']:x.get('description','root') for x in a+roots}
expected={'inner workflow':'execute_tool inner_tool','invoke_agent inner':'inner workflow','execute_tool inner_tool':'invoke_agent outer','invoke_agent outer':'outer workflow'}
for x in a:
 if x['description'] in expected:assert names[x['parent_span_id']]==expected[x['description']]
inner=next(x for x in a if x['description']=='invoke_agent inner');assert [inp(x) for x in a if x['parent_span_id']==inner['span_id'] and op(x)=='gen_ai.chat']==[200]
print('PASS: all 15 claims and full-consumption / no-Sentry controls checked against transport output.')
