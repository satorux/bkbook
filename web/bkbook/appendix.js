// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux
// Copyright (c) 1997-2006 Motoyuki Kasahara

/**
 * appendix ファイル——辞書に添えて配布される外字表。Python 版 appendix.py の移植。
 * 人が読める *.app のソース形式を読む。
 */

import { fixupDecoded } from "./jacode.js";
import { EbError } from "./zio.js";

/** 試す文字コード。順に試す。 */
const ENCODINGS = ["iso-2022-jp", "euc-jp", "utf-8"];

export class AppendixError extends EbError {}
/** このサブブック用の appendix がここにはない。 */
export class AppendixNotFoundError extends AppendixError {}

/** サブブック 1 つ分の appendix。 */
export class Appendix {
  constructor(path = "") {
    this.characterCode = "jisx0208";
    this.stopCode = null;
    this.narrow = new Map();
    this.wide = new Map();
    this.path = path;
  }

  /** レンダラが受け取る Map（"ha121" → text）に変換する。 */
  asGaijiTable() {
    const table = new Map();
    for (const [code, text] of this.narrow) table.set(`h${code.toString(16).padStart(4, "0")}`, text);
    for (const [code, text] of this.wide) table.set(`z${code.toString(16).padStart(4, "0")}`, text);
    return table;
  }
}

/** *.app ファイルの中身を解析する。 */
export function parse(text, path = "") {
  const appendix = new Appendix(path);
  let block = null;
  const lines = text.split(/\r?\n|\r/);
  lines.forEach((rawLine, index) => {
    const number = index + 1;
    const line = rawLine.trimStart().startsWith("#") ? rawLine.split("#", 1)[0] : rawLine;
    if (!line.trim()) return;
    const stripped = line.trim();
    const m = /^(\S+)\s*(.*)$/s.exec(stripped);
    const keyword = m[1];
    const value = m[2];

    if (keyword === "begin") {
      if (value !== "narrow" && value !== "wide") throw new AppendixError(`${path}:${number}: unknown block ${JSON.stringify(value)}`);
      block = value;
    } else if (keyword === "end") {
      block = null;
    } else if (keyword === "character-code") {
      appendix.characterCode = value;
    } else if (keyword === "stop-code") {
      appendix.stopCode = parseStopCode(value, path, number);
    } else if (keyword === "range-start" || keyword === "range-end") {
      // コンパイラのための宣言。
    } else if (keyword.startsWith("0x")) {
      if (block === null) throw new AppendixError(`${path}:${number}: character outside a block`);
      if (!/^0x[0-9a-fA-F]+$/.test(keyword)) throw new AppendixError(`${path}:${number}: bad code ${JSON.stringify(keyword)}`);
      (block === "narrow" ? appendix.narrow : appendix.wide).set(parseInt(keyword, 16), value);
    } else {
      throw new AppendixError(`${path}:${number}: unknown directive ${JSON.stringify(keyword)}`);
    }
  });
  return appendix;
}

function parseStopCode(value, path, number) {
  const parts = value.split(/\s+/).filter(Boolean);
  if (parts.length !== 2) throw new AppendixError(`${path}:${number}: stop-code needs two values`);
  const numbers = parts.map((p) => (/^0x[0-9a-fA-F]+$/.test(p) ? parseInt(p, 16) : /^[0-9]+$/.test(p) ? Number(p) : NaN));
  if (numbers.some(Number.isNaN)) throw new AppendixError(`${path}:${number}: bad stop-code ${JSON.stringify(value)}`);
  return numbers;
}

/** *.app ファイルを、文字コードを判定しながら読む。 */
export async function load(fs, path) {
  const file = await fs.open(path);
  let raw;
  try {
    raw = await file.read(0, await file.size());
  } finally {
    await file.close();
  }
  for (const encoding of ENCODINGS) {
    try {
      const text = new TextDecoder(encoding, { fatal: true }).decode(raw);
      return parse(encoding === "utf-8" ? text : fixupDecoded(text), path);
    } catch (error) {
      if (error instanceof TypeError) continue; // 復号できなかった
      throw error;
    }
  }
  throw new AppendixError(`${path}: could not decode as any of ${ENCODINGS.join(", ")}`);
}

async function matchDirectory(fs, parent, name) {
  const entries = await fs.listdir(parent);
  if (entries === null) return null;
  const wanted = name.toLowerCase();
  for (const entry of entries) {
    if (entry.toLowerCase() === wanted) {
      const path = fs.join(parent, entry);
      if (await fs.isdir(path)) return path;
    }
  }
  return null;
}

async function matchFile(fs, directory, wanted) {
  const entries = await fs.listdir(directory);
  if (entries === null) return null;
  for (const entry of entries) {
    if (entry.toLowerCase() === wanted) {
      const path = fs.join(directory, entry);
      if (!(await fs.isdir(path))) return path;
    }
  }
  return null;
}

async function isFile(fs, path) {
  if (await fs.isdir(path)) return false;
  const slash = path.lastIndexOf("/");
  const parent = slash >= 0 ? path.slice(0, slash) : "";
  const name = slash >= 0 ? path.slice(slash + 1) : path;
  const entries = await fs.listdir(parent);
  return entries !== null && entries.includes(name);
}

const basename = (path) => path.replace(/\/+$/, "").split("/").pop();

/** subbook のものでありうる *.app を列挙する。 */
export async function candidates(fs, where, subbook) {
  if (await isFile(fs, where)) return [where];
  if (!(await fs.isdir(where))) return [];

  const wanted = `${subbook.directoryName.toLowerCase()}.app`;
  const here = await matchFile(fs, where, wanted);
  if (here) return [here];

  const paired = await matchDirectory(fs, where, basename(subbook.book.path));
  if (paired) {
    const match = await matchFile(fs, paired, wanted);
    if (match) return [match];
  }

  const found = [];
  for (const entry of ((await fs.listdir(where)) || []).sort()) {
    const nested = fs.join(where, entry);
    if (await fs.isdir(nested)) {
      const match = await matchFile(fs, nested, wanted);
      if (match) found.push(match);
    }
  }
  return found;
}

/** サブブックの *.app を 1 つ特定する。決まらなければ null。 */
export async function find(fs, where, subbook) {
  const found = await candidates(fs, where, subbook);
  return found.length === 1 ? found[0] : null;
}

/** where から subbook の appendix を読み込む。 */
export async function forSubbook(fs, where, subbook) {
  const found = await candidates(fs, where, subbook);
  if (found.length === 0) {
    throw new AppendixNotFoundError(`${where}: no ${subbook.directoryName.toLowerCase()}.app for 「${subbook.title}」`);
  }
  if (found.length > 1) {
    throw new AppendixError(
      `${where}: several appendices are named after the ${JSON.stringify(subbook.directoryName)} directory; pass the right one directly:\n  ${found.join("\n  ")}`,
    );
  }
  return load(fs, found[0]);
}
