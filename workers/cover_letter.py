"""Draft and verify a grounded cover letter, on request (M10 Phase 1).

LLM-calling and cost-shaped, like `workers/first_draft.py`: no automatic
retry, its own audit-engine connection, explicit pricing.
"""
from workers.runtime import main

if __name__ == "__main__":
    main("cover-letter")
