// The PCM pane's sizing maths, executed (not described).
//
// A frame is a grid of DEVICE pixels, so it belongs on screen at
// frame_px / devicePixelRatio CSS px and then, if the pane is smaller, shrunk
// to fit. The old maths used frame_px CSS px, which on a 2x screen stretched
// every frame over twice the device pixels it had — the live view stayed soft
// however large the capture was.
const fs = require('fs');
const path = require('path');

let checks = 0;
function ok(cond, label) {
  checks++;
  if (!cond) { console.error('FAIL: ' + label); process.exit(1); }
}

const admin = fs.readFileSync(path.join(__dirname, '..', 'Admin.html'), 'utf8');
const start = admin.indexOf('function fitPcmStage(){');
const end = admin.indexOf('// Rendering: reuse PCM image');
ok(start > 0 && end > start, 'fitPcmStage() is in Admin.html where we expect it');
const src = admin.slice(start, end);

ok(src.includes('window.devicePixelRatio'), 'the sizing asks the screen its scale');
ok(src.includes('let w = fw / dpr, h = fh / dpr;'),
  'and divides the frame by it (one frame pixel per screen pixel)');
ok(!/stage\.style\.width\s*=\s*fw\s*\+\s*'px'/.test(src),
  'never the old raw frame pixel size');

function fit(opts) {
  const stage = { style: {} };
  const img = { naturalWidth: opts.fw, naturalHeight: opts.fh };
  const wrap = { clientWidth: opts.ww, clientHeight: opts.wh };
  const $ = (id) => (id === 'pcm-frame-stage' ? stage
    : id === 'pcm-image' ? img : wrap);
  const win = { devicePixelRatio: opts.dpr };
  // eslint-disable-next-line no-new-func
  const fn = new Function('$', 'window', src + '\nreturn fitPcmStage;')($, win);
  fn();
  return { w: parseFloat(stage.style.width), h: parseFloat(stage.style.height) };
}

// 2200x1375 frame (1280 CSS page at 1.719x) on a 2x screen in a 1200x900 pane:
// 1100x688 CSS px is one frame pixel per screen pixel, and it fits.
let r = fit({ fw: 2200, fh: 1375, dpr: 2, ww: 1200, wh: 900 });
ok(r.w === 1100, '2200 frame px on a 2x screen is 1100 CSS px, got ' + r.w);
ok(r.h === 688, 'and 1375 is 688 tall, got ' + r.h);

// The same frame on a 1x screen cannot be shown at 2200 CSS px in a 1200 pane.
r = fit({ fw: 2200, fh: 1375, dpr: 1, ww: 1200, wh: 900 });
ok(r.w === 1198, 'a 1x screen shrinks it to the pane (1200 - borders), got ' + r.w);
ok(Math.abs(r.h - 749) <= 1, 'keeping the aspect ratio, got ' + r.h);

// A small pane shrinks, ratio-locked, and stays inside it.
r = fit({ fw: 2200, fh: 1375, dpr: 2, ww: 500, wh: 400 });
ok(r.w === 498 && r.h === 311, 'a 500x400 pane fits it exactly (498x311), got ' + r.w + 'x' + r.h);
ok(r.w <= 500 && r.h <= 400, 'never wider or taller than the pane');

// A hidden pane (0x0) must not collapse the picture to nothing.
r = fit({ fw: 2200, fh: 1375, dpr: 2, ww: 0, wh: 0 });
ok(r.w === 1100 && r.h === 688, 'an unmeasured pane keeps the device-exact size, got ' + r.w + 'x' + r.h);

// Small frames are never stretched past their own pixels.
r = fit({ fw: 400, fh: 300, dpr: 2, ww: 1600, wh: 1200 });
ok(r.w === 200 && r.h === 150, 'a 400x300 frame is 200x150 CSS px on a 2x screen, got ' + r.w + 'x' + r.h);

// devicePixelRatio junk must not divide by zero.
r = fit({ fw: 1000, fh: 500, dpr: 0, ww: 4000, wh: 4000 });
ok(r.w === 1000 && r.h === 500, 'a 0 DPR falls back to 1x, got ' + r.w + 'x' + r.h);

// And the panel tells the server the box it is showing, so the frames arrive
// with enough pixels in the first place.
ok(/width:\s*pane\.width,\s*height:\s*pane\.height/.test(admin),
  'view_scale carries the pane size');
ok(admin.includes('function pcmPaneResized()'), 'a pane resize renegotiates the scale');
ok(admin.includes('setTimeout(() => { pcmPaneSendTimer = null; pcmSendViewScale(); }, 380)'),
  'debounced, so a splitter drag is one recast and not sixty');

console.log('pcm view size: ' + checks + ' checks passed');
