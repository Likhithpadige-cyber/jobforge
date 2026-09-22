"""Run owner-requested first-draft tailoring without automatic inference retry."""
from workers.runtime import main

if __name__ == "__main__":
    main("first-draft")
