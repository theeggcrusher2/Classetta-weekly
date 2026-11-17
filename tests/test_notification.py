"""The Notification Manager (Inside Macintosh: Processes, Notification
Manager): a request's alert is shown at once, and its response procedure
is called at an event call once the alert has been dismissed."""

from guest import Guest

from macrt.m68k import glue_os

nmType = 8
nmTypErr = -299
RESPONSE = 0x00F00000               # where the response procedure is: only what it is matters


class Alerts:
    """The display's part: the alerts notify was asked for, each with what
    to call as it's dismissed."""

    def __init__(self):
        self.shown = []

    def notify(self, name, message, dismissed=None):
        self.shown.append((name, message, dismissed))


def request(g, message=None, response=RESPONSE):
    rec = g.ptr(bytes(36))
    g.mem.w16(rec + 4, nmType)
    if message is not None:
        g.mem.w32(rec + glue_os.NM_STR, g.ptr(bytes([len(message)]) + message))
    g.mem.w32(rec + glue_os.NM_RESP, response & 0xFFFFFFFF)
    return rec


def responses(g):
    return [args[0][1] for addr, args in g.calls if addr == RESPONSE]


def test_gestalt_says_the_notification_manager_is_there(qapp):
    """Auspice 3.1 looks for it before anything else."""
    assert glue_os.Gestalt(Guest(), int.from_bytes(b"nmgr", "big")) == (0, 1)


def test_a_request_without_a_message_is_responded_to_at_the_next_event_call(qapp):
    g = Guest()
    rec = request(g)
    assert g("NMInstall", rec) == 0
    assert responses(g) == []
    glue_os.respond(g)
    assert responses(g) == [rec]
    glue_os.respond(g)
    assert responses(g) == [rec]                        # once
    assert rec in g.process.notifications               # until it's removed
    assert g("NMRemove", rec) == 0 and not g.process.notifications


def test_a_message_is_shown_and_its_response_waits_for_the_alert_to_be_dismissed(qapp):
    g = Guest()
    g.tb._d = alerts = Alerts()
    rec = request(g, b"Time to go")
    g("NMInstall", rec)
    [(name, message, dismissed)] = alerts.shown
    assert (name, message) == ("Test App", "Time to go")
    glue_os.respond(g)
    assert responses(g) == []
    dismissed()
    glue_os.respond(g)
    assert responses(g) == [rec]


def test_a_response_of_minus_one_removes_the_request_and_nil_does_nothing(qapp):
    g = Guest()
    gone, stays = request(g, response=-1), request(g, response=0)
    g("NMInstall", gone)
    g("NMInstall", stays)
    glue_os.respond(g)
    assert g.calls == [] and g.process.notifications == {stays}


def test_a_request_installed_again_by_its_response_waits_for_the_next_call(qapp):
    g = Guest()
    rec = request(g)
    g.procs[RESPONSE] = lambda p: g("NMInstall", p)
    g("NMInstall", rec)
    glue_os.respond(g)
    assert responses(g) == [rec]
    glue_os.respond(g)
    assert responses(g) == [rec, rec]


def test_a_request_removed_before_its_response_is_not_responded_to(qapp):
    g = Guest()
    rec = request(g)
    g("NMInstall", rec)
    g("NMRemove", rec)
    glue_os.respond(g)
    assert g.calls == []


def test_a_record_that_is_not_a_request_is_refused(qapp):
    g = Guest()
    rec = request(g)
    g.mem.w16(rec + 4, 0)
    assert g("NMInstall", rec) == nmTypErr and not g.process.notifications
