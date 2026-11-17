import {test, before, after} from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createManager} from './server.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
const fixture = fs.mkdtempSync(path.join(root, '.http-test-'));
for (const [name, text] of Object.entries({'index.html': '<!doctype html><p>测试</p>', 'app.css': 'body{}', 'app.js': 'void 0;'})) fs.writeFileSync(path.join(fixture, name), text);
let app, base, origin, cookie, csrf, unblock;
const calls = [];
before(async () => {
  app = createManager({publicDir: fixture, bridge: {async call(body) {calls.push(body); if (body.action === 'backup') await new Promise(resolve => {unblock = resolve;}); return {ok:true, data:{action:body.action}};}}});
  await new Promise(resolve => app.server.listen(0, '127.0.0.1', resolve));
  base = origin = `http://127.0.0.1:${app.server.address().port}`;
});
after(async () => {
  app.server.closeAllConnections(); await new Promise(resolve => app.server.close(resolve));
  assert.equal(path.dirname(fixture), root);
  for (const name of ['index.html', 'app.css', 'app.js']) fs.unlinkSync(path.join(fixture, name));
  fs.rmdirSync(fixture);
});
const post = (url, data, headers = {}) => fetch(base+url, {method:'POST', headers:{'Content-Type':'application/json', Origin:origin,...headers}, body:JSON.stringify(data)});
const action = (body, headers={}) => post('/api/action', body, {Cookie:cookie,'X-CSRF-Token':csrf,...headers});

test('health exposes only public identity', async () => {
  const r = await fetch(base+'/health'); const j=await r.json();
  assert.equal(j.app,'whale-manager'); assert.deepEqual(Object.keys(j).sort(), ['app','instance','version']);
  assert.equal(r.headers.get('cache-control'),'no-store');
});
test('unauthenticated shell carries CSP and no secrets', async () => {
  const r=await fetch(base+'/'); assert.equal(r.status,200); assert.match(r.headers.get('content-security-policy'), /frame-ancestors 'none'/); assert.equal(r.headers.get('x-frame-options'),'DENY');
});
test('all remote calls require authentication', async () => {
  assert.equal((await post('/api/action',{action:'status'})).status,401); assert.equal(calls.length,0);
});
test('paths and query token bypass rejected', async () => {
  assert.equal((await fetch(base+'/.runtime/access.json')).status,404);
  assert.equal((await fetch(base+'/api/session?token=guess')).status,400);
});
test('DNS rebinding Host refused', async () => {
  const status=await new Promise(resolve=>{http.get(base+'/health',{headers:{Host:'evil.test'}},r=>{r.resume();resolve(r.statusCode);});}); assert.equal(status,403);
});
test('launch capability not available to web origins', async () => {
  assert.equal((await post('/api/launch',{}, {Authorization:`Bearer ${app.launchKey}`})).status,403);
  const r=await fetch(base+'/api/launch',{method:'POST',headers:{Authorization:'Bearer nope'}}); assert.equal(r.status,403);
});
test('local launcher exchanges key for a one-time login token', async () => {
  const r=await fetch(base+'/api/launch',{method:'POST',headers:{Authorization:`Bearer ${app.launchKey}`}}); assert.equal(r.status,200);
  const token=(await r.json()).data.token;
  const login=await post('/api/login',{token}); assert.equal(login.status,200);
  const header=login.headers.get('set-cookie'); assert.match(header,/HttpOnly; SameSite=Strict/); cookie=header.split(';')[0];
  assert.equal((await post('/api/login',{token})).status,401);
  csrf=(await login.json()).data.csrf;
  const session=await fetch(base+'/api/session',{headers:{Cookie:cookie,'X-CSRF-Token':csrf}}); assert.equal((await session.json()).data.csrf,csrf); assert.equal(csrf.length,64);
});
test('a leaked loopback cookie cannot retrieve the browser grant', async () => {
  const r=await fetch(base+'/api/session',{headers:{Cookie:cookie}});
  assert.equal(r.status,401); const body=await r.text(); assert.ok(!body.includes(csrf));
});
test('cross-origin login cannot use valid bootstrap', async () => {
  assert.equal((await post('/api/login',{token:app.issueBootstrap()},{Origin:'https://evil.test'})).status,403);
});
test('CSRF and Origin enforced before bridge', async () => {
  assert.equal((await action({action:'status'},{'X-CSRF-Token':'bad'})).status,403);
  assert.equal((await action({action:'service_start'},{Origin:'https://evil.test'})).status,403);
  assert.equal((await action({action:'service_start'},{'Sec-Fetch-Site':'cross-site'})).status,403);
  assert.equal(calls.length,0);
});
test('unknown commands never reach SSH', async () => {
  assert.equal((await action({action:'exec',command:'anything'})).status,400); assert.equal(calls.length,0);
});
test('authenticated status works without production', async () => {
  const r=await action({action:'status'}); assert.equal(r.status,200); assert.equal((await r.json()).data.action,'status'); assert.equal(calls.length,1);
});
test('large, malformed and non-object JSON rejected', async () => {
  assert.equal((await action({action:'personality_save',text:'a'.repeat(70000)})).status,400);
  assert.equal((await post('/api/action',[],{Cookie:cookie,'X-CSRF-Token':csrf})).status,400);
  const r=await fetch(base+'/api/action',{method:'POST',headers:{Cookie:cookie,Origin:origin,'X-CSRF-Token':csrf,'Content-Type':'text/plain'},body:'{}'}); assert.equal(r.status,400);
});
test('write operations are serialized and block reads', async () => {
  const first=action({action:'backup',confirm:'备份记忆'});
  await new Promise(resolve=>setImmediate(resolve));
  // Wait for the bridge entry, not an arbitrary network sleep.
  for (let i=0;i<100 && !unblock;i++) await new Promise(resolve=>setImmediate(resolve));
  assert.equal(typeof unblock,'function');
  try {
    assert.equal((await action({action:'service_start'})).status,409);
    assert.equal((await action({action:'status'})).status,409);
  } finally {unblock();}
  assert.equal((await first).status,200);
});
test('remote envelope preserved, no shell interpolation', async () => {
  const query='"; touch /fake; <script>alert(1)</script>';
  await action({action:'memory_list',scope:'isolated-test',query});
  assert.equal(calls.at(-1).query,query);
});
test('unsupported methods do not mutate', async () => {
  assert.equal((await fetch(base+'/api/action',{headers:{Cookie:cookie}})).status,404);
});
