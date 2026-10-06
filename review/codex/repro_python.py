import sys, os, time
import fixture as f
from sentry_sdk.integrations.openai import OpenAIIntegration
from sentry_sdk.integrations.anthropic import AnthropicIntegration
from sentry_sdk.integrations.litellm import LiteLLMIntegration
import sentry_sdk
case=sys.argv[1]
if os.environ.get('NO_SENTRY')!='1':
 f.init([LiteLLMIntegration()] if case in ('SP-01','SP-03','SP-15') else [OpenAIIntegration(),AnthropicIntegration()])
f.responder=lambda p,b: f.stream_response() if b.get('stream') and 'responses' in p else f.stream_chat() if b.get('stream') else f.response() if 'responses' in p else f.chat()
from openai import OpenAI
client=OpenAI(api_key='local-placeholder',base_url=f.url)
with sentry_sdk.start_transaction(name=case):
 if case in ('SP-01','SP-03','SP-15'):
  import litellm
  result=litellm.completion(model='gpt-5-mini',messages=[{'role':'user','content':'hi'}],api_key='local-placeholder',api_base=f.url,stream=case!='SP-01')
  if case=='SP-03':
   for i,c in enumerate(result):
    if i==1 and os.environ.get('FULL')!='1': break
  elif case=='SP-15':
   chunks=list(result); print('CHUNK_MODELS', [c.model for c in chunks])
  time.sleep(2)  # LiteLLM finishes its logging callbacks on background threads.
 elif case=='SP-02':
  for kind in ('chat','responses'):
   stream=client.chat.completions.create(model='gpt-5-mini',messages=[{'role':'user','content':'hi'}],stream=True) if kind=='chat' else client.responses.create(model='gpt-5-mini',input='hi',stream=True)
   for i,c in enumerate(stream):
    if i==1 and os.environ.get('FULL')!='1': break
   stream.close()
 elif case=='SP-04':
  r=client.responses.create(model='gpt-5-mini',input='hi'); print('PARSED_USAGE',r.usage.model_dump())
 elif case in ('SP-05','SP-10'):
  from anthropic import Anthropic
  f.responder=lambda p,b: dict(id='msg_local',type='message',role='assistant',model='claude-sonnet-4-5',content=[dict(type='text',text='ok')],stop_reason='end_turn',stop_sequence=None,usage=dict(input_tokens=40,output_tokens=120,cache_read_input_tokens=2048,cache_creation_input_tokens=512))
  a=Anthropic(api_key='local-placeholder',base_url=f.url)
  kw=dict(model='claude-sonnet-4-5',max_tokens=500,messages=[dict(role='user',content='hi')])
  if case=='SP-05': print('PARSED_USAGE',a.messages.with_raw_response.create(**kw).parse().usage)
  a.messages.create(**kw)
  if case=='SP-10':
   f.responder=lambda p,b:f.chat(); client.chat.completions.create(model='gpt-5-mini',messages=[dict(role='user',content='hi')])
f.report()
