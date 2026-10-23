// Minimal static server for the Playwright run (serves this directory only).
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL(".", import.meta.url));
const types = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css" };
const allowed = new Set(["/index.html", "/ltfs.js", "/viewer.js", "/style.css"]);
const port = Number(process.env.PORT || 4173);

createServer(async (req, res) => {
  let path = new URL(req.url, "http://x").pathname;
  if (path === "/") path = "/index.html";
  if (!allowed.has(path)) { res.writeHead(404).end(); return; }
  try {
    const body = await readFile(join(root, normalize(path)));
    res.writeHead(200, { "content-type": types[extname(path)] || "application/octet-stream" }).end(body);
  } catch {
    res.writeHead(404).end();
  }
}).listen(port, "127.0.0.1", () => console.log(`serving ${root} on http://127.0.0.1:${port}/`));
