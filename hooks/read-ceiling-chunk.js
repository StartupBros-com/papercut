#!/usr/bin/env node
/**
 * PreToolUse Hook: Read ceiling guard
 *
 * A Read past the token ceiling fails with advice ("use offset and limit"),
 * and agents retry anyway: measured 2026-09-02 over one day, 442 of 535
 * ceiling failures were repeats of depth three or more, one agent re-reading
 * the same review diff 104 times. The advisory hint that fires from attempt
 * three (papercut-log.js) was live for all of it.
 *
 * What the retries look like: a survey of 1,176 failing Reads inside repeat
 * loops (2026-08-31 to 09-02) found NONE without a limit and 88% at the
 * default 2000-line page or larger. Agents always send a window; they send
 * one too big, again. The 104-deep loop asked for the default page 15 times,
 * 20000 lines 9 times, and 3000-6000-line windows for the rest, while its
 * successful reads of the same diff used 1000.
 *
 * This hook DENIES a retry that would fail again, and the denial names the
 * window that fits: half the smallest window that already failed for this
 * caller and file (a record without a window failed at the default page),
 * halving again per repeat as a bound. The agent's next call at or under
 * that window passes through untouched. Denying rather than rewriting is
 * deliberate: a limited Read returns numbered lines and nothing else, so a
 * silently clamped window hands the model a partial file it cannot tell is
 * partial, while a denial's reason is shown to the model and its own next
 * call sees real line numbers. Once halving would pass the floor no window
 * fits (a 13-line file with one 827 KB line failed at limit 7, seen live),
 * and the denial says so and points at Grep or a Bash extract.
 *
 * The caller is (session, agent): the same 8-hex session tail and agent tail
 * the capture hook writes. A subagent's failure never guards a sibling's
 * genuine first attempt, and a session id that is not a UUID tail (a harness
 * suite's literal `test`) is never used as an identity. The first attempt is
 * never touched: its error is what teaches the agent the ceiling exists.
 *
 * State is the store the capture hook appends to on every failure, so no new
 * file is written; fail-open on any surprise.
 */

'use strict';

const fs = require('fs');
const path = require('path');
const { storeDir, projectSlug, logDenial } = require('./papercut-log.js');

const CEILING_SIG = /^read:file content .*exceeds maximum allowed/;
const REAL_SESSION = /^[0-9a-f]{8}$/;
// Bytes from the end of the store, not a line count: under a 30-agent fleet a
// 400-line tail rolls off in seconds; 2 MiB is minutes of the busiest store.
const SCAN_BYTES = 2 * 1024 * 1024;
// Claude Code's Read returns this many lines when no limit is given, so a
// failure recorded without a window failed at this window.
const DEFAULT_PAGE = 2000;
const MIN_LIMIT = 50;
// Mirrors the capture side's cap on the stored target (papercut-log.js).
const TARGET_CHARS = 200;

function windowOf(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : DEFAULT_PAGE;
}

/**
 * Ceiling failures already recorded for this caller + file in the store's
 * recent bytes: how many, and the smallest window (limit) among them.
 */
function ceilingFailures(storeFile, session, agent, target) {
  let fd;
  try {
    fd = fs.openSync(storeFile, 'r');
    const st = fs.fstatSync(fd);
    if (!st.isFile()) return { count: 0, minLimit: DEFAULT_PAGE };
    const len = Math.min(st.size, SCAN_BYTES);
    const buf = Buffer.alloc(len);
    fs.readSync(fd, buf, 0, len, st.size - len);
    let count = 0;
    let minLimit = DEFAULT_PAGE;
    for (const line of buf.toString('utf8').split('\n')) {
      if (!line || !line.includes(session)) continue;
      try {
        const r = JSON.parse(line);
        if (r.session === session && String(r.agent || '') === agent
            && r.target === target && CEILING_SIG.test(String(r.sig || ''))) {
          count += 1;
          const w = windowOf(r.page && r.page.limit);
          if (w < minLimit) minLimit = w;
        }
      } catch { /* skip unparsable or partial line */ }
    }
    return { count, minLimit };
  } catch {
    return { count: 0, minLimit: DEFAULT_PAGE };
  } finally {
    if (fd !== undefined) { try { fs.closeSync(fd); } catch { /* ignore */ } }
  }
}

/** The window that fits after these failures: half the smallest failed window, halving again per repeat. */
function rungFor(count, minLimit) {
  const evidence = Math.floor(minLimit / 2);
  const bound = Math.floor(DEFAULT_PAGE / Math.pow(2, count));
  return Math.min(evidence, bound);
}

/**
 * A PreToolUse denial reaches no post-hook (probed live: neither PostToolUse,
 * PostToolUseFailure nor PermissionRequest dispatches), so a guard that wants
 * to be counted logs itself -- same convention as every other guard here,
 * under guard_blocked:read-ceiling-chunk, a signature the ceiling scan above
 * never matches. Wrapped so a logging failure never changes the decision.
 */
function deny(reason, input, ti) {
  try {
    logDenial({
      guard: 'read-ceiling-chunk', tool: 'Read', command: ti.file_path,
      reason, sessionId: input.session_id, cwd: input.cwd,
    });
  } catch { /* counting is optional; the decision is not */ }
  return JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: 'deny',
      permissionDecisionReason: reason,
    },
  });
}

/**
 * @param {string} rawInput - JSON string from Claude Code stdin
 * @returns {string} JSON response or pass-through
 */
function run(rawInput) {
  try {
    const input = JSON.parse(rawInput);
    if (input.tool_name !== 'Read') return rawInput;
    const ti = input.tool_input;
    if (!ti || typeof ti !== 'object' || Array.isArray(ti)) return rawInput;
    if (typeof ti.file_path !== 'string' || !ti.file_path) return rawInput;
    const target = ti.file_path.slice(0, TARGET_CHARS);

    const session = String(input.session_id || '').slice(-8);
    if (!REAL_SESSION.test(session)) return rawInput;
    const agent = String(input.agent_id || '').slice(-8);
    const storeFile = path.join(storeDir(), `${projectSlug(input.cwd)}.jsonl`);
    const { count, minLimit } = ceilingFailures(storeFile, session, agent, target);
    if (count < 1) return rawInput;

    const limit = rungFor(count, minLimit);
    const asked = ti.limit === undefined || ti.limit === null ? DEFAULT_PAGE : windowOf(ti.limit);
    // Finite only: JSON `1e400` parses to Infinity and would name an
    // unfollowable offset (refute-vet finding, 2026-09-02).
    const off = Number(ti.offset);
    const offset = ti.offset !== undefined && ti.offset !== null && Number.isFinite(off) && off > 0
      ? Math.floor(off) : 0;
    const history = 'This file already failed the token ceiling ' + count + ' time(s) for this caller '
      + 'in this session; the smallest window that failed was ' + minLimit + ' lines';
    if (limit < MIN_LIMIT) {
      // No window fits: the lines themselves blow the ceiling.
      return deny('Read of ' + ti.file_path + ' stopped before running. ' + history
        + ', so no Read window fits this file (its lines are too long). Use Grep for the part you '
        + 'need, or a Bash extract such as `head -c 20000 <file>` or `jq`/`python3` for JSON.', input, ti);
    }
    // At or under the window that fits: the agent is converging on its own.
    if (asked <= limit) return rawInput;

    return deny('Read of ' + ti.file_path + ' stopped before running. ' + history
      + ', and this call asks for ' + asked + ' lines, which would fail again. Call Read with '
      + 'offset ' + offset + ' and limit ' + limit + ', then continue from offset '
      + (offset + limit) + '.', input, ti);
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

module.exports = { run, ceilingFailures, rungFor };
