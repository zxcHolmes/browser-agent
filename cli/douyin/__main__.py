"""Entry point: `python3 -m cli.douyin …` from the repository root, or
`python3 path/to/cli.douyin …` from anywhere."""
import sys
from pathlib import Path

if __package__:
    from .main import main
else:
    # Run as a directory: make the repository root importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from cli.douyin.main import main

raise SystemExit(main())
