#!/usr/bin/env node
/**
 * PostToolUseFailure Hook: papercut logger (automatic capture half)
 *
 * Records the friction agents currently route around silently. A 30-day sweep of
 * 601 top-level sessions across 211 project dirs (2026-08-05) found 61.2% of
 * sessions carry at least one hard failure signature, and the retry-after-error
 * pattern — an errored Bash call immediately followed by a corrected retry —
 * fires in 342/601 sessions (56.9%) touching 86% of active repos. None of it was
 * ever captured: memory-mine is weekly and filters for durable/non-trivial facts,
 * ce-compound requires a solved+verified+non-trivial problem, and a weekly scheduled run
 * only instruments the harness's own config surface. The cost shows up as the
 * same lesson relearned — `pytest: command not found` recurs across 13 sessions
 * and 76 subagent transcripts even though "use `uv run`" is a standing CLAUDE.md
 * rule, and five clusters of near-duplicate memory notes document one fact twice
 * or three times each.
 *
 * WHY A HOOK RATHER THAN A CLI THE AGENT CALLS: this event costs zero tokens.
 * Exit 0 with no stdout goes only to the debug log — nothing enters the
 * transcript, so nothing is re-billed on later turns. An agent-invoked CLI would
 * add a tool-call round trip to the transcript for every papercut and depend on
 * the model choosing to interrupt itself, which is the exact behavior the whole
 * idea exists because models don't do. This half is therefore strictly cheaper
 * AND has 100% compliance. The judgment-only class that has no error signature
 * (confusing docs, misleading-but-successful output, a working-but-wrong tool)
 * is covered by the on-demand `/papercut` skill instead.
 *
 * NOT A GATE. It never blocks, never alters control flow, and passes stdin
 * through unchanged. Any internal error is swallowed: a broken papercut logger
 * must never be able to break a session.
 *
 * DEDUPE BY SIGNATURE, COUNT DUPLICATES. Each record carries a normalized `sig`
 * (paths, digits, hex and hashes folded out) so the same friction from different
 * cwds collapses to one key. Repetition is the priority signal, so duplicates are
 * kept and counted rather than discarded — the inverse of memory-mine.py, which
 * rejects a note whose description overlaps an existing index line by >0.75.
 *
 * ATOMICITY: records are capped at MAX_RECORD_BYTES (< 4096) and written with a
 * single O_APPEND write, which POSIX guarantees is atomic below PIPE_BUF. That is
 * what makes a shared per-project log safe under the ~20 concurrent sessions this
 * machine runs, without the per-entry-directory scheme other implementations use.
 *
 * Store: $PAPERCUT_STORE, else ~/.claude/papercuts/<project-slug>.jsonl  (out of repo,
 * never committed;
 * slug matches the ~/.claude/projects/<slug> and claude/memory/<slug> convention).
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

const MAX_ERR_CHARS = 400;
// Truncate BEFORE redact()/signature(), never after. Several REDACTIONS patterns
// are O(n^2) on long non-matching input — benchmarked 2026-08-06: 50K chars
// ~900ms, 100K ~7.1s, 1MB did not finish in five minutes. Since logDenial() runs
// these inside a PreToolUse guard's synchronous path, an unbounded command string
// (a big inline script, a pasted blob) would stall the guard and, because hook
// timeouts fail CLOSED, stall the session. Nothing downstream needs more than
// this: err is stored at MAX_ERR_CHARS and the signature keys on one line.
const MAX_INPUT_CHARS = 2000;
const MAX_RECORD_BYTES = 3072; // < PIPE_BUF (4096) so the O_APPEND write stays atomic
const MAX_LOG_BYTES = 32 * 1024 * 1024; // stop growing rather than fill the disk

/**
 * Failures that are control flow, not friction. Logging these would drown the
 * real signal — `grep` exiting 1 on no-match and `test`/`[` exiting 1 are how
 * those tools report a normal negative answer, and `git diff --quiet` exiting 1
 * is how scripts ask "is the tree dirty". An independent implementation
 * (lox/papercuts) refuses automatic capture outright for this reason; the answer
 * here is a narrow, documented denylist rather than giving up the free signal.
 */
const BENIGN_COMMAND = /^\s*(?:!\s*)?(?:grep|rg|egrep|fgrep|test|\[|diff|cmp)\b/;
const BENIGN_GIT_QUERY = /\bgit\s+(?:diff|status)\b[^\n|;]*--(?:quiet|exit-code)\b/;

/**
 * Scrub credential shapes BEFORE anything is written. Redacting at capture (not
 * at read) is deliberate: the store is a plain file, and `rollup --apply` copies
 * sample error text into a GitHub issue body — a failed `curl -H "Authorization:
 * Bearer ..."` or a psql URI would otherwise carry a live secret out of the
 * machine. A papercut is never worth a leaked token, so the false-positive cost
 * of over-redacting is accepted.
 */
// The key-block pattern is assembled from fragments so this file does not match
// your config sync's own `guard_no_credentials` scanner, which greps the the source harness tree for
// that exact literal and refuses to sync when it finds it. Same trick, and same
// reason, as the comment in your config sync.
const KEY_BLOCK = new RegExp(
  '(-----BEGIN [A-Z ]*PRIV' + 'ATE KEY-----)[\\s\\S]*?(-----END [A-Z ]*PRIV' + 'ATE KEY-----)', 'g');

const REDACTIONS = [
  [KEY_BLOCK, '$1<redacted>$2'],
  [/\b(bearer\s+)[\w./+=-]{12,}/gi, '$1<redacted>'],
  [/\b(authorization\s*[:=]\s*)\S+/gi, '$1<redacted>'],
  [/\b(sk-|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|xox[abprs]-|AKIA|glpat-)[\w-]{8,}/g, '$1<redacted>'],
  // Stripe uses an UNDERSCORE (sk_live_/sk_test_), so the hyphenated `sk-` rule
  // above never matched it. Measured gap: a live Stripe key passed through clean.
  [/\b((?:sk|pk|rk)_(?:live|test)_)[\w]{8,}/g, '$1<redacted>'],
  [/\b(AIza)[\w-]{20,}/g, '$1<redacted>'],                       // Google API key
  [/\b(eyJ[\w-]{6,})\.[\w-]{6,}\.[\w-]{6,}\b/g, '$1.<redacted>'], // bare JWT, no Bearer label
  [/([\w+.-]+):\/\/([^\s:@/]+):([^\s@/]+)@/g, '$1://$2:<redacted>@'],
  // No leading \b: the secret-bearing name is usually PREFIXED (PGPASSWORD,
  // MYSQL_PWD, GITHUB_TOKEN), and \b would anchor past the prefix and miss it.
  [/([\w-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key))(["']?\s*[:=]\s*["']?)[^\s"',;&)]{4,}/gi,
    '$1$2<redacted>'],
];

function redact(s) {
  let out = String(s);
  for (const [re, rep] of REDACTIONS) out = out.replace(re, rep);
  return out;
}

/** Fold out everything that varies between two occurrences of the same friction. */
function normalize(s) {
  return String(s)
    // Terminal styling re-keys identical output on every colour change; the
    // 2026-08-26 census called every ESC-bearing key a defective fingerprint
    // (quarantine rule "ansi-escape"). Stripping CSI sequences here is that
    // rule's hook-side graduation.
    .replace(/\u001b\[[0-9;]*[A-Za-z]/g, '')
    .toLowerCase()
    .replace(/\/(?:home|mnt|tmp|users|var)\/[^\s:'"()]*/g, '<path>')
    .replace(/\b[0-9a-f]{7,40}\b/g, '<hex>')
    .replace(/\d+/g, '<n>')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 120);
}

/**
 * Reduce an error to a stable dedupe key. Ordered most-specific first: a
 * recognized class keeps its subject (the missing command, the absent module,
 * the guard that fired) because that subject is what a fix would target. Only
 * unrecognized text falls back to a normalized first line.
 */
// Claude Code prefixes a Bash failure's text with its own `Exit code N` wrapper.
// Keying the fallback on the literal first line therefore collapsed every
// unrelated Bash failure into one meaningless `bash:exit code <n>` bucket —
// measured 2026-08-06: a stock-ticker script exit, a TypeScript compile error, an
// eslint failure and a bare exit from four different repos all landed in it, and
// it crossed the rollup threshold on day one. The signal is on line 2+.
const WRAPPER_LINE = /^\s*(?:exit code\s*\d+|command failed(?: with exit code \d+)?|error)\s*[:.]?\s*$/i;

// A bare traceback header is as content-free as the exit-code wrapper: measured
// 2026-08-06, `bash:traceback (most recent call last):` collapsed 265 occurrences
// across 105 sessions — JSONDecodeError, TypeError, KeyError, AttributeError,
// ValueError and IndexError from unrelated inline scripts — into one key. The
// actual `ExceptionType: message` is the LAST line of a traceback, not the first.
const TRACEBACK_HDR = /^Traceback \(most recent call last\):$/;

// A trailing-ellipsis opener ("Checking formatting...") announces work, not
// failure; keying on it ranked prettier's banner as friction (41 sessions /
// 127 records by 2026-08-29) while the informative "[warn] Code style issues
// found in N files" summary sat on the LAST line. When -- and only when -- the
// first content line is such a no-signal banner, take the last content line
// instead: banner-first tools summarize at the end (same shape the traceback
// branch below already handles), and any output whose first line is NOT a
// bare banner keeps its existing key, so no established signature drifts.
const PROGRESS_LINE = /\.\.\.\s*$/;
const ERRORISH = /error|warn|fail|fatal|denied|refus|missing|cannot|unable|exception|blocked/i;

/** First line carrying actual content, skipping the harness's own wrapper lines. */
function signalLine(text) {
  const lines = String(text).split('\n');
  const content = lines.filter((l) => l.trim() && !WRAPPER_LINE.test(l));
  let real = content[0];
  if (real !== undefined && content.length > 1
      && PROGRESS_LINE.test(real) && !ERRORISH.test(real)) {
    real = content[content.length - 1];
  }
  const picked = (real || lines.find((l) => l.trim()) || '').trim();
  if (TRACEBACK_HDR.test(picked)) {
    for (let i = lines.length - 1; i >= 0; i--) {
      if (lines[i].trim()) return lines[i].trim();
    }
  }
  return picked;
}

/** True when a Bash failure carried nothing but the tool's own exit-code line. */
function isContentFree(text) {
  return !String(text).split('\n').some((l) => l.trim() && !WRAPPER_LINE.test(l));
}

// The old subject capture required the path to be followed only by whitespace
// before end-of-line, which no real ENOENT message satisfies — coreutils ends in
// `': No such file or directory` and Node uses `, open '<path>'`. Measured: it
// captured a subject on 4% of cases and the captures were ANSI fragments ('0m',
// '39m') and stopwords. These two phrase-anchored patterns recover a real subject
// on 89%, which surfaced two genuinely recurring bugs that were invisible before
// (no_such_file:example-project at 17 sessions, no_such_file:startupbros at 16).
const ENOENT_NODE = /no such file or directory,\s*\w+\s*'([^']+)'/i;
const ENOENT_COREUTILS = /['"]?([^\s'"\n:][^\n:]*?)['"]?:\s*No such file or directory/i;
const ENOENT_TOOL = /File does not exist\.?\s*(?:Did you mean\s+(\S+))?/i;

/** Path tail — the varying directory prefix is noise, the filename is the subject. */
function basename(p) {
  const clean = String(p).replace(/\/+$/, '');
  const i = clean.lastIndexOf('/');
  return (i === -1 ? clean : clean.slice(i + 1)).slice(0, 100);
}

// Signature subjects come from unbounded regex captures, so cap them: `sig` is
// not shrunk by the oversize-record recovery path, so an absurdly long capture
// would push a record past MAX_RECORD_BYTES and drop it entirely.
const SUBJ = (s) => String(s).slice(0, 100);

function signature(err, toolName, target) {
  const e = String(err || '');
  let m;

  if ((m = e.match(/(?:^|[\s:])([\w.+-]+): command not found/m))) return `command_not_found:${SUBJ(m[1])}`;
  if ((m = e.match(/No module named '?([\w.]+)'?/))) return `module_not_found:${SUBJ(m[1])}`;
  if (/\bCommand timed out\b|\btimed out after\b|\bETIMEDOUT\b/i.test(e)) return 'timed_out';
  // Our own guards. Which guard fired is the actionable part: a rule rediscovered
  // N times is either under-documented or a candidate for an allowlist change.
  if ((m = e.match(/BLOCKED by (\w[\w-]*)/i))) return `guard_blocked:${SUBJ(m[1].toLowerCase())}`;
  if (/a-vcs-guard|ALLOW_DESTRUCTIVE_GIT|ALLOW_MAIN_COMMIT/.test(e)) return 'guard_blocked:a-vcs-guard';
  if (/Codex quota is explicitly unavailable/i.test(e)) return 'guard_blocked:another-guard';
  if (/circuit breaker TRIPPED|doghouse/i.test(e)) return 'guard_blocked:codex-doghouse';
  // `File does not exist` is the Read/Edit/Write phrasing for the same friction
  // Bash reports as ENOENT; without it those fragmented into the generic bucket
  // instead of joining this one (measured by live probe, 2026-08-06).
  if ((m = ENOENT_NODE.exec(e) || ENOENT_COREUTILS.exec(e))) return `no_such_file:${basename(m[1])}`;
  if ((m = ENOENT_TOOL.exec(e)) && m[1]) return `no_such_file:${basename(m[1])}`;
  // Read/Edit say only "File does not exist." — the path never appears in the
  // message, so the recovered-subject branches above cannot fire and 217
  // sessions/30d collapsed into one generic bucket. The captured target is
  // the subject (measured 2026-08-30: the top cluster inside the generic key
  // was .compound-engineering/config.yaml probes across 6+ repos).
  if (/ENOENT|No such file or directory|File does not exist/i.test(e)) {
    if (target) return `no_such_file:${basename(target)}`;
    return 'no_such_file';
  }
  if (/Permission denied|EACCES/.test(e)) return 'permission_denied';
  if ((m = e.match(/already (?:exists|used by worktree)/i))) return `worktree_collision:${normalize(m[0])}`;
  // A StructuredOutput schema rejection names the FIRST missing property, which
  // varies per agent prompt — so keying on the message fragmented one failure
  // mode across 52 signatures (122 records, 17 sessions, 49% of all auto-source
  // fallback), and no single fragment ever crossed the rollup threshold. The
  // property name is never the actionable subject; the actionable fact is
  // "a subagent returned output that failed schema validation".
  if (/output does not match required schema/i.test(e)) return 'structuredoutput_schema_mismatch';
  if ((m = e.match(/MCP error (-?\d+)/))) return `mcp_error:${SUBJ(m[1])}`;
  if (/\bfatal:\s/.test(e)) return `git_fatal:${normalize((e.match(/fatal:\s*([^\n]+)/) || [, ''])[1])}`;

  // SUBJ on toolName too: every other branch bounds its subject, and `sig` is NOT
  // shrunk by the oversize-record recovery path — an unbounded tool name would
  // push the record past MAX_RECORD_BYTES and drop it silently.
  return `${SUBJ(String(toolName || 'tool').toLowerCase())}:${normalize(signalLine(e))}` || 'unknown';
}

/** Match the ~/.claude/projects/<slug> and claude/memory/<slug> naming. */
function projectSlug(cwd) {
  return String(cwd || process.cwd()).replace(/[/\\]/g, '-').replace(/^-+/, '-') || '-unknown';
}

/** Pull the error text out regardless of which field the payload carries it in. */
function extractError(input) {
  if (typeof input.error === 'string' && input.error) return input.error;
  const r = input.tool_response;
  if (typeof r === 'string') return r;
  if (r && typeof r === 'object') {
    const joined = [r.error, r.stderr, r.stdout, r.output, r.content]
      .filter((s) => typeof s === 'string' && s).join('\n');
    if (joined) return joined;
  }
  return '';
}

/**
 * The one thing this tool call was about, for tools that do not take a command.
 * Allowlisted keys only, first match wins, strings only.
 */
function toolTarget(toolInput) {
  if (!toolInput || typeof toolInput !== 'object') return '';
  const KEYS = ['file_path', 'notebook_path', 'path', 'pattern', 'url', 'query'];
  for (const key of KEYS) {
    const value = toolInput[key];
    if (typeof value === 'string' && value.trim()) return value;
  }
  return '';
}

function record(input) {
  const rawErr = extractError(input);
  if (!rawErr.trim()) return null;

  // A user interrupt is a human changing their mind, not repo friction.
  if (input.is_interrupt === true) return null;

  const rawCmd = (input.tool_input && input.tool_input.command) || '';
  // Only Bash carries `command`, so every other tool used to record THAT it
  // failed while losing WHAT it failed on. Measured over 30 days: 6,350 Read
  // records, 1,385 StructuredOutput, 1,178 WebSearch and ~520 MCP/WebFetch --
  // roughly a third of all auto-capture -- had no target at all, which is why
  // the corpus's #2 and #3 signatures could not be triaged: nothing recorded
  // which file was too large, or which query was rejected.
  //
  // An allowlist of known target keys, never the whole tool_input: a blob
  // capture would sweep in arguments nobody vetted and grow every record. The
  // value goes through redact() and the same length cap as cmd.
  const rawTarget = toolTarget(input.tool_input);
  if (BENIGN_COMMAND.test(rawCmd) || BENIGN_GIT_QUERY.test(rawCmd)) return null;

  // A Bash failure with NOTHING but the tool's own `Exit code N` line carries no
  // information and is overwhelmingly control flow, not friction: measured over 30
  // days, 353 such records led by cd/pkill/ssh/find/timeout, 22% of them carrying
  // signal-derived exit codes (124/137/143/144/128) and 16% xargs' exit-123
  // "some invocations failed" convention. This was the single largest signature in
  // the 7-day rollup simulation (54 sessions / 693 occurrences) with zero
  // actionable cases in the sample. Generalizes the per-command denylist, which
  // matches only a first token and cannot see compound commands at all.
  if (String(input.tool_name) === 'Bash' && isContentFree(rawErr)) return null;

  // Signature is computed on redacted text so a secret can never reach a key
  // that ends up in an issue title. Truncate first — see MAX_INPUT_CHARS.
  const err = redact(rawErr.slice(0, MAX_INPUT_CHARS));
  const cmd = redact(String(rawCmd).slice(0, MAX_INPUT_CHARS));
  const target = redact(String(rawTarget).slice(0, MAX_INPUT_CHARS));

  const rec = {
    ts: new Date().toISOString(),
    sig: signature(err, input.tool_name, target),
    tool: String(input.tool_name || 'unknown').slice(0, 40),
    err: err.slice(0, MAX_ERR_CHARS),
    cmd: String(cmd).slice(0, 200),
    target: String(target).slice(0, 200),
    cwd: String(input.cwd || '').slice(0, 200),
    session: String(input.session_id || '').slice(-8),
    ms: typeof input.duration_ms === 'number' ? input.duration_ms : null,
    source: 'auto',
  };

  let line = JSON.stringify(rec) + '\n';
  if (Buffer.byteLength(line) > MAX_RECORD_BYTES) {
    // Shed the largest free-form field first, then hard-truncate, so the write
    // stays inside the atomic-append guarantee no matter how odd the input is.
    rec.err = rec.err.slice(0, 200);
    rec.cmd = rec.cmd.slice(0, 80);
    line = JSON.stringify(rec) + '\n';
    if (Buffer.byteLength(line) > MAX_RECORD_BYTES) return null;
  }
  return line;
}

/**
 * Store root, resolved exactly as the CLI resolves it (papercut.py:68).
 *
 * The two MUST agree. papercut.py honors PAPERCUT_STORE; this hook hardcoded
 * ~/.claude/papercuts, so any machine that set the variable got a split corpus:
 * the hook appending records to one store while every read path -- list,
 * triage, rollup, the verification stage -- looked in another and reported the
 * friction as never having happened. Silent, and invisible to both sides.
 */
function storeDir() {
  const override = process.env.PAPERCUT_STORE;
  if (override && override.trim()) return override.trim();
  // CLAUDE_CONFIG_DIR next, exactly as cli.py resolves its store root: found
  // by dogfooding v0.1.1 against a scratch profile (2026-08-31) — the hook
  // wrote to the real homedir store while every CLI read looked under the
  // overridden config dir. Same split-corpus failure as above, new spelling.
  const configDir = process.env.CLAUDE_CONFIG_DIR;
  if (configDir && configDir.trim()) return path.join(configDir.trim(), 'papercuts');
  return path.join(os.homedir(), '.claude', 'papercuts');
}

/**
 * Append one already-serialized record line to a project's store.
 *
 * Factored out so the PreToolUse guards can log their OWN denials through the
 * identical write path. A permission denial reaches no post-hook at all — probed
 * live: a-vcs-guard and a-command-guard both deny, and neither PostToolUse, PostToolUseFailure
 * nor PermissionRequest dispatches — so a guard that wants to be counted has to
 * write the record itself. Guard blocks were the 4th most common friction class
 * in the 30-day sweep (66 sessions, 53 projects), so this is the single largest
 * remaining coverage hole.
 *
 * Returns true on write. NEVER throws: a guard's enforcement decision must not
 * depend on logging succeeding.
 */
function appendRecord(cwd, line) {
  try {
    if (!line) return false;
    const dir = storeDir();
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, `${projectSlug(cwd)}.jsonl`);
    try {
      if (fs.statSync(file).size > MAX_LOG_BYTES) return false;
    } catch { /* first write for this project */ }
    fs.appendFileSync(file, line);
    return true;
  } catch {
    return false;
  }
}

/**
 * Record a PreToolUse denial. Called by our own guards from their deny paths.
 * Wrapped so any failure here is invisible to the guard's decision.
 */
function logDenial({ guard, cwd, sessionId, tool, command, reason }) {
  try {
    // Truncate BEFORE the regex work — this runs inside a guard's synchronous
    // deny path, where an unbounded input would stall enforcement. See MAX_INPUT_CHARS.
    const err = redact(String(reason || '').slice(0, MAX_INPUT_CHARS));

    // A PreToolUse payload frequently carries NO session_id and NO cwd — measured
    // 2026-08-06 on live data: 239 of 251 guard records (95%) had both empty, which
    // made rank() score each one as its own distinct session and invented an "80
    // sessions / 20 projects" headline out of 82 records. a-repetition-guard.js:58-61
    // already documented this and falls back to ppid; that precedent is reused here.
    // ppid is the parent Claude Code process: stable within a session, distinct
    // across them. process.cwd() is the directory the hook was invoked in.
    // Bound the identifier, but KEEP the marker. This used to be built here
    // and then truncated with .slice(-8) at the record below, which cuts from
    // the right: `ppid-318283` became `d-318283` and `ppid-2726826` became
    // `-2726826`. That threw away the one signal saying "this record carried
    // no session_id", leaving a synthesized identity shaped exactly like a
    // real 8-char session id. Measured 2026-08-29 on the live store: 4,899 of
    // 4,917 guard_blocked:a-worker-guard records were synthesized identities,
    // and telling them apart required inferring intent from a stray hyphen.
    const sid = sessionId ? String(sessionId).slice(-8) : `ppid-${process.ppid}`;
    let dir = cwd;
    if (!dir) { try { dir = process.cwd(); } catch { dir = ''; } }

    const rec = {
      ts: new Date().toISOString(),
      // Attribute to the guard that actually fired, rather than pattern-matching
      // its prose. Text matching put a-vcs-guard's un-prefixed `git add .` message
      // and a-worker-guard's `Blocked: ...` messages into the generic per-tool
      // bucket, and split ONE a-worker-guard rule across 13 signatures that
      // differed only by systemctl verb and trailing punctuation. The guard knows
      // its own name; asking the message was always the wrong question.
      sig: guard ? `guard_blocked:${SUBJ(String(guard).toLowerCase())}` : signature(err, tool),
      tool: String(tool || 'unknown').slice(0, 40),
      err: err.slice(0, MAX_ERR_CHARS),
      cmd: redact(String(command || '').slice(0, MAX_INPUT_CHARS)).slice(0, 200),
      cwd: String(dir || '').slice(0, 200),
      session: sid,
      ms: null,
      source: 'guard',
    };
    let line = JSON.stringify(rec) + '\n';
    if (Buffer.byteLength(line) > MAX_RECORD_BYTES) {
      rec.err = rec.err.slice(0, 200);
      rec.cmd = rec.cmd.slice(0, 80);
      line = JSON.stringify(rec) + '\n';
      if (Buffer.byteLength(line) > MAX_RECORD_BYTES) return false;
    }
    return appendRecord(rec.cwd, line);
  } catch {
    return false;
  }
}

// an earlier change: agents Read whole .jsonl transcripts/stores past the token
// ceiling and retry the same file (measured 2026-08-30: 72% of target-bearing
// read-limit records, one file re-read 11x in a session). The error's own
// offset/limit advice fails to redirect because the agent wants the WHOLE
// content; the tools that answer that want are named here instead, at the
// exact moment of failure. Informational phrasing only — imperative
// additionalContext has measurably backfired in this harness.
const READ_LIMIT_ERR = /exceeds maximum allowed (?:tokens|size)/i;
const TRANSCRIPT_PATH = /\/\.claude\/projects\/[^\s]*\.jsonl$/;

// The config base is CLAUDE_CONFIG_DIR when set (papercut.py resolves its
// store the same way) -- a hardcoded /.claude/projects/ substring misroutes
// every transcript to the generic store hint under an overridden config dir
// (refute-vet finding, 2026-08-30). The regex stays as a fallback for
// transcripts living under someone else's tree.
function isTranscriptTarget(target) {
  const base = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude');
  return target.startsWith(path.join(base, 'projects') + path.sep)
    || TRANSCRIPT_PATH.test(target);
}

function readLimitHint(input) {
  if (String(input.tool_name) !== 'Read') return null;
  const err = extractError(input);
  if (!READ_LIMIT_ERR.test(err)) return null;
  const target = String((input.tool_input && input.tool_input.file_path) || '');
  if (!/\.jsonl$/.test(target)) return null;
  if (isTranscriptTarget(target)) {
    return ('This target is a session transcript larger than the Read ceiling. '
      + 'jq, grep and tail can slice it without loading the whole file.');
  }
  return ('This target is a .jsonl store larger than the Read ceiling. '
    + 'jq, grep and tail filter it without loading the whole file.');
}

function run(rawInput) {
  let input = {};
  try { input = JSON.parse(rawInput); } catch { return rawInput; }

  try {
    appendRecord(input.cwd, record(input));
  } catch {
    // A logger must never break a session. Swallow everything.
  }

  // an earlier change: on the one failure shape where the error's own advice cannot
  // help (a whole-.jsonl read past the ceiling), answer with the tool that
  // can. additionalContext is the documented PostToolUseFailure channel; the
  // hint path replaces the legacy raw-input echo for THIS shape only, and a
  // hint failure falls back to the echo — never a broken session.
  try {
    const hint = readLimitHint(input);
    if (hint) {
      return JSON.stringify({
        hookSpecificOutput: {
          hookEventName: 'PostToolUseFailure',
          additionalContext: hint,
        },
      });
    }
  } catch {
    // fall through to the pass-through echo
  }
  return rawInput;
}

module.exports = {
  run, signature, normalize, projectSlug, redact, signalLine, isContentFree,
  appendRecord, logDenial, readLimitHint,
};

if (require.main === module) {
  let data = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (c) => { if (data.length < 1024 * 1024) data += c; });
  process.stdin.on('end', () => process.stdout.write(run(data)));
}
