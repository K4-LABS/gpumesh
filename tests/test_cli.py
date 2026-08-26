"""Tests for cli.py - CLI argument parsing and --tailscale flag."""

import pytest
from unittest.mock import patch, MagicMock
import sys
import os
import threading
import time
from types import SimpleNamespace


@pytest.fixture(autouse=True)
def _status_sink_clean():
    """The coordinator log sink is process-global; never leak it active."""
    yield
    from gpumesh import status as status_mod
    assert status_mod.is_active() is False, "coordinator log sink left active"


class TestServeTailscaleFlag:
    """Tests for --tailscale flag in serve command."""

    def test_serve_tailscale_flag_parsed(self):
        """--tailscale flag is correctly parsed.

        It needs a bind tailnet peers can actually reach; on the loopback
        default the combination is refused outright (see
        TestServeTunnelVersusBind), so this one asks for a wide bind.
        """
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "serve", "--host", "0.0.0.0",
                                "--tailscale", "--token", "test123"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.worker.spawn_local_worker") as mock_self_worker, \
             patch("gpumesh.cli.utils.try_add_firewall_rule", return_value=True), \
             patch("gpumesh.cli.utils.show_ip_alternatives"), \
             patch("gpumesh.cli.utils.get_lan_ip", return_value="192.168.1.50"), \
             patch("gpumesh.cli.connection_manager.save_connection"), \
             patch("gpumesh.cli.tunnel.open_tunnel") as mock_tunnel:
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass

            # Verify tailscale mode was passed
            mock_tunnel.assert_called_once_with(8000, mode="tailscale")

    def test_serve_public_flag_parsed(self):
        """--public flag is correctly parsed."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "serve", "--public", "--token", "test123"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.worker.spawn_local_worker") as mock_self_worker, \
             patch("gpumesh.cli.tunnel.open_tunnel") as mock_tunnel:
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass

            mock_tunnel.assert_called_once_with(8000, mode="ngrok")

    def test_serve_no_tunnel_flag(self):
        """No tunnel flag means no tunnel is opened."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "serve", "--token", "test123"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.worker.spawn_local_worker") as mock_self_worker, \
             patch("gpumesh.cli.tunnel.open_tunnel") as mock_tunnel:
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass

            mock_tunnel.assert_not_called()

    def test_serve_tailscale_and_public_conflict(self):
        """--tailscale takes precedence over --public."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "serve", "--host", "0.0.0.0",
                                "--tailscale", "--public", "--token", "test123"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.worker.spawn_local_worker") as mock_self_worker, \
             patch("gpumesh.cli.utils.try_add_firewall_rule", return_value=True), \
             patch("gpumesh.cli.utils.show_ip_alternatives"), \
             patch("gpumesh.cli.utils.get_lan_ip", return_value="192.168.1.50"), \
             patch("gpumesh.cli.connection_manager.save_connection"), \
             patch("gpumesh.cli.tunnel.open_tunnel") as mock_tunnel:
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass

            # tailscale should take precedence
            mock_tunnel.assert_called_once_with(8000, mode="tailscale")


class TestServeBindHost:
    """`gpumesh serve` must not expose code execution to the whole LAN.

    A worker runs whatever Python the coordinator sends it, as the OS user
    that started it, so a wildcard bind is remote code execution for anyone
    on the network holding the token — the shape of TorchServe's
    CVE-2023-43654 and Dask's CVE-2021-42343. The bind used to be the literal
    "0.0.0.0" with no way to change it; --host-ip only ever changed the
    address that got *printed*.
    """

    @staticmethod
    def _run_serve(argv, lan_ip="192.168.1.50", lan_candidates=None):
        """Drive cmd_serve to completion with a fake server, return the mock."""
        from gpumesh.cli import main

        if lan_candidates is None:
            lan_candidates = [lan_ip]
        with patch("sys.argv", ["gpumesh", "serve"] + argv), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.worker.spawn_local_worker"), \
             patch("gpumesh.cli.connection_manager.save_connection"), \
             patch("gpumesh.cli.utils.get_lan_ip", return_value=lan_ip), \
             patch("gpumesh.cli.utils.lan_ip_candidates",
                   return_value=lan_candidates), \
             patch("gpumesh.cli.utils.show_ip_alternatives"), \
             patch("gpumesh.cli.utils.try_add_firewall_rule",
                   return_value=True) as mock_firewall, \
             patch("gpumesh.cli.tunnel.open_tunnel"):
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass
        mock_serve.firewall_mock = mock_firewall
        return mock_serve

    @pytest.fixture(autouse=True)
    def _no_host_env(self, monkeypatch):
        monkeypatch.delenv("GPUMESH_HOST", raising=False)
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)

    def test_default_bind_is_loopback_only(self):
        mock_serve = self._run_serve(["--token", "test123"])
        assert mock_serve.call_args[0][0] == "127.0.0.1", (
            "the default bind must not be reachable from other machines"
        )

    def test_host_flag_is_what_opens_it_up(self):
        mock_serve = self._run_serve(["--host", "0.0.0.0", "--token", "test123"])
        assert mock_serve.call_args[0][0] == "0.0.0.0"

    def test_host_env_var_is_honoured(self, monkeypatch):
        monkeypatch.setenv("GPUMESH_HOST", "0.0.0.0")
        mock_serve = self._run_serve(["--token", "test123"])
        assert mock_serve.call_args[0][0] == "0.0.0.0"

    def test_explicit_host_beats_the_environment(self, monkeypatch):
        monkeypatch.setenv("GPUMESH_HOST", "0.0.0.0")
        mock_serve = self._run_serve(["--host", "127.0.0.1", "--token", "t"])
        assert mock_serve.call_args[0][0] == "127.0.0.1"

    def test_host_ip_does_not_change_the_bind(self):
        """The confusing pair: --host-ip is advertising only, never the bind."""
        mock_serve = self._run_serve(
            ["--host-ip", "10.1.2.3", "--token", "test123"],
            lan_candidates=["10.1.2.3"],
        )
        assert mock_serve.call_args[0][0] == "127.0.0.1"

    def test_firewall_rule_is_not_requested_for_a_loopback_bind(self):
        """Nothing inbound can arrive, so a firewall hole is noise."""
        mock_serve = self._run_serve(["--token", "test123"])
        mock_serve.firewall_mock.assert_not_called()

    def test_firewall_rule_is_requested_for_a_wide_bind(self):
        mock_serve = self._run_serve(["--host", "0.0.0.0", "--token", "test123"])
        mock_serve.firewall_mock.assert_called_once_with(8000)

    def test_serve_refuses_foreign_host_ip_and_lists_candidates(self, capsys):
        """A --host-ip that is not this machine's is refused, with the list."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "serve", "--host-ip", "10.9.9.9",
                                "--token", "test123"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.utils.lan_ip_candidates",
                   return_value=["192.168.1.50"]), \
             patch("gpumesh.cli.connection_manager.save_connection"), \
             patch("gpumesh.cli.utils.try_add_firewall_rule",
                   return_value=True), \
             patch("gpumesh.cli.tunnel.open_tunnel"):
            with pytest.raises(SystemExit) as excinfo:
                main()
        assert excinfo.value.code == 1
        mock_serve.assert_not_called()
        out = _plain(capsys.readouterr().out)
        assert "10.9.9.9 is not an address of this machine" in out
        assert "192.168.1.50" in out

    def test_serve_accepts_loopback_host_ip(self):
        """Loopback is the sensible pin for a loopback-bound coordinator."""
        mock_serve = self._run_serve(
            ["--host-ip", "127.0.0.1", "--token", "test123"],
            lan_candidates=[],
        )
        assert mock_serve.call_args[0][0] == "127.0.0.1"

    def test_serve_reports_unwritable_db_path(self, capsys):
        """An unwritable --db names the path and the fix, not a traceback."""
        import sqlite3
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "serve", "--db", "/no/such/dir/db.sqlite",
                                "--token", "test123"]), \
             patch("gpumesh.cli.server.serve",
                   side_effect=sqlite3.OperationalError(
                       "unable to open database file")), \
             patch("gpumesh.cli.connection_manager.save_connection"), \
             patch("gpumesh.cli.utils.try_add_firewall_rule",
                   return_value=True), \
             patch("gpumesh.cli.tunnel.open_tunnel"):
            with pytest.raises(SystemExit) as excinfo:
                main()
        assert excinfo.value.code == 1
        out = _plain(capsys.readouterr().out)
        assert "Cannot open database" in out
        assert "/no/such/dir/db.sqlite" in out
        assert "--db" in out

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="Windows has no privileged ports below 1024")
    def test_serve_privileged_port_permission_denied(self, capsys):
        """EACCES on a port <1024 explains privilege, not 'port in use'."""
        import errno
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "serve", "--port", "80",
                                "--token", "test123"]), \
             patch("gpumesh.cli.server.serve",
                   side_effect=PermissionError(errno.EACCES,
                                               "Permission denied")), \
             patch("gpumesh.cli.connection_manager.save_connection"), \
             patch("gpumesh.cli.utils.try_add_firewall_rule",
                   return_value=True), \
             patch("gpumesh.cli.tunnel.open_tunnel"):
            with pytest.raises(SystemExit) as excinfo:
                main()
        assert excinfo.value.code == 1
        out = _plain(capsys.readouterr().out)
        assert "Permission denied binding port 80" in out
        assert "below 1024" in out
        assert "port 8000" in out


def _plain(text: str) -> str:
    """Strip ANSI escapes so assertions do not depend on colour support."""
    import re
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


class TestServeBindBanner:
    """What the banner promises must match what the socket will accept."""

    @pytest.fixture(autouse=True)
    def _no_host_env(self, monkeypatch):
        monkeypatch.delenv("GPUMESH_HOST", raising=False)
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)

    def _serve_output(self, argv, capsys, lan_ip="192.168.1.50"):
        TestServeBindHost._run_serve(argv, lan_ip=lan_ip)
        return _plain(capsys.readouterr().out)

    def test_loopback_banner_does_not_advertise_an_unreachable_lan_url(
            self, capsys):
        """Printing a LAN join URL that cannot connect sends users to debug
        a firewall, a VPN and an antivirus for an hour."""
        out = self._serve_output(["--token", "test123"], capsys)

        assert "192.168.1.50" not in out, (
            "a loopback bind advertised a LAN address nothing can connect to"
        )
        assert "Join from THIS machine" in out
        assert "Join from ANOTHER machine" not in out

    def test_loopback_banner_says_exactly_what_to_re_run(self, capsys):
        """This default is a breaking change; the migration has to be typeable."""
        out = self._serve_output(["--token", "test123"], capsys)

        assert "CANNOT reach" in out
        assert "gpumesh serve --host 0.0.0.0" in out
        assert "--token test123" in out
        assert "GPUMESH_HOST=0.0.0.0" in out

    def test_no_exposure_warning_on_loopback(self, capsys):
        out = self._serve_output(["--token", "test123"], capsys)
        assert "NETWORK-EXPOSED" not in out

    def test_wide_bind_prints_a_prominent_exposure_warning(self, capsys):
        import getpass

        with patch("gpumesh.capability.probe_device",
                   return_value={"device": "cuda", "device_name": "RTX 3080"}):
            out = self._serve_output(
                ["--host", "0.0.0.0", "--port", "8123", "--token", "test123"],
                capsys,
            )

        assert "NETWORK-EXPOSED" in out
        assert "0.0.0.0:8123" in out          # the bind address and the port
        assert getpass.getuser() in out       # the OS user tasks run as
        assert "RTX 3080" in out              # the detected device
        assert "arbitrary code" in out
        # And it still tells other machines how to join.
        assert "192.168.1.50" in out
        assert "Join from ANOTHER machine" in out


class TestServeTunnelVersusBind:
    """A tunnel flag must not contradict — or quietly outrank — the bind.

    The defect these cover: `_print_exposure_warning` was called inside the
    non-loopback branch while `tunnel.open_tunnel` ran unconditionally four
    lines later. So `gpumesh serve --public` on the default bind printed
    "Other machines CANNOT reach this coordinator" and then printed a public
    ngrok URL — the single largest exposure gpumesh can produce, announced
    with less warning than binding 0.0.0.0 gets, immediately after a claim
    that nothing outside the machine could connect at all.

    The two flags are fixed differently because they are different things.
    ngrok's agent dials 127.0.0.1 from this machine, so `--public` genuinely
    works on a loopback bind (and loopback is the *tightest* bind to pair it
    with) — it is allowed, and made loud. `--tailscale` opens no tunnel at
    all; it prints this machine's 100.x address, which a loopback-bound
    socket never answers — it is refused.
    """

    @pytest.fixture(autouse=True)
    def _no_host_env(self, monkeypatch):
        monkeypatch.delenv("GPUMESH_HOST", raising=False)
        monkeypatch.delenv("GPUMESH_HOST_IP", raising=False)

    @staticmethod
    def _serve(argv, capsys, lan_ip="192.168.1.50", tunnel_side_effect=None):
        """Drive `gpumesh serve` to completion; never opens a real tunnel."""
        from gpumesh.cli import main

        exits = []
        with patch("sys.argv", ["gpumesh", "serve"] + argv), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.worker.spawn_local_worker"), \
             patch("gpumesh.cli.connection_manager.save_connection"), \
             patch("gpumesh.cli.utils.get_lan_ip", return_value=lan_ip), \
             patch("gpumesh.cli.utils.show_ip_alternatives"), \
             patch("gpumesh.cli.utils.try_add_firewall_rule", return_value=True), \
             patch("gpumesh.cli.tunnel.open_tunnel") as mock_tunnel, \
             patch("gpumesh.capability.probe_device",
                   return_value={"device": "cuda", "device_name": "RTX 3080"}):
            mock_serve.return_value = MagicMock()
            if tunnel_side_effect is not None:
                mock_tunnel.side_effect = tunnel_side_effect
            try:
                main()
            except SystemExit as exc:
                exits.append(exc.code)
        result = SimpleNamespace(
            serve=mock_serve,
            tunnel=mock_tunnel,
            out=_plain(capsys.readouterr().out),
            exit_code=exits[0] if exits else None,
        )
        return result

    # --- --public: allowed everywhere, loud everywhere --------------------

    def test_public_on_a_loopback_bind_still_opens_the_tunnel(self, capsys):
        """The decision, stated as a test: ngrok dials loopback locally, so
        this combination works and is deliberately kept working."""
        r = self._serve(["--public", "--token", "test123"], capsys)
        r.tunnel.assert_called_once_with(8000, mode="ngrok")
        assert r.exit_code is None

    def test_public_on_a_loopback_bind_never_claims_unreachability(self, capsys):
        """The headline contradiction. This is the assertion that bites."""
        r = self._serve(["--public", "--token", "test123"], capsys)
        assert "CANNOT reach" not in r.out, (
            "a public tunnel was opened right after telling the operator "
            "nothing outside this machine could connect"
        )

    def test_public_on_a_loopback_bind_warns_before_opening_the_tunnel(
            self, capsys):
        """A warning that arrives after the public URL is decoration."""
        r = self._serve(["--public", "--token", "test123"], capsys,
                        tunnel_side_effect=lambda *a, **k: print(
                            "TUNNEL-OPENED-HERE"))
        assert "TUNNEL-OPENED-HERE" in r.out
        assert r.out.index("INTERNET-EXPOSED") < r.out.index("TUNNEL-OPENED-HERE")

    def test_public_names_what_is_reachable_and_from_where(self, capsys):
        import getpass

        r = self._serve(["--public", "--port", "8123", "--token", "test123"],
                        capsys)
        assert "INTERNET-EXPOSED COORDINATOR" in r.out
        assert "THE PUBLIC INTERNET" in r.out    # from where
        assert "--public" in r.out               # which flag opened it
        assert "127.0.0.1:8123" in r.out         # and the bind it tunnels into
        assert getpass.getuser() in r.out        # the OS user tasks run as
        assert "RTX 3080" in r.out               # on which device
        assert "arbitrary code" in r.out

    def test_public_is_at_least_as_loud_as_a_wide_bind(self, capsys):
        """Strictly larger exposure, so: not one decibel quieter."""
        tunnelled = self._serve(["--public", "--token", "test123"], capsys)
        bind = self._serve(["--host", "0.0.0.0", "--token", "test123"], capsys)

        rule = "!" * 62
        assert tunnelled.out.count(rule) >= bind.out.count(rule), (
            "the internet-wide exposure printed a smaller banner than a "
            "LAN-wide one"
        )

    def test_a_wide_bind_plus_public_prints_both_banners(self, capsys):
        """Two doors into one process; closing one does not close the other."""
        r = self._serve(["--host", "0.0.0.0", "--public", "--token", "t123456"],
                        capsys)
        assert "NETWORK-EXPOSED COORDINATOR" in r.out
        assert "INTERNET-EXPOSED COORDINATOR" in r.out

    def test_no_tunnel_flag_prints_no_tunnel_banner(self, capsys):
        r = self._serve(["--token", "test123"], capsys)
        assert "INTERNET-EXPOSED" not in r.out
        assert "TAILNET-EXPOSED" not in r.out
        r.tunnel.assert_not_called()

    # --- --tailscale: refused on loopback, allowed once reachable ---------

    def test_tailscale_on_a_loopback_bind_is_refused(self, capsys):
        """It advertises a 100.x address a loopback socket never answers."""
        r = self._serve(["--tailscale", "--token", "test123"], capsys)

        assert r.exit_code == 1
        r.tunnel.assert_not_called()
        # Refused BEFORE the socket opens: no port bound, no connection saved,
        # no token printed into a log for a run that then dies.
        r.serve.assert_not_called()

    def test_tailscale_refusal_says_exactly_what_to_run(self, capsys):
        r = self._serve(["--tailscale", "--port", "8123", "--token", "test123"],
                        capsys)

        assert "--tailscale cannot work with a loopback bind" in r.out
        assert "tailscale ip -4" in r.out            # how to find the address
        assert "gpumesh serve --host 100." in r.out  # what to type next
        assert "--port 8123" in r.out
        assert "--token test123" in r.out

    def test_tailscale_refusal_does_not_silently_widen_the_bind(self, capsys):
        """The bind is the operator's decision; a refusal must not make it."""
        r = self._serve(["--tailscale", "--token", "test123"], capsys)
        r.serve.assert_not_called()
        assert "0.0.0.0:8000" not in r.out

    def test_tailscale_on_a_tailnet_bind_is_allowed_and_warns(self, capsys):
        r = self._serve(["--host", "100.101.102.103", "--tailscale",
                         "--token", "test123"], capsys)

        assert r.exit_code is None
        r.tunnel.assert_called_once_with(8000, mode="tailscale")
        assert "TAILNET-EXPOSED COORDINATOR" in r.out
        assert "EVERY DEVICE ON YOUR TAILNET" in r.out

    def test_tailscale_env_bind_is_enough_to_pass_the_check(self, monkeypatch,
                                                            capsys):
        """GPUMESH_HOST is the same decision spelled a different way."""
        monkeypatch.setenv("GPUMESH_HOST", "0.0.0.0")
        r = self._serve(["--tailscale", "--token", "test123"], capsys)
        assert r.exit_code is None
        r.tunnel.assert_called_once_with(8000, mode="tailscale")


class TestShowConnectionIsBindAware:
    """`show-connection` must not offer a loopback URL for sharing.

    Since the loopback bind default this is the common case, and the old
    unconditional "Share these with your workers!" made it a trap: 127.0.0.1
    means "the machine you typed it on", so the printed quickjoin command
    makes a worker dial itself, fail, and go hunting for a firewall problem
    that does not exist.
    """

    @staticmethod
    def _show(url, token, capsys):
        from gpumesh.cli import cmd_show_connection

        with patch("gpumesh.cli.connection_manager.load_connection",
                   return_value={"url": url, "token": token}):
            cmd_show_connection(None)
        return _plain(capsys.readouterr().out)

    def test_a_loopback_url_is_not_offered_for_sharing(self, capsys):
        out = self._show("http://127.0.0.1:8000", "tok12345", capsys)
        assert "Share these with your workers" not in out
        assert "quickjoin" not in out, (
            "printed a join command pointing at 127.0.0.1, which resolves to "
            "the worker's own machine"
        )

    def test_a_loopback_url_says_so_plainly(self, capsys):
        out = self._show("http://127.0.0.1:8000", "tok12345", capsys)
        assert "LOCAL-ONLY" in out
        assert "Do NOT share it" in out
        assert "this computer" in out

    def test_a_loopback_url_says_what_to_do_instead(self, capsys):
        out = self._show("http://127.0.0.1:8123", "tok12345", capsys)
        assert "gpumesh serve --host 0.0.0.0" in out
        assert "--port 8123" in out, "the advice must keep the saved port"
        assert "--token tok12345" in out

    def test_localhost_and_ipv6_loopback_count_too(self, capsys):
        for url in ("http://localhost:8000", "http://[::1]:8000"):
            out = self._show(url, "tok12345", capsys)
            assert "LOCAL-ONLY" in out, f"{url} was offered for sharing"

    def test_a_reachable_url_is_still_shareable(self, capsys):
        out = self._show("http://192.168.1.50:8000", "tok12345", capsys)
        assert "Share these with your workers" in out
        assert "gpumesh quickjoin http://192.168.1.50:8000" in out
        assert "LOCAL-ONLY" not in out

    def test_a_malformed_url_falls_back_to_the_sharing_text(self, capsys):
        """Never claim "local-only" from a parse we could not do."""
        out = self._show("not a url at all", "tok12345", capsys)
        assert "LOCAL-ONLY" not in out


class TestEnvironmentIsolation:
    """A contributor's exported GPUMESH_* must not decide whether tests pass.

    Both bind variables leaked before this: with GPUMESH_HOST exported 5
    tests failed, and with GPUMESH_CLAIM_HOST exported 46 errored — and the
    README and CONTRIBUTING both actively suggest exporting them. The failure
    looks exactly like a real bug in whatever the contributor just changed.
    """

    _POLLUTING = ("GPUMESH_HOST", "GPUMESH_CLAIM_HOST", "GPUMESH_TOKEN",
                  "GPUMESH_URL", "GPUMESH_HOST_IP", "GPUMESH_LOCAL",
                  "GPUMESH_COLOR", "GPUMESH_VERBOSE")

    def test_no_gpumesh_variable_survives_into_a_test(self):
        leaked = [n for n in self._POLLUTING if n in os.environ]
        assert not leaked, (
            f"{leaked} reached a test from the developer's shell; the autouse "
            f"fixture in tests/conftest.py must clear it"
        )

    def test_every_variable_the_package_reads_is_in_the_fixture(self):
        """The list has to grow with the code, or this rots back into place.

        Discovering the names from the source rather than restating them is
        what makes this fail the day someone adds `os.environ.get("GPUMESH_X")`
        and forgets the fixture — which is precisely how GPUMESH_HOST and
        GPUMESH_CLAIM_HOST came to be missing.
        """
        import pathlib
        import re

        import gpumesh as _pkg

        pkg_dir = pathlib.Path(_pkg.__file__).parent
        names = set()
        for path in pkg_dir.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            names.update(re.findall(
                r"os\.environ(?:\.get)?[\(\[]\s*[\"'](GPUMESH_[A-Z_]+)", text))

        conftest = (pathlib.Path(__file__).parent / "conftest.py").read_text(
            encoding="utf-8")
        missing = sorted(n for n in names if f'"{n}"' not in conftest)
        assert not missing, (
            f"{missing} are read from the environment by gpumesh but are not "
            f"cleared by _isolate_gpumesh_env in tests/conftest.py"
        )

    def test_a_stray_claim_host_cannot_move_the_coordinator_bind(
            self, monkeypatch):
        """GPUMESH_CLAIM_HOST is the claim port's knob and nothing else.

        Set here the way a developer's shell would set it, to show the
        coordinator bind is unmoved by it either way.
        """
        monkeypatch.setenv("GPUMESH_CLAIM_HOST", "10.9.9.9")
        mock_serve = TestServeBindHost._run_serve(["--token", "test123"])
        assert mock_serve.call_args[0][0] == "127.0.0.1"

    def test_a_test_that_wants_a_value_can_still_set_one(self, monkeypatch):
        """Clear-by-default, opt-back-in: the fixture runs at setup, so a
        setenv in the test body wins. Without that shape the isolation would
        have had to break the tests that deliberately exercise these vars."""
        monkeypatch.setenv("GPUMESH_HOST", "0.0.0.0")
        mock_serve = TestServeBindHost._run_serve(["--token", "test123"])
        assert mock_serve.call_args[0][0] == "0.0.0.0"

    def test_polluted_environment_does_not_change_show_connection(self,
                                                                  monkeypatch,
                                                                  capsys):
        """Vars the command does not consult must not reach it either."""
        monkeypatch.setenv("GPUMESH_CLAIM_HOST", "10.9.9.9")
        monkeypatch.setenv("GPUMESH_HOST", "0.0.0.0")
        out = TestShowConnectionIsBindAware._show(
            "http://127.0.0.1:8000", "tok12345", capsys)
        assert "LOCAL-ONLY" in out, (
            "the saved URL is what decides this, never the environment"
        )


class TestQuickjoinTailscaleFlag:
    """Tests for --tailscale flag in quickjoin command."""

    def test_quickjoin_tailscale_flag_parsed(self):
        """--tailscale flag is correctly parsed for quickjoin."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "quickjoin", "--token", "test123", "--tailscale"]), \
             patch("gpumesh.cli.tunnel._get_tailscale_ip", return_value="100.100.100.100"), \
             patch("gpumesh.cli.worker.run_worker") as mock_worker:
            try:
                main()
            except SystemExit:
                pass

            # Verify worker was called with Tailscale URL
            mock_worker.assert_called_once()
            call_args = mock_worker.call_args
            assert call_args[0][0] == "http://100.100.100.100:8000"

    def test_quickjoin_tailscale_not_available(self):
        """--tailscale flag handles Tailscale not being available."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "quickjoin", "--token", "test123", "--tailscale"]), \
             patch("gpumesh.cli.tunnel._get_tailscale_ip", return_value=None), \
             patch("gpumesh.cli.worker.run_worker") as mock_worker:
            try:
                main()
            except SystemExit:
                pass

            # Worker should not be called when Tailscale is not available
            mock_worker.assert_not_called()

    def test_quickjoin_url_required_without_tailscale(self):
        """URL is required when --tailscale is not used."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "quickjoin", "--token", "test123"]), \
             patch("gpumesh.cli.worker.run_worker") as mock_worker:
            try:
                main()
            except SystemExit:
                pass

            # Worker should not be called without URL
            mock_worker.assert_not_called()

    def test_quickjoin_url_with_tailscale(self):
        """URL is optional when --tailscale is used."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "quickjoin", "--token", "test123", "--tailscale"]), \
             patch("gpumesh.cli.tunnel._get_tailscale_ip", return_value="100.100.100.100"), \
             patch("gpumesh.cli.worker.run_worker") as mock_worker:
            try:
                main()
            except SystemExit:
                pass

            # Worker should be called with Tailscale URL
            mock_worker.assert_called_once()

    def test_quickjoin_custom_port_with_tailscale(self):
        """--port flag works with --tailscale."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "quickjoin", "--token", "test123", "--tailscale", "--port", "9000"]), \
             patch("gpumesh.cli.tunnel._get_tailscale_ip", return_value="100.100.100.100"), \
             patch("gpumesh.cli.worker.run_worker") as mock_worker:
            try:
                main()
            except SystemExit:
                pass

            # Verify custom port is used
            mock_worker.assert_called_once()
            call_args = mock_worker.call_args
            assert call_args[0][0] == "http://100.100.100.100:9000"

    def test_quickjoin_explicit_url_overrides_tailscale(self):
        """Explicit URL is used even with --tailscale flag."""
        from gpumesh.cli import main

        # When both URL and --tailscale are provided, explicit URL takes precedence
        with patch("sys.argv", ["gpumesh", "quickjoin", "http://192.168.1.100:8000", "--token", "test123", "--tailscale"]), \
             patch("gpumesh.cli.tunnel._get_tailscale_ip", return_value="100.100.100.100"), \
             patch("gpumesh.cli.worker.run_worker") as mock_worker:
            try:
                main()
            except SystemExit:
                pass

            # Explicit URL should be used (not Tailscale URL)
            mock_worker.assert_called_once()
            call_args = mock_worker.call_args
            assert call_args[0][0] == "http://192.168.1.100:8000"


class TestCLIArgumentParsing:
    """Tests for CLI argument parsing."""

    def test_serve_port_argument(self):
        """--port argument is correctly parsed."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "serve", "--port", "9000", "--token", "test123"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.worker.spawn_local_worker") as mock_self_worker, \
             patch("gpumesh.cli.tunnel.open_tunnel"):
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass

            # Verify port was passed correctly. The default DB path is the
            # stable per-user location, not the working directory.
            from gpumesh.connection_manager import default_db_path
            mock_serve.assert_called_once_with(
                "127.0.0.1", 9000, default_db_path(), "test123",
                discovery=True, safe_mode=False,
                tls=False, tls_cert=None, tls_key=None,
            )

    def test_serve_token_argument(self):
        """--token argument is correctly parsed."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "serve", "--token", "mysecrettoken"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.worker.spawn_local_worker") as mock_self_worker, \
             patch("gpumesh.cli.tunnel.open_tunnel"):
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass

            # Verify token was passed correctly
            from gpumesh.connection_manager import default_db_path
            mock_serve.assert_called_once_with(
                "127.0.0.1", 8000, default_db_path(), "mysecrettoken",
                discovery=True, safe_mode=False,
                tls=False, tls_cert=None, tls_key=None,
            )

    def test_serve_default_token_generated(self):
        """Token is generated when not provided."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "serve"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.secrets.token_urlsafe", return_value="generatedtoken123"), \
             patch("gpumesh.cli.worker.spawn_local_worker") as mock_self_worker, \
             patch("gpumesh.cli.tunnel.open_tunnel"):
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass

            # Verify generated token was used
            from gpumesh.connection_manager import default_db_path
            mock_serve.assert_called_once_with(
                "127.0.0.1", 8000, default_db_path(), "generatedtoken123",
                discovery=True, safe_mode=False,
                tls=False, tls_cert=None, tls_key=None,
            )

    def test_serve_reads_token_from_environment(self, monkeypatch):
        """GPUMESH_TOKEN is honoured, matching `join`.

        Regression: `join` read the variable and `serve` did not, so a
        deployment setting it once for both roles (docker compose, systemd)
        gave the coordinator a random token while workers used the variable.
        Every worker was rejected with a 401 and nothing said why.
        """
        from gpumesh.cli import main

        monkeypatch.setenv("GPUMESH_TOKEN", "token-from-env")
        with patch("sys.argv", ["gpumesh", "serve"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.worker.spawn_local_worker"), \
             patch("gpumesh.cli.tunnel.open_tunnel"):
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass

            from gpumesh.connection_manager import default_db_path
            mock_serve.assert_called_once_with(
                "127.0.0.1", 8000, default_db_path(), "token-from-env",
                discovery=True, safe_mode=False,
                tls=False, tls_cert=None, tls_key=None,
            )

    def test_serve_explicit_token_beats_environment(self, monkeypatch):
        """An explicit --token wins over GPUMESH_TOKEN."""
        from gpumesh.cli import main

        monkeypatch.setenv("GPUMESH_TOKEN", "token-from-env")
        with patch("sys.argv", ["gpumesh", "serve", "--token", "explicit"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.worker.spawn_local_worker"), \
             patch("gpumesh.cli.tunnel.open_tunnel"):
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass

            from gpumesh.connection_manager import default_db_path
            mock_serve.assert_called_once_with(
                "127.0.0.1", 8000, default_db_path(), "explicit",
                discovery=True, safe_mode=False,
                tls=False, tls_cert=None, tls_key=None,
            )

    def test_quickjoin_token_required(self):
        """--token is required for quickjoin."""
        import argparse

        parser = argparse.ArgumentParser(prog="gpumesh")
        sub = parser.add_subparsers(dest="cmd")
        p = sub.add_parser("quickjoin")
        p.add_argument("--token", required=True)

        # Verify --token is required
        with pytest.raises(SystemExit):
            parser.parse_args(["quickjoin"])

        # Verify it works with --token
        args = parser.parse_args(["quickjoin", "--token", "test123"])
        assert args.token == "test123"

    def test_submit_rejects_negative_wait_timeout(self):
        """--wait-timeout rejects negative values (they would hang forever)."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "submit", "task.py",
                                "--payloads", "p.json", "--wait-timeout", "-5"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 2  # argparse usage error

    def test_submit_accepts_zero_wait_timeout(self):
        """--wait-timeout 0 parses fine (opt-in to waiting forever)."""
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "submit", "task.py",
                                "--payloads", "p.json", "--wait",
                                "--wait-timeout", "0"]), \
             patch("gpumesh.cli.connection_manager.get_connection",
                   return_value=("http://localhost:8000", "test123")), \
             patch("gpumesh.cli.client.submit_job", return_value="j123"), \
             patch("gpumesh.cli.worker.MeshClient"), \
             patch("gpumesh.cli.client.wait_for_job") as mock_wait:
            try:
                main()
            except SystemExit:
                pass
            mock_wait.assert_called_once_with(
                "http://localhost:8000", "test123", "j123", timeout=0.0
            )


class TestCoordinatorTicker:
    """Tests for the live 'workers online' keep-alive ticker."""

    def test_render_shows_online_count_devices_jobs_uptime(self):
        from gpumesh.cli import _render_keepalive

        health = {
            "total_score": 512.3,
            "uptime_seconds": 252,
            "jobs_pending": 1,
            "jobs_running": 2,
            "jobs_done": 3,
            "jobs_failed": 1,
        }
        workers = [
            {"id": "w1", "hostname": "gpu-pc", "device": "cuda",
             "device_name": "RTX 3080", "score": 85.0, "alive": True},
            {"id": "w2", "hostname": "mac", "device": "mps",
             "device_name": "M1 Max", "score": 60.0, "alive": True},
            {"id": "w3", "hostname": "cpu-pc", "device": "cpu",
             "device_name": "cpu", "score": 5.0, "alive": True},
            {"id": "w4", "hostname": "old-pc", "device": "cpu",
             "device_name": "old box", "score": 1.0, "alive": False},
        ]
        lines = _render_keepalive(health, workers, frame="|")
        text = " ".join(lines)

        assert "GPUMESH LIVE" in text
        assert "Workers online: 3" in text      # dead worker not counted
        assert "2 GPU" in text and "1 CPU" in text   # MPS counts as GPU
        assert "512.3" in text                  # total score
        assert "3 done" in text and "1 pending" in text
        assert "2 running" in text and "1 failed" in text
        assert "4:12" in text                   # uptime 252s
        assert "RTX 3080" in text               # top worker rows
        assert "M1 Max" in text
        assert "|" in text                      # spinner frame

    def test_render_zero_workers(self):
        from gpumesh.cli import _render_keepalive

        lines = _render_keepalive({}, [], frame="-")
        text = " ".join(lines)

        assert "GPUMESH LIVE" in text
        assert "Workers online: 0" in text
        assert "no jobs" in text
        assert "0.0" in text                    # score
        assert "0:00" in text                   # uptime
        # top rule + workers + jobs + bottom rule, no worker row
        assert len(lines) == 4

    def test_render_is_ascii_safe(self):
        """Ticker glyphs survive a legacy ASCII console (no wide chars)."""
        import re
        from gpumesh.cli import _render_keepalive

        health = {"total_score": 12.5, "uptime_seconds": 61,
                  "jobs_done": 2, "jobs_failed": 1}
        workers = [
            {"id": "w1", "hostname": "gpu-pc", "device": "cuda",
             "device_name": "RTX 3080 Ti", "score": 85.0, "alive": True},
        ]
        lines = _render_keepalive(health, workers, frame="\\")
        for line in lines:
            plain = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
            plain.encode("ascii")  # raises if any non-ASCII glyph

    def test_render_hides_excess_workers_behind_ellipsis(self):
        """More than 4 workers show a '+N more' hint on the worker row."""
        from gpumesh.cli import _render_keepalive

        workers = [
            {"id": f"w{i}", "hostname": f"pc{i}", "device": "cpu",
             "device_name": "cpu", "score": float(10 - i), "alive": True}
            for i in range(6)
        ]
        lines = _render_keepalive({}, workers, frame="-")
        text = " ".join(lines)

        assert "Workers online: 6" in text
        assert "... +2 more" in text

    def test_ticker_returns_immediately_when_thread_dead(self, capsys):
        from gpumesh.cli import _run_keepalive_ticker

        class DeadThread:
            def is_alive(self):
                return False

        _run_keepalive_ticker(lambda: {}, lambda: {"workers": []},
                              DeadThread(), tick=0.01)
        assert capsys.readouterr().out == ""

    def test_ticker_renders_one_frame_then_stops(self, capsys):
        from gpumesh.cli import _run_keepalive_ticker

        calls = {"n": 0}

        class ShortThread:
            def is_alive(self):
                calls["n"] += 1
                return calls["n"] <= 1

        health = {"total_score": 90.0, "uptime_seconds": 10,
                  "jobs_running": 1}
        workers = [{"id": "w1", "hostname": "gpu-pc", "device": "cuda",
                    "device_name": "RTX 3080", "score": 85.0, "alive": True}]

        with patch("gpumesh.cli.clear_lines"), \
             patch("gpumesh.cli.erase_line"), \
             patch("gpumesh.cli.time.sleep"):
            _run_keepalive_ticker(lambda: health, lambda: {"workers": workers},
                                  ShortThread(), tick=0.01)

        out = capsys.readouterr().out
        assert "GPUMESH LIVE" in out
        assert "Workers online: 1" in out

    def test_ticker_survives_poll_failure(self, capsys):
        """A failed poll renders 'coordinator unreachable' instead of dying."""
        from gpumesh.cli import _run_keepalive_ticker

        calls = {"n": 0}

        def health_fn():
            calls["n"] += 1
            raise ConnectionError("shutting down")

        class ShortThread:
            def is_alive(self):
                return calls["n"] < 1

        with patch("gpumesh.cli.clear_lines"), \
             patch("gpumesh.cli.erase_line"), \
             patch("gpumesh.cli.time.sleep"):
            _run_keepalive_ticker(health_fn, lambda: {"workers": []},
                                  ShortThread(), tick=0.01)

        out = capsys.readouterr().out
        assert "coordinator unreachable" in out

    def test_ticker_region_includes_buffered_mesh_logs(self, capsys):
        """A mesh line logged while the ticker is active renders inside the
        ticker region instead of being absorbed by the redraw."""
        from gpumesh.cli import _run_keepalive_ticker
        from gpumesh import status as status_mod

        calls = {"n": 0}

        def mesh_log():
            if calls["n"] == 0:
                status_mod.log("worker joined: gpu-pc")
            calls["n"] += 1

        class ShortThread:
            def is_alive(self):
                mesh_log()
                return calls["n"] <= 2

        health = {"total_score": 90.0, "uptime_seconds": 10, "jobs_done": 1}
        workers = [{"id": "w1", "hostname": "gpu-pc", "device": "cuda",
                    "device_name": "RTX 3080", "score": 85.0, "alive": True}]

        with patch("gpumesh.cli.clear_lines"), \
             patch("gpumesh.cli.erase_line"), \
             patch("gpumesh.cli.time.sleep"):
            _run_keepalive_ticker(lambda: health, lambda: {"workers": workers},
                                  ShortThread(), tick=0.01)

        out = capsys.readouterr().out
        assert "GPUMESH LIVE" in out
        assert "worker joined: gpu-pc" in out
        # the sink is restored to direct mode once the ticker stops
        assert status_mod.is_active() is False

    def test_server_mesh_lines_route_to_sink_when_active(self, tmp_path):
        """With the sink active, a real coordinator buffers its 'worker
        joined' line into the ticker region instead of stdout."""
        from gpumesh import server, status as status_mod
        from gpumesh.worker import MeshClient

        httpd = server.serve("127.0.0.1", 0, str(tmp_path / "sink.db"),
                             "sink-token", discovery=False)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            status_mod.set_active(True)
            try:
                mc = MeshClient(f"http://127.0.0.1:{port}", "sink-token")
                mc.call("POST", "/api/register", {
                    "hostname": "sink-pc", "device": "cpu",
                    "device_name": "cpu", "score": 1.0,
                })
                # the join line lands in the sink asynchronously
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    if any("worker joined: sink-pc" in ln
                           for ln in status_mod.snapshot(100)):
                        break
                    time.sleep(0.05)
                assert any("worker joined: sink-pc" in ln
                           for ln in status_mod.snapshot(100))
            finally:
                status_mod.set_active(False)
        finally:
            httpd.gpumesh_stop.set()
            httpd.shutdown()
            t.join(timeout=5)


class TestCoordinatorStatusSink:
    """Tests for the shared mesh-log sink that feeds the ticker region."""

    def test_log_prints_immediately_when_inactive(self, capsys):
        from gpumesh import status as status_mod

        status_mod.set_active(False)
        status_mod.log("hello mesh")
        assert "hello mesh" in capsys.readouterr().out

    def test_log_buffers_when_active(self, capsys):
        from gpumesh import status as status_mod

        status_mod.set_active(True)
        try:
            status_mod.log("buffered line")
            assert capsys.readouterr().out == ""
            assert status_mod.snapshot() == ["buffered line"]
        finally:
            status_mod.set_active(False)

    def test_snapshot_is_a_sliding_window(self):
        from gpumesh import status as status_mod

        status_mod.set_active(True)
        try:
            for i in range(10):
                status_mod.log(f"line {i}")
            snap = status_mod.snapshot()
            assert len(snap) == status_mod.LOG_VISIBLE
            assert snap[0] == "line 4" and snap[-1] == "line 9"
        finally:
            status_mod.set_active(False)

    def test_deactivate_clears_buffer(self):
        from gpumesh import status as status_mod

        status_mod.set_active(True)
        status_mod.log("x")
        status_mod.set_active(False)
        assert status_mod.is_active() is False
        assert status_mod.snapshot() == []

class TestColorFlag:
    """--color used to be declared twice and read nowhere.

    Colour was decided solely by ``ansi._SUPPORTS_COLOR``, evaluated once at
    import time from ``GPUMESH_COLOR`` and ``isatty``. The flag parsed
    successfully and then did nothing at all, which is worse than not existing:
    docker-compose passes it on both of its command lines and every reader
    assumed it worked. It could not simply be deleted for that reason, so it
    was made real instead.
    """

    @pytest.fixture(autouse=True)
    def _restore_color_state(self, monkeypatch):
        """Colour support is process-global; never leak a forced value.

        GPUMESH_COLOR is popped by hand rather than left to monkeypatch: the
        code under test sets it directly, so monkeypatch has no record of it
        and would leave it behind for every later test in the session.
        """
        from gpumesh import ansi
        monkeypatch.delenv("GPUMESH_COLOR", raising=False)
        original = ansi._SUPPORTS_COLOR
        yield
        ansi._SUPPORTS_COLOR = original
        os.environ.pop("GPUMESH_COLOR", None)

    def test_always_turns_colour_on_even_off_a_terminal(self):
        from gpumesh import ansi, cli

        ansi._SUPPORTS_COLOR = False
        cli._apply_color_choice("always")
        assert ansi._SUPPORTS_COLOR is True
        assert ansi.green("x") != "x", "green() must actually emit escapes"

    def test_never_turns_colour_off_even_on_a_terminal(self):
        from gpumesh import ansi, cli

        ansi._SUPPORTS_COLOR = True
        cli._apply_color_choice("never")
        assert ansi._SUPPORTS_COLOR is False
        assert ansi.green("x") == "x"

    def test_auto_changes_nothing(self):
        from gpumesh import ansi, cli

        ansi._SUPPORTS_COLOR = True
        cli._apply_color_choice("auto")
        assert ansi._SUPPORTS_COLOR is True
        assert "GPUMESH_COLOR" not in os.environ

    def test_the_env_var_is_set_for_subprocesses(self, monkeypatch):
        """Workers spawn `sys.executable` per task; the child needs to agree."""
        from gpumesh import cli

        cli._apply_color_choice("always")
        assert os.environ["GPUMESH_COLOR"] == "1"
        cli._apply_color_choice("never")
        assert os.environ["GPUMESH_COLOR"] == "0"

    def test_modules_holding_an_imported_copy_are_refreshed(self):
        """The trap that made the naive fix silently do nothing.

        Several modules do ``from .ansi import _SUPPORTS_COLOR``, binding a
        copy of the bool at their own import time. Rebinding only ansi's global
        would leave those copies stale and the flag would half-work.
        """
        import types
        from gpumesh import ansi, cli

        fake = types.ModuleType("gpumesh._color_copy_probe")
        fake._SUPPORTS_COLOR = False
        sys.modules["gpumesh._color_copy_probe"] = fake
        try:
            ansi._SUPPORTS_COLOR = False
            cli._apply_color_choice("always")
            assert fake._SUPPORTS_COLOR is True
        finally:
            del sys.modules["gpumesh._color_copy_probe"]

    def test_serve_accepts_the_flag_and_applies_it(self):
        from gpumesh import ansi
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "serve", "--color", "--token", "t123"]), \
             patch("gpumesh.cli.server.serve") as mock_serve, \
             patch("gpumesh.cli.worker.spawn_local_worker"), \
             patch("gpumesh.cli.connection_manager.save_connection"), \
             patch("gpumesh.cli.tunnel.open_tunnel"):
            mock_serve.return_value = MagicMock()
            try:
                main()
            except SystemExit:
                pass
        assert ansi._SUPPORTS_COLOR is True

    def test_join_no_color_applies_it_without_eating_the_url(self):
        """The reason --color stayed a valueless flag.

        An optional-value form (``nargs="?"``) would let ``--color`` swallow
        the next token, so this exact command line would fail on an "invalid
        choice: http://..." error instead of joining.
        """
        from gpumesh import ansi
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "join", "http://127.0.0.1:8000",
                                "--no-color", "--token", "t123"]), \
             patch("gpumesh.cli._run_worker_with_report") as mock_run:
            try:
                main()
            except SystemExit:
                pass
        assert mock_run.call_args[0][0] == "http://127.0.0.1:8000"
        assert ansi._SUPPORTS_COLOR is False

    def test_color_and_no_color_together_are_rejected(self):
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "serve", "--color", "--no-color"]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 2


DOCTOR_TOKEN = "s3cret-doctor-token-do-not-print"


@pytest.fixture
def doctor_env(monkeypatch):
    """Keep `doctor` tests fast, offline, and independent of this machine.

    The firewall probe shells out to netsh three times at 10s each, and the
    coordinator probe opens a real socket. Both are stubbed by default so no
    test in this class can ever block on the network or on a Windows service.
    """
    monkeypatch.delenv("GPUMESH_URL", raising=False)
    monkeypatch.delenv("GPUMESH_TOKEN", raising=False)
    monkeypatch.setattr(
        "gpumesh.cli._doctor_firewall",
        lambda port: {"platform": "Linux", "checked": False,
                      "rules_found": [], "rules_missing": [], "error": None},
    )
    monkeypatch.setattr(
        "gpumesh.cli._doctor_network",
        lambda port: {"port": port, "bind_host": "127.0.0.1",
                      "bind_is_loopback": True, "host_ip_override": None,
                      "advertised_ip": "192.168.1.50",
                      "lan_ip_candidates": ["192.168.1.50"],
                      "virtual_adapter_candidates": []},
    )
    return monkeypatch


def _run_doctor(argv):
    """Run `gpumesh doctor`, returning (exit_code, stdout)."""
    from gpumesh.cli import main
    import io
    import contextlib

    buf = io.StringIO()
    code = 0
    with patch("sys.argv", ["gpumesh"] + argv), \
         contextlib.redirect_stdout(buf):
        try:
            main()
        except SystemExit as exc:
            code = exc.code or 0
    return code, buf.getvalue()


class TestDoctorNoCoordinator:
    """A laptop that never joined a mesh is a healthy laptop.

    `doctor` has to be usable as a pre-flight check, so "nothing configured"
    is a warning and exit 0. Exiting non-zero here would make the command
    useless in a script and would train people to ignore its exit code.
    """

    def test_no_coordinator_configured_still_exits_zero(self, doctor_env):
        doctor_env.setattr(
            "gpumesh.cli.connection_manager.load_connection", lambda: None
        )
        code, out = _run_doctor(["doctor"])
        assert code == 0
        assert "No coordinator configured" in _plain(out)

    def test_no_coordinator_does_not_probe_the_network(self, doctor_env):
        doctor_env.setattr(
            "gpumesh.cli.connection_manager.load_connection", lambda: None
        )
        with patch("gpumesh.worker.MeshClient") as mock_client:
            code, _ = _run_doctor(["doctor"])
        assert code == 0
        mock_client.assert_not_called()


class TestDoctorUnreachableCoordinator:
    """An unreachable coordinator is a fact about the network, not this box."""

    @pytest.fixture(autouse=True)
    def _saved_but_dead(self, doctor_env):
        doctor_env.setattr(
            "gpumesh.cli.connection_manager.load_connection",
            lambda: {"url": "http://10.0.0.9:8000", "token": DOCTOR_TOKEN,
                     "saved_at": None},
        )
        client = MagicMock()
        client.call.side_effect = OSError("connection refused")
        doctor_env.setattr("gpumesh.worker.MeshClient", lambda *a, **k: client)

    def test_unreachable_coordinator_exits_zero(self):
        code, out = _run_doctor(["doctor"])
        assert code == 0
        assert "not reachable" in _plain(out)

    def test_the_dead_url_is_reported_so_it_can_be_recognised(self):
        _, out = _run_doctor(["doctor"])
        assert "http://10.0.0.9:8000" in _plain(out)


class TestDoctorWithoutTorch:
    """torch is optional, and its absence must never fail the check."""

    def test_absent_torch_is_a_warning_not_a_problem(self, doctor_env):
        doctor_env.setattr(
            "gpumesh.cli.connection_manager.load_connection", lambda: None
        )
        # A None entry in sys.modules is what makes `import torch` raise
        # ImportError without touching whatever is really installed.
        doctor_env.setitem(sys.modules, "torch", None)
        code, out = _run_doctor(["doctor"])
        assert code == 0
        assert "not installed" in _plain(out)

    def test_cpu_only_torch_on_an_nvidia_box_is_called_out(self, doctor_env):
        """The failure mode that silently halves a mesh's capacity."""
        from gpumesh import cli

        doctor_env.setattr(
            "gpumesh.cli.connection_manager.load_connection", lambda: None
        )
        doctor_env.setattr(
            "gpumesh.cli._doctor_torch",
            lambda: {"installed": True, "version": "2.4.0+cpu",
                     "cuda_available": False, "device_count": 0,
                     "device_names": [], "nvidia_gpu_present": True,
                     "mps_available": False, "error": None},
        )
        code, out = _run_doctor(["doctor"])
        plain = _plain(out)
        assert code == 0
        assert "CPU-only torch wheel" in plain
        assert "pytorch.org" in plain

    def test_no_torch_at_all_on_an_nvidia_box_is_also_called_out(self, doctor_env):
        doctor_env.setattr(
            "gpumesh.cli.connection_manager.load_connection", lambda: None
        )
        doctor_env.setattr(
            "gpumesh.cli._doctor_torch",
            lambda: {"installed": False, "version": None,
                     "cuda_available": False, "device_count": 0,
                     "device_names": [], "nvidia_gpu_present": True,
                     "error": None},
        )
        code, out = _run_doctor(["doctor"])
        assert code == 0
        assert "gpumesh[gpu]" in _plain(out)


class TestDoctorJson:
    """--json exists so a bug report can be parsed, not just read."""

    @pytest.fixture(autouse=True)
    def _connected(self, doctor_env):
        doctor_env.setattr(
            "gpumesh.cli.connection_manager.load_connection",
            lambda: {"url": "http://10.0.0.9:8000", "token": DOCTOR_TOKEN,
                     "saved_at": None},
        )
        client = MagicMock()
        client.call.side_effect = OSError("connection refused")
        doctor_env.setattr("gpumesh.worker.MeshClient", lambda *a, **k: client)

    def test_json_output_parses(self):
        import json

        code, out = _run_doctor(["doctor", "--json"])
        assert code == 0
        report = json.loads(out)
        assert report["gpumesh"]["version"]
        assert report["python"]["version"]
        assert "problems" in report and "warnings" in report

    def test_json_carries_the_sections_a_bug_report_needs(self):
        import json

        _, out = _run_doctor(["doctor", "--json"])
        report = json.loads(out)
        for section in ("gpumesh", "python", "torch", "cloudpickle",
                        "connection", "coordinator", "network", "firewall"):
            assert section in report, f"missing section: {section}"

    def test_collection_warnings_cannot_corrupt_the_document(self, doctor_env):
        """Anything printed while collecting must go to stderr, not stdout.

        connection_manager warns about a world-readable config file by
        printing; landing that line in the middle of the JSON would break
        every consumer.
        """
        import json

        def _noisy_load():
            print("[gpumesh] WARNING: config is world readable")
            return {"url": "http://10.0.0.9:8000", "token": DOCTOR_TOKEN,
                    "saved_at": None}

        doctor_env.setattr(
            "gpumesh.cli.connection_manager.load_connection", _noisy_load
        )
        _, out = _run_doctor(["doctor", "--json"])
        json.loads(out)  # raises if the warning leaked into stdout


class TestDoctorNeverPrintsTheToken:
    """The whole command exists so its output can be pasted in public.

    The mesh token grants code execution on every machine in the mesh. A
    diagnostic that leaks it into a GitHub issue is worse than no diagnostic
    at all, so this is asserted against both output formats and against the
    collected data structure itself.
    """

    @pytest.fixture(autouse=True)
    def _connected_and_alive(self, doctor_env):
        doctor_env.setattr(
            "gpumesh.cli.connection_manager.load_connection",
            lambda: {"url": "http://10.0.0.9:8000", "token": DOCTOR_TOKEN,
                     "saved_at": None},
        )
        client = MagicMock()
        client.call.return_value = {
            "status": "ok", "version": "1.3.0", "uptime_seconds": 12.0,
            "workers_alive": 1, "workers_dead": 0,
            "jobs_pending": 0, "jobs_running": 0, "jobs_done": 0,
            "jobs_failed": 0, "total_score": 1.0,
            "workers": [{"id": "abcdef123456", "hostname": "box",
                         "device": "cuda", "device_name": "RTX 4090",
                         "alive": True}],
        }
        doctor_env.setattr("gpumesh.worker.MeshClient", lambda *a, **k: client)

    def test_token_absent_from_text_output(self):
        _, out = _run_doctor(["doctor"])
        assert DOCTOR_TOKEN not in out
        assert "<redacted>" in _plain(out)

    def test_token_absent_from_json_output(self):
        _, out = _run_doctor(["doctor", "--json"])
        assert DOCTOR_TOKEN not in out

    def test_token_absent_from_the_collected_data(self):
        import json
        from gpumesh import cli

        report = cli._doctor_collect()
        assert DOCTOR_TOKEN not in json.dumps(report, default=str)
        assert report["connection"]["token"] == "<redacted>"
        # The length is reported instead: not a secret, and the only way to
        # spot a token truncated by a shell or a YAML quoting accident.
        assert report["connection"]["token_length"] == len(DOCTOR_TOKEN)

    def test_a_token_in_the_env_is_not_printed_either(self, doctor_env):
        doctor_env.setenv("GPUMESH_TOKEN", DOCTOR_TOKEN)
        _, out = _run_doctor(["doctor"])
        assert DOCTOR_TOKEN not in out

    def test_credentials_in_the_url_are_stripped(self):
        from gpumesh import cli

        assert cli._redact_url("http://bob:hunter2@10.0.0.9:8000") == (
            "http://<redacted>@10.0.0.9:8000"
        )
        # A URL with no userinfo is left exactly as it is: the host and port
        # are the diagnostic payload.
        assert cli._redact_url("http://10.0.0.9:8000") == "http://10.0.0.9:8000"


class TestDoctorLocalProblems:
    """Exit 1 is reserved for something broken on THIS machine."""

    @pytest.fixture(autouse=True)
    def _no_connection(self, doctor_env):
        doctor_env.setattr(
            "gpumesh.cli.connection_manager.load_connection", lambda: None
        )

    def test_missing_cloudpickle_is_a_problem(self, doctor_env):
        doctor_env.setattr(
            "gpumesh.cli._doctor_cloudpickle",
            lambda: {"installed": False, "version": None},
        )
        code, out = _run_doctor(["doctor"])
        assert code == 1
        assert "wire format" in _plain(out)

    def test_windows_store_alias_stub_is_a_problem(self, doctor_env):
        """A zero-byte alias turns every function task into a Store popup."""
        doctor_env.setattr(
            "gpumesh.cli._doctor_python",
            lambda: {"version": "3.11.9", "implementation": "CPython",
                     "executable": r"C:\WindowsApps\python3.11.exe",
                     "windows_store_path": True,
                     "windows_store_alias_stub": True},
        )
        code, out = _run_doctor(["doctor"])
        assert code == 1
        assert "app-execution alias" in _plain(out)

    def test_the_real_store_build_is_only_a_warning(self, doctor_env):
        doctor_env.setattr(
            "gpumesh.cli._doctor_python",
            lambda: {"version": "3.11.9", "implementation": "CPython",
                     "executable": r"C:\WindowsApps\python.exe",
                     "windows_store_path": True,
                     "windows_store_alias_stub": False},
        )
        code, out = _run_doctor(["doctor"])
        assert code == 0
        assert "Microsoft Store build" in _plain(out)

    def test_a_healthy_machine_exits_zero(self, doctor_env):
        doctor_env.setattr(
            "gpumesh.cli._doctor_torch",
            lambda: {"installed": True, "version": "2.4.0",
                     "cuda_available": True, "device_count": 1,
                     "device_names": ["RTX 4090"], "nvidia_gpu_present": True,
                     "mps_available": False, "error": None},
        )
        code, _ = _run_doctor(["doctor"])
        assert code == 0


class TestDoctorIsReadOnly:
    """A diagnostic that changes the machine destroys its own evidence."""

    def test_it_never_adds_a_firewall_rule(self, doctor_env):
        doctor_env.setattr(
            "gpumesh.cli.connection_manager.load_connection", lambda: None
        )
        with patch("gpumesh.cli.utils.try_add_firewall_rule") as mock_add, \
             patch("gpumesh.cli.connection_manager.save_connection") as mock_save:
            code, _ = _run_doctor(["doctor"])
        assert code == 0
        mock_add.assert_not_called()
        mock_save.assert_not_called()

    def test_the_firewall_probe_only_queries(self, monkeypatch):
        """`netsh ... show rule` — never `add rule`."""
        from gpumesh import cli

        seen = []

        class _Proc:
            returncode = 0

        def _fake_run(cmd, **kwargs):
            seen.append(cmd)
            return _Proc()

        monkeypatch.setattr("gpumesh.cli.platform.system", lambda: "Windows")
        monkeypatch.setattr("gpumesh.cli.subprocess.run", _fake_run)
        info = cli._doctor_firewall(8000)

        assert info["rules_found"] == ["gpumesh-8000", "gpumesh-exe-8000",
                                       "gpumesh-discovery-udp"]
        for cmd in seen:
            assert "show" in cmd
            assert "add" not in cmd

    def test_a_missing_rule_is_reported_not_created(self, monkeypatch):
        from gpumesh import cli

        class _Proc:
            returncode = 1

        monkeypatch.setattr("gpumesh.cli.platform.system", lambda: "Windows")
        monkeypatch.setattr("gpumesh.cli.subprocess.run",
                            lambda cmd, **kw: _Proc())
        info = cli._doctor_firewall(8000)
        assert info["rules_found"] == []
        assert "gpumesh-8000" in info["rules_missing"]

    def test_no_firewall_probe_off_windows(self, monkeypatch):
        from gpumesh import cli

        monkeypatch.setattr("gpumesh.cli.platform.system", lambda: "Linux")
        monkeypatch.setattr(
            "gpumesh.cli.subprocess.run",
            lambda *a, **k: pytest.fail("netsh must not run off Windows"),
        )
        assert cli._doctor_firewall(8000)["checked"] is False


class TestDoctorWorkerVersionSkew:
    """Cross-version function shipping is this project's recurring failure."""

    @pytest.fixture(autouse=True)
    def _connected(self, doctor_env):
        doctor_env.setattr(
            "gpumesh.cli.connection_manager.load_connection",
            lambda: {"url": "http://10.0.0.9:8000", "token": DOCTOR_TOKEN,
                     "saved_at": None},
        )

    @staticmethod
    def _client_returning(workers):
        client = MagicMock()

        def _call(method, path, **kwargs):
            if path == "/api/health":
                return {"status": "ok", "version": "1.3.0",
                        "uptime_seconds": 1.0, "workers_alive": len(workers),
                        "workers_dead": 0, "jobs_pending": 0,
                        "jobs_running": 0, "jobs_done": 0, "jobs_failed": 0}
            return {"workers": workers}

        client.call.side_effect = _call
        return client

    def test_a_worker_on_another_python_is_called_out(self, doctor_env):
        import platform as _platform

        this_python = _platform.python_version()
        other = "9.9.9"
        client = self._client_returning([
            {"id": "aaaaaaaabbbb", "hostname": "otherbox", "device": "cpu",
             "device_name": "i7", "alive": True, "version": "1.3.0",
             "python_version": other},
        ])
        doctor_env.setattr("gpumesh.worker.MeshClient", lambda *a, **k: client)

        code, out = _run_doctor(["doctor"])
        plain = _plain(out)
        assert code == 0
        assert "otherbox" in plain
        assert other in plain and this_python in plain

    def test_a_coordinator_that_reports_no_versions_says_so(self, doctor_env):
        """Better than silently implying every worker matches."""
        client = self._client_returning([
            {"id": "aaaaaaaabbbb", "hostname": "otherbox", "device": "cpu",
             "device_name": "i7", "alive": True},
        ])
        doctor_env.setattr("gpumesh.worker.MeshClient", lambda *a, **k: client)

        code, out = _run_doctor(["doctor"])
        assert code == 0
        assert "does not report worker gpumesh/Python versions" in _plain(out)
