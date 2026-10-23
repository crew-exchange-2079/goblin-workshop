#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Property-based tests for tapefec's framer and decoder (needs hypothesis).

    python3 -m pip install -r requirements-dev.txt
    python3 -m unittest -v test_properties

The contract under test, per parity group of k' data + m parity frames:

  * at most m frames erased (deleted, payload damaged, or header damaged):
    decode returns the original bytes exactly and verifies the trailer;
  * more than m erased: decode reports the group unrecoverable, exits
    non-zero, and every byte it did NOT list as unrecoverable is the
    original byte -- it never hands back wrong data as if it were good.

`fuzz.py` drives the same oracle with unstructured byte-level damage.
"""

import io
import os
import sys
import unittest

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tapefec as T  # noqa: E402

PROFILE = os.environ.get("TAPEFEC_HYPOTHESIS_PROFILE", "default")
settings.register_profile("default", max_examples=150, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow])
settings.register_profile("long", max_examples=3000, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow])
settings.load_profile(PROFILE)

KEEP, DROP, BAD_PAYLOAD, BAD_HEADER = "keep", "drop", "payload", "header"
ERASURES = (DROP, BAD_PAYLOAD, BAD_HEADER)


# ------------------------------------------------------------------ helpers ---


def encode(data, **kw):
    out = io.BytesIO()
    T.encode_stream(io.BytesIO(data), out, **kw)
    return out.getvalue()


def decode(enc):
    out = io.BytesIO()
    rep = T.decode_stream(io.BytesIO(enc), out)
    return rep, out.getvalue()


def split_frames(enc):
    """[(header_dict, raw_frame_bytes)] in wire order, for a clean stream."""
    frames, p = [], 0
    while p < len(enc):
        head = T.unpack_header(enc[p:p + T.HEADER_SIZE])
        assert head is not None, "split_frames needs an undamaged stream"
        n = T.HEADER_SIZE + head["payload_len"]
        frames.append((head, enc[p:p + n]))
        p += n
    return frames


def group_of(head, k, m):
    if head["ftype"] == T.FT_DATA:
        return head["block_id"] // k
    if head["ftype"] == T.FT_PARITY:
        return head["block_id"] // m
    return None


def apply_damage(raw, how, salt):
    """Turn one frame into an erasure the way a tape would."""
    if how == KEEP:
        return raw
    if how == DROP:
        return b""
    buf = bytearray(raw)
    if how == BAD_PAYLOAD:
        if len(buf) == T.HEADER_SIZE:      # empty payload: nothing to flip
            return b""
        buf[T.HEADER_SIZE + salt % (len(buf) - T.HEADER_SIZE)] ^= 0x5A
    else:                                  # BAD_HEADER, past the magic
        buf[8 + salt % (T.HEADER_SIZE - 8)] ^= 0x5A
    return bytes(buf)


def decode_rc(rep):
    """The exit status cmd_decode would return (without --strict)."""
    if rep.blocks_unrecoverable or rep.sha256_match is False:
        return 1
    if not rep.trailer_seen:
        return 1
    return 0


def assert_no_wrong_bytes(tc, rep, orig, out):
    """Every byte outside a reported-unrecoverable range is the original."""
    holes = sorted((r["start"], r["end"]) for r in rep.unrecoverable_ranges)
    pos = 0
    for start, end in holes + [(len(orig), len(orig))]:
        if start > pos:
            got, want = out[pos:start], orig[pos:start]
            if got != want:
                bad = next(i for i, (a, b) in enumerate(zip(got, want))
                           if a != b) if len(got) == len(want) else 0
                tc.fail("wrong data at offset %d (outside every reported "
                        "unrecoverable range)" % (pos + bad))
        pos = max(pos, end)


# --------------------------------------------------------------- strategies ---


@st.composite
def code_params(draw):
    k = draw(st.integers(1, 12))
    m = draw(st.integers(1, 6))
    block_size = draw(st.sampled_from([1, 2, 3, 7, 16, 64, 129]))
    groups = draw(st.integers(0, 4))
    extra = draw(st.integers(0, k * block_size))
    length = groups * k * block_size + extra
    seed = draw(st.integers(0, 2**32 - 1))
    data = bytes((seed * 2654435761 + i * 40503) >> 7 & 0xFF
                 for i in range(length))
    version = draw(st.sampled_from([2, 2, 2, 1]))
    return dict(k=k, m=m, block_size=block_size, version=version), data


def erasure_plan(draw, frames, k, m, limit):
    """Per frame: KEEP or one kind of erasure, at most `limit` per group
    (`limit=None` means unbounded).  Header and trailer are kept."""
    per_group = {}
    plan = []
    for head, raw in frames:
        g = group_of(head, k, m)
        if g is None:
            plan.append((KEEP, 0))
            continue
        used = per_group.get(g, 0)
        if limit is not None and used >= limit:
            plan.append((KEEP, 0))
            continue
        how = draw(st.sampled_from((KEEP, KEEP) + ERASURES))
        if how != KEEP:
            per_group[g] = used + 1
        plan.append((how, draw(st.integers(0, 1 << 16))))
    return plan, per_group


def rebuild(frames, plan):
    return b"".join(apply_damage(raw, how, salt)
                    for (_h, raw), (how, salt) in zip(frames, plan))


# ------------------------------------------------------------------- tests ----


class TestFramerProperties(unittest.TestCase):

    @given(code_params(), st.integers(0, 0xFFFF))
    def test_clean_round_trip(self, params, stream_id):
        kw, data = params
        rep, out = decode(encode(data, stream_id=stream_id, **kw))
        self.assertEqual(out, data)
        self.assertTrue(rep.verified)
        self.assertEqual(rep.blocks_repaired + rep.blocks_unrecoverable, 0)

    @given(code_params())
    def test_frames_are_self_describing(self, params):
        kw, data = params
        k, m, bs = kw["k"], kw["m"], kw["block_size"]
        frames = split_frames(encode(data, **kw))
        self.assertEqual(frames[0][0]["ftype"], T.FT_HEADER)
        self.assertEqual(frames[-1][0]["ftype"], T.FT_TRAILER)
        self.assertEqual([h["frame_index"] for h, _ in frames],
                         list(range(len(frames))))
        n_data = -(-len(data) // bs)
        n_groups = -(-n_data // k)
        data_ids = [h["block_id"] for h, _ in frames if h["ftype"] == T.FT_DATA]
        par_ids = [h["block_id"] for h, _ in frames
                   if h["ftype"] == T.FT_PARITY]
        self.assertEqual(data_ids, list(range(n_data)))
        self.assertEqual(par_ids, list(range(n_groups * m)))
        for h, raw in frames:
            self.assertEqual(len(raw), T.HEADER_SIZE + h["payload_len"])
            if h["ftype"] in (T.FT_DATA, T.FT_PARITY):
                self.assertLess(h["block_id"], h["frame_index"])
                g = group_of(h, k, m)
                self.assertEqual(h["group_data_len"],
                                 min(k, n_data - g * k))

    @given(code_params(), st.data())
    def test_up_to_m_erasures_per_group_always_recover(self, params, data):
        kw, orig = params
        assume(kw["version"] == 2)        # v1 is not MDS for every (k, m)
        frames = split_frames(encode(orig, **kw))
        plan, _ = erasure_plan(data.draw, frames, kw["k"], kw["m"],
                               limit=kw["m"])
        rep, out = decode(rebuild(frames, plan))
        self.assertEqual(out, orig)
        self.assertTrue(rep.verified, rep.notes)
        self.assertEqual(rep.blocks_unrecoverable, 0)
        self.assertEqual(decode_rc(rep), 0)

    @given(code_params(), st.data())
    def test_beyond_m_fails_loudly_and_never_lies(self, params, data):
        kw, orig = params
        k, m = kw["k"], kw["m"]
        frames = split_frames(encode(orig, **kw))
        plan, per_group = erasure_plan(data.draw, frames, k, m, limit=None)
        # Force one group past the limit so the property is never vacuous.
        groups = sorted({group_of(h, k, m) for h, _ in frames} - {None})
        assume(groups)
        victim = data.draw(st.sampled_from(groups))
        members = [i for i, (h, _) in enumerate(frames)
                   if group_of(h, k, m) == victim]
        # A group of k' data blocks only has k'+m frames; > m of them must go,
        # and at least one data frame among them, or nothing is lost.
        assume(len(members) > m)
        need = m + 1
        pick = data.draw(st.permutations(members))[:need]
        if not any(frames[i][0]["ftype"] == T.FT_DATA for i in pick):
            pick[-1] = next(i for i in members
                            if frames[i][0]["ftype"] == T.FT_DATA)
        for i in pick:
            if plan[i][0] == KEEP:
                plan[i] = (data.draw(st.sampled_from(ERASURES)), i)

        rep, out = decode(rebuild(frames, plan))
        self.assertGreater(rep.blocks_unrecoverable, 0)
        self.assertFalse(rep.ok)
        self.assertNotEqual(decode_rc(rep), 0)
        self.assertEqual(len(out), len(orig))
        assert_no_wrong_bytes(self, rep, orig, out)
        for r in rep.unrecoverable_ranges:
            self.assertEqual(out[r["start"]:r["end"]],
                             bytes(r["end"] - r["start"]))

    @given(code_params(), st.data())
    def test_v1_streams_never_lie_either(self, params, data):
        kw, orig = params
        kw["version"] = 1
        frames = split_frames(encode(orig, **kw))
        plan, _ = erasure_plan(data.draw, frames, kw["k"], kw["m"],
                               limit=kw["m"])
        rep, out = decode(rebuild(frames, plan))
        # v1 may be singular for some patterns, but must say so.
        if rep.blocks_unrecoverable:
            self.assertNotEqual(decode_rc(rep), 0)
            assert_no_wrong_bytes(self, rep, orig, out)
        else:
            self.assertEqual(out, orig)
            self.assertTrue(rep.verified)

    @given(code_params(), st.data())
    def test_frame_order_within_a_group_is_irrelevant(self, params, data):
        kw, orig = params
        k, m = kw["k"], kw["m"]
        frames = split_frames(encode(orig, **kw))
        head, body, tail = frames[:1], frames[1:-1], frames[-1:]
        shuffled, i = [], 0
        while i < len(body):
            g = group_of(body[i][0], k, m)
            j = i
            while j < len(body) and group_of(body[j][0], k, m) == g:
                j += 1
            shuffled += data.draw(st.permutations(body[i:j]))
            i = j
        rep, out = decode(b"".join(r for _, r in head + shuffled + tail))
        self.assertEqual(out, orig)
        self.assertTrue(rep.verified)

    @given(code_params(), st.data())
    def test_junk_between_frames_is_harmless(self, params, data):
        kw, orig = params
        frames = split_frames(encode(orig, **kw))
        parts = []
        for _h, raw in frames:
            parts.append(raw)
            parts.append(data.draw(st.binary(max_size=60)))
        rep, out = decode(b"".join(parts))
        self.assertEqual(out, orig)
        self.assertTrue(rep.verified)

    @given(code_params(), st.data())
    def test_truncation_never_lies(self, params, data):
        kw, orig = params
        enc = encode(orig, **kw)
        cut = data.draw(st.integers(T.HEADER_SIZE + T.STREAM_HEADER_SIZE,
                                    len(enc) - 1))
        rep, out = decode(enc[:cut])
        self.assertNotEqual(decode_rc(rep), 0)
        self.assertFalse(rep.verified)
        n = min(len(out), len(orig))
        # Without a trailer the tail estimate may be zero-padded; whatever is
        # outside a reported hole must still be original.
        assert_no_wrong_bytes(self, rep, orig[:n], out[:n])


class TestCodecProperties(unittest.TestCase):

    @given(st.integers(1, 30), st.integers(1, 10), st.integers(0, 40),
           st.data())
    def test_rs_any_m_erasures_recover(self, k, m, length, data):
        g = T.cauchy_generator_matrix(k, m)
        blocks = [data.draw(st.binary(min_size=length, max_size=length))
                  for _ in range(k)]
        parity = T.rs_encode_group(blocks, length, g, m)
        lost_total = data.draw(st.integers(0, m))
        lost = set(data.draw(st.permutations(range(k + m)))[:lost_total])
        known = {j: blocks[j] for j in range(k) if j not in lost}
        erased = [j for j in range(k) if j in lost]
        live = {i: parity[i] for i in range(m) if k + i not in lost}
        fixed = T.rs_repair(known, erased, live, length, g, m)
        for j in erased:
            self.assertEqual(fixed[j], blocks[j])

    @given(st.integers(1, 30), st.integers(1, 10), st.data())
    def test_rs_too_few_parity_raises(self, k, m, data):
        assume(k > m)
        g = T.cauchy_generator_matrix(k, m)
        blocks = [bytes([j]) * 4 for j in range(k)]
        parity = T.rs_encode_group(blocks, 4, g, m)
        n_erased = data.draw(st.integers(1, k))
        n_live = data.draw(st.integers(0, min(m, n_erased - 1)))
        erased = list(range(n_erased))
        known = {j: blocks[j] for j in range(n_erased, k)}
        live = {i: parity[i] for i in range(n_live)}
        with self.assertRaises(T.Unrecoverable):
            T.rs_repair(known, erased, live, 4, g, m)

    @given(st.lists(st.lists(st.integers(0, 255), min_size=1, max_size=6),
                    min_size=1, max_size=6))
    def test_matrix_inverse_or_singular(self, rows):
        n = len(rows)
        mat = [(r * n)[:n] for r in rows]
        try:
            inv = T.gf_matrix_invert(mat)
        except T.TapefecError:
            return
        for i in range(n):
            for j in range(n):
                acc = 0
                for t in range(n):
                    acc ^= T.gf_mul(mat[i][t], inv[t][j])
                self.assertEqual(acc, int(i == j))


if __name__ == "__main__":
    unittest.main()
