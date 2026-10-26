"""Run as a module or directly as a package directory."""
import sys
from pathlib import Path

if __package__:
    from .main import main
else:
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    from importlib import import_module
    main=import_module('cli.'+Path(__file__).parent.name+'.main').main

raise SystemExit(main())
