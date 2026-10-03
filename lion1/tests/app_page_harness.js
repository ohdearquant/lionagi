'use strict';
// Runs the script the areas server serves at `/app` under node, against a small stand-in for the
// browser, and prints what a scripted session left behind as one JSON line. The shell's HTML is read
// from stdin and the scenario named in argv[2] drives the page: it emits the stream's events, answers
// each fetch when it chooses, types and clicks, and advances the timers. The document parses the
// shell's markup and every string the script writes into it, and lays a log box out at 20px a line in
// a 200px window, so a count of nodes and a scroll position mean what they do in a browser.
//
//   node app_page_harness.js <scenario> < page.html

const fs = require('node:fs');
const vm = require('node:vm');

const LINE = 20;  // px of one log line
const VIEW = 200;  // px the log box shows
const DIR = '/x/desk';

// ---- a document ----
const VOID = new Set(['input', 'meta', 'link', 'br', 'hr', 'img']);
const ENTITIES = {
  amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", hellip: '\u2026', middot: '\u00b7', nbsp: '\u00a0',
};
// a comment, the doctype, or a tag: [1] its slash, [2] its name, [3] its attributes
const TOKEN = new RegExp(
  '<!--[\\s\\S]*?-->|<!doctype[^>]*>|<(/?)([a-zA-Z][a-zA-Z0-9]*)' +
    '((?:\\s+[^\\s=>/]+(?:\\s*=\\s*(?:"[^"]*"|\'[^\']*\'|[^\\s>]+))?)*)\\s*/?>',
  'gi',
);
const ATTRIBUTE = /([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;

function decode(s) {
  return s.replace(/&(#x[0-9a-f]+|#[0-9]+|[a-z]+);/gi, (whole, e) => {
    if (e[0] !== '#') return e in ENTITIES ? ENTITIES[e] : whole;
    const hex = e[1].toLowerCase() === 'x';
    return String.fromCodePoint(parseInt(e.slice(hex ? 2 : 1), hex ? 16 : 10));
  });
}

class El {
  constructor(tag, attrs = {}) {
    this.tag = tag;
    this.attrs = attrs;
    this.kids = [];  // elements and strings
    this.parent = null;
    this.listeners = {};
    this.value = attrs.value === undefined ? '' : attrs.value;
    this.disabled = false;
    this.dataset = {};
    this.top = 0;
    for (const [k, v] of Object.entries(attrs)) {
      if (k.startsWith('data-')) this.dataset[k.slice(5).replace(/-(.)/g, (_, c) => c.toUpperCase())] = v;
    }
  }
  get id() { return this.attrs.id || ''; }
  get children() { return this.kids.filter((k) => k instanceof El); }
  get childElementCount() { return this.children.length; }
  get firstElementChild() { return this.children[0] || null; }
  get textContent() { return this.kids.map((k) => (k instanceof El ? k.textContent : k)).join(''); }
  set textContent(v) { this.setKids([String(v)]); }
  set innerHTML(html) { this.setKids(parse(html)); }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = v; }
  get classList() {
    const names = () => this.className.split(/\s+/).filter(Boolean);
    const save = (l) => { this.className = l.join(' '); };
    return {
      add: (c) => { if (!names().includes(c)) save([...names(), c]); },
      remove: (c) => save(names().filter((n) => n !== c)),
      toggle: (c, force) => {
        const on = force === undefined ? !names().includes(c) : force;
        save(on ? [...names().filter((n) => n !== c), c] : names().filter((n) => n !== c));
      },
    };
  }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  insertAdjacentHTML(where, html) {
    if (where !== 'beforeend') throw new Error('insertAdjacentHTML: only beforeend is modelled');
    this.setKids([...this.kids, ...parse(html)]);
  }
  removeChild(node) {
    if (!this.kids.includes(node)) throw new Error('removeChild: not a child');
    this.setKids(this.kids.filter((k) => k !== node));
    return node;
  }
  setKids(kids) {
    for (const k of this.kids) if (k instanceof El) k.parent = null;
    this.kids = kids;
    for (const k of kids) if (k instanceof El) k.parent = this;
    this.top = Math.min(this.top, this.maxTop());  // a browser clamps the scroll position to what is left
  }
  // layout: only the log box scrolls
  get clientHeight() { return this.className.split(/\s+/).includes('logbox') ? VIEW : 0; }
  get scrollHeight() {
    return this.className.split(/\s+/).includes('logbox') ? Math.max(VIEW, this.children.length * LINE) : 0;
  }
  maxTop() { return Math.max(0, this.scrollHeight - this.clientHeight); }
  get scrollTop() { return this.top; }
  set scrollTop(v) { this.top = Math.max(0, Math.min(v, this.maxTop())); }
}

function parse(html) {
  const root = new El('#fragment');
  let at = root;
  let last = 0;
  let m;
  TOKEN.lastIndex = 0;
  const text = (s) => { if (s) at.setKids([...at.kids, decode(s)]); };
  while ((m = TOKEN.exec(html))) {
    text(html.slice(last, m.index));
    last = TOKEN.lastIndex;
    if (!m[2]) continue;  // a comment or the doctype
    const tag = m[2].toLowerCase();
    if (m[1]) {
      for (let n = at; n !== root; n = n.parent) {
        if (n.tag === tag) { at = n.parent; break; }
      }
      continue;
    }
    const attrs = {};
    for (const a of m[3].matchAll(ATTRIBUTE)) attrs[a[1]] = decode(a[2] ?? a[3] ?? a[4] ?? '');
    const el = new El(tag, attrs);
    at.setKids([...at.kids, el]);
    if (!VOID.has(tag)) at = el;
  }
  text(html.slice(last));
  const kids = root.kids;
  root.setKids([]);
  return kids;
}

function find(node, test) {
  const out = [];
  for (const k of node.children) {
    if (test(k)) out.push(k);
    out.push(...find(k, test));
  }
  return out;
}

// ---- the page's world ----
const settle = () => new Promise((resolve) => setImmediate(resolve));  // every promise job has run

function boot(html) {
  const open = html.indexOf('<script>');
  const close = html.indexOf('</script>');
  const bodyAt = html.indexOf('<body>');
  if (open < 0 || close < open || bodyAt < 0) throw new Error('the shell has no body or no script');
  const body = new El('body');
  body.setKids(parse(html.slice(bodyAt + 6, open)));

  const requests = [];  // every fetch, answered when the scenario says
  const streams = [];  // every EventSource
  const windowListeners = {};
  const timers = new Map();
  let clock = 0;
  let nextTimer = 1;
  let tokens = 0;

  const location = { hash: '' };
  const document = { getElementById: (id) => find(body, (e) => e.id === id)[0] || null };
  const window = {
    addEventListener: (type, fn) => { (windowListeners[type] = windowListeners[type] || []).push(fn); },
  };
  class EventSource {
    constructor(url) {
      this.url = url;
      this.handlers = {};
      this.onerror = null;
      streams.push(this);
    }
    addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); }
  }
  const fetch = (url, init) => new Promise((resolve, reject) => {
    requests.push({ url, init: init || {}, resolve, reject });
  });
  const timer = (every) => (fn, ms) => {
    const id = nextTimer++;
    timers.set(id, { fn, ms, at: clock + ms, every });
    return id;
  };
  const sandbox = {
    document,
    window,
    location,
    EventSource,
    fetch,
    URLSearchParams,
    crypto: { getRandomValues: (a) => a.fill(++tokens) },
    setInterval: timer(true),
    setTimeout: timer(false),
    clearInterval: (id) => timers.delete(id),
    clearTimeout: (id) => timers.delete(id),
  };
  vm.runInNewContext(html.slice(open + 8, close), sandbox, { filename: 'app-script' });

  const fire = (target, type) => {
    for (let n = target; n; n = n.parent) for (const fn of n.listeners[type] || []) fn.call(n, { type, target });
  };
  const byId = (id) => {
    const el = document.getElementById(id);
    if (!el) throw new Error('no element #' + id);
    return el;
  };
  const page = {
    byId,
    requests: (prefix) => requests.filter((r) => r.url.startsWith(prefix)),
    actors: () => find(body, (e) => e.className.split(/\s+/).includes('actor')).map((e) => e.textContent),
    type(id, text) { const el = byId(id); el.value = text; fire(el, 'input'); },
    click(el) { if (!el.disabled) fire(el, 'click'); },
    act: (name) => find(body, (e) => e.attrs['data-act'] === name)[0],
    emit(type, data) {
      for (const fn of streams.at(-1).handlers[type] || []) fn({ data: JSON.stringify(data) });
    },
    streamError() { streams.at(-1).onerror(); },
    async answer(req, data) { req.resolve({ ok: true, json: () => Promise.resolve(data) }); await settle(); },
    async fail(req, error) { req.reject(error); await settle(); },
    async advance(ms) {
      const end = clock + ms;
      for (;;) {
        const due = [...timers].filter(([, t]) => t.at <= end).sort(([, a], [, b]) => a.at - b.at)[0];
        if (!due) break;
        const [id, t] = due;
        clock = t.at;
        if (t.every) t.at += t.ms; else timers.delete(id);
        t.fn();
        await settle();
      }
      clock = end;
    },
    async go(hash) {
      location.hash = hash;
      for (const fn of windowListeners.hashchange || []) fn();
      await settle();
    },
    async open(dir) {  // navigate to a resident and answer its detail
      await page.go('#r=' + encodeURIComponent(dir));
      await page.answer(page.requests('/api/resident').at(-1), detail(dir));
    },
  };
  return page;
}

function row(name) {
  return {
    dir: '/x/' + name, actor: name, role: 'desk', model: 'm', wake: 1, last_at: '', last_outcome: '',
    runs_today: 0, mail_today: 0, cost_today: 0, unknown_today: 0, problem: '', serving: true, in_wake: false,
    waiting: 0, controls: {},
  };
}

function detail(dir) {
  return {
    resident: { ...row(dir.split('/').pop()), pid: null },
    controls: { idle: true }, budget: {}, wake: {}, waiting: [], wakes: [], talk: [],
  };
}

const lines = (box) => box.children.map((e) => e.textContent);

// ---- the scenarios ----
const scenarios = {
  // 405 live lines, then three more with a reader 50 lines up, then the box rebuilt from the buffer
  async log(page) {
    await page.open(DIR);
    for (let i = 0; i < 405; i++) page.emit('log', { dir: DIR, line: 'line ' + i });
    let box = page.byId('log-box');
    const full = lines(box);
    const following = box.scrollTop + VIEW === box.scrollHeight;
    box.scrollTop = 50 * LINE;
    const inView = (b) => b.children[Math.floor(b.scrollTop / LINE)].textContent;
    const before = inView(box);
    for (let i = 405; i < 408; i++) page.emit('log', { dir: DIR, line: 'line ' + i });
    const after = inView(box);
    const scrolled = lines(box);
    await page.go('#');
    await page.open(DIR);
    box = page.byId('log-box');
    return {
      full: { nodes: full.length, first: full[0], last: full.at(-1), following },
      scrolled: { nodes: scrolled.length, before, after },
      rebuilt: { nodes: box.children.length, first: lines(box)[0], last: lines(box).at(-1) },
    };
  },

  // words typed while a send is out are in the composer after the send's answer and the reload
  async sending(page) {
    await page.open(DIR);
    page.type('talk-subject', 'hello');
    page.type('talk-content', 'first');
    page.click(page.byId('talk-send'));
    const send = page.requests('/send')[0];
    page.type('talk-content', 'first more');
    await page.answer(send, { ok: true });
    await page.answer(page.requests('/api/resident').at(-1), detail(DIR));
    return {
      sent: Object.fromEntries(new URLSearchParams(send.init.body)),
      subject: page.byId('talk-subject').value,
      content: page.byId('talk-content').value,
      toast: page.byId('toast').textContent,
    };
  },

  // what a send's answer clears, what a control's reload keeps, and what a new page starts with
  async clearing(page) {
    await page.open(DIR);
    page.type('talk-content', 'only');
    page.click(page.byId('talk-send'));
    await page.answer(page.requests('/send')[0], { ok: true });
    await page.answer(page.requests('/api/resident').at(-1), detail(DIR));
    const answered = page.byId('talk-content').value;
    page.type('talk-content', 'half a thought');
    page.click(page.act('wake'));
    await page.answer(page.requests('/control')[0], { ok: true, detail: 'woken' });
    await page.answer(page.requests('/api/resident').at(-1), detail(DIR));
    const reloaded = page.byId('talk-content').value;
    await page.go('#');
    await page.open(DIR);
    return { answered, reloaded, returned: page.byId('talk-content').value };
  },

  // a poll's answer after the stream has delivered is dropped, and the next poll still goes out
  async pollStale(page) {
    page.streamError();
    await page.advance(5000);
    const slow = page.requests('/api/areas')[0];
    page.emit('areas', [row('streamed')]);
    await page.answer(slow, [row('stale')]);
    const afterStale = { actors: page.actors(), conn: page.byId('conn-label').textContent };
    page.streamError();
    await page.advance(5000);
    const next = page.requests('/api/areas')[1];  // a poll that never went out is a count, not a crash
    if (next) await page.answer(next, [row('polled')]);
    return { afterStale, afterPoll: { actors: page.actors(), polls: page.requests('/api/areas').length } };
  },

  // ticks while a poll is out start no poll, and a poll that fails frees the next
  async pollSerial(page) {
    page.streamError();
    await page.advance(15000);
    const out = page.requests('/api/areas').length;
    await page.fail(page.requests('/api/areas')[0], new Error('offline'));
    await page.advance(5000);
    const after = page.requests('/api/areas').length;
    const next = page.requests('/api/areas')[1];
    if (next) await page.answer(next, [row('later')]);
    return { out, after, actors: page.actors() };
  },
};

const scenario = scenarios[process.argv[2]];
if (!scenario) throw new Error('no scenario ' + process.argv[2] + '; have ' + Object.keys(scenarios).join(', '));
scenario(boot(fs.readFileSync(0, 'utf8'))).then((seen) => process.stdout.write(JSON.stringify(seen) + '\n'));
