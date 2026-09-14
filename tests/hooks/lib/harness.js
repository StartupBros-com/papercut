'use strict';
/**
 * Tiny, zero-dependency test harness for the SHIPPED hooks (node:test only —
 * no npm install, no devDependencies; the package stays installable with zero
 * dependencies).
 *
 * Hooks are exercised BLACK-BOX: spawn `node <hook>`, feed a JSON payload on
 * stdin, and assert on {exit code, stdout, stderr} — exactly how the packaged
 * hooks.json invokes them. This deliberately runs the hooks at their SHIPPED
 * path (repo-root/hooks/*.js), not a source copy, so a packaging-only
 * regression (e.g. a sibling require that only resolves relative to the
 * installed layout) is caught here even though the private harness's own
 * source-side suite has no way to see it.
 *
 * Hermeticity: every spawn gets its own throwaway HOME/USERPROFILE (the store
 * hooks write under $HOME honors os.homedir(), which reads $HOME on POSIX and
 * USERPROFILE on Windows) and PAPERCUT_STORE/CLAUDE_CONFIG_DIR are cleared
 * before each spawn so a developer's real store is never touched. A test that
 * needs a specific store passes its own env override, which wins.
 */

const { spawn } = require('node:child_process');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

// tests/hooks/lib/harness.js  ->  ../../../hooks = the shipped hooks directory
const HOOKS_DIR = path.resolve(__dirname, '..', '..', '..', 'hooks');

// Env vars that change where a hook writes; cleared before each spawn so a
// developer's real environment can never leak into a test run.
const NEUTRALIZE = ['PAPERCUT_STORE', 'CLAUDE_CONFIG_DIR'];

function buildEnv(overrides = {}) {
  const env = { ...process.env };
  for (const k of NEUTRALIZE) delete env[k];
  const home = mkTmp('papercut-hooktest-home-');
  env.HOME = home;
  env.USERPROFILE = home;
  return { ...env, ...overrides };
}

// Matches the way a dangling require / missing sibling module surfaces at
// runtime — used to assert a hook did NOT crash rather than merely that it
// produced some exit code.
const MODULE_ERROR_RE = /MODULE_NOT_FOUND|Cannot find module|ModuleNotFoundError|ImportError|No module named/;

/**
 * Spawn `node <script> [argv...]`, feed `stdin`, resolve with the result.
 * Never rejects — a spawn error resolves as {code:-1, stderr}.
 */
function spawnNode(scriptRel, { argv = [], stdin = '', env = {}, cwd } = {}) {
  const scriptAbs = path.isAbsolute(scriptRel) ? scriptRel : path.join(HOOKS_DIR, scriptRel);
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [scriptAbs, ...argv], {
      cwd: cwd || HOOKS_DIR,
      env: buildEnv(env),
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => { stdout += d; });
    child.stderr.on('data', (d) => { stderr += d; });
    child.on('close', (code) => resolve({ code, stdout, stderr }));
    child.on('error', (e) => resolve({ code: -1, stdout, stderr: String((e && e.stack) || e) }));
    child.stdin.end(stdin);
  });
}

/** Run a hook directly, feeding a JSON payload (object -> stringified, or raw string). */
function runHook(scriptRel, payload, opts = {}) {
  const stdin = typeof payload === 'string' ? payload : JSON.stringify(payload);
  return spawnNode(scriptRel, { ...opts, stdin });
}

/**
 * Assert a hook ALLOWED the action by passing input through unchanged: exit 0
 * AND stdout === the exact raw input. Not the same as "no decision" — a
 * crashed hook also has no decision, so this makes a crash fail loudly
 * instead of reading as an allow.
 */
function assertPassThrough(res, payload) {
  const expected = typeof payload === 'string' ? payload : JSON.stringify(payload);
  assert.strictEqual(res.code, 0, `expected exit 0, got ${res.code}; stderr: ${res.stderr}`);
  assert.strictEqual(res.stdout, expected, 'expected raw input to pass through unchanged');
}

/**
 * Assert a PreToolUse hook blocked via the current schema
 * (hookSpecificOutput.permissionDecision === 'deny').
 */
function assertPreToolUseDeny(res) {
  assert.strictEqual(res.code, 0, `expected exit 0, got ${res.code}; stderr: ${res.stderr}`);
  const out = JSON.parse(res.stdout);
  assert.ok(out.hookSpecificOutput, 'expected hookSpecificOutput (current PreToolUse schema)');
  assert.strictEqual(out.hookSpecificOutput.hookEventName, 'PreToolUse');
  assert.strictEqual(out.hookSpecificOutput.permissionDecision, 'deny');
  assert.ok(out.hookSpecificOutput.permissionDecisionReason, 'expected permissionDecisionReason');
}

function mkTmp(prefix = 'hooktest-') {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}
function rmTmp(dir) {
  try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* ignore */ }
}

module.exports = {
  HOOKS_DIR,
  spawnNode,
  runHook,
  MODULE_ERROR_RE,
  assertPassThrough,
  assertPreToolUseDeny,
  mkTmp,
  rmTmp,
  buildEnv,
};
