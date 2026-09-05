// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux
// Copyright (c) 1997-2006 Motoyuki Kasahara

/**
 * 検索語とインデックス項目を比べる関数群。Python 版 match.py の移植。
 *
 * 0 なら一致、正なら語のほうが後ろ、負なら語のほうが前。
 * 終端の先を読むと 0 が返るのは、C 版の NUL 終端と同じ意味になる。
 */

import { HIRAGANA_ROW, KATAKANA_ROW } from "./tables.js";

const at = (data, index) => (index < data.length ? data[index] : 0);

/** 前方一致。語で始まる項目をヒットとする。 */
export function matchWord(word, pattern) {
  const length = pattern.length;
  for (let i = 0; ; i++) {
    if (length <= i) return at(word, i);
    if (i >= word.length) return 0;
    if (word[i] !== pattern[i]) return word[i] - pattern[i];
  }
}

/** 中間ノードで子ページを選ぶときに使う前方一致の比較。 */
export function preMatchWord(word, pattern) {
  const length = pattern.length;
  for (let i = 0; ; i++) {
    if (length <= i || i >= word.length) return 0;
    if (word[i] !== pattern[i]) return word[i] - pattern[i];
  }
}

function exactMatch(word, pattern, padding, pre) {
  const length = pattern.length;
  for (let i = 0; ; i++) {
    if (length <= i) return pre ? 0 : at(word, i);
    if (i >= word.length) {
      while (i < length && pattern[i] === padding) i++;
      return i - length;
    }
    if (word[i] !== pattern[i]) return word[i] - pattern[i];
  }
}

/** 完全一致。項目末尾の NUL の詰め物は無視する。 */
export const exactMatchWordJis = (word, pattern) => exactMatch(word, pattern, 0, false);
export const exactPreMatchWordJis = (word, pattern) => exactMatch(word, pattern, 0, true);
/** ISO 8859-1 のディスク用の完全一致。末尾の空白は詰め物。 */
export const exactMatchWordLatin = (word, pattern) => exactMatch(word, pattern, 0x20, false);
export const exactPreMatchWordLatin = (word, pattern) => exactMatch(word, pattern, 0x20, true);

const isKanaRow = (byte) => byte === HIRAGANA_ROW || byte === KATAKANA_ROW;

/** かなを見る 4 つの比較関数の共通部分。 */
function kanaCompare(word, pattern, exact, foldRows) {
  const length = pattern.length;
  for (let i = 0; ; i += 2) {
    if (length <= i) return at(word, i);
    if (i >= word.length) return exact ? -at(pattern, i) : 0;
    if (length <= i + 1 || i + 1 >= word.length) return word[i] - at(pattern, i);

    const wc0 = word[i];
    const wc1 = word[i + 1];
    const pc0 = pattern[i];
    const pc1 = pattern[i + 1];
    if (isKanaRow(wc0) && isKanaRow(pc0)) {
      if (wc1 !== pc1) {
        if (foldRows) return wc1 - pc1;
        return ((wc0 << 8) + wc1) - ((pc0 << 8) + pc1);
      }
    } else if (wc0 !== pc0 || wc1 !== pc1) {
      return ((wc0 << 8) + wc1) - ((pc0 << 8) + pc1);
    }
  }
}

export const matchWordKanaGroup = (word, pattern) => kanaCompare(word, pattern, false, false);
export const matchWordKanaSingle = (word, pattern) => kanaCompare(word, pattern, false, true);
export const exactMatchWordKanaGroup = (word, pattern) => kanaCompare(word, pattern, true, false);
export const exactMatchWordKanaSingle = (word, pattern) => kanaCompare(word, pattern, true, true);
