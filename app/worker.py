from app.services.generation_jobs import recover_interrupted_jobs, run_worker_once


if __name__ == "__main__":
    import os
    import time
    from concurrent.futures import ThreadPoolExecutor

    from app.services.generation_jobs import claim_next_job, run_job

    recover_interrupted_jobs()
    # Parallelize independent works only. claim_next_job keeps a per-work lock,
    # so planning, outlining and writing for one novel remain sequential.
    concurrency = max(1, min(4, int(os.getenv("NOVEL_WORKER_CONCURRENCY", "2"))))
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="novel-worker") as executor:
        running = set()
        while True:
            running = {future for future in running if not future.done()}
            while len(running) < concurrency:
                job = claim_next_job()
                if not job:
                    break
                running.add(executor.submit(run_job, job))
            time.sleep(0.2 if running else 1)
