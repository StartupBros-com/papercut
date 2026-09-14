'use strict';
/**
 * Behavior tests over the SHIPPED structured-output-unwrap.js — its
 * corrective rewrite of a wrapped StructuredOutput retry.
 */
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { runHook, assertPassThrough, mkTmp, rmTmp } = require('./lib/harness');

const HOOK = 'structured-output-unwrap.js';
const MISMATCH = "Output does not match required schema: root: must have required property 'refuted', root: must have required property 'reasoning'";

function transcriptWith(dir, lines, name = 'session.jsonl') {
  const p = path.join(dir, name);
  fs.writeFileSync(p, lines.map((l) => JSON.stringify(l)).join('\n') + '\n');
  return p;
}
const use = (id, input) => ({
  type: 'assistant',
  message: { role: 'assistant', content: [{ type: 'tool_use', id, name: 'StructuredOutput', input }] },
});
const result = (id, text) => ({
  type: 'user',
  message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: id, content: text }] },
});
const WRAPPED = { input: { refuted: false, reasoning: 'holds' } };
const payload = (over) => ({
  hook_event_name: 'PreToolUse',
  tool_name: 'StructuredOutput',
  tool_input: { input: { refuted: false, reasoning: 'holds, retried' } },
  session_id: 's',
  cwd: '/tmp',
  ...over,
});

// --- 5. structured-output-unwrap: corrective behavior ----------------------

test('the retry of a wrapped attempt that failed on its inner keys is unwrapped', async () => {
  const dir = mkTmp('so-unwrap-');
  try {
    const t = transcriptWith(dir, [use('t1', WRAPPED), result('t1', MISMATCH)]);
    const res = await runHook(HOOK, payload({ transcript_path: t }));
    assert.strictEqual(res.code, 0, `hook crashed: ${res.stderr}`);
    const out = JSON.parse(res.stdout);
    assert.strictEqual(out.hookSpecificOutput.hookEventName, 'PreToolUse');
    assert.deepStrictEqual(out.hookSpecificOutput.updatedInput, { refuted: false, reasoning: 'holds, retried' });
    assert.match(out.hookSpecificOutput.permissionDecisionReason, /unwrapped/);
    assert.strictEqual('permissionDecision' in out.hookSpecificOutput, false,
      'no auto-approve: the modified input still flows through normal permission evaluation');
  } finally { rmTmp(dir); }
});

test('a wrapped call with no prior attempt passes through untouched', async () => {
  // A schema whose only top-level property is `input` looks exactly like a
  // wrapper on its first call; without a demonstrated failure the hook must
  // not guess.
  const dir = mkTmp('so-unwrap-');
  try {
    const t = transcriptWith(dir, [{ type: 'user', message: { role: 'user', content: 'hello' } }]);
    const p = payload({ transcript_path: t });
    assertPassThrough(await runHook(HOOK, p), p);
  } finally { rmTmp(dir); }
});

test('a missing or malformed transcript fails open', async () => {
  const dir = mkTmp('so-unwrap-');
  try {
    const p1 = payload({ transcript_path: path.join(dir, 'nope.jsonl') });
    assertPassThrough(await runHook(HOOK, p1), p1);
    const bad = path.join(dir, 'bad.jsonl');
    fs.writeFileSync(bad, '{not json StructuredOutput tool_result does not match required schema\n');
    const p2 = payload({ transcript_path: bad });
    assertPassThrough(await runHook(HOOK, p2), p2);
  } finally { rmTmp(dir); }
});

test('other tools are never touched', async () => {
  const dir = mkTmp('so-unwrap-');
  try {
    const t = transcriptWith(dir, [use('t1', WRAPPED), result('t1', MISMATCH)]);
    const bash = payload({ transcript_path: t, tool_name: 'Bash', tool_input: { command: 'ls' } });
    assertPassThrough(await runHook(HOOK, bash), bash);
  } finally { rmTmp(dir); }
});
