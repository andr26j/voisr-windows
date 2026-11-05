"""PyInstaller entry point. A separate script (not voisr/ui.py itself) so
the frozen build imports voisr as a package rather than running ui.py as a
bare top-level script."""
import os
import sys


def self_test(report_path):
    """Import everything the frozen app ships and check its bundled files,
    without opening a window, hooking the keyboard or touching the mic.

    CI runs this against build/dist, so a module PyInstaller missed or a
    file the spec forgot fails the build instead of the owner's first
    launch. A windowed build has no stdout, so the result goes to a file.
    """
    lines = []
    ok = True
    try:
        import webview  # noqa: F401
        import voisr.ui  # noqa: F401 -- pulls in every runtime module
        import voisr.updater  # noqa: F401
        # imported inside functions only: a missing one would fail at the
        # first download or dictation, not at startup
        import voisr.models  # noqa: F401
        import voisr.whisper_guard  # noqa: F401
        import voisr.whisper_server  # noqa: F401
        import voisr.click_watch  # noqa: F401
        import voisr.uia  # noqa: F401
        import certifi
        from voisr.paths import app_icon, assets_dir, find_binary, models_dir, web_dir

        checks = {
            "whisper-cli": find_binary("whisper-cli"),
            "whisper-server": find_binary("whisper-server"),
            "certifi": os.path.isfile(certifi.where()) and certifi.where(),
            "index.html": (web_dir() / "index.html").is_file(),
            "widget.html": (web_dir() / "widget.html").is_file(),
            "model": (models_dir() / "ggml-base.en.bin").is_file(),
            "icon": app_icon(),
            # names on screen are told from words with it (voisr.common_words)
            "word list": (assets_dir() / "words.gz").is_file(),
        }
        server = checks["whisper-server"]
        if server:
            # loads its DLLs from bin\ and exits 0: a DLL missing from the
            # bundle would show here as 0xc0000135
            import subprocess
            code = subprocess.run([server, "--help"], capture_output=True,
                                  timeout=60, creationflags=0x08000000).returncode
            lines.append(f"whisper-server --help: exit code {code:#x}")
            checks["whisper-server starts"] = code == 0
        for name, value in checks.items():
            lines.append(f"{name}: {value}")
            ok = ok and bool(value)
    except Exception:
        import traceback
        lines.append(traceback.format_exc())
        ok = False
    lines.append("OK" if ok else "FAILED")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        sys.exit(self_test(sys.argv[2]))
    from voisr.ui import main
    main()
