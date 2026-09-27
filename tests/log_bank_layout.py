"""The register table of an error log bank, layout 2: the one place the tests take the layout from.

The table is written out here and never read from the generator, so a test that compares the
generated map, the C header or the simulated registers with it fails when the layout moves.
"""

LAYOUT = 2
BANK_BYTES = 64

# name, byte offset, lsb, width, access. A width is the field's place in the layout: a bank's
# actual field can be narrower, and its high bits then read zero. The fields of the first word and
# the revision are fixed at generation: the map gives each a value.
FIELDS = (
    ("capability_audit_flags", 0x00, 0, 1, "ro"),
    ("capability_user_flags", 0x00, 1, 1, "ro"),
    ("capability_target_user_flags", 0x00, 2, 1, "ro"),
    ("capability_next_logger", 0x00, 3, 1, "ro"),
    ("capability_given_revision", 0x00, 4, 1, "ro"),
    ("logger_index", 0x00, 8, 5, "ro"),
    ("log_index", 0x00, 16, 6, "ro"),
    ("layout", 0x00, 24, 8, "ro"),
    ("record_held", 0x04, 0, 1, "rw1c"),
    ("record_overflow", 0x04, 1, 1, "rw1c"),
    ("record_class", 0x04, 4, 4, "ro"),
    ("alarm_enable", 0x04, 8, 1, "rw"),
    ("wait_when_full", 0x04, 9, 1, "rw"),
    ("record_write", 0x04, 12, 1, "ro"),
    ("record_wrap", 0x04, 13, 1, "ro"),
    ("record_exclusive", 0x04, 14, 1, "ro"),
    ("record_address_partial", 0x04, 15, 1, "ro"),
    ("record_sequence", 0x04, 16, 16, "ro"),
    ("record_address_low", 0x08, 0, 32, "ro"),
    ("record_address_high", 0x0c, 0, 16, "ro"),
    ("record_bytes", 0x0c, 16, 16, "ro"),
    ("record_source_path", 0x10, 0, 13, "ro"),
    ("record_target_path", 0x10, 16, 13, "ro"),
    ("record_region_index", 0x14, 0, 32, "ro"),
    ("record_audit_flags", 0x18, 0, 32, "ro"),
    ("revision", 0x1c, 0, 32, "ro"),
) + tuple((f"record_user_flags{k}", 0x20 + 4 * k, 0, 32, "ro") for k in range(8))

CAPABILITIES = ("audit_flags", "user_flags", "target_user_flags", "next_logger", "given_revision")
CLASS_DECODE, CLASS_TARGET = 1, 2
ID, STATE, REVISION = 0x00, 0x04, 0x1c
HELD, OVERFLOW = "record_held", "record_overflow"


def field(name):
    return next(dict(name=n, offset=o, lsb=l, width=w, access=a)
                for n, o, l, w, a in FIELDS if n == name)


def mask(name):
    f = field(name)
    return ((1 << f["width"]) - 1) << f["lsb"]


def put(name, value):
    f = field(name)
    assert 0 <= value < 1 << f["width"], (name, value)
    return value << f["lsb"]


def controls():
    return [dict(name=n, offset=o, lsb=l, width=w) for n, o, l, w, a in FIELDS if a == "rw"]


def identification(log, logger, parts):
    """The ID word of a bank: its place and the names of the optional parts it has."""
    assert set(parts) <= set(CAPABILITIES), parts
    word = put("layout", LAYOUT) | put("log_index", log) | put("logger_index", logger)
    for part in parts:
        word |= put("capability_" + part, 1)
    return word


def generator_revision(version):
    """The revision word of a definition without a revision: one byte per part of the version."""
    major, minor, patch = (int(part) for part in version.split("-")[0].split(".")[:3])
    return major << 16 | minor << 8 | patch
