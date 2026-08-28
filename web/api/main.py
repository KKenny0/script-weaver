"""Compatibility entry point for script-weaverd."""

from script_weaver.daemon.server import app, main  # noqa: F401

if __name__ == "__main__":
    main()
