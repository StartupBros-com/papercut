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
const { signature, projectSlug, redact, redactThenTrim } = require('../../hooks/papercut-log.js');

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

test('redaction: the bounded URI-credential rule still redacts (pins the {1,64} bound)', () => {
  // The bound replaced an unbounded `+` to kill quadratic backtracking. The
  // claim at the patch site is that bounding "cannot lose a match". Nothing in
  // this suite pinned it: reverting both bounds in the shipped hook left all
  // tests green, because the only credential URI exercised used a short scheme
  // that matches either way. These cases fail if the bound is removed OR
  // tightened past a realistic scheme.
  const cases = [
    'postgres://admin:s3cr3tp4ssw0rd@db.internal:5432/app',
    'https://user:hunter2hunter2@example.com/path',
    'git+ssh://deploy:tok3nv4lue123@git.example.com/repo.git',
  ];
  for (const input of cases) {
    const out = redact(input);
    assert.match(out, /<redacted>@/, `URI credential survived: ${input} -> ${out}`);
  }
});

test('redaction: the bounded credential-name rule still redacts (pins the {0,64} bound)', () => {
  // This pattern had NO coverage at all, and it is the one whose prefix bound
  // changed from `*` to {0,64}. The long-prefix case is the one a too-tight
  // bound would break.
  const cases = [
    ['PGPASSWORD=supersecretvalue', 'supersecretvalue'],
    ['GITHUB_TOKEN: ghp_abcdefghijklmnop', 'ghp_abcdefghijklmnop'],
    ['MY_SERVICE_API_KEY="k3yv4lu3abcdef"', 'k3yv4lu3abcdef'],
    // a prefix right at the bound's edge
    ['A'.repeat(60) + '_SECRET=abcdefghijkl', 'abcdefghijkl'],
  ];
  for (const [input, secret] of cases) {
    const out = redact(input);
    assert.ok(!out.includes(secret),
      `credential survived redact(): ${input} -> ${out}`);
  }
});

test('redaction: a long non-matching body stays linear (pins the ReDoS fix)', () => {
  // The defect the bounds exist for: an unbounded quantifier in front of an
  // alternation is retried at every start position, which is quadratic. One
  // rule took 40.2s on a 40k-character run of word characters. This asserts a
  // generous ceiling -- it is a ReDoS tripwire, not a benchmark.
  // Tuned against measured values rather than guessed. On this engine a 100k
  // run of word characters costs ~16ms with the bounds and ~6600ms without --
  // a 400x separation. A first attempt used a 40k body and a 2000ms ceiling,
  // which the UNBOUNDED pattern passed at ~1030ms: the tripwire asserted
  // nothing. 500ms keeps ~30x headroom for a slow CI box while still failing
  // an unbounded quantifier by better than 10x.
  const body = 'a'.repeat(100000);
  const started = process.hrtime.bigint();
  redact(body);
  const elapsedMs = Number(process.hrtime.bigint() - started) / 1e6;
  assert.ok(elapsedMs < 500,
    `redact() took ${elapsedMs.toFixed(0)}ms on a 100k body -- a quantifier bound was lost`);
});

test('redaction: a credential straddling MAX_INPUT_CHARS is not written unredacted', () => {
  // Slicing before redacting let a key lose enough trailing characters to fall
  // under a pattern's minimum length and pass through intact. Measured: an
  // AKIA-shaped key with 4-11 characters left after the cut survived.
  const key = 'AKIAIOSFODNN7EXAMPLE';
  for (const keep of [4, 6, 10, 11, 14, 20]) {
    const pad = '. '.repeat(1200).slice(0, 2000 - keep);
    const out = redactThenTrim(pad + key + ' trailing');
    assert.ok(!/AKIA[A-Z0-9]{4,}/.test(out),
      `a straddling credential survived with ${keep} chars kept: ...${out.slice(-40)}`);
  }
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
