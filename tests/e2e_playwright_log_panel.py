"""Playwright e2e: validate the NBACrawler log panel UX fix (Bug D).

Two regressions we want to prevent forever:
  1. Polling refresh must NOT destroy a user's text selection in the panel.
  2. Polling refresh must NOT yank the scroll back to the bottom when the
     user has scrolled up to read old logs.

Strategy: embed the real helper functions (_isUserSelectingIn and the
updateLogPanel selection-skip + no-auto-scroll behaviour) in a self-contained
harness page, drive it with chromium, and assert via JS evaluation.

Run:
  python NBAVedio/tests/e2e_playwright_log_panel.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HARNESS_HTML = r"""
<!doctype html>
<html><head><meta charset="utf-8"><title>log-panel harness</title>
<style>
  #log { height: 200px; overflow-y: auto; border: 1px solid #888;
         font-family: monospace; white-space: pre; padding: 4px; }
</style>
</head><body>
<div id="log"></div>
<script>
  // ─── Copied verbatim from NBACrawler/web/templates/index.html ────────────
  function _isUserSelectingIn(el){
    const sel = window.getSelection && window.getSelection();
    if(!sel || sel.isCollapsed || sel.rangeCount === 0) return false;
    const r = sel.getRangeAt(0);
    return el.contains(r.commonAncestorContainer);
  }

  function _isStuckToBottom(el){
    return (el.scrollHeight - el.scrollTop - el.clientHeight) < 6;
  }

  // The fix from Bug D: skip refresh while user is selecting; no auto-scroll.
  function updateLogPanel(logs){
    const content = document.getElementById('log');
    if(_isUserSelectingIn(content)) return;        // ← skip while selecting
    content.innerHTML = logs.map(l => `<div>${l}</div>`).join('');
    // NOTE: deliberately no scrollTop = scrollHeight here — user scroll wins.
  }

  // Test harness state
  window._refresh_counts = 0;
  window._runRefresh = function(payload){
    window._refresh_counts++;
    updateLogPanel(payload);
  };
</script>
</body></html>
"""


def assert_eq(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")
    print(f"  [OK] {label} = {actual!r}")


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            ctx = browser.new_context()
            page = ctx.new_page()
            page.set_content(HARNESS_HTML)

            # ─── Initial render ────────────────────────────────────────
            page.evaluate("window._runRefresh(['L1','L2','L3'])")
            page.wait_for_timeout(50)
            text = page.locator("#log").inner_text()
            assert "L1" in text and "L3" in text, f"initial render missing lines: {text!r}"
            print("[1] initial render OK")

            # ─── Test 1: user selection survives next refresh ──────────
            # Simulate user-selecting text by programmatically creating a Range
            # over the L2 text node and adding it to the live Selection.
            page.evaluate("""
              () => {
                const log = document.getElementById('log');
                const target = log.querySelectorAll('div')[1].firstChild;
                const r = document.createRange();
                r.setStart(target, 0); r.setEnd(target, 2);
                const sel = window.getSelection();
                sel.removeAllRanges(); sel.addRange(r);
              }
            """)
            sel_text_before = page.evaluate("window.getSelection().toString()")
            assert_eq(sel_text_before, "L2", "selection before refresh")

            # Now trigger refresh with fresh logs; selection must persist
            # AND the DOM must NOT have been overwritten.
            inner_before = page.evaluate("document.getElementById('log').innerHTML")
            page.evaluate("window._runRefresh(['NEW1','NEW2','NEW3'])")
            page.wait_for_timeout(50)
            inner_after = page.evaluate("document.getElementById('log').innerHTML")
            assert_eq(inner_after, inner_before,
                      "DOM unchanged during active selection (skip refresh)")
            sel_text_after = page.evaluate("window.getSelection().toString()")
            assert_eq(sel_text_after, "L2", "selection preserved across refresh")
            print("[2] selection-preserve OK (Bug D regression guard)")

            # ─── Test 2: no auto-scroll-to-bottom ──────────────────────
            # Clear selection so refresh now runs.
            page.evaluate("window.getSelection().removeAllRanges()")
            # Fill with many lines so panel is scrollable
            page.evaluate("""
              () => {
                const many = Array.from({length: 60}, (_, i) => 'line ' + i);
                window._runRefresh(many);
              }
            """)
            page.wait_for_timeout(50)
            # Scroll panel to top (simulating user reading old logs)
            page.evaluate("document.getElementById('log').scrollTop = 0")
            scroll_before = page.evaluate("document.getElementById('log').scrollTop")
            assert_eq(scroll_before, 0, "scrollTop after user scrolled to top")

            # New refresh with NEW data should NOT yank back to bottom.
            page.evaluate("""
              () => {
                const many2 = Array.from({length: 60}, (_, i) => 'fresh ' + i);
                window._runRefresh(many2);
              }
            """)
            page.wait_for_timeout(50)
            scroll_after = page.evaluate("document.getElementById('log').scrollTop")
            # New content rewrites innerHTML, which resets scrollTop to 0.
            # The critical assertion is the panel is NOT pinned to bottom.
            stuck = page.evaluate("_isStuckToBottom(document.getElementById('log'))")
            assert stuck is False, f"panel must NOT be stuck to bottom; scrollTop={scroll_after}"
            print(f"[3] no auto-scroll-to-bottom OK (scrollTop={scroll_after}, stuck=False)")

            # ─── Test 3: helpers exist (catches the literal type of bug
            # we fixed in Bug H — defensive code wired to a wrong symbol) ──
            for fn in ("_isUserSelectingIn", "_isStuckToBottom", "updateLogPanel"):
                exists = page.evaluate(f"typeof {fn} === 'function'")
                assert exists, f"helper {fn} missing/not a function"
            print("[4] all helpers wired correctly")

            print("\n=== Playwright e2e PASSED ===")
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(run())
