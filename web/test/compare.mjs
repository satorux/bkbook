// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux

/**
 * golden.py が書き出した正解と、JavaScript 版の答えを突き合わせる。
 *
 *     $ node web/test/compare.mjs golden.json ~/eb            # ローカルファイル
 *     $ node web/test/compare.mjs golden.json http://localhost:8000/   # HTTP Range
 *
 * HTTP のときは serve.mjs が動いている前提で、manifest.txt をそこから取る。
 */

import { createHash } from "node:crypto";
import { EbError } from "../bkbook/zio.js";
import { readFile } from "node:fs/promises";

import { Book, HttpFs, NodeFs, findBooks } from "../bkbook/index.js";
import { iterIndex, iterKeywordHits, search, searchKeyword, searchMulti, Position, parsePattern } from "../bkbook/search.js";
import { PlainTextRenderer, readHeading, readText } from "../bkbook/text.js";
import * as appendixModule from "../bkbook/appendix.js";
import * as gaijiModule from "../bkbook/gaiji.js";
import * as stopcode from "../bkbook/stopcode.js";
import { categorise, nearbyAppendix, readable } from "../bkbook/collection.js";
import { FontError, fontSet } from "../bkbook/font.js";

const [goldenPath, root] = process.argv.slice(2);
if (!goldenPath || !root) {
  console.error("usage: compare.mjs golden.json <directory | url>");
  process.exit(2);
}
const golden = JSON.parse(await readFile(goldenPath, "utf8"));
const http = /^https?:/.test(root);
const fs = http ? await HttpFs.fromManifestUrl(root, new URL("manifest.txt", root).href) : new NodeFs();
const base = http ? "" : root;

let failures = 0;
let checks = 0;
function check(label, actual, expected) {
  checks += 1;
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    failures += 1;
    console.log(`FAIL ${label}\n  js: ${a.slice(0, 300)}\n  py: ${e.slice(0, 300)}`);
  }
}

const sha = (data) => createHash("sha256").update(data).digest("hex");
const hitList = (hits) => hits.map((h) => [h.text.page, h.text.offset, h.heading.page, h.heading.offset]);
const searchDict = (s) =>
  s && {
    index_id: s.indexId, start_page: s.startPage, end_page: s.endPage,
    katakana: s.katakana, lower: s.lower, mark: s.mark, long_vowel: s.longVowel,
    double_consonant: s.doubleConsonant, contracted_sound: s.contractedSound,
    small_vowel: s.smallVowel, voiced_consonant: s.voicedConsonant, p_sound: s.pSound, space: s.space,
  };

const tableSha = (table) => {
  const digest = createHash("sha256");
  for (const key of [...table.keys()].sort()) digest.update(`${key}\t${table.get(key)}\n`);
  return digest.digest("hex");
};

async function textRecord(subbook, position, gaiji, heading = false) {
  let result;
  try {
    result = await (heading ? readHeading : readText)(subbook, position, { renderer: new PlainTextRenderer({ gaiji }) });
  } catch (error) {
    if (!(error instanceof EbError)) throw error;
    return { error: error.constructor.name };
  }
  return {
    text: result.text,
    readable: readable(result.text),
    stop: result.stop,
    next: [result.nextPosition.page, result.nextPosition.offset],
    references: result.references.map((r) => [r.textOf(result.text), r.position.page, r.position.offset]),
    candidates: result.candidates.map((c) => [c.textOf(result.text), c.position === null ? null : [c.position.page, c.position.offset]]),
    unknown_gaiji: result.unknownGaiji,
  };
}

async function fontRecord(subbook, narrow) {
  let fonts;
  try {
    fonts = await fontSet(subbook, narrow);
  } catch (error) {
    if (error instanceof FontError) return null;
    throw error;
  }
  const codes = [...fonts.codes()];
  const digest = createHash("sha256");
  let errors = 0;
  for (const code of [...codes.slice(0, 64), ...codes.slice(-8)]) {
    try {
      digest.update((await fonts.bitmap(code)).data);
    } catch (error) {
      if (!(error instanceof FontError)) throw error;
      errors += 1;
    }
  }
  return {
    start: fonts.start, end: fonts.end, count: fonts.count, width: fonts.width, height: fonts.height,
    codes: codes.length, first: codes.slice(0, 3), last: codes.slice(-3), sha256: digest.digest("hex"), errors,
  };
}

async function record(fn) {
  try {
    return { hits: hitList(await fn()) };
  } catch (error) {
    if (error.constructor.name === "WordError" || error.constructor.name === "NoSuchSearchError") {
      return { error: error.constructor.name };
    }
    throw error;
  }
}

const started = Date.now();
const paths = await findBooks(fs, base);
check("book list", paths.map((p) => (base ? p.slice(base.length).replace(/^\//, "") : p)), golden.books.map((b) => b.path));

for (const expectedBook of golden.books) {
  const path = base ? `${base}/${expectedBook.path}` : expectedBook.path;
  const book = await Book.open(fs, path);
  const label = expectedBook.path;
  check(`${label} disc`, [book.discCode, book.characterCode, book.epwingVersion], [expectedBook.disc, expectedBook.character_code, expectedBook.epwing_version]);
  check(`${label} subbook count`, book.subbooks.length, expectedBook.subbooks.length);

  for (const expected of expectedBook.subbooks) {
    const subbook = book.subbooks[expected.code];
    const name = `${label}/${expected.directory}`;
    await subbook.load();
    check(`${name} title`, [subbook.title, subbook.directoryName, subbook.indexPage], [expected.title, expected.directory, expected.index_page]);

    const zio = await subbook.zio();
    check(`${name} zio`, [zio.code, zio.sliceSize, zio.fileSize], [expected.zio.code, expected.zio.slice_size, expected.zio.file_size]);
    for (const [page, digest] of Object.entries(expected.zio.pages)) {
      check(`${name} page ${page}`, sha(await zio.readPage(Number(page))), digest);
    }

    const searches = Object.fromEntries(Object.entries(subbook.searches).map(([k, s]) => [k, searchDict(s)]));
    check(`${name} searches`, searches, expected.searches);
    const multis = (await subbook.multis()).map((m) => ({
      label: m.label,
      entries: m.entries.map((e) => ({ label: e.label, index: searchDict(e.index), candidates: e.candidates.map((c) => c.indexId) })),
    }));
    check(`${name} multis`, multis, expected.multis);
    const fonts = (table) => Object.fromEntries(Object.entries(table).map(([k, f]) => [k, [f.page, f.fileName]]));
    check(`${name} fonts`, { narrow: fonts(subbook.narrowFonts), wide: fonts(subbook.wideFonts) }, expected.fonts);

    for (const q of expected.queries) {
      const actual = await record(() => search(subbook, q.text, { exact: q.exact, limit: 50, backward: q.backward }));
      check(`${name} search ${JSON.stringify(q.text)} exact=${q.exact} backward=${q.backward}`, actual, q.hits ? { hits: q.hits } : { error: q.error });
    }

    for (const k of expected.keywords) {
      let actual;
      try {
        const hits = [];
        for await (const hit of iterKeywordHits(subbook, k.words[0])) hits.push(hit);
        let positions = null;
        if (k.words.length > 1) {
          positions = (await searchKeyword(subbook, k.words)).map((h) => [h.text.page, h.text.offset]);
          positions.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
        }
        actual = { hits: hitList(hits).slice(0, 500), count: hits.length, positions };
      } catch (error) {
        if (!["WordError", "NoSuchSearchError"].includes(error.constructor.name)) throw error;
        actual = { error: error.constructor.name };
      }
      const wanted = k.error ? { error: k.error } : { hits: k.hits, count: k.count, positions: k.positions };
      check(`${name} keyword ${JSON.stringify(k.words)}`, actual, wanted);
    }

    for (const m of expected.multi_queries) {
      const actual = await record(() => searchMulti(subbook, 0, m.words, { limit: 50 }));
      check(`${name} multi ${JSON.stringify(m.words)}`, actual, m.hits ? { hits: m.hits } : { error: m.error });
    }

    for (const [indexName, wanted] of Object.entries(expected.indexes)) {
      let actual;
      try {
        const digest = createHash("sha256");
        let count = 0;
        for await (const hit of iterIndex(subbook, indexName)) {
          digest.update(`${hit.text.page}:${hit.text.offset}/${hit.heading.page}:${hit.heading.offset}\n`);
          count += 1;
        }
        actual = { count, sha256: digest.digest("hex") };
      } catch (error) {
        if (error.constructor.name !== "NoSuchSearchError") throw error;
        actual = { error: "NoSuchSearchError" };
      }
      check(`${name} index ${indexName}`, actual, wanted);
    }
    // -- 本文層 --
    if (expected.texts !== undefined) {
      let appendix = null;
      const source = await nearbyAppendix(fs, path);
      if (source) {
        try {
          appendix = await appendixModule.forSubbook(fs, source, subbook);
        } catch (error) {
          if (!(error instanceof appendixModule.AppendixNotFoundError)) throw error;
        }
      }
      check(`${name} appendix`, appendix === null ? null : {
        stop_code: appendix.stopCode, narrow: appendix.narrow.size, wide: appendix.wide.size, sha256: tableSha(appendix.asGaijiTable()),
      }, expected.appendix);
      const gaiji = gaijiModule.resolve(subbook, null, appendix);
      check(`${name} gaiji`, { count: gaiji.size, sha256: tableSha(gaiji) }, expected.gaiji);
      check(`${name} category`, await categorise(subbook), expected.category);
      check(`${name} inferred stop code`, await stopcode.infer(subbook), expected.inferred_stop_code);
      if (appendix !== null && appendix.stopCode !== null) subbook.stopCode = appendix.stopCode;
      check(`${name} stop code`, await subbook.inferStopCode(), expected.stop_code);

      const texts = [];
      for (const query of [...new Set(expected.texts.map((t) => t.query))]) {
        let hits;
        try {
          const { word, backward } = parsePattern(query);
          hits = await search(subbook, word, { limit: 5, backward });
        } catch (error) {
          if (["WordError", "NoSuchSearchError"].includes(error.constructor.name)) continue;
          throw error;
        }
        for (const h of hits) {
          texts.push({
            query, hit: hitList([h])[0],
            heading: await textRecord(subbook, h.heading, gaiji, true),
            body: await textRecord(subbook, h.text, gaiji),
          });
        }
      }
      check(`${name} texts count`, texts.length, expected.texts.length);
      texts.forEach((t, i) => {
        const e = expected.texts[i] || {};
        check(`${name} heading ${t.query}#${i}`, t.heading, e.heading);
        check(`${name} body ${t.query}#${i}`, t.body, e.body);
      });
      const menu = subbook.searches.menu;
      check(`${name} menu`, menu ? await textRecord(subbook, new Position(menu.startPage, 0), gaiji) : null, expected.menu);
      check(`${name} fonts16`, { narrow: await fontRecord(subbook, true), wide: await fontRecord(subbook, false) }, expected.fonts16);
    }
    console.error(`${name} ok so far: ${checks} checks, ${failures} failures`);
  }
  await book.close();
}

if (golden.gaiji_builtin) {
  const actual = Object.fromEntries(
    Object.entries(gaijiModule.BUILTIN).map(([title, table]) => [title, { count: Object.keys(table).length, sha256: tableSha(new Map(Object.entries(table))) }]),
  );
  check("gaiji builtin", actual, golden.gaiji_builtin);
}

const seconds = ((Date.now() - started) / 1000).toFixed(1);
const requests = http ? `, ${fs.cache.requests} HTTP requests` : "";
console.log(`${checks} checks, ${failures} failures, ${seconds}s${requests}`);
process.exit(failures ? 1 : 0);
