"""Direct tests for the shared 'vers' parsing in resfork: Get Info's
messages, the System's raw version number, and the malformed records
neither may crash on."""

from builders import pstr, vers

from macrt.resfork import (
    Resource, build_fork, resCompressed, vers_body, vers_message, vers_messages,
)
from macrt.toolbox.system import file_version
from macrt.macfile import MacFile


def _fork(*resources: Resource) -> bytes:
    return build_fork(list(resources))


# -- vers_message ------------------------------------------------------------

def test_long_message_wins():
    assert vers_message(vers("1.0", "1.0, a test app")) == "1.0, a test app"


def test_short_string_is_the_fallback():
    # a record with an empty long message gives the short version string
    body = bytes([1, 0, 0x80, 0, 0, 0]) + pstr("1.0") + pstr("")
    assert vers_message(body) == "1.0"


def test_record_too_short_says_nothing():
    assert vers_message(b"") is None
    assert vers_message(b"\x01\x00") is None


def test_truncated_long_string_falls_back_or_none():
    body = bytes([1, 0, 0x80, 0, 0, 0]) + pstr("1.0")   # claims a long string that isn't there
    # the short string is intact; the long one runs off the end, so the short one is all there is
    assert vers_message(body) == "1.0"
    # a short string whose own length runs past the body: nothing usable at all
    bad = bytes([1, 0, 0x80, 0, 0, 0, 200]) + b"1."
    assert vers_message(bad) is None


# -- vers_messages -----------------------------------------------------------

def test_both_ids_come_back():
    fork = _fork(Resource(b"vers", 1, vers("1.0", "the version field")),
                 Resource(b"vers", 2, vers("1.0", "beneath the name")))
    assert vers_messages(fork) == ("the version field", "beneath the name")


def test_missing_ids_are_none():
    fork = _fork(Resource(b"vers", 1, vers("1.0", "only one")))
    assert vers_messages(fork) == ("only one", None)
    assert vers_messages(_fork(Resource(b"STR ", 128, b"x"))) == (None, None)


def test_a_fork_that_is_not_one_says_nothing():
    assert vers_messages(b"not a fork") == (None, None)


# -- vers_body / file_version ------------------------------------------------

def test_vers_body_gives_the_raw_record():
    body = vers("7.5.5", "x")
    fork = _fork(Resource(b"vers", 1, body))
    assert vers_body(fork) == body


def test_vers_body_refuses_a_compressed_record():
    # the System's version numbers never live in a compressed vers; raw means raw
    body = b"\xa8\x9f\x65\x72" + b"\x00" * 20
    fork = _fork(Resource(b"vers", 1, body, None, resCompressed))
    assert vers_body(fork) is None


def test_vers_body_refuses_garbage_and_missing():
    assert vers_body(b"not a fork") is None
    assert vers_body(_fork(Resource(b"STR ", 128, b"x"))) is None
    assert vers_body(_fork(Resource(b"vers", 1, vers("7.5.5", "x"))), rid=2) is None


def test_file_version_reads_id_1_only():
    fork = _fork(Resource(b"vers", 1, vers("7.5.5", "x")),
                 Resource(b"vers", 2, vers("9.9.9", "y")))
    f = MacFile("SomeFile", rsrc=fork)
    assert file_version(f) == int.from_bytes(vers("7.5.5", "x")[:2], "big")
    assert file_version(MacFile("Garbage", rsrc=b"junk")) == 0
    assert file_version(None) == 0
