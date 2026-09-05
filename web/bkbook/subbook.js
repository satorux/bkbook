// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux
// Copyright (c) 1997-2006 Motoyuki Kasahara

/**
 * サブブック——データファイルの先頭にある索引表。Python 版 subbook.py の移植。
 */

import { CHARCODE_ISO8859_1, decodeJisx0208 } from "./jacode.js";
import { EbError, Zio, be, findDirectory, findFile } from "./zio.js";

const INDEX_TABLE_ENTRY_SIZE = 16;

// 索引を引く前に、語をどう変形するか。
export const STYLE_CONVERT = 0;
export const STYLE_ASIS = 1;
export const STYLE_DELETE = 2;

// フォントの種類。
export const FONT_16 = 0;
export const FONT_24 = 1;
export const FONT_30 = 2;
export const FONT_48 = 3;
export const FONT_HEIGHTS = { [FONT_16]: 16, [FONT_24]: 24, [FONT_30]: 30, [FONT_48]: 48 };

/** 検索方式を表す索引 ID と、それを保持する名前の対応。 */
export const SEARCH_INDEX_IDS = {
  0x00: "text",
  0x01: "menu",
  0x02: "copyright",
  0x10: "image_menu",
  0x70: "endword_kana",
  0x71: "endword_asis",
  0x72: "endword_alphabet",
  0x80: "keyword",
  0x81: "cross",
  0x90: "word_kana",
  0x91: "word_asis",
  0x92: "word_alphabet",
  0xd8: "sound",
};

/** multi 検索の表が、欄の語索引として使う索引 ID。 */
export const MULTI_INDEX_IDS = new Set([0x71, 0x72, 0x91, 0x92, 0xa1, 0xa2, 0xb1, 0xb2]);
/** そのうち、かなで鍵づけられているもの。 */
export const MULTI_KANA_INDEX_IDS = new Set([0xa1, 0xb1]);

/** 内蔵フォントの開始ページを示す索引 ID。EB のみ。 */
const FONT_INDEX_IDS = {
  0xf1: ["wide", FONT_16],
  0xf2: ["narrow", FONT_16],
  0xf3: ["wide", FONT_24],
  0xf4: ["narrow", FONT_24],
  0xf5: ["wide", FONT_30],
  0xf6: ["narrow", FONT_30],
  0xf7: ["wide", FONT_48],
  0xf8: ["narrow", FONT_48],
};

/** 欄に入力できる値を候補の木として並べた補助ページ。 */
const MULTI_MENU_INDEX_ID = 0x01;

/** サブブックのデータファイルがないか、索引表が壊れている。 */
export class SubBookError extends EbError {}

/** 索引 1 つ。どこにあるかと、そこ向けに語をどう正規化するか。 */
export class Search {
  constructor(indexId, startPage, endPage) {
    this.indexId = indexId;
    this.startPage = startPage;
    this.endPage = endPage;
    this.katakana = STYLE_ASIS;
    this.lower = STYLE_CONVERT;
    this.mark = STYLE_ASIS;
    this.longVowel = STYLE_ASIS;
    this.doubleConsonant = STYLE_ASIS;
    this.contractedSound = STYLE_ASIS;
    this.smallVowel = STYLE_ASIS;
    this.voicedConsonant = STYLE_ASIS;
    this.pSound = STYLE_ASIS;
    this.space = STYLE_DELETE;
  }

  get pageCount() {
    return this.endPage - this.startPage + 1;
  }
}

/** multi 検索の欄 1 つ。ラベルと、それに答える索引。 */
export class MultiEntry {
  constructor(label) {
    this.label = label;
    this.index = null;
    this.candidates = [];
  }

  get menu() {
    return this.candidates.find((search) => search.indexId === MULTI_MENU_INDEX_ID) || null;
  }
}

/** 語 1 つではなく、欄を 1 つずつ埋めて行う検索。 */
export class MultiSearch {
  constructor(search) {
    this.search = search;
    this.entries = [];
  }

  get label() {
    return this.entries.map((entry) => entry.label).filter(Boolean).join(" / ");
  }
}

/** 内蔵フォント。ビットマップの開始位置と大きさ。 */
export class Font {
  constructor(fontCode, page = 0, fileName = "") {
    this.fontCode = fontCode;
    this.page = page;
    this.fileName = fileName;
  }

  get height() {
    return FONT_HEIGHTS[this.fontCode];
  }
}

/** 本の中の辞書 1 つ。 */
export class SubBook {
  constructor(book, { code, title, directoryName, indexPage, narrowFontNames = [], wideFontNames = [] }) {
    this.book = book;
    this.fs = book.fs;
    this.code = code;
    this.title = title;
    this.directoryName = directoryName;
    this.indexPage = indexPage;

    this.searches = {};
    this.multiSearches = [];
    this.narrowFonts = {};
    this.wideFonts = {};
    this.searchTitlePage = 0;

    this._narrowFontNames = narrowFontNames;
    this._wideFontNames = wideFontNames;
    this._zio = null;
    this._fontZios = new Map();
    this._loading = null;
    this._multis = null;
    this.stopCode = undefined; // undefined = 未定、null = なし
    this._inferring = null;
  }

  /**
   * この辞書で項目を終わらせるエスケープシーケンス。[code, argument] か null。
   * 最初に問われたときに本自身の索引から割り出す（stopcode.js）。appendix の
   * ように答えが分かっているなら stopCode に代入しておけばよい。
   */
  async inferStopCode() {
    if (this.stopCode !== undefined) return this.stopCode;
    if (this._inferring === null) {
      this._inferring = (async () => {
        const { infer } = await import("./stopcode.js");
        try {
          this.stopCode = await infer(this);
        } catch (error) {
          if (!(error instanceof EbError)) throw error;
          this.stopCode = null; // 失敗を項目ごとに繰り返さないため
        }
        return this.stopCode;
      })();
    }
    return this._inferring;
  }

  // -- データファイル ------------------------------------------------------

  async directoryPath() {
    const path = await findDirectory(this.fs, this.book.path, this.directoryName);
    if (path === null) {
      throw new SubBookError(
        `${this.book.path}: no directory ${JSON.stringify(this.directoryName)} for subbook ${JSON.stringify(this.title)}`,
      );
    }
    return path;
  }

  async _findTextFile() {
    const directory = await this.directoryPath();
    if (this.book.discCode === "eb") {
      const path = await findFile(this.fs, directory, "start");
      if (path === null) throw new SubBookError(`${directory}: no start file`);
      return path;
    }
    const dataDirectory = (await findDirectory(this.fs, directory, "data")) || directory;
    for (const name of ["honmon", "honmon2"]) {
      const path = await findFile(this.fs, dataDirectory, name);
      if (path !== null) return path;
    }
    throw new SubBookError(`${dataDirectory}: no honmon file`);
  }

  /** 本文と索引のデータファイル。最初に使うときに開く。 */
  async zio() {
    if (this._zio === null) {
      this._zio = Zio.open(this.fs, await this._findTextFile()).catch((error) => {
        this._zio = null;
        throw error;
      });
    }
    return this._zio;
  }

  /** 1 起点のページ番号 page から count ページ読む。 */
  async readPage(page, count = 1) {
    return (await this.zio()).readPage(page, count);
  }

  // -- 索引表 ----------------------------------------------------------------

  /** 索引表を解析する。何度呼んでもよい。 */
  load() {
    if (this._loading === null) {
      this._loading = this._load().catch((error) => {
        this._loading = null;
        throw error;
      });
    }
    return this._loading;
  }

  async _load() {
    const page = await this.readPage(this.indexPage);
    if (page.length < INDEX_TABLE_ENTRY_SIZE) {
      throw new SubBookError(`${this.title}: truncated index table`);
    }
    const count = page[1];
    if (count === 0 || count >= Math.floor(page.length / INDEX_TABLE_ENTRY_SIZE)) {
      throw new SubBookError(`${this.title}: bad index count ${count}`);
    }
    let globalAvailability = page[4];
    if (globalAvailability > 0x02) globalAvailability = 0;

    for (let i = 0; i < count; i++) {
      const offset = (i + 1) * INDEX_TABLE_ENTRY_SIZE;
      this._parseIndexEntry(page.subarray(offset, offset + INDEX_TABLE_ENTRY_SIZE), globalAvailability);
    }
    this._attachFontFiles();
    return this;
  }

  /** 16 バイトの索引表レコード 1 つから Search を組み立てる。 */
  _searchFromEntry(entry, globalAvailability) {
    const indexId = entry[0];
    const startPage = be(entry, 2, 4);
    const blockCount = be(entry, 6, 4);
    if (startPage === 0 || blockCount === 0) return null;

    const search = new Search(indexId, startPage, startPage + blockCount - 1);
    const availability = entry[10];
    if ((globalAvailability === 0x00 && availability === 0x02) || globalAvailability === 0x02) {
      applyStyleFlags(search, be(entry, 11, 3));
    } else if (indexId === 0x70 || indexId === 0x90 || MULTI_KANA_INDEX_IDS.has(indexId)) {
      // かなの索引は、既定ですべてを畳む。
      search.katakana = STYLE_CONVERT;
      search.lower = STYLE_CONVERT;
      search.mark = STYLE_DELETE;
      search.longVowel = STYLE_CONVERT;
      search.doubleConsonant = STYLE_CONVERT;
      search.contractedSound = STYLE_CONVERT;
      search.smallVowel = STYLE_CONVERT;
      search.voicedConsonant = STYLE_CONVERT;
      search.pSound = STYLE_CONVERT;
    }
    if (this.book.characterCode === CHARCODE_ISO8859_1 || indexId === 0x72 || indexId === 0x92) {
      search.space = STYLE_ASIS;
    } else {
      search.space = STYLE_DELETE;
    }
    return search;
  }

  _parseIndexEntry(entry, globalAvailability) {
    const search = this._searchFromEntry(entry, globalAvailability);
    if (search === null) return;
    const indexId = search.indexId;
    const name = SEARCH_INDEX_IDS[indexId];
    if (name !== undefined) {
      this.searches[name] = search;
    } else if (indexId === 0xff) {
      this.multiSearches.push(search);
    } else if (indexId === 0x16) {
      if (this.book.discCode !== "eb") this.searchTitlePage = search.startPage;
    } else if (indexId in FONT_INDEX_IDS && this.book.discCode === "eb") {
      const [width, fontCode] = FONT_INDEX_IDS[indexId];
      const fonts = width === "wide" ? this.wideFonts : this.narrowFonts;
      fonts[fontCode] = new Font(fontCode, search.startPage);
    }
  }

  // -- multi 検索 ------------------------------------------------------------

  /** このサブブックの multi 検索。欄まで読み出したもの。 */
  async multis() {
    await this.load();
    if (this._multis === null) {
      const multis = [];
      for (const search of this.multiSearches) multis.push(await this._readMulti(search));
      this._multis = multis;
    }
    return this._multis;
  }

  async _readMulti(search) {
    const page = await this.readPage(search.startPage);
    const count = be(page, 0, 2);
    const multi = new MultiSearch(search);
    let offset = 16;
    for (let field = 0; field < count; field++) {
      if (offset + 32 > page.length) {
        throw new SubBookError(`${this.title}: multi search table overruns its page`);
      }
      const indexCount = page[offset];
      let labelBytes = page.subarray(offset + 2, offset + 32);
      const nul = labelBytes.indexOf(0);
      if (nul >= 0) labelBytes = labelBytes.subarray(0, nul);
      const entry = new MultiEntry(decodeJisx0208(labelBytes));
      offset += 32;
      for (let i = 0; i < indexCount; i++) {
        const record = page.subarray(offset, offset + INDEX_TABLE_ENTRY_SIZE);
        offset += INDEX_TABLE_ENTRY_SIZE;
        const index = this._searchFromEntry(record, 0);
        if (index === null) continue;
        if (MULTI_INDEX_IDS.has(index.indexId)) entry.index = index;
        else entry.candidates.push(index);
      }
      multi.entries.push(entry);
    }
    return multi;
  }

  _attachFontFiles() {
    for (const [fonts, names] of [
      [this.narrowFonts, this._narrowFontNames],
      [this.wideFonts, this._wideFontNames],
    ]) {
      names.forEach((name, fontCode) => {
        if (!name) return;
        if (!(fontCode in fonts)) fonts[fontCode] = new Font(fontCode);
        fonts[fontCode].fileName = name;
        fonts[fontCode].page = 1;
      });
    }
  }

  /** font の字形が入っているデータファイル。 */
  async fontZio(font) {
    if (!font.fileName) return this.zio();
    let cached = this._fontZios.get(font.fileName);
    if (cached === undefined) {
      const base = await this.directoryPath();
      const directory = (await findDirectory(this.fs, base, "gaiji")) || base;
      const path = await findFile(this.fs, directory, font.fileName);
      if (path === null) throw new SubBookError(`${directory}: no font file ${JSON.stringify(font.fileName)}`);
      cached = await Zio.open(this.fs, path);
      this._fontZios.set(font.fileName, cached);
    }
    return cached;
  }

  // -- 取り出し口 --------------------------------------------------------------

  /** 名前で索引（word_asis、text など）を返す。なければ null。 */
  async search(name) {
    await this.load();
    return this.searches[name] || null;
  }

  async availableSearches() {
    await this.load();
    const names = Object.keys(this.searches).sort();
    if (this.multiSearches.length) names.push(`multi(${this.multiSearches.length})`);
    return names;
  }

  async close() {
    for (const zio of this._fontZios.values()) await zio.close();
    this._fontZios.clear();
    if (this._zio !== null) {
      const zio = await this._zio.catch(() => null);
      if (zio) await zio.close();
      this._zio = null;
    }
  }
}

/** 24 ビットの正規化フラグを Search に展開する。 */
function applyStyleFlags(search, flags) {
  search.katakana = (flags & 0xc00000) >> 22;
  search.lower = (flags & 0x300000) >> 20;
  // この欄だけ反転している。0 が「記号を落とす」の意味。
  search.mark = (flags & 0x0c0000) >> 18 === 0 ? STYLE_DELETE : STYLE_ASIS;
  search.longVowel = (flags & 0x030000) >> 16;
  search.doubleConsonant = (flags & 0x00c000) >> 14;
  search.contractedSound = (flags & 0x003000) >> 12;
  search.smallVowel = (flags & 0x000c00) >> 10;
  search.voicedConsonant = (flags & 0x000300) >> 8;
  search.pSound = (flags & 0x0000c0) >> 6;
}
