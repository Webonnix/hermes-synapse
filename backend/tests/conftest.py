"""Unit tests use explicit local auth state; production never falls back to it.

The Redis-backed state is exercised separately against an ephemeral Valkey.
No fixture connects to the installation's credentials, OTP or sessions.
"""
from backend import auth

auth._test_memory = True

import os
os.environ["HERMES_AUTH_SECURE_COOKIE"] = "false"
