// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux

/**
 * 項目がどこで終わるのかを、教えてもらわずに突き止める。Python 版 stopcode.py の移植。
 *
 * 索引はすべての項目の開始位置を持っているので、項目の 1 文字目の直前にある
 * バイトが、1 つ前の項目を終わらせたバイトである。数百項目を抜き出して共通の
 * エスケープを候補にし、候補ごとに項目を読み連ねて、毎回きちんと次の項目の
 * 境界に着地するものを選ぶ。
 */

import { NoSuchSearchError, Position, iterIndex } from "./search.js";
import { DEFAULT_MAX_LENGTH, Renderer, STOP_SOFT, readText } from "./text.js";
import { EbError, PAGE_SIZE } from "./zio.js";

/** 項目を終わらせうるエスケープ。字下げと、見出し語を開く keyword。 */
const STOP_ESCAPES = [0x1f09, 0x1f41];

/** 項目の 1 バイト目から、開始用エスケープをどこまで探すか。 */
const PROLOGUE_OFFSETS = [-4, 0, 4];

const MIN_SHARE = 0.5;
const TIE = 0.03;

export const DEFAULT_SAMPLE = 1200;
export const DEFAULT_TRIALS = 60;
export const DEFAULT_DEPTH = 25;

/** 項目を終わらせているかもしれないエスケープと、その前置きの中での位置。 */
export class Candidate {
  constructor(code, argument, offset, share = 0, score = 0) {
    this.code = code;
    this.argument = argument;
    this.offset = offset;
    this.share = share;
    this.score = score;
  }

  get stopCode() {
    return [this.code, this.argument];
  }
}

const location = (position) => (position.page - 1) * PAGE_SIZE + position.offset;
const position = (loc) => new Position(Math.floor(loc / PAGE_SIZE) + 1, loc % PAGE_SIZE);

/** 項目の開始位置を、その本が持つ見出し語索引すべてから集める。 */
async function sampleStarts(subbook, limit) {
  const names = ["word_asis", "word_kana", "word_alphabet"].filter(
    (name) => name in subbook.searches && subbook.searches[name].startPage,
  );
  const starts = new Set();
  for (const name of names) {
    const perIndex = Math.max(1, Math.floor(limit / names.length));
    let count = 0;
    try {
      for await (const hit of iterIndex(subbook, name)) {
        if (count >= perIndex) break;
        count += 1;
        starts.add(location(hit.text));
      }
    } catch (error) {
      if (error instanceof NoSuchSearchError) continue;
      throw error;
    }
  }
  return [...starts].sort((a, b) => a - b);
}

/** 項目の 1 バイト目の前後にある字下げ／keyword のエスケープを読む。 */
async function escapesAt(zio, start) {
  const base = start + PROLOGUE_OFFSETS[0];
  if (base < 0) return [];
  const data = await zio.read(base, 4 + PROLOGUE_OFFSETS[PROLOGUE_OFFSETS.length - 1] - PROLOGUE_OFFSETS[0]);
  const found = [];
  for (const offset of PROLOGUE_OFFSETS) {
    const at = offset - PROLOGUE_OFFSETS[0];
    if (data.length < at + 4) continue;
    const code = (data[at] << 8) | data[at + 1];
    if (STOP_ESCAPES.includes(code)) found.push([offset, code, (data[at + 2] << 8) | data[at + 3]]);
  }
  return found;
}

/** 抜き出した項目の（ほぼ）すべてを開いているエスケープを探す。 */
async function candidates(zio, starts) {
  const counts = new Map();
  for (const start of starts) {
    for (const [offset, code, argument] of await escapesAt(zio, start)) {
      const key = `${code},${argument},${offset}`;
      counts.set(key, (counts.get(key) || 0) + 1);
    }
  }
  const total = starts.length || 1;
  const found = [];
  for (const [key, count] of counts) {
    if (count / total >= MIN_SHARE) {
      const [code, argument, offset] = key.split(",").map(Number);
      found.push(new Candidate(code, argument, offset, count / total));
    }
  }
  // 前にあるものから順に。境界に近いほうが appendix の挙げているもの。
  found.sort((a, b) => a.offset - b.offset || b.share - a.share);
  return found;
}

const bytesKey = (data) => Array.from(data, (b) => b.toString(16).padStart(2, "0")).join("");

/** 項目の始まりを示すバイト列。よくある形と、まれでも繰り返し現れる形。 */
async function signatures(zio, starts, found) {
  const offsets = [...new Set(found.filter((c) => c.offset >= 0).map((c) => c.offset))].sort((a, b) => a - b);
  if (offsets.length === 0) return { shapes: new Set(), width: 0 };
  const width = offsets[offsets.length - 1] + 4;

  const shapes = new Map();
  for (const start of starts) {
    const data = await zio.read(start, width);
    if (data.length !== width) continue;
    let ok = true;
    for (let at = 0; at < width; at += 4) if (data[at] !== 0x1f) ok = false;
    if (!ok) continue;
    const key = bytesKey(data);
    shapes.set(key, (shapes.get(key) || 0) + 1);
  }
  const floor = Math.max(2, Math.floor(starts.length / 200));
  return { shapes: new Set([...shapes].filter(([, count]) => count >= floor).map(([key]) => key)), width };
}

/** ある位置が項目の始まりかどうかを判定するのに要るもの一式。 */
class Boundaries {
  constructor(zio, starts, sigs) {
    this.zio = zio;
    this.known = new Set(starts);
    this.signatures = sigs.shapes;
    this.width = sigs.shapes.size ? sigs.width : 0;
  }

  async has(loc) {
    if (this.known.has(loc)) return true;
    if (!this.width || loc < 0) return false;
    return this.signatures.has(bytesKey(await this.zio.read(loc, this.width)));
  }
}

/** この stop code で項目から次の項目へどれだけ確実に渡っていけるか。 */
async function score(subbook, candidate, seeds, boundaries, depth) {
  let good = 0;
  let bad = 0;
  const read = new Set();
  for (const seed of seeds) {
    let loc = seed;
    for (let step = 0; step < depth; step++) {
      if (read.has(loc)) break; // 先行する連鎖に追いついた
      read.add(loc);
      let result;
      try {
        result = await readText(subbook, position(loc), {
          renderer: new Renderer(),
          stopCode: candidate.stopCode,
          maxLength: DEFAULT_MAX_LENGTH,
        });
      } catch (error) {
        if (error instanceof EbError) break;
        throw error;
      }
      if (result.stop !== STOP_SOFT) break;
      loc = location(result.nextPosition) - 4 - candidate.offset;
      if (!(await boundaries.has(loc))) {
        bad += 1;
        break;
      }
      good += 1;
    }
  }
  return good || bad ? good / (good + bad) : 0;
}

/** 候補に点をつける。前置きの中で前にあるものから順に。 */
async function weigh(subbook, sample, trials, depth, allOfThem = true) {
  await subbook.load();
  const zio = await subbook.zio();
  const starts = await sampleStarts(subbook, sample);
  if (starts.length < 8) return [];
  const found = await candidates(zio, starts);
  if (found.length === 0) return [];

  const boundaries = new Boundaries(zio, starts, await signatures(zio, starts, found));
  const stride = Math.max(1, Math.floor(starts.length / trials));
  const seeds = starts.filter((_, i) => i % stride === 0).slice(0, trials);

  const scored = [];
  for (const candidate of found) {
    const s = await score(subbook, candidate, seeds, boundaries, depth);
    scored.push(new Candidate(candidate.code, candidate.argument, candidate.offset, candidate.share, s));
    if (s >= 1.0 && !allOfThem) break;
  }
  return scored;
}

/**
 * このサブブックで項目を終わらせているエスケープシーケンスを求める。
 * [escape, argument] の組——たいていは [0x1f09, 0x0001]——か、決められなければ null。
 */
export async function infer(subbook, { sample = DEFAULT_SAMPLE, trials = DEFAULT_TRIALS, depth = DEFAULT_DEPTH } = {}) {
  const scored = await weigh(subbook, sample, trials, depth, false);
  if (scored.length === 0) return null;
  const top = Math.max(...scored.map((c) => c.score));
  if (top < MIN_SHARE) return null;
  const near = scored.filter((c) => c.score >= top - TIE);
  return near.reduce((best, c) => (c.offset < best.offset ? c : best)).stopCode;
}

/** 全候補とその点数を、良い順に。 */
export async function report(subbook, { sample = DEFAULT_SAMPLE, trials = DEFAULT_TRIALS, depth = DEFAULT_DEPTH } = {}) {
  const scored = await weigh(subbook, sample, trials, depth);
  return scored.sort((a, b) => b.score - a.score || a.offset - b.offset);
}
