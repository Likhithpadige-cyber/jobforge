"""Produce deduplicated monitoring ticks using an app_monitor connection."""

from workers.runtime import main

if __name__ == "__main__":
    main("scheduler")
