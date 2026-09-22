"""Run with an app_agent WORKER_DATABASE_URL."""

from workers.runtime import main

if __name__ == "__main__":
    main("relay")
