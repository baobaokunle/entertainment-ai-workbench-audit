"""Run via -I -B -S. All runtime side effects are restricted to B."""
import sys
if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
    raise SystemExit("S1 requires the run.ps1 launcher or Python -I -B -S")
from pathlib import Path

sys.dont_write_bytecode = True
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from live_data_poc.guard import install
install()
from live_data_poc.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
