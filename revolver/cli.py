from __future__ import annotations

import runpy


def main() -> None:
    """Run the built-in Revolver self-test."""
    runpy.run_module("revolver", run_name="__main__")
