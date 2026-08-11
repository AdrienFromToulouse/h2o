"""Fixtures for the h2o_core suite. No test here touches AWS.

The environment is set before any import that could construct a boto3 client, so
a missing region never turns into a confusing failure three modules deep.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("AWS_REGION", "eu-west-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-1")
os.environ.setdefault("H2O_ENV", "test")

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The checks in scripts/ are part of what this package guarantees -- they run
#: h2o_core's own functions against the real artefacts -- so the suite imports
#: them to assert there is exactly one definition of each.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
