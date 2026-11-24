"""Test-wide settings. Unit tests don't load the 740 MB injection classifier: the input guard
runs without it unless a test passes a scorer (tests/test_classifier.py runs the real model
when its files are present)."""

import os

os.environ.setdefault("INJECTION_CLASSIFIER", "false")
