#!/usr/bin/env python3
from __future__ import annotations

import sys


if "--probe" in sys.argv:
    print("probe:tools-disabled")
elif "--worker" in sys.argv:
    print("worker:ok")
else:
    print("fake-pi:ready")
