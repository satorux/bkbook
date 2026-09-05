// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux
// Copyright (c) 1997-2006 Motoyuki Kasahara

/**
 * 利用者の検索文字列を、索引が実際に持っているバイト列に変える。
 * Python 版 setword.py の移植。
 */

import { CHARCODE_ISO8859_1, EncodeError, encodeEucJp, encodeLatin1 } from "./jacode.js";
import { STYLE_CONVERT, STYLE_DELETE } from "./subbook.js";
import {
  ASCII_TO_JISX0208,
  HIRAGANA_ROW,
  JISX0201_TO_JISX0208,
  KANA_FIRST,
  KANA_LAST,
  KATAKANA_ROW,
  LONG_VOWEL,
  VOICED_CONSONANT,
} from "./tables.js";
import { EbError } from "./zio.js";

export const WORD_ALPHABET = "alphabet";
export const WORD_KANA = "kana";
export const WORD_OTHER = "other";

const MAX_WORD_LENGTH = 255;

/** 0x21 区にある、落とすべき「記号」とみなす点のバイト。 */
const MARK_CELLS = new Set([0x26, 0x3e, 0x47, 0x5d]);

/** 検索語が空か、長すぎるか、表現できない文字を含む。 */
export class WordError extends EbError {}

/** ある 1 つの索引向けに用意した検索語。 */
export class Query {
  constructor(word, canonicalized, wordCode, search) {
    this.word = word;
    this.canonicalized = canonicalized;
    this.wordCode = wordCode;
    this.search = search;
  }
}

// -- 第 1 段階: JIS X 0208 の生バイトにする ----------------------------------

/** text を JIS X 0208 の生バイトに符号化し、どんな語かを判定する。 */
export function convertToJisx0208(text) {
  let data;
  try {
    data = encodeEucJp(text);
  } catch (error) {
    if (error instanceof EncodeError) {
      throw new WordError(`${JSON.stringify(text)}: contains characters outside JIS X 0208`);
    }
    throw error;
  }
  data = stripPadding(data);

  const out = [];
  let alphabet = 0;
  let kana = 0;
  let kanji = 0;
  let position = 0;
  while (position < data.length) {
    if (MAX_WORD_LENGTH < out.length + 2) {
      throw new WordError(`${JSON.stringify(text)}: search word is too long`);
    }
    let byte = data[position++];
    if (byte === 0x09) byte = 0x20;

    let c1;
    let c2;
    if (byte >= 0x20 && byte <= 0x7e) {
      const code = ASCII_TO_JISX0208[byte - 0x20];
      c1 = code >> 8;
      c2 = code & 0xff;
    } else if (byte === 0x8e) {
      if (position >= data.length) {
        throw new WordError(`${JSON.stringify(text)}: truncated half-width katakana`);
      }
      const k = data[position++];
      if (!(k >= 0xa1 && k <= 0xdf)) {
        throw new WordError(`${JSON.stringify(text)}: bad half-width katakana`);
      }
      const code = JISX0201_TO_JISX0208[k - 0xa0];
      c1 = code >> 8;
      c2 = code & 0xff;
    } else if (byte >= 0xa1 && byte <= 0xfe) {
      if (position >= data.length) {
        throw new WordError(`${JSON.stringify(text)}: truncated multibyte character`);
      }
      const second = data[position++];
      if (second >= 0xa1 && second <= 0xfe) {
        c1 = byte & 0x7f;
        c2 = second & 0x7f;
      } else if (second >= 0x20 && second <= 0x7e) {
        c1 = byte; // ディスク固有の文字。2 バイトをそのまま通す。
        c2 = second;
      } else {
        throw new WordError(`${JSON.stringify(text)}: bad multibyte character`);
      }
    } else {
      throw new WordError(`${JSON.stringify(text)}: unsupported character`);
    }

    out.push(c1, c2);
    if (c1 === 0x23) alphabet += 1;
    else if (c1 === HIRAGANA_ROW || c1 === KATAKANA_ROW) kana += 1;
    else if (c1 !== 0x21) kanji += 1;
  }

  if (out.length === 0) throw new WordError("empty search word");

  let wordCode;
  if (alphabet === 0 && kana !== 0 && kanji === 0) wordCode = WORD_KANA;
  else if (alphabet !== 0 && kana === 0 && kanji === 0) wordCode = WORD_ALPHABET;
  else wordCode = WORD_OTHER;
  return [Uint8Array.from(out), wordCode];
}

/** 前後の空白を落とす。ASCII の空白と全角空白の両方。 */
function stripPadding(data) {
  let start = 0;
  let end = data.length;
  while (start < end) {
    if (data[start] === 0x20 || data[start] === 0x09) start += 1;
    else if (data[start] === 0xa1 && data[start + 1] === 0xa1) start += 2;
    else break;
  }
  while (start < end) {
    if (data[end - 1] === 0x20 || data[end - 1] === 0x09) end -= 1;
    else if (end - 2 >= start && data[end - 2] === 0xa1 && data[end - 1] === 0xa1) end -= 2;
    else break;
  }
  return data.subarray(start, end);
}

/** ISO 8859-1 の本向けに text を符号化する。 */
export function convertToLatin(text) {
  let data;
  try {
    data = encodeLatin1(text);
  } catch (error) {
    if (error instanceof EncodeError) {
      throw new WordError(`${JSON.stringify(text)}: contains non Latin-1 characters`);
    }
    throw error;
  }
  let start = 0;
  let end = data.length;
  while (start < end && (data[start] === 0x20 || data[start] === 0x09)) start += 1;
  while (end > start && (data[end - 1] === 0x20 || data[end - 1] === 0x09)) end -= 1;
  data = data.subarray(start, end);
  if (data.length === 0) throw new WordError("empty search word");
  if (MAX_WORD_LENGTH < data.length) {
    throw new WordError(`${JSON.stringify(text)}: search word is too long`);
  }
  return [data, WORD_ALPHABET];
}

// -- 第 2 段階: 索引ごとの正規化規則を適用する -------------------------------

/** search の正規化規則を適用し、最終的な Query を組み立てる。 */
export function fixWord(search, word, characterCode, wordCode, backward = false) {
  let canonicalized = word;

  if (characterCode === CHARCODE_ISO8859_1) {
    if (search.space === STYLE_DELETE) canonicalized = canonicalized.filter((b) => b !== 0x20);
    if (search.lower === STYLE_CONVERT) canonicalized = upperLatin(canonicalized);
  } else {
    if (search.space === STYLE_DELETE) canonicalized = deletePairs(canonicalized, [[0x21, 0x21]]);
    if (search.katakana === STYLE_CONVERT) {
      canonicalized = foldKana(canonicalized, KATAKANA_ROW, HIRAGANA_ROW);
    } else if (search.katakana === STYLE_DELETE) {
      canonicalized = foldKana(canonicalized, HIRAGANA_ROW, KATAKANA_ROW);
    }
    if (search.lower === STYLE_CONVERT) canonicalized = upperJis(canonicalized);
    if (search.mark === STYLE_DELETE) canonicalized = deleteMarks(canonicalized);
    if (search.longVowel === STYLE_CONVERT) {
      canonicalized = convertLongVowels(canonicalized);
    } else if (search.longVowel === STYLE_DELETE) {
      canonicalized = deletePairs(canonicalized, [[0x21, 0x3c]]);
    }
    if (search.doubleConsonant === STYLE_CONVERT) {
      canonicalized = mapKanaCells(canonicalized, { 0x43: 0x44 });
    }
    if (search.contractedSound === STYLE_CONVERT) {
      canonicalized = mapKanaCells(canonicalized, {
        0x63: 0x64, 0x65: 0x66, 0x67: 0x68, 0x6e: 0x6f, 0x75: 0x2b, 0x76: 0x31,
      });
    }
    if (search.smallVowel === STYLE_CONVERT) {
      canonicalized = mapKanaCells(canonicalized, {
        0x21: 0x22, 0x23: 0x24, 0x25: 0x26, 0x27: 0x28, 0x29: 0x2a,
      });
    }
    if (search.voicedConsonant === STYLE_CONVERT) {
      canonicalized = tableKanaCells(canonicalized, VOICED_CONSONANT);
    }
    if (search.pSound === STYLE_CONVERT) {
      canonicalized = mapKanaCells(canonicalized, {
        0x51: 0x4f, 0x54: 0x52, 0x57: 0x55, 0x5a: 0x58, 0x5d: 0x5b,
      });
    }
  }

  // かなの索引では、グループの構成要素は正規化前の語と比べる。
  let fixed = search.indexId === 0x70 || search.indexId === 0x90 ? word : canonicalized;
  let canonical = canonicalized;

  if (backward) {
    const reverse = characterCode === CHARCODE_ISO8859_1 ? reverseLatin : reverseJisx0208;
    fixed = reverse(fixed);
    canonical = reverse(canonical);
  }
  return new Query(Uint8Array.from(fixed), Uint8Array.from(canonical), wordCode, search);
}

function reverseLatin(data) {
  return Uint8Array.from(data).reverse();
}

/** 2 バイト——1 文字——単位で逆順にする。 */
function reverseJisx0208(data) {
  const out = [];
  for (let i = (data.length & ~1) - 2; i >= 0; i -= 2) out.push(data[i], data[i + 1]);
  return Uint8Array.from(out);
}

function mapPairs(data, transform) {
  const out = [];
  for (let i = 0; i + 1 < data.length; i += 2) {
    const pair = transform(data[i], data[i + 1]);
    if (pair !== null) out.push(pair[0], pair[1]);
  }
  return Uint8Array.from(out);
}

function deletePairs(data, pairs) {
  return mapPairs(data, (c1, c2) => (pairs.some(([p1, p2]) => p1 === c1 && p2 === c2) ? null : [c1, c2]));
}

function deleteMarks(data) {
  return mapPairs(data, (c1, c2) => (c1 === 0x21 && MARK_CELLS.has(c2) ? null : [c1, c2]));
}

function foldKana(data, fromRow, toRow) {
  return mapPairs(data, (c1, c2) => [c1 === fromRow && c2 >= KANA_FIRST && c2 <= KANA_LAST ? toRow : c1, c2]);
}

function upperJis(data) {
  return mapPairs(data, (c1, c2) => [c1, c1 === 0x23 && c2 >= 0x61 && c2 <= 0x7a ? c2 - 0x20 : c2]);
}

function isKanaRow(c1) {
  return c1 === HIRAGANA_ROW || c1 === KATAKANA_ROW;
}

function mapKanaCells(data, mapping) {
  return mapPairs(data, (c1, c2) => [c1, isKanaRow(c1) && c2 in mapping ? mapping[c2] : c2]);
}

function tableKanaCells(data, table) {
  return mapPairs(data, (c1, c2) => [
    c1,
    isKanaRow(c1) && c2 >= KANA_FIRST && c2 <= KANA_LAST ? table[c2 - KANA_FIRST] : c2,
  ]);
}

/** ー を、直前のかなの母音に置き換える。 */
function convertLongVowels(data) {
  let p1 = 0;
  let p2 = 0;
  return mapPairs(data, (c1, c2) => {
    if (c1 === 0x21 && c2 === 0x3c && isKanaRow(p1) && p2 >= KANA_FIRST && p2 <= KANA_LAST) {
      c1 = p1;
      c2 = LONG_VOWEL[p2 - KANA_FIRST];
    }
    p1 = c1;
    p2 = c2;
    return [c1, c2];
  });
}

function upperLatin(data) {
  return data.map((byte) =>
    (byte >= 0x61 && byte <= 0x7a) || (byte >= 0xe0 && byte <= 0xf6) || (byte >= 0xf8 && byte <= 0xfe)
      ? byte - 0x20
      : byte,
  );
}
