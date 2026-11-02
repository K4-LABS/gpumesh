"""Tests for gpumesh gui (tray + local web dashboard)."""

import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gpumesh.gui import TrayController

FAKE_TOKEN = "SUPERSECRET-TOKEN-12345"

MACHINES = [
    {"name": "gpu-pc", "device": "cuda", "device_name": "RTX 3080",
     "score": 85.2, "alive": True},
    {"name": "old-pc", "device": "cpu", "device_name": "",
     "score": 1.0, "alive": False},
]


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


class TestDashboardHtml:
    def test_cards_and_counts(self):
        from gpumesh.gui import build_dashboard_html

        out = build_dashboard_html({"machines": MACHINES})
        assert "gpu-pc" in out and "RTX 3080" in out
        assert "85.2" in out and "Online" in out and "Offline" in out
        assert "1 of 2 computers online" in out

    def test_status_override_and_empty(self):
        from gpumesh.gui import build_dashboard_html

        assert "2 computers sharing" in build_dashboard_html(
            {"status": "2 computers sharing", "machines": MACHINES})
        out = build_dashboard_html({})
        assert "GPUMesh" in out and "No computers found yet" in out

    def test_token_never_rendered(self):
        """Extra keys (notably any secret) cannot reach the page."""
        from gpumesh.gui import build_dashboard_html

        state = {"status": "Ready", "token": FAKE_TOKEN, "machines": [
            {"name": "gpu-pc", "device": "cuda", "device_name": "x",
             "score": 1.0, "alive": True, "token": FAKE_TOKEN}]}
        out = build_dashboard_html(state, pair=("123456", 299))
        assert FAKE_TOKEN not in out
        assert "gpu-pc" in out and "123456" in out
        assert 'type="hidden"' not in out.lower()

    def test_plain_language_and_escaping(self):
        from gpumesh.gui import build_dashboard_html

        out = build_dashboard_html({"machines": [
            {"name": "<script>alert(1)</script>", "device": "cpu",
             "device_name": "", "score": 1.0, "alive": True}]})
        assert "<script>" not in out and "&lt;script&gt;" in out
        for word in ("coordinator", "lease", "subnet"):
            assert word not in out.lower()

    def test_pair_panel_and_join_form(self):
        from gpumesh.gui import build_dashboard_html

        out = build_dashboard_html(
            {}, pair=("482916", 240),
            nearby=[("laptop", "http://192.168.1.10:8000")])
        assert "482916" in out and "New code" in out
        assert 'action="/join"' in out and "http://192.168.1.10:8000" in out
        plain = build_dashboard_html({})
        assert "Create mesh" in plain and 'action="/join"' in plain


class LiveDashboard:
    """Real HTTPServer on 127.0.0.1 for handler tests."""

    def __init__(self):
        from gpumesh.gui import serve_dashboard

        self.httpd = serve_dashboard(0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            return r.status, r.read().decode()

    def post(self, path, fields):
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()


class TestHandler:
    def test_serves_and_binds_loopback(self):
        dash = LiveDashboard()
        try:
            host, _ = dash.httpd.server_address[:2]
            assert host == "127.0.0.1"
            status, body = dash.get("/ui")
            assert status == 200 and "GPUMesh" in body
            assert FAKE_TOKEN not in body
        finally:
            dash.close()

    def test_join_rejects_bad_address(self):
        dash = LiveDashboard()
        try:
            status, body = dash.post("/join", {"host": "nope", "code": "1"})
            assert status == 200 and "http://" in body
        finally:
            dash.close()

    def test_join_claims_saves_and_spawns(self):
        import gpumesh.gui as gui

        dash = LiveDashboard()
        try:
            with patch.object(gui, "_claim", return_value="tok-abc") as claim, \
                 patch("gpumesh.connection_manager.save_connection") as save, \
                 patch("gpumesh.gui.subprocess.Popen") as popen:
                popen.return_value = MagicMock(poll=lambda: None)
                status, _ = dash.post("/join", {
                    "host": "http://192.168.1.10:8000", "code": "482916"})
            assert status in (200, 302)
            claim.assert_called_once_with("http://192.168.1.10:8000", "482916")
            save.assert_called_once_with("http://192.168.1.10:8000", "tok-abc")
            argv, kwargs = popen.call_args[0][0], popen.call_args[1]
            assert "join" in argv and "http://192.168.1.10:8000" in argv
            assert "tok-abc" not in " ".join(argv)  # never on the command line
            assert kwargs["env"]["GPUMESH_TOKEN"] == "tok-abc"
        finally:
            dash.close()

    def test_join_failure_shows_notice(self):
        import gpumesh.gui as gui

        dash = LiveDashboard()
        try:
            with patch.object(gui, "_claim", return_value=""):
                status, body = dash.post("/join", {
                    "host": "http://192.168.1.10:8000", "code": "000000"})
            assert status == 200 and "not accepted" in body
        finally:
            dash.close()

    def test_join_form_has_no_duplicate_field_names(self):
        """Two controls named ``host`` silently threw the typed address away.

        A browser submits both the nearby dropdown and the address box, the
        server read the first value, and the address a person typed was
        discarded -- so a form that looked correct could never join by
        typing, which is the one path docs/gui.md tells people to use.
        Field names must stay unique.
        """
        from gpumesh.gui import build_dashboard_html

        out = build_dashboard_html(
            {}, nearby=[("laptop", "http://192.168.1.10:8000")])
        form = out.split('action="/join"', 1)[1].split("</form>", 1)[0]
        names = re.findall(r'name="([^"]+)"', form)
        assert names == ["host", "code"]

    def test_typed_address_wins_over_nearby_pick(self):
        """Typing an address must do what it looks like it does."""
        import gpumesh.gui as gui

        dash = LiveDashboard()
        try:
            with patch.object(gui, "_claim", return_value="") as claim:
                dash.post("/join", {"nearby": "http://192.168.1.10:8000",
                                    "host": "http://192.0.2.7:9999",
                                    "code": "482916"})
            claim.assert_called_once_with("http://192.0.2.7:9999", "482916")
        finally:
            dash.close()

    def test_expired_code_is_replaced_not_left_stalled(self):
        """An expired code must be re-minted, not cached forever.

        The panel used to keep showing "a code appears here in a moment"
        indefinitely once the 5-minute code in the cache went stale, because
        the cache was only filled when it was empty. Someone waiting on that
        sentence is waiting forever.
        """
        import time as _time

        import gpumesh.gui as gui
        from gpumesh import connection_manager

        connection_manager.save_connection("http://127.0.0.1:9", FAKE_TOKEN)
        dash = LiveDashboard()
        try:
            stale = {"code": "111111", "at": _time.time() - 400.0, "ttl": 300}
            fresh = {"code": "222222", "at": _time.time(), "ttl": 300}
            dash.httpd.gpumesh_pair = stale
            with patch.object(gui, "_snapshot",
                              return_value={"machines": []}), \
                 patch.object(gui, "_mint_pair", return_value=fresh) as mint:
                status, body = dash.get("/ui")
            assert status == 200
            mint.assert_called_once()
            assert "222222" in body
            assert "111111" not in body
        finally:
            dash.close()

    def test_create_button_starts_a_mesh(self):
        """Every tray menu action is also a button on the page.

        Without this the page is a dead end on any machine that has no tray
        backend -- which is every headless Linux box and any Python without
        the gui extra -- because starting a mesh was tray-only.
        """
        dash = LiveDashboard()
        try:
            with patch.object(dash.httpd.gpumesh_controller,
                              "create_mesh") as create:
                status, _ = dash.post("/create", {})
            assert status in (200, 302)
            create.assert_called_once_with()
        finally:
            dash.close()

    def test_stop_button_stops_the_mesh_and_clears_the_code(self):
        dash = LiveDashboard()
        try:
            dash.httpd.gpumesh_pair = {"code": "111222", "at": 0.0,
                                       "ttl": 300}
            with patch.object(dash.httpd.gpumesh_controller, "quit") as quit_:
                status, _ = dash.post("/stop", {})
            assert status in (200, 302)
            quit_.assert_called_once_with()
            assert dash.httpd.gpumesh_pair == {}
        finally:
            dash.close()

    def test_scan_button_asks_again(self):
        dash = LiveDashboard()
        try:
            status, body = dash.post("/scan", {})
            assert status == 200 and "Scanning" in body
        finally:
            dash.close()

    def test_dashboard_is_threaded(self):
        """A slow page load must not block the next request.

        One page load can wait two seconds on another computer, and the page
        reloads itself every five. On a single-threaded server those queue up
        and the dashboard feels frozen, which is exactly when someone is
        standing there watching it.
        """
        dash = LiveDashboard()
        try:
            assert isinstance(dash.httpd, ThreadingHTTPServer)
            assert dash.httpd.daemon_threads is True
            # The controller is owned by the server, so a mesh started from
            # the page is stopped when the dashboard goes away.
            assert isinstance(dash.httpd.gpumesh_controller, TrayController)
        finally:
            dash.close()

    def test_cross_site_post_is_refused(self):
        """A page on another site must not be able to drive the dashboard.

        It can POST to 127.0.0.1 even though it cannot read the reply, and
        the side effects here are real: minting a code, or joining a
        stranger's mesh and then running its work.
        """
        dash = LiveDashboard()
        try:
            data = urllib.parse.urlencode(
                {"host": "http://192.0.2.7:9999", "code": "482916"}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{dash.port}/join", data=data, method="POST",
                headers={"Origin": "http://evil.example"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    status = r.status
            except urllib.error.HTTPError as e:
                status = e.code
            assert status == 403
        finally:
            dash.close()

    def test_dns_rebinding_host_header_is_refused(self):
        """``Host: evil.example`` resolving to 127.0.0.1 must be refused."""
        dash = LiveDashboard()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{dash.port}/ui",
                headers={"Host": "evil.example"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    status = r.status
            except urllib.error.HTTPError as e:
                status = e.code
            assert status == 403
        finally:
            dash.close()

    def test_code_button_mints(self):
        import time as _time

        import gpumesh.gui as gui
        from gpumesh import connection_manager

        # conftest isolates ~/.gpumesh: save a mesh first, else there is
        # nothing to mint a code for and the panel correctly hides.
        connection_manager.save_connection("http://127.0.0.1:9", FAKE_TOKEN)
        dash = LiveDashboard()
        try:
            # The saved mesh has to look alive. A pairing code is only shown
            # for a mesh that answers, because a code for a mesh that is down
            # is a trap: it is typed on the other computer and rejected.
            with patch.object(gui, "_snapshot",
                              return_value={"machines": []}), \
                 patch.object(gui, "_mint_pair",
                              return_value={"code": "111222",
                                            "at": _time.time(), "ttl": 300}):
                status, body = dash.post("/code", {})
            assert status == 200 and "111222" in body
        finally:
            dash.close()

    def test_code_hidden_when_mesh_is_unreachable(self):
        """No code for a mesh that is not answering.

        The page has exactly one useful next step when the mesh is gone --
        the Start button -- and a stale code next to it is what sends someone
        to type six digits into a machine that will refuse them.
        """
        import gpumesh.gui as gui
        from gpumesh import connection_manager

        connection_manager.save_connection("http://127.0.0.1:9", FAKE_TOKEN)
        dash = LiveDashboard()
        try:
            with patch.object(gui, "_mint_pair",
                              return_value={"code": "999888",
                                            "at": 0.0, "ttl": 300}):
                status, body = dash.get("/ui")
            assert status == 200
            assert "reach your mesh right now" in body
            assert "999888" not in body
        finally:
            dash.close()

    def test_loopback_helper(self):
        from gpumesh.gui import is_loopback_client

        for ip in ("127.0.0.1", "127.0.0.5", "localhost", "::1"):
            assert is_loopback_client(ip) is True
        for ip in ("192.168.1.5", "0.0.0.0", ""):
            assert is_loopback_client(ip) is False


class TestTray:
    def test_degrades_without_pystray(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "pystray", None)
        import gpumesh.gui as gui

        assert gui.tray_available() is False
        assert gui.TrayController().run() is False

    def test_create_binds_lan(self):
        from gpumesh.gui import TrayController

        controller = TrayController("http://127.0.0.1:8080/ui", 8123)
        fake = MagicMock()
        fake.poll.return_value = None
        with patch("gpumesh.gui.subprocess.Popen", return_value=fake) as popen:
            assert controller.create_mesh() is fake
        argv = popen.call_args[0][0]
        assert argv[:3] == [sys.executable, "-m", "gpumesh"]
        # loopback would make "create then join" silently impossible
        assert "--host" in argv and "0.0.0.0" in argv and "8123" in argv

    def test_join_without_args_starts_nothing(self):
        from gpumesh.gui import TrayController

        with patch("gpumesh.gui.subprocess.Popen") as popen:
            assert TrayController().join_mesh() is None
        popen.assert_not_called()

    def test_quit_stops_children(self):
        from gpumesh.gui import TrayController

        controller = TrayController()
        serve = MagicMock(poll=lambda: None)
        join = MagicMock(poll=lambda: None)
        controller._serve_proc, controller._join_proc = serve, join
        controller.quit()
        serve.terminate.assert_called_once()
        join.terminate.assert_called_once()


class TestCliGuiParser:
    def test_gui_wires_to_cmd_gui(self):
        from gpumesh.cli import cmd_gui, main

        with patch("sys.argv", ["gpumesh", "gui", "--port", "8090"]), \
             patch("gpumesh.gui.cmd_gui") as mock_gui:
            try:
                main()
            except SystemExit:
                pass
        mock_gui.assert_called_once()
        assert mock_gui.call_args[0][0].port == 8090
        assert mock_gui.call_args[0][0].func is cmd_gui

    def test_gui_defaults(self):
        from gpumesh.cli import main

        with patch("sys.argv", ["gpumesh", "gui"]), \
             patch("gpumesh.gui.cmd_gui") as mock_gui:
            try:
                main()
            except SystemExit:
                pass
        args = mock_gui.call_args[0][0]
        assert (args.port, args.mesh_port) == (8080, 8000)
        assert args.no_tray is False

    def test_gui_never_logs_token(self, capsys):
        from gpumesh import connection_manager
        from gpumesh.gui import cmd_gui

        with patch.object(connection_manager, "load_connection",
                          return_value={"url": "http://127.0.0.1:8000",
                                        "token": FAKE_TOKEN}), \
             patch("gpumesh.gui.serve_dashboard") as mock_serve, \
             patch("gpumesh.gui.webbrowser.open"), \
             patch("gpumesh.gui.time.sleep", side_effect=KeyboardInterrupt):
            fake_httpd = MagicMock()
            fake_httpd.server_address = ("127.0.0.1", 8099)
            mock_serve.return_value = fake_httpd
            cmd_gui(SimpleNamespace(port=8099, mesh_port=8000,
                                    no_tray=True, no_browser=True))
        out = _plain(capsys.readouterr().out)
        assert FAKE_TOKEN not in out and "8099" in out
