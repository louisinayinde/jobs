"""Permet `python -m src` comme alias de la CLI."""

from src.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
