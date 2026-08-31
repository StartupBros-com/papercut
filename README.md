# papercut

**Status: release candidate. Sanitized, tested, not yet announced** — publication waits on the first fix completing its live verification window in the source harness, so the README's central claim is witnessed, not promised.

Agents hit small frictions constantly — a misleading error, an undocumented
step, a command that succeeds but does the wrong thing — and route around all
of them silently. Friction nobody records is friction nobody fixes.

papercut is the cheap write path for that, plus the machinery to turn the pile
into ranked, tracked fixes:

- a `PostToolUseFailure` hook records hard tool failures automatically
- a stdlib-only CLI ranks signatures by **distinct sessions hit**, so
  repetition is the priority signal
- related signatures fold into a causal **family**, so you fix the cause
  instead of filing near-duplicates of its spellings
- one family at a time carries an evidence dossier into a filed work item
- a **verification stage** reports whether the fix actually held — and refuses
  to call a fix verified on silence alone, because silence has two causes

## Provenance

This tree is produced by a re-runnable transform from a private source
harness, not by hand-editing: the vendor translates evidence-bearing comments
instead of stripping them, verifies its output parses before verifying it is
clean, and a checker holds the private-reference count at zero (it was 183
before the transform existed). The packaged test suite runs green as shipped.
See [docs/EXTRACTION-DESIGN.md](docs/EXTRACTION-DESIGN.md) for what ships,
what becomes configuration, and the design record.

```bash
python3 scripts/check_stdlib_only.py      # dependency contract
python3 scripts/check_no_private_refs.py  # sanitization debt, as a number
```
