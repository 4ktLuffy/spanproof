import sys
import fixture as f
import sentry_sdk
from sentry_sdk.integrations.openai_agents import OpenAIAgentsIntegration
from agents import Agent,Runner,OpenAIChatCompletionsModel,function_tool,set_tracing_disabled
from openai import AsyncOpenAI
set_tracing_disabled(True)
f.init([OpenAIAgentsIntegration()])
model=OpenAIChatCompletionsModel(model='gpt-5-mini',openai_client=AsyncOpenAI(api_key='local-placeholder',base_url=f.url))
@function_tool
def ping()->str:
 return 'pong'
case=sys.argv[1]
if case=='SP-06':
 agent=Agent(name='single',model=model,tools=[ping])
 f.responder=lambda p,b:f.chat(tokens=100 if len(f.requests)==1 else 200,tool='ping' if len(f.requests)==1 else None)
else:
 inner=Agent(name='inner',instructions='inner instruction',model=model)
 agent=Agent(name='outer',model=model,tools=[inner.as_tool(tool_name='inner_tool',tool_description='ask inner')])
 def reply(p,b):
  n=len(f.requests)
  r=f.chat(tokens={1:100,2:200,3:300}[n],tool='inner_tool' if n==1 else None)
  if n==1:r['choices'][0]['message']['tool_calls'][0]['function']['arguments']='{"input":"hello"}'
  return r
 f.responder=reply
original_reply=f.responder
def valid_reply(p,b):
 r=original_reply(p,b); r['usage']['prompt_tokens_details']['cached_tokens']=0; return r
f.responder=valid_reply
with sentry_sdk.start_transaction(name=case):
 result=Runner.run_sync(agent,'hi'); print('RESULT',result.final_output,'USAGE',result.context_wrapper.usage)
f.report()
