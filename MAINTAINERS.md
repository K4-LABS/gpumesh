# Maintainers

Who has commit rights, what each person actually looks after, and how to reach
them. This is the roster; `GOVERNANCE.md` is the process, and
`.github/CODEOWNERS` is the machine-readable version that GitHub acts on.

Three files, one fact. If you change the roster here, change it in all three,
or the review requests and the documentation will disagree about who is
responsible for what.

## Current maintainers

| Maintainer | GitHub | Role | Areas |
| --- | --- | --- | --- |
| Arijit Konar | [@Samurai007AK](https://github.com/Samurai007AK) | Lead maintainer (BDFL) | Everything; final say on scope, releases, and security response |
| Jinia Konar | [@jinia-konar](https://github.com/jinia-konar) | Maintainer | Review and merge across the repository |

Both accounts hold write access, both are listed in `.github/CODEOWNERS`, and
both are automatically requested on every pull request.

## What "maintainer" means here

Concretely, and no more than this:

- **Write access** to the repository, so they can merge.
- **Review authority** on any pull request. A change lands when a maintainer
  who did not write it approves it and CI is green.
- **Release authority.** Tagging, publishing to PyPI, and pushing the
  container image are done from CI, triggered by a maintainer.
- **Security response.** Receiving and triaging private reports, and deciding
  when an advisory is published. See `SECURITY.md`.

gpumesh is a BDFL project. Where two maintainers disagree and cannot settle it
in the thread, @Samurai007AK decides. `GOVERNANCE.md` says so at more length
and explains why the honest description is that one rather than a committee.

## Contact

- **Bugs, features, questions:** open an issue. Public by default, and that is
  where decisions are supposed to be made, and a question answered in a DM
  helps exactly one person.
- **Security vulnerabilities:** do **not** open an issue. Use
  [GitHub Security Advisories](https://github.com/K4-LABS/gpumesh/security/advisories/new)
  for this repository. `SECURITY.md` carries the full disclosure policy and the
  fallback contact path if advisories are unavailable to you.
- **Anything that does not fit either:** arijitkonar16@gmail.com.

## Becoming a maintainer

There is no application form and no fixed count of merged pull requests.
What actually earns write access is a track record of review-quality work:
changes that arrive with tests, that say plainly what they do not cover, and
that hold up when someone reads them six months later. Sustained review of
*other people's* pull requests counts for at least as much as authoring them.

If that describes you, @Samurai007AK will most likely ask before you do. You
are also welcome to ask directly.

## Emeritus

None yet. When a maintainer steps back, their name moves here rather than
disappearing. The commit history is public either way, and quietly deleting
people from a roster is a bad habit for a project to acquire.
