"""netmon - a small real-time network monitor for a Raspberry Pi with a 5" TFT.

Two halves that share one process:

  * a collector (asyncio, background thread) that runs the probes and writes
    both an in-RAM history and a SQLite rollup, and
  * a Tkinter dashboard (main thread) that reads snapshots of that history.

Keeping them in one process means one Python interpreter on a 1 GB Pi and no
IPC; run with ``--headless`` on a Pi with no screen.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
