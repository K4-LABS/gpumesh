# Security Policy

## Supported versions

Only the latest release is supported. Fixes land on `main` and ship in the
next release; please upgrade to the newest version before reporting an issue.

## Reporting a vulnerability

**Do not open a public issue for a security problem.** Use GitHub's private
vulnerability reporting instead:

1. Open the **Security** tab on this repository.
2. Click **Report a vulnerability**.
3. Describe the problem. You will get a response (usually within a few days)
   and can follow up privately from there.

Please include:

- The gpumesh version (`gpumesh --version`) and Python version
- What you did, what you expected, and what happened instead
- A minimal reproduction if you can make one
- Whether you believe the issue is exploitable across a network

## What gpumesh's threat model is

These are documented properties of the design, not vulnerabilities:

- **No transport encryption on a plain LAN.** Use `--tailscale` or `--public`
  (ngrok) when traffic crosses a network you do not control.
- **Workers execute code sent by the coordinator.** Anyone holding your URL
  and token can run arbitrary code on every machine in your mesh. gpumesh is
  built for trusted networks and is not a sandbox. Treat the token like a
  password.
- **Token authentication is the only gate.** The token is compared in
  constant time, rate-limited per IP, and never written to the database, but
  a leaked token is full access.

## What counts as a vulnerability

- A way to bypass token authentication
- A way to make a worker execute code from an unauthenticated source
- A way to read another mesh's data or tasks without its token

Reports that merely restate the known threat model above are not
vulnerabilities.
