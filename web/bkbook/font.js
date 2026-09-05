// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux
// Copyright (c) 1997-2006 Motoyuki Kasahara

/**
 * JIS X 0208 にない文字（外字）のための、ディスク内蔵のビットマップフォント。
 * Python 版 font.py の移植。
 */

import { CHARCODE_ISO8859_1 } from "./jacode.js";
import { FONT_HEIGHTS } from "./subbook.js";
import { EbError, PAGE_SIZE, be } from "./zio.js";

/** 字形 1 つあたりのバイト数。フォントの高さ別、半角・全角別。 */
const NARROW_SIZES = { 16: 16, 24: 48, 30: 60, 48: 144 };
const WIDE_SIZES = { 16: 32, 24: 72, 30: 120, 48: 288 };
const NARROW_WIDTHS = { 16: 8, 24: 16, 30: 16, 48: 24 };
const WIDE_WIDTHS = { 16: 16, 24: 24, 30: 32, 48: 48 };

const CHUNK = 1024;

/** 求められたフォント、または文字のビットマップがない。 */
export class FontError extends EbError {}

/** 字形 1 つ。1 画素 1 ビット、行優先、最上位ビットが左端。 */
export class Bitmap {
  constructor(code, width, height, data) {
    this.code = code;
    this.width = width;
    this.height = height;
    this.data = data;
  }

  get rowBytes() {
    return (this.width + 7) >> 3;
  }

  pixel(x, y) {
    return Boolean(this.data[y * this.rowBytes + (x >> 3)] & (0x80 >> (x & 7)));
  }

  toText(on = "█", off = "·") {
    const rows = [];
    for (let y = 0; y < this.height; y++) {
      let row = "";
      for (let x = 0; x < this.width; x++) row += this.pixel(x, y) ? on : off;
      rows.push(row);
    }
    return rows.join("\n");
  }

  /** 1 画素 1 バイト（0 または 255）の透明度。ImageData や PNG に使う。 */
  toAlpha() {
    const out = new Uint8Array(this.width * this.height);
    for (let y = 0; y < this.height; y++) {
      for (let x = 0; x < this.width; x++) out[y * this.width + x] = this.pixel(x, y) ? 255 : 0;
    }
    return out;
  }

  /** SVG に描く。文字色は currentColor。 */
  toSvg() {
    const rects = [];
    for (let y = 0; y < this.height; y++) {
      let x = 0;
      while (x < this.width) {
        if (!this.pixel(x, y)) {
          x += 1;
          continue;
        }
        let run = 1;
        while (x + run < this.width && this.pixel(x + run, y)) run += 1;
        rects.push(`<rect x="${x}" y="${y}" width="${run}" height="1"/>`);
        x += run;
      }
    }
    return (
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${this.width} ${this.height}" ` +
      `width="${this.width}" height="${this.height}" shape-rendering="crispEdges" fill="currentColor">${rects.join("")}</svg>`
    );
  }
}

/** あるサブブックの半角または全角フォント、1 サイズ分。 */
export class FontSet {
  constructor(subbook, font, narrow) {
    this.subbook = subbook;
    this.font = font;
    this.narrow = narrow;
    this.height = FONT_HEIGHTS[font.fontCode];
    this.glyphSize = narrow ? NARROW_SIZES[this.height] : WIDE_SIZES[this.height];
    this.width = narrow ? NARROW_WIDTHS[this.height] : WIDE_WIDTHS[this.height];
    this.zio = null;
    this.count = 0;
    this.start = 0;
    this.end = 0;
    this._cache = new Map();
  }

  static async open(subbook, font, narrow) {
    const set = new FontSet(subbook, font, narrow);
    if (font.page === 0) {
      throw new FontError(`${JSON.stringify(subbook.title)}: no built-in ${narrow ? "narrow" : "wide"} font`);
    }
    set.zio = await subbook.fontZio(font);
    const header = await set.zio.readPage(font.page);
    if (header.length < 16) throw new FontError(`${JSON.stringify(subbook.title)}: truncated font header`);

    set.count = be(header, 12, 2);
    set.start = be(header, 10, 2);
    if (set.count === 0) throw new FontError(`${JSON.stringify(subbook.title)}: font holds no characters`);

    const latin = subbook.book.characterCode === CHARCODE_ISO8859_1;
    const rowLength = latin ? 0xfe : 0x5e;
    set.end = set.start + (Math.floor(set.count / rowLength) << 8) + (set.count % rowLength) - 1;
    if (latin) {
      if (0xfe < (set.end & 0xff)) set.end += 3;
    } else if (0x7e < (set.end & 0xff)) {
      set.end += 0xa3;
    }
    set._rowLength = rowLength;
    set._latin = latin;
    set._cellFirst = latin ? 0x01 : 0x21;
    return set;
  }

  has(code) {
    const cell = code & 0xff;
    return this.start <= code && code <= this.end && this._cellFirst <= cell && cell <= (this._latin ? 0xfe : 0x7e);
  }

  /** ディスク固有の文字符号に対応する字形を返す。 */
  async bitmap(code) {
    if (!this.has(code)) {
      throw new FontError(
        `0x${code.toString(16)} is outside this font (0x${this.start.toString(16)}..0x${this.end.toString(16)})`,
      );
    }
    const cached = this._cache.get(code);
    if (cached) return cached;

    const index = ((code >> 8) - (this.start >> 8)) * this._rowLength + ((code & 0xff) - (this.start & 0xff));
    const perChunk = Math.floor(CHUNK / this.glyphSize);
    const offset = Math.floor(index / perChunk) * CHUNK + (index % perChunk) * this.glyphSize;

    // 字形はヘッダのページの次のページから始まる。
    const data = await this.zio.read(this.font.page * PAGE_SIZE + offset, this.glyphSize);
    if (data.length !== this.glyphSize) throw new FontError(`0x${code.toString(16)}: truncated glyph`);
    const bitmap = new Bitmap(code, this.width, this.height, data);
    this._cache.set(code, bitmap);
    return bitmap;
  }

  /** このフォントが定義している文字符号をすべて列挙する。 */
  *codes() {
    let code = this.start;
    const lastCell = this._latin ? 0xfe : 0x7e;
    for (let i = 0; i < this.count; i++) {
      yield code;
      if ((code & 0xff) >= lastCell) code = (code & ~0xff) + 0x100 + this._cellFirst;
      else code += 1;
    }
  }
}

/** サブブックの半角／全角フォントを、指定の高さで開く。 */
export async function fontSet(subbook, narrow, height = 16) {
  await subbook.load();
  const fonts = narrow ? subbook.narrowFonts : subbook.wideFonts;
  const codes = Object.keys(fonts).map(Number).sort((a, b) => a - b);
  for (const fontCode of codes) {
    if (FONT_HEIGHTS[fontCode] === height) return FontSet.open(subbook, fonts[fontCode], narrow);
  }
  const have = codes.map((c) => FONT_HEIGHTS[c]);
  throw new FontError(
    `${JSON.stringify(subbook.title)}: no ${narrow ? "narrow" : "wide"} ${height}px font (have ${JSON.stringify(have)})`,
  );
}
