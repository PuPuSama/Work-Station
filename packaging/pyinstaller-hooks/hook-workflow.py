"""PyInstaller hook for the application's local ``workflow`` package.

The contrib hook with the same name targets an unrelated PyPI distribution
and tries to copy metadata that this local package intentionally does not have.
"""

hiddenimports: list[str] = []
datas: list[tuple[str, str]] = []
