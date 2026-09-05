// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux
// Copyright (c) 1997-2006 Motoyuki Kasahara

/**
 * 本——catalog / catalogs ファイルと、そこに並ぶサブブック。Python 版 book.py の移植。
 */

import {
  CHARCODE_ISO8859_1,
  CHARCODE_JISX0208,
  CHARCODE_JISX0208_GB2312,
  CHARCODE_NAMES,
  decodeTitle,
} from "./jacode.js";
import { SubBook } from "./subbook.js";
import { EbError, Zio, be, findFile } from "./zio.js";

export const DISC_EB = "eb";
export const DISC_EPWING = "epwing";

const CATALOG_HEADER_SIZE = 16;
const EB_CATALOG_ENTRY_SIZE = 40;
const EPWING_CATALOG_ENTRY_SIZE = 164;

const EB_TITLE_LENGTH = 30;
const EPWING_TITLE_LENGTH = 80;
const DIRECTORY_NAME_LENGTH = 8;

const MAX_SUBBOOKS = 50;
const MAX_FONTS = 4;

/** 本を開けないか、catalog が壊れている。 */
export class BookError extends EbError {}

/** 固定長 ASCII の名前欄を復号する。NUL か空白で切る。 */
function trimmedName(data) {
  let text = "";
  for (let i = 0; i < data.length; i++) text += String.fromCharCode(data[i]);
  for (const terminator of ["\0", " "]) {
    const index = text.indexOf(terminator);
    if (index >= 0) text = text.slice(0, index);
  }
  return text;
}

/** path に本があるか——つまり catalog があるか。 */
export async function isBook(fs, path) {
  if (!(await fs.isdir(path))) return false;
  return Boolean((await findFile(fs, path, "catalog")) || (await findFile(fs, path, "catalogs")));
}

/** path にある本、または path の直下にある本すべて。 */
export async function findBooks(fs, path) {
  if (await isBook(fs, path)) return [path];
  if (!(await fs.isdir(path))) return [];
  const entries = (await fs.listdir(path)) || [];
  entries.sort();
  const books = [];
  for (const entry of entries) {
    const entryPath = fs.join(path, entry);
    if (await isBook(fs, entryPath)) books.push(entryPath);
  }
  return books;
}

/** 開かれた EB または EPWING の本。 */
export class Book {
  constructor(fs, path) {
    this.fs = fs;
    this.path = path;
    this.characterCode = CHARCODE_JISX0208;
    this.discCode = DISC_EB;
    this.epwingVersion = 0;
    this.subbooks = [];
  }

  static async open(fs, path) {
    if (!(await fs.isdir(path))) throw new BookError(`${path}: not a directory`);
    const book = new Book(fs, path);
    book.characterCode = await book._loadLanguage();
    const [discCode, catalogPath] = await book._findCatalog();
    book.discCode = discCode;
    await book._loadCatalog(catalogPath);
    return book;
  }

  async _findCatalog() {
    let catalog = await findFile(this.fs, this.path, "catalog");
    if (catalog !== null) return [DISC_EB, catalog];
    catalog = await findFile(this.fs, this.path, "catalogs");
    if (catalog !== null) return [DISC_EPWING, catalog];
    throw new BookError(`${this.path}: no catalog or catalogs file`);
  }

  async _loadLanguage() {
    const path = await findFile(this.fs, this.path, "language");
    if (path === null) return CHARCODE_JISX0208;
    let header;
    try {
      const zio = await Zio.open(this.fs, path);
      try {
        header = await zio.read(0, CATALOG_HEADER_SIZE);
      } finally {
        await zio.close();
      }
    } catch (error) {
      if (error instanceof EbError) return CHARCODE_JISX0208;
      throw error;
    }
    if (header.length < 2) return CHARCODE_JISX0208;
    const code = be(header, 0, 2);
    if (![CHARCODE_ISO8859_1, CHARCODE_JISX0208, CHARCODE_JISX0208_GB2312].includes(code)) {
      return CHARCODE_JISX0208;
    }
    return code;
  }

  async _loadCatalog(catalogPath) {
    const isEb = this.discCode === DISC_EB;
    const entrySize = isEb ? EB_CATALOG_ENTRY_SIZE : EPWING_CATALOG_ENTRY_SIZE;
    const titleLength = isEb ? EB_TITLE_LENGTH : EPWING_TITLE_LENGTH;

    const zio = await Zio.open(this.fs, catalogPath);
    try {
      const header = await zio.read(0, CATALOG_HEADER_SIZE);
      if (header.length < CATALOG_HEADER_SIZE) {
        throw new BookError(`${catalogPath}: truncated catalog`);
      }
      let count = be(header, 0, 2);
      if (count === 0) throw new BookError(`${catalogPath}: catalog lists no subbooks`);
      count = Math.min(count, MAX_SUBBOOKS);
      if (!isEb) this.epwingVersion = be(header, 2, 2);

      for (let index = 0; index < count; index++) {
        const offset = CATALOG_HEADER_SIZE + index * entrySize;
        const entry = await zio.read(offset, entrySize);
        if (entry.length < entrySize) {
          throw new BookError(`${catalogPath}: truncated entry for subbook ${index}`);
        }
        this.subbooks.push(this._parseEntry(index, entry, titleLength));
      }
    } finally {
      await zio.close();
    }
  }

  _parseEntry(index, entry, titleLength) {
    const title = decodeTitle(entry.subarray(2, 2 + titleLength), this.characterCode);
    const nameAt = 2 + titleLength;
    const directoryName = trimmedName(entry.subarray(nameAt, nameAt + DIRECTORY_NAME_LENGTH));

    let indexPage = 1;
    let narrowFonts = [];
    let wideFonts = [];
    if (this.discCode !== DISC_EB) {
      indexPage = be(entry, nameAt + DIRECTORY_NAME_LENGTH + 4, 2);
      wideFonts = fontNames(entry, 2 + titleLength + 18);
      narrowFonts = fontNames(entry, 2 + titleLength + 50);
    }
    return new SubBook(this, {
      code: index,
      title,
      directoryName,
      indexPage,
      narrowFontNames: narrowFonts,
      wideFontNames: wideFonts,
    });
  }

  get characterCodeName() {
    return CHARCODE_NAMES[this.characterCode] || "unknown";
  }

  /** サブブックを番号、ディレクトリ名、または完全なタイトルで引く。 */
  subbook(which) {
    if (typeof which === "number") return this.subbooks[which];
    const lowered = which.toLowerCase();
    for (const subbook of this.subbooks) {
      if (subbook.directoryName.toLowerCase() === lowered || subbook.title === which) return subbook;
    }
    throw new Error(`no subbook ${which}`);
  }

  async close() {
    for (const subbook of this.subbooks) await subbook.close();
  }
}

/** 8 バイトのフォント名の枠を 4 つ読む。空の枠は "" にする。 */
function fontNames(entry, offset) {
  const names = [];
  for (let index = 0; index < MAX_FONTS; index++) {
    const at = offset + index * DIRECTORY_NAME_LENGTH;
    const slot = entry.subarray(at, at + DIRECTORY_NAME_LENGTH);
    if (slot.length === 0 || slot[0] === 0 || slot[0] >= 0x80) {
      names.push("");
      continue;
    }
    names.push(trimmedName(slot));
  }
  return names;
}
