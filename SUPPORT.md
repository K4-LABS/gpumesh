# Getting help with gpumesh

Most of what arrives in the issue tracker is a question, not a bug — and
questions get better answers in a thread that is not also trying to be a bug
report. This page says where each kind of thing goes.

## Where to go

| You have… | Go here |
|---|---|
| A question — "how do I", "is this supposed to work like this", "which flag do I want" | [Discussions → Q&A](https://github.com/K4-LABS/gpumesh/discussions/categories/q-a) |
| Something that does not work the way the docs say it should | [Open a bug report](https://github.com/K4-LABS/gpumesh/issues/new?template=bug_report.yml) |
| A capability gpumesh does not have yet | [Open a feature request](https://github.com/K4-LABS/gpumesh/issues/new?template=feature_request.yml) |
| An idea that is not yet a concrete request | [Discussions → Ideas](https://github.com/K4-LABS/gpumesh/discussions/categories/ideas) |
| A security vulnerability | **Do not open an issue.** See [SECURITY.md](SECURITY.md) and use the Security tab → *Report a vulnerability* |
| A code of conduct concern | [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) |
| You want to contribute | [CONTRIBUTING.md](CONTRIBUTING.md) |

Before any of those, two things are worth thirty seconds:

- **Search existing issues and discussions.** Networking problems in particular
  repeat — the same VPN adapter and the same hypervisor bridge come up more than
  once.
- **Read the README's Limitations section.** Some gaps are deliberate design
  boundaries. Model sharding and running untrusted code are the two that come up
  most often as bug reports and are neither bugs nor oversights.

## What a good gpumesh question contains

gpumesh is not one program, it is at least two programs on two machines talking
to each other. A question that describes only one of them is usually
unanswerable. Include:

**1. The versions on *both* machines.** Not just the one that printed the error.

```bash
gpumesh doctor           # run this on the coordinator AND on the worker
```

`gpumesh doctor` is read-only and exists for exactly this: it prints the
gpumesh version, the interpreter, torch/CUDA, cloudpickle (the wire format),
the saved coordinator, and the addresses this machine would advertise — one
block, meant for pasting. `gpumesh --version` and `python --version` on both
machines are the minimum if you would rather not paste the whole report.

This matters more here than in most projects. `@mesh` and `@accelerate` ship
your *function* to another machine, and the mechanism for that depends on the
Python version on both ends matching. A coordinator on 3.11 and a worker on 3.9
is the single most common cause of a failure that looks like a bug in your own
code — a `NameError`, an unpickling error, or a task refused outright with a
message about the worker's Python version. If the two versions differ, say so
up front; it is very often the whole answer.

**2. The exact commands you ran, on which machine, in order.** "I started the
coordinator and joined" leaves out the port, the URL form, and which of the two
machines is which.

**3. The full output, not the last line.** The lines above an error usually name
the address and port that were actually tried, which is the part that identifies
the problem. **Redact your token.**

**4. Anything unusual about the network.** VPN (Tailscale, corporate, WireGuard),
WSL, VirtualBox or VMware adapters, Docker Desktop, corporate Wi-Fi with client
isolation, or the two machines being on different subnets. Any one of these can
be the entire cause, and none of them are visible from the error message.

**5. For connection failures, one reachability check.** From the **worker**:

```bash
curl http://<COORDINATOR-IP>:<PORT>/api/workers
```

An HTTP **401** means the machine is reachable and only the token is wrong. A
timeout or a refusal means something quite different. These need opposite
advice, so this one line usually saves a whole round trip.

## What to expect

Two people maintain gpumesh, in their own time. You can expect a response
within **7 days**. If a week passes with nothing, ping the thread — that is not
rude, it is the intended behaviour, and a silent thread is more likely to have
been lost than ignored.

Faster responses are likely if your report includes the five things above.
Slower ones are likely for anything that needs two physical machines and a
particular network adapter to reproduce.

## Things that are not support requests

- **"gpumesh has no transport encryption"** and **"workers execute code the
  coordinator sends"** are documented properties of the design, not bugs. See
  [SECURITY.md](SECURITY.md).
- **A misleading error message *is* a bug**, though, even if you worked out the
  real cause yourself. Please do report those.
