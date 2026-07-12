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

    def test_explicit_args_save_to_config(self, isolated_config):
        cm.get_connection("http://explicit:8000", "explicit")
        data = json.loads(isolated_config.read_text())
        assert data["url"] == "http://explicit:8000"
        assert data["token"] == "explicit"

    def test_env_vars_fallback(self, isolated_config, monkeypatch):
        monkeypatch.setenv("GPUMESH_URL", "http://env:8000")
        monkeypatch.setenv("GPUMESH_TOKEN", "envtoken")
        url, token = cm.get_connection(None, None)
        assert url == "http://env:8000"
        assert token == "envtoken"

    def test_env_vars_save_to_config(self, isolated_config, monkeypatch):
        monkeypatch.setenv("GPUMESH_URL", "http://env:8000")
        monkeypatch.setenv("GPUMESH_TOKEN", "envtoken")
        cm.get_connection(None, None)
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
