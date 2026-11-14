"""Desktop launcher kept for backwards compatibility.

The real code lives in the ``mcpmanager`` package; the CLI is
``python -m mcpmanager`` and the GUI is ``python -m mcpmanager gui``.
"""

from mcpmanager.gui import main

if __name__ == "__main__":
    main()
