// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux

/**
 * 本文イベントを HTML にするレンダラ。Web UI のためのもので、Python 版にはない。
 *
 * 強調は <b>、上付き・下付きは <sup>/<sub>、参照は <a data-ref="page:offset">、
 * 見出し語の範囲は <span class="kw">、対応表にない外字は <span class="gaiji">
 * の空箱にしておき、あとで UI がフォントのビットマップで埋める。
 *
 * テキストは cli.py の _readable と同じ整形をかける。全角英数を半角に畳み、
 * 音声・画像の印を落とし、研究社 のアクセントの角を上線に戻す。
 */

import { formatGaijiCode, Renderer } from "./text.js";

const MEDIA_MARKER = /[ 　]*<(?:sound|image)=[0-9A-Fa-f]+:[0-9A-Fa-f]+>[ 　]*/g;
const PITCH_RISE = "┏";
const PITCH_FALL = "┓";
const OVERLINE = "̅";

const inWord = (character) => /[\p{L}\p{N}'’\-−ー]/u.test(character);

function escapeHtml(text) {
  return text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

/** 全角 ASCII を ASCII に、全角空白を空白に戻す。意図して NFKC より狭い。 */
export function foldFullWidth(text) {
  return text.replace(/[！-～]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xfee0)).replace(/　/g, " ");
}

/** アクセントの角を上線に戻す。テキストの断片をまたいで状態を持つ。 */
class PitchAccent {
  constructor() {
    this.high = false;
    this.wordTail = []; // 直前の断片から続いている語の末尾（上線を遡ってかけるため）
  }

  apply(text, retro) {
    // retro(count) は、直前までに出力した語の末尾 count 文字に上線をかける。
    if (!text.includes(PITCH_RISE) && !text.includes(PITCH_FALL) && !this.high) {
      this._remember(text);
      return text;
    }
    const out = [];
    for (const character of text) {
      if (character === PITCH_RISE) {
        this.high = true;
      } else if (character === PITCH_FALL) {
        if (this.high) {
          this.high = false;
        } else {
          // 上がりのない下がり。語は高く始まっているので、既出の部分に線をかける。
          let index = out.length - 1;
          for (; index >= 0; index--) {
            if (!inWord(out[index][0])) break;
            out[index] += OVERLINE;
          }
          if (index < 0) retro(this.wordTail.length);
        }
      } else {
        if (!inWord(character)) this.high = false;
        out.push(this.high ? character + OVERLINE : character);
      }
    }
    const result = out.join("");
    this._remember(result);
    return result;
  }

  _remember(text) {
    for (const character of text) {
      if (inWord(character)) this.wordTail.push(character);
      else this.wordTail = [];
    }
  }
}

export class HtmlRenderer extends Renderer {
  constructor({ gaiji = null, readable = true } = {}) {
    super();
    this.gaijiMap = gaiji || new Map();
    this.readable = readable;
    this._pieces = []; // { text } または { html }
    this._open = []; // 閉じ待ちのタグ
    this.unknownGaiji = [];
    this.references = [];
    this._subscriptStarts = [];
    this._atLineStart = true;
  }

  _text(text) {
    if (!text) return;
    // 文字は 1 つずつ来る。続きのテキストは 1 つの断片にまとめておく。
    // 音声・画像の印のように文字列全体を見て消すものがあるため。
    const last = this._pieces[this._pieces.length - 1];
    if (last !== undefined && last.text !== undefined) last.text += text;
    else this._pieces.push({ text });
  }

  _html(html) {
    this._pieces.push({ html });
  }

  _begin(tag, attributes = "") {
    this._html(`<${tag}${attributes}>`);
    this._open.push({ tag, attributes });
  }

  _end(tag) {
    // 対応の崩れた閉じは無視する。本によっては閉じだけが来る。
    let index = -1;
    for (let i = this._open.length - 1; i >= 0; i--) if (this._open[i].tag === tag) { index = i; break; }
    if (index < 0) return;
    for (let i = this._open.length - 1; i >= index; i--) this._html(`</${this._open[i].tag}>`);
    this._open.length = index;
  }

  character(text) {
    this._atLineStart = false;
    this._text(text);
  }

  gaiji(code, narrow) {
    this._atLineStart = false;
    const key = formatGaijiCode(narrow, code);
    const replacement = this.gaijiMap.get(key);
    if (replacement === undefined) {
      this.unknownGaiji.push(key);
      this._html(`<span class="gaiji" data-code="${key}" title="${key}">&lt;${key}&gt;</span>`);
    } else if (/^［.+］$/.test(replacement)) {
      // 枠囲みのロゴを ［…］ に写したもの。文字ではなく印なので、小さなバッジに描く。
      this._html(`<span class="badge">${escapeHtml(replacement.slice(1, -1))}</span>`);
    } else {
      this._text(replacement);
    }
  }

  /** 改行。行はブロックになるので、開いている装飾はいったん閉じて次の行で開き直す。 */
  newline() {
    for (let i = this._open.length - 1; i >= 0; i--) this._html(`</${this._open[i].tag}>`);
    this._pieces.push({ br: true });
    for (const { tag, attributes } of this._open) this._html(`<${tag}${attributes}>`);
    this._atLineStart = true;
  }

  /** 字下げ。行頭にあるものだけ段落の深さとして扱い、行の途中のものは無視する。 */
  indent(level) {
    if (this._atLineStart) this._pieces.push({ indent: level });
  }

  beginSubscript() {
    // ここから先の文字を前の断片につなげないよう、空の区切りを置く。
    this._pieces.push({ html: "" });
    this._subscriptStarts.push(this._pieces.length);
  }

  /** 下付き。数字なら本物の下付きに、読みのような文字列なら括弧でくくる。 */
  endSubscript() {
    if (!this._subscriptStarts.length) return;
    const start = this._subscriptStarts.pop();
    const inner = this._pieces.slice(start);
    const text = inner.map((piece) => piece.text ?? "").join("");
    if (inner.every((piece) => piece.text !== undefined) && /^\p{Nd}+$/u.test(text)) {
      this._pieces.splice(start, inner.length, { html: "<sub>" }, { text }, { html: "</sub>" });
    } else {
      this._pieces.splice(start, 0, { html: '<span class="reading">' }, { text: "(" });
      this._pieces.push({ text: ")" }, { html: "</span>" });
    }
  }

  beginSuperscript() {
    this._begin("sup");
  }

  endSuperscript() {
    this._end("sup");
  }

  beginEmphasis() {
    this._begin("b");
  }

  endEmphasis() {
    this._end("b");
  }

  beginDecoration(kind) {
    this._begin("span", ` class="deco deco-${kind}"`);
  }

  endDecoration() {
    this._end("span");
  }

  beginKeyword(code) {
    this._begin("span", ` class="kw" data-kw="${code}"`);
  }

  endKeyword() {
    this._end("span");
  }

  beginReference() {
    this._begin("a", ' class="ref" href="#"');
  }

  endReference(position) {
    // 開始タグを書いたあとに行き先が分かるので、遡って埋める。
    for (let i = this._pieces.length - 1; i >= 0; i--) {
      const piece = this._pieces[i];
      if (piece.html === '<a class="ref" href="#">') {
        piece.html = `<a class="ref" href="#" data-ref="${position.page}:${position.offset}">`;
        break;
      }
    }
    this.references.push(position);
    this._end("a");
  }

  beginCandidate() {
    this._begin("a", ' class="cand" href="#"');
  }

  endCandidate(position) {
    for (let i = this._pieces.length - 1; i >= 0; i--) {
      const piece = this._pieces[i];
      if (piece.html === '<a class="cand" href="#">') {
        piece.html = position === null ? '<span class="cand-label">' : `<a class="cand" href="#" data-ref="${position.page}:${position.offset}">`;
        if (position === null) {
          for (let j = this._open.length - 1; j >= 0; j--) if (this._open[j].tag === "a") { this._open[j] = { tag: "span", attributes: "" }; break; }
        }
        break;
      }
    }
    this._end(position === null ? "span" : "a");
  }

  /** 組み上がった HTML。行ごとのブロックに組む。 */
  get html() {
    while (this._open.length) this._html(`</${this._open.pop().tag}>`);

    // 1. テキストの整形。断片をまたいでアクセントの状態を持つ。
    const pitch = new PitchAccent();
    const pieces = [];
    const textIndexes = [];
    for (const piece of this._pieces) {
      if (piece.text === undefined) {
        pieces.push(piece);
        continue;
      }
      let text = piece.text;
      if (this.readable) {
        text = foldFullWidth(text).replace(MEDIA_MARKER, "");
        text = pitch.apply(text, (count) => {
          let remaining = count;
          for (let i = textIndexes.length - 1; i >= 0 && remaining > 0; i--) {
            const at = textIndexes[i];
            const chars = [...pieces[at].text];
            for (let j = chars.length - 1; j >= 0 && remaining > 0; j--) {
              if (!inWord(chars[j][0])) return;
              chars[j] += OVERLINE;
              remaining -= 1;
            }
            pieces[at] = { text: chars.join("") };
          }
        });
      }
      textIndexes.push(pieces.length);
      pieces.push({ text });
    }

    // 2. 行に分ける。
    const lines = [];
    let line = { level: 1, parts: [] };
    for (const piece of pieces) {
      if (piece.br) {
        lines.push(line);
        line = { level: 1, parts: [] };
      } else if (piece.indent !== undefined) {
        line.level = piece.indent;
      } else {
        line.parts.push(piece);
      }
    }
    lines.push(line);

    // 3. 行を組む。
    const out = [];
    let blank = false;
    lines.forEach((current, index) => {
      const text = current.parts.map((p) => p.text ?? "").join("");
      if (!text.trim() && !current.parts.some((p) => p.html && /<span class="gaiji"/.test(p.html))) {
        if (index > 0 && index < lines.length - 1 && !blank) out.push('<div class="line empty"></div>');
        blank = true;
        return;
      }
      blank = false;
      const classes = ["line"];
      const level = Math.max(0, current.level - 1);
      let marker = null;
      if (index === 0) {
        classes.push("headline");
      } else {
        const leading = current.parts.find((p) => p.text !== undefined && p.text.trim());
        const first = current.parts.indexOf(leading);
        const m = leading && current.parts.slice(0, first).every((p) => p.html === undefined || /^<\/?(b|span|sup|sub)/.test(p.html)) ? MARKER.exec(leading.text) : null;
        if (m) {
          marker = m[0];
          classes.push("hang");
          current.parts[first] = { text: leading.text.slice(marker.length) };
        }
      }
      const body = current.parts
        .map((p) => (p.text !== undefined ? decorate(escapeHtml(p.text)) : p.html))
        .join("")
        .replace(/<(b|sup|sub|span)(?:\s[^>]*)?><\/\1>/g, "");
      const head = marker === null ? "" : `<span class="marker">${escapeHtml(marker)}</span>`;
      out.push(`<div class="${classes.join(" ")}" style="--lv:${level}">${head}${body}</div>`);
    });
    return out.join("\n");
  }
}

/** 行頭の印。語義番号、細分の記号、用例の印。記号はそのまま、英数字は区切りが要る。 */
const MARKER = new RegExp(
  "^\\s*(?:" +
    "[¶◆◇・▸▶►●○■□★☆※▽▼△▲①-⑳㋐-㋾]|【[0-9]{1,2}】" + // 記号と、囲みの語義番号
    "|(?:\\(?[0-9]{1,2}\\)?[a-z]?|\\([ア-ンa-zA-Zａ-ｚ一二三四五六七八九十]\\)|\\[[0-9一二三四五六七八九十]{1,2}\\]|[a-z]\\.)" +
    "(?=\\s|[^\\sa-zA-Z0-9.]|$)" +
    ")\\s*",
);

/** 本文中のラベルに印をつける。【名】のような品詞・分野、《口語》のような位相。 */
function decorate(html) {
  return html
    .replace(/【([^【】]{1,12})】/g, '<span class="label">$1</span>')
    .replace(/《([^《》]{1,12})》/g, '<span class="reg">《$1》</span>');
}
