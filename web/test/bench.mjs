// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux

/**
 * TUI と同じ動作を HTTP 越しに模擬し、キー入力ごとのリクエスト数と所要時間を測る。
 *
 *     $ node web/test/bench.mjs https://example.net/eb/ [--auth user:pass] [--no-body]
 *
 * 開いている辞書すべてに 40 件ずつ検索し、見出し 30 件と本文 1 件を読む。
 * 本文 1 件目には stop code の推定が含まれるので、その分は別に出す。
 */

import { HttpFs, PlainTextRenderer, openCollection, parsePattern, readHeading, readText, search } from "../bkbook/index.js";

const args = process.argv.slice(2);
const base = args.find((a) => !a.startsWith("--")) || "http://localhost:8000/";
const auth = args.includes("--auth") ? args[args.indexOf("--auth") + 1] : null;
const withBody = !args.includes("--no-body");
const headers = auth ? { Authorization: `Basic ${Buffer.from(auth).toString("base64")}` } : {};
const fetchImpl = (url, init = {}) => fetch(url, { ...init, headers: { ...(init.headers || {}), ...headers } });

const t0 = Date.now();
const fs = await HttpFs.fromManifestUrl(base, new URL("manifest.txt", base).href, { fetch: fetchImpl });
const { sources } = await openCollection(fs, [""]);
console.log(`startup: ${sources.length} subbooks, ${fs.cache.requests} requests, ${Date.now() - t0}ms`);

const QUERIES = ["l", "li", "lig", "ligh", "light", "ひ", "ひか", "ひかり", "光", "*ization", "book"];
for (const q of QUERIES) {
  const before = fs.cache.requests;
  const t = Date.now();
  const { word, backward } = parsePattern(q);
  const results = await Promise.all(sources.map((s) => search(s.subbook, word, { limit: 40, backward }).catch(() => [])));
  const searched = Date.now();
  const searchRequests = fs.cache.requests - before;

  // 交互に並べて上位 30 件の見出しを読む
  const entries = [];
  const cursors = results.map(() => 0);
  for (let took = true; took; ) {
    took = false;
    results.forEach((hits, i) => {
      if (cursors[i] < hits.length) {
        entries.push([sources[i], hits[cursors[i]]]);
        cursors[i] += 1;
        took = true;
      }
    });
  }
  const shown = entries.slice(0, 30);
  await Promise.all(shown.map(([s, hit]) => readHeading(s.subbook, hit.heading, { renderer: new PlainTextRenderer({ gaiji: s.gaiji }) })));
  const headed = Date.now();
  const headingRequests = fs.cache.requests - before - searchRequests;

  let bodyNote = "";
  if (withBody && entries.length) {
    const [s, hit] = entries[0];
    const b = fs.cache.requests;
    const known = s.subbook.stopCode !== undefined;
    await readText(s.subbook, hit.text, { renderer: new PlainTextRenderer({ gaiji: s.gaiji }) });
    bodyNote = ` body=${fs.cache.requests - b}req/${Date.now() - headed}ms${known ? "" : " (stop code の推定を含む)"}`;
  }
  console.log(
    `${q.padEnd(10)} hits=${String(entries.length).padStart(4)} ` +
      `search=${String(searchRequests).padStart(3)}req/${String(searched - t).padStart(4)}ms ` +
      `headings=${String(headingRequests).padStart(3)}req/${String(headed - searched).padStart(4)}ms${bodyNote}`,
  );
}
console.log(`total: ${fs.cache.requests} requests, ${Date.now() - t0}ms`);
