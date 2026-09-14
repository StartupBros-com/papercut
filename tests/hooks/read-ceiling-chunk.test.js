'use strict';
/**
 * Behavior tests over the SHIPPED read-ceiling-chunk.js.
 *
 * This hook requires its sibling capture module as `./papercut-log.js` (the
 * packaging-specific rewrite papercut-vendor.py makes to the source's
 * `../PostToolUseFailure/papercut-log.js`). The require below resolves that
 * at load time against the SHIPPED layout — if the rewrite ever regressed,
 * this whole file would fail to load with MODULE_NOT_FOUND before a single
 * test ran, and every spawn-based test also asserts a clean exit code, which
 * a broken require would not produce either.
 */
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { runHook, assertPassThrough, assertPreToolUseDeny, mkTmp, rmTmp } = require('./lib/harness');
const { projectSlug } = require('../../hooks/papercut-log.js');
const { rungFor } = require('../../hooks/read-ceiling-chunk.js');

const HOOK = 'read-ceiling-chunk.js';
const CEILING_SIG = 'read:file content (<n> tokens) exceeds maximum allowed tokens (<n>). use offset and limit parameters to read specific portion';
const CWD = '/srv/app';
const FILE = '/srv/app/big.diff';

function storeWith(dir, records) {
  const f = path.join(dir, `${projectSlug(CWD)}.jsonl`);
  fs.writeFileSync(f, records.map((r) => JSON.stringify(r)).join('\n') + '\n');
  return dir;
}
const failure = (over = {}) => ({
  ts: '2026-09-02T05:00:00.000Z', sig: CEILING_SIG, tool: 'Read',
  err: 'File content (<n> tokens) exceeds maximum allowed tokens (<n>).',
  cmd: '', target: FILE, cwd: CWD, session: 'abcd1234', agent: '', source: 'auto', ...over,
});
const payload = (over = {}) => ({
  hook_event_name: 'PreToolUse', tool_name: 'Read', tool_input: { file_path: FILE },
  session_id: 'sess-0000-abcd1234', cwd: CWD, ...over,
});
function denied(res) {
  assertPreToolUseDeny(res);
  const o = JSON.parse(res.stdout).hookSpecificOutput;
  assert.strictEqual('updatedInput' in o, false, 'a denial never rewrites');
  return o.permissionDecisionReason;
}

// --- 6. read-ceiling-chunk: corrective behavior -----------------------------

test('rungFor: evidence halves, the repeat bound halves, the prediction caps, the smallest wins', () => {
  assert.strictEqual(rungFor(1, 2000, 374), 374);
  assert.strictEqual(rungFor(1, 2000, Infinity), 1000);
  assert.strictEqual(rungFor(1, 2000, 5000), 1000);
  assert.strictEqual(rungFor(1, 2000), 1000);
  assert.strictEqual(rungFor(2, 2000), 500);
  assert.strictEqual(rungFor(3, 1000), 250);
});

test('a whole-file retry after one recorded ceiling failure is denied with the window that fits', async () => {
  const dir = mkTmp('read-chunk-');
  try {
    storeWith(dir, [failure()]);
    const reason = denied(await runHook(HOOK, payload(), { env: { PAPERCUT_STORE: dir } }));
    assert.match(reason, /big\.diff/);
    assert.match(reason, /failed the token ceiling 1 time/);
    assert.match(reason, /asks for 2000 lines/);
    assert.match(reason, /offset 1 and limit 1000/);
    assert.match(reason, /continue from offset 1001/);
  } finally { rmTmp(dir); }
});

test('a retry already at or under the rung is the agent converging and passes through', async () => {
  const dir = mkTmp('read-chunk-');
  try {
    storeWith(dir, [failure()]);
    const env = { PAPERCUT_STORE: dir };
    const atRung = payload({ tool_input: { file_path: FILE, limit: 1000 } });
    assertPassThrough(await runHook(HOOK, atRung, { env }), atRung);
    const under = payload({ tool_input: { file_path: FILE, offset: 400, limit: 200 } });
    assertPassThrough(await runHook(HOOK, under, { env }), under);
  } finally { rmTmp(dir); }
});

test('below the floor no window fits: the denial says so and points at Grep or a Bash extract', async () => {
  const dir = mkTmp('read-chunk-');
  try {
    const env = { PAPERCUT_STORE: dir };
    // A 13-line JSON artifact with one huge line failed at limit 7; half of 7
    // is under the floor.
    storeWith(dir, [failure({ page: { limit: 2000 } }), failure({ page: { offset: 0, limit: 7 } })]);
    const tiny = payload({ tool_input: { file_path: FILE, limit: 3 } });
    assertPassThrough(await runHook(HOOK, tiny, { env }), tiny);
    const reason = denied(await runHook(HOOK, payload({ tool_input: { file_path: FILE, limit: 100 } }), { env }));
    assert.match(reason, /no Read window fits/);
    assert.match(reason, /Grep/);
    assert.match(reason, /head -c/);
  } finally { rmTmp(dir); }
});

test('a denial logs itself as guard_blocked:read-ceiling-chunk through the shared capture module', async () => {
  // Proves the sibling require: logDenial() is imported from papercut-log.js
  // and this is the record it writes when the ceiling guard fires.
  const dir = mkTmp('read-chunk-');
  try {
    const env = { PAPERCUT_STORE: dir };
    storeWith(dir, [failure({ agent: 'aaaaaaaa' })]);
    denied(await runHook(HOOK, payload({ agent_id: 'agent-aaaaaaaa' }), { env }));
    const lines = fs.readFileSync(path.join(dir, `${projectSlug(CWD)}.jsonl`), 'utf8').trim().split('\n');
    assert.strictEqual(lines.length, 2, 'one ceiling failure plus one denial record');
    const rec = JSON.parse(lines[1]);
    assert.strictEqual(rec.sig, 'guard_blocked:read-ceiling-chunk');
    assert.strictEqual(rec.source, 'guard');
    assert.strictEqual(rec.tool, 'Read');
    assert.strictEqual(rec.cmd, FILE);
    assert.strictEqual(rec.agent, 'aaaaaaaa');
    assert.match(rec.err, /limit 1000/);
  } finally { rmTmp(dir); }
});

test('the first attempt is never touched: no recorded failure, no denial', async () => {
  const dir = mkTmp('read-chunk-');
  try {
    storeWith(dir, [failure({ target: '/srv/app/other.diff' })]);
    const p = payload();
    assertPassThrough(await runHook(HOOK, p, { env: { PAPERCUT_STORE: dir } }), p);
  } finally { rmTmp(dir); }
});

test('other tools, a missing store, and malformed input all fail open (and the shipped require never crashes the process)', async () => {
  const dir = mkTmp('read-chunk-');
  try {
    const env = { PAPERCUT_STORE: dir };
    const bash = payload({ tool_name: 'Bash', tool_input: { command: 'ls' } });
    const bashRes = await runHook(HOOK, bash, { env });
    assertPassThrough(bashRes, bash);
    assert.doesNotMatch(bashRes.stderr, /MODULE_NOT_FOUND|Cannot find module/);

    const p = payload();
    const missingStoreRes = await runHook(HOOK, p, { env: { PAPERCUT_STORE: path.join(dir, 'nope') } });
    assertPassThrough(missingStoreRes, p);

    const res = await runHook(HOOK, '{not json', { env });
    assert.strictEqual(res.stdout, '{not json');
    assert.strictEqual(res.code, 0);
  } finally { rmTmp(dir); }
});
