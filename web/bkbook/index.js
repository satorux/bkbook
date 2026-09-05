// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux

/** bkbook の JavaScript 版。EB / EPWING 辞書を読む。 */

export { Book, BookError, findBooks, isBook } from "./book.js";
export { BlockCache, HttpFs, NodeFs } from "./fs.js";
export { SubBook, SubBookError, Search } from "./subbook.js";
export { EbError, Zio, ZioError } from "./zio.js";
export {
  Candidate,
  PlainTextRenderer,
  Reference,
  Renderer,
  TextError,
  TextResult,
  formatGaijiCode,
  readHeading,
  readText,
} from "./text.js";
export { Bitmap, FontError, FontSet, fontSet } from "./font.js";
export { Appendix, AppendixError, AppendixNotFoundError } from "./appendix.js";
export * as appendix from "./appendix.js";
export * as gaiji from "./gaiji.js";
export * as stopcode from "./stopcode.js";
export { Source, categorise, openCollection, parseOrder, readable } from "./collection.js";
export {
  Hit,
  NoSuchSearchError,
  Position,
  SearchError,
  WordError,
  iterIndex,
  iterKeywordHits,
  parsePattern,
  search,
  searchKeyword,
  searchMulti,
  searchPattern,
} from "./search.js";
