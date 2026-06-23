"""Headroom proxy support modules retained for the compression library.

The full proxy server runtime was removed in the compression-only amputation.
This package now exposes only the pieces the compression core depends on:

- :mod:`headroom.proxy.auth_mode` — ``AuthMode`` used by compression policy.
- :mod:`headroom.proxy.helpers` — SSE / wire helpers used by CCR.
- :mod:`headroom.proxy.interceptors` — tool-result interceptor transforms.
"""

__all__: list[str] = []
