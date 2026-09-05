// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 satorux

/**
 * bkbook の Web UI。辞書データをサーバーに置き、ブラウザが Range リクエストで読む。
 */

import { HtmlRenderer } from "./bkbook/html.js";
import {
  HttpFs,
  PlainTextRenderer,
  Position,
  fontSet,
  openCollection,
  parsePattern,
  readHeading,
  readText,
  search,
  searchKeyword,
} from "./bkbook/index.js";
import { foldFullWidth, readable } from "./bkbook/collection.js";

const params = new URLSearchParams(location.search);
const BASE = new URL(params.get("eb") || document.documentElement.dataset.eb || "eb/", location.href).href;
const HITS_PER_SOURCE = 40;
const EAGER_HEADINGS = 40;
const CATEGORY_CLASS = { 英: "en", 国: "ja", 百: "ency" };
const CATEGORY_NAME = { 英: "英語", 国: "国語", 百: "百科" };

const $ = (id) => document.getElementById(id);
const ui = {
  q: $("q"), mode: $("mode"), status: $("status"), listHead: $("list-head"), listBody: $("list-body"), hint: $("hint"),
  entry: $("entry"), entryTitle: $("entry-title"), entryBody: $("entry-body"), back: $("back"),
  settings: $("settings"), settingsButton: $("settings-button"), books: $("books"), settingsReset: $("settings-reset"),
};

const store = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem(`bkbook.${key}`);
      return raw === null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(`bkbook.${key}`, JSON.stringify(value));
    } catch {
      // 保存できなくても動く
    }
  },
};

const state = {
  fs: null,
  all: [], // 開いた辞書すべて（既定の順）
  sources: [], // 有効なもの、利用者の順
  keyword: false,
  query: "",
  generation: 0,
  entries: [],
  selected: -1,
  stack: [], // 本文ペインの履歴 [{source, hit|position, heading}]
  fonts: new Map(),
};

const isMobile = () => matchMedia("(max-width: 760px)").matches;

function setStatus(text, busy = false) {
  ui.status.textContent = text;
  ui.status.classList.toggle("busy", busy);
}

// -- 起動 ------------------------------------------------------------------------

async function start() {
  setStatus("辞書の一覧を取得しています…", true);
  try {
    state.fs = await HttpFs.fromManifestUrl(BASE, new URL("manifest.txt", BASE).href);
  } catch (error) {
    setStatus("");
    ui.hint.innerHTML = `<p>辞書の一覧 <code>${new URL("manifest.txt", BASE).href}</code> を読めませんでした。</p><p>${escape(String(error.message))}</p>`;
    return;
  }
  const { sources } = await openCollection(state.fs, [""], {
    progress: (index, total, path) => setStatus(`辞書を開いています… ${index + 1}/${total} ${path.split("/").pop()}`, true),
  });
  state.all = sources;
  const cached = store.get("stopcodes", {});
  for (const source of sources) {
    if (source.subbook.stopCode === undefined && source.title in cached) source.subbook.stopCode = cached[source.title];
  }
  applySettings();
  setStatus("");
  ui.q.disabled = false;
  const initial = decodeURIComponent(location.hash.replace(/^#q=/, "")) || "";
  if (initial) {
    ui.q.value = initial;
    runSearch();
  }
  ui.q.focus();
}

function applySettings() {
  const settings = store.get("books", { order: [], disabled: [] });
  const rank = new Map(settings.order.map((title, i) => [title, i]));
  const disabled = new Set(settings.disabled);
  const ordered = [...state.all].sort((a, b) => {
    const ra = rank.has(a.title) ? rank.get(a.title) : rank.size + state.all.indexOf(a);
    const rb = rank.has(b.title) ? rank.get(b.title) : rank.size + state.all.indexOf(b);
    return ra - rb;
  });
  state.sources = ordered.filter((s) => !disabled.has(s.title));
  renderSettings(ordered, disabled);
}

// -- 検索 ------------------------------------------------------------------------

let debounce = null;
ui.q.addEventListener("input", () => {
  clearTimeout(debounce);
  debounce = setTimeout(runSearch, 60);
});
$("search").addEventListener("submit", (event) => {
  event.preventDefault();
  clearTimeout(debounce);
  runSearch();
  if (isMobile() && state.entries.length) ui.q.blur();
});
$("logo").addEventListener("click", (event) => {
  event.preventDefault();
  ui.q.value = "";
  closeEntry();
  runSearch();
  ui.q.focus();
});
ui.mode.addEventListener("click", () => {
  state.keyword = !state.keyword;
  ui.mode.textContent = state.keyword ? "本文" : "見出し";
  ui.mode.setAttribute("aria-pressed", String(state.keyword));
  ui.q.placeholder = state.keyword ? "本文中の語で探す（空白区切りで AND）" : "辞書を引く";
  runSearch();
  ui.q.focus();
});

async function runSearch() {
  const text = ui.q.value;
  state.query = text;
  const generation = ++state.generation;
  history.replaceState(null, "", text.trim() ? `#q=${encodeURIComponent(text.trim())}` : location.pathname + location.search);

  let word;
  let backward = false;
  let words = [];
  if (state.keyword) {
    words = text.split(/\s+/).filter(Boolean);
    word = words.join(" ");
  } else {
    try {
      ({ word, backward } = parsePattern(text));
    } catch (error) {
      setStatus(error.message);
      return;
    }
  }
  if (!word) {
    state.entries = [];
    renderList();
    setStatus("");
    return;
  }

  setStatus("", true);
  const perSource = await Promise.all(
    state.sources.map(async (source) => {
      try {
        const hits = state.keyword
          ? await searchKeyword(source.subbook, words, { limit: HITS_PER_SOURCE, nextHeading: nextHeadingOf(source.subbook) })
          : await search(source.subbook, word, { limit: HITS_PER_SOURCE, backward });
        return hits.map((hit) => ({ source, hit, heading: null }));
      } catch {
        return []; // この辞書はその問い合わせを表現も索引もできない
      }
    }),
  );
  if (generation !== state.generation) return;

  // 種類ごとにまとめ、その中では辞書を交互に並べる。1 冊が一覧を埋め尽くさないように。
  const entries = [];
  for (const category of ["英", "国", "百"]) {
    const lists = perSource.filter((list, i) => list.length && state.sources[i].category === category);
    const cursors = lists.map(() => 0);
    for (let took = true; took; ) {
      took = false;
      lists.forEach((list, i) => {
        if (cursors[i] >= list.length) return;
        const step = list[0].source.hitsPerRound;
        entries.push(...list.slice(cursors[i], cursors[i] + step));
        cursors[i] += step;
        took = true;
      });
    }
  }
  state.entries = entries;
  renderList();
  setStatus(entries.length ? "" : "見つかりませんでした");

  if (entries.length && !isMobile()) select(0, { open: true });
  else if (!entries.length) closeEntry();
  await loadHeadings(entries.slice(0, EAGER_HEADINGS), generation);
}

const nextHeadingOf = (subbook) => async (position) => (await readHeading(subbook, position)).nextPosition;

async function headingOf(entry) {
  if (entry.heading === null) {
    try {
      const result = await readHeading(entry.source.subbook, entry.hit.heading, { renderer: new PlainTextRenderer({ gaiji: entry.source.gaiji }) });
      entry.heading = readable(result.text).trim() || "(no heading)";
    } catch (error) {
      entry.heading = `(error: ${error.message})`;
    }
  }
  return entry.heading;
}

async function loadHeadings(entries, generation) {
  await Promise.all(
    entries.map(async (entry) => {
      const text = await headingOf(entry);
      if (generation !== state.generation) return;
      if (entry.node) entry.node.querySelector(".head").textContent = text;
    }),
  );
}

// -- 一覧 ------------------------------------------------------------------------

const observer = new IntersectionObserver(
  (records) => {
    const generation = state.generation;
    const pending = [];
    for (const record of records) {
      if (!record.isIntersecting) continue;
      observer.unobserve(record.target);
      const entry = state.entries[Number(record.target.dataset.index)];
      if (entry && entry.heading === null) pending.push(entry);
    }
    if (pending.length) loadHeadings(pending, generation);
  },
  { root: $("list"), rootMargin: "300px" },
);

function renderList() {
  observer.disconnect();
  ui.listBody.textContent = "";
  ui.listHead.textContent = "";
  ui.hint.hidden = state.entries.length > 0 || state.query.trim() !== "";
  state.selected = -1;
  if (!state.entries.length) {
    if (state.query.trim()) ui.listBody.innerHTML = '<div class="empty">見つかりませんでした</div>';
    return;
  }
  const fragment = document.createDocumentFragment();
  const counts = {};
  let lastCategory = null;
  state.entries.forEach((entry, index) => {
    const category = entry.source.category;
    counts[category] = (counts[category] || 0) + 1;
    if (category !== lastCategory) {
      const title = document.createElement("div");
      title.className = "section-title";
      title.id = `section-${CATEGORY_CLASS[category] || "x"}`;
      title.textContent = CATEGORY_NAME[category] || category;
      fragment.appendChild(title);
      lastCategory = category;
    }
    const node = document.createElement("div");
    node.className = `item ${CATEGORY_CLASS[category] || ""}`;
    node.dataset.index = String(index);
    node.setAttribute("role", "button");
    node.innerHTML = `<span class="dict" data-cat="${category}">${escape(shortTitle(entry.source.title))}</span><span class="head"></span>`;
    if (entry.heading !== null) node.querySelector(".head").textContent = entry.heading;
    node.addEventListener("click", () => select(index, { open: true, push: true }));
    entry.node = node;
    fragment.appendChild(node);
    if (index >= EAGER_HEADINGS) observer.observe(node);
  });
  ui.listBody.appendChild(fragment);

  for (const category of ["英", "国", "百"]) {
    if (!counts[category]) continue;
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `chip ${CATEGORY_CLASS[category]}`;
    chip.innerHTML = `<b>${category}</b>${counts[category]}`;
    chip.addEventListener("click", () => $(`section-${CATEGORY_CLASS[category]}`).scrollIntoView({ block: "start" }));
    ui.listHead.appendChild(chip);
  }
  $("list").scrollTop = 0;
}

function shortTitle(title) {
  return foldFullWidth(title).replace(/[　 ]+/g, " ");
}

function select(index, { open = false, push = false } = {}) {
  if (index < 0 || index >= state.entries.length) return;
  const previous = state.entries[state.selected];
  if (previous && previous.node) previous.node.classList.remove("selected");
  state.selected = index;
  const entry = state.entries[index];
  entry.node.classList.add("selected");
  entry.node.scrollIntoView({ block: "nearest" });
  if (open) {
    state.stack = [];
    showEntry(entry.source, entry.hit.text, { push });
  }
}

// -- 本文 ------------------------------------------------------------------------

function openEntryPane(push) {
  if (!ui.entry.classList.contains("open")) {
    ui.entry.classList.add("open");
    if (isMobile() && push) history.pushState({ entry: true }, "");
  }
}

function closeEntry() {
  ui.entry.classList.remove("open");
  state.stack = [];
}

addEventListener("popstate", () => {
  if (ui.entry.classList.contains("open")) closeEntry();
});
addEventListener("hashchange", () => {
  const wanted = decodeURIComponent(location.hash.replace(/^#q=/, ""));
  if (wanted !== ui.q.value.trim()) {
    ui.q.value = wanted;
    runSearch();
  }
});
ui.back.addEventListener("click", () => {
  if (state.stack.length > 1) {
    state.stack.pop();
    const top = state.stack.pop();
    showEntry(top.source, top.position, { push: false });
  } else if (history.state && history.state.entry) {
    history.back();
  } else {
    closeEntry();
  }
});

let entryGeneration = 0;

async function showEntry(source, position, { push = false } = {}) {
  const generation = ++entryGeneration;
  state.stack.push({ source, position });
  openEntryPane(push);
  ui.entryBody.classList.add("loading");
  ui.entryTitle.innerHTML = `<b>${escape(source.title)}</b>`;
  ui.entryBody.scrollTop = 0;

  const subbook = source.subbook;
  const known = subbook.stopCode !== undefined;
  const html = await renderEntry(source, position, known ? undefined : null);
  if (generation !== entryGeneration) return;
  ui.entryBody.innerHTML = html + (state.stack.length > 1 ? "" : "");
  ui.entryBody.classList.remove("loading");
  wireEntry(source);
  drawGaiji(source);

  if (!known) {
    // 項目の終わりが分からない本。libeb 流の推定でまず出し、裏で本を読んで割り出す。
    subbook.inferStopCode().then(async () => {
      const cached = store.get("stopcodes", {});
      cached[source.title] = subbook.stopCode;
      store.set("stopcodes", cached);
      if (generation !== entryGeneration) return;
      const again = await renderEntry(source, position, undefined);
      if (generation !== entryGeneration) return;
      ui.entryBody.innerHTML = again;
      wireEntry(source);
      drawGaiji(source);
    });
  }
}

async function renderEntry(source, position, stopCode) {
  const renderer = new HtmlRenderer({ gaiji: source.gaiji });
  try {
    await readText(source.subbook, position, { renderer, stopCode });
  } catch (error) {
    return `<p class="note">読めませんでした: ${escape(error.message)}</p>`;
  }
  return renderer.html;
}

function wireEntry(source) {
  for (const link of ui.entryBody.querySelectorAll("a[data-ref]")) {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const [page, offset] = link.dataset.ref.split(":").map(Number);
      showEntry(source, new Position(page, offset), { push: false });
    });
  }
}

/** 対応表にない外字を、本のフォントのビットマップで描く。 */
async function drawGaiji(source) {
  const spans = ui.entryBody.querySelectorAll(".gaiji[data-code]");
  if (!spans.length) return;
  const generation = entryGeneration;
  for (const span of spans) {
    const key = span.dataset.code;
    const narrow = key[0] === "h";
    const code = parseInt(key.slice(1), 16);
    try {
      const fonts = await fontsOf(source, narrow);
      if (!fonts || !fonts.has(code)) continue;
      const bitmap = await fonts.bitmap(code);
      if (generation !== entryGeneration) return;
      span.innerHTML = bitmap.toSvg();
      span.classList.add("drawn");
    } catch {
      // 描けなければプレースホルダのまま
    }
  }
}

async function fontsOf(source, narrow) {
  const key = `${source.title}/${narrow ? "h" : "z"}`;
  if (!state.fonts.has(key)) {
    state.fonts.set(key, fontSet(source.subbook, narrow, 16).catch(() => null));
  }
  return state.fonts.get(key);
}

// -- ペインの幅 --------------------------------------------------------------------

const MIN_LIST_WIDTH = 220;
const DEFAULT_LIST_WIDTH = 340;

function setListWidth(width, save = true) {
  const max = Math.max(MIN_LIST_WIDTH, Math.floor($("main").clientWidth * 0.7));
  const clamped = Math.min(max, Math.max(MIN_LIST_WIDTH, Math.round(width)));
  document.documentElement.style.setProperty("--list-width", `${clamped}px`);
  if (save) store.set("listWidth", clamped);
  return clamped;
}

{
  const saved = store.get("listWidth", null);
  if (typeof saved === "number") setListWidth(saved, false);
  const splitter = $("splitter");
  let dragging = null;
  splitter.addEventListener("pointerdown", (event) => {
    dragging = { x: event.clientX, width: $("list").getBoundingClientRect().width };
    splitter.setPointerCapture(event.pointerId);
    splitter.classList.add("dragging");
    document.body.classList.add("dragging");
    event.preventDefault();
  });
  splitter.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    setListWidth(dragging.width + event.clientX - dragging.x, false);
  });
  const stop = (event) => {
    if (!dragging) return;
    setListWidth(dragging.width + event.clientX - dragging.x);
    dragging = null;
    splitter.classList.remove("dragging");
    document.body.classList.remove("dragging");
  };
  splitter.addEventListener("pointerup", stop);
  splitter.addEventListener("pointercancel", stop);
  splitter.addEventListener("dblclick", () => setListWidth(DEFAULT_LIST_WIDTH));
  splitter.addEventListener("keydown", (event) => {
    const width = $("list").getBoundingClientRect().width;
    if (event.key === "ArrowLeft") setListWidth(width - 20);
    else if (event.key === "ArrowRight") setListWidth(width + 20);
    else return;
    event.preventDefault();
  });
  splitter.tabIndex = 0;
}

// -- キーボード ------------------------------------------------------------------

addEventListener("keydown", (event) => {
  const inInput = event.target === ui.q;
  if (event.key === "Escape") {
    if (ui.settings.open) return;
    if (isMobile() && ui.entry.classList.contains("open")) {
      ui.back.click();
    } else if (inInput && ui.q.value) {
      ui.q.value = "";
      runSearch();
    } else {
      ui.q.focus();
    }
    event.preventDefault();
  } else if (event.key === "ArrowDown" || (event.ctrlKey && event.key === "n")) {
    select(state.selected + 1, { open: !isMobile() });
    event.preventDefault();
  } else if (event.key === "ArrowUp" || (event.ctrlKey && event.key === "p")) {
    select(state.selected - 1, { open: !isMobile() });
    event.preventDefault();
  } else if (event.key === "Enter" && inInput && isMobile() && state.selected >= 0) {
    select(state.selected, { open: true, push: true });
    event.preventDefault();
  } else if (event.key === "Tab" && inInput && !event.shiftKey && state.entries.length) {
    // 次の種類の先頭へ
    const current = state.entries[Math.max(state.selected, 0)].source.category;
    const next = state.entries.findIndex((e, i) => i > state.selected && e.source.category !== current);
    select(next >= 0 ? next : 0, { open: !isMobile() });
    event.preventDefault();
  } else if (event.key === "/" && !inInput && !ui.settings.open) {
    ui.q.focus();
    ui.q.select();
    event.preventDefault();
  } else if ((event.ctrlKey || event.metaKey) && event.key === "k" && inInput) {
    ui.mode.click();
    event.preventDefault();
  }
});

// -- 設定 ------------------------------------------------------------------------

ui.settingsButton.addEventListener("click", () => ui.settings.showModal());
ui.settings.addEventListener("close", () => ui.q.focus());
ui.settingsReset.addEventListener("click", () => {
  store.set("books", { order: [], disabled: [] });
  applySettings();
  runSearch();
});

function renderSettings(ordered, disabled) {
  ui.books.textContent = "";
  ordered.forEach((source, index) => {
    const li = document.createElement("li");
    li.className = `${CATEGORY_CLASS[source.category] || ""} ${disabled.has(source.title) ? "off" : ""}`;
    li.innerHTML =
      `<input type="checkbox" ${disabled.has(source.title) ? "" : "checked"} aria-label="有効">` +
      `<span class="title"><span class="cat">${source.category}</span>${escape(source.title)}</span>` +
      `<button type="button" title="上へ" ${index === 0 ? "disabled" : ""}>↑</button>` +
      `<button type="button" title="下へ" ${index === ordered.length - 1 ? "disabled" : ""}>↓</button>`;
    const [up, down] = li.querySelectorAll("button");
    li.querySelector("input").addEventListener("change", (event) => {
      const settings = { order: ordered.map((s) => s.title), disabled: [...disabled] };
      settings.disabled = event.target.checked ? settings.disabled.filter((t) => t !== source.title) : [...settings.disabled, source.title];
      store.set("books", settings);
      applySettings();
      runSearch();
    });
    const move = (delta) => {
      const order = ordered.map((s) => s.title);
      const [title] = order.splice(index, 1);
      order.splice(index + delta, 0, title);
      store.set("books", { order, disabled: [...disabled] });
      applySettings();
      runSearch();
    };
    up.addEventListener("click", () => move(-1));
    down.addEventListener("click", () => move(1));
    ui.books.appendChild(li);
  });
}

function escape(text) {
  return String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

ui.q.disabled = true;
start();
