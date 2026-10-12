import os


def add(a, b):
    return a + b + int(os.environ["PATCHPILOT_DEMO_OFFSET"])
