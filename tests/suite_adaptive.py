"""Gradual admission between CTest waves; test timeouts exclude admission wait."""

import math
import xml.etree.ElementTree as ET
from pathlib import Path


def props(test):
    return {p["name"]: p["value"] for p in test.get("properties", [])}


def components(tests):
    """Keep dependencies and every user of a fixture in one CTest invocation."""
    names = {t["name"] for t in tests}
    parent = {n: n for n in names}

    def find(n):
        while n != parent[n]:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    def join(a, b):
        if b not in names:
            raise ValueError("Dependency missing from selected inventory: " + b)
        parent[find(a)] = find(b)

    fixtures = {}
    for t in tests:
        p = props(t)
        for other in p.get("DEPENDS", []):
            join(t["name"], other)
        for key in ("FIXTURES_SETUP", "FIXTURES_REQUIRED", "FIXTURES_CLEANUP"):
            for fixture in p.get(key, []):
                if fixture in fixtures:
                    join(t["name"], fixtures[fixture])
                fixtures[fixture] = t["name"]
    groups = {}
    for t in tests:
        groups.setdefault(find(t["name"]), []).append(t)
    return list(groups.values())


def take_wave(pending, slots):
    if slots < 1:
        return []
    wave, weight = [], 0
    while pending:
        group = pending[0]
        cost = sum(max(1, int(props(t).get("PROCESSORS", 1))) for t in group)
        if wave and weight + cost > slots:
            break
        wave.extend(pending.pop(0))
        weight += cost
        if weight >= slots:
            break
    return wave


def cpu_ticks():
    # Host-wide contention matters even when this batch has a smaller affinity set.
    values = [
        int(n) for n in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]
    ]
    return sum(values), values[3] + values[4]


def busy_fraction(before, after):
    total = after[0] - before[0]
    return max(0.0, min(1.0, 1 - (after[1] - before[1]) / total)) if total > 0 else 1.0


class Admission:
    def __init__(
        self,
        ceiling,
        initial,
        interval,
        engines,
        reserve,
        engine_cap,
        slot_bytes,
        cpu_target,
    ):
        self.ceiling = ceiling
        self.slots = min(initial, ceiling)
        self.interval = interval
        self.engines = engines
        self.reserve = reserve
        self.engine_cap = engine_cap
        self.slot_bytes = slot_bytes
        self.cpu_target = cpu_target
        self.healthy_since = None
        self.previous = None

    def update(self, seconds, available, busy, rss, observed_slot_bytes=0):
        self.slot_bytes = max(self.slot_bytes, observed_slot_bytes)
        drop = 0
        if self.previous:
            dt = seconds - self.previous[0]
            if dt > 0:
                drop = max(0, (self.previous[1] - available) / dt)
        self.previous = seconds, available
        projected = available - drop * self.interval
        memory_limit = math.floor(self.engine_cap * 0.65 / self.slot_bytes)
        limit = max(0, min(self.ceiling, memory_limit))
        reason = "HOLD"
        # Stop admission early, before the coordinator's hard high-water abort.
        if projected < self.reserve + self.engines * self.slot_bytes or any(
            r > self.engine_cap * 0.70 for r in rss
        ):
            self.slots = 0
            self.healthy_since = None
            reason = "PAUSE_MEMORY"
        elif busy > min(0.90, self.cpu_target + 0.15):
            self.slots = max(1, self.slots // 2) if limit else 0
            self.slots = min(self.slots, limit)
            self.healthy_since = None
            reason = "REDUCE_CPU"
        elif self.slots > limit:
            self.slots = limit
            self.healthy_since = None
            reason = "REDUCE_OBSERVED_MEMORY"
        else:
            healthy = (
                busy < self.cpu_target
                and projected > self.reserve + 2 * self.engines * self.slot_bytes
                and all(r < self.engine_cap * 0.60 for r in rss)
            )
            if healthy:
                if self.healthy_since is None:
                    self.healthy_since = seconds
                if seconds - self.healthy_since >= self.interval and self.slots < limit:
                    self.slots += 1
                    self.healthy_since = seconds
                    reason = "INCREASE_ONE"
            else:
                self.healthy_since = None
        return {
            "slots_per_engine": self.slots,
            "ceiling_per_engine": self.ceiling,
            "memory_safe_slots_per_engine": limit,
            "reason": reason,
            "cpu_busy_fraction": busy,
            "projected_available_bytes": max(0, int(projected)),
            "slot_estimate_bytes": self.slot_bytes,
        }


def merge_junit(paths, destination, selected):
    names = {t["name"] for t in selected}
    suite = ET.Element("testsuite", name="suite-waves")
    seen = set()
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            n = case.attrib["name"]
            if n not in names or n in seen:
                raise ValueError("Unexpected or repeated wave result: " + n)
            seen.add(n)
            suite.append(case)
    suite.set("tests", str(len(seen)))
    suite.set(
        "failures",
        str(
            sum(
                c.find("failure") is not None or c.find("error") is not None
                for c in suite
            )
        ),
    )
    suite.set("skipped", str(sum(c.find("skipped") is not None for c in suite)))
    suite.set("time", str(sum(float(c.attrib.get("time", 0)) for c in suite)))
    ET.ElementTree(suite).write(destination, encoding="utf-8", xml_declaration=True)


def duration_class(status, seconds):
    if status == "NOT_RUN":
        return {"speed": "UNMEASURED", "lower_bound": False}
    speed = (
        "FAST"
        if seconds <= 30
        else "MEDIUM"
        if seconds <= 120
        else "SLOW"
        if seconds <= 600
        else "VERY_SLOW"
    )
    return {
        "speed": speed,
        "lower_bound": status == "TIMEOUT",
        "scope": "Observed CTest wall time, including compile and driver work; not simulation-only time",
    }
