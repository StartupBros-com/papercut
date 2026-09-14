'use strict';
/**
 * Behavior tests over the SHIPPED papercut-log.js — the hook that lands in a
 * customer's ~/.claude/papercuts store, not the private harness's source copy.
 * These run against the packaged file at its installed path (tests/hooks/lib
 * harness resolves HOOKS_DIR to <repo>/hooks) so a packaging-only regression
 * (a sibling require, a path that only resolves relative to the vendored
 * layout) is caught here even when the source-side suite is green.
 *
 * Every test uses a temp HOME (and, where the store matters, an explicit
 * PAPERCUT_STORE override) — never the real ~/.claude/papercuts.
 */
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { runHook, assertPassThrough, mkTmp, rmTmp, HOOKS_DIR } = require('./lib/harness');

const HOOK = 'papercut-log.js';
const { signature, projectSlug, redact } = require('../../hooks/papercut-log.js');

const CWD = '/srv/app/demo';

function sandbox() {
  const home = mkTmp('papercut-home-');
  return { home, env: { HOME: home, USERPROFILE: home } };
}
const storeFor = (home, cwd) =>
  path.join(home, '.claude', 'papercuts', `${projectSlug(cwd)}.jsonl`);

function readRecords(home, cwd) {
  const f = storeFor(home, cwd);
  if (!fs.existsSync(f)) return [];
  return fs.readFileSync(f, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
}

const failure = (over = {}) => ({
  hook_event_name: 'PostToolUseFailure',
  session_id: 'abcdefgh12345678',
  cwd: CWD,
  tool_name: 'Bash',
  tool_input: { command: 'pytest -q' },
  error: 'Exit code 127\n/bin/bash: line 1: pytest: command not found',
  duration_ms: 42,
  ...over,
});

// --- 1. capture ----------------------------------------------------------

test('capture: a hard tool failure produces a record with the expected shape', async () => {
  const { home, env } = sandbox();
  try {
    const p = failure();
    const res = await runHook(HOOK, p, { env });
    assert.strictEqual(res.code, 0, `hook crashed: ${res.stderr}`);
    const recs = readRecords(home, p.cwd);
    assert.strictEqual(recs.length, 1);
    assert.strictEqual(recs[0].sig, 'command_not_found:pytest');
    assert.strictEqual(recs[0].source, 'auto');
    assert.strictEqual(recs[0].tool, 'Bash');
    assert.strictEqual(recs[0].session, '12345678', 'session is stored short (last 8)');
    assert.strictEqual(recs[0].cwd, p.cwd);
    assert.match(recs[0].ts, /^\d{4}-\d{2}-\d{2}T/, 'ts is an ISO timestamp');
    // Always a logger, never a gate: stdin passes through unchanged.
    assert.strictEqual(res.stdout, JSON.stringify(p));
  } finally { rmTmp(home); }
});

// --- 2. redaction ----------------------------------------------------------

test('redaction: redact() strips a Stripe live key and a bearer token', () => {
  const cases = [
    ['stripe error: using key sk_live_51H8xAbCdEfGhIjKlMnOp', 'sk_live_51H8xAbCdEfGhIjKlMnOp'],
    ['curl -H "Authorization: Bearer sk-livetoken1234567890" https://api.x', 'sk-livetoken1234567890'],
  ];
  for (const [input, secret] of cases) {
    const out = redact(input);
    assert.ok(!out.includes(secret), `secret survived redact(): ${input} -> ${out}`);
    assert.match(out, /<redacted>/);
  }
});

test('redaction: a Stripe key and a bearer token in a failure payload never reach the store', async () => {
  const { home, env } = sandbox();
  try {
    const p = failure({
      tool_input: { command: 'curl -H "Authorization: Bearer sk-livetoken1234567890" https://api.x' },
      error: 'stripe error: using key sk_live_51H8xAbCdEfGhIjKlMnOp; curl: (22) HTTP 401',
    });
    await runHook(HOOK, p, { env });
    const raw = fs.readFileSync(storeFor(home, p.cwd), 'utf8');
    assert.ok(!raw.includes('sk-livetoken1234567890'), 'bearer token leaked into the store');
    assert.ok(!raw.includes('sk_live_51H8xAbCdEfGhIjKlMnOp'), 'Stripe live key leaked into the store');
    assert.match(raw, /<redacted>/);
  } finally { rmTmp(home); }
});

// --- 3. store path: the hook and the CLI agree ----------------------------

test('store path: the CLI reads back the record the hook just wrote to PAPERCUT_STORE', () => {
  // Binds the two sides by the hook's OWN computed signature, so a change to
  // signature() cannot pass this vacuously.
  const { home, env } = sandbox();
  const store = mkTmp('papercut-parity-');
  try {
    const p = failure();
    const expected = signature(p.error, p.tool_name);

    const hook = spawnSync(process.execPath, [path.join(HOOKS_DIR, HOOK)], {
      input: JSON.stringify(p),
      env: { ...process.env, ...env, PAPERCUT_STORE: store },
      encoding: 'utf8',
    });
    assert.strictEqual(hook.status, 0, `hook failed: ${hook.stderr}`);

    const cli = path.join(HOOKS_DIR, '..', 'papercut', 'cli.py');
    assert.ok(fs.existsSync(cli), `expected the packaged CLI at ${cli}`);
    const read = spawnSync('python3', [cli, 'list', '--days', '30'], {
      env: { ...process.env, ...env, PAPERCUT_STORE: store },
      encoding: 'utf8',
    });
    assert.strictEqual(read.status, 0, `CLI failed: ${read.stderr}`);
    assert.ok(
      read.stdout.includes(expected),
      `the CLI must read back the hook's record from the same store; ` +
      `wanted signature ${expected} in:\n${read.stdout}`,
    );

    // The default store must be untouched — a pass cannot come from both
    // sides quietly agreeing on ~/.claude/papercuts instead of the override.
    assert.ok(!fs.existsSync(path.join(home, '.claude', 'papercuts')),
      'nothing may be written to the default store while PAPERCUT_STORE is set');
  } finally { rmTmp(home); rmTmp(store); }
});

// --- 4. no false capture ---------------------------------------------------

test('no false capture: an ordinary tool call with no error carries nothing to write', async () => {
  const { home, env } = sandbox();
  try {
    // What a genuinely successful tool completion looks like: no `error`
    // field and no failing tool_response. papercut-log.js is wired only to
    // the PostToolUseFailure event (see hooks/hooks.json), but the module
    // itself must also refuse to write when there is nothing to say.
    const ok = {
      hook_event_name: 'PostToolUse',
      session_id: 'abcdefgh12345678',
      cwd: CWD,
      tool_name: 'Bash',
      tool_input: { command: 'ls' },
    };
    assertPassThrough(await runHook(HOOK, ok, { env }), ok);
    assert.strictEqual(readRecords(home, CWD).length, 0, 'a non-failure call must write nothing');
  } finally { rmTmp(home); }
});

test('no false capture: a Bash failure with only the exit-code wrapper is not recorded', async () => {
  // Control-flow exits (cd/pkill/ssh/find/timeout) carry no information and
  // are overwhelmingly not friction.
  const { home, env } = sandbox();
  try {
    for (const err of ['Exit code 1', 'Exit code 143\n', 'Exit code 123']) {
      await runHook(HOOK, failure({ tool_input: { command: 'pkill -f something' }, error: err }), { env });
    }
    assert.strictEqual(readRecords(home, CWD).length, 0);
  } finally { rmTmp(home); }
});

test('confirms hooks.json wires papercut-log.js only to PostToolUseFailure', () => {
  // Guards the premise of the two tests above: if this were ever also wired
  // to a success event, "no false capture" would have to hold for real
  // traffic, not just for a hand-built payload.
  const hooksJson = JSON.parse(fs.readFileSync(path.join(HOOKS_DIR, 'hooks.json'), 'utf8'));
  const events = Object.keys(hooksJson.hooks).filter((event) =>
    JSON.stringify(hooksJson.hooks[event]).includes('papercut-log.js'));
  assert.deepStrictEqual(events, ['PostToolUseFailure']);
});
