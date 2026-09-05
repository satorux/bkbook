// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux
// Copyright (c) 1997-2006 Motoyuki Kasahara

/**
 * 項目本文の読み出しと整形。Python 版 text.py の移植。
 *
 * 本文は JIS X 0208 の 2 バイト文字とエスケープシーケンスが混ざったバイト列で、
 * 解釈器はそれをたどりながらレンダラのメソッドを呼ぶ。PlainTextRenderer は
 * その呼び出しを文字列にする。
 */

import { CHARCODE_ISO8859_1, decodeJisx0208Char } from "./jacode.js";
import { Position } from "./search.js";
import { EbError, PAGE_SIZE, be } from "./zio.js";

/** 終わりの来ない項目を諦めるまでに読む量。 */
export const DEFAULT_MAX_LENGTH = 64 * 1024;

const READ_CHUNK = PAGE_SIZE;

const ESCAPE = 0x1f;

export const STOP_NONE = "none";
export const STOP_SOFT = "soft";
export const STOP_HARD = "hard";

/** 読み飛ばすだけでよいエスケープと、その全長。ここになければ 2 バイト。 */
const FIXED_LENGTH = {
  0x02: 2, 0x04: 2, 0x05: 2, 0x06: 2, 0x07: 2,
  0x09: 4, 0x0a: 2, 0x0b: 2, 0x0c: 2, 0x0e: 2, 0x0f: 2,
  0x10: 2, 0x11: 2, 0x12: 2, 0x13: 2, 0x14: 4,
  0x32: 2, 0x39: 46, 0x3c: 20,
  0x41: 4, 0x42: 4, 0x43: 2, 0x44: 12,
  0x4c: 4, 0x4d: 20, 0x4f: 34,
  0x52: 8, 0x53: 10, 0x59: 2, 0x5c: 2,
  0x61: 2, 0x62: 8, 0x63: 8, 0x64: 8,
  0x6a: 2, 0x6b: 2, 0x6c: 2, 0x6d: 2, 0x6f: 2,
  0xe1: 2,
};

/** 引数のバイトが、対応する「ここまで読み飛ばす」符号を選ぶエスケープ。 */
const SKIP_PLUS_20 = new Set([0x35, 0x36, 0x37, 0x38, 0x3a, 0x3b, 0x3d, 0x3e, 0x3f, 0x49, 0x4e]);
for (let code = 0x70; code < 0x90; code++) SKIP_PLUS_20.add(code);
const SKIP_PLUS_01 = new Set();
for (let code = 0xe4; code < 0x100; code += 2) SKIP_PLUS_01.add(code);

/** EPWING では 4 バイトの引数を伴うが、EB では伴わないことのあるエスケープ。 */
const OPTIONAL_ARGUMENT = new Set([0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f, 0xe0]);

/** 長さが後続のバイトで決まるエスケープ。 */
const VARIABLE_LENGTH = new Set([0x42, 0x4b]);

const gbDecoder = new TextDecoder("gb2312");

/** 本文の流れが壊れているか、途中で終わっている。 */
export class TextError extends EbError {}

/** 2 バイトのパック 10 進数を読む。 */
export function bcd2(data, offset) {
  return (
    ((data[offset] >> 4) & 0x0f) * 1000 +
    (data[offset] & 0x0f) * 100 +
    ((data[offset + 1] >> 4) & 0x0f) * 10 +
    (data[offset + 1] & 0x0f)
  );
}

/** 4 バイトのパック 10 進数を読む。 */
export function bcd4(data, offset) {
  let value = 0;
  for (let i = 0; i < 4; i++) {
    const byte = data[offset + i];
    value = value * 100 + ((byte >> 4) & 0x0f) * 10 + (byte & 0x0f);
  }
  return value;
}

// -- レンダラ -------------------------------------------------------------------

/** 本文イベントを捨てる受け皿。継承して必要なものだけ上書きする。 */
export class Renderer {
  character(_text) {}
  gaiji(_code, _narrow) {}
  newline() {}
  indent(_level) {}
  beginNarrow() {}
  endNarrow() {}
  beginSubscript() {}
  endSubscript() {}
  beginSuperscript() {}
  endSuperscript() {}
  beginEmphasis() {}
  endEmphasis() {}
  beginNoNewline() {}
  endNoNewline() {}
  beginDecoration(_kind) {}
  endDecoration() {}
  beginKeyword(_code) {}
  endKeyword() {}
  beginReference() {}
  endReference(_position) {}
  beginCandidate() {}
  endCandidate(_position) {}
}

/** 参照。それを担っているテキストと、指している先。 */
export class Reference {
  constructor(start, end, position) {
    this.start = start;
    this.end = end;
    this.position = position;
  }

  textOf(text) {
    return text.slice(this.start, this.end);
  }
}

/** メニューの 1 行、または multi 検索のある欄に入れられる値 1 つ。position が null なら見出し。 */
export class Candidate extends Reference {}

/** 外字の符号を、プレースホルダや対応表ファイルと同じ書き方で文字列にする。 */
export function formatGaijiCode(narrow, code) {
  return `${narrow ? "h" : "z"}${code.toString(16).padStart(4, "0")}`;
}

/**
 * イベントを平文の文字列に集める。
 *
 * gaiji には外字の対応表を渡す。鍵は formatGaijiCode の形（"ha121"、"zb121"）。
 * 対応のないものは eblook 式の <gaiji=z1234> と書き出す。
 *
 * 文字列の位置は Python 版と同じく UTF-16 の単位ではなく、コードポイントで
 * 数えない——JavaScript の文字列添字（UTF-16）で数える。参照の切り出しに
 * textOf を使うかぎり違いは出ない。
 */
export class PlainTextRenderer extends Renderer {
  static SUBSCRIPT_DIGITS = "₀₁₂₃₄₅₆₇₈₉";

  constructor({ gaiji = null, subscript = true } = {}) {
    super();
    this._parts = [];
    this._length = 0;
    this.gaijiMap = gaiji || new Map();
    this.references = [];
    this.candidates = [];
    this.unknownGaiji = [];
    this._referenceStart = null;
    this._candidateStart = null;
    this._subscript = subscript;
    this._subscriptStarts = [];
  }

  _emit(text) {
    if (text) {
      this._parts.push(text);
      this._length += text.length;
    }
  }

  get text() {
    return this._parts.join("");
  }

  character(text) {
    this._emit(text);
  }

  gaiji(code, narrow) {
    const key = formatGaijiCode(narrow, code);
    let replacement = this.gaijiMap instanceof Map ? this.gaijiMap.get(key) : this.gaijiMap[key];
    if (replacement === undefined) {
      this.unknownGaiji.push(key);
      replacement = `<gaiji=${key}>`;
    }
    this._emit(replacement);
  }

  newline() {
    this._emit("\n");
  }

  beginSubscript() {
    if (this._subscript) this._subscriptStarts.push(this._parts.length);
  }

  /** 下付きのテキストを区切る。数字は本物の下付き文字に、それ以外は括弧に。 */
  endSubscript() {
    if (this._subscriptStarts.length === 0) return;
    const start = this._subscriptStarts.pop();
    const inner = this._parts.slice(start).join("");
    if (!inner) return;
    this._parts.length = start;
    this._length -= inner.length;
    // Python の str.isdigit() に合わせる。10 進数字（全角を含む）と、
    // 上付き・丸数字のような「数字」型の文字。
    if (/^[\p{Nd}\u00b2\u00b3\u00b9\u2070-\u2079\u2080-\u2089\u2460-\u249b\u24ea-\u24ff\u2776-\u2793]+$/u.test(inner)) {
      // 置き換えがあるのは ASCII の数字だけ。それ以外はそのまま。
      this._emit(inner.replace(/[0-9]/g, (d) => PlainTextRenderer.SUBSCRIPT_DIGITS[Number(d)]));
    } else {
      this._emit(`(${inner})`);
    }
  }

  beginReference() {
    this._referenceStart = this._length;
  }

  endReference(position) {
    if (this._referenceStart !== null) {
      this.references.push(new Reference(this._referenceStart, this._length, position));
      this._referenceStart = null;
    }
  }

  beginCandidate() {
    this._candidateStart = this._length;
  }

  endCandidate(position) {
    if (this._candidateStart !== null) {
      this.candidates.push(new Candidate(this._candidateStart, this._length, position));
      this._candidateStart = null;
    }
  }
}

// -- 解釈器 ---------------------------------------------------------------------

/** readText の呼び出し 1 回の結果。 */
export class TextResult {
  constructor(text, stop, nextPosition) {
    this.text = text;
    this.stop = stop;
    this.nextPosition = nextPosition;
    this.references = [];
    this.candidates = [];
    this.unknownGaiji = [];
  }
}

/** サブブックの本文ファイルを少しずつ読む道具。 */
class Stream {
  constructor(zio, location) {
    this._zio = zio;
    this.location = location;
    this._buffer = new Uint8Array(0);
    this._offset = 0;
    this._atEnd = false;
  }

  async peek(count) {
    if (this._buffer.length - this._offset < count) await this._refill();
    return this._buffer.subarray(this._offset, this._offset + count);
  }

  async _refill() {
    if (this._atEnd) return;
    const rest = this._buffer.subarray(this._offset);
    const chunk = await this._zio.read(this.location + rest.length, READ_CHUNK);
    if (chunk.length < READ_CHUNK) this._atEnd = true;
    const buffer = new Uint8Array(rest.length + chunk.length);
    buffer.set(rest, 0);
    buffer.set(chunk, rest.length);
    this._buffer = buffer;
    this._offset = 0;
  }

  advance(count) {
    this._offset += count;
    this.location += count;
  }
}

export function positionToLocation(position) {
  return (position.page - 1) * PAGE_SIZE + position.offset;
}

export function locationToPosition(location) {
  return new Position(Math.floor(location / PAGE_SIZE) + 1, location % PAGE_SIZE);
}

/**
 * position から項目（または見出し）を 1 つ読む。
 *
 * 読み出しは、本文終了のエスケープ、その項目の stop code、または見出しの
 * 場合は最初の改行で止まる。stopCode を省くと本に自前のものを尋ね、本は
 * 初回に索引からそれを割り出す（stopcode.js）。
 */
export async function readText(
  subbook,
  position,
  { heading = false, renderer = null, maxLength = DEFAULT_MAX_LENGTH, stopCode = undefined } = {},
) {
  await subbook.load();
  if (stopCode === undefined) stopCode = heading ? null : await subbook.inferStopCode();
  const sink = renderer !== null ? renderer : new PlainTextRenderer();
  const interpreter = new Interpreter(subbook, sink, heading, stopCode);
  const stop = await interpreter.run(positionToLocation(position), maxLength);

  const plain = sink instanceof PlainTextRenderer;
  const result = new TextResult(plain ? sink.text : "", stop, locationToPosition(interpreter.location));
  if (plain) {
    result.references = sink.references;
    result.candidates = sink.candidates;
    result.unknownGaiji = sink.unknownGaiji;
  }
  return result;
}

/** position の見出し語だけを読む。 */
export function readHeading(subbook, position, options = {}) {
  return readText(subbook, position, { ...options, heading: true });
}

class Interpreter {
  constructor(subbook, renderer, heading, stopCode) {
    this.subbook = subbook;
    this.renderer = renderer;
    this.heading = heading;
    this.stopCode = stopCode; // [code, argument] または null
    this.location = 0;

    this.narrow = false;
    this.skipCode = null;
    this.ebxacGaiji = false;
    this.printableCount = 0;
    this.autoStopCode = null;
    this._isEpwing = subbook.book.discCode !== "eb";
    this._latin = subbook.book.characterCode === CHARCODE_ISO8859_1;
    this._characterCode = subbook.book.characterCode;
  }

  async run(location, maxLength) {
    const stream = new Stream(await this.subbook.zio(), location);
    this.location = location;
    let consumed = 0;

    while (consumed < maxLength) {
      const head = await stream.peek(2);
      if (head.length === 0) {
        this.location = stream.location;
        return STOP_HARD;
      }
      if (head[0] === ESCAPE) {
        if (head.length < 2) throw new TextError(`${stream.location}: truncated escape`);
        const [step, stop] = await this._escape(stream, head[1]);
        if (stop === STOP_HARD) {
          this.location = stream.location;
          return STOP_HARD;
        }
        stream.advance(step);
        consumed += step;
        this.location = stream.location;
        if (stop === STOP_SOFT) return STOP_SOFT;
        continue;
      }
      const step = this._character(stream, head);
      stream.advance(step);
      consumed += step;
      this.location = stream.location;
    }
    return STOP_NONE;
  }

  // -- 文字 --------------------------------------------------------------------

  _character(stream, head) {
    this.printableCount += 1;

    if (this._latin) {
      const c1 = head[0];
      if ((c1 >= 0x20 && c1 < 0x7f) || (c1 >= 0xa0 && c1 <= 0xff)) {
        if (this.skipCode === null) this.renderer.character(String.fromCharCode(c1));
        return 1;
      }
      if (head.length < 2) throw new TextError(`${stream.location}: truncated local character`);
      if (this.skipCode === null) this.renderer.gaiji((head[0] << 8) | head[1], true);
      return 2;
    }

    if (head.length < 2) throw new TextError(`${stream.location}: truncated character`);
    const c1 = head[0];
    const c2 = head[1];

    // 文字の先頭にはなりえないバイト。1 バイトだけ進めて位相を戻す。
    if (c1 < 0x20) return 1;
    if (this.skipCode !== null) return 2;

    if (c1 > 0x20 && c1 < 0x7f && c2 > 0x20 && c2 < 0x7f) {
      this.renderer.character(decodeJisx0208Char((c1 << 8) | c2));
    } else if (c1 > 0x20 && c1 < 0x7f && c2 > 0xa0 && c2 < 0xff) {
      this.renderer.character(gbDecoder.decode(new Uint8Array([c1 | 0x80, c2])));
    } else if (c1 > 0xa0 && c1 < 0xff && c2 > 0x20 && c2 < 0x7f) {
      this.renderer.gaiji((c1 << 8) | c2, this.narrow);
    }
    return 2;
  }

  // -- エスケープシーケンス --------------------------------------------------

  /** エスケープを 1 つ処理し、[消費バイト数, 停止状態] を返す。 */
  async _escape(stream, code) {
    if (code === 0x03) return [0, STOP_HARD];

    let step = FIXED_LENGTH[code] ?? 2;
    if (OPTIONAL_ARGUMENT.has(code)) step = await this._optionalArgumentLength(stream, code);
    else if (VARIABLE_LENGTH.has(code)) step = await this._variableLength(stream, code);

    const data = await stream.peek(step);
    if (data.length < step) {
      throw new TextError(`${stream.location}: truncated escape 0x1f${code.toString(16).padStart(2, "0")}`);
    }

    if (this.skipCode !== null) {
      if (code === this.skipCode) this.skipCode = null;
      return [step, STOP_NONE];
    }
    if (SKIP_PLUS_20.has(code)) {
      this.skipCode = code + 0x20;
      return [2, STOP_NONE];
    }
    if (SKIP_PLUS_01.has(code)) {
      this.skipCode = code + 0x01;
      return [2, STOP_NONE];
    }
    return this._dispatch(code, data, step);
  }

  async _variableLength(stream, code) {
    if (code === 0x42) {
      const data = await stream.peek(4);
      if (data.length < 4) throw new TextError(`${stream.location}: truncated reference`);
      return data[2] !== 0x00 ? 2 : 4;
    }
    const data = await stream.peek(10);
    if (data.length < 10) throw new TextError(`${stream.location}: truncated paged reference`);
    return data[8] === 0x1f && data[9] === 0x6b ? 10 : 8;
  }

  async _optionalArgumentLength(stream, code) {
    if ((code === 0x1c || code === 0x1d) && this._characterCode === 3) return 2;
    const data = await stream.peek(4);
    if (data.length < 4) {
      throw new TextError(`${stream.location}: truncated escape 0x1f${code.toString(16).padStart(2, "0")}`);
    }
    if (!this._isEpwing && data[2] >= 0x1f) return 2;
    return 4;
  }

  _dispatch(code, data, step) {
    const renderer = this.renderer;
    switch (code) {
      case 0x04:
        this.narrow = true;
        renderer.beginNarrow();
        break;
      case 0x05:
        this.narrow = false;
        renderer.endNarrow();
        break;
      case 0x06:
        renderer.beginSubscript();
        break;
      case 0x07:
        renderer.endSubscript();
        break;
      case 0x09: {
        const argument = be(data, 2, 2);
        if (this._isStopCode(0x1f09, argument)) return [step, STOP_SOFT];
        renderer.indent(argument);
        break;
      }
      case 0x0a:
        if (this.heading) return [step, STOP_SOFT];
        renderer.newline();
        break;
      case 0x0e:
        renderer.beginSuperscript();
        break;
      case 0x0f:
        renderer.endSuperscript();
        break;
      case 0x10:
        renderer.beginNoNewline();
        break;
      case 0x11:
        renderer.endNoNewline();
        break;
      case 0x12:
        renderer.beginEmphasis();
        break;
      case 0x13:
        renderer.endEmphasis();
        break;
      case 0x14:
        this.skipCode = 0x15;
        break;
      case 0x1c:
        if (this._characterCode === 3) this.ebxacGaiji = true;
        break;
      case 0x1d:
        if (this._characterCode === 3) this.ebxacGaiji = false;
        break;
      case 0x41: {
        const argument = be(data, 2, 2);
        if (this._isStopCode(0x1f41, argument)) return [step, STOP_SOFT];
        if (this.autoStopCode === null) this.autoStopCode = argument;
        renderer.beginKeyword(argument);
        break;
      }
      case 0x42:
        renderer.beginReference();
        break;
      case 0x4b:
        if (step === 10) return [step, STOP_SOFT];
        break;
      case 0x43:
        renderer.beginCandidate();
        break;
      case 0x61:
        renderer.endKeyword();
        break;
      case 0x62:
        renderer.endReference(new Position(bcd4(data, 2), bcd2(data, 6)));
        break;
      case 0x63: {
        const page = bcd4(data, 2);
        const offset = bcd2(data, 6);
        renderer.endCandidate(page === 0 && offset === 0 ? null : new Position(page, offset));
        break;
      }
      case 0x6c:
        return [step, STOP_SOFT];
      case 0xe0:
        if (step === 4) renderer.beginDecoration(be(data, 2, 2));
        break;
      case 0xe1:
        renderer.endDecoration();
        break;
      default:
        break;
    }
    return [step, STOP_NONE];
  }

  /** このエスケープは項目を終わらせるか。 */
  _isStopCode(code0, code1) {
    if (this.heading || this.printableCount === 0) return false;
    if (this.stopCode !== null) return code0 === this.stopCode[0] && code1 === this.stopCode[1];
    return code0 === 0x1f41 && code1 === this.autoStopCode;
  }
}
