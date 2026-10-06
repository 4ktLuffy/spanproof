// Run ONE JavaScript scenario with @sentry/node and print its spans.
//
//   node worker.cjs <job.json> [--no-data-collection]
//
// The job (written by spanproof/js_bridge.py) carries the scripted provider replies;
// fixtures and ground truth stay in Python so both SDKs are judged by the same truth.
'use strict';

const fs = require('fs');
const http = require('http');

const MARK = '\n@@SPANPROOF-RESULT@@\n';
const job = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const dataCollection = !process.argv.includes('--no-data-collection');
const DUMMY_KEY = 'spanproof-dummy-key';

// ------------------------------------------------------------------ mock provider
const replies = job.replies.slice();
const requests = [];
const server = http.createServer((req, res) => {
  let body = '';
  req.on('data', c => (body += c));
  req.on('end', () => {
    requests.push({ path: req.url });
    const r = replies.shift() || { body: { error: { message: 'script exhausted' } }, status: 500 };
    if (!r.events) {
      const data = JSON.stringify(r.body);
      res.writeHead(r.status || 200, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data) });
      res.end(data);
      return;
    }
    res.writeHead(r.status || 200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'close' });
    r.events.forEach((ev, i) => {
      if (r.cut_after != null && i >= r.cut_after) return;
      let chunk = '';
      if (r.sse_event_names) chunk += `event: ${r.sse_event_names[i]}\n`;
      chunk += `data: ${typeof ev === 'string' ? ev : JSON.stringify(ev)}\n\n`;
      res.write(chunk);
    });
    if (r.cut_after != null) { res.destroy(); return; }
    if (r.sse_done !== false && !r.sse_event_names) res.write('data: [DONE]\n\n');
    res.end();
  });
});

// ------------------------------------------------------------------ sentry capture
const envelopes = [];
const Sentry = require('@sentry/node');
Sentry.init({
  dsn: 'http://spanproof@127.0.0.1:9/1',
  tracesSampleRate: 1.0,
  sendDefaultPii: dataCollection,
  defaultIntegrations: false,
  integrations: [
    Sentry.openAIIntegration({ recordInputs: dataCollection, recordOutputs: dataCollection }),
    Sentry.anthropicAIIntegration({ recordInputs: dataCollection, recordOutputs: dataCollection }),
    Sentry.vercelAIIntegration({ recordInputs: dataCollection, recordOutputs: dataCollection }),
  ],
  transport: () => ({
    send: async env => { envelopes.push(env); return { statusCode: 200 }; },
    flush: async () => true,
  }),
});

function flatten() {
  const spans = [];
  const errors = [];
  let transactions = 0;
  const ts = v => (typeof v === 'number' ? v : v ? Date.parse(v) / 1000 : null);
  for (const env of envelopes) {
    for (const [hdr, payload] of env[1]) {
      if (hdr.type === 'transaction') {
        transactions++;
        const tc = (payload.contexts || {}).trace || {};
        spans.push({ trace_id: tc.trace_id, span_id: tc.span_id, parent_span_id: tc.parent_span_id || null, op: tc.op,
          description: payload.transaction, data: tc.data || {}, status: tc.status, start: ts(payload.start_timestamp),
          finished: payload.timestamp != null, is_root: true });
        for (const s of payload.spans || []) {
          spans.push({ trace_id: s.trace_id, span_id: s.span_id, parent_span_id: s.parent_span_id, op: s.op,
            description: s.description, data: s.data || {}, status: s.status, start: ts(s.start_timestamp),
            finished: s.timestamp != null, is_root: false });
        }
      } else if (hdr.type === 'span') {
        for (const s of (payload.items || [payload])) {
          const attrs = {};
          for (const [k, v] of Object.entries(s.attributes || {})) attrs[k] = v && typeof v === 'object' && 'value' in v ? v.value : v;
          spans.push({ trace_id: s.trace_id, span_id: s.span_id, parent_span_id: s.parent_span_id || null,
            op: attrs['sentry.op'], description: s.name, data: attrs, status: s.status, start: ts(s.start_timestamp),
            finished: s.end_timestamp != null, is_root: !s.parent_span_id });
        }
      } else if (hdr.type === 'event') {
        const v = ((payload.exception || {}).values || [{}]).slice(-1)[0];
        errors.push({ type: v.type, value: String(v.value).slice(0, 300) });
      }
    }
  }
  return { spans, errors, transactions };
}

// ------------------------------------------------------------------ scenarios
const MSG = [{ role: 'system', content: 'Be brief.' }, { role: 'user', content: 'Capital of France?' }];
const AKW = { model: 'claude-sonnet-5-5', max_tokens: 256, system: 'Be brief.', messages: [{ role: 'user', content: 'Capital of France?' }] };

function openai(url) { const OpenAI = require('openai'); const C = OpenAI.default || OpenAI; return new C({ apiKey: DUMMY_KEY, baseURL: url + '/v1', maxRetries: 0 }); }
function anthropic(url) { const A = require('@anthropic-ai/sdk'); const C = A.default || A; return new C({ apiKey: DUMMY_KEY, baseURL: url, maxRetries: 0 }); }

const SCENARIOS = {
  'js.openai.chat.sync': async url => { await openai(url).chat.completions.create({ model: 'gpt-5-mini', messages: MSG }); },
  'js.openai.chat.stream': async url => {
    const s = await openai(url).chat.completions.create({ model: 'gpt-5-mini', messages: MSG, stream: true, stream_options: { include_usage: true } });
    for await (const _ of s) { /* drain */ }
  },
  'js.openai.chat.stream.early_close': async url => {
    const s = await openai(url).chat.completions.create({ model: 'gpt-5-mini', messages: MSG, stream: true, stream_options: { include_usage: true } });
    let i = 0;
    for await (const _ of s) { if (++i === 3) break; }
  },
  'js.openai.chat.tool_call': async url => {
    await openai(url).chat.completions.create({ model: 'gpt-5-mini', messages: MSG, tools: [{ type: 'function', function: { name: 'get_weather', description: 'Weather', parameters: { type: 'object', properties: { city: { type: 'string' } } } } }] });
  },
  'js.openai.responses.sync': async url => { await openai(url).responses.create({ model: 'gpt-5-mini', input: 'Capital of France?', instructions: 'Be brief.' }); },
  'js.openai.responses.stream': async url => {
    const s = await openai(url).responses.create({ model: 'gpt-5-mini', input: 'Capital of France?', stream: true });
    for await (const _ of s) { /* drain */ }
  },
  'js.openai.responses.stream.early_close': async url => {
    const s = await openai(url).responses.create({ model: 'gpt-5-mini', input: 'Capital of France?', stream: true });
    let i = 0;
    for await (const _ of s) { if (++i === 3) break; }
  },
  'js.anthropic.messages.sync': async url => { await anthropic(url).messages.create(AKW); },
  'js.anthropic.messages.stream': async url => {
    const s = await anthropic(url).messages.create({ ...AKW, stream: true });
    for await (const _ of s) { /* drain */ }
  },
  'js.anthropic.messages.stream_helper': async url => {
    const s = anthropic(url).messages.stream(AKW);
    for await (const _ of s) { /* drain */ }
    await s.finalMessage();
  },
  'js.anthropic.messages.stream.early_close': async url => {
    const s = await anthropic(url).messages.create({ ...AKW, stream: true });
    let i = 0;
    for await (const _ of s) { if (++i === 3) break; }
  },
  'js.anthropic.messages.stream.as_response': async url => {
    // getsentry/sentry-javascript#24258: stream consumed through .asResponse()
    const resp = await anthropic(url).messages.create({ ...AKW, stream: true }).asResponse();
    await resp.text();
  },
  'js.anthropic.messages.with_response': async url => {
    const { data } = await anthropic(url).messages.create(AKW).withResponse();
    void data;
  },
  'js.vercel_ai.generate_text': async url => {
    const { generateText } = require('ai');
    const { createOpenAI } = require('@ai-sdk/openai');
    const provider = createOpenAI({ apiKey: DUMMY_KEY, baseURL: url + '/v1' });
    await generateText({ model: provider.chat('gpt-5-mini'), prompt: 'Capital of France?', experimental_telemetry: { isEnabled: true } });
  },
  'js.vercel_ai.stream_text.early_close': async url => {
    const { streamText } = require('ai');
    const { createOpenAI } = require('@ai-sdk/openai');
    const provider = createOpenAI({ apiKey: DUMMY_KEY, baseURL: url + '/v1' });
    const r = streamText({ model: provider.chat('gpt-5-mini'), prompt: 'Capital of France?', experimental_telemetry: { isEnabled: true } });
    let i = 0;
    for await (const _ of r.textStream) { if (++i === 2) break; }
  },
};

// ------------------------------------------------------------------ run
(async () => {
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const url = `http://127.0.0.1:${server.address().port}`;
  let exception = null;
  const fn = SCENARIOS[job.scenario];
  if (!fn) { process.stdout.write(MARK + JSON.stringify({ unknown: job.scenario })); process.exit(0); }
  await Sentry.startSpan({ name: job.scenario, op: 'spanproof.scenario', forceTransaction: true }, async () => {
    try { await fn(url); } catch (e) { exception = { type: e && e.constructor ? e.constructor.name : String(e), value: String(e && e.message).slice(0, 300), tb: String(e && e.stack).slice(-1500) }; }
  });
  await new Promise(r => setTimeout(r, 300));
  await Sentry.flush(3000);
  server.close();
  const out = flatten();
  const versions = {};
  for (const p of ['@sentry/node', 'openai', '@anthropic-ai/sdk', 'ai']) {
    try { versions[p] = JSON.parse(fs.readFileSync(require.resolve('./node_modules/' + p + '/package.json'), 'utf8')).version; } catch (_) { /* absent */ }
  }
  Object.assign(out, { exception, requests, versions, data_collection: dataCollection, node: process.version });
  process.stdout.write(MARK + JSON.stringify(out));
  process.exit(0);
})();
