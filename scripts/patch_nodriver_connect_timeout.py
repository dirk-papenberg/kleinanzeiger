#!/usr/bin/env python3
"""Post-install patch for nodriver's Chrome DevTools connect-retry loop.

kleinanzeigen-bot's ``nodriver/core/browser.py`` ``Browser.start()`` only
polls Chrome's DevTools ``/json/version`` endpoint 5 times (no per-attempt
asyncio timeout - just whatever ``HTTPApi``'s underlying ``urllib`` call
happens to use, 0.5s between attempts, 0.25s initial delay) before raising:

    "Failed to start browser... Failed to connect to browser... root..."

On a loaded host, Chrome's cold start can legitimately take longer than that,
even though the browser works fine a moment later. This isn't configurable
via kleinanzeigen-bot's own config.yaml (its ``timeouts:`` block only covers
DOM/version-probe timeouts, not this internal nodriver connect loop).

This patch makes the loop configurable via environment variables:
    NODRIVER_CONNECT_RETRIES        (default 5)
    NODRIVER_CONNECT_TIMEOUT        (default 2   seconds, per attempt)
    NODRIVER_CONNECT_RETRY_DELAY    (default 0.5 seconds, between attempts)
    NODRIVER_CONNECT_INITIAL_DELAY  (default 0.25 seconds, before first attempt)

Intended to run alongside kleinanzeigen-bot's own ``scripts/fix_nodriver.py``
post_install hook, against a source build (``pdm install``). Idempotent, same
marker-based approach as that script.
"""

from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path

_PATCH_MARKER = "KLEINANZEIGEN_AGENT_NODRIVER_CONNECT_TIMEOUT_PATCH_V1"

_ORIG_BLOCK = """\
        await asyncio.sleep(0.25)
        for _ in range(5):
            try:
                self.info = ContraDict(await self._http.get("version"), silent=True)

            except (Exception,):
                if _ == 4:
                    logger.debug("could not start", exc_info=True)
                await asyncio.sleep(0.5)
            else:
                break
"""

_MARKER_COMMENT_LINE = (
    f"        # {_PATCH_MARKER}: connect retry/timeout tunable via env vars."
)

_NEW_BLOCK = f"""\
{_MARKER_COMMENT_LINE}
        import os as _os
        _connect_retries = int(_os.environ.get("NODRIVER_CONNECT_RETRIES", "5"))
        _connect_timeout = float(_os.environ.get("NODRIVER_CONNECT_TIMEOUT", "2"))
        _connect_retry_delay = float(_os.environ.get("NODRIVER_CONNECT_RETRY_DELAY", "0.5"))
        _connect_initial_delay = float(_os.environ.get("NODRIVER_CONNECT_INITIAL_DELAY", "0.25"))
        await asyncio.sleep(_connect_initial_delay)
        for _ in range(_connect_retries):
            try:
                self.info = ContraDict(
                    await asyncio.wait_for(self._http.get("version"), _connect_timeout),
                    silent=True,
                )
            except (Exception,):
                if _ == _connect_retries - 1:
                    logger.debug("could not start", exc_info=True)
                await asyncio.sleep(_connect_retry_delay)
            else:
                break
"""


def _locate_file(relative: str) -> Path | None:
    """Locate an installed nodriver source file via importlib.metadata."""
    try:
        dist = importlib.metadata.distribution("nodriver")
    except importlib.metadata.PackageNotFoundError:
        return None
    try:
        return Path(dist.locate_file(relative))  # type: ignore[arg-type]
    except AttributeError:
        site_packages = Path(dist._path).parent  # type: ignore[attr-defined]  # noqa: SLF001
        return site_packages / relative


def _patch_connect_timeout(path: Path) -> str:
    try:
        text = path.read_text("utf-8")
    except OSError as exc:
        print(
            f"patch_nodriver_connect_timeout: cannot read {path}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    if _PATCH_MARKER in text:
        return "already-ok"

    if _ORIG_BLOCK not in text:
        print(
            "patch_nodriver_connect_timeout: expected connect-retry block not found "
            "(nodriver version mismatch?)",
            file=sys.stderr,
        )
        sys.exit(1)

    text = text.replace(_ORIG_BLOCK, _NEW_BLOCK)
    path.write_text(text, "utf-8")
    return "fixed"


def main() -> int:
    path = _locate_file("nodriver/core/browser.py")
    if path is None:
        print(
            "patch_nodriver_connect_timeout: nodriver not installed, cannot patch",
            file=sys.stderr,
        )
        return 1
    if not path.is_file():
        print(
            f"patch_nodriver_connect_timeout: {path} not found, cannot patch",
            file=sys.stderr,
        )
        return 1
    print(f"patch_nodriver_connect_timeout: {path} -> {_patch_connect_timeout(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
