"""The survey tool: running applications headless and reporting on them."""
import collections
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QColor, QImage

from macrt import survey

ROOT = Path(__file__).resolve().parent.parent
HELLO = str(Path(__file__).resolve().parent / "apps" / "Hello.bin")
DIALOGS = str(Path(__file__).resolve().parent / "apps" / "Dialogs.bin")


# -- steps -------------------------------------------------------------------
def test_steps_are_parsed_and_checked():
    steps = survey.parse_steps(r"w 2.5; W 8;c 10,20 ;d 1,2 3,4 5,6;k ab\;c\n;K Return;"
                               r"K Left,0.5;C q;s menu-1;O")
    assert steps == [("w", 2.5), ("W", 8), ("c", (10, 20)), ("d", [(1, 2), (3, 4), (5, 6)]),
                     ("k", "ab;c\n"), ("K", "Return", None), ("K", "Left", 0.5),
                     ("C", "q"), ("s", "menu-1"), ("O",)]
    assert survey.parse_steps("") == [] == survey.parse_steps(" ; ;")


@pytest.mark.parametrize("text", [
    "w", "w -1", "w 4000", "w x", "W", "W -1", "c 10", "c a,b", "d 1,2", "k", r"k a\q", "k a\\",
    "K NoSuchKey", "C", "C qq", "s", "s end", "s a/b", "O now", "jump 1,2", "c10,20",
])
def test_a_step_that_isnt_one_is_refused(text):
    with pytest.raises(survey.StepError):
        survey.parse_steps(text)


def test_steps_take_about_as_long_as_their_waits_and_typing():
    steps = survey.parse_steps("w 2;W 3;k abcd;K Up,1")
    assert survey.steps_time(steps) == pytest.approx(2 + 3 + 4 * survey.TYPING_PACE + 1 + 0.4)


# -- lists of runs -----------------------------------------------------------
def test_a_list_fills_in_each_runs_fields(tmp_path):
    (tmp_path / "list.json").write_text(json.dumps({
        "defaults": {"wait": 2, "model": "ii"},
        "runs": [{"file": "Disk.dsk", "app": "Games:Dark Castle"},
                 {"file": ["a.dsk", "b.dsk"], "name": "two", "model": "plus", "wait": 0},
                 {"file": "Some App.bin", "depth": 8, "steps": "s a"}]}))
    runs = survey.load_list(tmp_path / "list.json")
    assert [r["name"] for r in runs] == ["Dark_Castle", "two", "Some_App"]
    assert runs[0]["file"] == [str(tmp_path / "Disk.dsk")]
    assert runs[1]["file"] == [str(tmp_path / "a.dsk"), str(tmp_path / "b.dsk")]
    assert [r["model"] for r in runs] == ["ii", "plus", "ii"]
    assert [r["wait"] for r in runs] == [2, 0, 2]
    argv = survey.one_argv(runs[2], tmp_path / "out")
    assert argv == [str(tmp_path / "Some App.bin"), "--out", str(tmp_path / "out"),
                    "--model", "ii", "--depth", "8", "--wait", "2", "--steps", "s a"]
    assert survey.one_argv({**runs[2], "settle": True}, tmp_path / "out") == [*argv, "--settle"]
    assert survey.one_argv({**runs[2], "outside_port_rect": True}, tmp_path / "out") \
        == [*argv, "--outside-port-rect"]
    assert survey.one_argv({**runs[2], "row_bytes": 1024}, tmp_path / "out") \
        == [*argv[:7], "--row-bytes", "1024", *argv[7:]]
    # A plain list of runs will do too.
    assert survey.check_runs([{"file": "x.bin"}], tmp_path)[0]["model"] == "plus"


@pytest.mark.parametrize("data, says", [
    ({"runs": [], "extra": 1}, "unknown keys"),
    ({"runs": {}}, "a list of runs"),
    ([1], "isn't an object"),
    ([{"file": "a", "colour": True}], "unknown fields"),
    ([{}], "no file"),
    ([{"file": ""}], "no file"),
    ([{"file": "a", "app": 3}], "app is a string"),
    ([{"file": "a", "name": "a/b"}], "a name is"),
    ([{"file": "a.bin"}, {"file": "b/a.bin"}], "another run named 'a'"),
    ([{"file": "a", "model": "se30"}], "model is"),
    ([{"file": "a", "wait": -1}], "wait is a number, 0 or more"),
    ([{"file": "a", "timeout": 0}], "timeout is a number, more than 0"),
    ([{"file": "a", "memory": 1.5}], "memory is a number"),
    ([{"file": "a", "speed": True}], "speed is a number"),
    ([{"file": "a", "depth": 3, "model": "ii"}], "depth is 1, 2, 4"),
    ([{"file": "a", "depth": 8}], "depth needs model"),
    ([{"file": "a", "size": "big"}], "isn't a screen size"),
    ([{"file": "a", "row_bytes": "1024"}], "row_bytes is a number"),
    ([{"file": "a", "row_bytes": 641}], "row-bytes is an even number"),
    ([{"file": "a", "row_bytes": 0x4000}], "less than 16384"),
    ([{"file": "a", "trace": "yes"}], "trace is true or false"),
    ([{"file": "a", "settle": 1}], "settle is true or false"),
    ([{"file": "a", "outside_port_rect": "yes"}], "outside_port_rect is true or false"),
    ([{"file": "a", "steps": "c 1"}], "isn't a point"),
])
def test_a_list_with_a_mistake_says_what_it_is(tmp_path, data, says):
    with pytest.raises(ValueError, match=says):
        survey.check_runs(data, tmp_path)


def test_a_list_that_isnt_json_says_so(tmp_path):
    (tmp_path / "list.json").write_text("[{")
    with pytest.raises(ValueError, match="list.json"):
        survey.load_list(tmp_path / "list.json")


def test_how_long_a_run_may_take():
    assert survey.time_limit({"timeout": 5}, 99) == 5
    assert survey.time_limit({}, 99) == 99
    assert survey.time_limit({"wait": 1, "steps": "w 3"}) == pytest.approx(
        1 + 3.1 + survey.SPARE_TIME)
    assert survey.time_limit({}) == pytest.approx(survey.DEFAULT_WAIT + survey.SPARE_TIME)


# -- settled waits -----------------------------------------------------------
def settles_at(pictures):
    """When settled() says the screen has, looked at every SETTLE_LOOK
    seconds showing each of pictures in turn (None: not every process
    asking for input); None if it never does."""
    looks = collections.deque()
    for i, picture in enumerate(pictures):
        if survey.settled(looks, i * survey.SETTLE_LOOK, picture):
            return i * survey.SETTLE_LOOK
    return None


QUIET = 2 * survey.SETTLE_QUIET                 # how long it takes to settle, at least
LOOKS = 40                                      # four seconds of looks


def test_a_screen_settles_once_it_has_stayed_the_same_a_while():
    assert QUIET - 2 * survey.SETTLE_LOOK < settles_at(["a"] * LOOKS) <= QUIET
    # A caret blinking, about twice a second, doesn't stop it.
    blink = (["a"] * 5 + ["b"] * 5) * 4
    assert QUIET - 2 * survey.SETTLE_LOOK < settles_at(blink) <= QUIET
    # Changed after a second, it settles that much later.
    assert 1 + QUIET - 2 * survey.SETTLE_LOOK < settles_at(["a"] * 10 + ["b"] * LOOKS) <= 1 + QUIET


def test_a_screen_that_keeps_changing_or_a_process_busy_isnt_settled():
    assert settles_at((["a", "b", "c"] * LOOKS)[:LOOKS]) is None
    assert settles_at([None] * LOOKS) is None
    # Not asking for a moment starts it again.
    later = settles_at(["a"] * 10 + [None] + ["a"] * LOOKS)
    assert 1.1 + QUIET - 2 * survey.SETTLE_LOOK < later <= 1.1 + QUIET


def queue(**stamps):
    """What asking() reads of a process's event queue."""
    never = float("-inf")
    return SimpleNamespace(**{"waiting": False, "waited_at": never, "asked_at": never,
                              "timed_at": never, **stamps})


def test_a_process_is_asking_for_input_while_it_waits_for_an_event_or_asks_and_asks():
    now, lately, a_while_ago = 100.0, 100.0 - survey.ASKED / 2, 100.0 - 2 * survey.ASKED
    assert not survey.asking(queue(), now)
    assert survey.asking(queue(waiting=True), now)
    assert survey.asking(queue(waited_at=lately), now)
    assert not survey.asking(queue(waited_at=a_while_ago), now)
    assert survey.asking(queue(asked_at=lately), now)
    assert not survey.asking(queue(asked_at=a_while_ago), now)
    # Reading the clock as it asks, it's waiting for a time, not for input,
    # even if it waits for an event between.
    assert survey.asking(queue(asked_at=lately, timed_at=a_while_ago), now)
    assert not survey.asking(queue(asked_at=lately, timed_at=lately), now)
    assert not survey.asking(queue(waited_at=lately, timed_at=lately), now)
    assert not survey.asking(queue(waiting=True, timed_at=lately), now)
    assert survey.asking(queue(waiting=True, timed_at=a_while_ago), now)


def test_a_settled_wait_ends_when_every_process_is_waiting_and_the_screen_still(qapp, tmp_path):
    from PyQt6.QtWidgets import QWidget

    screen = QWidget()
    screen.resize(64, 48)
    screen.show()
    run = survey.Run(SimpleNamespace(out=str(tmp_path), steps="", file=None, app=None,
                                     model=None))
    waiting = SimpleNamespace(events=queue(waiting=True), windows=[])
    busy = SimpleNamespace(events=queue(), windows=[])
    run.d = SimpleNamespace(screen_widget=screen, processes=[waiting])
    run.launched = time.monotonic()
    try:
        began = time.monotonic()
        assert run.wait(10, settle=True)
        assert QUIET - 2 * survey.SETTLE_LOOK < time.monotonic() - began < 8
        run.d.processes.append(busy)
        began = time.monotonic()
        assert not run.wait(2, settle=True)
        assert time.monotonic() - began >= 2
    finally:
        screen.close()


def test_a_wait_ends_when_the_last_process_quits(qapp, tmp_path, monkeypatch):
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QWidget

    monkeypatch.setattr(survey, "SETTLE_LOOK", 0)    # it looks at the screen each time round
    screen = QWidget()
    screen.resize(64, 48)
    screen.show()
    run = survey.Run(SimpleNamespace(out=str(tmp_path), steps="", file=None, app=None,
                                     model=None))
    run.d = SimpleNamespace(screen_widget=screen,
                            processes=[SimpleNamespace(events=queue(waiting=True), windows=[])])
    run.launched = time.monotonic()

    def quit():                 # as the display does when its last process quits
        run.d.processes.clear()
        screen.deleteLater()
    QTimer.singleShot(100, quit)
    began = time.monotonic()
    assert not run.wait(10, settle=True)
    assert time.monotonic() - began < 2


# -- reports -----------------------------------------------------------------
def result(name, outcome, unimplemented=(), system_errors=()):
    return {"name": name, "outcome": outcome,
            "processes": [{"name": name, "unimplemented": dict(u)} for u in unimplemented],
            "system_errors": [{"app": name, "id": i, "message": ""} for i in system_errors]}


def test_a_report_ranks_unimplemented_calls_by_how_many_runs_make_them():
    report = survey.summarize([
        result("a", "running", [{"SetDepth": 1, "Pack8 selector $0830": 40}]),
        result("b", "system error", [{"SetDepth": 2}], [3]),
        result("c", "quit", [{"SetDepth": 1}, {"SetDepth": 1, "SCSIDispatch": 1}]),
        result("d", "timeout"),
        result("e", "system error", [], [3, 28]),
    ])
    assert report["runs"] == 5
    assert report["outcomes"] == {"running": 1, "quit": 1, "system error": 2, "timeout": 1}
    assert report["by_outcome"] == {"system error": ["b", "e"], "timeout": ["d"]}
    assert report["unimplemented"] == [
        {"name": "SetDepth", "runs": 3, "calls": 5, "examples": ["a", "b", "c"]},
        {"name": "Pack8 selector $0830", "runs": 1, "calls": 40, "examples": ["a"]},
        {"name": "SCSIDispatch", "runs": 1, "calls": 1, "examples": ["c"]},
    ]
    assert report["system_error_ids"] == {"3": 2, "28": 1}
    text = survey.format_report(report, top=1)
    assert text.splitlines()[0] == "5 runs: 1 running, 1 quit, 2 system error, 1 timeout"
    assert "SetDepth" in text and "SCSIDispatch" not in text


def test_a_report_ranks_what_runs_asked_for_and_found_missing():
    asked = [{"name": n, "missing_asked": m} for n, m in (
        ("a", {"Gestalt 'qtim'": 2, "SlotManager": 1}),
        ("b", {"Gestalt 'qtim'": 1}),
        ("c", {}))]
    report = survey.summarize([{"name": a["name"], "outcome": "running",
                                "processes": [a]} for a in asked])
    assert report["missing_asked"] == [
        {"name": "Gestalt 'qtim'", "runs": 2, "calls": 3, "examples": ["a", "b"]},
        {"name": "SlotManager", "runs": 1, "calls": 1, "examples": ["a"]},
    ]
    text = survey.format_report(report)
    assert "asked for but missing, by how many runs ask:" in text
    assert "Gestalt 'qtim'  (a, b)" in text


def running(name, *windows):
    return {"name": name, "outcome": "running",
            "processes": [{"name": name, "windows": list(windows)}]}


def test_a_report_says_which_titles_running_show_nothing_or_wait_at_a_dialog():
    main = {"title": "Main", "visible": True}
    report = survey.summarize([
        running("blank"),
        running("hidden", {"title": "Main", "visible": False}),
        running("asks", {"title": "", "visible": True, "text": ["Error -43", "Try\nagain."]},
                main),
        running("plays", main),
        running("behind", {"title": "", "visible": False, "text": ["Gone"]}, main),
        running("long", {"title": "", "visible": True, "text": ["x" * 150]}),
        dict(running("opens"), host_dialogs=[{"title": "Open", "text": ["Games"]}]),
        dict(running("over", main), host_dialogs=[{"title": "Save", "text": []}]),
        result("gone", "quit"),
    ])
    assert report["no_window"] == ["blank", "hidden"]
    assert report["at_a_dialog"] == {"asks": ["Error -43", "Try\nagain."],
                                     "long": ["x" * 150], "opens": ["Open", "Games"],
                                     "over": ["Save"]}
    text = survey.format_report(report).splitlines()
    assert "running with no window: blank, hidden" in text
    assert "  asks: Error -43 / Try again." in text
    assert "  long: " + "x" * survey.DIALOG_TEXT + "…" in text


def test_a_report_names_the_runs_slowest_to_a_first_window(monkeypatch):
    monkeypatch.setattr(survey, "SLOWEST", 2)
    runs = [dict(result(name, "running"), first_window=secs)
            for name, secs in (("a", 1.25), ("b", None), ("c", 4.0), ("d", 0.5), ("e", 4.0))]
    report = survey.summarize(runs)
    assert report["slowest_to_a_window"] == [["c", 4.0], ["e", 4.0]]
    assert "slowest to show a window: c (4.0 s), e (4.0 s)" in survey.format_report(report)


def test_a_report_names_the_runs_that_kept_the_host_busy_as_they_waited():
    runs = [dict(result(name, outcome), busy=share)
            for name, outcome, share in (("a", "running", 0.05), ("b", "running", None),
                                         ("c", "running", 0.5), ("d", "quit", 1.0),
                                         ("e", "running", 1.02), ("f", "running", 0.5))]
    report = survey.summarize(runs)
    assert report["busy"] == [["e", 1.02], ["c", 0.5], ["f", 0.5]]
    text = survey.format_report(report, top=2)
    assert "busy at the end of their wait (3): e (102%), c (50%)" in text.splitlines()
    assert "busy" not in survey.format_report(survey.summarize(runs[:2]))


def test_a_report_names_the_runs_that_drew_outside_a_port_rect():
    note = {"port": "$012340", "port_rect": [0, 0, 20, 0], "bounds": [0, 0, 24, 44],
            "via": "fill", "box": [0, 0, 24, 44], "times": 3}
    runs = [dict(result("a", "running"), outside_port_rect=[]),
            dict(result("b", "running"), outside_port_rect=[note, dict(note, via="line")]),
            result("c", "quit")]
    report = survey.summarize(runs)
    assert report["outside_port_rect"] == [["b", 2]]
    assert "drew outside a portRect (1): b (2)" in survey.format_report(report).splitlines()
    assert "portRect" not in survey.format_report(survey.summarize([runs[0], runs[2]]))


def image(w, h, dots=()):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    for x, y in dots:
        img.setPixelColor(x, y, QColor("black"))
    return img


def test_two_screenshots_differ_in_a_box_round_the_pixels_that_do():
    assert survey.image_difference(image(64, 40), image(64, 40)) is None
    assert survey.image_difference(image(64, 40), image(64, 41)) == "size 64x40 -> 64x41"
    a, b = image(64, 40, [(5, 5)]), image(64, 40, [(5, 5), (0, 7), (63, 30), (20, 9)])
    assert survey.image_difference(a, b) == ((0, 7, 64, 31), 3)
    assert survey.image_difference(image(8, 8, [(3, 2)]), image(8, 8)) == ((3, 2, 4, 3), 1)
    faint = image(8, 8)                 # a bit at the top of a byte
    faint.setPixelColor(6, 1, QColor(255, 255, 127))
    assert survey.image_difference(image(8, 8), faint) == ((6, 1, 7, 2), 1)


def write_run(out, name, res, shots=()):
    folder = out / name
    folder.mkdir(parents=True)
    res["screenshots"] = {}
    for shot, img in shots:
        img.save(str(folder / f"{shot}.png"))
        res["screenshots"][shot] = f"{shot}.png"
    (folder / "result.json").write_text(json.dumps(res))


def test_comparing_two_surveys(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    write_run(old, "same", result("same", "running", [{"X": 1}]), [("end", image(16, 16))])
    write_run(new, "same", result("same", "running", [{"X": 9}]), [("end", image(16, 16))])
    write_run(old, "gone", result("gone", "quit"))
    write_run(new, "added", result("added", "quit"))
    write_run(old, "moved", result("moved", "running", [{"A": 1, "B": 1}]),
              [("end", image(16, 16)), ("a", image(8, 8))])
    write_run(new, "moved", result("moved", "system error", [{"B": 1, "C": 2}]),
              [("end", image(16, 16, [(2, 3), (4, 5)])), ("b", image(8, 8))])
    assert survey.compare(old, new) == [
        f"added: only in {new}",
        f"gone: only in {old}",
        "moved: running -> system error",
        "moved: unimplemented -A +C",
        f"moved/a.png: only in {old}",
        f"moved/b.png: only in {new}",
        "moved/end.png: 2 pixels differ in (2, 3, 5, 6)",
    ]
    assert survey.compare(new, new) == []


# -- running -----------------------------------------------------------------
def test_a_survey_that_resumes_runs_only_what_hadnt_finished(tmp_path, monkeypatch):
    ran = []

    def run_entry(entry, out, timeout=None):
        ran.append(entry["name"])
        res = {**result(entry["name"], "quit"), "elapsed": 1.0}
        (out / entry["name"]).mkdir(exist_ok=True)
        (out / entry["name"] / "result.json").write_text(json.dumps(res))
        return res

    monkeypatch.setattr(survey, "run_entry", run_entry)
    entries = survey.check_runs([{"file": f"{n}.bin"} for n in "abcd"], tmp_path)
    out, said = tmp_path / "out", []
    survey.run_list(entries[:2], out, 1, echo=said.append)
    assert said == ["[1/2] a: quit (1s)", "[2/2] b: quit (1s)"]
    (out / "c").mkdir()                 # stopped before it finished
    (out / "x").mkdir()                 # a run of some other list
    (out / "x" / "result.json").write_text(json.dumps(result("x", "quit")))
    ran.clear()
    said.clear()
    report = survey.run_list(entries, out, 1, echo=said.append, resume=True)
    assert ran == ["c", "d"]
    assert said == ["2 runs done before, 2 to run", "[1/2] c: quit (1s)", "[2/2] d: quit (1s)"]
    assert report["runs"] == 4 and report["outcomes"] == {"quit": 4}


def test_a_survey_runs_each_title_and_reports_what_it_did(tmp_path):
    (tmp_path / "list.json").write_text(json.dumps({"defaults": {"wait": 1}, "runs": [
        {"name": "quits", "file": HELLO, "steps": "s a;C q;w 2"},
        {"name": "stays", "file": HELLO, "steps": "c 100,100;s b", "outside_port_rect": True},
        {"name": "missing", "file": "no such file.bin"},
        {"name": "slow", "file": HELLO, "wait": 60, "timeout": 2},
        {"name": "asks", "file": DIALOGS, "wait": 2},
        {"name": "settles", "file": HELLO, "wait": 60, "settle": True},
    ]}))
    out = tmp_path / "out"
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    done = subprocess.run([sys.executable, "-m", "macrt.survey", "run",
                           str(tmp_path / "list.json"), "--out", str(out), "--jobs", "4"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    results = {r["name"]: r for r in survey.read_results(out)}

    quits = results["quits"]
    assert quits["outcome"] == "quit", quits
    assert quits["screenshots"] == {"a": "a.png"}       # no end shot: it had quit
    assert [p["running"] for p in quits["processes"]] == [False]

    stays = results["stays"]
    assert stays["outcome"] == "running", stays
    assert stays["steps_done"] == 2 and not stays["step_errors"]
    assert stays["screenshots"] == {"b": "b.png", "end": "end.png"}
    assert stays["processes"][0]["windows"] == [{"title": "Hello", "visible": True}]
    assert stays["host_dialogs"] == []
    assert stays["settled"] is None
    assert stays["outside_port_rect"] == []             # watched, and its window keeps inside
    assert "outside_port_rect" not in quits
    assert not QImage(str(out / "stays" / "end.png")).isNull()

    # Waiting for an event with its window drawn, it's settled soon after.
    settles = results["settles"]
    assert settles["outcome"] == "running", settles
    assert settles["first_window"] < settles["settled"] < 30 and settles["elapsed"] < 40
    assert 0 <= settles["busy"] < survey.BUSY            # and leaves the host alone

    # A dialog left up gives its text, ParamText's string put in and the
    # hidden item left out.
    asks = results["asks"]
    assert asks["outcome"] == "running", asks
    assert 0 < asks["first_window"] < 2
    assert asks["processes"][0]["windows"] == [
        {"title": "", "visible": True, "text": ["Hello, World!"]}]

    assert results["missing"]["outcome"] == "can't open"
    assert "no such file.bin" in results["missing"]["error"]

    slow = results["slow"]
    assert slow["outcome"] == "timeout" and slow["exit_status"] is None
    assert "most recent call first" in (out / "slow" / "log.txt").read_text()

    report = json.loads((out / "report.json").read_text())
    assert report["outcomes"] == {"running": 3, "quit": 1, "can't open": 1, "timeout": 1}
    assert "6 runs: 3 running, 1 quit, 1 can't open, 1 timeout" in done.stdout


def test_a_screenshot_shows_macrts_own_dialogs_over_the_screen(qapp, tmp_path):
    """Standard File's dialog and macrt's alerts are windows of their own:
    the screenshot puts them over the Mac's screen, one hanging past its
    edge moved back onto it."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QDialog, QWidget

    def coloured(w, colour, x, y, width, height):
        w.setAutoFillBackground(True)
        palette = w.palette()
        palette.setColor(w.backgroundRole(), QColor(colour))
        w.setPalette(palette)
        w.setGeometry(x, y, width, height)
        w.show()
        return w

    screen = coloured(QWidget(), Qt.GlobalColor.white, 0, 0, 200, 100)
    over = coloured(QDialog(screen), Qt.GlobalColor.blue, 20, 30, 40, 20)
    past = coloured(QDialog(screen), Qt.GlobalColor.red, 180, 90, 30, 30)
    qapp.processEvents()
    run = survey.Run(SimpleNamespace(out=str(tmp_path), steps="", file=None, app=None,
                                     model=None))
    run.d = SimpleNamespace(screen_widget=screen)
    try:
        run.shoot("dialogs")
    finally:
        for w in (over, past, screen):
            w.close()
    shot = QImage(str(tmp_path / "dialogs.png"))
    assert run.shots == {"dialogs": "dialogs.png"}
    assert (shot.width(), shot.height()) == (200, 100)
    assert shot.pixelColor(0, 0) == QColor(Qt.GlobalColor.white)
    assert shot.pixelColor(30, 40) == QColor(Qt.GlobalColor.blue)
    assert shot.pixelColor(199, 99) == QColor(Qt.GlobalColor.red)       # moved onto it
    assert shot.pixelColor(170, 70) == QColor(Qt.GlobalColor.red)


def test_a_run_records_macrts_own_dialogs_that_are_up(qapp):
    """An application waiting at Standard File's dialog has no window of
    its own up: the run says which of macrt's is, and what it says."""
    from PyQt6.QtWidgets import QDialog, QLabel, QVBoxLayout

    dialog = QDialog()
    dialog.setWindowTitle("Open")
    layout = QVBoxLayout(dialog)
    for text in ("Games 2.0", "", "Hidden"):
        layout.addWidget(QLabel(text))
    dialog.findChildren(QLabel)[2].hide()
    gone = QDialog()
    gone.setWindowTitle("Closed")
    try:
        assert survey.Run._host_dialogs() == []
        dialog.show()
        qapp.processEvents()
        assert survey.Run._host_dialogs() == [{"title": "Open", "text": ["Games 2.0"]}]
    finally:
        dialog.close()
        gone.close()


def test_typing_goes_into_macrts_own_dialog_while_one_is_up(qapp, tmp_path):
    """A Save dialog's name field gets the keys, Return included, which
    presses its default button; with no dialog up they go to the screen."""
    from PyQt6.QtWidgets import QDialog, QLineEdit, QPushButton, QVBoxLayout, QWidget

    class Screen(QWidget):
        def __init__(self):
            super().__init__()
            self.typed = ""

        def keyPressEvent(self, event):
            self.typed += event.text()

    screen = Screen()
    screen.show()
    run = survey.Run(SimpleNamespace(out=str(tmp_path), steps="", file=None, app=None,
                                     model=None))
    run.d = SimpleNamespace(screen_widget=screen, processes=[])
    run.do(("k", "ab"))
    assert screen.typed == "ab"

    dialog = QDialog(screen)
    name = QLineEdit(dialog)
    save = QPushButton("Save", dialog)
    save.setDefault(True)
    save.clicked.connect(dialog.accept)
    layout = QVBoxLayout(dialog)
    layout.addWidget(name)
    layout.addWidget(save)
    dialog.show()
    name.setFocus()
    qapp.processEvents()
    try:
        run.do(("k", "Player 1"))
        assert name.text() == "Player 1"
        run.do(("k", "\n"))
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert not dialog.isVisible()
    finally:
        dialog.close()
        screen.close()
    assert screen.typed == "ab"
