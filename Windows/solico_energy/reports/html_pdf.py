from __future__ import annotations
import base64, os, shutil, subprocess, tempfile, time, threading
from pathlib import Path

_PDF_RENDER_LOCK = threading.RLock()


def img_data(path):
    if not path:
        return ''
    p = Path(path)
    if not p.exists():
        return ''
    mime = 'image/png' if p.suffix.lower() == '.png' else 'image/jpeg'
    return f'data:{mime};base64,' + base64.b64encode(p.read_bytes()).decode('ascii')


def _registry_browser_paths():
    """Discover Edge/Chrome from Windows App Paths registry entries."""
    if os.name != 'nt':
        return []
    result = []
    try:
        import winreg
        roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
        names = ('msedge.exe', 'chrome.exe')
        prefixes = (
            r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths',
            r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths',
        )
        access_modes = [winreg.KEY_READ]
        for extra_name in ('KEY_WOW64_64KEY', 'KEY_WOW64_32KEY'):
            extra = getattr(winreg, extra_name, 0)
            if extra:
                access_modes.append(winreg.KEY_READ | extra)
        for root in roots:
            for prefix in prefixes:
                for name in names:
                    key_path = prefix + '\\' + name
                    for access in access_modes:
                        try:
                            with winreg.OpenKey(root, key_path, 0, access) as key:
                                value, _ = winreg.QueryValueEx(key, None)
                            if value:
                                result.append(Path(str(value).strip('"')))
                                break
                        except OSError:
                            continue
    except Exception:
        pass
    return result


def find_browsers():
    """Return installed Edge/Chrome executables in preference order.

    Detection deliberately uses several independent methods because locked-down plant PCs can
    expose different environment variables even though Edge is installed.
    """
    candidates = []
    if os.name == 'nt':
        # Registry is the most reliable source on Windows.
        candidates.extend(_registry_browser_paths())

        # Standard environment-driven locations.
        env_keys = ('PROGRAMFILES(X86)', 'PROGRAMFILES', 'LOCALAPPDATA')
        for key in env_keys:
            b = os.environ.get(key)
            if not b:
                continue
            base = Path(b)
            candidates.extend([
                base / 'Microsoft' / 'Edge' / 'Application' / 'msedge.exe',
                base / 'Google' / 'Chrome' / 'Application' / 'chrome.exe',
            ])

        # Hard-coded standard paths as a fallback for unusual/frozen environments.
        system_drive = os.environ.get('SystemDrive', 'C:')
        for base in (
            Path(system_drive + r'\Program Files (x86)'),
            Path(system_drive + r'\Program Files'),
        ):
            candidates.extend([
                base / 'Microsoft' / 'Edge' / 'Application' / 'msedge.exe',
                base / 'Google' / 'Chrome' / 'Application' / 'chrome.exe',
            ])
        local = os.environ.get('LOCALAPPDATA')
        if local:
            candidates.extend([
                Path(local) / 'Microsoft' / 'Edge' / 'Application' / 'msedge.exe',
                Path(local) / 'Google' / 'Chrome' / 'Application' / 'chrome.exe',
            ])
    else:
        candidates.extend([
            Path('/usr/bin/microsoft-edge'), Path('/usr/bin/microsoft-edge-stable'),
            Path('/usr/bin/google-chrome'), Path('/usr/bin/chromium'),
        ])

    for name in (
        'msedge', 'msedge.exe', 'microsoft-edge', 'microsoft-edge-stable',
        'chrome', 'chrome.exe', 'google-chrome', 'chromium', 'chromium.exe'
    ):
        q = shutil.which(name)
        if q:
            candidates.append(Path(q))

    seen = set()
    result = []
    for p in candidates:
        try:
            p = Path(p)
            key = str(p.resolve()).lower()
        except Exception:
            key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            if p.is_file():
                result.append(str(p))
        except Exception:
            continue
    return result


def find_browser():
    xs = find_browsers()
    return xs[0] if xs else None


def _pdf_ready(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and path.stat().st_size >= 1000
    except Exception:
        return False


def _pdf_has_browser_error(path: Path) -> bool:
    """Detect an Edge/Chrome error page accidentally printed as a PDF."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:3])
        low = text.lower()
        return (
            "err_file_not_found" in low
            or "file not found" in low
            or "it may have been moved, edited, or deleted" in low
        )
    except Exception:
        # If extraction itself fails, do not reject here; merge/read later will
        # still raise a proper PDF error.
        return False


def _wait_for_pdf(path: Path, seconds: float = 2.0) -> bool:
    """Chromium can flush the PDF a fraction of a second after the process result is available."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _pdf_ready(path):
            return True
        time.sleep(0.10)
    return _pdf_ready(path)


def _best_effort_remove_tree(path):
    """Remove a Chromium temp/profile tree without ever failing report creation.

    On Windows, Edge/Chrome crashpad can keep CrashpadMetrics-active.pma open briefly after
    the browser process exits.  TemporaryDirectory.__exit__ treats that lock as fatal and used
    to turn a successfully-created PDF into WinError 5.  Cleanup here is deliberately non-fatal.
    """
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def _cleanup_old_pdf_temp_dirs():
    """Clean only old temp folders; never remove another active report render."""
    try:
        base = Path(tempfile.gettempdir())
        now = time.time()
        for p in base.glob('SolicoEnergyPDF_*'):
            if not p.is_dir():
                continue
            try:
                age = now - p.stat().st_mtime
            except Exception:
                continue
            # A currently running Edge/Chrome render can legitimately live here
            # for over a minute. Only purge folders older than 30 minutes.
            if age > 30 * 60:
                _best_effort_remove_tree(p)
    except Exception:
        pass


def _browser_pdf(html_text, out_pdf):
    browsers = find_browsers()
    if not browsers:
        raise RuntimeError(
            'برای ساخت PDF، Microsoft Edge یا Google Chrome روی این ویندوز پیدا نشد. '
            'وجود مرورگر را بررسی کنید.'
        )

    out_pdf = Path(out_pdf).expanduser().resolve()
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    errors = []

    # IMPORTANT: do not use tempfile.TemporaryDirectory here.  Chromium crashpad can keep a
    # profile file locked for a short period after exit on Windows, making __exit__ raise
    # PermissionError even though the PDF itself was created successfully.
    _cleanup_old_pdf_temp_dirs()
    td = tempfile.mkdtemp(prefix='SolicoEnergyPDF_')
    tdp = Path(td)
    try:
        html_file = tdp / 'report.html'
        html_file.write_text(html_text, encoding='utf-8')

        for browser_index, browser in enumerate(browsers):
            # New Chromium uses --headless=new; older Edge/Chrome only accepts --headless.
            for mode_index, headless in enumerate(('--headless=new', '--headless')):
                profile = tdp / f'profile-{browser_index}-{mode_index}'
                try:
                    out_pdf.unlink(missing_ok=True)
                except Exception:
                    pass

                args = [
                    browser,
                    headless,
                    '--disable-gpu',
                    '--disable-extensions',
                    '--disable-background-networking',
                    '--disable-default-apps',
                    '--disable-component-update',
                    '--disable-breakpad',
                    '--disable-crash-reporter',
                    '--disable-sync',
                    '--noerrdialogs',
                    '--no-first-run',
                    '--no-default-browser-check',
                    '--no-service-autorun',
                    '--allow-file-access-from-files',
                    '--run-all-compositor-stages-before-draw',
                    '--virtual-time-budget=2500',
                    '--no-pdf-header-footer',
                    f'--user-data-dir={profile}',
                    f'--print-to-pdf={str(out_pdf)}',
                    html_file.as_uri(),
                ]
                if os.name != 'nt':
                    args.insert(2, '--no-sandbox')

                try:
                    cp = subprocess.run(
                        args,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=75,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                    )
                    # Edge/Chrome can return a warning/non-zero status while still producing a
                    # perfectly valid PDF. File existence is the source of truth.
                    if _wait_for_pdf(out_pdf) and not _pdf_has_browser_error(out_pdf):
                        return str(out_pdf)
                    stderr = cp.stderr.decode(errors='ignore')[-900:].strip()
                    stdout = cp.stdout.decode(errors='ignore')[-900:].strip()
                    detail = stderr or stdout or f'exit code {cp.returncode}'
                    errors.append(f'{Path(browser).name} {headless}: {detail}')
                except subprocess.TimeoutExpired as e:
                    if _wait_for_pdf(out_pdf, 0.5) and not _pdf_has_browser_error(out_pdf):
                        return str(out_pdf)
                    detail = ''
                    if getattr(e, 'stderr', None):
                        try:
                            detail = e.stderr.decode(errors='ignore')[-500:].strip()
                        except Exception:
                            pass
                    errors.append(f'{Path(browser).name} {headless}: timeout {detail}'.strip())
                except Exception as e:
                    # If the browser wrote the PDF before another non-critical exception, accept it.
                    if _wait_for_pdf(out_pdf, 0.3) and not _pdf_has_browser_error(out_pdf):
                        return str(out_pdf)
                    errors.append(f'{Path(browser).name} {headless}: {e}')
    finally:
        # Never allow a locked Crashpad/profile file to turn a successful PDF into a failure.
        _best_effort_remove_tree(tdp)

    detail = ' | '.join(errors[-4:])
    raise RuntimeError(
        'مرورگر پیدا شد، اما نتوانست PDF را بسازد.' +
        (f'\nجزئیات: {detail}' if detail else '')
    )

def _weasy_pdf(html_text, out_pdf):
    """Development/Linux fallback only; Windows installations do not depend on WeasyPrint."""
    try:
        from weasyprint import HTML
    except Exception as e:
        raise RuntimeError('موتور جایگزین PDF نصب نیست.') from e
    HTML(string=html_text).write_pdf(str(out_pdf))
    return str(out_pdf)


def html_to_pdf(html_text, out_pdf, landscape=False):
    """Create the report PDF offline without concurrent Chromium temp races."""
    with _PDF_RENDER_LOCK:
        if os.name == 'nt':
            return _browser_pdf(html_text, out_pdf)
        try:
            return _weasy_pdf(html_text, out_pdf)
        except Exception:
            return _browser_pdf(html_text, out_pdf)


def merge_pdfs(paths, out_pdf):
    from pypdf import PdfWriter
    out_pdf = Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    try:
        for p in paths:
            writer.append(str(p))
        with open(out_pdf, 'wb') as f:
            writer.write(f)
    finally:
        writer.close()
    if not _pdf_ready(out_pdf):
        raise RuntimeError('فایل PDF نهایی ساخته نشد.')
    return str(out_pdf)
