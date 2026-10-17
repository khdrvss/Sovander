"""Shared helpers for the approach-B spike: LibreOffice (snap) + PyMuPDF.

UNO import trick: the snap ships pyuno built for Python 3.12 (same as the host),
so the host venv can import it once LD_LIBRARY_PATH/PYTHONPATH/URE_BOOTSTRAP
point at the snap's program dir. Set those env vars BEFORE starting Python
(LD_LIBRARY_PATH is read by the dynamic loader at startup); `uno_env()` returns them.
"""
import json
import os
import subprocess
import time
from pathlib import Path

SPIKE = Path(__file__).resolve().parents[1]
HERE = SPIKE / "b-libreoffice"
CORPUS = SPIKE / "corpus"
RESULTS = SPIKE / "results"
LO_BIN = "/snap/bin/libreoffice"
LO_PROGRAM = "/snap/libreoffice/current/lib/libreoffice/program"
PROFILE_URL = "file://" + str(HERE / "lo-profile")
PORT = 2002

SHEET_EXT = {".xlsx", ".xls", ".ods", ".xltx", ".csv", ".xlsm"}


def uno_env():
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = LO_PROGRAM + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    env["PYTHONPATH"] = LO_PROGRAM
    env["URE_BOOTSTRAP"] = "vnd.sun.star.pathname:" + LO_PROGRAM + "/fundamentalrc"
    return env


def cli_convert(src: Path, outdir: Path, target: str, timeout: float = 60.0, profile_url: str = PROFILE_URL):
    """Cold path: one `soffice --convert-to` process per file. Returns (status, stderr, out_path, seconds).

    soffice exits 0 even when it fails, so success = expected output file exists and is non-empty.
    `target` like "pdf" or 'html:HTML (StarCalc)'; output name = src stem + target extension.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    ext = target.split(":", 1)[0]
    out = outdir / (src.stem + "." + ext)
    if out.exists():
        out.unlink()
    cmd = [LO_BIN, "-env:UserInstallation=" + profile_url, "--headless", "--norestore",
           "--nolockcheck", "--convert-to", target, "--outdir", str(outdir), str(src)]
    t0 = time.perf_counter()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         start_new_session=True)  # own process group -> killpg only hits our soffice
    try:
        so, se = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(p.pid)
        p.communicate()
        return "timeout", "timeout after %ss" % timeout, None, time.perf_counter() - t0
    dt = time.perf_counter() - t0
    stderr = (se or "").strip()
    if out.exists() and out.stat().st_size > 0:
        return "ok", stderr, out, dt
    return "error", (stderr or (so or "").strip() or "no output file")[-2000:], None, dt


def kill_group(pgid):
    import signal
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def render_first_page(pdf_path: Path, png_path: Path = None, width: int = 1000):
    """Open PDF with PyMuPDF, render page 1 at ~width px. Returns (page_count, seconds_open+render)."""
    import pymupdf
    t0 = time.perf_counter()
    doc = pymupdf.open(str(pdf_path))
    n = doc.page_count
    if n:
        page = doc[0]
        zoom = width / max(page.rect.width, 1)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        if png_path:
            png_path.parent.mkdir(parents=True, exist_ok=True)
            pix.save(str(png_path))
    dt = time.perf_counter() - t0
    doc.close()
    return n, dt


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False))


# ---------------------------------------------------------------- warm mode (UNO)

WRITER = {".docx", ".doc", ".odt", ".rtf", ".docm", ".dotx", ".txt"}
IMPRESS = {".pptx", ".ppt", ".pptm", ".odp", ".ppsx", ".potx"}


def pdf_filter_for(path: Path):
    ext = path.suffix.lower()
    if ext in SHEET_EXT:
        return "calc_pdf_Export"
    if ext in IMPRESS:
        return "impress_pdf_Export"
    return "writer_pdf_Export"


def page1_target(path: Path):
    """CLI target string for first-page-only PDF export (JSON filter options, LO >= 7.4)."""
    return 'pdf:%s:{"PageRange":{"type":"string","value":"1"}}' % pdf_filter_for(path)


def start_warm(port: int = PORT, profile_url: str = PROFILE_URL, log_path: Path = None):
    """Start a persistent headless soffice listening on 127.0.0.1:<port> (UNO urp). Returns Popen."""
    cmd = [LO_BIN, "-env:UserInstallation=" + profile_url, "--headless", "--invisible", "--norestore",
           "--nologo", "--nodefault", "--nolockcheck",
           "--accept=socket,host=127.0.0.1,port=%d,tcpNoDelay=1;urp;StarOffice.ComponentContext" % port]
    log = open(log_path or (HERE / "warm-soffice.log"), "ab")
    return subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)


class WarmLO:
    """UNO client for a running soffice. Needs uno importable (see uno_env())."""

    def __init__(self, port: int = PORT, wait: float = 90.0):
        import uno
        self.uno = uno
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
        url = "uno:socket,host=127.0.0.1,port=%d;urp;StarOffice.ComponentContext" % port
        t0 = time.perf_counter()
        while True:
            try:
                self.ctx = resolver.resolve(url)
                break
            except Exception:
                if time.perf_counter() - t0 > wait:
                    raise
                time.sleep(0.2)
        self.connect_s = time.perf_counter() - t0
        self.desktop = self.ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)

    def _props(self, **kw):
        from com.sun.star.beans import PropertyValue
        out = []
        for k, v in kw.items():
            p = PropertyValue()
            p.Name, p.Value = k, v
            out.append(p)
        return tuple(out)

    def load(self, src: Path):
        # Untrusted input: never run macros, never update links, read-only, hidden.
        props = self._props(Hidden=True, ReadOnly=True, MacroExecutionMode=0, UpdateDocMode=0)
        doc = self.desktop.loadComponentFromURL(self.uno.systemPathToFileUrl(str(src)), "_blank", 0, props)
        if doc is None:
            raise RuntimeError("loadComponentFromURL returned None")
        return doc

    def export(self, doc, out: Path, filter_name: str, page1: bool = False):
        fd = ()
        if page1:
            fd = self.uno.Any("[]com.sun.star.beans.PropertyValue", self._props(PageRange="1"))
        kw = {"FilterName": filter_name, "Overwrite": True}
        if page1:
            kw["FilterData"] = fd
        doc.storeToURL(self.uno.systemPathToFileUrl(str(out)), self._props(**kw))

    @staticmethod
    def doc_pdf_filter(doc, src: Path):
        try:
            if doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
                return "calc_pdf_Export"
            if doc.supportsService("com.sun.star.presentation.PresentationDocument"):
                return "impress_pdf_Export"
            if doc.supportsService("com.sun.star.drawing.DrawingDocument"):
                return "draw_pdf_Export"
            if doc.supportsService("com.sun.star.text.TextDocument"):
                return "writer_pdf_Export"
        except Exception:
            pass
        return pdf_filter_for(src)

    def convert_pdf(self, src: Path, out: Path, page1: bool = False):
        """Load + export PDF + close. Returns dict of phase timings."""
        t0 = time.perf_counter()
        doc = self.load(src)
        t1 = time.perf_counter()
        try:
            self.export(doc, out, self.doc_pdf_filter(doc, src), page1=page1)
        finally:
            t2 = time.perf_counter()
            try:
                doc.close(True)
            except Exception:
                try:
                    doc.dispose()
                except Exception:
                    pass
        t3 = time.perf_counter()
        return {"load_s": t1 - t0, "export_s": t2 - t1, "close_s": t3 - t2}

    def terminate(self):
        try:
            self.desktop.terminate()
        except Exception:
            pass  # bridge drops when soffice exits; that is expected
