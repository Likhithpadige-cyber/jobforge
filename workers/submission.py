"""Run human-authorized email submission execution and durable retry recovery (M11).

Consumes `submission.requested` and `submission.retry_requested` from
`evt.submissions`. Performs external delivery via configured EmailTransport with
bounded, deterministic retry handling for safe transient failures.
"""

from workers.runtime import main

if __name__ == "__main__":
    main("submission")
