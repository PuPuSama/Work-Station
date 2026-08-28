# Windows portable package

Build the operator-facing portable ZIP from the repository root:

```powershell
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements-build.txt
.\packaging\build-portable.ps1
```

The build creates:

- `dist\ArticleAgent-Portable\`
- `dist\ArticleAgent-Portable.zip`

The package contains a PyInstaller backend, a production Next.js server with a
bundled Node runtime, a clean per-operator data workspace, the configured
humanization prompt, and one selected environment file. The build prefers the
root `.env` and falls back to the legacy `backend/.env` only when the root file
does not exist. Treat the ZIP as confidential because it contains API keys.
