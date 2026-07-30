"""PyInstaller entry point. A separate script (not voisr/ui.py itself) so
the frozen build imports voisr as a package rather than running ui.py as a
bare top-level script."""
from voisr.ui import main

if __name__ == "__main__":
    main()
