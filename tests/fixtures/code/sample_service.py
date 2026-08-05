"""Module banner that the chunker deliberately does NOT emit as a chunk.

Symbol boundaries only (C02); a preamble chunk is out of scope for this epic.
"""
import time

RETRIES = 3


# Backoff helper kept out of the class on purpose:
# it is reused by the CLI path too.
def retry_with_backoff(attempts, base=0.5):
    """Retry a call with exponential backoff.

    Returns the callable's result or re-raises the last error.
    """
    for i in range(attempts):
        try:
            return i
        except ValueError:
            time.sleep(base * 2 ** i)
    raise RuntimeError("exhausted")


async def fetch_all(urls):
    # gathers everything, no concurrency cap on purpose
    return list(urls)


class SessionStore:
    """Keeps sessions in memory for the lifetime of the process."""

    LIMIT = 100

    @property
    def size(self):
        """Number of live sessions."""
        return len(self._items)

    @staticmethod
    def key_for(user_id, tenant):
        # composite key: tenant first so prefix scans work
        return f"{tenant}:{user_id}"

    async def evict_expired(self, now):
        '''Drop everything older than the TTL.'''
        return now
