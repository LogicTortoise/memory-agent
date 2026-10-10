"""Packaged React audit UI.

The Vite build emits hashed assets under ``web/dist``. ``scripts/build_ui.sh``
runs the Vite build and mirrors the output into ``src/memory_agent/ui/dist``
so the artefacts ship as package data and ``memory ui`` can serve the real
SPA from a clean wheel install with no developer checkout.

The bundled directory is *only* read at runtime — the build is reproducible
and the contents are deterministic Vite output (index.html + ``assets/`` with
content-hashed JS / CSS). It must never be edited by hand.
"""

from __future__ import annotations

__all__ = ["DIST_DIRNAME"]
DIST_DIRNAME = "dist"