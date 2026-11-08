/* ==========================================================================
   SHF ADMIN — COMMAND CENTRE
   --------------------------------------------------------------------------
   Binds the dashboard to the server. Every number on this screen comes from an
   endpoint that already existed before the redesign:

       GET /api/server/metrics   cpu, memory, active sessions, uptime, gpu
       GET /api/server/info      cpu count, total memory, free storage
       GET /api/links            the generated-link library

   There is no fabricated data and no placeholder chart: if an endpoint is
   unreachable the card says so instead of drawing a pretty line.

   Polling policy — the part that matters on a mid-range tablet:
     * only while the Command Centre is the visible tab;
     * only while the document is visible;
     * one request per tick (metrics), with the slow-changing endpoints
       (server info, links) refreshed every sixth tick;
     * every timer is cleared on stop(); nothing survives a tab switch.
   ========================================================================== */
(function (global) {
    'use strict';

    var SHF = global.SHF = global.SHF || {};

    var POLL_MS = 5000;
    var SLOW_EVERY = 6;          /* every 30 s */
    var HISTORY = 40;

    var Dash = {
        timer: null,
        tick: 0,
        running: false,
        history: { cpu: [], mem: [], sess: [] },
        last: null,
        info: null,
        links: null,
        errors: 0
    };
    SHF.Dashboard = Dash;

    /* ------------------------------------------------------------ helpers */
    function $(id) { return document.getElementById(id); }

    function setText(id, value) {
        var node = $(id);
        if (node && node.textContent !== String(value)) node.textContent = String(value);
    }

    function apiFetch(path) {
        /* Use the panel's own authenticated fetch when it exists — it attaches
           the admin JWT. Falling back to plain fetch keeps the dashboard
           working in the standalone panels. */
        if (typeof global.serverFetch === 'function') return global.serverFetch(path);
        var base = (typeof global.SERVER_API_BASE === 'string') ? global.SERVER_API_BASE : '';
        return global.fetch(base + path, { credentials: 'include' });
    }

    function readJSON(path) {
        return apiFetch(path).then(function (res) {
            if (!res || !res.ok) throw new Error(path + ' → ' + (res && res.status));
            return res.json();
        });
    }

    function pct(value) {
        var n = Number(value);
        if (!isFinite(n)) return 0;
        return Math.max(0, Math.min(100, Math.round(n)));
    }

    function humanMB(mb) {
        var n = Number(mb) || 0;
        if (n >= 1024) return (n / 1024).toFixed(1) + ' GB';
        return Math.round(n) + ' MB';
    }

    function humanBytes(bytes) {
        var n = Number(bytes) || 0;
        if (n >= 1e9) return (n / 1e9).toFixed(1) + ' GB';
        if (n >= 1e6) return (n / 1e6).toFixed(1) + ' MB';
        return Math.round(n / 1e3) + ' KB';
    }

    function humanUptime(seconds) {
        var s = Math.max(0, Math.floor(Number(seconds) || 0));
        var d = Math.floor(s / 86400);
        var h = Math.floor((s % 86400) / 3600);
        var m = Math.floor((s % 3600) / 60);
        if (d) return d + 'd ' + h + 'h';
        if (h) return h + 'h ' + m + 'm';
        return m + 'm ' + (s % 60) + 's';
    }

    function barClass(value, warnAt, badAt) {
        var v = Number(value) || 0;
        if (v >= badAt) return 'shf-bar-fill shf-bar-fill--bad';
        if (v >= warnAt) return 'shf-bar-fill shf-bar-fill--warn';
        return 'shf-bar-fill shf-bar-fill--ok';
    }

    function setBar(id, value) {
        var fill = $(id);
        if (!fill) return;
        var v = pct(value);
        fill.style.width = v + '%';
        fill.className = barClass(v, 70, 90);
    }

    function push(series, value) {
        series.push(Number(value) || 0);
        while (series.length > HISTORY) series.shift();
    }

    /* ------------------------------------------------------------ sparkline */
    function drawSpark(canvasId, series, colour) {
        var canvas = $(canvasId);
        if (!canvas || !canvas.getContext) return;
        var dpr = Math.min(global.devicePixelRatio || 1, 2);
        var rect = canvas.getBoundingClientRect();
        var w = Math.max(60, rect.width || 160);
        var h = Math.max(24, rect.height || 34);
        if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
            canvas.width = Math.round(w * dpr);
            canvas.height = Math.round(h * dpr);
        }
        var ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, w, h);
        if (series.length < 2) return;

        var max = Math.max.apply(null, series);
        var min = Math.min.apply(null, series);
        var span = Math.max(1, max - min);
        var x = function (i) { return (i / (series.length - 1)) * (w - 2) + 1; };
        var y = function (v) { return h - 2 - ((v - min) / span) * (h - 6); };

        ctx.beginPath();
        ctx.moveTo(x(0), h);
        series.forEach(function (v, i) { ctx.lineTo(x(i), y(v)); });
        ctx.lineTo(x(series.length - 1), h);
        ctx.closePath();
        var grad = ctx.createLinearGradient(0, 0, 0, h);
        grad.addColorStop(0, colour.replace('ALPHA', '0.28'));
        grad.addColorStop(1, colour.replace('ALPHA', '0'));
        ctx.fillStyle = grad;
        ctx.fill();

        ctx.beginPath();
        series.forEach(function (v, i) {
            if (i === 0) ctx.moveTo(x(i), y(v));
            else ctx.lineTo(x(i), y(v));
        });
        ctx.strokeStyle = colour.replace('ALPHA', '0.95');
        ctx.lineWidth = 1.6;
        ctx.lineJoin = 'round';
        ctx.stroke();

        /* The last point is the present: mark it. */
        ctx.beginPath();
        ctx.arc(x(series.length - 1), y(series[series.length - 1]), 2.2, 0, Math.PI * 2);
        ctx.fillStyle = colour.replace('ALPHA', '1');
        ctx.fill();
    }

    /* --------------------------------------------------------------- render */
    function renderMetrics(data) {
        if (!data) return;
        var cpu = (data.cpu && data.cpu.percent) != null ? data.cpu.percent : data.cpu_percent;
        var mem = (data.memory && data.memory.percent) != null ? data.memory.percent : data.memory_percent;
        var usedMb = data.memory && data.memory.used_mb;
        var totalMb = data.memory && data.memory.total_mb;
        var sessions = (data.active_sessions != null) ? data.active_sessions : data.active_sessions_count;
        var maxSessions = data.max_sessions;
        var uptime = data.uptime_s;
        var gpu = data.gpu || {};

        /* Hero core. */
        setText('shfCoreCpu', pct(cpu) + '%');
        setText('shfCoreMem', pct(mem) + '%');
        setText('shfCoreUptime', uptime != null ? humanUptime(uptime) : '—');
        setText('shfCoreSessions', (sessions != null ? sessions : '—') +
            (maxSessions ? '/' + maxSessions : ''));
        setText('shfCoreGpu', gpu.available ? 'Available' : 'CPU only');

        /* Metric cards. `stat-active-clients` and `stat-generated-links` are
           the panel's own ids — deliberately kept as the VISIBLE values so a
           legacy updater and this one can never show different numbers. */
        setText('stat-active-clients', sessions != null ? sessions : 0);
        setText('stat-generated-links', Dash.links != null ? Dash.links : 0);
        setText('shfM1Foot', sessions ? 'Live connections' : 'No one connected');
        setText('shfM2Value', maxSessions ? maxSessions : '—');
        setText('shfM2Foot', maxSessions ? 'Configured maximum' : 'No limit set');
        setText('shfM4Value', uptime != null ? humanUptime(uptime) : '—');
        if ($('stat-gpu')) setText('stat-gpu', gpu.available ? (gpu.name || 'Available') : 'N/A');

        /* Session statistics strip. */
        push(Dash.history.sess, sessions || 0);
        Dash.peak = Math.max(Dash.peak || 0, Number(sessions) || 0);
        setText('shfSessionNow', sessions != null ? sessions : '—');
        setText('shfSessionPeak', Dash.peak || 0);
        setText('shfSessionEvents', (SHF.Shell && SHF.Shell._notifs) ? SHF.Shell._notifs.length : 0);
        setText('shfSessionUpdated', new Date().toLocaleTimeString([], {
            hour: '2-digit', minute: '2-digit', second: '2-digit'
        }));

        /* Health bars. */
        var cpuBar = $('shfBarCpu');
        if (cpuBar) {
            cpuBar.style.width = pct(cpu) + '%';
            cpuBar.className = barClass(cpu, 70, 90);
        }
        var memBar = $('shfBarMem');
        if (memBar) {
            memBar.style.width = pct(mem) + '%';
            memBar.className = barClass(mem, 75, 92);
        }
        setText('shfBarCpuVal', pct(cpu) + '%');
        setText('shfBarMemVal', pct(mem) + '%');
        setText('shfBarMemSub', usedMb != null && totalMb ? humanMB(usedMb) + ' of ' + humanMB(totalMb) : '');

        if (Dash.info) {
            setText('shfBarDiskVal', '—');
            setText('shfBarDiskSub', Dash.info.storage_free != null
                ? humanBytes(Dash.info.storage_free) + ' free' : '');
            setText('shfCoreStorage', Dash.info.storage_free != null
                ? humanBytes(Dash.info.storage_free) + ' free' : '—');
            setText('shfBarCpuSub', Dash.info.cpu_count ? Dash.info.cpu_count + ' threads' : '');
        }

        /* Sparklines. */
        push(Dash.history.cpu, pct(cpu));
        push(Dash.history.mem, pct(mem));
        drawSpark('shfSparkCpu', Dash.history.cpu, 'rgba(57,201,255,ALPHA)');
        drawSpark('shfSparkMem', Dash.history.mem, 'rgba(232,61,77,ALPHA)');
        drawSpark('shfSparkSessions', Dash.history.sess, 'rgba(39,217,155,ALPHA)');

        /* Overall state — one verdict, not seven. */
        var verdict = 'ok';
        if (pct(cpu) >= 90 || pct(mem) >= 92) verdict = 'bad';
        else if (pct(cpu) >= 70 || pct(mem) >= 78) verdict = 'warn';
        Dash.state = verdict;
        var badge = $('shfDashStatus');
        if (badge) {
            badge.textContent = verdict === 'bad' ? 'Under load' : verdict === 'warn' ? 'Elevated' : 'All systems nominal';
            badge.className = 'shf-badge ' + (verdict === 'bad' ? 'shf-badge--bad'
                : verdict === 'warn' ? 'shf-badge--warn' : 'shf-badge--ok');
        }
        if (SHF.Reactor) SHF.Reactor.setStatus('shfReactor', verdict);
        Dash.last = data;
    }

    function renderError() {
        Dash.errors += 1;
        var badge = $('shfDashStatus');
        if (badge && Dash.errors >= 2) {
            badge.textContent = 'Metrics unavailable';
            badge.className = 'shf-badge shf-badge--warn';
        }
    }

    /* ----------------------------------------------------------- activity */
    function tickClock() {
        var clock = $('shfDashClock');
        if (!clock) return;
        var now = new Date();
        clock.textContent = now.toLocaleDateString([], {
            weekday: 'short', day: '2-digit', month: 'short'
        }) + ' · ' + now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function renderFeed() {
        var host = $('shfFeed');
        if (!host) return;
        var items = (SHF.Shell && SHF.Shell._notifs) ? SHF.Shell._notifs.slice(-8).reverse() : [];
        if (!items.length) {
            host.innerHTML = '<div class="shf-empty"><div class="shf-empty-title">Quiet</div>' +
                '<div class="shf-empty-text">Connections, page pushes and errors appear here as they happen.</div></div>';
            return;
        }
        host.innerHTML = items.map(function (n) {
            var colour = n.type === 'error' ? 'rgba(255,83,100,0.9)'
                : n.type === 'warning' ? 'rgba(255,184,77,0.9)'
                : n.type === 'success' ? 'rgba(39,217,155,0.9)' : 'rgba(57,201,255,0.9)';
            var when = new Date(n.at);
            var stamp = when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
            return '<div class="shf-feed-item">' +
                '<span class="shf-feed-ico" style="background:' + colour.replace('0.9', '0.14') + '">' +
                '<svg viewBox="0 0 24 24" style="fill:' + colour + '"><circle cx="12" cy="12" r="4"/></svg></span>' +
                '<div class="shf-feed-body"><div class="shf-feed-text">' + String(n.text).replace(/</g, '&lt;') +
                '</div><div class="shf-feed-time">' + stamp + '</div></div></div>';
        }).join('');
    }

    /* --------------------------------------------------------------- poll */
    function tick() {
        Dash.tick += 1;
        readJSON('/api/server/metrics').then(function (data) {
            Dash.errors = 0;
            renderMetrics(data);
            if (Dash.tick % SLOW_EVERY === 1) {
                readJSON('/api/server/info').then(function (info) {
                    Dash.info = info;
                    renderMetrics(Dash.last);
                }).catch(function () { /* keep the last known values */ });
                readJSON('/api/links').then(function (links) {
                    var list = Array.isArray(links) ? links : (links && links.links) || [];
                    Dash.links = list.length;
                    renderMetrics(Dash.last);
                }).catch(function () { /* links tab will report its own error */ });
            }
        }).catch(renderError);
        renderFeed();
    }

    Dash.start = function () {
        if (Dash.running) return;
        Dash.running = true;
        tickClock();
        Dash.clock = global.setInterval(tickClock, 30000);
        tick();
        Dash.timer = global.setInterval(function () {
            if (document.hidden) return;      /* backgrounded: no polling */
            tick();
        }, POLL_MS);
    };

    Dash.stop = function () {
        Dash.running = false;
        if (Dash.timer) global.clearInterval(Dash.timer);
        Dash.timer = null;
        if (Dash.clock) global.clearInterval(Dash.clock);
        Dash.clock = null;
    };

    Dash.status = function () { return Dash.state || 'ok'; };

    /* The feed is fed by the shell's notification stream; refresh it when the
       notification list changes without polling for it. */
    Dash.refreshFeed = renderFeed;

    /* Quick actions: any [data-go] inside the dashboard switches tab through
       the shell (which itself delegates to the panel's own switchTab). */
    document.addEventListener('click', function (e) {
        var btn = e.target.closest && e.target.closest('#dashboard-content [data-go]');
        if (!btn) return;
        e.preventDefault();
        if (SHF.Shell && SHF.Shell.go) SHF.Shell.go(btn.dataset.go);
    });

    /* Re-draw the sparks when the window changes size (canvas is pixel-sized). */
    global.addEventListener('resize', function () {
        if (!Dash.running) return;
        drawSpark('shfSparkCpu', Dash.history.cpu, 'rgba(57,201,255,ALPHA)');
        drawSpark('shfSparkMem', Dash.history.mem, 'rgba(232,61,77,ALPHA)');
        drawSpark('shfSparkSessions', Dash.history.sess, 'rgba(39,217,155,ALPHA)');
    }, { passive: true });
})(window);
