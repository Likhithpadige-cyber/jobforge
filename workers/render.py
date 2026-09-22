"""Render approved resumes into durable artifacts (M8).

Opt-in, like every other worker. Deterministic and entirely offline: no
model, no provider budget, and no network beyond the object store.
"""
from workers.runtime import main

if __name__ == "__main__":
    main("render")
