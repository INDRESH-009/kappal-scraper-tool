# Windows Packaging

This app is packaged as a local desktop launcher:

1. The user opens `Kappal Rate Capture.exe`.
2. The launcher starts the FastAPI backend on a free `127.0.0.1` port.
3. The launcher opens the browser automatically.
4. Runtime data is stored under `%LOCALAPPDATA%\KappalRateCapture`.

## Build Machine Setup

Build on a Windows machine, not macOS. PyInstaller builds Windows `.exe` files only from Windows.

Recommended:

- Python 3.11 or 3.12
- Git
- Internet access for installing Python dependencies and Playwright Chromium

## Build

From the project folder on Windows:

```bat
build_windows.bat
```

The build script sets:

```bat
PLAYWRIGHT_BROWSERS_PATH=0
```

This tells Playwright to install Chromium inside the Python package so PyInstaller can bundle it.

The output will be:

```text
dist\Kappal Rate Capture.exe
```

## Client Runtime Data

The packaged app writes user-specific files here:

```text
%LOCALAPPDATA%\KappalRateCapture
```

That folder contains:

- `batch_uploads`
- `debug`
- `kappal-auth-profile`

Do not bundle your own `.kappal-auth-profile` or old uploaded workbooks.

## Supabase Config

The packaged app uses the public Supabase URL and publishable key in `server.py`.

Do not put any of these in the package:

- Supabase secret key
- service role key
- database password

## Installer

For first testing, you can send the `.exe` directly.

For a proper client installer, use Inno Setup and include:

```text
dist\Kappal Rate Capture.exe
```

Install target suggestion:

```text
%LOCALAPPDATA%\Programs\Kappal Rate Capture
```

## Client Instructions

1. Install/open Kappal Rate Capture.
2. Browser opens automatically.
3. Sign in with the tool account you created in Supabase.
4. Upload batch workbook or start capture.
5. When Kappal opens, log into Kappal separately if prompted.

No terminal, Python, or VS Code is needed on the client laptop.
