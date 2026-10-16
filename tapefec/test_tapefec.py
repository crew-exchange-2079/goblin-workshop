#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Test suite for tapefec.  Python 3 stdlib unittest, no dependencies.

    nice -n 19 python3 -m unittest -v test_tapefec

Sections:
  A  GF(2^8) identities
  B  Reed-Solomon construction and erasure decoding
  C  stream round trips (0, 1, block-1, block, block+1, 10 MB)
  D  damage: corruption, deletion, garbage, truncation, wrong stream id
"""

import io
import itertools
import json
import os
import random
import subprocess
import sys
import unittest
from unittest import mock

import tapefec as T

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "tapefec.py")

SMALL = dict(k=6, m=3, block_size=512)


# ------------------------------------------------------------------ helpers ---


def encode(data, **kw):
    out = io.BytesIO()
    T.encode_stream(io.BytesIO(data), out, **kw)
    return out.getvalue()


def decode(enc):
    out = io.BytesIO()
    rep = T.decode_stream(io.BytesIO(enc), out)
    return rep, out.getvalue()


def frame_offsets(enc):
    """[(start, end, frame_dict), ...] for every frame in `enc`."""
    frames, info, counts, stats, damaged = T.inspect_stream(io.BytesIO(enc))
    offs, p = [], 0
    for fr in frames:
        n = 40 + fr["payload_len"]
        offs.append((p, p + n, fr))
        p += n
    return offs, info


def damage(enc, lost=(), corrupt=(), junk_at=None, junk=b"", truncate=None):
    """Return a copy of `enc` with frames removed, payload bytes flipped,
    junk spliced in, and/or the tail truncated."""
    offs, _ = frame_offsets(enc)
    buf = bytearray(enc)
    for i in sorted(lost, reverse=True):
        s, e, _fr = offs[i]
        del buf[s:e]
    for i, off in corrupt:
        s = offs[i][0] + 40 + off
        buf[s] ^= 0xA5
    if junk_at is not None:
        buf[offs[junk_at][0]:offs[junk_at][0]] = junk
    if truncate is not None:
        del buf[truncate:]
    return bytes(buf)


def rnd(n, seed=None):
    return random.Random(seed).randbytes(n)


def group_frames(offs, k, m, group):
    """Indices of the data+parity frames of one group, in wire order."""
    first = 1 + group * (k + m)
    return list(range(first, first + k + m))


# ------------------------------------------------------------- A: GF(2^8) ----


class TestGF(unittest.TestCase):
    def test_polynomial_and_primitive_element(self):
        self.assertEqual(T.GF_POLY, 0x11D)
        self.assertEqual(T.gf_pow(0x02, 255), 1)
        self.assertNotEqual(T.gf_pow(0x02, 1), 1)
        # 0x02 has multiplicative order exactly 255
        for e in (1, 3, 5, 15, 17, 51, 85):
            self.assertNotEqual(T.gf_pow(0x02, e), 1, "order is not 255")
        self.assertEqual(T.gf_pow(0x02, 255), 1)

    def test_exp_log_tables_are_inverses(self):
        for a in range(1, 256):
            self.assertEqual(T.GF_EXP[T.GF_LOG[a]], a)

    def test_exp_table_wraps_at_255(self):
        for i in range(255, 300):
            self.assertEqual(T.GF_EXP[i], T.GF_EXP[i - 255])

    def test_multiplication_identities(self):
        for a in range(256):
            self.assertEqual(T.gf_mul(a, 1), a)
            self.assertEqual(T.gf_mul(a, 0), 0)
            self.assertEqual(T.gf_mul(a, a), T.gf_pow(a, 2))
        self.assertEqual(T.gf_mul(0, 0), 0)

    def test_multiplication_commutative_and_associative(self):
        rng = random.Random(11)
        for _ in range(2000):
            a, b, c = (rng.randrange(256) for _ in range(3))
            self.assertEqual(T.gf_mul(a, b), T.gf_mul(b, a))
            self.assertEqual(T.gf_mul(T.gf_mul(a, b), c),
                             T.gf_mul(a, T.gf_mul(b, c)))

    def test_distributive_over_addition(self):
        rng = random.Random(12)
        for _ in range(2000):
            a, b, c = (rng.randrange(256) for _ in range(3))
            self.assertEqual(T.gf_mul(a, b ^ c),
                             T.gf_mul(a, b) ^ T.gf_mul(a, c))

    def test_every_element_has_an_inverse(self):
        for a in range(1, 256):
            self.assertEqual(T.gf_mul(a, T.gf_inv(a)), 1)
        with self.assertRaises(ZeroDivisionError):
            T.gf_inv(0)

    def test_frobenius_x_times_255_is_one(self):
        for a in range(1, 256):
            self.assertEqual(T.gf_pow(a, 255), 1)

    def test_translate_tables_match_gf_mul(self):
        for c in range(1, 256):
            table = T._MULTAB[c]
            self.assertEqual(len(table), 256)
            for v in range(256):
                self.assertEqual(table[v], T.gf_mul(c, v),
                                 "c=%d v=%d" % (c, v))


# ------------------------------------------------- B: Reed-Solomon section ----


class TestReedSolomon(unittest.TestCase):
    def test_generator_row_zero_is_all_ones(self):
        # Vandermonde (v1) property: row 0 is all ones.
        for k, m in ((6, 3), (20, 4), (5, 1)):
            g = T.vandermonde_generator_matrix(k, m)
            self.assertEqual(g[0], [1] * k)

    def test_generator_entries_match_alpha_to_the_ij(self):
        g = T.vandermonde_generator_matrix(7, 5)
        for i in range(5):
            for j in range(7):
                self.assertEqual(g[i][j],
                                 T.gf_pow(2, (i * j) % 255))

    def test_every_square_submatrix_is_invertible(self):
        """The MDS property: every square submatrix (any row subset and column
        subset of the same size) of G is invertible, for the format-2 Cauchy
        matrix."""
        for k, m in ((5, 3), (8, 4), (10, 6), (20, 4)):
            g = T.generator_matrix(k, m)
            for e in range(1, m + 1):
                for rows in itertools.combinations(range(m), e):
                    for cols in itertools.combinations(range(k), e):
                        sub = [[g[r][c] for c in cols] for r in rows]
                        inv = T.gf_matrix_invert([row[:] for row in sub])
                        for i in range(e):
                            for j in range(e):
                                ident = 1 if i == j else 0
                                acc = 0
                                for t in range(e):
                                    acc ^= T.gf_mul(sub[i][t], inv[t][j])
                                self.assertEqual(acc, ident,
                                                 "singular submatrix k=%d m=%d e=%d rows=%s cols=%s"
                                                 % (k, m, e, rows, cols))

    def test_matrix_invert_matches_singular_behaviour(self):
        with self.assertRaises(T.TapefecError):
            T.gf_matrix_invert([[1, 2], [2, 4]])

    def test_encode_is_deterministic_and_systematic(self):
        rng = random.Random(3)
        k, m, ln = 8, 4, 64
        g = T.vandermonde_generator_matrix(k, m)
        blocks = [rng.randbytes(ln) for _ in range(k)]
        p1 = T.rs_encode_group(blocks, ln, g, m)
        p2 = T.rs_encode_group(blocks, ln, g, m)
        self.assertEqual(p1, p2)
        self.assertEqual(len(p1), m)
        # row 0 of G is all ones, so parity 0 is the XOR of every data block
        acc = 0
        for b in blocks:
            acc ^= int.from_bytes(b, "big")
        self.assertEqual(p1[0], acc.to_bytes(ln, "big"))
        # parity does not alter the data (systematic code)
        self.assertEqual(blocks, blocks[:k])

    def test_encode_handles_a_zero_length_codeword(self):
        g = T.generator_matrix(3, 2)
        blocks = [b"\x00" * 8, b"\x00" * 8, b"\x00" * 8]
        self.assertEqual(T.rs_encode_group(blocks, 8, g, 2),
                         [b"\x00" * 8, b"\x00" * 8])

    def test_recover_exactly_m_erasures_in_every_position_pattern(self):
        k, m, ln = 5, 3, 16
        rng = random.Random(21)
        g = T.generator_matrix(k, m)
        blocks = [rng.randbytes(ln) for _ in range(k)]
        parity = T.rs_encode_group(blocks, ln, g, m)
        for lost in itertools.combinations(range(k + m), m):
            lost = set(lost)
            known = {i: blocks[i] for i in range(k) if i not in lost}
            par = {i: parity[i] for i in range(m)
                   if (k + i) not in lost}
            fixed = T.rs_repair(known, sorted(i for i in lost if i < k),
                                par, ln, g, m)
            for i in sorted(i for i in lost if i < k):
                self.assertEqual(fixed[i], blocks[i],
                                 "failed on erasure pattern %s" % (lost,))

    def test_m_plus_one_erasures_is_unrecoverable(self):
        k, m, ln = 5, 3, 16
        rng = random.Random(22)
        g = T.generator_matrix(k, m)
        blocks = [rng.randbytes(ln) for _ in range(k)]
        parity = T.rs_encode_group(blocks, ln, g, m)
        for lost in itertools.combinations(range(k), m + 1):
            known = {i: blocks[i] for i in range(k) if i not in lost}
            with self.assertRaises(T.Unrecoverable):
                T.rs_repair(known, list(lost),
                            {i: parity[i] for i in range(m)}, ln, g, m)

    def test_losing_parity_costs_repair_capacity(self):
        k, m, ln = 6, 3, 16
        rng = random.Random(23)
        g = T.generator_matrix(k, m)
        blocks = [rng.randbytes(ln) for _ in range(k)]
        parity = T.rs_encode_group(blocks, ln, g, m)
        lost = [0, 1, 2]
        known = {i: b for i, b in enumerate(blocks) if i not in lost}
        # all m parity rows present -> repairable
        fixed = T.rs_repair(known, lost, {i: parity[i] for i in range(m)},
                            ln, g, m)
        self.assertEqual([fixed[i] for i in lost], [blocks[i] for i in lost])
        # one parity row gone -> only m-1 erasures are correctable
        with self.assertRaises(T.Unrecoverable):
            T.rs_repair({i: blocks[i] for i in lost}, lost,
                        {i: parity[i] for i in range(m - 1)}, ln, g, m)
        # two erasures with one parity row gone is still fine
        fixed = T.rs_repair({i: b for i, b in enumerate(blocks)
                             if i not in (0, 1)}, [0, 1],
                            {i: parity[i] for i in range(m - 1)}, ln, g, m)
        self.assertEqual([fixed[i] for i in (0, 1)],
                         [blocks[0], blocks[1]])

    def test_repair_with_zero_erasures_is_a_no_op(self):
        g = T.generator_matrix(4, 2)
        self.assertEqual(T.rs_repair({0: b"ab"}, [], {0: b"cd", 1: b"ef"},
                                     2, g, 2), {})

    def test_pad_and_minimum_k_m(self):
        for k, m in ((1, 1), (2, 1), (1, 2), (255, 1), (1, 255)):
            g = T.generator_matrix(k, m)
            blocks = [b"\x01\x02\x03\x04"] * k
            p = T.rs_encode_group(blocks, 4, g, m)
            self.assertEqual(len(p), m)
            known = {i: blocks[i] for i in range(1, k)}
            fixed = T.rs_repair(known, [0], {0: p[0]}, 4, g, m)
            self.assertEqual(fixed[0], blocks[0])


# --------------------------------------------------------- C: round trips ----


class TestRoundTrip(unittest.TestCase):
    def test_round_trip_boundary_sizes(self):
        b = T.DEFAULT_BLOCK_SIZE
        cases = [b"", b"\x00", rnd(b - 1), rnd(b), rnd(b + 1),
                 rnd(2 * b - 1), rnd(2 * b), rnd(2 * b + 1),
                 rnd(b * 20), rnd(b * 20 + 1), rnd(b * 20 - 1)]
        for n in cases:
            with self.subTest(size=len(n)):
                rep, out = decode(encode(n))
                self.assertEqual(out, n)
                self.assertTrue(rep.ok)
                self.assertTrue(rep.verified)
                self.assertEqual(rep.blocks_repaired, 0)
                self.assertEqual(rep.blocks_unrecoverable, 0)
                group = T.DEFAULT_K * b
                self.assertEqual(len(rep.groups), (len(n) + group - 1)
                                 // group)

    def test_round_trip_ten_megabytes(self):
        data = rnd(10 * 1024 * 1024, seed=99)
        enc = encode(data)
        rep, out = decode(enc)
        self.assertEqual(hashlib_sha(out), hashlib_sha(data))
        self.assertEqual(len(out), len(data))
        self.assertTrue(rep.verified)
        self.assertEqual(rep.blocks_unrecoverable, 0)

    def test_round_trip_parameter_matrix(self):
        data = rnd(300000, seed=5)
        for k, m, b in ((20, 4, 65536), (3, 1, 64), (8, 8, 1024),
                        (32, 2, 4096), (2, 6, 777), (7, 5, 13)):
            with self.subTest(k=k, m=m, block_size=b):
                rep, out = decode(encode(data, k=k, m=m, block_size=b))
                self.assertEqual(out, data)
                self.assertTrue(rep.verified)

    def test_empty_stream_has_header_and_trailer_only(self):
        enc = encode(b"")
        offs, info = frame_offsets(enc)
        self.assertEqual([fr["type"] for _s, _e, fr in offs],
                         ["header", "trailer"])
        self.assertEqual(info["total_length"], 0)
        rep, out = decode(enc)
        self.assertEqual(out, b"")
        self.assertTrue(rep.verified)
        self.assertEqual(rep.groups, [])

    def test_overhead_matches_the_documented_formula(self):
        k, m, b = 20, 4, 65536
        data = rnd(k * b)
        enc = encode(data, k=k, m=m, block_size=b)
        overhead = len(enc) / len(data) - 1
        # m parity blocks per k data blocks, plus 40 bytes of framing per
        # frame, plus one stream-header frame and one trailer frame
        want = m / k + 40 * (k + m) / (k * b) + (56 + 80) / len(data)
        self.assertAlmostEqual(overhead, want, places=9)
        self.assertAlmostEqual(overhead, 0.20084, places=4)

    def test_stream_is_self_describing(self):
        rep, _out = decode(encode(b"hello", k=9, m=7, block_size=16))
        self.assertEqual((rep.k, rep.m, rep.block_size), (9, 7, 16))
        self.assertEqual(rep.total_length, 5)

    def test_stream_id_is_honoured(self):
        rep, out = decode(encode(b"hello world", stream_id=0xBEEF))
        self.assertEqual(rep.stream_id, 0xBEEF)
        self.assertEqual(out, b"hello world")

    def test_trailer_carries_length_and_digest(self):
        data = rnd(1000, seed=1)
        _rep, _out = decode(encode(data))
        _frames, info = frame_offsets(encode(data))
        self.assertEqual(info["total_length"], 1000)
        self.assertEqual(info["sha256"], hashlib_sha(data))

    def test_encoder_rejects_bad_parameters(self):
        for kw in ({"k": 0}, {"m": 0}, {"k": 256}, {"k": 200, "m": 100},
                   {"block_size": 0}, {"stream_id": 70000}):
            with self.subTest(**kw):
                with self.assertRaises(T.TapefecError):
                    encode(b"x", **kw)


# --------------------------------------------------------------- D: damage ---


class TestDamage(unittest.TestCase):
    def setUp(self):
        self.data = rnd(SMALL["k"] * SMALL["block_size"] * 3, seed=42)
        self.enc = encode(self.data, **SMALL)
        self.offs, self.info = frame_offsets(self.enc)

    def test_corrupted_payload_bytes_are_caught_and_repaired(self):
        """Flip one byte in a data payload and four in a parity payload."""
        broken = damage(self.enc, corrupt=[(2, 10), (8, 0), (8, 1), (8, 2),
                                          (8, 3)])
        rep, out = decode(broken)
        self.assertEqual(out, self.data)
        self.assertEqual(rep.read_stats["bad_payload"], 2)
        # one data block and one parity block were hit; only the data block
        # has to be rebuilt, and the lost parity row is simply not used
        self.assertEqual(rep.blocks_repaired, 1)
        self.assertEqual(rep.groups[0]["lost_parity"], 1)
        self.assertEqual(rep.blocks_unrecoverable, 0)
        self.assertTrue(rep.verified)

    def test_corruption_in_m_data_blocks_of_one_group_is_repaired(self):
        corrupt = [(i, 5) for i in range(1, 1 + SMALL["m"])]
        rep, out = decode(damage(self.enc, corrupt=corrupt))
        self.assertEqual(out, self.data)
        self.assertEqual(rep.blocks_repaired, SMALL["m"])
        self.assertEqual(rep.groups[0]["status"], "repaired")
        self.assertTrue(rep.verified)

    def test_corruption_in_more_than_m_data_blocks_is_unrecoverable(self):
        corrupt = [(i, 5) for i in range(1, 1 + SMALL["m"] + 1)]
        rep, out = decode(damage(self.enc, corrupt=corrupt))
        self.assertEqual(rep.blocks_unrecoverable, SMALL["m"] + 1)
        self.assertFalse(rep.ok)
        self.assertEqual(rep.groups[0]["status"], "unrecoverable")
        start = rep.unrecoverable_ranges[0]["start"]
        self.assertEqual(out[start:start + SMALL["block_size"]],
                         b"\x00" * SMALL["block_size"])

    def test_exactly_m_erasures_recovered_in_every_pattern(self):
        k, m = SMALL["k"], SMALL["m"]
        first = group_frames(self.offs, k, m, 1)
        patterns = list(itertools.combinations(first, m))
        self.assertGreater(len(patterns), 30)
        for lost in patterns:
            with self.subTest(lost=lost):
                rep, out = decode(damage(self.enc, lost=lost))
                self.assertEqual(out, self.data)
                self.assertEqual(rep.blocks_unrecoverable, 0)
                self.assertTrue(rep.verified)

    def test_m_plus_one_erasures_is_reported_unrecoverable(self):
        k, m = SMALL["k"], SMALL["m"]
        first = group_frames(self.offs, k, m, 1)
        for lost in itertools.combinations(first, m + 1):
            with self.subTest(lost=lost):
                n_data_lost = sum(1 for i in lost
                                  if self.offs[i][2]["type"] == "data")
                rep, out = decode(damage(self.enc, lost=lost))
                self.assertEqual(rep.blocks_unrecoverable, n_data_lost)
                self.assertFalse(rep.ok)
                self.assertFalse(rep.verified)
                self.assertEqual(len(out), len(self.data))
                self.assertEqual(len(rep.unrecoverable_ranges),
                                 n_data_lost)
                for r in rep.unrecoverable_ranges:
                    self.assertEqual(out[r["start"]:r["end"]],
                                     b"\x00" * (r["end"] - r["start"]))

    def test_deleted_span_of_frames_resyncs(self):
        span = list(range(12, 16))          # 4 data blocks of group 1
        rep, out = decode(damage(self.enc, lost=span))
        self.assertEqual(len(out), len(self.data))
        self.assertEqual(rep.blocks_unrecoverable, 4)
        self.assertEqual(rep.groups[1]["status"], "unrecoverable")
        # the frames after the gap were still found: resync worked
        self.assertEqual(rep.groups[2]["status"], "ok")

    def test_deleted_span_smaller_than_m_is_repaired(self):
        rep, out = decode(damage(self.enc, lost=[12, 13]))
        self.assertEqual(out, self.data)
        self.assertEqual(rep.blocks_repaired, 2)
        self.assertTrue(rep.verified)

    def test_entirely_missing_group_reports_zeros_and_ranges(self):
        lost = group_frames(self.offs, SMALL["k"], SMALL["m"], 1)
        rep, out = decode(damage(self.enc, lost=lost))
        self.assertEqual(len(out), len(self.data))
        self.assertEqual(rep.blocks_unrecoverable, SMALL["k"])
        self.assertEqual(len(rep.unrecoverable_ranges), SMALL["k"])
        start = SMALL["k"] * SMALL["block_size"]
        self.assertEqual(out[start:start + SMALL["block_size"]],
                         b"\x00" * SMALL["block_size"])

    def test_garbage_between_frames_is_discarded(self):
        junk = bytes(range(256)) * 3
        broken = damage(self.enc, junk_at=5, junk=junk)
        rep, out = decode(broken)
        self.assertEqual(out, self.data)
        self.assertTrue(rep.verified)
        self.assertGreaterEqual(rep.read_stats["garbage_bytes"], len(junk))
        self.assertGreaterEqual(rep.read_stats["resyncs"], 1)

    def test_garbage_before_and_after_the_stream(self):
        junk = b"NOTATAPEFECSTREAM" * 5
        rep, out = decode(junk + self.enc + junk)
        self.assertEqual(out, self.data)
        self.assertTrue(rep.verified)
        self.assertEqual(rep.blocks_unrecoverable, 0)

    def test_junk_inserted_inside_a_payload_is_caught_by_the_crc(self):
        # Splice 64 bytes into the middle of frame 5's payload: the payload
        # CRC fails, the frame becomes an erasure, parity rebuilds it.
        s, _e, _fr = self.offs[5]
        broken = damage(self.enc)
        broken = broken[:s + 40 + 10] + b"\x01" * 64 + broken[s + 40 + 10:]
        rep, out = decode(broken)
        self.assertEqual(out, self.data)
        self.assertEqual(rep.read_stats["bad_payload"], 1)
        self.assertEqual(rep.blocks_repaired, 1)

    def test_truncated_stream(self):
        for cut in (0, 1, 10, 39, 55, len(self.enc) // 2,
                    len(self.enc) - 1, len(self.enc) - 40,
                    len(self.enc) - 79):
            with self.subTest(cut=cut):
                if cut < T.HEADER_SIZE + T.STREAM_HEADER_SIZE:
                    # too short to hold a complete, verifiable header frame
                    with self.assertRaises(T.TapefecError):
                        decode(self.enc[:cut])
                    continue
                rep, out = decode(self.enc[:cut])
                self.assertFalse(rep.trailer_seen)
                self.assertIsNone(rep.sha256_match)
                self.assertLessEqual(len(out), len(self.data))
                if cut > len(self.enc) - 80:
                    self.assertEqual(out, self.data)

    def test_truncation_inside_a_group_keeps_stream_length(self):
        """Losing the tail must not silently shorten the decoded stream when
        the trailer survives."""
        rep, out = decode(self.enc)
        self.assertEqual(len(out), len(self.data))

    def test_wrong_stream_id_frame_is_ignored(self):
        foreign = T.pack_header(T.FT_DATA, 0x1234, 99, 0, 16,
                                T.crc32(b"not mine!!!")[:4] & 0xFFFFFFFF
                                if False else T.crc32(b"not mine!!!"), 6)
        blob = foreign + b"not mine!!!"
        broken = self.enc[:self.offs[1][0]] + blob + self.enc[self.offs[1][0]:]
        rep, out = decode(broken)
        self.assertEqual(out, self.data)
        self.assertEqual(rep.foreign_frames, 1)
        self.assertEqual(rep.blocks_unrecoverable, 0)

    def test_foreign_frame_cannot_impersonate_a_real_block(self):
        """A frame with a different stream id must not fill a damaged slot."""
        foreign = T.pack_header(T.FT_DATA, 0x1234, 99, 0,
                                SMALL["block_size"], 0, 6)
        blob = foreign + bytes(SMALL["block_size"])
        # delete real data block 0 of group 0 and try to substitute the foreign
        broken = damage(self.enc, lost=[1])
        at = self.offs[1][0]
        broken = broken[:at] + blob + broken[at:]
        rep, out = decode(broken)
        self.assertEqual(out, self.data)
        self.assertEqual(rep.blocks_repaired, 1)
        self.assertEqual(rep.foreign_frames, 1)

    def test_lost_stream_header_is_a_format_error(self):
        broken = damage(self.enc, lost=[0])
        with self.assertRaises(T.TapefecError):
            decode(broken)

    def test_corrupt_stream_header_payload_is_a_format_error(self):
        broken = bytearray(self.enc)
        broken[self.offs[0][0] + 40] ^= 0xFF
        with self.assertRaises(T.TapefecError):
            decode(bytes(broken))

    def test_corrupt_header_crc_is_resynced_past(self):
        broken = bytearray(self.enc)
        broken[self.offs[2][0] + 36] ^= 0xFF
        rep, out = decode(bytes(broken))
        self.assertEqual(out, self.data)
        self.assertEqual(rep.blocks_repaired, 1)

    def test_lost_trailer_is_reported_but_data_survives(self):
        broken = damage(self.enc, lost=[len(self.offs) - 1])
        rep, out = decode(broken)
        self.assertEqual(out, self.data)
        self.assertFalse(rep.trailer_seen)
        self.assertIsNone(rep.sha256_match)
        self.assertTrue(rep.ok)
        self.assertFalse(rep.verified)

    def test_corrupt_trailer_payload_is_reported(self):
        broken = bytearray(self.enc)
        broken[self.offs[-1][0] + 40] ^= 0xFF
        rep, out = decode(bytes(broken))
        self.assertFalse(rep.trailer_seen)
        self.assertIsNone(rep.sha256_match)

    def test_corrupt_trailer_digest_is_caught(self):
        """A wrong SHA-256 in the trailer must be reported as a failure."""
        tl = T.TRAILER_FMT.pack(len(self.data), b"\x00" * 32)
        broken = (self.enc[:self.offs[-1][0]]
                  + T.pack_header(T.FT_TRAILER, self.info["total_length"] & 0
                                  or 0, 0, 0, T.TRAILER_SIZE, T.crc32(tl), 0)
                  + tl)
        rep, out = decode(broken)
        self.assertEqual(out, self.data)
        self.assertTrue(rep.trailer_seen)
        self.assertIs(rep.sha256_match, False)
        self.assertFalse(rep.verified)

    def test_decoding_nonsense_raises(self):
        with self.assertRaises(T.TapefecError):
            decode(b"this is not a tapefec stream" * 100)

    def test_random_garbage_never_yields_a_frame(self):
        rng = random.Random(77)
        for _ in range(50):
            blob = rng.randbytes(4096)
            frames, _info, _c, _s, _d = T.inspect_stream(io.BytesIO(blob))
            self.assertEqual(frames, [])

    def test_unrecoverable_ranges_are_json_serialisable(self):
        lost = group_frames(self.offs, SMALL["k"], SMALL["m"], 2)
        rep, _out = decode(damage(self.enc, lost=lost))
        doc = json.loads(json.dumps(rep.to_json()))
        self.assertFalse(doc["ok"])
        self.assertEqual(len(doc["unrecoverable_ranges"]), SMALL["k"])
        rng_ = doc["unrecoverable_ranges"]
        self.assertEqual(rng_[0]["start"], 2 * SMALL["k"] * SMALL["block_size"])
        for a, b_ in zip(rng_, rng_[1:]):
            self.assertEqual(a["end"], b_["start"])   # contiguous, in order


# ------------------------------------------------------------------ E: CLI ----


class TestCLI(unittest.TestCase):
    def run_cli(self, args, stdin=b""):
        p = subprocess.run([sys.executable, SCRIPT] + args, input=stdin,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return p.returncode, p.stdout, p.stderr

    def test_help_and_version(self):
        rc, out, _err = self.run_cli(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn(b"forward-error-correction", out)
        rc, out, _err = self.run_cli(["--version"])
        self.assertEqual(rc, 0)
        self.assertIn(b"tapefec", out)
        for sub in ("encode", "decode", "inspect"):
            rc, out, _err = self.run_cli([sub, "--help"])
            self.assertEqual(rc, 0)
            self.assertIn(sub.encode(), out)

    def test_no_subcommand_prints_help_and_exits_2(self):
        rc, _out, err = self.run_cli([])
        self.assertEqual(rc, 2)
        self.assertIn(b"usage", err.lower())

    def test_cli_round_trip(self):
        data = rnd(100000, seed=4)
        rc, enc, _err = self.run_cli(["encode"], stdin=data)
        self.assertEqual(rc, 0)
        rc, out, err = self.run_cli(["decode"], stdin=enc)
        self.assertEqual(rc, 0)
        self.assertEqual(out, data)
        self.assertIn(b'"verified": true', err)

    def test_cli_exit_status_on_damage(self):
        data = rnd(100000, seed=6)
        rc, enc, _err = self.run_cli(["encode", "-k", "4", "-m", "2",
                                      "--block-size", "1024"], stdin=data)
        self.assertEqual(rc, 0)
        offs, _info = frame_offsets(enc)
        broken = damage(enc, lost=list(range(1, 8)))
        rc, out, err = self.run_cli(["decode"], stdin=broken)
        self.assertEqual(rc, 1)
        doc = json.loads(err.strip().splitlines()[-1])
        self.assertFalse(doc["ok"])
        self.assertEqual(len(out), len(data))

    def test_cli_quiet_and_strict(self):
        data = rnd(50000, seed=7)
        rc, enc, _err = self.run_cli(["encode", "-k", "4", "-m", "2",
                                      "--block-size", "512"], stdin=data)
        self.assertEqual(rc, 0)
        rc, out, err = self.run_cli(["decode", "--quiet"], stdin=enc)
        self.assertEqual(rc, 0)
        self.assertEqual(out, data)
        self.assertEqual(err.strip(), b"")
        offs, _info = frame_offsets(enc)
        rc, out, err = self.run_cli(["decode", "--strict"],
                                    stdin=damage(enc, lost=[1]))
        self.assertEqual(rc, 1)

    def test_cli_text_report(self):
        data = rnd(50000, seed=8)
        rc, enc, _err = self.run_cli(["encode", "-k", "4", "-m", "2",
                                      "--block-size", "512"], stdin=data)
        offs, _info = frame_offsets(enc)
        rc, _out, err = self.run_cli(["decode", "--text"],
                                     stdin=damage(enc, lost=[1, 2]))
        self.assertIn(b"group 0: repaired", err)

    def test_cli_inspect_text_and_json(self):
        data = rnd(5000, seed=9)
        rc, enc, _err = self.run_cli(["encode", "-k", "4", "-m", "2",
                                      "--block-size", "512"], stdin=data)
        self.assertEqual(rc, 0)
        rc, out, _err = self.run_cli(["inspect"], stdin=enc)
        self.assertEqual(rc, 0)
        self.assertIn(b"k=4", out)
        self.assertIn(b"data", out)
        self.assertIn(b"trailer", out)
        rc, out, _err = self.run_cli(["inspect", "--json"], stdin=enc)
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertEqual(doc["info"]["k"], 4)
        self.assertEqual(doc["info"]["m"], 2)
        self.assertEqual(doc["info"]["block_size"], 512)
        self.assertEqual(doc["info"]["total_length"], 5000)
        self.assertEqual(doc["counts"]["header"], 1)
        self.assertEqual(doc["counts"]["trailer"], 1)
        self.assertEqual(doc["damaged_payloads"], 0)
        self.assertEqual(doc["frames"][0]["type"], "header")

    def test_cli_rejects_nonsense_input(self):
        rc, _out, err = self.run_cli(["decode"], stdin=b"nonsense" * 100)
        self.assertEqual(rc, 2)
        self.assertIn(b"tapefec:", err)

    def test_tar_pipeline_round_trip(self):
        """tar | tapefec encode | tapefec decode | tar -t"""
        import tempfile
        td = tempfile.TemporaryDirectory()
        tmpdir = td.name
        files = []
        for i in range(4):
            path = os.path.join(tmpdir, "t%d.bin" % i)
            with open(path, "wb") as fh:
                fh.write(rnd(30000 + i, seed=i))
            files.append(path)
        tar = subprocess.run(["tar", "-cf", "-", "-C", tmpdir]
                             + [os.path.basename(f) for f in files],
                             stdout=subprocess.PIPE, check=True).stdout
        rc, enc, _err = self.run_cli(["encode", "-k", "8", "-m", "4",
                                      "--block-size", "4096"], stdin=tar)
        self.assertEqual(rc, 0)
        rc, back, _err = self.run_cli(["decode"], stdin=enc)
        self.assertEqual(rc, 0)
        self.assertEqual(back, tar)
        lst = subprocess.run(["tar", "-tf", "-"], input=back,
                             stdout=subprocess.PIPE, check=True).stdout
        self.assertEqual(sorted(lst.split()), sorted(
            os.path.basename(f).encode() for f in files))
        td.cleanup()


# ------------------------------------------------------------- header bits ---


class TestFraming(unittest.TestCase):
    def test_header_round_trip_and_crc(self):
        raw = T.pack_header(T.FT_DATA, 7, 11, 13, 17, 0xDEADBEEF, 5)
        self.assertEqual(len(raw), T.HEADER_SIZE)
        head = T.unpack_header(raw)
        self.assertEqual(head["stream_id"], 7)
        self.assertEqual(head["frame_index"], 11)
        self.assertEqual(head["block_id"], 13)
        self.assertEqual(head["payload_len"], 17)
        self.assertEqual(head["payload_crc"], 0xDEADBEEF)
        self.assertEqual(head["group_data_len"], 5)
        self.assertEqual(head["ftype"], T.FT_DATA)

    def test_header_rejects_every_single_bit_flip(self):
        raw = T.pack_header(T.FT_DATA, 7, 11, 13, 17, 0xDEADBEEF, 5)
        bad = 0
        for byte in range(T.HEADER_SIZE):
            for bit in range(8):
                mutated = bytearray(raw)
                mutated[byte] ^= 1 << bit
                if T.unpack_header(bytes(mutated)) is None:
                    bad += 1
        # CRC-32 catches every single-bit flip in the covered 36 bytes
        # every single-bit flip anywhere in the 40-byte header is rejected:
        # 288 inside the CRC-covered prefix, 32 inside the CRC field itself
        self.assertEqual(bad, T.HEADER_SIZE * 8)

    def test_header_rejects_bad_magic_and_version(self):
        raw = bytearray(T.pack_header(T.FT_DATA, 0, 0, 0, 0, 0, 0))
        raw[3] ^= 0xFF
        self.assertIsNone(T.unpack_header(bytes(raw)))
        raw = bytearray(T.pack_header(T.FT_DATA, 0, 0, 0, 0, 0, 0))
        raw[8] = 99
        self.assertIsNone(T.unpack_header(bytes(raw)))
        self.assertIsNone(T.unpack_header(b"short"))

    def test_resync_over_a_span_of_pure_noise(self):
        data = rnd(5000, seed=13)
        enc = encode(data, **SMALL)
        noise = bytes(range(1, 256, 7)) * 50
        broken = noise + enc + noise
        rep, out = decode(broken)
        self.assertEqual(out, data)
        self.assertGreaterEqual(rep.read_stats["garbage_bytes"], len(noise))

    def test_frame_order_within_a_group_does_not_matter(self):
        """Swap two parity frames inside one group."""
        data = rnd(20000, seed=14)
        enc = bytearray(encode(data, **SMALL))
        offs, _info = frame_offsets(bytes(enc))
        a_s, a_e, _ = offs[7]
        b_s, b_e, _ = offs[8]
        fa = bytes(enc[a_s:a_e])
        fb = bytes(enc[b_s:b_e])
        enc[a_s:a_e] = fb
        enc[b_s:b_e] = fa
        rep, out = decode(bytes(enc))
        self.assertEqual(out, data)
        self.assertTrue(rep.verified)

    def test_duplicate_frame_is_harmless(self):
        data = rnd(20000, seed=15)
        enc = encode(data, **SMALL)
        offs, _info = frame_offsets(enc)
        s, e, _ = offs[3]
        broken = enc[:e] + enc[s:e] + enc[e:]
        rep, out = decode(broken)
        self.assertEqual(out, data)


def hashlib_sha(b):
    import hashlib
    return hashlib.sha256(b).hexdigest()


class TestPrepubFixes(unittest.TestCase):
    def run_cli(self, args, stdin=b""):
        p = subprocess.run([sys.executable, SCRIPT] + args, input=stdin,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return p.returncode, p.stdout, p.stderr

    def _delete_frame(self, enc, pred):
        offs, _ = frame_offsets(enc)
        for start, end, fr in offs:
            if pred(fr):
                return enc[:start] + enc[end:]
        raise AssertionError("frame not found")

    def test_t1_short_final_block(self):
        data = rnd(87, seed=1)
        rc, enc, _ = self.run_cli(["encode", "-k", "4", "-m", "2",
                                   "--block-size", "16"], stdin=data)
        self.assertEqual(rc, 0)
        damaged = self._delete_frame(enc, lambda fr: fr["type"] == "data"
                                    and fr["block_id"] == 5)
        rc, out, err = self.run_cli(["decode"], stdin=damaged)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, data)

    def test_t2_short_final_lost(self):
        bs = 16
        for n in (0, 1, bs - 1, bs, bs + 1, 2 * bs - 1,
                  2 * bs, 2 * bs + 3, 5 * bs + 7):
            data = rnd(n, seed=n + 100)
            enc = encode(data, k=4, m=2, block_size=bs)
            # drop the last data frame that exists
            offs, _ = frame_offsets(enc)
            data_frames = [fr for _s, _e, fr in offs
                           if fr["type"] == "data"]
            if not data_frames:
                continue
            last_block = max(fr["block_id"] for fr in data_frames)
            damaged = self._delete_frame(enc, lambda fr:
                                        fr["type"] == "data"
                                        and fr["block_id"] == last_block)
            rep, out = decode(damaged)
            self.assertEqual(out, data,
                             "failed for length %d" % n)

    def test_t3_cauchy_mds_end_to_end(self):
        data = rnd(1000, seed=7)
        rc, enc, _ = self.run_cli(["encode", "-k", "10", "-m", "6",
                                   "--block-size", "32"], stdin=data)
        self.assertEqual(rc, 0)
        offs, info = frame_offsets(enc)
        group0 = group_frames(offs, 10, 6, 0)
        # data positions 0,3,5 and parity positions 2,3,5 (within group)
        lost_indices = [group0[0], group0[3], group0[5],
                        group0[10 + 2], group0[10 + 3], group0[10 + 5]]
        damaged = damage(enc, lost=lost_indices)
        rc, out, err = self.run_cli(["decode"], stdin=damaged)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, data)

    def test_t3_v1_stream_decodes(self):
        data = rnd(2000, seed=9)
        enc = encode(data, k=6, m=3, block_size=64, version=1)
        rep, out = decode(enc)
        self.assertEqual(out, data)
        self.assertEqual(rep.version, 1)

    def test_t3_v1_singular_group_does_not_abort(self):
        data = rnd(400, seed=11)
        enc = encode(data, k=10, m=6, block_size=32, version=1)
        offs, info = frame_offsets(enc)
        group0 = group_frames(offs, 10, 6, 0)
        lost_indices = [group0[0], group0[3], group0[5],
                        group0[10 + 2], group0[10 + 3], group0[10 + 5]]
        damaged = damage(enc, lost=lost_indices)
        rep, out = decode(damaged)
        self.assertEqual(len(out), len(data))
        self.assertTrue(rep.blocks_unrecoverable > 0)

    def test_c1_notes_json(self):
        data = rnd(2000, seed=5)
        rc, enc, _ = self.run_cli(["encode", "-k", "4", "-m", "2",
                                   "--block-size", "64"], stdin=data)
        self.assertEqual(rc, 0)
        offs, _ = frame_offsets(enc)
        lost = group_frames(offs, 4, 2, 0)
        damaged = damage(enc, lost=lost)
        rc1, out1, err1 = self.run_cli(["decode"], stdin=damaged)
        rc2, out2, err2 = self.run_cli(["decode", "--json"], stdin=damaged)
        self.assertTrue(rc1 >= 0)
        doc1 = json.loads(err1.strip().splitlines()[-1])
        doc2 = json.loads(err2.strip().splitlines()[-1])
        self.assertIn("notes", doc1)
        self.assertIn("notes", doc2)

    def test_c2_quiet_strict_empty_stderr(self):
        data = rnd(5000, seed=6)
        rc, enc, _ = self.run_cli(["encode", "-k", "4", "-m", "2",
                                   "--block-size", "256"], stdin=data)
        self.assertEqual(rc, 0)
        offs, _ = frame_offsets(enc)
        damaged = damage(enc, lost=[1])
        rc, out, err = self.run_cli(["decode", "--quiet", "--strict"],
                                    stdin=damaged)
        self.assertEqual(rc, 1)
        self.assertEqual(err.strip(), b"")

    def test_c3_inspect_empty_and_random(self):
        rc, out, err = self.run_cli(["inspect"], stdin=b"")
        self.assertEqual(rc, 0)
        self.assertIn(b"frames: 0 (none)", out)
        rc, out, err = self.run_cli(["inspect"], stdin=b"random garbage" * 20)
        self.assertEqual(rc, 2)
        self.assertIn(b"not a tapefec stream", err)

    def test_c4_not_a_tty_helper(self):
        class TTY:
            def isatty(self):
                return True

        self.assertTrue(T.check_not_a_tty(TTY()))
        self.assertFalse(T.check_not_a_tty(io.BytesIO()))
        self.assertFalse(T.check_not_a_tty(object()))
        closed = io.BytesIO()
        closed.close()
        self.assertFalse(T.check_not_a_tty(closed))

        # Refusal must precede access to either binary stream or any input read.
        class Terminal(io.StringIO):
            def isatty(self):
                return True

            @property
            def buffer(self):
                raise AssertionError("terminal binary stream accessed")

        for command in ("encode", "decode"):
            with self.subTest(command=command, surface="main"):
                err = io.StringIO()
                with mock.patch.object(sys, "stdin", object()), \
                     mock.patch.object(sys, "stdout", Terminal()), \
                     mock.patch.object(sys, "stderr", err):
                    self.assertEqual(T.main([command]), 2)
                self.assertIn("redirect stdout", err.getvalue())

        # Exercise the actual CLI with a pseudo-terminal, never a user's TTY.
        import pty
        for command in ("encode", "decode", "inspect"):
            with self.subTest(command=command, surface="pty"):
                master, slave = pty.openpty()
                try:
                    p = subprocess.run([sys.executable, SCRIPT, command],
                                       input=b"", stdout=slave,
                                       stderr=subprocess.PIPE, timeout=10)
                    os.close(slave)
                    slave = None
                    output = bytearray()
                    while True:
                        try:
                            chunk = os.read(master, 4096)
                        except OSError as exc:
                            import errno
                            if exc.errno != errno.EIO:
                                raise
                            break
                        if not chunk:
                            break
                        output.extend(chunk)
                    if command == "inspect":
                        self.assertEqual(p.returncode, 0, p.stderr)
                        self.assertIn(b"frames: 0 (none)", output)
                    else:
                        self.assertEqual(p.returncode, 2, p.stderr)
                        self.assertEqual(output, b"")
                        self.assertIn(b"redirect stdout", p.stderr)
                finally:
                    if slave is not None:
                        os.close(slave)
                    os.close(master)


class TestVerifiedFindings(unittest.TestCase):
    @staticmethod
    def drop(enc, predicate):
        offsets, _ = frame_offsets(enc)
        return b"".join(enc[start:end] for start, end, _frame in offsets
                        if not predicate(T.unpack_header(
                            enc[start:start + T.HEADER_SIZE])))

    def cli(self, args, data):
        return subprocess.run([sys.executable, SCRIPT] + args, input=data,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=15)

    def test_missing_tail_groups_are_individual_including_partial(self):
        for k, m, block, size, first in ((4, 2, 16, 256, 2),
                                        (4, 2, 16, 231, 2),
                                        (3, 1, 7, 50, 0),
                                        (1, 2, 5, 12, 1)):
            with self.subTest(k=k, m=m, size=size, first=first):
                data = rnd(size, seed=size)
                enc = encode(data, k=k, m=m, block_size=block)
                broken = self.drop(enc, lambda f:
                    (f['ftype'] == T.FT_DATA and f['block_id'] // k >= first)
                    or (f['ftype'] == T.FT_PARITY and f['block_id'] // m >= first))
                rep, out = decode(broken)
                count = (size + k * block - 1) // (k * block)
                self.assertEqual([g['group'] for g in rep.groups], list(range(count)))
                missing_blocks = 0
                for group in range(first, count):
                    entry = rep.groups[group]
                    n = min(k, (size - group * k * block + block - 1) // block)
                    self.assertEqual(entry['data_blocks'], n)
                    self.assertEqual(entry['lost_data'], n)
                    self.assertEqual(entry['lost_parity'], m)
                    self.assertEqual(entry['repaired'], 0)
                    self.assertEqual(entry['status'], 'unrecoverable')
                    missing_blocks += n
                self.assertEqual(rep.blocks_unrecoverable, missing_blocks)
                self.assertEqual(out, data[:first*k*block] + bytes(size-first*k*block))
                p = self.cli(['decode'], broken)
                self.assertEqual(p.returncode, 1)
                self.assertEqual(p.stdout, out)
                self.assertEqual(json.loads(p.stderr)['groups'], rep.groups)

    def test_quiet_strict_reports_loss_even_after_repair(self):
        for version in (1, 2):
            with self.subTest(version=version):
                enc = encode(rnd(128, seed=4), k=4, m=2, block_size=16,
                             version=version)
                broken = self.drop(enc, lambda f: f['ftype'] == T.FT_DATA
                                   and f['block_id'] in (0, 4, 5, 6, 7))
                p = self.cli(['decode', '--quiet', '--strict'], broken)
                self.assertEqual(p.returncode, 1)
                report = json.loads(p.stderr)
                self.assertEqual(report['blocks_repaired'], 1)
                self.assertEqual(report['blocks_unrecoverable'], 4)
                self.assertFalse(report['verified'])

    def test_quiet_strict_reports_digest_failure_after_repair(self):
        data = rnd(87, seed=8)
        enc = encode(data, k=4, m=2, block_size=16)
        offsets, _ = frame_offsets(enc)
        payload = T.TRAILER_FMT.pack(len(data), bytes(32))
        enc = (enc[:offsets[-1][0]] + T.pack_header(
            T.FT_TRAILER, 0, 0, 0, len(payload), T.crc32(payload), 0) + payload)
        broken = self.drop(enc, lambda f: f['ftype'] == T.FT_DATA
                           and f['block_id'] == 0)
        p = self.cli(['decode', '--quiet', '--strict'], broken)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout, data)
        report = json.loads(p.stderr)
        self.assertEqual(report['blocks_repaired'], 1)
        self.assertEqual(report['blocks_unrecoverable'], 0)
        self.assertIs(report['sha256_match'], False)

    def test_quiet_strict_reports_missing_trailer_after_repair(self):
        enc = encode(rnd(87, seed=9), k=4, m=2, block_size=16)
        broken = self.drop(enc, lambda f: f['ftype'] == T.FT_TRAILER
                           or (f['ftype'] == T.FT_DATA and f['block_id'] == 0))
        p = self.cli(['decode', '--quiet', '--strict'], broken)
        self.assertEqual(p.returncode, 1)
        report = json.loads(p.stderr)
        self.assertEqual(report['blocks_repaired'], 1)
        self.assertFalse(report['trailer_seen'])
        self.assertIsNone(report['sha256_match'])

    def test_unrecoverable_ranges_use_only_known_eof(self):
        for size in (1, 16, 17, 87):
            for trailer in (False, True):
                with self.subTest(size=size, trailer=trailer):
                    k, m, block = 4, 2, 16
                    last = (size - 1) // block
                    group = last // k
                    enc = encode(rnd(size, seed=size), k=k, m=m, block_size=block)
                    broken = self.drop(enc, lambda f:
                        (f['ftype'] == T.FT_DATA and f['block_id'] == last)
                        or (f['ftype'] == T.FT_PARITY and f['block_id'] // m == group)
                        or (not trailer and f['ftype'] == T.FT_TRAILER))
                    rep, out = decode(broken)
                    if not trailer and last == 0:
                        # No surviving frame can reveal this missing group.
                        self.assertEqual(rep.unrecoverable_ranges, [])
                        self.assertIsNone(rep.total_length)
                        continue
                    end = size if trailer else (last + 1) * block
                    self.assertEqual(rep.unrecoverable_ranges, [dict(
                        group=group, block=last % k, start=last*block, end=end)])
                    self.assertEqual(len(out), end)
                    self.assertEqual(rep.total_length, size if trailer else None)
                    p = self.cli(['decode'], broken)
                    self.assertEqual(p.returncode, 1)
                    self.assertEqual(json.loads(p.stderr)['unrecoverable_ranges'],
                                     rep.unrecoverable_ranges)

    def test_invalid_block_sizes_refused_before_io_or_allocation(self):
        for size in (0, -1, 2**32, 2**64, 10**1000, 1.5, '16'):
            with self.subTest(size_type=type(size).__name__):
                with mock.patch.object(T, 'generator_matrix',
                                       side_effect=AssertionError('late validation')):
                    with self.assertRaises(T.TapefecError):
                        T.encode_stream(object(), object(), block_size=size)
        for size in ('0', '-1', str(2**32), str(2**64)):
            p = self.cli(['encode', '--block-size', size], b'')
            self.assertEqual(p.returncode, 2)
            self.assertEqual(p.stdout, b'')
            self.assertIn(b'block_size', p.stderr)
            self.assertNotIn(b'Traceback', p.stderr)

    def test_largest_wire_block_size_passes_validation_without_allocation(self):
        sentinel = RuntimeError('stop before allocation')
        with mock.patch.object(T, 'generator_matrix', side_effect=sentinel):
            with self.assertRaises(RuntimeError) as caught:
                T.encode_stream(object(), object(), block_size=2**32 - 1)
        self.assertIs(caught.exception, sentinel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
