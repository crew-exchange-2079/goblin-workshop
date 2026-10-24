from quadlet_lint.unitfile import parse


def test_sections_and_line_numbers():
    u = parse("# comment\n[Unit]\nDescription=x\n\n[Container]\nImage=a\n")
    assert u.sections == {"Unit": 2, "Container": 5}
    assert [(e.section, e.key, e.value, e.line) for e in u.entries] == [
        ("Unit", "Description", "x", 3), ("Container", "Image", "a", 6)]
    assert not u.errors


def test_whitespace_around_equals_is_trimmed():
    u = parse("[Container]\n  Image =  quay.io/a/b  \n")
    assert u.get("Container", "Image") == "quay.io/a/b"


def test_value_may_contain_equals():
    u = parse("[Container]\nEnvironment=A=b=c\n")
    assert u.get("Container", "Environment") == "A=b=c"


def test_continuation_lines_join_with_space():
    u = parse("[Container]\nExec=/bin/app \\\n  --flag \\\n  value\nImage=x\n")
    assert u.get("Container", "Exec") == "/bin/app  --flag  value"
    assert u.last("Container", "Image").line == 5


def test_comment_inside_continuation_is_skipped():
    u = parse("[Container]\nExec=a \\\n# not part of it\n b\n")
    assert u.get("Container", "Exec") == "a  b"


def test_continuation_at_eof_does_not_crash():
    u = parse("[Container]\nExec=a \\")
    assert u.get("Container", "Exec") == "a"


def test_semicolon_comments():
    u = parse("; c\n[Container]\n;Image=x\nImage=y\n")
    assert [e.value for e in u.entries] == ["y"]


def test_last_and_all_with_reset():
    u = parse("[Container]\nVolume=a:/a\nVolume=\nVolume=b:/b\nVolume=c:/c\n")
    assert [e.value for e in u.all("Container", "Volume")] == ["b:/b", "c:/c"]
    assert u.get("Container", "Volume") == "c:/c"


def test_syntax_errors_are_collected_not_raised():
    u = parse("Key=before\n[Bad\n[Container]\nnoequals\n9 bad key=1\nImage=x\n")
    lines = [line for line, _ in u.errors]
    assert lines == [1, 2, 4, 5]
    assert u.get("Container", "Image") == "x"


def test_entries_after_bad_header_are_not_attributed_to_previous_section():
    u = parse("[Container]\nImage=a\n[Oops\nImage=b\n")
    assert u.get("Container", "Image") == "a"


def test_file_disable_directive():
    u = parse("# quadlet-lint: disable=QL101, ql201\n[Container]\n")
    assert u.disabled == {"QL101", "QL201"}


def test_disable_next_line_directive_attaches_to_next_assignment():
    u = parse("[Container]\n# quadlet-lint: disable-next-line=QL101\n\nImage=nginx\nPublishPort=1:1\n")
    assert u.disabled_lines == {4: {"QL101"}}


def test_crlf_input():
    u = parse("[Container]\r\nImage=a\r\n")
    assert u.get("Container", "Image") == "a"
