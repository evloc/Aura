# Rebuilding the exe

**Easiest:** install Python 3.12 for Windows (tick "Add python.exe to PATH"),
then double-click `build.bat`. It sets up a private build environment, builds,
leaves AURA in `dist\AURA\` (run `AURA.exe` there; keep the folder together)
and runs its self-test.

**Releases and auto-updates:** see README → *Sharing AURA and updating it*.
The GitHub workflow in `.github/workflows/release.yml` builds AURA and
`AURA-Setup.exe` (Inno Setup, `installer.iss`) whenever you push a `v*` tag.

Doing it by hand:

Folder layout (the page must sit in `templates/`):

    app.py
    icon.ico
    requirements.txt
    templates/index.html

Windows (or Linux + Wine), Python 3.12:

    pip install -r requirements.txt pyinstaller
    pyinstaller --noconfirm --clean --onedir --noconsole ^
      --name AURA --icon icon.ico ^
      --add-data "templates;templates" ^
      --add-data "version.json;." ^
      --collect-all soundfile ^
      --collect-all faster_whisper ^
      --collect-all ctranslate2 ^
      --collect-all av ^
      --collect-all onnxruntime ^
      --collect-all webview ^
      --collect-all pythonnet ^
      --collect-all clr_loader ^
      --hidden-import _cffi_backend ^
      app.py

Result lands in `dist/AURA/` (`AURA.exe` plus `_internal/`). The library still lives in `%APPDATA%\Unreleased\`,
so an existing Unreleased library opens as-is.

Gotchas, learned the hard way:
- Pin numpy to 1.x. numpy 2.x calls ucrtbase functions Wine 9 doesn't implement,
  which breaks a Wine-hosted build (fine on real Windows).
- Do NOT add `--exclude-module setuptools`. It produces an exe that dies at
  `init_sys_streams` before any of the app's own code runs.
- Lyrics auto-sync uses faster-whisper (CTranslate2). The two `--collect-all`
  lines for it are needed, or the exe builds but auto-sync reports "isn't
  included in this build". The self-test prints a `lyrics auto-sync` line to
  confirm. It adds roughly 100 MB to the exe; the speech models themselves are
  not bundled and download on first use.
- `--collect-all av` brings the audio decoder/MP3 encoder used by *Convert to MP3*
  (the self-test prints an `mp3 converter` line).
- `--onedir`, not `--onefile`: a one-file exe unpacks ~250 MB to a temp folder
  on every start, which was most of AURA's start-up time. The installer ships
  the folder and clears the old `_internal` first.
- `--collect-all onnxruntime` is for vocal separation; `webview`, `pythonnet`
  and `clr_loader` are AURA's own window (pywebview on WebView2). The
  self-test prints `vocal isolation`, `own window` and `window component`
  lines. If the window component doesn't load, AURA still works in an Edge
  window.
- `--noconsole` hides the black terminal window. AURA quits when its last
  window closes (the page sends a goodbye; a 90-second watchdog is the backstop). The self-test then prints nothing on screen;
  read `%APPDATA%\Unreleased\selftest.txt` (build.bat shows it for you).
- To run from source instead: `python app.py`.
