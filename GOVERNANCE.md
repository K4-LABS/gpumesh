# Governance

This document describes how gpumesh is actually run today, not how it might be
run one day. Where the honest answer is "one person decides", it says that.
A governance document that describes an aspirational structure is worse than
none, because people plan around it.

## The short version

gpumesh is a **BDFL project**. [@Samurai007AK](https://github.com/Samurai007AK)
is the sole maintainer and has final say on every decision: what ships, what is
rejected, what the release cadence is, and what the project is for.

There is no steering committee, no vote, and no tie-break procedure, because
there is no one to tie with. If that changes, this file changes with it.

## How decisions get made

**In public, in issues.** Design decisions are discussed in the issue or pull
request they affect, not over DM and not in private. If you find a decision you
cannot trace to a public thread, that is a gap — ask about it and it will be
written down.

**Discussion is genuinely wanted, and it is not a vote.** A well-argued
objection changes outcomes here regularly; a count of thumbs-up does not. If you
disagree with a decision, the productive move is to state what breaks for you
and why, ideally with the case you would make to someone who has to maintain it
for years.

**Bias toward "not yet".** gpumesh's public surface (`@mesh`, `@accelerate`,
`GPUMesh`, the CLI) is small on purpose and everything added to it has to be
maintained across three operating systems and five Python versions. A proposal
being reasonable is not sufficient; it also has to be worth its permanent cost.
Expect "open an issue and make the case first" as the standard answer to
unsolicited feature PRs.

**Some limits are design boundaries, not gaps.** Model sharding and running
untrusted code are the two that come up most. Proposals to move those are still
welcome, but they are arguments about the project's purpose, not bug reports.

## Who can do what

| Role | Who | Can |
|---|---|---|
| **Maintainer** | @Samurai007AK | Merge, release, set direction, grant roles |
| **Triager** | Nobody yet | Label, close duplicates, ask for repro info, mark `needs-triage` / `needs-repro` |
| **Contributor** | Anyone with a merged PR | Everything anyone can do, plus the credibility that comes with having shipped something here |

## Becoming a triager

This is the one role with a real path attached, and it is deliberately a low
bar, because triage is the bottleneck on a solo project. There is no application
form. The maintainer will offer triage rights to someone who has, over a few
weeks:

- Reproduced or ruled out bugs on issues that were not theirs — especially the
  networking ones, where the reporter and the maintainer often have no OS or
  network adapter in common
- Asked reporters for the missing pieces (versions on both machines, full
  output, the `curl` reachability check) rather than waiting for the maintainer to
- Been accurate about what is a bug, what is a documented limitation, and what
  is a question that belongs in Discussions

Triage rights are GitHub's `triage` permission: labels, milestones, closing and
reopening issues, requesting reviews. They do not include merge access. Nobody
has them yet — if you want them, the way in is to start doing the work
publicly.

## Becoming a maintainer

There is no fixed criterion, because it has not happened yet and inventing a
rubric in advance would be exactly the kind of fiction this document is trying
to avoid. Realistically it would follow sustained triage work plus a body of
merged, non-trivial changes, and it would be an offer rather than an
application. If it happens, this section gets rewritten to describe what
actually occurred.

## Releases

The maintainer cuts releases. Versioning is semantic-ish: the public surface
follows semver, and internal APIs change without a deprecation cycle. Each
release ships from `main` with `CHANGELOG.md` updated under the version
heading.

## If the maintainer steps away

The realistic failure mode of a solo project is not a hostile takeover, it is
silence. What adopters should be able to plan around:

**The license is the guarantee.** gpumesh is AGPL-3.0 and every contribution is
AGPL-licensed inbound (see [CONTRIBUTING.md](CONTRIBUTING.md)). No CLA assigns
copyright to anyone, and there is no entity that could relicense the existing
code out from under you. Whatever happens to this repository, the code you have
stays usable, modifiable and redistributable, forever, by anyone — and any
modification made available over a network must offer its source in turn.

**Fork without asking.** If the project goes quiet and you need it maintained,
fork it. That is not a hostile act and no permission is required or expected.
Please do rename the PyPI distribution and the Docker image so users are not
confused about who is answering for what.

**Archiving is announced, not silent.** If the maintainer decides to stop, the
intent is to: mark the repository archived, say so in the README and in a
release note, and — if a credible fork exists by then — link to it from the
README so that people arriving from search engines land somewhere maintained.

**Unresponsive for six months.** If there has been no maintainer activity on
issues or commits for six months and no archive notice, treat the project as
unmaintained and act accordingly. Six months of silence is a real answer, and
you should not have to guess at it.

**What is not transferable.** The PyPI project `gpumesh`, the Docker Hub
namespace `samurai007ak/gpumesh`, and the GitHub repository are tied to the
maintainer's personal accounts. There is no organisation account and no shared
credential, so a fork cannot inherit the published names. This is a real
limitation of a solo project and is stated here rather than discovered later.

## Changing this document

Open an issue. Changes to governance are made by the maintainer, in public,
with the reasoning written down.
