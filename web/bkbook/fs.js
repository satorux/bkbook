// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux

/**
 * ファイルの在り処を抽象化する。
 *
 * Python 版は os.listdir と open(2) を直接呼ぶが、ブラウザにはどちらもない。
 * 代わりに「ディレクトリの中身を列挙する」「ファイルの一部分を読む」の 2 つを
 * インターフェースにして、Node ではローカルファイル、ブラウザでは HTTP の
 * Range リクエストで実装する。
 *
 *   fs.join(directory, name)      -> path
 *   fs.listdir(directory)         -> Promise<string[] | null>   (読めなければ null)
 *   fs.isdir(path)                -> Promise<boolean>
 *   fs.open(path)                 -> Promise<File>
 *   file.size()                   -> Promise<number>
 *   file.read(offset, length)     -> Promise<Uint8Array>        (末尾を越えれば短く返る)
 *   file.close()
 *
 * HTTP にはディレクトリ一覧がないので、HttpFs はファイルの一覧（manifest）を
 * 受け取ってそれを listdir の答えにする。一覧は `find . -type f` の出力で足りる。
 */

// -- Node ---------------------------------------------------------------

export class NodeFs {
  constructor() {
    this._fs = null;
    this._path = null;
  }

  async _load() {
    if (this._fs === null) {
      this._fs = await import("node:fs/promises");
      this._path = await import("node:path");
    }
  }

  join(directory, name) {
    return directory.endsWith("/") ? directory + name : `${directory}/${name}`;
  }

  async listdir(directory) {
    await this._load();
    try {
      return await this._fs.readdir(directory);
    } catch {
      return null;
    }
  }

  async isdir(path) {
    await this._load();
    try {
      return (await this._fs.stat(path)).isDirectory();
    } catch {
      return false;
    }
  }

  async open(path) {
    await this._load();
    const handle = await this._fs.open(path, "r");
    return new NodeFile(handle);
  }
}

class NodeFile {
  constructor(handle) {
    this._handle = handle;
  }

  async size() {
    return (await this._handle.stat()).size;
  }

  async read(offset, length) {
    const buffer = new Uint8Array(length);
    const { bytesRead } = await this._handle.read(buffer, 0, length, offset);
    return buffer.subarray(0, bytesRead);
  }

  async close() {
    await this._handle.close();
  }
}

// -- HTTP ---------------------------------------------------------------

/**
 * 取ってきたバイト範囲を、ファイルをまたいで保持する LRU キャッシュ。
 * ブロック単位で取るので、隣り合う小さな読み出しが 1 回のリクエストにまとまる。
 */
export class BlockCache {
  constructor({ blockSize = 32768, maxBlocks = 2048 } = {}) {
    this.blockSize = blockSize;
    this.maxBlocks = maxBlocks;
    this._blocks = new Map(); // key -> Promise<Uint8Array>
    this.requests = 0; // 統計。実際に飛んだリクエストの数。
  }

  get(key, fetch) {
    const cached = this._blocks.get(key);
    if (cached !== undefined) {
      this._blocks.delete(key);
      this._blocks.set(key, cached);
      return cached;
    }
    this.requests += 1;
    const promise = fetch().catch((error) => {
      this._blocks.delete(key); // 失敗を覚え込まない
      throw error;
    });
    this._blocks.set(key, promise);
    if (this._blocks.size > this.maxBlocks) {
      const oldest = this._blocks.keys().next().value;
      this._blocks.delete(oldest);
    }
    return promise;
  }
}

export class HttpFs {
  /**
   * @param {string} baseUrl   辞書のトップ。`eb/` など。末尾の `/` は任意。
   * @param {string[]} manifest baseUrl からの相対パスの一覧。ファイルだけでよい。
   */
  constructor(baseUrl, manifest, { cache = new BlockCache(), fetch: fetchImpl = globalThis.fetch.bind(globalThis) } = {}) {
    this.baseUrl = baseUrl.endsWith("/") ? baseUrl : baseUrl + "/";
    this.cache = cache;
    this._fetch = fetchImpl;
    this._files = new Set();
    this._dirs = new Map(); // dir -> Set(names)
    this._dirs.set("", new Set());
    for (const raw of manifest) {
      const path = raw.replace(/^\.?\//, "").replace(/\/$/, "");
      if (!path) continue;
      this._files.add(path);
      const parts = path.split("/");
      for (let i = 1; i <= parts.length; i++) {
        const parent = parts.slice(0, i - 1).join("/");
        if (!this._dirs.has(parent)) this._dirs.set(parent, new Set());
        this._dirs.get(parent).add(parts[i - 1]);
      }
      for (let i = 1; i < parts.length; i++) {
        const dir = parts.slice(0, i).join("/");
        if (!this._dirs.has(dir)) this._dirs.set(dir, new Set());
      }
    }
  }

  static async fromManifestUrl(baseUrl, manifestUrl, options) {
    const response = await fetch(manifestUrl);
    if (!response.ok) throw new Error(`${manifestUrl}: ${response.status}`);
    const text = await response.text();
    return new HttpFs(baseUrl, text.split("\n").map((line) => line.trim()).filter(Boolean), options);
  }

  _normalize(path) {
    return path.replace(/^\.?\//, "").replace(/\/$/, "");
  }

  join(directory, name) {
    directory = this._normalize(directory);
    return directory ? `${directory}/${name}` : name;
  }

  async listdir(directory) {
    const names = this._dirs.get(this._normalize(directory));
    return names ? [...names] : null;
  }

  async isdir(path) {
    return this._dirs.has(this._normalize(path));
  }

  async open(path) {
    path = this._normalize(path);
    if (!this._files.has(path)) throw new Error(`${path}: not in manifest`);
    return new HttpFile(this.baseUrl + path.split("/").map(encodeURIComponent).join("/"), this.cache, this._fetch);
  }
}

class HttpFile {
  constructor(url, cache, fetchImpl) {
    this.url = url;
    this._cache = cache;
    this._fetch = fetchImpl;
    this._size = null;
  }

  async size() {
    if (this._size === null) {
      // 最初のブロックを取れば Content-Range が全体の大きさを教えてくれる。
      await this._block(0);
      if (this._size === null) {
        const response = await this._fetch(this.url, { method: "HEAD" });
        if (!response.ok) throw new Error(`${this.url}: ${response.status}`);
        this._size = Number(response.headers.get("content-length"));
      }
    }
    return this._size;
  }

  _block(index) {
    const blockSize = this._cache.blockSize;
    return this._cache.get(`${this.url}#${index}`, async () => {
      const start = index * blockSize;
      const end = start + blockSize - 1;
      const response = await this._fetch(this.url, { headers: { Range: `bytes=${start}-${end}` } });
      if (response.status === 416) {
        return new Uint8Array(0);
      }
      if (response.status === 200) {
        // サーバーが Range を無視した。ファイル全体が返ってきている。
        const whole = new Uint8Array(await response.arrayBuffer());
        this._size = whole.length;
        return whole.subarray(start, start + blockSize);
      }
      if (response.status !== 206) {
        throw new Error(`${this.url}: ${response.status}`);
      }
      const range = response.headers.get("content-range");
      const match = range && /\/(\d+)$/.exec(range);
      if (match) this._size = Number(match[1]);
      return new Uint8Array(await response.arrayBuffer());
    });
  }

  async read(offset, length) {
    if (length <= 0) return new Uint8Array(0);
    const blockSize = this._cache.blockSize;
    const first = Math.floor(offset / blockSize);
    const last = Math.floor((offset + length - 1) / blockSize);
    const blocks = await Promise.all(
      Array.from({ length: last - first + 1 }, (_, i) => this._block(first + i)),
    );
    const out = new Uint8Array(length);
    let filled = 0;
    for (let i = 0; i < blocks.length; i++) {
      const blockStart = (first + i) * blockSize;
      const from = Math.max(offset, blockStart) - blockStart;
      const to = Math.min(offset + length, blockStart + blockSize) - blockStart;
      const chunk = blocks[i].subarray(from, Math.min(to, blocks[i].length));
      out.set(chunk, filled);
      filled += chunk.length;
      if (chunk.length < to - from) break; // ファイル末尾
    }
    return out.subarray(0, filled);
  }

  async close() {}
}
