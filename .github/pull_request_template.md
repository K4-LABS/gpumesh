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

- [ ] `pytest` is green locally — no failures and no errors. (Windows shows a
      few skips and one xpass; those are intentional. The total count moves as
      tests are added, so a *failure* is what matters, not the number.)
- [ ] `ruff check --select E9,F63,F7,F82 .` is clean — that is the subset CI
      hard-gates on. The full `ruff check .` and `mypy` are advisory in CI
      (there is a pre-existing backlog), but please read their annotations on
      the lines you touched. Any `# noqa` added carries a reason on the same line.
- [ ] Coverage still clears the CI floor: `pytest --cov=gpumesh --cov-fail-under=80`
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`
- [ ] If the wire changed, `gpumesh.PROTOCOL_VERSION` is bumped — and
      `.github/workflows/compat.yml` still passes
- [ ] Branch named `fix/`, `feat/`, `docs/`, `test/`, or `chore/` + short description
- [ ] No reformatting of files unrelated to this change
- [ ] **Signed off.** Every commit carries a `Signed-off-by:` line (`git commit -s`),
      certifying the [Developer Certificate of Origin](https://developercertificate.org/).
      Forgot? `git rebase --signoff HEAD~N` and force-push.

### AI assistance

- [ ] **No AI tool was used**, or — if one was — I have said so in the
      description above, I have read every line of this diff myself, and I have
      run it. See [AI-assisted contributions](https://github.com/K4-LABS/gpumesh/blob/master/CONTRIBUTING.md#ai-assisted-contributions).

Fixes #(issue)
