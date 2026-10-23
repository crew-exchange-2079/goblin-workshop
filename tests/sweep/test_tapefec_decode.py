"""Input-handling bugs in tapefec's decoder, driven by hand-built frames."""
import io

import pytest

import tapefec as t

BS = 4


def _header(k, m):
    out = io.BytesIO()
    t.encode_stream(io.BytesIO(b""), out, k=k, m=m, block_size=BS)
    return out.getvalue()[:t.HEADER_SIZE + t.STREAM_HEADER_SIZE]


def _frame(ftype, frame_index, block_id, payload, gdl=0):
    return t.pack_header(ftype, 0, frame_index, block_id, len(payload),
                         t.crc32(payload), gdl) + payload


class _Budget:
    """Fail instead of hanging when the decoder pads without bound."""

    def __init__(self, monkeypatch, limit=5000):
        self.n = 0
        orig = t._GroupDecoder._emit

        def emit(dec, payload):
            self.n += 1
            if self.n > limit:
                raise AssertionError("decoder emitted >%d blocks from a tiny input" % limit)
            return orig(dec, payload)
        monkeypatch.setattr(t._GroupDecoder, "_emit", emit)


def test_group_data_len_above_k_is_not_trusted():
    # k=2, m=1; a data frame claims its group holds 3 data blocks, so the
    # decoder emits 3 blocks (12 bytes) for a group that can hold only 2.
    # No k=2 encoder writes such a frame, so it is ignored as not ours
    # (it may well belong to a stream with a larger k).
    stream = (_header(2, 1)
              + _frame(t.FT_DATA, 1, 0, b"abcd", gdl=3)
              + _frame(t.FT_PARITY, 2, 0, b"\0" * BS, gdl=3))
    out = io.BytesIO()
    rep = t.decode_stream(io.BytesIO(stream), out)
    assert len(out.getvalue()) <= 2 * BS
    assert rep.implausible_frames == 2


def test_trailer_total_length_must_be_consistent_with_frame_count(monkeypatch):
    # Header + trailer only (2 frames) but the trailer claims 2**40 bytes.
    _Budget(monkeypatch)
    trailer = t.TRAILER_FMT.pack(2 ** 40, bytes(32))
    stream = _header(2, 1) + _frame(t.FT_TRAILER, 1, 0, trailer)
    rep = t.decode_stream(io.BytesIO(stream), io.BytesIO())
    assert rep.bytes_written < 1 << 20


def test_block_id_cannot_exceed_frame_index(monkeypatch):
    # Frame #1 claims to be data block 2**31: millions of absent groups.
    _Budget(monkeypatch)
    stream = _header(1, 1) + _frame(t.FT_DATA, 1, 2 ** 31, b"abcd", gdl=1)
    rep = t.decode_stream(io.BytesIO(stream), io.BytesIO())
    assert rep.bytes_written < 1 << 20
