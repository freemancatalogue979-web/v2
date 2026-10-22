// No dependencies: node tests/test_page_actions.cjs
//
// Three things an operator needs after a page is already in the archive:
//
//   1. EDIT it — paste or re-upload new HTML, keeping everything else
//   2. ACTIONS — "if they click this, show that page / open that link"
//   3. WORKFLOW — an action step that arms such a click mid-chain
//
// The browser side is checked here (the client's click path) and the panel
// side is checked against Admin.html. The store/API behaviour is covered by
// tests/test_page_actions.py.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

let checks = 0;
const ok = (cond, label) => { assert.ok(cond, label); checks++; };

const root = path.join(__dirname, '..');
const read = (f) => fs.readFileSync(path.join(root, f), 'utf8');

const api = read('api.py');
const store = read('lpv_store.py');
const admin = read('Admin.html');
const client = read('client.html');

// ---- 1. editing an archive ----------------------------------------------
ok(/def replace_page_html\(/.test(store), 'store can replace a page\'s HTML in place');
ok(/lpv_page_versions/.test(store.slice(store.indexOf('def replace_page_html'),
                                        store.indexOf('def rename_page'))),
   'the outgoing HTML is snapshotted before it is replaced');
ok(api.includes('@app.put("/api/lpv/archive/{page_id}/html")'), 'PUT .../html replaces the content');
ok(api.includes('@app.get("/api/lpv/archive/{page_id}/source")'), 'there is a raw source route to edit from');
const sourceFn = api.slice(api.indexOf('async def lpv_archive_source'),
                          api.indexOf('async def lpv_archive_replace_html'));
ok(/read_page_html/.test(sourceFn), 'the source route reads the stored file');
ok(!/apply_bindings|_apply_page_bindings/.test(sourceFn),
   'the source route never returns a rendered copy');
const replaceFn = api.slice(api.indexOf('async def lpv_archive_replace_html'),
                            api.indexOf('async def lpv_page_actions_get'));
ok(/_sanitize_lpv_html/.test(replaceFn), 'uploaded HTML is sanitised like a fresh upload');
ok(/verify_admin_token/.test(replaceFn), 'editing is admin-only');

ok(/id="lpv-edit-html"/.test(admin), 'Admin has an editor area');
ok(/function lpvEditHtml\(/.test(admin), 'Admin opens it from the page card');
ok(admin.includes("'/api/lpv/archive/' + pageId + '/html'"), 'Admin saves through the replace route');
ok(/id="lpv-edit-file"/.test(admin), 'Admin can load a file into the editor');
ok(/id="lpv-edit-reload"/.test(admin), 'Admin can discard changes and reload the stored copy');
ok(/data-action="edit"/.test(admin), 'the preview dialog offers editing too');

// ---- 2. click actions ----------------------------------------------------
ok(/def normalise_actions\(/.test(store), 'actions are validated in one place');
ok(/ACTION_MAX_RULES = 20/.test(store), 'the list is bounded');
ok(/"actions": normalise_actions\(_row_get\(row, "actions"\)\)/.test(store),
   'actions load with the page');
ok(/if "actions" not in page_cols/.test(store), 'an old database gains the column');

ok(api.includes('@app.get("/api/lpv/pages/{page_id}/actions")'), 'GET actions');
ok(api.includes('@app.put("/api/lpv/pages/{page_id}/actions")'), 'PUT actions');
ok(/page not found: /.test(api), 'a page target must exist before it is saved');

// every push hands the actions over
ok((api.match(/"actions": _page_actions\(/g) || []).length >= 4,
   'manual push, workflow step, default page and chat redirect all ship actions');

ok(/async def _run_action\(/.test(api), 'one place carries a click out');
ok(/async def _deliver_page\(/.test(api), 'page targets go through the normal push path');
ok(/_page_guard\(served_id, page\)[\s\S]{0,200}_page_actions\(served_id, page\)/.test(api),
   'a delivered page carries both its guard and its actions');
ok(/if not _chat_device_allows/.test(api) || /_chat_device_allows\(/.test(api),
   'chat availability still applies around redirect mode');

ok(/function lpvActionsOpen\(/.test(admin), 'Admin has an actions editor');
ok(/class="lpv-act-match"/.test(admin), 'a row names the control to watch');
ok(/<option value="page">go to a page<\/option>/.test(admin), 'one target is an archived page');
ok(/<option value="link">open a link<\/option>/.test(admin), 'the other is a link');
ok(/data-action="actions"/.test(admin), 'the preview dialog can open it');
ok(/actBtn\.textContent = _nAct \? '⚡ ' \+ _nAct : '⚡'/.test(admin), 'the card shows how many actions exist');

// ---- 3. workflow action step --------------------------------------------
ok(/if kind == "action":/.test(store), 'the store validates an action step');
ok(store.includes('step_out["kind"] = "action"'), 'the step keeps its kind');
ok(/elif kind == "action":/.test(api), 'the runner handles an action step');
const runnerAction = api.slice(api.indexOf('elif kind == "action":'),
                               api.indexOf('elif kind == "goto":'));
ok(/"type": "lpv_arm_action"/.test(runnerAction), 'the runner arms the rule on the client');
ok(/await asyncio\.wait_for\(fired_event\.wait\(\), timeout=wait_secs\)/.test(runnerAction),
   'it can wait for the click before carrying on');
ok(admin.includes('id="lpv-wf-add-action"'), 'the workflow editor has a + Action button');
ok(/data-step-field="action_match"/.test(admin), 'the step renders its matcher');
ok(/data-step-field="action_do"/.test(admin), 'and its target kind');
ok(/s\.kind === 'action'/.test(admin), 'the step renders itself');

// ---- 4. the client: click -> server -------------------------------------
ok(/function guardMatchesRule\(/.test(client), 'the client has the guard rule matcher');
ok(/setActions\(actions\)/.test(client), 'the client stores the page actions');
ok(/armAction\(msg\)/.test(client), 'the client can be armed by a workflow');
ok(/triggerAction\(el\)/.test(client), 'a click is matched to an action');
ok(/type: 'lpv_action'/.test(client), 'the client reports the click');
const clickHandler = client.slice(client.indexOf("innerDoc.addEventListener('click'"),
                                  client.indexOf("innerDoc.addEventListener('submit'"));
ok(clickHandler.indexOf('triggerAction') < clickHandler.indexOf('LPV.allows'),
   'a mapped control wins over the click guard');

// Run the matcher/trigger logic against fake elements.
const start = client.indexOf('matchAction(el) {');
const end = client.indexOf('isLocked() {', start);
assert.ok(start > 0 && end > start, 'action matcher not found');
function actionHit(state, el) {
  const ctx = vm.createContext({
    el, state, Date: { now: () => 1000 }, Object, String, Array, console,
    guardMatchesRule: (node, rule) => node.matches === rule,
  });
  const obj = vm.runInContext(
    '({' +
    '  actions: state.actions, armed: state.armed, _lastActionAt: 0, _sent: [],' +
    '  _send(m) { this._sent.push(m); },' +
    client.slice(start, end) +
    '})', ctx);
  Object.assign(ctx.LPV || (ctx.LPV = {}), obj);
  ctx.LPV.actions = state.actions;
  ctx.LPV.armed = state.armed;
  const hit = ctx.LPV.triggerAction(el);
  return { hit, sent: ctx.LPV._sent };
}
const state = {
  actions: [{ match: 'text:Sign in', do: 'page', page_id: 'p2' }],
  armed: [],
};
ok(actionHit(state, { matches: 'text:Sign in' }).hit, 'the mapped control matches');
ok(actionHit(state, { matches: 'text:Sign in' }).sent[0].type === 'lpv_action',
   'matching a control tells the server');
ok(actionHit(state, { matches: 'something else' }).hit === false,
   'an unmapped control is left to the page');
state.armed = [{ id: 'arm-1', match: '.next' }];
ok(actionHit(state, { matches: '.next' }).sent[0].armed === 'arm-1',
   'an armed workflow rule fires by its id');

// ---- 5. chat bubble: two modes ------------------------------------------
ok(/chat_mode: str = "chat"/.test(read('server_settings.py')), 'bubble defaults to chat mode');
ok(/chat_redirect_kind: str = "link"/.test(read('server_settings.py')), 'redirect defaults to a link');
ok(/"mode": mode/.test(api) && /"redirect_kind": kind/.test(api), 'the client is told the mode and target');
ok(/async def _handle_chat_redirect\(/.test(api), 'the server performs the redirect');
ok(/msg_type == "chat_redirect"/.test(api) && /msg_type == 'chat_redirect'/.test(api),
   'both websocket paths accept it');
ok(/redirects\(\)/.test(client), 'the client knows it is in redirect mode');
const bubbleSrc = client.slice(client.indexOf("bubble.addEventListener('click'"),
                               client.indexOf("panel.querySelector('.lpvch-close')"));
ok(/this\.redirects\(\)/.test(bubbleSrc) && /chat_redirect/.test(bubbleSrc),
   'pressing the bubble in redirect mode asks the server to move');
ok(/if \(this\.redirects\(\)\) this\.toggle\(false\)/.test(client),
   'switching to redirect mode closes an open conversation');
ok(/if \(!this\.redirects\(\)\) this\.requestHistory\(\);/.test(
   client.slice(client.indexOf('this.badge = bubble'), client.indexOf('destroy() {'))),
   'redirect mode does not ask for a chat thread');
// Admin: the chat pane carries the settings, including the two modes.
ok(/id="lpv-chat-enabled"/.test(admin), 'the chat pane has the on/off switch');
ok(/id="lpv-chat-mode"/.test(admin), 'the chat pane has the mode selector');
ok(/<option value="redirect">Go to a page or link<\/option>/.test(admin), 'redirect mode is offered');
ok(/id="lpv-chat-redirect-page"/.test(admin), 'the bubble can go to an archived page');
ok(/id="lpv-chat-redirect-url"/.test(admin), 'the bubble can go to a link');
ok(/function lpvChatSettingsLoad\(/.test(admin), 'the settings load into the pane');
ok(/function lpvChatSettingsSave\(/.test(admin), 'and save from it');

ok(admin.includes("else if (name === 'chat') { lpvChatLoadThreads(); lpvChatSettingsLoad(); }"),
   'opening the Chat tab refreshes them');

console.log('page actions + edit + chat modes: ' + checks + ' checks passed');
