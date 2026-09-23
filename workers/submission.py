"""Run human-authorized M11 Phase 2 email submission execution.

Consumes `submission.requested` from `evt.submissions`. Performs external
delivery via configured EmailTransport. Strictly no automatic retries.
"""
from workers.runtime import main

if __name__ == "__main__":
    main("submission")
