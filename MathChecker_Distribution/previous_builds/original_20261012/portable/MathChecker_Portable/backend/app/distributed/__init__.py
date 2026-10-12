"""Distributed multi-school layer (accounts, schools/classes, workers, sync,
error reporting, update infrastructure).

Principles (§2):
* The existing correction engine stays the source of truth; this layer only
  adds shared identity/organisation/synchronisation around it.
* The shared backend is the source of truth for distributed metadata; the
  Teacher's computer is just another client.
* Local-first workers: persistent device identity, offline event queue,
  incremental synchronisation by monotonic event version (§18).
"""
