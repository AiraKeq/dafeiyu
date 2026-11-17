import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { PRIVATE_RUNTIME, ensurePrivateRuntime } from './runtime-security.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REMOTE = '/opt/qq-deepseek/manager/remote.py';
const READ_ACTIONS = new Set(['status', 'personality', 'style_get', 'memory_list', 'backups']);
const ACTIONS = new Set([...READ_ACTIONS, 'personality_save', 'style_save', 'event_update', 'event_delete', 'backup', 'service_start', 'service_stop']);
const random = () => crypto.randomBytes(32).toString('hex');
const equal = (a, b) => typeof a === 'string' && typeof b === 'string' && Buffer.byteLength(a) === Buffer.byteLength(b) && crypto.timingSafeEqual(Buffer.from(a), Buffer.from(b));
const failure = (code, message) => ({ok: false, error: {code, message}});

// Regular-file stdio avoids Windows sandbox named pipes. No credentials are copied.
export class SshBridge {
  constructor({runtimeDir = PRIVATE_RUNTIME, timeoutMs = 90000, ssh = 'C:\\Windows\\System32\\OpenSSH\\ssh.exe', key = path.join(os.homedir(), '.ssh', 'qq_deepseek_20261113'), host = 'root@8.217.178.5', remoteDigest} = {}) {
    Object.assign(this, {runtimeDir, timeoutMs, ssh, key, host});
    this.remoteDigest = remoteDigest ?? crypto.createHash('sha256').update(fs.readFileSync(path.join(HERE, 'remote.py'))).digest('hex');
    if (!/^[a-f0-9]{64}$/.test(this.remoteDigest)) throw new Error('Invalid remote digest');
  }
  async call(request) {
    await ensurePrivateRuntime(this.runtimeDir);
    const dir = fs.mkdtempSync(path.join(this.runtimeDir, 'request-'));
    const input = path.join(dir, 'input.json'), output = path.join(dir, 'output.json'), error = path.join(dir, 'error.txt');
    fs.writeFileSync(input, JSON.stringify(request), {mode: 0o600, flag: 'wx'});
    const fds = [fs.openSync(input, 'r'), fs.openSync(output, 'wx', 0o600), fs.openSync(error, 'wx', 0o600)];
    // Refuse an old or replaced remote helper; fixed Python expression, no user input in command.
    const check = `import hashlib,sys; p='${REMOTE}'; b=open(p,'rb').read(); assert hashlib.sha256(b).hexdigest()=='${this.remoteDigest}', 'helper mismatch'; exec(compile(b,p,'exec'))`;
    const command = `python3 -c "${check}"`;
    let timedOut = false;
    try {
      const code = await new Promise((resolve, reject) => {
        const child = spawn(this.ssh, ['-i', this.key, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3', this.host, command], {stdio: fds, windowsHide: true});
        const timer = setTimeout(() => {timedOut = true; child.kill();}, this.timeoutMs);
        child.once('error', err => {clearTimeout(timer); reject(err);});
        child.once('close', exit => {clearTimeout(timer); resolve(exit);});
      });
      if (timedOut) return failure('RESULT_UNKNOWN', '连接超时，操作结果未知。请刷新状态核验，不要重复点击启用。');
      if (code !== 0) return failure('SSH_FAILED', '云端连接或管理程序校验失败。未获得可信结果，请检查网络、SSH权限与部署版本。');
      if (fs.statSync(output).size > 16 * 1024 * 1024) return failure('BAD_RESPONSE', '云端响应超过安全上限。');
      let result;
      try { result = JSON.parse(fs.readFileSync(output, 'utf8')); } catch { return failure('BAD_RESPONSE', '云端返回内容无效，结果未知。'); }
      if (typeof result?.ok !== 'boolean') return failure('BAD_RESPONSE', '云端响应格式不匹配。');
      return result;
    } catch {
      return failure('SSH_FAILED', '无法执行SSH连接。请检查现有密钥权限与网络，不要复制或公开密钥。');
    } finally {
      for (const fd of fds) fs.closeSync(fd);
      // Only these just-created, resolved per-request files may be removed.
      if (path.dirname(path.resolve(dir)) !== path.resolve(this.runtimeDir)) throw new Error('Unsafe cleanup path');
      for (const file of [input, output, error]) fs.unlinkSync(file);
      fs.rmdirSync(dir);
    }
  }
}

export function createManager({bridge, port = 17865, sessionTtlMs = 12 * 3600000, bootstrapTtlMs = 120000, publicDir = path.join(HERE, 'public')} = {}) {
  if (!bridge || typeof bridge.call !== 'function') throw new Error('A bridge is required');
  const launchKey = random(), instance = random(), sessions = new Map(), bootstraps = new Map();
  let busy = false, inflight = 0;
  const prune = map => {for (const [key, item] of map) if (item.expires <= Date.now()) map.delete(key);};
  const issueBootstrap = () => { prune(bootstraps); if (bootstraps.size > 32) bootstraps.delete(bootstraps.keys().next().value); const token = random(); bootstraps.set(token, {expires: Date.now() + bootstrapTtlMs}); return token; };
  const server = http.createServer(async (req, res) => {
    const actualPort = server.address()?.port ?? port;
    const origin = `http://127.0.0.1:${actualPort}`;
    const headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY', 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'", 'Cross-Origin-Resource-Policy': 'same-origin'};
    const send = (status, payload, extra = {}) => {res.writeHead(status, {...headers, 'Content-Type': 'application/json; charset=utf-8', ...extra}); res.end(JSON.stringify(payload));};
    if (req.headers.host !== `127.0.0.1:${actualPort}`) return send(403, failure('HOST_REJECTED', '仅允许从本机127.0.0.1访问。'));
    if (!['127.0.0.1', '::ffff:127.0.0.1'].includes(req.socket.remoteAddress)) return send(403, failure('LOCAL_ONLY', '只允许本机连接。'));
    let url;
    try {url = new URL(req.url, origin);} catch {return send(400, failure('BAD_REQUEST', '请求无效。'));}
    if (url.search) return send(400, failure('QUERY_REJECTED', '接口不接受地址查询参数。'));
    if (req.method === 'GET' && url.pathname === '/health') return send(200, {app: 'whale-manager', version: '1.0.0', instance});
    const sameOrigin = req.headers.origin === origin && !['cross-site', 'same-site'].includes(req.headers['sec-fetch-site']);
    const readBody = async () => {
      if (req.headers['content-type']?.split(';')[0].trim() !== 'application/json') throw new Error('CONTENT_TYPE');
      if (Number(req.headers['content-length']) > 65536) throw new Error('TOO_LARGE');
      let bytes = 0, chunks = [];
      for await (const chunk of req) {bytes += chunk.length; if (bytes > 65536) throw new Error('TOO_LARGE'); chunks.push(chunk);}
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      if (!body || Array.isArray(body) || typeof body !== 'object') throw new Error('BAD_JSON');
      return body;
    };
    try {
      if (req.method === 'POST' && url.pathname === '/api/launch') {
        // This local capability is read by the launcher only, never by web content.
        if (req.headers.origin || !equal(req.headers.authorization, `Bearer ${launchKey}`)) return send(403, failure('FORBIDDEN', '启动授权无效。'));
        return send(200, {ok: true, data: {token: issueBootstrap()}});
      }
      if (req.method === 'POST' && url.pathname === '/api/login') {
        if (!sameOrigin) return send(403, failure('ORIGIN_REJECTED', '请从本机启动器打开管理器。'));
        const body = await readBody(); prune(bootstraps);
        if (typeof body.token !== 'string' || !bootstraps.has(body.token)) return send(401, failure('LOGIN_EXPIRED', '登录链接已使用或过期，请重新双击启动器。'));
        bootstraps.delete(body.token); prune(sessions);
        if (sessions.size >= 32) sessions.delete(sessions.keys().next().value);
        const sid = random(), csrf = random(); sessions.set(sid, {csrf, expires: Date.now() + sessionTtlMs});
        return send(200, {ok: true, data: {authenticated: true, csrf}}, {'Set-Cookie': `whale_session=${sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age=${Math.floor(sessionTtlMs / 1000)}`});
      }
      if (url.pathname.startsWith('/api/')) {
        prune(sessions);
        const sid = req.headers.cookie?.split(';').map(c => c.trim()).find(c => c.startsWith('whale_session='))?.slice('whale_session='.length);
        const session = sessions.get(sid);
        if (!session) return send(401, failure('AUTH_REQUIRED', '请使用“大肥鱼聊天管理器”启动入口登录。'));
        if (req.method === 'GET' && url.pathname === '/api/session') {
          // Loopback cookies have no port isolation. A cookie seen by another
          // local web server must not be exchangeable for this browser grant.
          if (!equal(req.headers['x-csrf-token'], session.csrf)) return send(401, failure('AUTH_REQUIRED', '请从桌面启动入口重新授权此页面。'));
          return send(200, {ok: true, data: {authenticated: true, csrf: session.csrf}});
        }
        if (req.method !== 'POST' || url.pathname !== '/api/action') return send(404, failure('NOT_FOUND', '接口不存在。'));
        if (!sameOrigin || !equal(req.headers['x-csrf-token'], session.csrf)) return send(403, failure('CSRF_REJECTED', '页面授权失效，请重新打开管理器。'));
        const body = await readBody();
        if (!ACTIONS.has(body.action)) return send(400, failure('UNKNOWN_ACTION', '此操作不受支持。'));
        const write = !READ_ACTIONS.has(body.action);
        if (busy || (write && inflight > 0)) return send(409, failure('BUSY', '另一项操作进行中，请稍后刷新。'));
        if (inflight >= 4) return send(429, failure('BUSY', '查询过多，请稍候。'));
        if (write) busy = true;
        inflight++;
        try {return send(200, await bridge.call(body));} finally {inflight--; if (write) busy = false;}
      }
      const statics = {'/': ['index.html', 'text/html'], '/index.html': ['index.html', 'text/html'], '/app.js': ['app.js', 'text/javascript'], '/app.css': ['app.css', 'text/css']};
      if (req.method !== 'GET' || !statics[url.pathname]) return send(404, failure('NOT_FOUND', '页面不存在。'));
      const [name, type] = statics[url.pathname];
      const file = fs.readFileSync(path.join(publicDir, name));
      res.writeHead(200, {...headers, 'Content-Type': `${type}; charset=utf-8`}); res.end(file);
    } catch {
      if (!res.headersSent) send(400, failure('BAD_REQUEST', '请求处理失败，请刷新状态后重试。'));
      else res.end();
    }
  });
  server.requestTimeout = 15000;
  server.headersTimeout = 10000;
  return {server, instance, launchKey, issueBootstrap};
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const runtimeDir = PRIVATE_RUNTIME;
  await ensurePrivateRuntime(runtimeDir);
  const port = 17865;
  const manager = createManager({bridge: new SshBridge({runtimeDir}), port});
  manager.server.on('error', () => {console.error('管理器端口不可用。若已打开，请使用启动器重新进入；不要启动第二套服务。'); process.exitCode = 1;});
  manager.server.listen(port, '127.0.0.1', () => {
    const accessPath = path.join(runtimeDir, 'access.json');
    fs.writeFileSync(accessPath, JSON.stringify({port, instance: manager.instance, launchKey: manager.launchKey, pid: process.pid}), {mode: 0o600});
    console.log(`大肥鱼管理器已就绪：http://127.0.0.1:${port}/（请使用启动器登录；未启用QQ聊天）`);
  });
}
