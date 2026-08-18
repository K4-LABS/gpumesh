# Windows setup guide

Windows has two failure modes that send people around in circles, and both
are worth understanding before they happen — they look identical from the
*other* machine, which is why they eat an hour each:

1. **`gpumesh` is not on PATH** — the console script was installed, but your
   shell cannot find it. Easy fix, explained below.
2. **Windows Firewall silently drops the port** — the coordinator is running
   perfectly; the packets just never arrive. The error says "timed out", not
   "refused", and the fix is one `netsh` command.

## Run the coordinator as Administrator (why it matters)

gpumesh tries to add its own Windows Firewall rule at startup via
`try_add_firewall_rule()` in `gpumesh/utils.py`. It does this silently and
returns `False` without doing anything when the current process is **not
elevated** — you get one yellow `WARNING` line and it moves on:

```
[gpumesh] WARNING: could not add firewall rule (need admin).
          Run 'gpumesh serve' as Administrator, or manually allow port 8000.
```

So the coordinator will happily start, bind `0.0.0.0:8000`, print
"NETWORK-EXPOSED COORDINATOR", and then be unreachable from every other
machine because the firewall is dropping inbound packets on that port. The
rule it *wants* to add, for reference:

- `gpumesh-8000` — inbound TCP on the serve port (the default `8000`, or
  whatever `--port` you chose)
- `gpumesh-discovery-udp` — inbound UDP on `48900` (the LAN presence
  broadcast that lets a coordinator discover and claim a worker)

Both are added with `profile=any edge=yes` — the `edge` flag matters if your
machines reach each other through a phone hotspot.

Two ways to fix it — pick one:

**Option A: start the coordinator as Administrator.** Right-click your
terminal and choose *Run as administrator*, then `gpumesh serve` as usual.
gpumesh detects the elevated process and adds the rules itself.

**Option B: add the rule manually, once, as a normal user.** Run this from
an *elevated* terminal (one time only — the rule persists across reboots):

```bat
netsh advfirewall firewall add rule name=gpumesh-8000 dir=in action=allow protocol=TCP localport=8000 profile=any edge=yes
netsh advfirewall firewall add rule name=gpumesh-discovery-udp dir=in action=allow protocol=UDP localport=48900 profile=any edge=yes
```

Change `8000` to your actual `--port` if you did not use the default. Then
start the coordinator from a normal (non-admin) terminal. Verify the rule
exists with `gpumesh doctor`, which reports on Windows whether a firewall
rule for the port is present:

```bat
gpumesh doctor
```

## `gpumesh` vs `python -m gpumesh`

`pip install gpumesh` installs a `gpumesh.exe` console script into your
Python's `Scripts` directory, e.g. `C:\Users\you\AppData\Local\Programs\Python\Python313\Scripts\gpumesh.exe`.
If that directory is not on your `PATH`, the command is:

```
'gpumesh' is not recognized as an internal or external command
```

This is an install-path problem, not an install problem. Either:

- add that `Scripts` directory to your `PATH` (Windows Settings →
  *Environment Variables* → edit `Path`), or
- just use the module form, which works no matter what:

```bat
python -m gpumesh --version
python -m gpumesh serve --port 8000 --token <token>
```

`python -m gpumesh` and `gpumesh` are the same program with the same
commands and flags — nothing else differs. (If you installed into a
per-user or virtual environment, `python` and `Scripts` live inside that
environment; use that environment's `python -m gpumesh`.)

## Reading the error: 10060 (timed out) vs 10061 (refused)

The worker's connection errors already carry the diagnosis — the two cases
need *opposite* fixes, and the worker prints different advice for each, so
read the line before touching anything:

| Error | What it means | What to do |
|-------|---------------|------------|
| **Connection REFUSED (10061)** — something answered and said no | Nothing is listening on that address. The coordinator is **not running**, or is bound to a different interface (e.g. still on `127.0.0.1`) | Start the coordinator (`gpumesh serve --token ...`), or restart it with `--host 0.0.0.0` so other machines can reach it. The worker's advice: test from this machine with `curl http://<host>:<port>/api/workers` — HTTP 401 means it *is* reachable and only the token is wrong |
| **Connection TIMED OUT (10060)** — nothing answered at all | The packets never arrived. **The coordinator may be running fine** — a firewall is dropping the port, or the advertised address is unroutable from this machine | On the coordinator, allow the inbound TCP port (the `netsh` commands above) or restart it as Administrator. Also check the advertised address: a VPN / VirtualBox / VMware / Hyper-V / Docker adapter advertises an address only that machine can reach — ask for the address that matches your real network (`ipconfig` on Windows), and pin it with `--host-ip <IP>` |

The rule of thumb: **refused means "not there", timed out means "blocked"**.
A timeout is the signature of Windows Firewall; if the coordinator is bound
to `0.0.0.0` (the banner says "NETWORK-EXPOSED COORDINATOR") and the worker
still times out, the firewall is your first suspect, not the coordinator.

Two more Windows-specific gotchas that produce the same symptoms:

- **The default bind is `127.0.0.1`.** If you never restarted with
  `--host 0.0.0.0`, no other machine can connect *at all* — from the worker
  that looks identical to a firewall block. Check the bind **before** the
  firewall. See [`docs/cli.md`](cli.md) for `--host` vs `--host-ip`.
- **Virtual adapters advertise themselves.** A hotspot or VMware/Hyper-V
  adapter is a perfectly valid address to `ipconfig` — and unreachable from
  any other machine. gpumesh prints "This machine has other addresses" when
  it detects one; pin the right one with `--host-ip <IP>` or
  `GPUMESH_HOST_IP=<IP>`.

## Quick checklist

If a second machine cannot join:

1. `gpumesh doctor` on the coordinator — read-only, never prints the token,
   and shows in one screen whether the coordinator binds loopback, which
   address it advertises, and whether the firewall rule exists.
2. Coordinator started with `--host 0.0.0.0`? (Not just `--host-ip`.)
3. Firewall rule for the port exists? (Administrator, or the `netsh` commands
   above.)
4. Worker error says **refused** or **timed out**? Refused → the coordinator
   is not listening where the worker is dialing. Timed out → firewall or
   unroutable address.
5. Both machines on the same network? Different networks need
   [Tailscale](https://tailscale.com/download).
