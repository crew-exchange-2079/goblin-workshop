/*
 * serve.test.js — the local static server answers bad input with an error
 * status instead of crashing, and never serves outside its folder.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { request } from 'node:http';

const port = 18944 + (process.pid % 1000);

// raw request so the path reaches the server exactly as written (fetch would normalise it)
const get = (path) =>
  new Promise((resolve, reject) => {
    const req = request({ host: '127.0.0.1', port, path, method: 'GET' }, (res) => {
      res.resume();
      res.on('end', () => resolve(res.statusCode));
    });
    req.on('error', reject);
    req.end();
  });

test('serve.mjs survives malformed and hostile paths', async (t) => {
  const child = spawn(process.execPath, ['serve.mjs', String(port)], { cwd: new URL('.', import.meta.url), stdio: ['ignore', 'pipe', 'pipe'] });
  t.after(() => child.kill());
  let stderr = '';
  child.stderr.on('data', (d) => (stderr += d));
  await new Promise((resolve, reject) => {
    child.stdout.once('data', resolve);
    child.once('exit', (code) => reject(new Error(`server exited early (${code}): ${stderr}`)));
  });

  assert.equal(await get('/%'), 400);
  assert.equal(await get('/%E0%A4%A'), 400);
  assert.equal(await get('/../README.md'), 404);
  assert.equal(await get('/%2e%2e/%2e%2e/etc/passwd'), 404);
  assert.equal(await get('/node_modules/playwright/index.js'), 404);
  assert.equal(await get('/'), 200, 'still alive and serving after the bad requests');
  assert.equal(await get('/planner.js'), 200);
  assert.equal(child.exitCode, null);
  assert.equal(stderr, '');
});
