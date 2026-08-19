from app.services.generation_jobs import run_worker_once


if __name__ == "__main__":
    import time

    while True:
        if not run_worker_once():
            time.sleep(1)
