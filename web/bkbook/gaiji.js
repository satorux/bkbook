// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux

/**
 * ディスク固有の文字（外字）を Unicode に対応づける。Python 版 gaiji.py の移植。
 *
 * 表そのものは gaiji-table.js にあり、Python 側から生成する。ここにあるのは
 * 表を選んで重ねる手続きだけ。表は Map（"ha121" → 置き換え文字列）。
 */

import { BUILTIN } from "./gaiji-table.js";

export { BUILTIN };

/** 外字の符号を、プレースホルダや対応表ファイルと同じ書き方で文字列にする。 */
export function formatCode(narrow, code) {
  return `${narrow ? "h" : "z"}${code.toString(16).padStart(4, "0")}`;
}

/** subbook の組み込み表を返す。なければ空の表。 */
export function forSubbook(subbook) {
  return new Map(Object.entries(BUILTIN[subbook.title] || {}));
}

/**
 * 外字の対応表ファイルを解析する。1 行に 1 対応。符号、空白、置き換えテキスト。
 * 空行と # のコメントは無視する。置き換えが空なら「何も出力しない」。
 */
export function parse(text) {
  const table = new Map();
  text.split(/\r?\n/).forEach((line, index) => {
    const stripped = line.trim();
    if (!stripped || stripped.startsWith("#")) return;
    const m = /^(\S+)\s*(.*)$/.exec(stripped);
    const codeText = m[1];
    if (!/^[hz][0-9a-fA-F]{4}$/.test(codeText)) {
      throw new Error(`line ${index + 1}: expected h/z + 4 hex digits, got ${JSON.stringify(codeText)}`);
    }
    table.set(codeText.toLowerCase(), m[2]);
  });
  return table;
}

/**
 * subbook を整形するのに使う表を組み立てる。3 層あり、後のものが前を上書きする:
 * appendix、この本の組み込み表、extra（呼び出し側の Map）。
 */
export function resolve(subbook, extra = null, appendix = null) {
  const table = new Map();
  if (appendix !== null) for (const [k, v] of appendix.asGaijiTable()) table.set(k, v);
  for (const [k, v] of forSubbook(subbook)) table.set(k, v);
  if (extra !== null) for (const [k, v] of extra) table.set(k, v);
  return table;
}
