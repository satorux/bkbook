// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux

/**
 * Range リクエストに応える、開発用の静的ファイルサーバー。
 *
 *     $ node web/test/serve.mjs [directory] [port]
 *
 * Python の http.server は Range を解さないので、ブラウザ版を手元で試すには
 * これを使う。`manifest.txt` がなければディレクトリを歩いて動的に作る。
 * 本番のさくらでは `find . -type f > manifest.txt` で同じものを置く。
 */

import { createServer } from "node:http";
import { createReadStream } from "node:fs";
import { readdir, stat } from "node:fs/promises";
import { join, normalize, resolve, sep } from "node:path";

const root = resolve(process.argv[2] || ".");
const port = Number(process.argv[3] || 8000);

export async function walk(directory, prefix = "") {
  const out = [];
  for (const entry of (await readdir(directory, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
    if (entry.name.startsWith(".")) continue;
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
    const info = entry.isSymbolicLink() ? await stat(join(directory, entry.name)) : entry;
    if (info.isDirectory()) out.push(...(await walk(join(directory, entry.name), relative)));
    else if (info.isFile()) out.push(relative);
  }
  return out;
}

const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css", ".json": "application/json", ".txt": "text/plain; charset=utf-8" };

createServer(async (request, response) => {
  const url = new URL(request.url, "http://localhost");
  const path = decodeURIComponent(url.pathname);
  const file = normalize(join(root, path));
  if (!file.startsWith(root + sep) && file !== root) {
    response.writeHead(403).end();
    return;
  }
  let info;
  try {
    info = await stat(file);
  } catch {
    if (path.endsWith("/manifest.txt")) {
      // どのディレクトリでも、manifest.txt がなければその場で作る。
      const directory = file.slice(0, -"/manifest.txt".length);
      const body = (await walk(directory)).join("\n") + "\n";
      response.writeHead(200, { "Content-Type": "text/plain; charset=utf-8" });
      response.end(body);
      return;
    }
    response.writeHead(404).end();
    return;
  }
  let target = file;
  if (info.isDirectory()) {
    // ディレクトリなら index.html を返す。
    if (!path.endsWith("/")) {
      response.writeHead(301, { Location: path + "/" }).end();
      return;
    }
    target = join(file, "index.html");
    try {
      info = await stat(target);
    } catch {
      response.writeHead(403).end();
      return;
    }
  }
  const type = TYPES[target.slice(target.lastIndexOf("."))] || "application/octet-stream";
  const range = request.headers.range && /^bytes=(\d*)-(\d*)$/.exec(request.headers.range);
  if (range) {
    let start = range[1] ? Number(range[1]) : info.size - Number(range[2]);
    let end = range[2] && range[1] ? Math.min(Number(range[2]), info.size - 1) : info.size - 1;
    if (start >= info.size) {
      response.writeHead(416, { "Content-Range": `bytes */${info.size}` }).end();
      return;
    }
    response.writeHead(206, {
      "Content-Type": type,
      "Content-Length": end - start + 1,
      "Content-Range": `bytes ${start}-${end}/${info.size}`,
      "Accept-Ranges": "bytes",
    });
    if (request.method === "HEAD") return response.end();
    createReadStream(target, { start, end }).pipe(response);
    return;
  }
  response.writeHead(200, { "Content-Type": type, "Content-Length": info.size, "Accept-Ranges": "bytes" });
  if (request.method === "HEAD") return response.end();
  createReadStream(target).pipe(response);
}).listen(port, () => console.log(`serving ${root} at http://localhost:${port}/`));
