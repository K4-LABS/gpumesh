"""Opt-in TLS for the coordinator's HTTP listener.

gpumesh speaks plain HTTP by default and that default is not changing here.
This module adds `gpumesh serve --tls`, which wraps the listener in TLS using
a self-signed certificate generated on first use.

## What --tls is for, and what it is not for

A self-signed certificate has no chain to a public root, so nothing verifies
*who* the coordinator is unless the operator moves the certificate to the
worker by hand. What it does buy, unconditionally, is confidentiality and
integrity on the wire: on a LAN with `--tls`, the shared token no longer
travels in cleartext where anyone on the same Wi-Fi can read it out of a
capture, and a submitted function's pickled bytes can no longer be rewritten
in flight by whoever is between the two machines.

So: `--tls` closes the passive-eavesdropper hole on a LAN. It does **not**
make gpumesh safe to expose to the internet. For anything crossing a network
you do not control, tunnel it -- `--tailscale` or `--public` (ngrok) -- and
let the tunnel be the boundary. Plain HTTP should be read as LAN-only, and
LAN-only should be read as "a network whose other occupants you trust".

## Trusting the certificate from a worker

Three options, in descending order of how much they are worth:

1. Copy the coordinator's `coordinator-cert.pem` to each worker and point
   `GPUMESH_TLS_CA` at it. The worker then verifies the coordinator, and the
   fingerprint printed at startup is what it is verifying against.
2. Pass `--tls-cert`/`--tls-key` with a certificate from a real CA (including
   an internal one, or Tailscale's `tailscale cert`). Nothing else to do.
3. `GPUMESH_TLS_INSECURE=1` -- encrypted, unauthenticated. An active attacker
   on the path can still substitute their own certificate and read
   everything. This is strictly better than plain HTTP and strictly worse
   than either option above.
"""

import datetime
import hashlib
import os
import pathlib
import socket
import ssl
import subprocess
import sys

# Certificates live beside the rest of gpumesh's per-user state so that the
# 0600/0700 story is the same one told for ~/.gpumesh/config.json.
DEFAULT_TLS_DIR = pathlib.Path.home() / ".gpumesh" / "tls"
CERT_NAME = "coordinator-cert.pem"
KEY_NAME = "coordinator-key.pem"

# Long enough that a home lab is not regenerating certificates every quarter,
# short enough that a key left behind on a decommissioned box stops working.
CERT_VALID_DAYS = 825

# Regenerate this far ahead of expiry rather than at expiry, so a coordinator
# that is restarted weekly never serves a certificate that dies mid-session.
CERT_RENEW_MARGIN_DAYS = 30


class TLSError(RuntimeError):
    """Raised when TLS was asked for and cannot be provided."""


def _san_names() -> list[str]:
    """Names to put in the certificate's SAN, best effort.

    A worker connects by IP far more often than by name, and a certificate
    with no matching SAN entry fails verification even when the operator did
    copy the CA across. So every local address this host can see goes in.
    Failures here are not fatal: a certificate with fewer names still
    encrypts, it just narrows which URLs can be verified.
    """
    names = ["localhost"]
    addresses = ["127.0.0.1", "::1"]
    try:
        hostname = socket.gethostname()
        if hostname:
            names.append(hostname)
        for info in socket.getaddrinfo(hostname, None):
            addr = info[4][0]
            if addr not in addresses:
                addresses.append(addr)
    except OSError:
        pass
    return names + addresses


def _generate_with_cryptography(certfile: pathlib.Path,
                                keyfile: pathlib.Path) -> bool:
    """Write a self-signed cert/key pair using `cryptography`.

    Returns False if the library is not installed, so the caller can fall
    back. Any other failure raises -- a half-written certificate is worse
    than a missing one.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return False

    import ipaddress as _ipaddress

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "gpumesh coordinator"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "gpumesh (self-signed)"),
    ])

    alt_names = []
    for name in _san_names():
        try:
            alt_names.append(x509.IPAddress(_ipaddress.ip_address(name)))
        except ValueError:
            alt_names.append(x509.DNSName(name))

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=CERT_VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        .sign(key, hashes.SHA256())
    )

    keyfile.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return True


def _generate_with_openssl(certfile: pathlib.Path,
                           keyfile: pathlib.Path) -> bool:
    """Fall back to the `openssl` binary. Returns False if it is not there."""
    san = ",".join(
        f"IP:{n}" if n.replace(".", "").replace(":", "").isalnum()
        and any(c in n for c in ".:") and not n[0].isalpha()
        else f"DNS:{n}"
        for n in _san_names()
    )
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(keyfile), "-out", str(certfile),
        "-days", str(CERT_VALID_DAYS),
        "-subj", "/CN=gpumesh coordinator/O=gpumesh (self-signed)",
        "-addext", f"subjectAltName={san}",
    ]
    try:
        # S603 is suppressed below, with the reason recorded here rather than
        # silently passed. Every element of `cmd` above is a literal, or is
        # derived from this machine's own
        # hostname and interface addresses via `_san_names`. Nothing here
        # comes off the network, out of a job payload, or from a submitted
        # function. There is no shell (`shell=False`, the default), so the
        # SAN string cannot break out of its argument even on a host whose
        # name contains shell metacharacters.
        proc = subprocess.run(cmd, capture_output=True, timeout=60)  # noqa: S603
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        raise TLSError(
            "openssl failed to generate a self-signed certificate: "
            + (proc.stderr.decode(errors="replace").strip() or "no output")
        )
    return True


def _restrict(path: pathlib.Path) -> None:
    """Best-effort 0600. A no-op on Windows, where chmod does not mean this."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _expires_soon(certfile: pathlib.Path) -> bool:
    """True if the certificate is missing, unreadable, or near expiry.

    Reading the expiry is best effort by design. This function decides only
    whether to *regenerate*, so being unable to answer must degrade to
    "leave the working certificate alone" rather than to an exception out of
    ``ensure_self_signed_cert`` -- a coordinator refusing to start because it
    could not read its own certificate's dates would be a worse failure than
    the aged-out certificate it was trying to prevent.

    ``not_valid_after_utc`` landed in cryptography 42; 41.x has only the
    deprecated naive ``not_valid_after``, which is UTC without saying so.
    Both are handled, so the ``tls`` extra's floor stays where it is.
    """
    try:
        from cryptography import x509
    except ImportError:
        return False  # Cannot tell; leave what is there alone.
    try:
        cert = x509.load_pem_x509_certificate(certfile.read_bytes())
    except Exception:
        return True  # Unreadable is as good as absent.
    try:
        not_after = getattr(cert, "not_valid_after_utc", None)
        if not_after is None:
            naive = cert.not_valid_after
            not_after = naive.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return False
    margin = datetime.timedelta(days=CERT_RENEW_MARGIN_DAYS)
    return not_after - margin <= datetime.datetime.now(datetime.timezone.utc)


def ensure_self_signed_cert(tls_dir=None, force: bool = False):
    """Return (certfile, keyfile), generating a self-signed pair if needed.

    Idempotent: an existing, non-expiring pair is reused, so workers that
    pinned the fingerprint keep working across coordinator restarts.
    """
    tls_dir = pathlib.Path(tls_dir) if tls_dir else DEFAULT_TLS_DIR
    tls_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(tls_dir, 0o700)
    except OSError:
        pass

    certfile = tls_dir / CERT_NAME
    keyfile = tls_dir / KEY_NAME

    have_both = certfile.exists() and keyfile.exists()
    if have_both and not force and not _expires_soon(certfile):
        return certfile, keyfile

    if not (_generate_with_cryptography(certfile, keyfile)
            or _generate_with_openssl(certfile, keyfile)):
        raise TLSError(
            "--tls needs a way to make a self-signed certificate, and this "
            "machine has neither. Fix, in order of preference:\n"
            "  pip install 'gpumesh[tls]'   (installs cryptography)\n"
            "  install the openssl command-line tool\n"
            "  pass --tls-cert/--tls-key with a certificate you already have"
        )

    _restrict(keyfile)
    return certfile, keyfile


def fingerprint(certfile) -> str:
    """SHA-256 fingerprint of a PEM certificate, colon-separated hex.

    Printed at coordinator startup so an operator can read it to whoever is
    joining and have them confirm it. That out-of-band check is the only
    thing that turns a self-signed certificate into an identity.
    """
    pem = pathlib.Path(certfile).read_bytes()
    der = ssl.PEM_cert_to_DER_cert(pem.decode("ascii"))
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def server_context(certfile, keyfile) -> ssl.SSLContext:
    """Build the coordinator's server-side SSLContext.

    TLS 1.2 is the floor. Anything older is broken in ways that would make
    the flag a lie, and every Python gpumesh supports can speak 1.2.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    except (ssl.SSLError, OSError) as exc:
        raise TLSError(
            f"could not load the TLS certificate at {certfile}: {exc}"
        ) from exc
    return context


def wrap_server(httpd, certfile, keyfile):
    """Wrap a live HTTPServer's socket in TLS. Returns the SSLContext used."""
    context = server_context(certfile, keyfile)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    httpd.gpumesh_tls_context = context
    httpd.gpumesh_tls_certfile = str(certfile)
    return context


def client_context(url: str):
    """SSLContext for talking to a coordinator, or None for plain HTTP.

    Reads two environment variables, both opt-in:

      GPUMESH_TLS_CA        path to the coordinator's certificate (or a CA
                            bundle). Verification on, against that file.
      GPUMESH_TLS_INSECURE  "1"/"true"/"yes" -- encrypt but do not verify.

    With neither set, the system trust store is used, which is correct for a
    real certificate and will (correctly) refuse a self-signed one. The
    resulting error names both variables, because "certificate verify failed"
    on its own has sent a lot of people to the wrong fix.
    """
    if not url.lower().startswith("https://"):
        return None

    ca = (os.environ.get("GPUMESH_TLS_CA") or "").strip()
    insecure = (os.environ.get("GPUMESH_TLS_INSECURE") or "").strip().lower()

    if insecure in ("1", "true", "yes", "on"):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    if ca:
        if not pathlib.Path(ca).exists():
            raise TLSError(
                f"GPUMESH_TLS_CA points at {ca!r}, which does not exist. "
                "Copy the coordinator's certificate to this machine, or "
                "unset the variable."
            )
        context = ssl.create_default_context(cafile=ca)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def explain_verify_failure(exc) -> str:
    """Turn an SSL verification error into an instruction, or return "".

    Called from the client's error path. A self-signed coordinator is the
    overwhelmingly likely cause of a verify failure in this project, and the
    fix is two lines that nobody guesses from the stock message.
    """
    text = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" not in text and "certificate verify" not in text:
        return ""
    return (
        "The coordinator is using TLS with a certificate this machine does "
        "not trust -- almost certainly the self-signed one that "
        "`gpumesh serve --tls` generates. Either:\n"
        "  copy the coordinator's certificate here and set "
        "GPUMESH_TLS_CA=/path/to/coordinator-cert.pem   (verified), or\n"
        "  set GPUMESH_TLS_INSECURE=1                    (encrypted, "
        "unverified -- an attacker on the path can still impersonate the "
        "coordinator)"
    )


if __name__ == "__main__":  # pragma: no cover - operator convenience
    cert, key = ensure_self_signed_cert(force="--force" in sys.argv)
    print(f"certificate: {cert}")
    print(f"private key: {key}")
    print(f"SHA-256:     {fingerprint(cert)}")
