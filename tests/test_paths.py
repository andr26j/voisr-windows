import sys

import voisr.paths as paths


def test_find_binary_prefers_bundled_bin(tmp_path, monkeypatch):
    bundled = tmp_path / "whisper-cli.exe"
    bundled.write_bytes(b"")
    monkeypatch.setattr(paths, "BIN_DIR", tmp_path)
    assert paths.find_binary("whisper-cli") == str(bundled)


def test_find_binary_appends_exe_suffix(tmp_path, monkeypatch):
    bundled = tmp_path / "ffmpeg.exe"
    bundled.write_bytes(b"")
    monkeypatch.setattr(paths, "BIN_DIR", tmp_path)
    assert paths.find_binary("ffmpeg") == str(bundled)


def test_find_binary_does_not_double_suffix(tmp_path, monkeypatch):
    bundled = tmp_path / "ffmpeg.exe"
    bundled.write_bytes(b"")
    monkeypatch.setattr(paths, "BIN_DIR", tmp_path)
    assert paths.find_binary("ffmpeg.exe") == str(bundled)


def test_find_binary_falls_back_to_path(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "BIN_DIR", tmp_path)
    monkeypatch.setattr(paths.shutil, "which", lambda name: r"C:\tools\ffmpeg.exe")
    assert paths.find_binary("ffmpeg") == r"C:\tools\ffmpeg.exe"


def test_find_binary_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "BIN_DIR", tmp_path)
    monkeypatch.setattr(paths.shutil, "which", lambda name: None)
    assert paths.find_binary("nope") is None


def test_app_data_dir_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.app_data_dir() == tmp_path / "Voisr"


def test_app_data_dir_falls_back_without_env(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    result = paths.app_data_dir()
    assert result.name == "Voisr"
    assert "AppData" in str(result)


def test_app_icon_returns_path_when_present(tmp_path, monkeypatch):
    icon = tmp_path / "voisr.ico"
    icon.write_bytes(b"")
    monkeypatch.setattr(paths, "ASSETS_DIR", tmp_path)
    assert paths.app_icon() == str(icon)


def test_app_icon_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ASSETS_DIR", tmp_path)
    assert paths.app_icon() is None


def test_real_app_icon_exists():
    """Guards the actual shipped asset. Without it pywebview falls back to
    extracting pythonw.exe's icon and the taskbar shows Python's logo."""
    assert paths.app_icon() is not None


def test_resource_root_is_repo_root_when_not_frozen(monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert paths.resource_root() == paths.REPO_ROOT


def test_resource_root_is_meipass_when_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.resource_root() == tmp_path


def test_bin_dir_follows_resource_root(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.bin_dir() == tmp_path / "bin"


def test_models_dir_follows_resource_root(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.models_dir() == tmp_path / "models"


def test_web_dir_follows_resource_root(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.web_dir() == tmp_path / "voisr" / "web"


def test_assets_dir_follows_resource_root(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.assets_dir() == tmp_path / "assets"


def test_app_data_dir_is_unaffected_by_freezing(monkeypatch, tmp_path):
    """User data must NEVER move into the bundle -- it would be wiped on
    every upgrade, and _MEIPASS is a temp dir for one-file builds."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert "Voisr" in str(paths.app_data_dir())
    assert str(tmp_path) not in str(paths.app_data_dir())
