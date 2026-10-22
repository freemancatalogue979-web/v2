// No dependencies: node tests/test_chat_switch.cjs
//
// The chat bubble is controlled from Settings: on/off, and whether it shows on
// both devices, desktop only, or mobile only. Three things have to line up:
//
//   1. the operator has a control for each choice (both panels)
//   2. the server decides per visitor, at boot and when the switch changes
//   3. the client refuses to build the bubble on the wrong device, so a
//      broadcast to every socket cannot put it on a phone
//
// This checks the wiring; the decision table itself is covered by
// tests/test_chat.py.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

let checks = 0;
const ok = (cond, label) => { assert.ok(cond, label); checks++; };

const root = path.join(__dirname, '..');
const read = (f) => fs.readFileSync(path.join(root, f), 'utf8');

const api = read('api.py');
const admin = read('Admin.html');
const mini = read('mini-admin.html');
const client = read('client.html');
const settings = read('server_settings.py');

// ---- 1. the settings exist and default to something safe -----------------
ok(/chat_enabled: bool = False/.test(settings), 'chat starts switched off');
ok(/chat_devices: str = "both"/.test(settings), 'when switched on it is for both devices');
ok(/def chat_device_allows\(/.test(settings), 'the device decision lives with the settings');
ok(/_CHAT_DEVICES = \("both", "desktop", "mobile"\)/.test(settings), 'three choices, nothing else');
ok(/if key == "chat_devices"/.test(settings), 'a typo cannot be stored as the mode');

// ---- 2. the server answers per visitor -----------------------------------
ok(/def _chat_config\(device: Optional\[str\] = None\)/.test(api),
   'the chat config is computed for a device');
ok(/"devices": devices/.test(api), 'the client is told which devices are allowed');
ok(/"chat": _chat_config\(_device_from_init\(init_data\)\)/.test(api),
   'the boot payload uses the device the visitor connected with');
// The re-broadcast must be per socket, not one config for everybody.
const pushFn = api.slice(api.indexOf('async def _push_chat_config_to_clients'),
                          api.indexOf('async def _push_lpv_branding_to_clients'));
ok(/targets\.append\(\(cid, ws\)\)/.test(pushFn), 'the broadcast keeps the client id next to each socket');
ok(/await _client_device\(cid\)/.test(pushFn), 'each socket gets its own device looked up');
ok(/_chat_config\(device\)/.test(pushFn), 'each socket gets its own answer');
// A stale widget on the wrong device cannot write into a thread.
const chatHandler = api.slice(api.indexOf('async def _handle_client_chat'),
                               api.indexOf('def _apply_redaction'));
ok(/_chat_device_allows\(/.test(chatHandler), 'inbound chat is filtered by device too');
ok(/_client_device\(client_id\)/.test(chatHandler), 'using the stored device of that client');

// ---- 3. the operator has the controls ------------------------------------
ok(/id="settings-chat-enabled"/.test(admin), 'Admin: a switch for the bubble');
ok(/id="settings-chat-devices"/.test(admin), 'Admin: a choice of devices');
ok(/<option value="desktop">Desktop only<\/option>/.test(admin), 'Admin: desktop only');
ok(/<option value="mobile">Mobile only<\/option>/.test(admin), 'Admin: mobile only');
ok(/chat_enabled:\s+document\.getElementById\('settings-chat-enabled'\)\.checked/.test(admin),
   'Admin: the switch is saved');
ok(/chat_devices:\s+\(document\.getElementById\('settings-chat-devices'\)\.value/.test(admin),
   'Admin: the device choice is saved');
ok(/\$\('settings-chat-enabled'\)\.checked\s+= !!s\.chat_enabled/.test(admin),
   'Admin: the switch shows the saved value');
ok(/\$\('settings-chat-devices'\)\.value\s+=/.test(admin),
   'Admin: the device choice shows the saved value');

ok(/id="stChat"/.test(mini), 'mini: the switch is reachable from a phone');
ok(/id="stChatDevices"/.test(mini), 'mini: the device choice too');
ok(/id="settings-chat-mode"/.test(admin), 'Admin: the bubble mode is in the Settings tab too');
ok(/id="settings-chat-page"/.test(admin), 'Admin: the Settings tab can pick an archived page');
ok(/id="settings-chat-url"/.test(admin), 'Admin: the Settings tab can take a link');
ok(/chat_redirect_page_id:/.test(admin), 'Admin: the chosen page is saved with the tab settings');
ok(/id="stChatMode"/.test(mini), 'mini: the bubble mode is reachable from a phone');
ok(/id="stChatPage"/.test(mini), 'mini: the archive list is reachable from a phone');
ok(/chat_enabled: \$\('#stChat'\)\.checked/.test(mini), 'mini: the switch is saved');
ok(/chat_devices: \$\('#stChatDevices'\)\.value/.test(mini), 'mini: the device choice is saved');

// ---- 4. the client filters for itself ------------------------------------
ok(/_deviceAllowed\(cfg\)/.test(client), 'the client checks the device before mounting');
ok(/const on = !!cfg\.enabled && this\._deviceAllowed\(cfg\)/.test(client),
   'both the switch and the device must agree');
ok(/if \(!on\) \{ this\.destroy\(\); return; \}/.test(client),
   'turning it off removes an existing bubble without a reload');

// Run the extracted device check against fake user agents.
const start = client.indexOf('_deviceAllowed(cfg) {');
const end = client.indexOf('applyConfig(cfg) {', start);
assert.ok(start > 0 && end > start, 'device check not found in client.html');
function allows(mode, ua) {
  const ctx = vm.createContext({ navigator: { userAgent: ua }, String });
  const chat = vm.runInContext('({' + client.slice(start, end) + '})', ctx);
  return chat._deviceAllowed({ devices: mode });
}
const IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15';
const MAC = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15';
ok(allows('both', IPHONE) && allows('both', MAC), 'both = everyone');
ok(allows('desktop', MAC) && !allows('desktop', IPHONE), 'desktop only stays off on a phone');
ok(allows('mobile', IPHONE) && !allows('mobile', MAC), 'mobile only stays off on a desktop');
ok(allows('', IPHONE), 'a config without a mode behaves like before (both)');

// ---- 5. no addresses introduced -----------------------------------------
ok(!/209\.74\.83\.91/.test(admin + mini + client), 'no production address in the panels');

console.log('chat switch: ' + checks + ' checks passed');
