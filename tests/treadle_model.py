"""Cycle-accurate policy model of one output arbiter.

The model states each policy's rule directly (an ordered list for FIFO, a
cursor index for round robin, a counter and a priority tree for rotating
priority, a rank table for fixed priority); it does not mirror the generated
netlist. One call to step() covers one clock
edge: it returns the outputs visible before the edge and then commits the
state the edge stores.
"""


def tree_order(first, size, counter):
    """Rotating priority order of candidates [first, first + size).

    The lower half holds the size // 2 smallest indexes; counter bit
    ceil(log2 size) - 1 puts the upper half first, and both halves order
    themselves from the lower counter bits.
    """
    if size == 1:
        return [first]
    low = size // 2
    lower = tree_order(first, low, counter)
    upper = tree_order(first + low, size - low, counter)
    return upper + lower if counter >> ((size - 1).bit_length() - 1) & 1 else lower + upper


class Arbiter:
    POLICIES = ("round_robin", "rotating_priority", "fixed_priority", "fifo")

    def __init__(self, policy, count, packet=True, rank=None):
        assert (policy in self.POLICIES or policy == "level_round_robin") and count >= 2
        self.policy, self.count, self.packet = policy, count, packet
        self.rank = list(rank) if rank is not None else None
        if policy == "fixed_priority":
            assert sorted(self.rank) == list(range(count))
        self.reset()

    def reset(self):
        self.held, self.owner, self.cursor = False, 0, 0
        # Level round robin: the last winner of each level, all candidate 0 after reset.
        self.lasts = {}
        # FIFO: candidates front to back, highest index first after reset.
        self.order = list(range(self.count))[::-1]
        # Arrival cycle of each waiting request (FIFO).
        self.since = [None] * self.count
        self.now = 0

    def choose(self, valid):
        live = [i for i in range(self.count) if valid[i]]
        if not live:
            return None
        if self.policy == "round_robin":
            return min(live, key=lambda i: (i - self.cursor) % self.count)
        if self.policy == "level_round_robin":
            start = (self.lasts.get(self.bucket(), 0) + 1) % self.count
            return min(live, key=lambda i: (i - start) % self.count)
        if self.policy == "rotating_priority":
            return next(i for i in tree_order(0, self.count, self.cursor) if valid[i])
        if self.policy == "fixed_priority":
            return min(live, key=lambda i: self.rank[i])
        return next(i for i in self.order if valid[i])

    def bucket(self):
        """The level whose last winner a level round robin searches after: the highest level of
        a valid candidate (raw valid; zero without levels)."""
        levels = getattr(self, "levels", None)
        return max((levels[i] for i in range(self.count) if self.raw[i]), default=0) if levels else 0

    def step(self, valid, last, ready):
        self.raw = list(valid)
        choice = self.owner if self.held else self.choose(valid)
        sink_valid = choice is not None and bool(valid[choice])
        transfer = sink_valid and bool(ready)
        tail = bool(last[choice]) if (self.packet and choice is not None) else True
        release = transfer and tail
        engage = sink_valid and (not ready or not tail)
        out = dict(winner=choice, held=self.held, sink_valid=sink_valid,
                   transfer=transfer, release=release)
        if self.policy == "level_round_robin" and sink_valid and not self.held:
            self.lasts[self.bucket()] = choice
        if engage and not self.held:
            self.owner = choice
        self.held = False if release else (True if engage else self.held)
        if self.policy == "round_robin" and release:
            self.cursor = (choice + 1) % self.count
        elif self.policy == "rotating_priority" and release:
            # The counter wraps at the next power of two, not at count.
            self.cursor = (self.cursor + 1) % (1 << (self.count - 1).bit_length())
        elif self.policy == "fifo":
            released = choice if release else None
            # A request is older than every later one; equal arrival cycles
            # keep the previous relative order; idle candidates follow every
            # request; the released winner goes last.
            for i in range(self.count):
                if not valid[i] or i == released:
                    self.since[i] = None
                elif self.since[i] is None:
                    self.since[i] = self.now
            position = {c: k for k, c in enumerate(self.order)}
            self.order.sort(key=lambda i: (
                3 if i == released else 2 if self.since[i] is None else 0,
                self.since[i] if self.since[i] is not None else 0, position[i]))
        self.now += 1
        return out
