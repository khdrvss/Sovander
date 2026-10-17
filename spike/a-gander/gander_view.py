"""
Approach A spike: open one Office file in Gander's shipped viewer page, in headless
Chromium, the way Gander's own tests/viewer suite does, and read back what it drew.

Shared by run_a.py (accuracy pass) and time_a.py (timing).

Signals, all taken from Gander's tests (tests/viewer/helpers.py and the test_* files):
  - which page: FileKind.kt's extension sets (docx/docm/dotx -> docx.html, odt/rtf/doc ->
    prose.html, xlsx/xls/ods/csv/xltx -> xlsx.html, pptx/pptm/ppsx/potx -> pptx.html)
  - URL: /assets/viewer/<page>?name=&ext=&ranged=0&length=<bytes>&night=0, the document
    at /doc/file.<ext> (server_a.py, a copy of tests/viewer/server.py)
  - rendered: #vw-status hidden (helpers.wait_until_done). Every Office page calls
    vwStatusDone() only once the document is drawn (docx: after Gander's Word pagination
    and second drawing; pptx: once PPTXjs's slides exist; xlsx: once the first sheet is in)
  - failed: #vw-status.vw-error visible (vwError, also app.js's window.onerror)
"""

import json
import time
from pathlib import Path


CAP_MS = 60_000
VIEWPORT = {"width": 1000, "height": 1400}

# FileKind.kt: wordExt, proseExt, sheetExt, slideExt
PAGE_FOR_EXT = {}
for _e in ("docx", "docm", "dotx"):
    PAGE_FOR_EXT[_e] = "docx.html"
for _e in ("odt", "ott", "fodt", "rtf", "doc", "dot"):
    PAGE_FOR_EXT[_e] = "prose.html"
for _e in ("xlsx", "xls", "xlsm", "xlsb", "xltx", "csv", "ods"):
    PAGE_FOR_EXT[_e] = "xlsx.html"
for _e in ("pptx", "ppsx", "pptm", "potx"):
    PAGE_FOR_EXT[_e] = "pptx.html"

# conftest.py: every page runs under ViewerPolicy's CSP; refusals are silent, so record them
POLICY_WATCH = """
window.__vwRefused = [];
document.addEventListener("securitypolicyviolation", function (e) {
  window.__vwRefused.push(e.effectiveDirective + " " + e.blockedURI);
});
"""

# Spike-only: stamps the moment the status card goes (or turns into an error), in ms since
# navigation start, so timing does not depend on how often the harness polls. Watches the
# whole document only until #vw-status is parsed, then that one element.
DONE_STAMP = """
(function () {
  function watch(el) {
    function check() {
      if (window.__vwSettledAt != null) return;
      if (el.classList.contains('vw-error')) { window.__vwSettledAt = performance.now(); window.__vwSettledHow = 'error'; }
      else if (el.style.display === 'none') { window.__vwSettledAt = performance.now(); window.__vwSettledHow = 'done'; }
    }
    new MutationObserver(check).observe(el, { attributes: true, attributeFilter: ['style', 'class'] });
    check();
  }
  var finder = new MutationObserver(function () {
    var el = document.getElementById('vw-status');
    if (el) { finder.disconnect(); watch(el); }
  });
  finder.observe(document, { childList: true, subtree: true });
})();
"""

# Done or failed, whichever comes first
# (Unlike helpers.wait_until_done, a missing card is not "done": right after navigation
# commits the new document is still empty, and every Office page has the card in its HTML.)
SETTLED = (
    "() => { const e = document.getElementById('vw-status');"
    " return !!e && (getComputedStyle(e).display === 'none'"
    " || e.classList.contains('vw-error')); }"
)

ERROR_TEXT = (
    "() => { const e = document.getElementById('vw-status');"
    " if (!e || getComputedStyle(e).display === 'none' || !e.classList.contains('vw-error')) return null;"
    " return e.innerText.replace(/\\s+/g, ' ').trim(); }"
)


def page_for(path):
    return PAGE_FOR_EXT.get(Path(path).suffix.lstrip(".").lower())


def new_page(browser, errors=None):
    context = browser.new_context(viewport=VIEWPORT)
    page = context.new_page()
    page.add_init_script(POLICY_WATCH + DONE_STAMP)
    if errors is not None:
        page.on("pageerror", lambda e: errors.append(str(e)[:300]))
    return context, page


def open_and_wait(page, server, path, cap_ms=CAP_MS):
    """
    Points the server at [path], loads the viewer page, waits for the rendered signal.
    Returns (state, elapsed_ms, error_text): state is "ok", "error" or "timeout".
    elapsed_ms is from navigation start to the signal.
    """
    html = page_for(path)
    server.show(Path(path))
    url = server.url(html)
    t0 = time.perf_counter()
    try:
        page.goto(url, wait_until="commit", timeout=cap_ms)
        left = max(1, cap_ms - int((time.perf_counter() - t0) * 1000))
        page.wait_for_function(SETTLED, timeout=left, polling=50)
    except Exception as e:  # playwright TimeoutError or a crashed target
        elapsed = (time.perf_counter() - t0) * 1000
        name = type(e).__name__
        if "Timeout" in name:
            return "timeout", elapsed, f"no rendered signal within {cap_ms // 1000} s"
        return "error", elapsed, f"{name}: {str(e).splitlines()[0][:300]}"
    elapsed = (time.perf_counter() - t0) * 1000
    err = page.evaluate(ERROR_TEXT)
    if err:
        return "error", elapsed, err
    return "ok", elapsed, None


# What each page drew, read after the signal. Kept to plain DOM queries.
MEASURE = r"""
(kind) => {
  const q = s => document.querySelectorAll(s).length;
  const out = { refused: (window.__vwRefused || []).slice(0, 10) };
  if (kind === 'docx.html') {
    out.count_kind = 'pages';
    out.count = q('.docx-wrapper > section.docx');
    const p = window.vwPages || {};
    out.docx = { split_on_word_marks: !!p.split, word_recorded_pages: p.wordPages || 0,
                 extra_turns: p.turnAt ? p.turnAt.length : 0, settled: !!p.settled };
  } else if (kind === 'prose.html') {
    out.count_kind = 'sheets_at_hard_breaks';
    out.count = q('.vw-paper > section');
    out.missing = q('.vw-nopicture');
  } else if (kind === 'pptx.html') {
    out.count_kind = 'slides';
    out.count = q('#result .slide');
    const boxes = [...document.querySelectorAll('#result .vw-missing')];
    out.missing = boxes.length;
    const kinds = {};
    boxes.forEach(b => { const k = b.textContent.trim() || '(too small for its label)';
                         kinds[k] = (kinds[k] || 0) + 1; });
    out.missing_kinds = kinds;
  } else if (kind === 'xlsx.html') {
    out.count_kind = 'sheets';
    out.count = q('#tabs button');
    // The default look of a cell, from a probe outside Gander's header-row rule
    const sheet = document.getElementById('sheet');
    let base = 'rgba(0, 0, 0, 0)';
    if (sheet) {
      const t = document.createElement('table');
      t.innerHTML = '<tr><td>x</td></tr>';
      sheet.appendChild(t);
      base = getComputedStyle(t.querySelector('td')).backgroundColor;
      t.remove();
    }
    const header = new Set(document.querySelectorAll('#sheet table.vw-top tr:first-child td'));
    const cells = [...document.querySelectorAll('#sheet td, #sheet th')];
    out.xlsx = {
      cells_in_dom: cells.length,
      filled_cells_in_dom: cells.filter(c => !header.has(c)).filter(c => {
        const bg = getComputedStyle(c).backgroundColor;
        return bg !== base && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'rgb(255, 255, 255)';
      }).length,
      parts: window.vwFindParts ? window.vwFindParts.count() : null,
    };
    // Every sheet's text and inline fills, from the tables vwFindParts builds for Find
    // (what the reader can reach by switching tabs and scrolling)
    if (window.vwFindParts) {
      const n = Math.min(window.vwFindParts.count(), 400);
      const texts = []; let inline = 0, total = 0, chars = 0;
      for (let i = 0; i < n && chars < 2000000; i++) {
        const t = window.vwFindParts.root(i);
        total += t.querySelectorAll('td, th').length;
        inline += t.querySelectorAll('td[style*="background"], td[bgcolor], th[style*="background"]').length;
        const rows = [...t.rows].map(r => [...r.cells].map(c => c.textContent).join('\t'));
        const s = rows.join('\n'); texts.push(s); chars += s.length;
      }
      out.xlsx.cells_all_sheets = total;
      out.xlsx.inline_bg_cells_all_sheets = inline;
      out.xlsx.parts_read = n;
      out.all_text = texts.join('\n');
    }
  }
  out.text = document.body.innerText;
  return out;
}
"""

# PPTXjs inserts pictures as <img>; a picture in EMF/WMF/TIFF is marked missing only when
# its load fails, which comes after the slides are in. So wait for every image to settle.
IMAGES_SETTLED = (
    "() => [...document.images].every(i => i.complete)"
)


def settle(page, html):
    """Waits for the late work a page does after its signal, briefly."""
    if html == "pptx.html":
        try:
            page.wait_for_function(IMAGES_SETTLED, timeout=5000, polling=100)
        except Exception:
            pass
    # docx-preview's tab pass runs 500 ms after the document is up; and onerror may fire late
    page.wait_for_timeout(700)


def measure(page, html):
    return page.evaluate(MEASURE, html)


def settled_stamp(page):
    """(ms since navigation start, 'done'|'error') as the page itself stamped it, or (None, None)."""
    return tuple(page.evaluate("() => [window.__vwSettledAt ?? null, window.__vwSettledHow ?? null]"))


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False)
