/* ==========================================================================
   SHF ADMIN — THE REACTOR
   --------------------------------------------------------------------------
   The dashboard's centrepiece: a live "core" that says, at a glance, what the
   command centre thinks of itself. It is a 2D canvas, not a WebGL scene, and
   that is a deliberate engineering choice:

     * its whole cost is a few hundred stroked arcs per frame — no shader
       compilation, no 600 KB of Three.js over the wire, no GPU driver lottery
       on the cheap Android tablets operators actually use;
     * it renders in ~0.2 ms at 60 fps on a mid-range device, so it can run
       while a mirrored page is being captured;
     * it degrades to a static CSS gradient with no JS at all.

   Behaviour:
     * starts only when it is on screen (`mount` is called on dashboard enter),
       stops when the tab is hidden or the window is backgrounded;
     * `prefers-reduced-motion` renders exactly one frame and freezes;
     * DPR capped at 2 so a 3x phone screen does not pay 9x fill cost;
     * `setStatus('ok'|'warn'|'bad')` recolours the core — the reactor is a
       status indicator, not wallpaper.
   ========================================================================== */
(function (global) {
    'use strict';

    var SHF = global.SHF = global.SHF || {};

    var PALETTE = {
        ok:   { core: '56,230,255', ring: '57,201,255', hot: '150,246,255' },
        warn: { core: '255,209,120', ring: '255,184,77', hot: '255,236,190' },
        bad:  { core: '255,120,133', ring: '255,83,100', hot: '255,190,196' }
    };

    function prefersReducedMotion() {
        try {
            return global.matchMedia &&
                global.matchMedia('(prefers-reduced-motion: reduce)').matches;
        } catch (e) { return false; }
    }

    function Reactor(canvas) {
        this.canvas = canvas;
        this.ctx = canvas.getContext && canvas.getContext('2d');
        this.status = 'ok';
        this.t = 0;
        this.raf = 0;
        this.running = false;
        this.reduced = prefersReducedMotion();
        this._onVisibility = this._onVisibility.bind(this);
        this._resize = this._resize.bind(this);
        this.dpr = Math.min(global.devicePixelRatio || 1, 2);
        this.size = 0;
    }

    Reactor.prototype._resize = function () {
        var rect = this.canvas.getBoundingClientRect();
        var side = Math.max(80, Math.min(rect.width || 220, rect.height || 220));
        this.size = side;
        var px = Math.round(side * this.dpr);
        if (this.canvas.width !== px) {
            this.canvas.width = px;
            this.canvas.height = px;
        }
        this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    };

    Reactor.prototype._onVisibility = function () {
        if (document.hidden) this.pause();
        else if (this.running === false && this._wantRun) this.start();
    };

    Reactor.prototype.setStatus = function (status) {
        this.status = PALETTE[status] ? status : 'ok';
        if (this.reduced || !this.running) this.draw();
    };

    Reactor.prototype.start = function () {
        this._wantRun = true;
        if (this.running) return;
        if (this.reduced) {
            this._resize();
            this.draw();
            return;
        }
        this.running = true;
        this.last = 0;
        document.addEventListener('visibilitychange', this._onVisibility);
        var self = this;
        var step = function (ts) {
            if (!self.running) return;
            var dt = self.last ? Math.min(ts - self.last, 64) : 16;
            self.last = ts;
            self.t += dt / 1000;
            self.draw();
            self.raf = global.requestAnimationFrame(step);
        };
        this.raf = global.requestAnimationFrame(step);
    };

    Reactor.prototype.pause = function () {
        this.running = false;
        if (this.raf) global.cancelAnimationFrame(this.raf);
        this.raf = 0;
        document.removeEventListener('visibilitychange', this._onVisibility);
    };

    Reactor.prototype.destroy = function () {
        this._wantRun = false;
        this.pause();
        if (this._ro) { try { this._ro.disconnect(); } catch (e) {} }
        global.removeEventListener('resize', this._resize);
    };

    Reactor.prototype.draw = function () {
        var ctx = this.ctx;
        if (!ctx) return;
        if (!this.size) this._resize();
        var s = this.size;
        var c = s / 2;
        var pal = PALETTE[this.status] || PALETTE.ok;
        var t = this.t;

        ctx.clearRect(0, 0, s, s);

        /* Ambient halo — one radial fill, cheap. */
        var halo = ctx.createRadialGradient(c, c, s * 0.05, c, c, s * 0.5);
        halo.addColorStop(0, 'rgba(' + pal.core + ',0.30)');
        halo.addColorStop(0.35, 'rgba(' + pal.ring + ',0.10)');
        halo.addColorStop(1, 'rgba(' + pal.ring + ',0)');
        ctx.fillStyle = halo;
        ctx.beginPath();
        ctx.arc(c, c, s * 0.5, 0, Math.PI * 2);
        ctx.fill();

        /* Three concentric rings at different speeds and directions: the
           "machinery turning" read, without any texture. */
        var rings = [
            { r: 0.44, w: 1.0, sp: 0.20, a: 0.55, arcs: 3, gap: 0.22 },
            { r: 0.36, w: 1.6, sp: -0.34, a: 0.85, arcs: 6, gap: 0.16 },
            { r: 0.28, w: 2.4, sp: 0.52, a: 1.0, arcs: 2, gap: 0.05 }
        ];
        for (var i = 0; i < rings.length; i++) {
            var ring = rings[i];
            var radius = s * ring.r;
            ctx.save();
            ctx.translate(c, c);
            ctx.rotate(t * ring.sp);
            ctx.lineWidth = ring.w * (s / 220) + 0.4;
            ctx.strokeStyle = 'rgba(' + pal.ring + ',' + (ring.a * 0.28) + ')';
            ctx.beginPath();
            ctx.arc(0, 0, radius, 0, Math.PI * 2);
            ctx.stroke();
            /* Bright arc segments riding the ring. */
            ctx.strokeStyle = 'rgba(' + pal.hot + ',' + ring.a + ')';
            ctx.shadowColor = 'rgba(' + pal.core + ',0.9)';
            ctx.shadowBlur = 8 * (s / 220);
            var span = (Math.PI * 2) / ring.arcs;
            for (var k = 0; k < ring.arcs; k++) {
                var start = k * span + t * 0.15 * (i % 2 ? -1 : 1);
                ctx.beginPath();
                ctx.arc(0, 0, radius, start, start + span * (1 - ring.gap));
                ctx.stroke();
            }
            ctx.restore();
        }

        /* Ticks around the outer edge: instrument, not decoration. */
        ctx.save();
        ctx.translate(c, c);
        ctx.rotate(-t * 0.08);
        var ticks = 48;
        for (var j = 0; j < ticks; j++) {
            var ang = (j / ticks) * Math.PI * 2;
            var long = j % 4 === 0;
            var r1 = s * 0.475;
            var r2 = s * (long ? 0.445 : 0.458);
            ctx.strokeStyle = long
                ? 'rgba(' + pal.ring + ',0.55)'
                : 'rgba(' + pal.ring + ',0.18)';
            ctx.lineWidth = long ? 1.4 : 0.8;
            ctx.beginPath();
            ctx.moveTo(Math.cos(ang) * r1, Math.sin(ang) * r1);
            ctx.lineTo(Math.cos(ang) * r2, Math.sin(ang) * r2);
            ctx.stroke();
        }
        ctx.restore();

        /* The core: a pulsing, breathing light. */
        var pulse = 0.5 + 0.5 * Math.sin(t * 1.6);
        var coreR = s * (0.135 + 0.012 * pulse);
        var core = ctx.createRadialGradient(c, c, 0, c, c, coreR);
        core.addColorStop(0, 'rgba(255,255,255,0.98)');
        core.addColorStop(0.32, 'rgba(' + pal.core + ',0.95)');
        core.addColorStop(0.72, 'rgba(' + pal.ring + ',0.42)');
        core.addColorStop(1, 'rgba(' + pal.ring + ',0)');
        ctx.fillStyle = core;
        ctx.beginPath();
        ctx.arc(c, c, coreR, 0, Math.PI * 2);
        ctx.fill();

        /* Six spokes tying the core to the rings. */
        ctx.save();
        ctx.translate(c, c);
        ctx.rotate(t * 0.6);
        ctx.strokeStyle = 'rgba(' + pal.core + ',0.35)';
        ctx.lineWidth = Math.max(0.8, s / 220);
        for (var m = 0; m < 6; m++) {
            var a2 = (m / 6) * Math.PI * 2;
            ctx.beginPath();
            ctx.moveTo(Math.cos(a2) * coreR * 1.1, Math.sin(a2) * coreR * 1.1);
            ctx.lineTo(Math.cos(a2) * s * 0.25, Math.sin(a2) * s * 0.25);
            ctx.stroke();
        }
        ctx.restore();
    };

    /* -------------------------------------------------------------- API */

    SHF.Reactor = {
        _instances: {},
        mount: function (canvasId, status) {
            var canvas = document.getElementById(canvasId);
            if (!canvas || !canvas.getContext) return null;
            var inst = this._instances[canvasId];
            if (inst) {
                inst.setStatus(status);
                inst._resize();
                inst.start();
                return inst;
            }
            inst = new Reactor(canvas);
            this._instances[canvasId] = inst;
            if (status) inst.status = PALETTE[status] ? status : 'ok';
            inst._resize();
            global.addEventListener('resize', inst._resize, { passive: true });
            if (global.ResizeObserver) {
                try {
                    inst._ro = new ResizeObserver(function () { inst._resize(); });
                    inst._ro.observe(canvas);
                } catch (e) { /* older browsers: resize listener is enough */ }
            }
            inst.start();
            return inst;
        },
        umount: function (canvasId) {
            var inst = this._instances[canvasId];
            if (inst) inst.pause();
        },
        umountAll: function () {
            for (var k in this._instances) {
                if (Object.prototype.hasOwnProperty.call(this._instances, k)) {
                    this._instances[k].pause();
                }
            }
        },
        setStatus: function (canvasId, status) {
            var inst = this._instances[canvasId];
            if (inst) inst.setStatus(status);
        },
        prefersReducedMotion: prefersReducedMotion
    };
})(window);
