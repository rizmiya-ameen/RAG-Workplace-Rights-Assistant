"""Test-wide settings.

- Unit tests don't load the 740 MB injection classifier: the input guard runs without it
  unless a test passes a scorer (tests/test_classifier.py runs the real model when its files
  are present).
- Tests never send traces to Langfuse, even when backend/.env has real keys (environment
  variables override the .env file); tests/test_observability.py uses a fake client.
"""

import os

os.environ.setdefault("INJECTION_CLASSIFIER", "false")
os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""
