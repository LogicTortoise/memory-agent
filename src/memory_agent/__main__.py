"""Module entry point so ``python -m memory_agent`` works."""

from memory_agent.interfaces.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())