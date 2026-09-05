// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux

/**
 * 日本語文字コードの変換。
 *
 * EB のディスクは日本語を JIS X 0208 の符号位置そのままで持っている。
 * 最上位ビットを立てれば EUC-JP になるので、復号はブラウザの
 * TextDecoder("euc-jp") に任せられる。
 *
 * 符号化の側には TextEncoder が UTF-8 しか話さないという問題がある。
 * そこで 94×94 の符号位置すべてを一度復号し、その逆引き表を作って
 * 検索語の符号化に使う。8,836 文字分の表で、最初に使うときに作る。
 */

/** ディスクの language ファイルに入っている文字コードの値。 */
export const CHARCODE_ISO8859_1 = 1;
export const CHARCODE_JISX0208 = 2;
export const CHARCODE_JISX0208_GB2312 = 3;

export const CHARCODE_NAMES = {
  [CHARCODE_ISO8859_1]: "iso8859-1",
  [CHARCODE_JISX0208]: "jisx0208",
  [CHARCODE_JISX0208_GB2312]: "jisx0208+gb2312",
};

const eucDecoder = new TextDecoder("euc-jp");

/**
 * ブラウザの euc-jp 復号器は WHATWG の jis0208 表を使っていて、Python の
 * euc_jp コーデックと 6 文字で食い違う——0x2141 を 〜 (U+301C) ではなく
 * ～ (U+FF5E) にする、など。Windows (CP932) 流の対応である。辞書の本文には
 * − や 〜 が山ほど出てくるので、Python 版と同じ字に戻す。NEC の 13 区や
 * IBM 拡張の 89〜92 区もブラウザは復号するが、JIS X 0208 にはないので
 * Python 版と同じく U+FFFD にする。
 */
const FIXUPS = {
  0x2141: "\u301c", // 〜 WAVE DASH
  0x2142: "\u2016", // ‖ DOUBLE VERTICAL LINE
  0x215d: "\u2212", // − MINUS SIGN
  0x2171: "\u00a2", // ¢
  0x2172: "\u00a3", // £
  0x224c: "\u00ac", // ¬
};
const OUTSIDE_JISX0208_ROWS = new Set([0x2d, 0x79, 0x7a, 0x7b, 0x7c]);
const REPLACEMENT = "\ufffd";

/** 復号した文字列の中で、ブラウザ流の字を Python 版と同じ字に戻す。ISO-2022-JP の appendix 用。 */
export function fixupDecoded(text) {
  return text.replace(/[～∥－￠￡￢]/g, (c) => ({ "～": "〜", "∥": "‖", "－": "−", "￠": "¢", "￡": "£", "￢": "¬" })[c]);
}

/** 全バイトの最上位ビットを立て、JIS X 0208 を EUC-JP にする。 */
export function jisx0208ToEuc(data) {
  const out = new Uint8Array(data.length);
  for (let i = 0; i < data.length; i++) out[i] = data[i] | 0x80;
  return out;
}

function stripPadding(data, pad) {
  const nul = data.indexOf(0);
  if (nul >= 0) data = data.subarray(0, nul);
  let end = data.length;
  while (end > 0 && data[end - 1] === pad) end -= 1;
  return data.subarray(0, end);
}

let jisTable = null; // 94×94 の符号位置 → 文字。復号できなければ U+FFFD

function buildTable() {
  // 全符号位置を改行区切りで 1 本のバッファに並べ、1 回で復号する。
  const count = 94 * 94;
  const buffer = new Uint8Array(count * 3);
  let at = 0;
  for (let c1 = 0x21; c1 <= 0x7e; c1++) {
    for (let c2 = 0x21; c2 <= 0x7e; c2++) {
      buffer[at++] = c1 | 0x80;
      buffer[at++] = c2 | 0x80;
      buffer[at++] = 0x0a;
    }
  }
  const pieces = eucDecoder.decode(buffer).split("\n");
  const table = new Array(count);
  let index = 0;
  for (let c1 = 0x21; c1 <= 0x7e; c1++) {
    for (let c2 = 0x21; c2 <= 0x7e; c2++) {
      let piece = pieces[index];
      const code = (c1 << 8) | c2;
      if (FIXUPS[code] !== undefined) piece = FIXUPS[code];
      else if (OUTSIDE_JISX0208_ROWS.has(c1) || piece.length === 0) piece = REPLACEMENT;
      table[index++] = piece;
    }
  }
  return table;
}

/** JIS X 0208 の符号位置 1 つ（例: 0x256A）を 1 文字に復号する。 */
export function decodeJisx0208Char(code) {
  if (jisTable === null) jisTable = buildTable();
  const c1 = (code >> 8) & 0x7f;
  const c2 = code & 0x7f;
  if (c1 < 0x21 || c1 > 0x7e || c2 < 0x21 || c2 > 0x7e) return REPLACEMENT;
  return jisTable[(c1 - 0x21) * 94 + (c2 - 0x21)];
}

/** JIS X 0208 の生バイト列を文字列に復号する。末尾の NUL と空白は詰め物。 */
export function decodeJisx0208(data) {
  data = stripPadding(data, 0x20);
  let text = "";
  for (let i = 0; i + 1 < data.length; i += 2) text += decodeJisx0208Char((data[i] << 8) | data[i + 1]);
  if (data.length & 1) text += REPLACEMENT;
  return text;
}

/** ISO 8859-1 を復号する。TextDecoder の "iso-8859-1" は windows-1252 なので使わない。 */
export function decodeLatin1(data) {
  let text = "";
  for (let i = 0; i < data.length; i++) text += String.fromCharCode(data[i]);
  return text;
}

/** catalog のタイトル欄を、そのディスクの文字コードに従って復号する。 */
export function decodeTitle(data, characterCode) {
  if (characterCode === CHARCODE_ISO8859_1) {
    return decodeLatin1(stripPadding(data, 0x20));
  }
  return decodeJisx0208(data);
}


// -- 符号化 ---------------------------------------------------------------

let toJis = null; // 文字 -> JIS X 0208 の符号位置

function buildEncoder() {
  if (jisTable === null) jisTable = buildTable();
  const table = new Map();
  let index = 0;
  for (let c1 = 0x21; c1 <= 0x7e; c1++) {
    for (let c2 = 0x21; c2 <= 0x7e; c2++) {
      const piece = jisTable[index++];
      // 同じ文字に 2 つの符号が対応するときは若いほうを取る。
      if (piece !== REPLACEMENT && !table.has(piece)) table.set(piece, (c1 << 8) | c2);
    }
  }
  return table;
}

export class EncodeError extends Error {}

/**
 * 文字列を EUC-JP のバイト列にする。Python の str.encode("euc_jp") にあたる。
 *
 * ASCII は 1 バイト、半角カタカナは SS2 (0x8E) に続く 1 バイト、
 * それ以外は最上位ビットの立った 2 バイト。表にない文字は EncodeError。
 */
export function encodeEucJp(text) {
  if (toJis === null) toJis = buildEncoder();
  const out = [];
  for (const char of text) {
    const cp = char.codePointAt(0);
    if (cp < 0x80) {
      out.push(cp);
    } else if (cp >= 0xff61 && cp <= 0xff9f) {
      out.push(0x8e, 0xa1 + (cp - 0xff61));
    } else {
      const code = toJis.get(char);
      if (code === undefined) {
        throw new EncodeError(`${JSON.stringify(text)}: contains characters outside JIS X 0208`);
      }
      out.push((code >> 8) | 0x80, (code & 0xff) | 0x80);
    }
  }
  return Uint8Array.from(out);
}

/** 文字列を ISO 8859-1 のバイト列にする。範囲外は EncodeError。 */
export function encodeLatin1(text) {
  const out = new Uint8Array(text.length);
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    if (code > 0xff) {
      throw new EncodeError(`${JSON.stringify(text)}: contains non Latin-1 characters`);
    }
    out[i] = code;
  }
  return out;
}
