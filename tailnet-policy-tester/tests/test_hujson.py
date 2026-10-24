import pytest

from tailnet_policy_tester import hujson


def test_line_comments():
    assert hujson.loads('{"a": 1 // one\n}') == {"a": 1}


def test_block_comments_keep_line_numbers():
    text = '/* a\nb\n*/ {"a": }'
    with pytest.raises(hujson.HuJSONError) as e:
        hujson.loads(text)
    assert e.value.line == 3


def test_trailing_commas():
    assert hujson.loads('{"a": [1, 2,], "b": {"c": 3,},}') == {"a": [1, 2], "b": {"c": 3}}


def test_trailing_comma_before_comment():
    assert hujson.loads('[1, // last\n]') == [1]


def test_comment_markers_inside_strings_are_kept():
    assert hujson.loads('{"u": "http://x/*y*/", "v": "a,]"}') == {"u": "http://x/*y*/", "v": "a,]"}


def test_escaped_quote_in_string():
    assert hujson.loads(r'{"a": "say \"hi\" // not a comment"}') == {"a": 'say "hi" // not a comment'}


def test_duplicate_keys_rejected():
    with pytest.raises(hujson.HuJSONError, match="duplicate key"):
        hujson.loads('{"a": 1, "a": 2}')


def test_unterminated_string():
    with pytest.raises(hujson.HuJSONError, match="unterminated string"):
        hujson.loads('{"a": "x')


def test_unterminated_block_comment():
    with pytest.raises(hujson.HuJSONError, match="unterminated"):
        hujson.loads('{"a": 1 /* oops')


def test_bom_and_bytes():
    assert hujson.loads("﻿{}".encode()) == {}


def test_non_utf8_rejected():
    with pytest.raises(hujson.HuJSONError, match="UTF-8"):
        hujson.loads(b'{"a": "\xff"}')


def test_nan_rejected():
    with pytest.raises(hujson.HuJSONError):
        hujson.loads('{"a": NaN}')


def test_lone_comma_is_still_an_error():
    with pytest.raises(hujson.HuJSONError):
        hujson.loads("[,]")


def test_size_limit():
    with pytest.raises(hujson.HuJSONError, match="larger"):
        hujson.loads(b" " * (hujson.MAX_BYTES + 1))


def test_double_comma_is_still_an_error():
    with pytest.raises(hujson.HuJSONError):
        hujson.loads("[1,,]")


def test_empty_object_with_comma_is_an_error():
    with pytest.raises(hujson.HuJSONError):
        hujson.loads('{"a": {,}}')
