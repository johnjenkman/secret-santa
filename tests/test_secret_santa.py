"""Test suite for secret_santa.py.

Run with:  pytest test_secret_santa.py -v
Requires:  pytest  (the module under test has no third-party dependencies)

Tests target observable behaviour rather than internal representation, so they
stay valid regardless of how the messages are rendered internally.
"""

import json
import random
from collections.abc import Sequence
from pathlib import Path

import pytest

from secret_santa import (
    Assignment,
    ConfigError,
    NoSolutionError,
    OutputError,
    OutputFormat,
    Participant,
    _draw_derangement,
    _draw_single_cycle,
    _permitted,
    _slugify,
    _validate,
    draw,
    emit,
    html_message,
    load_participants,
    main,
    write_per_person,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def p(name: str, *excludes: str) -> Participant:
    """Shorthand Participant with optional exclusions."""
    return Participant(name, excludes=frozenset(excludes))


def cycle_sizes(assignment: Assignment) -> list[int]:
    """Return the sorted sizes of the cycles in a permutation dict."""
    seen: set[Participant] = set()
    sizes: list[int] = []
    for node in assignment:
        if node in seen:
            continue
        length, cur = 0, node
        while cur not in seen:
            seen.add(cur)
            cur = assignment[cur]
            length += 1
        sizes.append(length)
    return sorted(sizes)


def assert_valid(people: Sequence[Participant], assignment: Assignment) -> None:
    """A proper derangement: bijection, no fixed points, no excluded pairs."""
    assert set(assignment) == set(people)
    assert set(assignment.values()) == set(people)
    for giver, receiver in assignment.items():
        assert giver != receiver, f"{giver.name} drew themselves"
        assert receiver.name not in giver.excludes, f"{giver.name} drew excluded {receiver.name}"


def write_participants(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# The festive cast from participants.example.json: two mutually-excluding pairs
# (Santa/Mrs. Claus and Buddy/Jovie) plus two unconstrained folks (Rudolph, Frosty).
COUPLES = [
    p("Santa", "Mrs. Claus"),
    p("Mrs. Claus", "Santa"),
    p("Rudolph"),
    p("Frosty"),
    p("Buddy", "Jovie"),
    p("Jovie", "Buddy"),
]

# Two couples that can only gift within their own pair: a valid derangement
# exists ([2, 2]) but there is NO single Hamiltonian cycle.
TWO_COUPLES = [p("A", "C", "D"), p("B", "C", "D"), p("C", "A", "B"), p("D", "A", "B")]


# --------------------------------------------------------------------------- #
# _permitted
# --------------------------------------------------------------------------- #
def test_permitted_rejects_self() -> None:
    a = p("A")
    assert _permitted(a, a) is False


def test_permitted_rejects_excluded() -> None:
    assert _permitted(p("A", "B"), p("B")) is False


def test_permitted_allows_others() -> None:
    assert _permitted(p("A"), p("B")) is True


# --------------------------------------------------------------------------- #
# Participant.from_dict
# --------------------------------------------------------------------------- #
def test_from_dict_full() -> None:
    got = Participant.from_dict({"name": "Al", "email": "al@x.com", "excludes": ["Bo"]})
    assert got == Participant("Al", "al@x.com", frozenset({"Bo"}))


def test_from_dict_minimal() -> None:
    got = Participant.from_dict({"name": "Al"})
    assert got.name == "Al"
    assert got.email is None
    assert got.excludes == frozenset()


@pytest.mark.parametrize(
    "data",
    [
        {"email": "no@name.com"},  # missing name
        {"name": 123},  # non-string name
        {"name": "Al", "email": 5},  # non-string email
        {"name": "Al", "excludes": "Bo"},  # excludes not a list
        {"name": "Al", "excludes": [1, 2]},  # excludes items not strings
        {"name": ""},  # blank name
        {"name": "   "},  # whitespace-only name
        ["not", "a", "dict"],  # not a mapping
        "just a string",  # not a mapping
        {"name": "Al", "exclude": ["Bo"]},  # typo'd key must not be silently dropped
        {"name": "Al", "notes": "hi"},  # unknown key
    ],
)
def test_from_dict_rejects_bad_input(data: object) -> None:
    with pytest.raises(ConfigError):
        Participant.from_dict(data)


def test_from_dict_unknown_key_error_names_the_key() -> None:
    # A typo like "exclude" would silently drop a constraint; the error must
    # point at the offending key so the user can fix the file.
    with pytest.raises(ConfigError, match="exclude"):
        Participant.from_dict({"name": "Al", "exclude": ["Bo"]})


def test_participant_is_hashable_and_frozen() -> None:
    person = p("A")
    _ = {person}  # hashable -> usable in sets/dict keys
    with pytest.raises(AttributeError):
        person.name = "B"  # ty: ignore[invalid-assignment]  # frozen: must fail at runtime


# --------------------------------------------------------------------------- #
# draw: fast path (no exclusions)
# --------------------------------------------------------------------------- #
def test_draw_no_exclusions_is_single_cycle() -> None:
    people = [p(c) for c in "ABCDE"]
    for seed in range(200):
        a = draw(people, rng=random.Random(seed))
        assert_valid(people, a)
        assert cycle_sizes(a) == [5], "unconstrained draw must be one N-cycle"


# --------------------------------------------------------------------------- #
# draw: constrained (prefers single cycle, falls back)
# --------------------------------------------------------------------------- #
def test_draw_constrained_prefers_single_cycle() -> None:
    for seed in range(200):
        a = draw(COUPLES, rng=random.Random(seed))
        assert_valid(COUPLES, a)
        assert cycle_sizes(a) == [len(COUPLES)], "should be a single cycle"
        assert 2 not in cycle_sizes(a), "no reciprocal pairs"


def test_draw_falls_back_when_no_single_cycle() -> None:
    # No Hamiltonian cycle exists, but a [2, 2] derangement does: must succeed.
    for seed in range(50):
        a = draw(TWO_COUPLES, rng=random.Random(seed))
        assert_valid(TWO_COUPLES, a)
        assert cycle_sizes(a) == [2, 2]


def test_draw_impossible_raises() -> None:
    with pytest.raises(NoSolutionError):
        draw([p("P", "Q"), p("Q", "P")], rng=random.Random(0))


def test_draw_too_few_participants_raises() -> None:
    with pytest.raises(NoSolutionError):
        draw([p("Solo")], rng=random.Random(0))


def test_draw_is_reproducible_with_seed() -> None:
    a = draw(COUPLES, rng=random.Random(42))
    b = draw(COUPLES, rng=random.Random(42))
    assert {g.name: r.name for g, r in a.items()} == {g.name: r.name for g, r in b.items()}


def test_draw_unpredictable_across_seeds() -> None:
    seen = {
        tuple(sorted((g.name, r.name) for g, r in draw(COUPLES, rng=random.Random(s)).items()))
        for s in range(50)
    }
    assert len(seen) > 1


def test_draw_default_rng_produces_valid_assignment() -> None:
    # No rng argument -> SystemRandom path; must still yield a valid draw.
    a = draw(COUPLES)
    assert_valid(COUPLES, a)


def test_exclusion_is_directional() -> None:
    # "A excludes B" only bars A -> B; B may still draw A.
    people = [p("A", "B"), p("B"), p("C"), p("D")]
    b_drew_a = False
    for seed in range(100):
        a = draw(people, rng=random.Random(seed))
        assert_valid(people, a)
        b_drew_a = b_drew_a or a[people[1]] == people[0]
    assert b_drew_a, "exclusions must not be treated as symmetric"


def test_draw_large_unconstrained_no_recursionerror() -> None:
    people = [p(f"n{i}") for i in range(3000)]
    a = draw(people, rng=random.Random(1))
    assert_valid(people, a)
    assert cycle_sizes(a) == [3000]


def test_draw_large_constrained_no_recursionerror() -> None:
    # 1,200 > default recursion limit; the iterative solver must not blow up.
    names = [f"q{i}" for i in range(1200)]
    people = [p(names[i], names[(i + 1) % len(names)]) for i in range(len(names))]
    a = draw(people, rng=random.Random(2))
    assert_valid(people, a)


# --------------------------------------------------------------------------- #
# _draw_single_cycle
# --------------------------------------------------------------------------- #
def test_single_cycle_returns_one_cycle() -> None:
    a = _draw_single_cycle(COUPLES, random.Random(0))
    assert a is not None
    assert_valid(COUPLES, a)
    assert cycle_sizes(a) == [len(COUPLES)]


def test_single_cycle_returns_none_when_impossible() -> None:
    assert _draw_single_cycle(TWO_COUPLES, random.Random(0)) is None


def test_single_cycle_returns_none_when_budget_exhausted() -> None:
    # A step budget of 1 cannot complete any real search -> give up.
    assert _draw_single_cycle(COUPLES, random.Random(0), max_steps=1) is None


# --------------------------------------------------------------------------- #
# _draw_derangement (fallback)
# --------------------------------------------------------------------------- #
def test_derangement_finds_valid_solution() -> None:
    a = _draw_derangement(TWO_COUPLES, random.Random(0))
    assert_valid(TWO_COUPLES, a)


def test_derangement_raises_when_impossible() -> None:
    with pytest.raises(NoSolutionError):
        _draw_derangement([p("P", "Q"), p("Q", "P")], random.Random(0))


# --------------------------------------------------------------------------- #
# load_participants & _validate
# --------------------------------------------------------------------------- #
def test_load_valid_file(tmp_path: Path) -> None:
    f = write_participants(tmp_path / "p.json", [{"name": "A"}, {"name": "B", "excludes": ["A"]}])
    people = load_participants(f)
    assert [x.name for x in people] == ["A", "B"]
    assert people[1].excludes == frozenset({"A"})


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_participants(tmp_path / "does-not-exist.json")


def test_load_non_utf8_file_raises_config_error(tmp_path: Path) -> None:
    f = tmp_path / "latin1.json"
    f.write_bytes(b'[{"name": "caf\xe9"}]')  # é as Latin-1: invalid UTF-8
    with pytest.raises(ConfigError, match="UTF-8"):
        load_participants(f)


def test_load_invalid_json_raises(tmp_path: Path) -> None:
    f = tmp_path / "bad.json"
    f.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_participants(f)


def test_load_non_list_top_level_raises(tmp_path: Path) -> None:
    f = write_participants(tmp_path / "obj.json", {"name": "A"})
    with pytest.raises(ConfigError):
        load_participants(f)


def test_validate_rejects_duplicate_names() -> None:
    with pytest.raises(ConfigError, match="duplicate"):
        _validate([p("A"), p("A"), p("B")])


def test_validate_rejects_dangling_exclusion() -> None:
    with pytest.raises(ConfigError, match="unknown"):
        _validate([p("A", "Ghost"), p("B")])


def test_validate_allows_self_exclusion() -> None:
    # Redundant (the draw already forbids self-gifting) but harmless config.
    _validate([p("A", "A"), p("B")])


# --------------------------------------------------------------------------- #
# _slugify (path-safety)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name, expected",
    [
        ("Alice", "alice"),
        ("Mary Jane", "mary-jane"),
        ("../Admin", "admin"),  # traversal neutralised
        ("/etc/passwd", "etc-passwd"),  # absolute path neutralised
        ("..", ""),  # pure traversal -> empty (caller falls back)
        ("!!!", ""),  # punctuation only -> empty
        ("José", "josé"),  # unicode letters preserved
        ("李雷", "李雷"),  # CJK preserved
    ],
)
def test_slugify(name: str, expected: str) -> None:
    assert _slugify(name) == expected


def test_slugify_never_contains_separators() -> None:
    for hostile in ["../../etc", "a/b\\c", "..\\..\\x", "/abs/path"]:
        slug = _slugify(hostile)
        assert "/" not in slug and "\\" not in slug and ".." not in slug


# --------------------------------------------------------------------------- #
# render / html_message
# --------------------------------------------------------------------------- #
def test_html_message_escapes_interpolated_names() -> None:
    msg = html_message(p("<script>Mallory</script>"), p("Bob"))
    assert "&lt;script&gt;" in msg
    assert "<script>" not in msg


def test_html_message_preserves_static_markup() -> None:
    msg = html_message(p("Alice"), p("Bob"))
    assert "Secret Santa" in msg  # literal badge text, not escaped
    assert "class='match-name'" in msg  # literal template markup preserved
    assert "Alice" in msg and "Bob" in msg


def test_html_message_giver_and_receiver_in_right_places() -> None:
    # Swapping the arguments must not go unnoticed: the giver is greeted,
    # the receiver is the revealed match.
    msg = html_message(p("Alice"), p("Bob"))
    assert "<h2 class='giver-name'>Alice</h2>" in msg
    assert "<h1 class='match-name'>Bob</h1>" in msg


# --------------------------------------------------------------------------- #
# emit
# --------------------------------------------------------------------------- #
def test_emit_table(capsys: pytest.CaptureFixture[str]) -> None:
    a = draw(COUPLES, rng=random.Random(1))
    emit(a, OutputFormat.TABLE)
    out = capsys.readouterr().out
    assert "→" in out  # rightwards arrow
    for person in COUPLES:
        assert person.name in out


def test_emit_table_no_ansi_when_not_a_tty(capsys: pytest.CaptureFixture[str]) -> None:
    # capsys' stdout is not a terminal: redirected output must be plain text.
    a = draw(COUPLES, rng=random.Random(1))
    emit(a, OutputFormat.TABLE)
    assert "\033" not in capsys.readouterr().out


def test_emit_table_colors_on_a_tty(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    a = draw(COUPLES, rng=random.Random(1))
    emit(a, OutputFormat.TABLE)
    assert "\033[92m" in capsys.readouterr().out


def test_emit_table_empty_assignment_is_a_no_op(capsys: pytest.CaptureFixture[str]) -> None:
    emit({}, OutputFormat.TABLE)  # library edge: must not raise on max()
    assert capsys.readouterr().out == ""


def test_emit_json(capsys: pytest.CaptureFixture[str]) -> None:
    a = draw(COUPLES, rng=random.Random(1))
    emit(a, OutputFormat.JSON)
    out = capsys.readouterr().out
    assert "\033" not in out  # no ANSI codes: must stay machine-parseable
    parsed = json.loads(out)
    assert parsed == {g.name: r.name for g, r in a.items()}  # exact pairings


def test_emit_json_non_ascii(capsys: pytest.CaptureFixture[str]) -> None:
    jose, bob = p("José"), p("Bob")
    emit({jose: bob, bob: jose}, OutputFormat.JSON)
    assert "José" in capsys.readouterr().out  # ensure_ascii=False: no é


def test_emit_html_escapes(capsys: pytest.CaptureFixture[str]) -> None:
    a = {p("<b>X"): p("Y"), p("Y"): p("<b>X")}
    emit(a, OutputFormat.HTML)
    out = capsys.readouterr().out
    assert "&lt;b&gt;X" in out  # interpolated name escaped
    assert "class='match-name'" in out  # static card markup preserved


# --------------------------------------------------------------------------- #
# write_per_person
# --------------------------------------------------------------------------- #
def test_write_per_person_creates_one_file_each(tmp_path: Path) -> None:
    a = draw(COUPLES, rng=random.Random(1))
    out = tmp_path / "msgs"
    count = write_per_person(a, out)
    assert count == len(COUPLES)
    assert len(list(out.glob("*.html"))) == len(COUPLES)


def test_write_per_person_traversal_stays_inside(tmp_path: Path) -> None:
    evil, safe = p("../evil"), p("safe")
    out = tmp_path / "msgs"
    write_per_person({evil: safe, safe: evil}, out)
    assert (out / "evil.html").exists()
    assert not (tmp_path / "evil.html").exists()  # nothing escaped the target dir


def test_write_per_person_collision_does_not_overwrite(tmp_path: Path) -> None:
    # "Amy!" and "Amy?" both slugify to "amy"; neither file may be lost.
    a1, a2 = p("Amy!"), p("Amy?")
    out = tmp_path / "msgs"
    write_per_person({a1: a2, a2: a1}, out)
    assert len(list(out.glob("*.html"))) == 2


def test_write_per_person_rename_collision_with_natural_slug(tmp_path: Path) -> None:
    # "a!" collides on "a" and gets renamed — but the renamed slug must not
    # collide with "a 3"'s natural slug "a-3" and overwrite that file.
    a, a3, abang = p("a"), p("a 3"), p("a!")
    out = tmp_path / "msgs"
    count = write_per_person({a: a3, a3: abang, abang: a}, out)
    files = list(out.glob("*.html"))
    assert count == 3
    assert len(files) == 3
    givers = {  # every giver's message must survive on disk, whatever its filename
        f.read_text(encoding="utf-8").split("class='giver-name'>")[1].split("<")[0] for f in files
    }
    assert givers == {"a", "a 3", "a!"}


def test_write_per_person_file_contains_receiver(tmp_path: Path) -> None:
    alice, bob = p("Alice"), p("Bob")
    out = tmp_path / "msgs"
    write_per_person({alice: bob, bob: alice}, out)
    msg = (out / "alice.html").read_text(encoding="utf-8")
    assert "<h2 class='giver-name'>Alice</h2>" in msg
    assert "<h1 class='match-name'>Bob</h1>" in msg


def test_write_per_person_unicode_names(tmp_path: Path) -> None:
    jose, lilei = p("José"), p("李雷")
    out = tmp_path / "msgs"
    write_per_person({jose: lilei, lilei: jose}, out)
    assert "李雷" in (out / "josé.html").read_text(encoding="utf-8")
    assert "José" in (out / "李雷.html").read_text(encoding="utf-8")


def test_write_per_person_avoids_windows_reserved_names(tmp_path: Path) -> None:
    # "Con" slugs to "con"; on Windows "con.html" is a device name and the
    # write can be silently swallowed. The file must be renamed, not lost.
    import ntpath

    con, bob = p("Con"), p("Bob")
    out = tmp_path / "msgs"
    count = write_per_person({con: bob, bob: con}, out)
    files = list(out.glob("*.html"))
    assert count == 2
    assert len(files) == 2
    assert all(not ntpath.isreserved(f.name) for f in files)
    givers = {
        f.read_text(encoding="utf-8").split("class='giver-name'>")[1].split("<")[0] for f in files
    }
    assert givers == {"Con", "Bob"}


def test_write_per_person_empty_name_falls_back(tmp_path: Path) -> None:
    blank, other = p("!!!"), p("Bob")
    out = tmp_path / "msgs"
    write_per_person({blank: other, other: blank}, out)
    assert (out / "participant.html").exists()


def test_write_per_person_mkdir_failure_raises_output_error(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    a = draw(COUPLES, rng=random.Random(1))
    with pytest.raises(OutputError):
        write_per_person(a, blocker / "sub")  # parent is a file -> mkdir fails


def test_write_per_person_write_failure_raises_output_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pathlib

    def boom(self: object, *args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(pathlib.Path, "write_text", boom)
    a = draw(COUPLES, rng=random.Random(1))
    with pytest.raises(OutputError):
        write_per_person(a, tmp_path / "msgs")


def test_write_per_person_containment_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even if slugify were compromised, the resolved-path check must block escape.
    import secret_santa

    monkeypatch.setattr(secret_santa, "_slugify", lambda name: "../escape")
    a = draw(COUPLES, rng=random.Random(1))
    with pytest.raises(OutputError):
        write_per_person(a, tmp_path / "msgs")


# --------------------------------------------------------------------------- #
# main (CLI)
# --------------------------------------------------------------------------- #
def test_main_success_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = write_participants(tmp_path / "p.json", [{"name": "A"}, {"name": "B"}, {"name": "C"}])
    rc = main([str(f), "--seed", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "A" in out and "B" in out and "C" in out


def test_main_json_format(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = write_participants(tmp_path / "p.json", [{"name": "A"}, {"name": "B"}, {"name": "C"}])
    rc = main([str(f), "--format", "json", "--seed", "1"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    # Same seed, same file -> main must print exactly this draw's pairings.
    expected = draw(load_participants(f), rng=random.Random(1))
    assert parsed == {g.name: r.name for g, r in expected.items()}


def test_main_config_error_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([str(tmp_path / "missing.json")])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_main_non_utf8_file_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "latin1.json"
    f.write_bytes(b'[{"name": "caf\xe9"}]')
    rc = main([str(f)])
    assert rc == 1
    assert "error:" in capsys.readouterr().err  # friendly message, not a traceback


def test_main_impossible_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = write_participants(
        tmp_path / "p.json",
        [{"name": "P", "excludes": ["Q"]}, {"name": "Q", "excludes": ["P"]}],
    )
    rc = main([str(f)])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_main_per_person_writes_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = write_participants(tmp_path / "p.json", [{"name": "A"}, {"name": "B"}, {"name": "C"}])
    out = tmp_path / "out"
    rc = main([str(f), "--seed", "1", "--per-person", str(out)])
    assert rc == 0
    assert len(list(out.glob("*.html"))) == 3


def test_main_html_format(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = write_participants(tmp_path / "p.json", [{"name": "A"}, {"name": "B"}, {"name": "C"}])
    rc = main([str(f), "--format", "html", "--seed", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("class='match-name'") == 3  # one card per giver


def test_main_output_error_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = write_participants(tmp_path / "p.json", [{"name": "A"}, {"name": "B"}, {"name": "C"}])
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    rc = main([str(f), "--seed", "1", "--per-person", str(blocker / "sub")])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("participants", [[], [{"name": "Solo"}]])
def test_main_too_few_participants_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], participants: list[dict[str, str]]
) -> None:
    f = write_participants(tmp_path / "p.json", participants)
    rc = main([str(f)])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_main_rejects_unknown_format(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = write_participants(tmp_path / "p.json", [{"name": "A"}, {"name": "B"}])
    with pytest.raises(SystemExit) as excinfo:
        main([str(f), "--format", "bogus"])
    assert excinfo.value.code == 2  # argparse usage error


def test_main_seed_zero_reproducible(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Seed 0 is falsy: it must still be honoured, not swapped for SystemRandom.
    f = write_participants(tmp_path / "p.json", [{"name": "A"}, {"name": "B"}, {"name": "C"}])
    assert main([str(f), "--format", "json", "--seed", "0"]) == 0
    first = capsys.readouterr().out
    assert main([str(f), "--format", "json", "--seed", "0"]) == 0
    assert capsys.readouterr().out == first
    json.loads(first)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
