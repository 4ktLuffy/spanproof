import asyncio
import fixture as f
import sentry_sdk
from sentry_sdk.integrations.pydantic_ai import PydanticAIIntegration
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
f.init([PydanticAIIntegration()])
a=Agent(OpenAIChatModel('gpt-5-mini',provider=OpenAIProvider(base_url=f.url,api_key='local-placeholder')))
@a.tool_plain
def ping()->str:return 'pong'
async def main():
 for mode in ('run','iter'):
  f.items.clear(); f.requests.clear()
  f.responder=lambda p,b:f.chat(tool='ping' if len(f.requests)==1 else None)
  with sentry_sdk.start_transaction(name=mode):
   if mode=='run':await a.run('hi')
   else:
    async with a.iter('hi') as run:
     async for node in run:pass
  print('MODE',mode); f.report()
asyncio.run(main())
