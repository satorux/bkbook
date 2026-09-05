// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux
// Copyright (c) 1997-2006 Motoyuki Kasahara

/**
 * サブブックの語索引を引く。Python 版 search.py の移植。
 *
 * 索引は 2048 バイトのページからなる B 木で、ページの読み出しはすべて
 * 非同期になる。それ以外の構造は Python 版と同じに保ってある。
 */

import { CHARCODE_ISO8859_1 } from "./jacode.js";
import * as match from "./match.js";
import {
  Query,
  WORD_ALPHABET,
  WORD_KANA,
  WordError,
  convertToJisx0208,
  convertToLatin,
  fixWord,
} from "./setword.js";
import { MULTI_KANA_INDEX_IDS } from "./subbook.js";
import { EbError, be } from "./zio.js";

export { WordError };

const MAX_INDEX_DEPTH = 6;

const PAGE_ID_LEAF = 0x80;
const PAGE_ID_LAYER_START = 0x40; // eslint-disable-line no-unused-vars
const PAGE_ID_LAYER_END = 0x20;
const PAGE_ID_HAS_GROUP = 0x10;

const GROUP_SINGLE = 0x00;
const GROUP_START = 0x80;
const GROUP_MEMBER = 0xc0;

/** 比較関数がかなを畳む索引。索引の並び順と比較の順が一致しない。 */
const KANA_INDEX_IDS = [0x70, 0x90];

/** グループ化された葉項目の、各欄の幅。 */
const WORD_LAYOUT = { groupCount: 2, memberKey: true };
const MULTI_LAYOUT = { groupCount: 4, memberKey: false };

/** 索引ページが壊れている。 */
export class SearchError extends EbError {}

/** この種の問い合わせに答えられる索引を、このサブブックは持たない。 */
export class NoSuchSearchError extends SearchError {}

/** 本文ファイル内の位置。1 起点のページ番号とバイト位置。 */
export class Position {
  constructor(page, offset) {
    this.page = page;
    this.offset = offset;
  }

  get key() {
    return `${this.page}:${this.offset}`;
  }

  toString() {
    return this.key;
  }
}

/** 一致した索引項目 1 つ。その本文と見出しの在り処。 */
export class Hit {
  constructor(text, heading) {
    this.text = text;
    this.heading = heading;
  }

  get key() {
    return `${this.text.key}/${this.heading.key}`;
  }
}

// -- 索引と比較関数を選ぶ -------------------------------------------------------

/** 引く索引を選ぶ。libeb と同じ優先順位で代替に落ちていく。 */
async function selectIndex(subbook, wordCode, backward) {
  await subbook.load();
  const kind = backward ? "endword" : "word";
  const preferred =
    wordCode === WORD_ALPHABET
      ? [`${kind}_alphabet`, `${kind}_asis`]
      : wordCode === WORD_KANA
        ? [`${kind}_kana`, `${kind}_asis`]
        : [`${kind}_asis`];
  for (const name of preferred) {
    const search = subbook.searches[name];
    if (search !== undefined && search.startPage !== 0) return [name, search];
  }
  throw new NoSuchSearchError(
    `${JSON.stringify(subbook.title)}: no ${kind} index for a ${wordCode} search word`,
  );
}

/** [中間ノード用, 葉用, グループ用] の比較関数を返す。 */
function comparators(subbook, indexName, exact) {
  if (subbook.book.characterCode === CHARCODE_ISO8859_1) {
    if (exact) {
      return [match.exactPreMatchWordLatin, match.exactMatchWordLatin, match.exactMatchWordLatin];
    }
    return [match.preMatchWord, match.matchWord, match.matchWord];
  }
  if (indexName.endsWith("_kana")) {
    if (exact) {
      return [match.exactPreMatchWordJis, match.exactMatchWordKanaSingle, match.exactMatchWordKanaGroup];
    }
    return [match.preMatchWord, match.matchWordKanaSingle, match.matchWordKanaGroup];
  }
  if (exact) {
    return [match.exactPreMatchWordJis, match.exactMatchWordJis, match.exactMatchWordKanaGroup];
  }
  return [match.preMatchWord, match.matchWord, match.matchWordKanaGroup];
}

function convert(subbook, text) {
  return subbook.book.characterCode === CHARCODE_ISO8859_1 ? convertToLatin(text) : convertToJisx0208(text);
}

/** text を検索するための問い合わせと比較関数を用意する。 */
export async function prepare(subbook, text, exact = false, backward = false) {
  const [raw, wordCode] = convert(subbook, text);
  const [indexName, search] = await selectIndex(subbook, wordCode, backward);
  const query = fixWord(search, raw, subbook.book.characterCode, wordCode, backward);
  return [query, indexName, comparators(subbook, indexName, exact)];
}

// -- B 木の走査 ---------------------------------------------------------------

function header(data, page, what) {
  if (data.length < 4) throw new SearchError(`page ${page}: truncated ${what} page`);
  return { pageId: data[0], entryLength: data[1], entryCount: be(data, 2, 2) };
}

/** 中間ページをたどり、語が載っているかもしれない葉ページまで降りる。 */
async function descend(subbook, page, query, comparePre) {
  for (let depth = 0; depth < MAX_INDEX_DEPTH; depth++) {
    const data = await subbook.readPage(page);
    const { pageId, entryLength, entryCount } = header(data, page, "index");
    if (pageId & PAGE_ID_LEAF) return page;

    let nextPage = page;
    let offset = 4;
    let found = false;
    for (let entry = 0; entry < entryCount; entry++) {
      if (data.length < offset + entryLength + 4) {
        throw new SearchError(`page ${page}: index entry overruns the page`);
      }
      const key = data.subarray(offset, offset + entryLength);
      if (comparePre(query.canonicalized, key) <= 0) {
        nextPage = be(data, offset + entryLength, 4);
        found = true;
        break;
      }
      offset += entryLength + 4;
    }
    if (!found) return null;
    if (nextPage === page) return null;
    page = nextPage;
  }
  throw new SearchError(`index is deeper than ${MAX_INDEX_DEPTH} levels`);
}

/** page から葉ページを前へ走査し、一致したものを順に返す。 */
async function* iterLeafHits(subbook, page, query, compareSingle, compareGroup, layout = WORD_LAYOUT) {
  // グループはページ境界で割れることがある。ヘッダの判定はページをまたいで
  // 生き延びなければならない。
  const state = { comparison: 1, inGroup: false };

  for (;;) {
    const data = await subbook.readPage(page);
    const { pageId, entryLength, entryCount } = header(data, page, "leaf");
    if (!(pageId & PAGE_ID_LEAF)) throw new SearchError(`page ${page}: expected a leaf page`);

    const hasGroup = Boolean(pageId & PAGE_ID_HAS_GROUP);
    const variable = entryLength === 0;
    let offset = 4;

    for (let entry = 0; entry < entryCount; entry++) {
      let hit;
      if (hasGroup) {
        [offset, hit] = readGroupEntry(data, offset, query, compareSingle, compareGroup, state, page, layout);
      } else if (variable) {
        [offset, hit] = readVariableEntry(data, offset, query, compareSingle, state, page);
      } else {
        [offset, hit] = readFixedEntry(data, offset, entryLength, query, compareSingle, state, page);
      }
      if (hit !== null) yield hit;
      if (state.comparison < 0) return;
    }
    if (pageId & PAGE_ID_LAYER_END) return;
    page += 1;
  }
}

/** 項目の鍵に続く、本文と見出しの位置を読む。 */
function hitAt(data, base) {
  return new Hit(
    new Position(be(data, base, 4), be(data, base + 4, 2)),
    new Position(be(data, base + 6, 4), be(data, base + 10, 2)),
  );
}

function readFixedEntry(data, offset, entryLength, query, compare, state, page) {
  if (data.length < offset + entryLength + 12) {
    throw new SearchError(`page ${page}: fixed entry overruns the page`);
  }
  state.comparison = compare(query.word, data.subarray(offset, offset + entryLength));
  const hit = state.comparison === 0 ? hitAt(data, offset + entryLength) : null;
  return [offset + entryLength + 12, hit];
}

function readVariableEntry(data, offset, query, compare, state, page) {
  if (data.length < offset + 1) throw new SearchError(`page ${page}: variable entry overruns the page`);
  const entryLength = data[offset];
  if (data.length < offset + entryLength + 13) {
    throw new SearchError(`page ${page}: variable entry overruns the page`);
  }
  state.comparison = compare(query.word, data.subarray(offset + 1, offset + 1 + entryLength));
  const hit = state.comparison === 0 ? hitAt(data, offset + entryLength + 1) : null;
  return [offset + entryLength + 13, hit];
}

/** グループ化された葉ページから項目を 1 つ読む。 */
function readGroupEntry(data, offset, query, compareSingle, compareGroup, state, page, layout) {
  if (data.length < offset + 2) throw new SearchError(`page ${page}: group entry overruns the page`);
  const groupId = data[offset];
  const entryLength = data[offset + 1];

  if (groupId === GROUP_SINGLE) {
    if (data.length < offset + entryLength + 14) {
      throw new SearchError(`page ${page}: group entry overruns the page`);
    }
    state.comparison = compareSingle(query.canonicalized, data.subarray(offset + 2, offset + 2 + entryLength));
    state.inGroup = false;
    const hit = state.comparison === 0 ? hitAt(data, offset + entryLength + 2) : null;
    return [offset + entryLength + 14, hit];
  }
  if (groupId === GROUP_START) {
    const key = 2 + layout.groupCount;
    if (data.length < offset + entryLength + key) {
      throw new SearchError(`page ${page}: group header overruns the page`);
    }
    state.comparison = compareSingle(query.canonicalized, data.subarray(offset + key, offset + key + entryLength));
    state.inGroup = true;
    return [offset + entryLength + key, null];
  }
  if (groupId === GROUP_MEMBER) {
    if (!layout.memberKey) {
      if (data.length < offset + 13) throw new SearchError(`page ${page}: group member overruns the page`);
      const hit = state.comparison === 0 && state.inGroup ? hitAt(data, offset + 1) : null;
      return [offset + 13, hit];
    }
    if (data.length < offset + 14) throw new SearchError(`page ${page}: group member overruns the page`);
    let hit = null;
    if (state.comparison === 0 && state.inGroup) {
      const member = data.subarray(offset + 2, offset + 2 + entryLength);
      if (compareGroup(query.word, member) === 0) hit = hitAt(data, offset + entryLength + 2);
    }
    return [offset + entryLength + 14, hit];
  }
  throw new SearchError(`page ${page}: unknown group id 0x${groupId.toString(16)}`);
}

// -- 公開する入口 ---------------------------------------------------------------

/** 索引に収められた最大の鍵。読めなければ null。 */
async function lastKey(subbook, searchIndex) {
  let data;
  try {
    data = await subbook.readPage(searchIndex.endPage);
  } catch (error) {
    if (error instanceof EbError) return null;
    throw error;
  }
  if (data.length < 4 || !(data[0] & PAGE_ID_LEAF)) return null;

  const entryLength = data[1];
  const count = be(data, 2, 2);
  const hasGroup = Boolean(data[0] & PAGE_ID_HAS_GROUP);
  let offset = 4;
  let key = null;
  for (let entry = 0; entry < count; entry++) {
    if (offset + 2 > data.length) return null;
    if (hasGroup) {
      const groupId = data[offset];
      const length = data[offset + 1];
      if (groupId === GROUP_SINGLE) {
        key = data.subarray(offset + 2, offset + 2 + length);
        offset += length + 14;
      } else if (groupId === GROUP_START) {
        key = data.subarray(offset + 4, offset + 4 + length);
        offset += length + 4;
      } else if (groupId === GROUP_MEMBER) {
        key = data.subarray(offset + 2, offset + 2 + length);
        offset += length + 14;
      } else {
        return null;
      }
    } else if (entryLength === 0) {
      const length = data[offset];
      key = data.subarray(offset + 1, offset + 1 + length);
      offset += length + 13;
    } else {
      key = data.subarray(offset, offset + entryLength);
      offset += entryLength + 12;
    }
    if (offset > data.length) return null;
  }
  return key;
}

/** 葉ページの先頭の鍵。葉ページでなければ null。 */
async function firstKey(subbook, page) {
  let data;
  try {
    data = await subbook.readPage(page);
  } catch (error) {
    if (error instanceof EbError) return null;
    throw error;
  }
  if (data.length < 6 || !(data[0] & PAGE_ID_LEAF) || be(data, 2, 2) === 0) return null;

  const entryLength = data[1];
  if (data[0] & PAGE_ID_HAS_GROUP) {
    const groupId = data[4];
    const length = data[5];
    const start = groupId === GROUP_START ? 8 : 6;
    if (![GROUP_SINGLE, GROUP_START, GROUP_MEMBER].includes(groupId)) return null;
    return data.subarray(start, start + length);
  }
  if (entryLength === 0) return data.subarray(5, 5 + data[4]);
  return data.subarray(4, 4 + entryLength);
}

/** 一致がありえない葉を飛ばして、走査を先へ進める。各葉の先頭の鍵を 2 分探索する。 */
async function skipToPage(subbook, query, compare, page) {
  if (KANA_INDEX_IDS.includes(query.search.indexId)) return page;
  let low = page;
  let high = query.search.endPage;
  if (high <= low) return page;
  let best = page;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const key = await firstKey(subbook, middle);
    if (key === null) return best;
    if (compare(query.word, key) > 0) {
      best = middle;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  return best;
}

/** その語は、索引の全項目より後ろに並ぶか。 */
async function beyondTheIndex(subbook, query, compare) {
  if (KANA_INDEX_IDS.includes(query.search.indexId)) return false;
  const key = await lastKey(subbook, query.search);
  return key !== null && compare(query.word, key) > 0;
}

/**
 * subbook から text を検索する。
 *
 * exact=false（既定）なら text で始まる見出し語がすべて一致する。exact=true なら
 * 見出し語全体が一致するものだけ。backward は代わりに endword 索引を引く。
 */
export async function search(subbook, text, { exact = false, limit = null, backward = false } = {}) {
  const [query, , [comparePre, compareSingle, compareGroup]] = await prepare(subbook, text, exact, backward);

  if (await beyondTheIndex(subbook, query, compareSingle)) return [];

  let leafPage = await descend(subbook, query.search.startPage, query, comparePre);
  if (leafPage === null) return [];
  leafPage = await skipToPage(subbook, query, compareSingle, leafPage);

  // かなの索引は見出し語をひらがなとカタカナで 1 回ずつ収めるので、
  // 項目の位置で重複を見分ける。他の索引では見出しと本文の両方で。
  const kana = KANA_INDEX_IDS.includes(query.search.indexId);
  const identity = kana ? (hit) => hit.text.key : (hit) => hit.key;

  const hits = [];
  const seen = new Set();
  for await (const hit of iterLeafHits(subbook, leafPage, query, compareSingle, compareGroup)) {
    const key = identity(hit);
    if (seen.has(key)) continue;
    seen.add(key);
    hits.push(hit);
    if (limit !== null && hits.length >= limit) break;
  }
  return hits;
}

/** 根のページから、その下の一番左の葉まで降りる。 */
async function firstLeaf(subbook, page) {
  for (let depth = 0; depth < MAX_INDEX_DEPTH; depth++) {
    const data = await subbook.readPage(page);
    if (data.length < 4) throw new SearchError(`page ${page}: truncated index page`);
    if (data[0] & PAGE_ID_LEAF) return page;
    const entryLength = data[1];
    if (data.length < 4 + entryLength + 4) throw new SearchError(`page ${page}: index entry overruns the page`);
    const child = be(data, 4 + entryLength, 4);
    if (child === page) throw new SearchError(`page ${page}: index points at itself`);
    page = child;
  }
  throw new SearchError(`index is deeper than ${MAX_INDEX_DEPTH} levels`);
}

/** サブブックのある索引に収められた項目を、索引順にすべて返す。 */
export async function* iterIndex(subbook, name = "word_asis") {
  await subbook.load();
  const searchIndex = subbook.searches[name];
  if (searchIndex === undefined || searchIndex.startPage === 0) {
    throw new NoSuchSearchError(`${JSON.stringify(subbook.title)}: no ${name} index`);
  }
  const everything = () => 0;
  const query = new Query(new Uint8Array(0), new Uint8Array(0), WORD_ALPHABET, searchIndex);
  const page = await firstLeaf(subbook, searchIndex.startPage);
  yield* iterLeafHits(subbook, page, query, everything, everything);
}

export const searchWord = (subbook, text, limit = null) => search(subbook, text, { limit });
export const searchEndword = (subbook, text, limit = null) => search(subbook, text, { limit, backward: true });
export const searchExactword = (subbook, text, limit = null) => search(subbook, text, { exact: true, limit });

// -- ワイルドカード -------------------------------------------------------------

const WILDCARD = "*";

/** 見出し語のどちらの端に錨を下ろすかを示す `*` を読む。 */
export function parsePattern(text) {
  let word = text.trim();
  const backward = word.startsWith(WILDCARD);
  if (backward) word = word.slice(1);
  const forward = word.endsWith(WILDCARD);
  if (forward) word = word.slice(0, -1);
  word = word.trim();
  if (backward && forward && word) {
    throw new WordError(
      `${JSON.stringify(text)}: matching both ends at once would need a substring index, which these dictionaries do not carry`,
    );
  }
  return { word, backward };
}

export function searchPattern(subbook, pattern, { exact = false, limit = null } = {}) {
  const { word, backward } = parsePattern(pattern);
  return search(subbook, word, { exact, limit, backward });
}

// -- keyword 検索と cross 検索 ----------------------------------------------------

const KEYWORD_INDEXES = ["keyword", "cross"];
const GROUP_MEMBER_SIZE = 7;
const POSITION_SIZE = 6;

async function keywordIndex(subbook, name) {
  await subbook.load();
  if (!KEYWORD_INDEXES.includes(name)) throw new NoSuchSearchError(`${JSON.stringify(name)} is not a keyword index`);
  const searchIndex = subbook.searches[name];
  if (searchIndex === undefined || searchIndex.startPage === 0) {
    throw new NoSuchSearchError(`${JSON.stringify(subbook.title)}: no ${name} index`);
  }
  return searchIndex;
}

function keywordQuery(subbook, word, searchIndex) {
  const [raw, wordCode] = convert(subbook, word);
  return fixWord(searchIndex, raw, subbook.book.characterCode, wordCode);
}

/**
 * keyword（または cross）索引で word が収められている箇所をすべて返す。
 *
 * グループの構成要素の見出しは索引に入っていない。グループの見出し位置は
 * 見出しの連なりの始まりで、各構成要素の見出しは 1 つ前の見出しを読み終えた
 * ところにある。nextHeading(position) が「その見出しを読み終えた位置」を
 * 返す（text.js が提供する）。null なら見出しは読まず、ヒットの見出しは
 * グループ自身のものになる——最初の構成要素については正しく、残りは誤る。
 */
export async function* iterKeywordHits(subbook, word, { index = "keyword", nextHeading = null } = {}) {
  const searchIndex = await keywordIndex(subbook, index);
  const query = keywordQuery(subbook, word, searchIndex);
  const latin = subbook.book.characterCode === CHARCODE_ISO8859_1;
  const comparePre = latin ? match.exactPreMatchWordLatin : match.exactPreMatchWordJis;
  const compare = latin ? match.exactMatchWordLatin : match.exactMatchWordJis;

  let page = await descend(subbook, searchIndex.startPage, query, comparePre);
  if (page === null) return;

  let inGroup = false;
  let heading = null;
  for (;;) {
    const data = await subbook.readPage(page);
    if (data.length < 4) throw new SearchError(`page ${page}: truncated keyword page`);
    if (!(data[0] & PAGE_ID_LEAF)) throw new SearchError(`page ${page}: expected a keyword leaf page`);
    const count = be(data, 2, 2);
    let offset = 4;

    for (let entry = 0; entry < count; entry++) {
      if (offset >= data.length) throw new SearchError(`page ${page}: keyword entry overruns the page`);
      const groupId = data[offset];

      if (groupId === GROUP_SINGLE) {
        const length = data[offset + 1];
        const base = offset + 2 + length;
        const comparison = compare(query.word, data.subarray(offset + 2, base));
        if (comparison === 0) {
          yield new Hit(
            new Position(be(data, base, 4), be(data, base + 4, 2)),
            new Position(be(data, base + 6, 4), be(data, base + 10, 2)),
          );
        } else if (comparison < 0) {
          return;
        }
        inGroup = false;
        offset = base + 2 * POSITION_SIZE;
      } else if (groupId === GROUP_START) {
        const length = data[offset + 1];
        const comparison = compare(query.word, data.subarray(offset + 6, offset + 6 + length));
        if (comparison < 0) return;
        inGroup = comparison === 0;
        const base = offset + 6 + length;
        heading = new Position(be(data, base, 4), be(data, base + 4, 2));
        offset = base + POSITION_SIZE;
      } else if (groupId === GROUP_MEMBER) {
        if (inGroup && heading !== null) {
          const text = new Position(be(data, offset + 1, 4), be(data, offset + 5, 2));
          yield new Hit(text, heading);
          if (nextHeading !== null) heading = await nextHeading(heading);
        }
        offset += GROUP_MEMBER_SIZE;
      } else {
        throw new SearchError(`page ${page}: unknown keyword entry id 0x${groupId.toString(16)}`);
      }
    }
    if (data[0] & PAGE_ID_LAYER_END) return;
    page += 1;
  }
}

/** keyword が指す項目だけを返す。見出しは 1 つも読まない。 */
export async function keywordPositions(subbook, word, index = "keyword") {
  const positions = new Map();
  for await (const hit of iterKeywordHits(subbook, word, { index })) {
    positions.set(hit.text.key, hit.text);
  }
  return positions;
}

/** words のすべてを含む項目を返す。 */
export async function searchKeyword(subbook, words, { index = "keyword", limit = null, nextHeading = null } = {}) {
  if (typeof words === "string") words = [words];
  words = words.filter((word) => word.trim());
  if (words.length === 0) return [];

  // いちばん珍しい語の一覧をたどる。
  let others = [];
  let driver = words[0];
  if (words.length > 1) {
    const found = new Map();
    for (const word of words) found.set(word, await keywordPositions(subbook, word, index));
    driver = words.reduce((best, word) => (found.get(word).size < found.get(best).size ? word : best));
    others = words.filter((word) => word !== driver).map((word) => found.get(word));
  }

  const hits = [];
  const seen = new Set();
  for await (const hit of iterKeywordHits(subbook, driver, { index, nextHeading })) {
    const key = hit.text.key;
    if (seen.has(key)) continue;
    if (others.some((other) => !other.has(key))) continue;
    seen.add(key);
    hits.push(hit);
    if (limit !== null && hits.length >= limit) break;
  }
  return hits;
}

// -- multi 検索 -------------------------------------------------------------------

async function multiSearch(subbook, number) {
  const multis = await subbook.multis();
  if (multis.length === 0) throw new NoSuchSearchError(`${JSON.stringify(subbook.title)}: no multi search`);
  if (number < 0 || number >= multis.length) {
    throw new NoSuchSearchError(
      `${JSON.stringify(subbook.title)}: no multi search ${number}; it has ${multis.length}`,
    );
  }
  return multis[number];
}

/** multi 検索のある欄が word で一致させる項目を返す。 */
export async function* iterMultiField(subbook, entry, word, exact = false) {
  const searchIndex = entry.index;
  if (searchIndex === null) throw new NoSuchSearchError(`${JSON.stringify(entry.label)}: this field has no index`);
  const [raw, wordCode] = convert(subbook, word);
  const query = fixWord(searchIndex, raw, subbook.book.characterCode, wordCode);
  const kana = MULTI_KANA_INDEX_IDS.has(searchIndex.indexId);
  const [comparePre, compareSingle, compareGroup] = comparators(subbook, kana ? "word_kana" : "word_asis", exact);
  const page = await descend(subbook, searchIndex.startPage, query, comparePre);
  if (page === null) return;
  yield* iterLeafHits(subbook, page, query, compareSingle, compareGroup, MULTI_LAYOUT);
}

/** multi 検索の欄を埋め、そのすべてを満たすものを返す。 */
export async function searchMulti(subbook, number, words, { exact = false, limit = null } = {}) {
  const multi = await multiSearch(subbook, number);
  if (typeof words === "string") words = [words];
  const asked = [];
  multi.entries.forEach((entry, i) => {
    const word = words[i];
    if (word && word.trim()) asked.push([entry, word]);
  });
  if (asked.length === 0) return [];

  const others = [];
  for (const [entry, word] of asked.slice(1)) {
    const positions = new Set();
    for await (const hit of iterMultiField(subbook, entry, word, exact)) positions.add(hit.text.key);
    others.push(positions);
  }

  const hits = [];
  const seen = new Set();
  const [entry, word] = asked[0];
  for await (const hit of iterMultiField(subbook, entry, word, exact)) {
    const key = hit.text.key;
    if (seen.has(key)) continue;
    if (others.some((other) => !other.has(key))) continue;
    seen.add(key);
    hits.push(hit);
    if (limit !== null && hits.length >= limit) break;
  }
  return hits;
}
