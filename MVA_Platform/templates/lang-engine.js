/**
 * IntakeOps runtime translator (English <-> Spanish).
 *
 * No static dictionaries: every string is translated on demand through the
 * Google Translate endpoint (same approach as playbook.html) and cached.
 * A MutationObserver keeps dynamically rendered content translated.
 */
(function (global) {
  const STORAGE_LANG = "intakeops_ui_lang_v2";
  const STORAGE_CACHE = "intakeops_translate_cache_v2";
  const MAX_CACHE_ENTRIES = 5000;
  const CHUNK_LEN = 1200;
  const CONCURRENCY = 6;
  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEXTAREA", "CODE"]);
  const ATTRS = ["placeholder", "title", "aria-label", "alt"];
  const EXPORT_SKIP_KEYS = new Set([
    "First Name", "Last Name", "Email", "Phone", "Lead Name", "Lead Email",
    "Lead ID", "NEOS Case #", "Reviewed By", "Lead Accepted By",
    "Assigned Specialist", "Language",
  ]);

  let lang = "en";
  let cache = new Map();
  const inflight = new Map();
  let active = 0;
  const waiting = [];
  let saveTimer = null;
  let observer = null;

  /* ───────── cache ───────── */
  function loadCache() {
    try {
      const raw = localStorage.getItem(STORAGE_CACHE);
      if (raw) cache = new Map(JSON.parse(raw));
    } catch (_) {
      cache = new Map();
    }
  }
  function scheduleSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      try {
        while (cache.size > MAX_CACHE_ENTRIES) {
          cache.delete(cache.keys().next().value);
        }
        localStorage.setItem(STORAGE_CACHE, JSON.stringify([...cache]));
      } catch (_) {
        /* quota — ignore */
      }
    }, 800);
  }

  /* ───────── translation API ───────── */
  function shouldTranslate(s) {
    const t = String(s ?? "").trim();
    if (t.length < 2) return false;
    if (!/[A-Za-z]{2,}/.test(t)) return false;
    if (/^[\w.+-]+@[\w-]+(\.[\w-]+)+$/.test(t)) return false;
    if (/^https?:\/\/\S+$/.test(t)) return false;
    return true;
  }

  function runQueued(fn) {
    return new Promise((resolve) => {
      const task = () => {
        active++;
        fn()
          .then(resolve, () => resolve(null))
          .finally(() => {
            active--;
            if (waiting.length) waiting.shift()();
          });
      };
      if (active < CONCURRENCY) task();
      else waiting.push(task);
    });
  }

  async function fetchChunk(text) {
    const url =
      "https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=es&dt=t&q=" +
      encodeURIComponent(text);
    const res = await fetch(url);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    if (!data || !Array.isArray(data[0])) throw new Error("bad response");
    return data[0].map((x) => (x && x[0]) || "").join("");
  }

  function splitChunks(text) {
    if (text.length <= CHUNK_LEN) return [text];
    const pieces = text.match(/[^.!?\n]+[.!?]*\s*/g) || [text];
    const chunks = [];
    let cur = "";
    pieces.forEach((p) => {
      if (cur && (cur + p).length > CHUNK_LEN) {
        chunks.push(cur);
        cur = "";
      }
      cur += p;
      while (cur.length > CHUNK_LEN) {
        chunks.push(cur.slice(0, CHUNK_LEN));
        cur = cur.slice(CHUNK_LEN);
      }
    });
    if (cur) chunks.push(cur);
    return chunks;
  }

  /** Async translate EN -> ES (always resolves; falls back to original). */
  function translate(text) {
    const src = String(text ?? "");
    if (!shouldTranslate(src)) return Promise.resolve(src);
    const key = src.trim();
    if (cache.has(key)) return Promise.resolve(cache.get(key));
    if (inflight.has(key)) return inflight.get(key);
    const p = (async () => {
      const parts = [];
      for (const chunk of splitChunks(key)) {
        const out = await runQueued(() => fetchChunk(chunk));
        if (out == null) return key; // failed: don't cache
        parts.push(out.trim());
      }
      const result = parts.join(" ");
      cache.set(key, result);
      scheduleSave();
      return result;
    })().finally(() => {
      inflight.delete(key);
      maybeHideFlash();
    });
    inflight.set(key, p);
    return p;
  }

  /** Sync lookup used by tr(): cached Spanish, otherwise English (and queue it). */
  function translateSync(text) {
    const src = String(text ?? "");
    if (lang !== "es" || !shouldTranslate(src)) return src;
    const key = src.trim();
    if (cache.has(key)) return cache.get(key);
    void translate(key);
    return src;
  }

  /* ───────── DOM translation ───────── */
  const textRecs = new WeakMap(); // Text -> { orig, applied }
  const attrRecs = new WeakMap(); // Element -> { attr: { orig, applied } }

  function isSkippedEl(el) {
    return !!el && (SKIP_TAGS.has(el.tagName) || el.hasAttribute("data-no-translate"));
  }
  function inSkipped(node) {
    const el = node.nodeType === 1 ? node : node.parentElement;
    if (!el) return true;
    if (SKIP_TAGS.has(el.tagName)) return true;
    return !!el.closest("[data-no-translate], script, style, textarea, code");
  }

  function applyValue(orig, setter, isStillOrig, rec) {
    const lead = orig.match(/^\s*/)[0];
    const trail = orig.match(/\s*$/)[0];
    const key = orig.trim();
    const put = (t) => {
      if (lang !== "es" || !isStillOrig()) return;
      const v = lead + t + trail;
      rec.applied = v;
      setter(v);
    };
    if (cache.has(key)) put(cache.get(key));
    else translate(key).then(put);
  }

  function handleText(node) {
    const cur = node.nodeValue;
    let rec = textRecs.get(node);
    if (rec && rec.applied !== null && cur === rec.applied) {
      if (lang === "en") {
        node.nodeValue = rec.orig;
        textRecs.delete(node);
      }
      return;
    }
    if (lang === "en") {
      textRecs.delete(node);
      return;
    }
    if (!shouldTranslate(cur) || inSkipped(node)) return;
    rec = { orig: cur, applied: null };
    textRecs.set(node, rec);
    applyValue(cur, (v) => { node.nodeValue = v; }, () => node.nodeValue === rec.orig, rec);
  }

  function handleAttrs(el) {
    let recs = attrRecs.get(el);
    ATTRS.forEach((a) => {
      if (!el.hasAttribute(a)) return;
      const cur = el.getAttribute(a);
      const rec = recs && recs[a];
      if (rec && rec.applied !== null && cur === rec.applied) {
        if (lang === "en") {
          el.setAttribute(a, rec.orig);
          delete recs[a];
        }
        return;
      }
      if (lang === "en") {
        if (recs) delete recs[a];
        return;
      }
      if (!shouldTranslate(cur) || inSkipped(el)) return;
      if (!recs) {
        recs = {};
        attrRecs.set(el, recs);
      }
      const nrec = { orig: cur, applied: null };
      recs[a] = nrec;
      applyValue(cur, (v) => el.setAttribute(a, v), () => el.getAttribute(a) === nrec.orig, nrec);
    });
  }

  function processTree(root) {
    if (!root) return;
    if (root.nodeType === 3) {
      handleText(root);
      return;
    }
    if (root.nodeType !== 1 || inSkipped(root)) return;
    handleAttrs(root);
    const walker = document.createTreeWalker(
      root,
      NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
      {
        acceptNode(n) {
          if (n.nodeType === 1 && isSkippedEl(n)) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      },
    );
    let n = walker.nextNode();
    while (n) {
      if (n.nodeType === 3) handleText(n);
      else handleAttrs(n);
      n = walker.nextNode();
    }
  }

  let pending = new Set();
  let flushTimer = null;
  function queue(node) {
    pending.add(node);
    if (flushTimer) return;
    flushTimer = setTimeout(() => {
      flushTimer = null;
      const nodes = pending;
      pending = new Set();
      nodes.forEach((nd) => {
        if (nd.isConnected) processTree(nd);
      });
    }, 30);
  }

  function startObserver() {
    if (observer) return;
    observer = new MutationObserver((muts) => {
      muts.forEach((m) => {
        if (m.type === "childList") m.addedNodes.forEach((n) => queue(n));
        else if (m.type === "characterData") queue(m.target);
        else if (m.type === "attributes") queue(m.target);
      });
    });
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ATTRS,
    });
  }

  /* ───────── title / toggle ───────── */
  let titleOrig = null;
  function applyTitle() {
    if (titleOrig === null) titleOrig = document.title;
    if (lang === "en") document.title = titleOrig;
    else translate(titleOrig).then((t) => { if (lang === "es") document.title = t; });
  }

  function syncToggle() {
    document.querySelectorAll(".lang-toggle-btn[data-lang]").forEach((b) => {
      b.classList.toggle("active", b.dataset.lang === lang);
    });
  }

  /* ───────── flash while translation API calls are in flight ───────── */
  const FLASH_MSG = "Translation may take a few seconds.";
  let flashEl = null;
  let flashWatching = false;

  function ensureFlash() {
    if (flashEl) return flashEl;
    const style = document.createElement("style");
    style.textContent =
      ".lang-flash{position:fixed;top:16px;left:50%;transform:translateX(-50%) translateY(-8px);" +
      "z-index:200001;display:flex;align-items:center;gap:8px;padding:8px 14px;border-radius:999px;" +
      "background:#1a1a1a;border:1px solid rgba(241,196,15,.45);color:#f1c40f;" +
      "font:600 12px Montserrat,system-ui,sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.45);" +
      "opacity:0;pointer-events:none;transition:opacity .2s ease,transform .2s ease}" +
      ".lang-flash.show{opacity:1;transform:translateX(-50%) translateY(0)}" +
      ".lang-flash-dot{width:7px;height:7px;border-radius:50%;background:#f1c40f;" +
      "animation:lang-flash-pulse 1s ease-in-out infinite}" +
      "@keyframes lang-flash-pulse{50%{opacity:.35}}";
    document.head.appendChild(style);
    flashEl = document.createElement("div");
    flashEl.className = "lang-flash";
    flashEl.setAttribute("data-no-translate", "");
    flashEl.setAttribute("role", "status");
    flashEl.innerHTML = '<span class="lang-flash-dot"></span><span></span>';
    flashEl.lastChild.textContent = FLASH_MSG;
    document.body.appendChild(flashEl);
    return flashEl;
  }

  function showLangFlash() {
    const el = ensureFlash();
    el.lastChild.textContent = FLASH_MSG;
    el.classList.add("show");
    flashWatching = true;
  }

  function hideLangFlash() {
    flashWatching = false;
    if (flashEl) flashEl.classList.remove("show");
  }

  function maybeHideFlash() {
    if (flashWatching && inflight.size === 0) hideLangFlash();
  }

  function setLanguage(next) {
    next = next === "es" ? "es" : "en";
    const inflightBefore = inflight.size;
    lang = next;
    try { localStorage.setItem(STORAGE_LANG, lang); } catch (_) {}
    document.documentElement.lang = lang;
    syncToggle();
    applyTitle();
    processTree(document.body);
    if (lang === "es") startObserver();
    if (lang === "es" && inflight.size > inflightBefore) showLangFlash();
    else if (lang === "en") hideLangFlash();
    window.dispatchEvent(new CustomEvent("intakeops:langchange", { detail: { lang } }));
  }

  function init() {
    loadCache();
    let saved = "en";
    try { saved = localStorage.getItem(STORAGE_LANG) || "en"; } catch (_) {}
    syncToggle();
    if (saved === "es") setLanguage("es");
  }

  /* ───────── Excel export ───────── */
  async function translateRows(rows) {
    if (lang !== "es" || !rows || !rows.length) return rows;
    const strings = new Set();
    rows.forEach((row) => {
      Object.keys(row).forEach((k) => {
        strings.add(k);
        if (!EXPORT_SKIP_KEYS.has(k) && typeof row[k] === "string") strings.add(row[k]);
      });
    });
    await Promise.all([...strings].map((s) => translate(s)));
    const t = (s) => (shouldTranslate(s) ? cache.get(String(s).trim()) || s : s);
    return rows.map((row) => {
      const out = {};
      Object.keys(row).forEach((k) => {
        const v = row[k];
        out[t(k)] =
          !EXPORT_SKIP_KEYS.has(k) && typeof v === "string" ? t(v) : v;
      });
      return out;
    });
  }

  async function translateLabel(text) {
    if (lang !== "es") return text;
    return translate(text);
  }

  global.IntakeOpsLang = {
    init,
    setLanguage,
    getLang: () => lang,
    translate,
    translateSync,
    translateRows,
    translateLabel,
  };
})(window);