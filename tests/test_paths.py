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
