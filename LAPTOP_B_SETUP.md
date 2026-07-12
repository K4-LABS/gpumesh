# gpumesh — Laptop B Worker Setup (Agent Handoff)

You are setting up a **worker** on this machine (Laptop B) to join a `gpumesh`
distributed compute cluster. The **coordinator** is already running on another
machine (Laptop A) and is reachable over **Tailscale**.

## Connection details (already provisioned)

| Item | Value |
|------|-------|
| Coordinator URL | `http://YOUR_COORDINATOR_IP:8000` |
| Auth token | `YOUR_SECRET_TOKEN` |
| Transport | Tailscale (both machines on the same tailnet) |
| Repo | https://github.com/Samurai007AK/gpumesh |

> The coordinator IP is Laptop A's **Tailscale** address, so this works
> from any network as long as this machine is logged into the **same
> Tailscale account**.

---

## Steps

### 1. Verify Tailscale is connected

Tailscale is already installed on this laptop. Confirm it can reach the coordinator:

```powershell
# Windows
& "C:\Program Files\Tailscale\tailscale.exe" status
ping YOUR_COORDINATOR_IP
```

```bash
# macOS / Linux
tailscale status
ping YOUR_COORDINATOR_IP
```

You must see a reply from the coordinator IP. If ping fails, make sure this machine
is logged into the **same Tailscale account** as the coordinator.

### 2. Verify Python 3.9+ is installed

```powershell
python --version    # need 3.9 or newer
```

If missing, install from https://www.python.org/downloads/ (check "Add to PATH").

### 3. Get the code

```powershell
git clone https://github.com/Samurai007AK/gpumesh
cd gpumesh
```

> NOTE: the repo ships the package as zip files (`gpumesh.zip`, `examples.zip`,
> `tests.zip`). If after cloning you do **not** see a `gpumesh/` folder with
> `.py` files inside, unzip them first:
>
> ```powershell
> Expand-Archive -Path gpumesh.zip  -DestinationPath . -Force
> Expand-Archive -Path examples.zip -DestinationPath . -Force
> ```

### 4. Install the package

```powershell
python -m pip install -e .
```

CPU-only is fine — no `torch` needed. (Optional: `pip install torch` if this
machine has an NVIDIA GPU and you want a real GPU benchmark + `cuda` device.)

### 5. Join the mesh

```powershell
gpumesh join http://YOUR_COORDINATOR_IP:8000 --token YOUR_SECRET_TOKEN
```

Expected output:

```
[worker] device=cpu (...) score=... GFLOP/s
[worker] joined mesh as <worker_id>
```

**Leave this command running** — it is the worker loop. It will pull tasks,
run them in a sandboxed subprocess, and report results until you press Ctrl+C.

If `gpumesh` is not recognized as a command, use the module form instead:

```powershell
python -m gpumesh.cli join http://YOUR_COORDINATOR_IP:8000 --token YOUR_SECRET_TOKEN
```

---

## Verify it worked

From **this** laptop (in a second terminal), or ask the operator of Laptop A to
run:

```powershell
gpumesh workers --url http://YOUR_COORDINATOR_IP:8000 --token YOUR_SECRET_TOKEN
```

You should see **two** workers listed — one for Laptop A and one for this
machine (hostname of Laptop B), both `[alive]`.

---

## Troubleshooting

| Symptom | Fix |
|--------|-----|
| `ping <coordinator_ip>` fails | Not on the same tailnet — log into the same Tailscale account and retry `tailscale status`. |
| `coordinator unreachable` in worker output | Coordinator not running, or a firewall blocks port 8000. Confirm with the Laptop A operator. |
| `401` / `bad or missing token` | Token mismatch — must be exactly the token used on the coordinator. |
| `gpumesh: command not found` | Use `python -m gpumesh.cli ...` instead. |
| No `gpumesh/*.py` after clone | Unzip the archives (see step 3 note). |

## What NOT to do

- Do not start a `serve` (coordinator) on this laptop — Laptop A is the
  coordinator. This machine only runs `join`.
- Do not change the token; it must match Laptop A.
- The worker executes code submitted to the coordinator (remote code execution
  by design). Only stay joined to a mesh you trust.

---

## Report back

Once joined, report to the operator:
1. This machine's hostname and reported `score` (GFLOP/s).
2. Confirmation that `gpumesh workers` shows both machines `[alive]`.
3. Leave the `join` process running so a test job can be distributed.
