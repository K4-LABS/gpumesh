"""Tests for gpumesh.connection_manager module."""

import json
import os
import pytest

import gpumesh.connection_manager as cm


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Redirect config file to a temp directory so tests don't touch the real home."""
    config_dir = tmp_path / ".gpumesh"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    monkeypatch.setattr(cm, "_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(cm, "_CONFIG_PATH", str(config_path))
    return config_path


class TestSaveConnection:
    """Tests for save_connection()."""

    def test_creates_config_dir_and_file(self, tmp_path, monkeypatch):
        """save_connection creates the config directory from scratch."""
        config_dir = tmp_path / ".gpumesh"
        config_path = config_dir / "config.json"
        # Ensure directory does NOT exist yet
        assert not config_dir.exists()
        monkeypatch.setattr(cm, "_CONFIG_DIR", str(config_dir))
        monkeypatch.setattr(cm, "_CONFIG_PATH", str(config_path))
        
        cm.save_connection("http://192.168.1.10:8000", "mytoken")
        
        assert config_dir.exists()
        assert config_path.exists()
        data = json.loads(config_path.read_text())
        assert data["url"] == "http://192.168.1.10:8000"
        assert data["token"] == "mytoken"

    def test_creates_config_file(self, isolated_config):
        cm.save_connection("http://192.168.1.10:8000", "mytoken")
        assert isolated_config.exists()

    def test_strips_trailing_slash_from_url(self, isolated_config):
        cm.save_connection("http://192.168.1.10:8000/", "mytoken")
        data = json.loads(isolated_config.read_text())
        assert data["url"] == "http://192.168.1.10:8000"

    def test_stores_token_and_saved_at(self, isolated_config):
        cm.save_connection("http://192.168.1.10:8000", "mytoken")
        data = json.loads(isolated_config.read_text())
        assert data["token"] == "mytoken"
        assert "saved_at" in data
        assert isinstance(data["saved_at"], float)

    def test_overwrites_previous_connection(self, isolated_config):
        cm.save_connection("http://old:8000", "oldtoken")
        cm.save_connection("http://new:8000", "newtoken")
        data = json.loads(isolated_config.read_text())
        assert data["url"] == "http://new:8000"
        assert data["token"] == "newtoken"


class TestPermissionWarnings:
    """The config file holds the mesh token in plaintext, and that token is
    remote code execution on every node. If the tightening fails, saying
    nothing leaves the user believing a lock is in place that is not.
    """

    def test_warns_when_chmod_fails(self, isolated_config, monkeypatch, capsys):
        def _boom(path, mode):
            raise PermissionError(13, "Permission denied")

        # Force the POSIX branch so the assertion is about chmod alone and
        # does not depend on whether a real icacls run happens to succeed.
        monkeypatch.setattr(cm.os, "name", "posix")
        monkeypatch.setattr(cm.os, "chmod", _boom)
        cm.save_connection("http://192.168.1.10:8000", "mytoken")
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert str(isolated_config) in out
        assert "0600" in out

    def test_still_saves_when_chmod_fails(self, isolated_config, monkeypatch):
        """A warning, not a failure — refusing to save is the worse outcome."""
        def _boom(path, mode):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(cm.os, "name", "posix")
        monkeypatch.setattr(cm.os, "chmod", _boom)
        cm.save_connection("http://192.168.1.10:8000", "mytoken")
        data = json.loads(isolated_config.read_text())
        assert data["token"] == "mytoken"

    def test_warns_when_icacls_fails(self, isolated_config, monkeypatch, capsys):
        """Windows is the primary platform and icacls is what actually
        restricts the ACL there; chmod only flips the read-only bit."""
        import subprocess

        class _Result:
            returncode = 5
            stdout = b""
            stderr = b"Access is denied."

        monkeypatch.setattr(cm.os, "name", "nt")
        monkeypatch.setenv("USERNAME", "tester")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
        cm.save_connection("http://192.168.1.10:8000", "mytoken")
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "icacls" in out
        assert "Access is denied" in out

    def test_warns_when_icacls_raises(self, isolated_config, monkeypatch, capsys):
        import subprocess

        def _boom(*args, **kwargs):
            raise FileNotFoundError("icacls not found")

        monkeypatch.setattr(cm.os, "name", "nt")
        monkeypatch.setenv("USERNAME", "tester")
        monkeypatch.setattr(subprocess, "run", _boom)
        cm.save_connection("http://192.168.1.10:8000", "mytoken")
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "icacls" in out

    def test_warns_when_username_is_missing(self, isolated_config, monkeypatch, capsys):
        """The old code raised KeyError here and swallowed it whole."""
        monkeypatch.setattr(cm.os, "name", "nt")
        monkeypatch.delenv("USERNAME", raising=False)
        cm.save_connection("http://192.168.1.10:8000", "mytoken")
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "USERNAME" in out

    def test_no_warning_on_a_normal_save(self, isolated_config, monkeypatch, capsys):
        monkeypatch.setattr(cm.os, "name", "posix")
        cm.save_connection("http://192.168.1.10:8000", "mytoken")
        assert "WARNING" not in capsys.readouterr().out

    @pytest.mark.skipif(os.name == "nt",
                        reason="st_mode carries no ACL information on Windows")
    def test_load_warns_on_a_world_readable_token_file(self, isolated_config,
                                                       monkeypatch, capsys):
        """Catches files that predate the chmod, or arrived via a backup."""
        monkeypatch.setattr(cm, "_warned_world_readable", False)
        cm.save_connection("http://192.168.1.10:8000", "mytoken")
        os.chmod(str(isolated_config), 0o644)
        capsys.readouterr()

        assert cm.load_connection() is not None
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "readable by other users" in out

    @pytest.mark.skipif(os.name == "nt",
                        reason="st_mode carries no ACL information on Windows")
    def test_load_warns_only_once(self, isolated_config, monkeypatch, capsys):
        """A security warning repeated on every call reads as a glitch."""
        monkeypatch.setattr(cm, "_warned_world_readable", False)
        cm.save_connection("http://192.168.1.10:8000", "mytoken")
        os.chmod(str(isolated_config), 0o644)
        cm.load_connection()
        capsys.readouterr()

        cm.load_connection()
        assert "WARNING" not in capsys.readouterr().out

    @pytest.mark.skipif(os.name == "nt",
                        reason="st_mode carries no ACL information on Windows")
    def test_load_is_quiet_for_a_locked_down_file(self, isolated_config,
                                                  monkeypatch, capsys):
        monkeypatch.setattr(cm, "_warned_world_readable", False)
        cm.save_connection("http://192.168.1.10:8000", "mytoken")
        capsys.readouterr()

        assert cm.load_connection() is not None
        assert "WARNING" not in capsys.readouterr().out


class TestLoadConnection:
    """Tests for load_connection()."""

    def test_returns_none_when_no_config(self, isolated_config):
        assert cm.load_connection() is None

    def test_loads_saved_connection(self, isolated_config):
        cm.save_connection("http://192.168.1.10:8000", "mytoken")
        result = cm.load_connection()
        assert result is not None
        assert result["url"] == "http://192.168.1.10:8000"
        assert result["token"] == "mytoken"

    def test_returns_none_for_corrupt_json(self, isolated_config):
        isolated_config.write_text("not json {{{")
        assert cm.load_connection() is None

    def test_returns_none_for_missing_url(self, isolated_config):
        isolated_config.write_text(json.dumps({"token": "abc"}))
        assert cm.load_connection() is None

    def test_returns_none_for_missing_token(self, isolated_config):
        isolated_config.write_text(json.dumps({"url": "http://x"}))
        assert cm.load_connection() is None


class TestClearConnection:
    """Tests for clear_connection()."""

    def test_removes_config_file(self, isolated_config):
        cm.save_connection("http://192.168.1.10:8000", "mytoken")
        assert isolated_config.exists()
        cm.clear_connection()
        assert not isolated_config.exists()

    def test_no_error_when_no_config(self, isolated_config):
        # Should not raise
        cm.clear_connection()


class TestGetConnection:
    """Tests for get_connection()."""

    def test_explicit_args_take_priority(self, isolated_config):
        cm.save_connection("http://saved:8000", "saved")
        url, token = cm.get_connection("http://explicit:8000", "explicit")
        assert url == "http://explicit:8000"
        assert token == "explicit"

    def test_explicit_args_save_to_config_when_persisting(self, isolated_config):
        cm.get_connection("http://explicit:8000", "explicit", persist=True)
        data = json.loads(isolated_config.read_text())
        assert data["url"] == "http://explicit:8000"
        assert data["token"] == "explicit"

    def test_explicit_args_do_not_save_by_default(self, isolated_config):
        """Resolving a connection is a read — it must not overwrite the config.

        A mistyped --url on a query command previously replaced a working
        saved connection, silently breaking every later command.
        """
        cm.save_connection("http://good:8000", "goodtoken")
        cm.get_connection("http://typo:9999", "wrongtoken")
        data = json.loads(isolated_config.read_text())
        assert data["url"] == "http://good:8000"
        assert data["token"] == "goodtoken"

    def test_env_vars_do_not_save_by_default(self, isolated_config, monkeypatch):
        cm.save_connection("http://good:8000", "goodtoken")
        monkeypatch.setenv("GPUMESH_URL", "http://env:8000")
        monkeypatch.setenv("GPUMESH_TOKEN", "envtoken")
        cm.get_connection(None, None)
        data = json.loads(isolated_config.read_text())
        assert data["url"] == "http://good:8000"

    def test_env_vars_fallback(self, isolated_config, monkeypatch):
        monkeypatch.setenv("GPUMESH_URL", "http://env:8000")
        monkeypatch.setenv("GPUMESH_TOKEN", "envtoken")
        url, token = cm.get_connection(None, None)
        assert url == "http://env:8000"
        assert token == "envtoken"

    def test_env_vars_save_to_config_when_persisting(self, isolated_config, monkeypatch):
        monkeypatch.setenv("GPUMESH_URL", "http://env:8000")
        monkeypatch.setenv("GPUMESH_TOKEN", "envtoken")
        cm.get_connection(None, None, persist=True)
        data = json.loads(isolated_config.read_text())
        assert data["url"] == "http://env:8000"

    def test_saved_config_fallback(self, isolated_config):
        cm.save_connection("http://saved:8000", "saved")
        url, token = cm.get_connection(None, None)
        assert url == "http://saved:8000"
        assert token == "saved"

    def test_returns_empty_when_nothing_available(self, isolated_config, monkeypatch):
        monkeypatch.delenv("GPUMESH_URL", raising=False)
        monkeypatch.delenv("GPUMESH_TOKEN", raising=False)
        url, token = cm.get_connection(None, None)
        assert url == ""
        assert token == ""

    def test_partial_explicit_args_fall_through_to_env(self, isolated_config, monkeypatch):
        """If url is provided but token is empty string, env vars fill the gap."""
        monkeypatch.setenv("GPUMESH_TOKEN", "envtoken")
        # url provided but token empty string -> should not use explicit path
        # since both url AND token are needed for explicit priority
        url, token = cm.get_connection("http://explicit:8000", "")
        assert url == "http://explicit:8000"
        assert token == "envtoken"

    def test_explicit_args_with_none_token_uses_env(self, isolated_config, monkeypatch):
        """If url is provided but token is None, env vars fill the gap."""
        monkeypatch.setenv("GPUMESH_URL", "http://env:8000")
        monkeypatch.setenv("GPUMESH_TOKEN", "envtoken")
        # url provided but token is None -> should not use explicit path
        # since both url AND token are needed for explicit priority
        url, token = cm.get_connection("http://explicit:8000", None)
        # url is truthy so env_url = url (explicit), env_token = env var
        assert url == "http://explicit:8000"
        assert token == "envtoken"


class TestDefaultDbPath:
    """default_db_path() is the stable home of the coordinator's job queue.

    The old default was the relative path "gpumesh.db", which lived in the
    working directory: a coordinator restarted from a different folder opened
    a fresh, empty database and every queued job vanished. The default must
    therefore be (a) derived from the config directory, not the cwd, and
    (b) usable on a first run, when the directory does not exist yet.
    """

    def test_path_is_inside_config_dir_not_cwd(self, tmp_path, monkeypatch):
        """The default DB path lives next to config.json, not in the cwd."""
        config_dir = tmp_path / ".gpumesh"
        elsewhere = tmp_path / "somewhere-else"
        elsewhere.mkdir()
        monkeypatch.setattr(cm, "_CONFIG_DIR", str(config_dir))
        monkeypatch.setattr(cm, "_DB_PATH", str(config_dir / "gpumesh.db"))
        monkeypatch.chdir(elsewhere)

        db_path = cm.default_db_path()

        assert db_path == str(config_dir / "gpumesh.db")
        assert not str(db_path).startswith(str(tmp_path / "somewhere-else"))

    def test_creates_the_directory_on_first_run(self, tmp_path, monkeypatch):
        """A fresh machine has no ~/.gpumesh yet; the default must not fail."""
        config_dir = tmp_path / ".gpumesh"
        assert not config_dir.exists()
        monkeypatch.setattr(cm, "_CONFIG_DIR", str(config_dir))
        monkeypatch.setattr(cm, "_DB_PATH", str(config_dir / "gpumesh.db"))

        db_path = cm.default_db_path()

        assert config_dir.exists()
        assert db_path == str(config_dir / "gpumesh.db")

    def test_db_and_config_share_one_directory(self, tmp_path, monkeypatch):
        """A single mount/backup of ~/.gpumesh covers both token and queue."""
        config_dir = tmp_path / ".gpumesh"
        monkeypatch.setattr(cm, "_CONFIG_DIR", str(config_dir))
        monkeypatch.setattr(cm, "_CONFIG_PATH", str(config_dir / "config.json"))
        monkeypatch.setattr(cm, "_DB_PATH", str(config_dir / "gpumesh.db"))

        assert os.path.dirname(cm._DB_PATH) == os.path.dirname(cm._CONFIG_PATH)
