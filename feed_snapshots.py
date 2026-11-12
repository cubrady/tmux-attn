"""Bounded background refreshes for immutable, immediately readable snapshots.

``cache.get(key, loader)`` registers a source and returns a payload plus metadata.
The loader only runs in a daemon worker; callers never wait for collection. Call
``start()`` once to refresh registered sources even without browser requests, and
``stop()`` when the server exits. A loader must enforce its own I/O deadlines:
blocked loaders retain their slots, so they cannot create unlimited threads.
"""
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import threading
import time


@dataclass
class _Entry:
    loader: object
    payload: object = None
    revision: int = 0
    generated_at: float | None = None
    observed: float | None = None
    next_refresh: float = 0
    last_error: str | None = None
    generation: int = 0
    invalidated: bool = False


class SnapshotCache:
    """Single-flight collection per key, with no pending-job queue.

    The result shape is ``{'payload': value_or_none, 'snapshot': metadata}``.
    Successful values and returned values are copied independently. ``age_s`` is
    measured with a monotonic clock; ``generated_at`` is a wall-clock timestamp.
    Payloads older than max_age are withheld, while their metadata remains useful
    for diagnosis. ``stop`` is terminal and never joins a blocked loader.
    """

    def __init__(self, *, ttl=2.0, retry_delay=5.0, max_age=3600.0,
                 max_workers=4, capacity=256, clock=time.monotonic,
                 wall_clock=time.time, scheduler_interval=.1):
        if (ttl <= 0 or retry_delay <= 0 or max_age < ttl or max_workers < 1
                or capacity < 1 or scheduler_interval <= 0):
            raise ValueError('Invalid snapshot cache limits')
        self.ttl, self.retry_delay, self.max_age = ttl, retry_delay, max_age
        self.max_workers, self.capacity = max_workers, capacity
        self.clock, self.wall_clock = clock, wall_clock
        self.scheduler_interval = scheduler_interval
        self._entries = OrderedDict()
        self._active = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._scheduler = None
        self._failures = 0

    def _register_locked(self, key, loader):
        entry = self._entries.get(key)
        if entry is not None:
            if loader is not None:
                entry.loader = loader
            self._entries.move_to_end(key)
            return entry
        if loader is None or self._stop.is_set():
            return None
        if len(self._entries) >= self.capacity:
            oldest = next((item for item in self._entries if item not in self._active), None)
            if oldest is None:
                return None
            del self._entries[oldest]
        entry = _Entry(loader=loader)
        self._entries[key] = entry
        return entry

    def register(self, key, loader):
        """Register or update a source; return False when capacity is exhausted."""
        if not callable(loader):
            raise TypeError('Snapshot loader must be callable')
        with self._lock:
            return self._register_locked(key, loader) is not None

    def _schedule_locked(self, key, entry, now):
        if (self._stop.is_set() or key in self._active
                or len(self._active) >= self.max_workers or now < entry.next_refresh):
            return
        generation, loader = entry.generation, entry.loader
        self._active[key] = entry
        try:
            threading.Thread(target=self._refresh, args=(key, entry, generation, loader),
                             name='feed-snapshot', daemon=True).start()
        except RuntimeError:
            self._active.pop(key, None)
            entry.last_error = 'thread_start_failed'
            entry.next_refresh = now + self.retry_delay
            self._failures += 1

    def get(self, key, loader=None):
        """Return immediately, optionally registering a loader and refreshing it."""
        if loader is not None and not callable(loader):
            raise TypeError('Snapshot loader must be callable')
        now = self.clock()
        with self._lock:
            entry = self._register_locked(key, loader)
            if entry is None:
                error = 'capacity_exhausted' if loader is not None and not self._stop.is_set() else None
                return {'payload': None, 'snapshot': {
                    'revision': 0, 'generated_at': None, 'age_s': None,
                    'state': 'error' if error else 'cold', 'stale': False,
                    'refreshing': False, 'last_error': error,
                }}
            self._schedule_locked(key, entry, now)
            age = max(0.0, now - entry.observed) if entry.observed is not None else None
            visible = entry.observed is not None and age <= self.max_age
            refreshing = key in self._active
            stale = entry.invalidated or entry.last_error is not None or (age is not None and age >= self.ttl)
            if not visible:
                state = 'error' if entry.last_error else ('stale' if age is not None else 'cold')
            elif refreshing:
                state = 'refreshing'
            else:
                state = 'stale' if stale else 'ready'
            return {'payload': deepcopy(entry.payload) if visible else None, 'snapshot': {
                'revision': entry.revision, 'generated_at': entry.generated_at,
                'age_s': round(age, 3) if age is not None else None,
                'state': state, 'stale': stale, 'refreshing': refreshing,
                'last_error': entry.last_error,
            }}

    def _refresh(self, key, entry, generation, loader):
        try:
            # Copy before publication, outside the cache lock. Neither a loader nor
            # its caller can mutate the subsequently published snapshot.
            payload = deepcopy(loader())
            now, generated = self.clock(), self.wall_clock()
            with self._lock:
                if self._entries.get(key) is entry:
                    entry.payload = payload
                    entry.observed, entry.generated_at = now, generated
                    entry.revision += 1
                    entry.last_error = None
                    entry.invalidated = entry.generation != generation
                    entry.next_refresh = now if entry.invalidated else now + self.ttl
        except Exception as exc:
            with self._lock:
                if self._entries.get(key) is entry:
                    entry.last_error = type(exc).__name__
                    entry.next_refresh = self.clock() + self.retry_delay
                    self._failures += 1
        finally:
            with self._lock:
                if self._active.get(key) is entry:
                    self._active.pop(key, None)

    def invalidate(self, key):
        """Mark a value stale after a mutation, coalescing concurrent refreshes."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False
            entry.generation += 1
            entry.invalidated = True
            entry.next_refresh = self.clock()
            self._schedule_locked(key, entry, entry.next_refresh)
            return True

    def retain(self, keys):
        """Unregister removed hosts; their running loaders retain worker slots."""
        wanted = set(keys)
        with self._lock:
            for key in list(self._entries):
                if key not in wanted:
                    del self._entries[key]

    def _run_scheduler(self):
        while not self._stop.is_set():
            now = self.clock()
            with self._lock:
                # Oldest due work first. A hot reader must not starve other hosts.
                due = sorted(self._entries.items(), key=lambda item: item[1].next_refresh)
                for key, entry in due:
                    self._schedule_locked(key, entry, now)
            self._stop.wait(self.scheduler_interval)

    def start(self):
        """Start periodic collection. Idempotent; a stopped cache stays stopped."""
        with self._lock:
            if self._stop.is_set():
                return False
            if self._scheduler is not None:
                return True
            self._scheduler = threading.Thread(target=self._run_scheduler,
                                               name='feed-snapshot-scheduler', daemon=True)
            try:
                self._scheduler.start()
            except RuntimeError:
                self._scheduler = None
                raise
            return True

    def stop(self, timeout=.2):
        """Prevent new work; wait at most timeout seconds for the scheduler."""
        self._stop.set()
        with self._lock:
            scheduler = self._scheduler
        if scheduler is not None and scheduler is not threading.current_thread():
            scheduler.join(max(0.0, timeout))

    def status(self):
        with self._lock:
            return {'registered': len(self._entries), 'active': len(self._active),
                    'max_workers': self.max_workers, 'capacity': self.capacity,
                    'failures': self._failures, 'stopped': self._stop.is_set()}
