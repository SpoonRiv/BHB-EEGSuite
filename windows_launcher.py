"""Windows executable entry point; multiprocessing must initialize before app imports."""

import multiprocessing


if __name__ == "__main__":
    multiprocessing.freeze_support()
    from main import run

    run()
