"""Run owner-requested M9 question extraction/classification/grounding.

Deterministic and entirely offline, like `workers/render.py`: no model, no
provider budget. Extraction, classification and grounding are pattern and
database matches, never inference.
"""

from workers.runtime import main

if __name__ == "__main__":
    main("application")
