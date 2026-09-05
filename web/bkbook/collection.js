// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux

/**
 * 何冊もの辞書をまとめて開く。Python 版 cli.py の tui まわりの移植。
 *
 * 辞書の種類（英 / 国 / 百）の判定、並び順、appendix と外字表の割り当て、
 * それに本文を読みやすくする整形。
 */

import * as appendixModule from "./appendix.js";
import { Book, findBooks } from "./book.js";
import * as gaijiModule from "./gaiji.js";
import { iterIndex } from "./search.js";
import { readHeading } from "./text.js";
import { EbError } from "./zio.js";

/** タイトルに出てくると辞書の種類が分かる語。 */
const CATEGORY_HINTS = [
  ["百", ["百科", "ペディア", "ブリタニカ", "encyclop"]],
  ["英", ["英和", "和英", "英英", "英語", "thesaurus", "dictionary", "roget"]],
];
export const DEFAULT_CATEGORY = "国";
export const CATEGORY_ORDER = ["英", "国", "百"];

/** ヒットを対で求める本。 */
export const PAIRED_TITLES = new Set(["リーダーズ＋プラス英和辞典"]);

// -- 整形 -------------------------------------------------------------------------

/** 全角 ASCII を ASCII に、全角空白を空白に戻す。意図して NFKC より狭い。 */
export function foldFullWidth(text) {
  return text.replace(/[！-～]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xfee0)).replace(/　/g, " ");
}

const MEDIA_TAG = "<(?:sound|image)=[0-9A-Fa-f]+:[0-9A-Fa-f]+>";
const MEDIA_MARKER = new RegExp(`^[ 　]*${MEDIA_TAG}[ 　]*\\n?|[ 　]*${MEDIA_TAG}`, "gm");

const PITCH_RISE = "┏";
const PITCH_FALL = "┓";
const OVERLINE = "̅";

function inWord(character) {
  return /[\p{L}\p{N}'’\-−ー]/u.test(character);
}

/** 研究社新和英大辞典 のアクセントの角を、語の高い部分の上線に戻す。 */
export function pitchAccent(text) {
  if (!text.includes(PITCH_RISE) && !text.includes(PITCH_FALL)) return text;
  const out = [];
  let high = false;
  for (const character of text) {
    if (character === PITCH_RISE) {
      high = true;
    } else if (character === PITCH_FALL) {
      if (high) {
        high = false;
      } else {
        for (let index = out.length - 1; index >= 0; index--) {
          if (!inWord(out[index][0])) break;
          out[index] += OVERLINE;
        }
      }
    } else {
      if (!inWord(character)) high = false;
      out.push(high ? character + OVERLINE : character);
    }
  }
  return out.join("");
}

/** 項目本文を読みやすい形にする。全角を畳み、音声と画像の印を落とし、アクセントを上線に。 */
export function readable(text) {
  return pitchAccent(foldFullWidth(text).replace(MEDIA_MARKER, ""));
}

// -- 種類と並び順 -----------------------------------------------------------------

/** タイトルから辞書を 英 / 国 / 百 に振り分ける。分からなければ 国。 */
export function guessCategory(title) {
  const lowered = foldFullWidth(title).toLowerCase();
  for (const [category, hints] of CATEGORY_HINTS) {
    if (hints.some((hint) => lowered.includes(hint))) return category;
  }
  return DEFAULT_CATEGORY;
}

/** 辞書を 英 / 国 / 百 に振り分ける。タイトルが黙っているときは見出し語を読む。 */
export async function categorise(subbook) {
  const guess = guessCategory(subbook.title || "");
  if (guess !== DEFAULT_CATEGORY) return guess;
  const headwords = [];
  try {
    for await (const hit of iterIndex(subbook)) {
      if (headwords.length >= 8) break;
      headwords.push((await readHeading(subbook, hit.heading)).text);
    }
  } catch (error) {
    if (error instanceof EbError) return guess;
    throw error;
  }
  let latin = 0;
  for (const word of headwords) {
    const letters = [...foldFullWidth(word)].filter((c) => /\p{L}/u.test(c));
    if (letters.length && letters.every((c) => c.charCodeAt(0) < 0x80)) latin += 1;
  }
  return latin * 2 > headwords.length ? "英" : guess;
}

export function categoryOrder(category) {
  const index = CATEGORY_ORDER.indexOf(category);
  return index >= 0 ? index : CATEGORY_ORDER.length;
}

/** その本の見出し語索引の大きさ。ページ数で測る。 */
export function headwordSize(subbook) {
  return Object.entries(subbook.searches)
    .filter(([name]) => name.startsWith("word_"))
    .reduce((sum, [, search]) => sum + search.pageCount, 0);
}

/** 順序ファイルの中身を解析する。1 行に 1 つ、辞書のディレクトリ名。 */
export function parseOrder(text) {
  const names = [];
  for (const line of text.split(/\r?\n/)) {
    const name = line.split("#", 1)[0].trim().replace(/\/+$/, "");
    if (name) names.push(name.split("/").pop());
  }
  return names;
}

const basename = (path) => path.replace(/\/+$/, "").split("/").pop();

/** 一覧の中でその辞書が来る位置。先に読みたいものから。 */
export function readingOrder(category, subbook, path = "", order = []) {
  const name = basename(path);
  const index = order.indexOf(name);
  return [index >= 0 ? index : order.length, categoryOrder(category), -headwordSize(subbook)];
}

// -- 開く -------------------------------------------------------------------------

/** 開いている辞書 1 冊。 */
export class Source {
  constructor({ label, subbook, title = "", category = "", gaiji = new Map(), appendix = null }) {
    this.label = label;
    this.subbook = subbook;
    this.title = title || label;
    this.category = category;
    this.gaiji = gaiji;
    this.appendix = appendix;
    this.hitsPerRound = PAIRED_TITLES.has(this.title) ? 2 : 1;
  }
}

/** コレクションがディスクの隣に置いている appendix ディレクトリ。なければ null。 */
export async function nearbyAppendix(fs, bookPath) {
  const parent = bookPath.replace(/\/+$/, "").split("/").slice(0, -1).join("/");
  const candidate = fs.join(parent, "appendix");
  return (await fs.isdir(candidate)) ? candidate : null;
}

/**
 * paths にある本をすべて開き、Source の一覧にして返す。
 * Python 版 command_tui と同じ手順: appendix を探して stop code と外字表を
 * 与え、種類を判定し、order（順序ファイルの名前の並び）で並べ替える。
 */
export async function openCollection(fs, paths, { order = [], appendix = null, gaiji = null, notes = [], progress = null } = {}) {
  const specs = [];
  for (const path of paths) {
    const found = await findBooks(fs, path);
    if (found.length === 0) throw new EbError(`${path}: no dictionary here`);
    for (const bookPath of found) specs.push([bookPath, appendix || (await nearbyAppendix(fs, bookPath))]);
  }

  const books = [];
  const sources = [];
  for (const [index, [path, appendixSource]] of specs.entries()) {
    if (progress) progress(index, specs.length, path);
    const book = await Book.open(fs, path);
    books.push(book);
    for (const subbook of book.subbooks) {
      await subbook.load();
      let found = null;
      if (appendixSource) {
        try {
          found = await appendixModule.forSubbook(fs, appendixSource, subbook);
        } catch (error) {
          if (error instanceof appendixModule.AppendixNotFoundError) {
            // そもそも大半の辞書にはない
          } else if (error instanceof EbError) {
            notes.push(String(error.message));
          } else {
            throw error;
          }
        }
      }
      if (found !== null && found.stopCode !== null) subbook.stopCode = found.stopCode;
      const title = subbook.title || subbook.directoryName;
      sources.push(
        new Source({
          label: title,
          subbook,
          title,
          category: await categorise(subbook),
          gaiji: gaijiModule.resolve(subbook, gaiji, found),
          appendix: found,
        }),
      );
    }
  }
  const keyed = sources.map((source) => [readingOrder(source.category, source.subbook, source.subbook.book.path, order), source]);
  keyed.sort(([a], [b]) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2]);
  return { sources: keyed.map(([, source]) => source), books };
}
