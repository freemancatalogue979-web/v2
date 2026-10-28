#!/usr/bin/env node
/* The clock the panels print in.

   The server writes every timestamp it produces in the zone chosen in
   Settings -> Clock -> Timezone. If the panel then prints the same instant with
   the viewer's device clock, one screen shows two different times for one
   event: an alert at 14:02 sitting above a session that says 06:02. This runs
   the panel's formatter (taken out of Admin.html, not re-typed here) to prove
   it follows the setting, keeps DST, and still degrades to the device's time
   when the server is on its own clock.

       node tests/test_clock_panel.cjs
*/

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const ADMIN = fs.readFileSync(path.join(ROOT, 'Admin.html'), 'utf8');
const MINI = fs.readFileSync(path.join(ROOT, 'mini-admin.html'), 'utf8');

let pass = 0;
const fails = [];
function ok(name, cond, detail) {
  if (cond) { pass++; return; }
  fails.push(name + (detail === undefined ? '' : '  ->  ' + detail));
}
function eq(name, actual, expected) {
  ok(name, actual === expected, 'got ' + JSON.stringify(actual) + ', want ' + JSON.stringify(expected));
}

/* -------------------------------------------------------------------------
   Lift the real formatter out of the page and run it with a controllable
   clock. Nothing is duplicated: if the page changes, this changes with it.
   ------------------------------------------------------------------------- */
function extract(source, startMarker, endMarker, label) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  if (start === -1 || end === -1) throw new Error('could not find the ' + label + ' block');
  return source.slice(start, end + endMarker.length);
}

const adminBlock = extract(ADMIN, 'function clockZoneSpec() {', 'window.__CLOCK_ZONE__ = clockZoneSpec;', 'Admin clock formatter');
const miniBlock = extract(MINI, 'let CLOCK = { zone:', 'window.loadClock = loadClock;', 'mini-admin clock formatter');

function makeAdminFormatters(clock) {
  const sandbox = { window: { __CLOCK_STATE: { clock }, __CLOCK_ZONE__: null }, Intl, Date, Math, Object, String, isNaN, parseInt, RegExp };
  vm.createContext(sandbox);
  vm.runInContext(adminBlock, sandbox);
  return { format: sandbox.window.clockFormat, time: sandbox.window.clockTime, sameDay: sandbox.window.clockSameDay };
}

function makeMiniFormatters(zone, offset) {
  const sandbox = { window: {}, Intl, Date, Math, Object, String, isNaN, parseInt, RegExp, console };
  vm.createContext(sandbox);
  // The mini block ends by assigning window.loadClock; give it the pieces it
  // expects so it runs, then set the clock the way loadClock would.
  sandbox.get = async () => ({});
  sandbox.loadAlerts = null;
  vm.runInContext(miniBlock, sandbox);
  // `let CLOCK` is a lexical binding, not a property of the sandbox object.
  vm.runInContext('CLOCK = ' + JSON.stringify({ zone: zone, offset: offset, source: zone ? 'admin' : '' }), sandbox);
  return {
    format: (v, opts) => vm.runInContext('clockFormat', sandbox)(v, opts),
    time: (v) => vm.runInContext('clockTime', sandbox)(v)
  };
}

const WINTER = new Date('2026-01-01T00:00:00Z');
const SUMMER = new Date('2026-07-01T00:00:00Z');

/* What a correct formatter must print: the instant moved into the zone and
   then read as UTC. The shift is stated explicitly (that is the definition of
   the offset), so this is not a copy of the implementation — the
   implementation has to *derive* the shift from the zone name or the setting.
   Locale is left to the device in both, so the two always agree. */
function inZone(t, shiftMinutes, opts) {
  return new Intl.DateTimeFormat(undefined, Object.assign({}, opts, { timeZone: 'UTC' }))
    .format(new Date(t.getTime() + shiftMinutes * 60000));
}
const HMS = { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' };
const YMD = { year: 'numeric', month: '2-digit', day: '2-digit' };

/* --- an IANA zone: the offset comes from the zone, DST included ---------- */
{
  const lagos = makeAdminFormatters({ name: 'Africa/Lagos', source: 'admin', offset: '+01:00' });
  eq('Lagos in January', lagos.format(WINTER, HMS), inZone(WINTER, 60, HMS));
  eq('Lagos in July', lagos.format(SUMMER, HMS), inZone(SUMMER, 60, HMS));
  eq('Lagos keeps the date', lagos.format(WINTER, YMD), inZone(WINTER, 60, YMD));
  eq('Lagos offset is the configured one in the default form',
     lagos.time(WINTER),
     inZone(WINTER, 60, { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
  const lagosMini = makeMiniFormatters('Africa/Lagos', '');
  eq('mini-admin agrees', lagosMini.time(WINTER.getTime()), inZone(WINTER, 60, { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
  eq('mini-admin agrees on the date', lagosMini.format(WINTER.getTime(), YMD), inZone(WINTER, 60, YMD));
}

{
  const tokyo = makeAdminFormatters({ name: 'Asia/Tokyo', source: 'admin', offset: '+09:00' });
  eq('Tokyo', tokyo.format(WINTER, HMS), inZone(WINTER, 540, HMS));
  const ny = makeAdminFormatters({ name: 'America/New_York', source: 'admin', offset: '-05:00' });
  eq('New York in January', ny.format(WINTER, HMS), inZone(WINTER, -300, HMS));
  eq('New York in July (DST)', ny.format(SUMMER, HMS), inZone(SUMMER, -240, HMS));
  ok('the clock really moves with DST',
     ny.format(WINTER, HMS) !== ny.format(SUMMER, HMS));
}

/* --- a fixed offset: shift the instant, print it as UTC ------------------ */
{
  const plus = makeAdminFormatters({ name: '', source: 'admin', offset: '+05:30' });
  eq('+05:30', plus.format(WINTER, HMS), inZone(WINTER, 330, HMS));
  const minus = makeAdminFormatters({ name: '', source: 'admin', offset: '-05:00' });
  eq('-05:00', minus.format(WINTER, HMS), inZone(WINTER, -300, HMS));
  eq('-05:00 across a date line', minus.format(WINTER, YMD), inZone(WINTER, -300, YMD));
  ok('the date really moved back a day',
     minus.format(WINTER, YMD) !== inZone(WINTER, 0, YMD));
}

/* --- server default: the device's own time, exactly as before ----------- */
{
  const serverClock = makeAdminFormatters({ name: '', source: 'server', offset: '+00:00' });
  eq('server default falls back to the device',
     serverClock.format(WINTER),
     WINTER.toLocaleString(undefined, { year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit' }));
  eq('server default, time only',
     serverClock.time(WINTER),
     WINTER.toLocaleString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
}

/* --- bad input must not print "Invalid Date" ---------------------------- */
{
  const lagos = makeAdminFormatters({ name: 'Africa/Lagos', source: 'admin', offset: '+01:00' });
  eq('nothing in, nothing out', lagos.format(''), '');
  eq('junk in, nothing out', lagos.format('not a date'), '');
  eq('a Date works', lagos.format(new Date(WINTER.getTime()), HMS), inZone(WINTER, 60, HMS));
  eq('a number works', lagos.format(WINTER.getTime(), HMS), inZone(WINTER, 60, HMS));
}

/* --- "same day" has to use the same clock ------------------------------- */
{
  const lagos = makeAdminFormatters({ name: 'Africa/Lagos', source: 'admin', offset: '+01:00' });
  // 22:00 and 23:30 UTC on the 31st are the same UTC day, but Lagos has
  // already rolled over to the 1st for the second one.
  const late = new Date('2025-12-31T23:30:00Z');
  const earlier = new Date('2025-12-31T22:00:00Z');
  ok('same-day is judged in the chosen zone', !lagos.sameDay(late, earlier));
  ok('and agrees when it really is the same day',
     lagos.sameDay(new Date('2026-01-01T10:00:00Z'), new Date('2026-01-01T22:00:00Z')));
  const serverClock = makeAdminFormatters({ name: '', source: 'server', offset: '+00:00' });
  ok('server default still compares like before',
     serverClock.sameDay(late, earlier) === (late.toDateString() === earlier.toDateString()));
}

/* --- the panel must actually USE the formatter -------------------------- */
{
  ok('Admin loads the clock on the settings tab', /await loadClock\(\);\s*\n\s*startClockPreview\(\);/.test(ADMIN));
  ok('Admin repaints when the clock changes', /const touchedClock = detail\.key === 'timezone'/.test(ADMIN));
  ok('Mini asks for it at boot', /loadClock\(\);\s*\n\s*loadAll\(\);/.test(MINI));
  const adminLeftovers = (ADMIN.match(/new Date\([^)]*\)\.toLocaleString\(\)/g) || []);
  eq('no bare toLocaleString() left on a server timestamp in Admin', adminLeftovers.length, 0);
  ok('no bare toLocaleTimeString() left in Admin', !/toLocaleTimeString\(\)/.test(ADMIN));
  ok('no bare toLocaleTimeString() left in mini-admin', !/toLocaleTimeString\(\)/.test(MINI));
  ok('Admin formats session times in the clock', /const firstSeen = clockFormat\(session\.first_seen\);/.test(ADMIN));
  ok('Admin formats the alert log in the clock', /clockTime\(\(e\.at \|\| 0\) \* 1000\)/.test(ADMIN));
  ok('mini-admin formats alert times in the clock', /clockFormat\(\(ts\|\|Date\.now\(\)\/1000\)\*1000/.test(MINI));
}

console.log((fails.length ? 'FAIL' : 'PASS') + '  clock panel: ' + pass + ' checks, ' + fails.length + ' failed');
fails.forEach((f) => console.log('  - ' + f));
process.exit(fails.length ? 1 : 0);
