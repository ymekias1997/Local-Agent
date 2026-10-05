"""Opt-in real Chromium pipe smoke check; opens only a fresh about:blank profile.

Run directly, separately from unittest discovery, because this starts Chromium.
No desktop inputs, downloads, or external navigation are performed.
"""
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_llm_tools.browser import BrowserTools

with tempfile.TemporaryDirectory() as directory:
    browser = BrowserTools(directory, lambda *_: False)
    try:
        snapshot = browser.browser_snapshot()
        assert snapshot['url'] == 'about:blank', snapshot
        image = browser.browser_screenshot()
        assert Path(image['path']).read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
        print('Chromium private pipe, blank-page snapshot, and PNG capture passed')
    finally:
        browser.close()
