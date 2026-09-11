"""Tests for gpumesh._file_perms, the shared permission-hardening helper
used by connection_manager.py (the saved token), db.py (the job database)
and tls.py (the TLS private key).
"""

import os

import pytest

from gpumesh import _file_perms


@pytest.fixture(autouse=True)
def restore_os_name():
    """_file_perms branches on os.name; tests that patch it must not leak
    the patch to whichever test runs next."""
    original = os.name
    yield
    os.name = original


class TestRestrictPath:

    @pytest.mark.skipif(os.name == "nt",
                        reason="st_mode does not reflect real ACL bits on Windows")
    def test_chmods_a_file_to_0600(self, tmp_path):
        target = tmp_path / "secret.db"
        target.write_text("data")
        os.name = "posix"
        _file_perms.restrict_path(str(target), "test data")
        assert (target.stat().st_mode & 0o777) == 0o600

    @pytest.mark.skipif(os.name == "nt",
                        reason="st_mode does not reflect real ACL bits on Windows")
    def test_chmods_a_directory_to_0700(self, tmp_path):
        target = tmp_path / "state"
        target.mkdir()
        os.name = "posix"
        _file_perms.restrict_path(str(target), "test data")
        assert (target.stat().st_mode & 0o777) == 0o700

    def test_warns_naming_the_path_and_what_it_holds_on_chmod_failure(
        self, tmp_path, monkeypatch, capsys,
    ):
        target = tmp_path / "secret.db"
        target.write_text("data")
        os.name = "posix"

        def _boom(path, mode):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(os, "chmod", _boom)
        _file_perms.restrict_path(str(target), "test data")
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert str(target) in out
        assert "test data" in out
        assert "0600" in out

    def test_never_raises_on_chmod_failure(self, tmp_path, monkeypatch):
        target = tmp_path / "secret.db"
        target.write_text("data")
        os.name = "posix"

        def _boom(path, mode):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(os, "chmod", _boom)
        _file_perms.restrict_path(str(target), "test data")  # must not raise

    def test_custom_remediation_replaces_the_generic_closing_line(
        self, tmp_path, monkeypatch, capsys,
    ):
        target = tmp_path / "secret.db"
        target.write_text("data")
        os.name = "posix"
        monkeypatch.setattr(os, "chmod", lambda *a: (_ for _ in ()).throw(OSError("nope")))
        _file_perms.restrict_path(str(target), "test data", remediation="Go fix it now.")
        out = capsys.readouterr().out
        assert "Go fix it now." in out
        assert "Restrict it by hand if this machine is shared." not in out

    def test_windows_runs_icacls(self, tmp_path, monkeypatch, capsys):
        import subprocess

        target = tmp_path / "secret.db"
        target.write_text("data")
        os.name = "nt"
        monkeypatch.setenv("USERNAME", "tester")

        class _Result:
            returncode = 5
            stdout = b""
            stderr = b"Access is denied."

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
        _file_perms.restrict_path(str(target), "test data")
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "icacls" in out
        assert "Access is denied" in out

    def test_windows_warns_when_username_is_missing(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / "secret.db"
        target.write_text("data")
        os.name = "nt"
        monkeypatch.delenv("USERNAME", raising=False)
        _file_perms.restrict_path(str(target), "test data")
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "USERNAME" in out

    def test_quiet_on_a_normal_posix_run(self, tmp_path, capsys):
        target = tmp_path / "secret.db"
        target.write_text("data")
        os.name = "posix"
        _file_perms.restrict_path(str(target), "test data")
        assert "WARNING" not in capsys.readouterr().out
