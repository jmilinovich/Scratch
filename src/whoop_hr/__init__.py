"""whoop-hr: per-sample overnight heart rate from WHOOP.

Prefers the official consented API (and its undocumented sleep stream); falls
back to the internal web-app API only when explicitly enabled.
"""

from .config import Config
from .models import HRSample, HRSeries, HRSource, Recovery, SleepSummary
from .provider import HRResult, WhoopHR

__all__ = [
    "Config",
    "WhoopHR",
    "HRResult",
    "HRSeries",
    "HRSample",
    "HRSource",
    "Recovery",
    "SleepSummary",
]
