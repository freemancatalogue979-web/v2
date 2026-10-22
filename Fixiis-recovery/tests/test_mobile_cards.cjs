// No dependencies: node tests/test_mobile_cards.cjs
//
// The page archive has to stay usable on a phone. Adding the click-guard
// buttons put four controls in one flex row, and with fixed-ish widths the
// row overflowed the card and squeezed "Push" out of view. These checks make
// sure the compact layout stays in place and that the arithmetic still fits
// the narrowest phones we support (320 CSS px).
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

let checks = 0;
function ok(cond, label) {
  assert.ok(cond, label);
  checks++;
}

const root = path.join(__dirname, '..');
const mini = fs.readFileSync(path.join(root, 'mini-admin.html'), 'utf8');
const admin = fs.readFileSync(path.join(root, 'Admin.html'), 'utf8');

// Pull one @media block out by its max-width header (balanced braces).
function mediaBlock(source, maxWidth) {
  const start = source.indexOf(`@media (max-width:${maxWidth}px){`);
  assert.ok(start >= 0, `no @media(max-width:${maxWidth}px) block`);
  let depth = 0;
  for (let i = source.indexOf('{', start); i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') {
      depth--;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error('unbalanced media block');
}

// ---- mini-admin: page card -----------------------------------------------
const phone = mediaBlock(mini, 640);
ok(/\.pcard-ic\{width:40px;min-width:40px;padding:0/.test(phone.replace(/\s+/g, '')),
  'phone icon buttons get a fixed 40px box');
ok(/\.pcard-icons\{gap:5px\}/.test(phone.replace(/\s+/g, '')),
  'icon group gap shrinks on phones');
ok(/\.g-page\{grid-template-columns:1fr\}/.test(phone.replace(/\s+/g, '')),
  'page grid is a single column on phones');
ok(/\.pcard-desc\.empty\{display:none\}/.test(phone.replace(/\s+/g, '')),
  'empty description no longer reserves 32px on phones');

// The action bar must be a class, not an inline flex row: inline styles win
// over the media query and were the cause of the overflow.
const cardSrc = mini.slice(mini.indexOf('function pageCard('), mini.indexOf('function wfCard('));
ok(cardSrc.includes('class="pcard-acts"'), 'action bar uses .pcard-acts');
ok(cardSrc.includes('class="pcard-icons"'), 'icon buttons live in .pcard-icons');
ok(!/style="display:flex;gap:6px"/.test(cardSrc), 'no inline flex row in the card actions');
ok((cardSrc.match(/pcard-ic/g) || []).length >= 3, 'all three icon buttons use .pcard-ic');
ok(cardSrc.includes('class="btn sm primary pcard-push"'), 'Push keeps its primary styling');
ok(!/[\u{1F512}\u{1F513}\u{1F4AC}\u{1F507}]/u.test(cardSrc),
  'icon buttons use inline SVG, not emoji (emoji width varies per device)');

// ---- mini-admin: does the narrowest phone still fit? ---------------------
// 320px viewport - 2*14px view padding - 2*14px card padding = 264px inside.
const iconBox = 40;         // .pcard-ic width on phones
const iconGap = 5;          // .pcard-icons gap on phones
const barGap = 5;           // .pcard-acts gap on phones
const icons = iconBox * 3 + iconGap * 2;
const inner = 320 - 14 * 2 - 14 * 2;
const pushWidth = inner - icons - barGap;
ok(pushWidth >= 64, `Push still has room on a 320px phone (${pushWidth}px)`);

// ---- Admin panel: archive rows and the preview footer -------------------
ok(/\.lpv-archive-item\s*\{[\s\S]*?flex-wrap:\s*wrap/.test(admin),
  'archive rows may wrap instead of overflowing');
ok(/\.lpv-acts\s*\{[^}]*flex-wrap:\s*wrap/.test(admin),
  'archive action cluster wraps');
// Several 900px blocks exist in the panel; only the archive one matters here.
const adminNarrow = admin.slice(admin.indexOf('.lpv-acts .lpv-btn'));
ok(/\.lpv-acts\s*\{\s*flex:\s*1 1 100%/.test(adminNarrow.slice(0, 900)),
  'archive actions take their own line on narrow panels');
ok(/\.lpv-modal-footer\s*\{[\s\S]*?flex-wrap:\s*wrap/.test(admin),
  'preview modal footer wraps');
ok(/\.lpv-acts \.lpv-btn \{\s*margin-left:\s*0/.test(admin),
  'leftover inline margin-left is neutralised');

// Every archive action lands inside .lpv-acts (nothing appended to the row
// directly any more, which is what pushed the row over the edge).
const itemSrc = admin.slice(admin.indexOf("item.className = 'lpv-archive-item'"),
                            admin.indexOf('list.appendChild(item);'));
ok((itemSrc.match(/item\.appendChild\(/g) || []).length === 2,
  'the row only receives the meta block and the action cluster');
ok((itemSrc.match(/acts\.appendChild\(/g) || []).length === 6,
  'all six archive actions (incl. device versions) are in the cluster');

// ---- Device versions picker (one page, phone build + desktop build) ------
// It reuses the bindings modal shell, which is width-capped to the viewport
// and scrolls, so it works on a phone as well as on a desktop.
ok(admin.includes("ov.className = 'lpv-bind-overlay'") &&
   admin.includes('id="lpv-device-seg"'),
   'the device-version picker uses the width-capped modal shell');
ok(/\.lpv-dev-seg\s*\{[^}]*flex-wrap:\s*wrap/.test(admin),
   'device options wrap on narrow screens');
ok(/\.lpv-bind-modal\s*\{[^}]*max-height:\s*82vh[^}]*overflow-y:\s*auto/.test(admin),
   'the picker scrolls instead of overflowing a short screen');
ok(admin.includes("'[data-action=\"device\"]'"),
   'the preview dialog can open the picker too');
ok(/p\.variant_page_id/.test(mini) && mini.includes('2 versions'),
   'the mini admin marks paired pages');

console.log('mobile page-card layout checks passed');
console.log(`${checks} checks passed`);
