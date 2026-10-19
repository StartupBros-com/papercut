'use strict';
/**
 * Shared quiet-on-no-op stdout writer for hook standalone entry points.
 *
 * Measured 2026-09-23 over 7 days of transcripts: 1.73GB of hook_success
 * stdout, none of it reaching the model. Every hook module signals a no-op
 * by returning its run(rawInput) argument unchanged; entry points that wrote
 * that return value straight to stdout were echoing the entire raw tool-call
 * payload back out on every no-op call, for no reader on either side
 * (an earlier change) -- PostToolUse/most events only ever route stdout into the
 * debug/transcript record (never the model), and for PreToolUse an empty
 * stdout body IS the correct "no decision" signal, not a missing one.
 *
 * Call as writeHookOutput(output, raw) wherever an entry would otherwise
 * `process.stdout.write(output)`.
 */

function writeHookOutput(output, raw) {
  const isString = typeof output === 'string';
  const isBuffer = Buffer.isBuffer(output);
  if (!isString && !isBuffer) return; // null/undefined/other: nothing to write
  if (output.length === 0) return; // empty: nothing to write
  if (isString && output === raw) return; // unchanged: the no-op signal itself
  if (isBuffer && typeof raw === 'string' && output.toString('utf8') === raw) return;
  process.stdout.write(output);
}

module.exports = { writeHookOutput };
