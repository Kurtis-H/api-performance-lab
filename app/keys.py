"""API keys are stored as SHA-256 hashes, never as issued (pentest BP-06).

A fast hash is right here: keys are long random tokens in a real deployment,
not passwords, so there is nothing to brute-force from the hash. The lab's
seed keys are predictable on purpose (see app/seed.py), which is accepted as
test data.
"""

from __future__ import annotations

import hashlib


def hash_key(key: str) -> str:
    """Hex SHA-256. Postgres computes the same value as
    encode(sha256(convert_to(key, 'UTF8')), 'hex'), used in app/seed.py and app/db.py."""
    return hashlib.sha256(key.encode()).hexdigest()
