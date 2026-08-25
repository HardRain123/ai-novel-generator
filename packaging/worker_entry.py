if __name__ == "__main__":
    import runpy

    from app.db import init_db

    # The web process usually initializes the database during FastAPI startup.
    # A packaged worker can start first, so it must perform the same idempotent
    # initialization before looking for queued generation jobs.
    init_db()
    runpy.run_module("app.worker", run_name="__main__")
