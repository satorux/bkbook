// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux
// Copyright (c) 1997-2006 Motoyuki Kasahara

/**
 * EB / EPWING のデータファイルのブロック入出力。展開は透過的に行う。
 *
 * Python 版 zio.py の移植。読み出しはすべて非同期で、fs.js の File を通す。
 * ebzip のスライスは zlib ストリームなので DecompressionStream("deflate") で
 * 展開できる。
 */

export const PAGE_SIZE = 2048;

const EBZIP_HEADER_SIZE = 22;
const EBZIP_MAX_LEVEL = 5;

/** 「このファイルは ebzip 圧縮」を意味するファイル名の拡張子。 */
export const EBZIP_SUFFIX = ".ebz";

/** bkbook が投げる例外すべての基底クラス。 */
export class EbError extends Error {}

/** データファイルが壊れているか、途中で切れているか、未対応の形式。 */
export class ZioError extends EbError {}

/** 任意の幅のビッグエンディアン符号なし整数を読む。 */
export function be(data, offset, width) {
  let value = 0;
  for (let i = 0; i < width; i++) value = value * 256 + data[offset + i];
  return value;
}

function normalizeEntry(name) {
  let stem = name;
  const version = stem.lastIndexOf(";");
  if (version >= 0) stem = stem.slice(0, version);
  if (stem.endsWith(".")) stem = stem.slice(0, -1);
  return stem.toLowerCase();
}

/**
 * 本のファイルを探す。大文字小文字、`;1`、`.ebz` を吸収する。
 * 無圧縮のファイルがあればそのパスを、なければ ebzip 版を、それもなければ null。
 */
export async function findFile(fs, directory, basename) {
  const entries = await fs.listdir(directory);
  if (entries === null) return null;
  const wanted = basename.toLowerCase();
  let plain = null;
  let zipped = null;
  for (const entry of entries) {
    const normalized = normalizeEntry(entry);
    if (normalized === wanted) {
      plain = plain || fs.join(directory, entry);
    } else if (normalized === wanted + EBZIP_SUFFIX) {
      zipped = zipped || fs.join(directory, entry);
    }
  }
  return plain || zipped;
}

/** catalog に書かれたサブディレクトリ名を、大文字小文字を無視して解決する。 */
export async function findDirectory(fs, parent, name) {
  if (!name) return null;
  const entries = await fs.listdir(parent);
  if (entries === null) return null;
  const wanted = name.toLowerCase();
  for (const entry of entries) {
    if (entry.toLowerCase() === wanted) {
      const path = fs.join(parent, entry);
      if (await fs.isdir(path)) return path;
    }
  }
  return null;
}

/** zlib ストリームを展開する。maxLength バイトを越えた分は捨てる。 */
export async function inflate(raw, maxLength) {
  const stream = new DecompressionStream("deflate");
  const writer = stream.writable.getWriter();
  const writing = writer.write(raw).then(() => writer.close());
  writing.catch(() => {});
  const reader = stream.readable.getReader();
  const chunks = [];
  let total = 0;
  try {
    while (total < maxLength) {
      const { value, done } = await reader.read();
      if (done) break;
      chunks.push(value);
      total += value.length;
    }
  } catch (error) {
    if (total === 0) throw error;
    // 末尾に余分なバイトがあっても、展開できた分は正しい。
  } finally {
    reader.cancel().catch(() => {});
  }
  const out = new Uint8Array(Math.min(total, maxLength));
  let at = 0;
  for (const chunk of chunks) {
    const take = Math.min(chunk.length, out.length - at);
    if (take <= 0) break;
    out.set(chunk.subarray(0, take), at);
    at += take;
  }
  return out;
}

/** 読み出し可能な EB データファイル。展開は透過的に行われる。 */
export class Zio {
  constructor(path, file, cacheSlices) {
    this.path = path;
    this._file = file;
    this._cache = new Map(); // index -> Promise<Uint8Array>
    this._cacheMax = Math.max(1, cacheSlices);
    this.code = "plain";
    this.sliceSize = PAGE_SIZE;
    this.fileSize = 0;
    this.indexWidth = 0;
  }

  static async open(fs, path, { cacheSlices = 256 } = {}) {
    const file = await fs.open(path);
    const zio = new Zio(path, file, cacheSlices);
    try {
      const header = await file.read(0, EBZIP_HEADER_SIZE);
      if (header.length >= 5 && String.fromCharCode(...header.subarray(0, 5)) === "EBZip") {
        zio._initEbzip(header);
      } else {
        zio.fileSize = await file.size();
      }
    } catch (error) {
      await file.close();
      throw error;
    }
    return zio;
  }

  _initEbzip(header) {
    if (header.length < EBZIP_HEADER_SIZE) {
      throw new ZioError(`${this.path}: truncated ebzip header`);
    }
    const mode = header[5] >> 4;
    const level = header[5] & 0x0f;
    if (mode !== 1 && mode !== 2) {
      throw new ZioError(`${this.path}: unsupported ebzip mode ${mode}`);
    }
    if (level > EBZIP_MAX_LEVEL) {
      throw new ZioError(`${this.path}: unsupported ebzip level ${level}`);
    }
    this.code = "ebzip1";
    this.zipLevel = level;
    this.sliceSize = PAGE_SIZE << level;
    this.fileSize = be(header, 9, 5);
    this.adler32 = be(header, 14, 4);
    this.mtime = be(header, 18, 4);

    const size = this.fileSize;
    if (size < 1 << 16) this.indexWidth = 2;
    else if (size < 1 << 24) this.indexWidth = 3;
    else if (size < 2 ** 32) this.indexWidth = 4;
    else this.indexWidth = 5;
  }

  // -- reading -----------------------------------------------------------

  /** バイト位置 location から length バイト読む。末尾を越えれば短く返る。 */
  async read(location, length) {
    if (location < 0) throw new RangeError("location must not be negative");
    if (length <= 0) return new Uint8Array(0);
    const end = Math.min(location + length, this.fileSize);
    if (end <= location) return new Uint8Array(0);

    if (this.code === "plain") {
      return this._file.read(location, end - location);
    }

    // 必要なスライスをまとめて要求し、揃ってから切り出す。
    const first = Math.floor(location / this.sliceSize);
    const last = Math.floor((end - 1) / this.sliceSize);
    const slices = await Promise.all(
      Array.from({ length: last - first + 1 }, (_, i) => this._slice(first + i)),
    );
    const out = new Uint8Array(end - location);
    let filled = 0;
    let position = location;
    for (const data of slices) {
      const offset = position % this.sliceSize;
      const count = Math.min(this.sliceSize - offset, end - position);
      const chunk = data.subarray(offset, offset + count);
      out.set(chunk, filled);
      filled += chunk.length;
      position += count;
      if (chunk.length < count) break; // スライスが短い
    }
    return out.subarray(0, filled);
  }

  /** 1 起点のページ番号 page から count ページ読む。 */
  async readPage(page, count = 1) {
    if (page < 1) throw new RangeError("page numbers are 1-origin");
    return this.read((page - 1) * PAGE_SIZE, count * PAGE_SIZE);
  }

  _slice(index) {
    const cached = this._cache.get(index);
    if (cached !== undefined) {
      this._cache.delete(index);
      this._cache.set(index, cached);
      return cached;
    }
    const promise = this._readSlice(index).catch((error) => {
      this._cache.delete(index);
      throw error;
    });
    this._cache.set(index, promise);
    if (this._cache.size > this._cacheMax) {
      this._cache.delete(this._cache.keys().next().value);
    }
    return promise;
  }

  async _readSlice(index) {
    const width = this.indexWidth;
    const entry = await this._file.read(EBZIP_HEADER_SIZE + index * width, width * 2);
    if (entry.length !== width * 2) {
      throw new ZioError(`${this.path}: truncated slice index at ${index}`);
    }
    const start = be(entry, 0, width);
    const next = be(entry, width, width);
    const size = next - start;
    if (size <= 0 || size > this.sliceSize) {
      throw new ZioError(`${this.path}: bad slice size ${size} at ${index}`);
    }
    const raw = await this._file.read(start, size);
    if (raw.length !== size) {
      throw new ZioError(`${this.path}: truncated slice ${index}`);
    }
    if (size === this.sliceSize) {
      return raw; // 圧縮できなかったスライスはそのまま入っている
    }
    return inflate(raw, this.sliceSize);
  }

  // -- lifecycle ---------------------------------------------------------

  async close() {
    this._cache.clear();
    await this._file.close();
  }
}
