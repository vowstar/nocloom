"""Oracle of the target-side upsize merge: its target descriptor per burst
(plan) and the case list of the merge's transaction case
(tests/verif/cases/axi4_upsize.py). The merge rules are restated here
independently of the generator."""


def plan(case, tbytes, write):
    # Merge rules: target descriptor (mode, start, length, size, burst); mode 0 passes.
    size, burst, a = case['size'], case['burst'], case['address']
    t = tbytes.bit_length() - 1
    step, n = 1 << size, case['length'] + 1
    if burst == 0 or size > t or (burst == 2 and size == t):
        return 0, a, case['length'], size, burst
    span = n * step
    if burst == 2:
        base = a // span * span
        if span <= tbytes:
            # The container is accessed at its own size.
            return 1, base, 0, span.bit_length() - 1, 1
        start = a // tbytes * tbytes
        if write and a % tbytes:
            start = base | ((start + tbytes) & (span - 1))
        return 2, start, span // tbytes - 1, t, 2
    end = a // step * step + span
    words = (end - a // tbytes * tbytes + tbytes - 1) // tbytes
    if words > 1:
        return 1, a, words - 1, t, 1
    cover = next(q for q in range(t + 1) if a >> q == (end - 1) >> q)
    return 1, a, 0, cover, 1


def cases(tbytes):
    t = tbytes.bit_length() - 1
    b = 0x1000
    out = []
    for w in (1, 0):
        d = 'w' if w else 'r'
        out += [
            dict(name=f'{d}-incr', write=w, address=b + 0x40, size=2, length=4, burst=1),
            dict(name=f'{d}-bytes', write=w, address=b + 0x103, size=0, length=5, burst=1),
            dict(name=f'{d}-unaligned', write=w, address=b + 0x44, size=2, length=5, burst=1),
            dict(name=f'{d}-half-start', write=w, address=b + 0x20c, size=3, length=6, burst=1),
            dict(name=f'{d}-wrap-small', write=w, address=b + 0x304, size=2, length=1, burst=2),
            # A container in the upper half of a word: its end is a word boundary.
            dict(name=f'{d}-wrap-small-high', write=w, address=b + 0x300 + tbytes - 4, size=2,
                 length=1, burst=2),
            dict(name=f'{d}-wrap-aligned', write=w, address=b + 0x440, size=2,
                 length=(4 * tbytes // 4) - 1 if tbytes <= 16 else 15, burst=2),
            dict(name=f'{d}-wrap-shift', write=w, address=b + 0x448, size=2,
                 length=(4 * tbytes // 4) - 1 if tbytes <= 16 else 15, burst=2),
            dict(name=f'{d}-fixed', write=w, address=b + 0x500, size=2, length=2, burst=0),
            dict(name=f'{d}-full', write=w, address=b + 0x600, size=t, length=1, burst=1),
            # A full-width INCR from mid-word takes its covering size.
            dict(name=f'{d}-full-mid', write=w, address=b + 0x640 + tbytes // 2, size=t,
                 length=0, burst=1),
            # A 16-byte WRAP container below the target width.
            dict(name=f'{d}-wrap-container', write=w, address=b + 0x688, size=2, length=3,
                 burst=2),
            dict(name=f'{d}-single', write=w, address=b + 0x704, size=2, length=0, burst=1),
            dict(name=f'{d}-single-odd', write=w, address=b + 0x802, size=3, length=0, burst=1),
        ]
    out.append(dict(name='w-back-to-back', write=1, address=b + 0xb00, size=2, length=4, burst=1,
                    then=dict(write=1, address=b + 0xc00, size=t, length=1, burst=1)))
    out.append(dict(name='w-zero-beat', write=1, address=b + 0x900, size=2, length=7, burst=1,
                    zero_beat=2))
    out.append(dict(name='r-error-word', write=0, address=b + 0xa00, size=2, length=7, burst=1,
                    error_word=b + 0xa00 + tbytes))
    # Each case as a modifiable burst and as a Non-modifiable burst of source 0
    # and of source 1: all merge alike.
    paired = []
    for c in out:
        for suffix, cache, source in (('', 2, 0), ('-nm', 0, 0), ('-all', 0, 1)):
            v = dict(c, name=c['name'] + suffix, cache=cache, source=source)
            if 'then' in c:
                v['then'] = dict(c['then'], cache=cache, source=source)
            paired.append(v)
    # Non-modifiable and modifiable bursts back to back, in both orders.
    paired.append(dict(name='w-pass-then-merge', write=1, address=b + 0xd04, size=1, length=4,
                       burst=1, cache=0, source=0,
                       then=dict(write=1, address=b + 0xe02, size=0, length=5, burst=1, cache=2,
                                 source=0)))
    paired.append(dict(name='w-merge-then-pass', write=1, address=b + 0xd44, size=1, length=4,
                       burst=1, cache=0, source=1,
                       then=dict(write=1, address=b + 0xe42, size=0, length=5, burst=1, cache=1,
                                 source=0)))
    return paired
