#!/usr/bin/env node
/**
 * PreToolUse Hook: StructuredOutput unwrap
 *
 * Subagents spawned with a `schema` return their result by calling the
 * StructuredOutput tool, whose parameters ARE the schema. Measured 2026-09-01
 * over 109 schema-mismatch failures paired with the tool_use that caused them:
 * the largest class wrapped the whole object under one top-level `input` key
 * -- `{"input": {refuted, reasoning}}` -- so the validator reported every
 * required property missing, the model read "add them", and regenerated the
 * same wrapper: 16 retries in one session, every one a silent full
 * regeneration.
 *
 * This hook rewrites exactly that shape, and only as the retry of a
 * DEMONSTRATED wrapped failure in the same lineage: the tool_input must be a
 * single `input` key holding an object, and the most recent StructuredOutput
 * attempt in the agent's own transcript must (a) have been that same wrapped
 * shape with the same inner keys and (b) have failed with a schema mismatch
 * whose missing properties all sit inside the wrapper. Binding to the
 * preceding attempt -- not to "some mismatch earlier in the transcript" -- is
 * what keeps a schema whose only top-level property legitimately IS `input`
 * safe: its calls validate, so the most recent attempt is a success and
 * nothing is rewritten; and an unrelated schema's older failure can never
 * seed a rewrite because it is not the most recent attempt. Should a rewrite
 * ever be wrong, the next mismatch names `input` itself, which is not inside
 * the wrapper, so the hook stops after one attempt.
 *
 * Fail-open everywhere: any parse, read or shape surprise passes the call
 * through unchanged. Mirrors settings-guard.js: exports run(rawInput).
 */

'use strict';

const fs = require('fs');
const path = require('path');

const SCHEMA_MISMATCH = /output does not match required schema/i;
const MISSING_PROP = /must have required property '([^']+)'/g;
const TAIL_BYTES = 1024 * 1024;

function loneInputObject(toolInput) {
  if (!toolInput || typeof toolInput !== 'object' || Array.isArray(toolInput)) return null;
  const keys = Object.keys(toolInput);
  if (keys.length !== 1 || keys[0] !== 'input') return null;
  const inner = toolInput.input;
  if (!inner || typeof inner !== 'object' || Array.isArray(inner)) return null;
  return inner;
}

function resultText(block) {
  let text = block.content;
  if (Array.isArray(text)) text = text.map((c) => (c && c.text) || '').join(' ');
  return String(text || '');
}

/**
 * The most recent StructuredOutput attempt in a transcript tail that has a
 * result: { input, text } or null. "Most recent" is by result order, so a
 * later successful attempt hides any older mismatch.
 */
function lastAttempt(transcriptPath) {
  if (typeof transcriptPath !== 'string' || !transcriptPath) return null;
  let fd;
  try {
    fd = fs.openSync(transcriptPath, 'r');
    const st = fs.fstatSync(fd);
    if (!st.isFile()) return null;
    const len = Math.min(st.size, TAIL_BYTES);
    const buf = Buffer.alloc(len);
    fs.readSync(fd, buf, 0, len, st.size - len);
    const uses = new Map();
    let last = null;
    for (const line of buf.toString('utf8').split('\n')) {
      if (!line.includes('StructuredOutput') && !line.includes('tool_result')) continue;
      let rec;
      try { rec = JSON.parse(line); } catch { continue; }
      const content = rec && rec.message && rec.message.content;
      if (!Array.isArray(content)) continue;
      for (const block of content) {
        if (!block) continue;
        if (block.type === 'tool_use' && block.name === 'StructuredOutput') {
          uses.set(block.id, block.input);
        } else if (block.type === 'tool_result' && uses.has(block.tool_use_id)) {
          last = { input: uses.get(block.tool_use_id), text: resultText(block) };
        }
      }
    }
    return last;
  } catch {
    return null;
  } finally {
    if (fd !== undefined) { try { fs.closeSync(fd); } catch { /* ignore */ } }
  }
}

/** The agent's own transcript: the hook's transcript_path, else agent-<id>.jsonl under its subagents tree. */
function candidateTranscripts(input) {
  const out = [];
  if (typeof input.transcript_path === 'string' && input.transcript_path) out.push(input.transcript_path);
  const agentId = typeof input.agent_id === 'string' ? input.agent_id : '';
  if (agentId && typeof input.transcript_path === 'string' && input.transcript_path) {
    // <session>.jsonl sits beside <session>/subagents/...: Task-tool agents write
    // subagents/agent-<id>.jsonl, Workflow agents subagents/workflows/<wf>/agent-<id>.jsonl.
    // The walk is depth-unbounded, capped only by the match count.
    const sessionDir = input.transcript_path.replace(/\.jsonl$/, '');
    const sub = path.join(sessionDir, 'subagents');
    try {
      const stack = [sub];
      while (stack.length && out.length < 8) {
        const dir = stack.pop();
        for (const name of fs.readdirSync(dir)) {
          const full = path.join(dir, name);
          if (name === `agent-${agentId}.jsonl`) out.push(full);
          else if (!name.includes('.')) stack.push(full);
        }
      }
    } catch { /* no subagent tree: fine */ }
  }
  return out;
}

// Documented PreToolUse shape (hooks reference, "Decision control"): updatedInput
// nests in hookSpecificOutput and REPLACES tool_input wholesale; with no
// permissionDecision the modified input flows through the normal permission
// evaluation, which is what a tool that never prompts wants. additionalContext
// is a PostToolUse/PostToolUseFailure field, not a PreToolUse one -- an unknown
// field can fail the output's schema validation and void the rewrite -- so the
// explanation rides the reason field, which every decision accepts
// (an-admission-guard.js is the house precedent for updatedInput alone).
function rewrite(updatedInput, note) {
  return JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecisionReason: note,
      updatedInput,
    },
  });
}

const sortedKeys = (o) => Object.keys(o).sort().join(' ');

/**
 * @param {string} rawInput - JSON string from Claude Code stdin
 * @returns {string} JSON response or pass-through
 */
function run(rawInput) {
  try {
    const input = JSON.parse(rawInput);
    if (input.tool_name !== 'StructuredOutput') return rawInput;
    const inner = loneInputObject(input.tool_input);
    if (!inner) return rawInput;

    let prior = null;
    for (const t of candidateTranscripts(input)) {
      prior = lastAttempt(t);
      if (prior) break;
    }
    if (!prior || !SCHEMA_MISMATCH.test(prior.text)) return rawInput;
    // Same lineage: the attempt that just failed was this exact wrapped shape.
    const priorInner = loneInputObject(prior.input);
    if (!priorInner || sortedKeys(priorInner) !== sortedKeys(inner)) return rawInput;
    const missing = [];
    let m;
    MISSING_PROP.lastIndex = 0;
    while ((m = MISSING_PROP.exec(prior.text)) !== null) missing.push(m[1]);
    if (missing.length === 0) return rawInput;
    const innerKeys = Object.keys(inner);
    if (!missing.every((k) => innerKeys.includes(k))) return rawInput;

    return rewrite(inner,
      'StructuredOutput call unwrapped: the previous attempt sent the same object nested under a '
      + 'top-level `input` key and failed naming ' + missing.slice(0, 8).join(', ')
      + '; the tool\'s parameters are the object itself.');
  } catch {
    // Invalid JSON or unexpected shape -- pass through (fail-open).
  }
  return rawInput;
}

if (require.main === module) {
  let data = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (chunk) => { if (data.length < 1024 * 1024) data += chunk; });
  process.stdin.on('end', () => { process.stdout.write(run(data)); process.exit(0); });
}

module.exports = { run, lastAttempt };
