# gpumesh with a window: three laptops, ten minutes

You have three laptops on one table and a sweep you want finished before
lunch. This page gets you there. If you already live in the terminal, the
[Quickstart](../README.md#quickstart) covers the same ground with commands.

## 1. Create — on your laptop

Install gpumesh with the window included, then open it:

```bash
pip install "gpumesh[gui]"
gpumesh gui
```

A dashboard opens in your browser, on this computer only. A tray icon
appears by the clock when the optional tray backend is installed; without
it you get the same page with every control on it, so nothing is missing.

The page has three steps on it. Press **Start sharing this computer** in
step 1. (The tray menu's **Create mesh** does the same thing, as does
`gpumesh serve --host 0.0.0.0` in a terminal.) This binds your local
network so the other laptops can reach you; the terminal shows the usual
exposure warning, worth one read. The page then lists your laptop and,
in step 2, shows a 6-digit code. That code is good for one join and five
minutes: **New code** mints a fresh one and **Copy code** puts it on the
clipboard. Press **Stop sharing** on this laptop when you are done.

## 2. Join — on each other laptop

On the second laptop, run `gpumesh gui` the same way. Step 3 lists any
meshes it hears on your network — pick one from the list — or type the
address (`http://192.168.1.10:8000`) into the box. Typing beats the
picker, so an address you enter is always the one used; if the list is
empty and you know the mesh is there, press **Scan again**. Enter the
6-digit code from the first laptop and press **Join this mesh**. The
worker starts in the background and the page confirms it. Repeat for the
third laptop. No keys are ever typed or displayed anywhere in this flow.

If a join fails, check two things before anything else: both laptops are
on the same Wi-Fi, and the code is fresh (mint a new one). See
[Troubleshooting](troubleshooting.md) if it still refuses.

> Joining means trusting. A machine in your mesh can run code on every
> other machine in it, and the other way around. Only mesh laptops you
> own with people you trust, on a network you trust.

## 3. Demo sweep — watch the work spread

On any one of the three laptops, run the demo sweep:

```bash
python examples/gui_demo_sweep.py
```

(From a clone of the repository. It needs nothing but Python — no torch,
no numpy.) Six small tasks go out to the pool, and each line of output
names the machine that ran that task:

```console
[gui-demo] pool: 3 device(s), sweeping 6 configs
[gui-demo] task 0: size=20000 checksum=... worker=laptop-a
[gui-demo] task 1: size=25000 checksum=... worker=laptop-b
...
[gui-demo] 6 tasks, 3 machine(s) worked as one: laptop-a, laptop-b, laptop-c
```

That last line is the payoff: three machines, one result list. Run the
same script with no mesh at all and it still prints the same answers,
every task naming the laptop in front of you. Your script never breaks
because the mesh went away; it just gets slower.

## Where to go next

- The terminal version of all of this: [Quickstart](../README.md#quickstart).
- What each command does: [CLI reference](cli.md).
- The same sweep as real code to copy from:
  [`examples/param_sweep.py`](../examples/param_sweep.py).
- When something misbehaves: [Troubleshooting](troubleshooting.md).
