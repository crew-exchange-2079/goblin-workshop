/*
 * serve.mjs — a tiny static file server for local use and the Playwright smoke
 * test (ES modules will not load over file://). No dependencies.
 *   node serve.mjs [port]      default 8944, binds 127.0.0.1 only
 */
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('.', import.meta.url));
const port = Number(process.argv[2] || process.env.PORT || 8944);
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };

createServer(async (req, res) => {
  let path;
  try {
    path = decodeURIComponent(new URL(req.url, 'http://x').pathname);
  } catch {
    res.writeHead(400).end('bad request'); // e.g. "/%": malformed percent-encoding
    return;
  }
  const file = normalize(join(root, path === '/' ? 'index.html' : path));
  // only the three kinds of file the page needs, and only from this folder
  if (!file.startsWith(root.endsWith(sep) ? root : root + sep) || !TYPES[extname(file)] || file.includes(`${sep}node_modules${sep}`)) {
    res.writeHead(404).end('not found');
    return;
  }
  try {
    const body = await readFile(file);
    res.writeHead(200, { 'content-type': TYPES[extname(file)], 'cache-control': 'no-store' }).end(body);
  } catch {
    res.writeHead(404).end('not found');
  }
}).listen(port, '127.0.0.1', () => console.log(`http://127.0.0.1:${port}/`));
