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
 * window that fits: the smallest of half the smallest window that already
 * failed for this caller and file (a record without a window failed at the
 * default page), the per-repeat halving bound, and a prediction from the
 * failure's own token count (tokens per line of the failed window, with
 * headroom; 500 lines of a plan document were 26,689 tokens, seen live).
 * The agent's next call at or under
 * that window passes through untouched, and so does a call for the tail of
 * a file whose remaining lines fit: the guard counts the file's lines
 * (regular files up to 8 MiB) and compares what is left from the offset,
 * not what was asked; each recorded failure's window is clamped the same
 * way before it feeds the halving and the prediction. The denial spells
 * out the literal Read calls that cover the remainder (three agents once
 * answered "limit 475" with 4750) and, from the third denial for the same
 * file, says which denial this is. Denying rather than rewriting is
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
// Fraction of the cap a predicted window aims for; lines are not uniform.
const FIT_HEADROOM = 0.8;
// Mirrors the capture side's cap on the stored target (papercut-log.js).
const TARGET_CHARS = 200;
// Files up to this size get their lines counted so a call for the TAIL of a
// file is never denied for a window larger than what is left. Seen live
// 2026-09-02: from offset 400 of a 588-line file only 188 lines remained,
// which fit, and asking 1000 was denied five times.
const COUNT_LINES_UP_TO = 8 * 1024 * 1024;
// Literal calls listed in a denial (the rest is "then continue").
const MAX_CHUNKS_LISTED = 4;
const DENY_SIG = 'guard_blocked:read-ceiling-chunk';

function windowOf(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : DEFAULT_PAGE;
}

/** Line count of a regular file up to COUNT_LINES_UP_TO bytes, else null. */
function fileLines(filePath) {
  try {
    const st = fs.statSync(filePath);
    if (!st.isFile() || st.size > COUNT_LINES_UP_TO) return null;
    if (st.size === 0) return 0;
    const buf = fs.readFileSync(filePath);
    let n = 0;
    for (let i = 0; i < buf.length; i += 1) if (buf[i] === 10) n += 1;
    if (buf[buf.length - 1] !== 10) n += 1;
    return n;
  } catch {
    return null;
  }
}

/**
 * Ceiling failures already recorded for this caller + file in the store's
 * recent bytes: how many, and the smallest window (limit) among them.
 */
function ceilingFailures(storeFile, session, agent, target, getLines = () => null) {
  let fd;
  try {
    fd = fs.openSync(storeFile, 'r');
    const st = fs.fstatSync(fd);
    if (!st.isFile()) return { count: 0, minLimit: DEFAULT_PAGE, fit: Infinity, denials: 0 };
    const len = Math.min(st.size, SCAN_BYTES);
    const buf = Buffer.alloc(len);
    fs.readSync(fd, buf, 0, len, st.size - len);
    let count = 0;
    // The smallest window that actually failed; a failure larger than the
    // default page is reported as what it was (seen live: a 2505-line
    // failure reported as 2000 because the tracker started at 2000).
    let minLimit = Infinity;
    let fit = Infinity;
    let denials = 0;
    for (const line of buf.toString('utf8').split('\n')) {
      if (!line || !line.includes(session)) continue;
      try {
        const r = JSON.parse(line);
        if (r.session === session && String(r.agent || '') === agent
            && r.sig === DENY_SIG && r.cmd === target) {
          denials += 1;
          continue;
        }
        if (r.session === session && String(r.agent || '') === agent
            && r.target === target && CEILING_SIG.test(String(r.sig || ''))) {
          // The window that actually ran is bounded by what the file had
          // left from that call's offset: a 588-line file that failed at an
          // assumed 2000-line window failed at 588 lines, and dividing its
          // tokens by 2000 predicts a window three times too wide. A record
          // from past the current end of the file predates a truncation and
          // says nothing about the file as it is now (refute-vet finding).
          let w = windowOf(r.page && r.page.limit);
          const lines = getLines();
          if (lines !== null) {
            const po = Number(r.page && r.page.offset);
            const from = Number.isFinite(po) && po > 0 ? Math.floor(po) : 1;
            if (from > lines) continue;
            w = Math.max(1, Math.min(w, lines - from + 1));
          }
          count += 1;
          if (w < minLimit) minLimit = w;
          // The error names the tokens that window produced and the cap:
          // tokens per line of the failed window predicts what fits, with
          // headroom for uneven lines. Seen live 2026-09-02: 500 lines of a
          // plan document were 26,689 tokens, so halving to 500 failed once
          // more where ~375 would have fit.
          const m = String(r.err || '').match(/\((\d+) tokens\) exceeds maximum allowed tokens \((\d+)\)/);
          if (m) {
            const est = Math.floor((FIT_HEADROOM * Number(m[2]) * w) / Number(m[1]));
            if (Number.isFinite(est) && est < fit) fit = est;
          }
        }
      } catch { /* skip unparsable or partial line */ }
    }
    if (!Number.isFinite(minLimit)) minLimit = DEFAULT_PAGE;
    return { count, minLimit, fit, denials };
  } catch {
    return { count: 0, minLimit: DEFAULT_PAGE, fit: Infinity, denials: 0 };
  } finally {
    if (fd !== undefined) { try { fs.closeSync(fd); } catch { /* ignore */ } }
  }
}

/** The window that fits after these failures: half the smallest failed window, halving again per repeat. */
function rungFor(count, minLimit, fit = Infinity) {
  const evidence = Math.floor(minLimit / 2);
  const bound = Math.floor(DEFAULT_PAGE / Math.pow(2, count));
  return Math.min(evidence, bound, Number.isFinite(fit) ? fit : Infinity);
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
      reason, sessionId: input.session_id, agentId: input.agent_id, cwd: input.cwd,
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
    // Counted at most once, and only when a recorded failure makes it matter:
    // this hook runs on every Read in every session.
    let counted = false;
    let lines = null;
    const getLines = () => {
      if (!counted) { counted = true; lines = fileLines(ti.file_path); }
      return lines;
    };
    const { count, minLimit, fit, denials } = ceilingFailures(storeFile, session, agent, target, getLines);
    if (count < 1) return rawInput;

    const limit = rungFor(count, minLimit, fit);
    const asked = ti.limit === undefined || ti.limit === null ? DEFAULT_PAGE : windowOf(ti.limit);
    // Finite only: JSON `1e400` parses to Infinity and would name an
    // unfollowable offset (refute-vet finding, 2026-09-02).
    const off = Number(ti.offset);
    const offset = ti.offset !== undefined && ti.offset !== null && Number.isFinite(off) && off > 0
      ? Math.floor(off) : 0;
    // Read's offset is a 1-based line number; 0 and 1 both mean the top.
    const start = Math.max(offset, 1);
    const remaining = lines === null ? null : Math.max(0, lines - start + 1);
    const history = 'this file already failed the token ceiling ' + count + ' time(s) for this caller '
      + 'in this session; the smallest window that failed was ' + minLimit + ' lines';
    // At or under the window that fits: the agent is converging on its own.
    // A call for the tail of the file counts what is left, not what it asked,
    // and that holds below the floor too: two remaining lines fit when the
    // window is 31 (refute-vet finding).
    const effective = remaining === null ? asked : Math.min(asked, remaining);
    if (effective <= limit) return rawInput;
    if (limit < MIN_LIMIT) {
      // No window fits: the lines themselves blow the ceiling.
      return deny('Read stopped before running: no Read window fits ' + ti.file_path
        + ' (its lines are too long). Use Grep for the part you need, or a Bash extract such as '
        + '`head -c 20000 <file>` or `jq`/`python3` for JSON. Why: ' + history + '.', input, ti);
    }

    const quoted = JSON.stringify(ti.file_path);
    const call = (o, l) => 'Read(file_path=' + quoted + ', offset=' + o + ', limit=' + l + ')';
    const chunks = [];
    let o = start;
    let left = remaining === null ? null : remaining;
    while (chunks.length < MAX_CHUNKS_LISTED && (left === null ? chunks.length < 1 : left > 0)) {
      const l = left === null ? limit : Math.min(limit, left);
      chunks.push(call(o, l));
      o += l;
      if (left !== null) left -= l;
    }
    const shape = lines === null
      ? ''
      : ' The file has ' + lines + ' lines and ' + remaining + ' remain from offset ' + start
        + '; one Read cannot return that many at this density.';
    // Keep the plain phrase (what the tests and the first cycle's agents saw)
    // and add the literal call: three agents answered "limit 475" with 4750.
    // One offset in the text: offset 0 and 1 both mean the top, and the
    // literal call says 1, so the prose says 1 too.
    const plan = ' Call Read with offset ' + start + ' and limit ' + limit + ', that is '
      + chunks.join(' then ')
      + (left !== null && left <= 0 ? ' (that covers the rest of the file).'
        : ', then continue from offset ' + (o) + '.');
    const repeat = denials >= 2
      ? ' This is denial ' + (denials + 1) + ' for this file: each call so far asked for more than the '
        + 'window; only a call at or under ' + limit + ' lines runs.'
      : '';
    // The numbers lead: the model reads them first, and the stored denial
    // record keeps only the first 400 characters (a long path used to push
    // the window past that cut -- refute-vet finding).
    return deny('Read stopped before running: this call asks for ' + asked + ' lines from offset ' + start
      + ' and the window that fits is ' + limit + '.' + repeat + ' File: ' + ti.file_path + '.' + shape + plan
      + ' Why: ' + history + '.', input, ti);
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
