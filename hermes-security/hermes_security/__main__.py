"""Run the standalone security CLI without importing Hermes."""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
