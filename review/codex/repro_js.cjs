const http = require('node:http');
const Sentry = require('@sentry/node');
const test = process.argv[2];
const envelopes=[];
Sentry.init({dsn:'http://public@localhost/1',tracesSampleRate:1,defaultIntegrations:false,integrations:[Sentry.openAIIntegration(),Sentry.anthropicAIIntegration(),Sentry.vercelAIIntegration()],transport:()=>({send:async e=>{envelopes.push(e);return {statusCode:200}},flush:async()=>true})});
const usage={input_tokens:1200,output_tokens:300,total_tokens:1500,input_tokens_details:{cached_tokens:1024,cache_write_tokens:96},output_tokens_details:{reasoning_tokens:256}};
const response={id:'resp_local',object:'response',created_at:1,status:'completed',model:'gpt-5-mini-2026-08-07',output:[{id:'msg_1',type:'message',role:'assistant',status:'completed',content:[{type:'output_text',text:'ok',annotations:[]}]}],usage};
const server=http.createServer(async(req,res)=>{
 let body='';for await(const c of req)body+=c; const b=JSON.parse(body);
 res.setHeader('Content-Type',b.stream?'text/event-stream':'application/json');
 if(b.stream){
  const events=[{type:'response.created',response:{...response,status:'in_progress',output:[]}},{type:'response.output_item.added',output_index:0,item:{id:'msg_1',type:'message',role:'assistant',content:[]}},{type:'response.content_part.added',item_id:'msg_1',output_index:0,content_index:0,part:{type:'output_text',text:'',annotations:[]}},...[1,2,3,4].map(()=>({type:'response.output_text.delta',item_id:'msg_1',output_index:0,content_index:0,delta:'hello'})),{type:'response.output_text.done',item_id:'msg_1',output_index:0,content_index:0,text:'hello'.repeat(4)},{type:'response.output_item.done',output_index:0,item:response.output[0]},{type:'response.completed',response}];
  for(let i=0;i<events.length;i++){res.write(`event: ${events[i].type}\ndata: ${JSON.stringify({...events[i],sequence_number:i})}\n\n`); await new Promise(r=>setTimeout(r,20));}res.end();
 }else if(req.url.includes('messages'))res.end(JSON.stringify({id:'msg_local',type:'message',role:'assistant',model:'claude-sonnet-4-5',content:[{type:'text',text:'ok'}],stop_reason:'end_turn',stop_sequence:null,usage:{input_tokens:40,cache_read_input_tokens:2048,cache_creation_input_tokens:512,output_tokens:120}}));
 else if(req.url.includes('responses'))res.end(JSON.stringify(response));
 else res.end(JSON.stringify({id:'chatcmpl-local',object:'chat.completion',created:1,model:'gpt-5-mini-2026-08-07',choices:[{index:0,message:{role:'assistant',content:'ok'},finish_reason:'stop'}],usage:{prompt_tokens:1200,completion_tokens:300,total_tokens:1500,prompt_tokens_details:{cached_tokens:1024},completion_tokens_details:{reasoning_tokens:256}}}));
});
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r)); const baseURL=`http://127.0.0.1:${server.address().port}/v1`;
 await Sentry.startSpan({name:test,op:'test'},async()=>{
  if(test==='SP-12'){
   const Anthropic=require('@anthropic-ai/sdk');const client=new Anthropic({apiKey:'local-placeholder',baseURL});
   await client.messages.create({model:'claude-sonnet-4-5',max_tokens:500,messages:[{role:'user',content:'hi'}]});
  }else if(test==='SP-13'){
   const {streamText}=require('ai'); const {createOpenAI}=require('@ai-sdk/openai');
   const result=streamText({model:createOpenAI({apiKey:'local-placeholder',baseURL})('gpt-5-mini'),prompt:'hi',experimental_telemetry:{isEnabled:true}});
   let n=0;for await(const part of result.textStream){if(++n===2 && process.env.FULL!=='1')break;}
   if(process.env.FULL==='1') await result.consumeStream();
   await new Promise(r=>setTimeout(r,500));
  }else{
   const OpenAI=require('openai');const client=new OpenAI({apiKey:'local-placeholder',baseURL});
   if(test==='SP-04')console.log('PARSED_USAGE',JSON.stringify((await client.responses.create({model:'gpt-5-mini',input:'hi'})).usage));
   else await client.chat.completions.create({model:'gpt-5-mini',messages:[{role:'user',content:'hi'}]});
  }
 });
 await new Promise(r=>setTimeout(r,1000));await Sentry.flush(5000);
 server.close();await Sentry.close(1000);console.log('ENVELOPES',JSON.stringify(envelopes,null,2));
})().catch(e=>{console.error(e);server.close();process.exitCode=1});
