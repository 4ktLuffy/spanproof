import json, threading, os
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"]="True"
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sentry_sdk
from sentry_sdk.transport import Transport
items=[]; requests=[]; responder=None
class Capture(Transport):
 def capture_envelope(self,envelope):
  for item in envelope.items:
   if item.headers['type'] in ('transaction','span'): items.append((item.headers['type'],item.payload.json))
def init(integrations):
 sentry_sdk.init(dsn='http://public@localhost/1',transport=Capture,traces_sample_rate=1,default_integrations=False,integrations=integrations,stream_gen_ai_spans=False)
def spans():
 sentry_sdk.flush()
 return [s for t,e in items for s in (e.get('spans',[]) if t=='transaction' else [e])]
def report():
 print('SPANS',json.dumps(spans(),indent=2,default=str))
 print('ROOTS',json.dumps([e['contexts']['trace'] for t,e in items if t=='transaction']))
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a): pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length']))); requests.append(body)
  result=responder(self.path,body)
  self.send_response(200); self.send_header('Content-Type','text/event-stream' if isinstance(result,list) else 'application/json'); self.end_headers()
  if isinstance(result,list):
   for event in result:
    self.wfile.write(event.encode()); self.wfile.flush()
  else: self.wfile.write(json.dumps(result).encode())
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
url=f'http://127.0.0.1:{server.server_port}/v1'
def chat(text='ok',tokens=1200,tool=None):
 return dict(id='chatcmpl-local',object='chat.completion',created=1,model='gpt-5-mini-2026-08-07',choices=[dict(index=0,message=dict(role='assistant',content=None if tool else text,**({'tool_calls':[dict(id='call_1',type='function',function=dict(name=tool,arguments='{}'))]} if tool else {})),finish_reason='tool_calls' if tool else 'stop')],usage=dict(prompt_tokens=tokens,completion_tokens=300,total_tokens=tokens+300,prompt_tokens_details=dict(cached_tokens=1024),completion_tokens_details=dict(reasoning_tokens=256)))
def response():
 return dict(id='resp_local',object='response',created_at=1,status='completed',model='gpt-5-mini-2026-08-07',output=[dict(id='msg_1',type='message',role='assistant',status='completed',content=[dict(type='output_text',text='ok',annotations=[])])],usage=dict(input_tokens=1200,output_tokens=300,total_tokens=1500,input_tokens_details=dict(cached_tokens=1024,cache_write_tokens=96),output_tokens_details=dict(reasoning_tokens=256)))
def stream_chat():
 chunks=[]
 for i in range(4):
  chunks.append('data: '+json.dumps(dict(id='chatcmpl-local',object='chat.completion.chunk',created=1,model='gpt-5-mini-2026-08-07',choices=[dict(index=0,delta=dict(content='x'),finish_reason='stop' if i==3 else None)]))+'\n\n')
 chunks.append('data: '+json.dumps(dict(id='chatcmpl-local',object='chat.completion.chunk',created=1,model='gpt-5-mini-2026-08-07',choices=[],usage=chat()['usage']))+'\n\n')
 return chunks+['data: [DONE]\n\n']
def stream_response():
 r=response(); events=[dict(type='response.created',response={**r,'status':'in_progress','output':[]})]+[dict(type='response.output_text.delta',item_id='msg_1',output_index=0,content_index=0,delta='x') for _ in range(3)]+[dict(type='response.completed',response=r)]
 return ['event: '+e['type']+'\ndata: '+json.dumps(dict(e,sequence_number=i))+'\n\n' for i,e in enumerate(events)]
