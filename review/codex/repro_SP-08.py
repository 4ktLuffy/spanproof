import fixture as f
import sentry_sdk
from sentry_sdk.integrations.langgraph import LanggraphIntegration
from sentry_sdk.integrations.langchain import LangchainIntegration
from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI
f.init([LanggraphIntegration(),LangchainIntegration()])
f.responder=lambda p,b:f.chat()
a=create_react_agent(ChatOpenAI(model='gpt-5-mini',api_key='local-placeholder',base_url=f.url),tools=[])
for mode in ('invoke','stream'):
 f.items.clear()
 with sentry_sdk.start_transaction(name=mode):
  r=getattr(a,mode)({'messages':[('user','hi')]})
  if mode=='stream':list(r)
 print('MODE',mode); f.report()
