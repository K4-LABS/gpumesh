## What was wrong before?

The behaviour you observed, not the code you read.

## What does this change?

One or two sentences.

## How do you know it works?

The test you added, or the manual steps you ran — especially for networking
changes, where "I ran it across two machines and here is the output" is worth
more than any unit test.

---

### Checklist

- [ ] `pytest` is green locally (Linux: 649 passed; Windows: 645 + 3 skips + 1 xpass)
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`
- [ ] Branch named `fix/`, `feat/`, `docs/`, `test/`, or `chore/` + short description
- [ ] No reformatting of files unrelated to this change (no linter is configured — match surrounding style)

Fixes #(issue)
