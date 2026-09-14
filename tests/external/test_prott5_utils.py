"""Tests for ProtT5Utils.

Two tiers:

- **Fast, no real model (always run).** Tokenisation correctness (UZOB mapping
  and residue space-separation) is asserted on the tokenizer input. Batching by
  residue count is asserted on the pure batcher. The critical PADDING test — the
  one that catches mean-pooling over padding — is run against a *fake* encoder
  whose per-residue outputs are deterministic and whose padded positions are
  filled with a huge sentinel value. Any pooling that includes padding produces a
  wildly different vector for the short sequence when batched next to a long one,
  so this fast test catches the classic bug without downloading multi-GB weights.

- **Slow, real model (opt-in).** The same padding assertion against the actual
  ``Rostlab/prot_t5_xl_half_uniref50-enc`` encoder, marked ``prott5_model`` +
  ``slow`` and skipped unless ``PROTT5_LIVE_TESTS=1``.

The padding test is therefore never skipped "in spirit": its logic runs in every
CI run via the fake encoder, and the real-model form is available on demand.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict

import numpy as np
import pytest

from kbutillib.domains.ai.prott5_utils import (
    EMBEDDING_DIM,
    ProtT5Utils,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def utils():
    """ProtT5Utils on CPU, without touching config/tokens or loading the model."""
    return ProtT5Utils(
        device="cpu",
        config_file=False,
        token_file=None,
        kbase_token_file=None,
    )


# A short sequence and a much longer one — the setup that surfaces padding bugs.
SHORT_SEQ = "MKV"
LONG_SEQ = "M" + "AKLVGST" * 20  # 141 residues, >> SHORT_SEQ


# ---------------------------------------------------------------------------
# Tokenisation: UZOB mapping + space-separation (no model needed)
# ---------------------------------------------------------------------------


class TestCleanSequence:
    """Tests for ProtT5Utils._clean_sequence (pure tokenisation prep)."""

    def test_residues_are_space_separated(self):
        assert ProtT5Utils._clean_sequence("MKV") == "M K V"

    def test_uzob_mapped_to_x(self):
        # U, Z, O, B must all become X.
        assert ProtT5Utils._clean_sequence("UZOB") == "X X X X"

    def test_uzob_mapped_within_sequence(self):
        assert ProtT5Utils._clean_sequence("MUKZOB") == "M X K X X X"

    def test_lowercase_is_uppercased(self):
        assert ProtT5Utils._clean_sequence("mkv") == "M K V"

    def test_internal_whitespace_stripped_before_separation(self):
        # A caller that pre-spaced or included newlines still yields exactly
        # single-spaced residues.
        assert ProtT5Utils._clean_sequence("M K\nV") == "M K V"

    def test_ordinary_residues_untouched(self):
        # A residue like X, or the 20 standard residues, are not remapped.
        assert ProtT5Utils._clean_sequence("ACDEFGHIKLMNPQRSTVWY") == " ".join(
            "ACDEFGHIKLMNPQRSTVWY"
        )


class TestTokenizerInput:
    """Assert what actually reaches the tokenizer, via a mocked forward pass."""

    def test_tokenizer_receives_space_separated_uzob_mapped(self, utils):
        # Build a fake tokenizer + model so no weights are loaded, and capture
        # the exact strings handed to batch_encode_plus.
        captured: Dict[str, Any] = {}

        fake_encoded = {
            "input_ids": _torch_zeros(1, 4),
            "attention_mask": _torch_ones(1, 4),
        }

        def _batch_encode_plus(seqs, **kwargs):
            captured["seqs"] = seqs
            return _FakeBatchEncoding(fake_encoded)

        utils._tokenizer = SimpleNamespace(batch_encode_plus=_batch_encode_plus)
        utils._device = "cpu"
        # 3 residues -> 3 real positions + 1 EOS = seq_len 4.
        utils._model = _make_fake_model(seq_len=4, hidden_dim=EMBEDDING_DIM)

        utils.embed_proteins({"p": "MUB"})

        # U and B -> X, single-spaced.
        assert captured["seqs"] == ["M X X"]


# ---------------------------------------------------------------------------
# Batching by residue count (pure)
# ---------------------------------------------------------------------------


class TestMakeBatches:
    """Tests for ProtT5Utils._make_batches (residue-count packing)."""

    def test_batches_bounded_by_residue_count_not_sequence_count(self):
        u = ProtT5Utils(
            device="cpu",
            max_batch_residues=10,
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        # cleaned sequences: residue count == number of space-separated tokens
        items = [
            ("a", "M K V"),        # 3
            ("b", "M K V L E"),    # 5
            ("c", "M K"),          # 2  -> a+b+c = 10, fits one batch
            ("d", "M K V L E Q"),  # 6  -> starts a new batch
        ]
        batches = u._make_batches(items)
        assert [[bid for bid, _ in b] for b in batches] == [["a", "b", "c"], ["d"]]

    def test_single_over_budget_sequence_forms_own_batch(self):
        u = ProtT5Utils(
            device="cpu",
            max_batch_residues=4,
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        items = [("big", " ".join("M" * 10))]  # 10 residues > budget of 4
        batches = u._make_batches(items)
        # Not dropped: it forms its own (over-budget) batch.
        assert batches == [[("big", " ".join("M" * 10))]]


# ---------------------------------------------------------------------------
# Truncation reporting
# ---------------------------------------------------------------------------


class TestTruncation:
    """Over-long sequences are truncated and their ids reported."""

    def test_truncated_ids_reported_and_others_untouched(self):
        u = ProtT5Utils(
            device="cpu",
            max_residues=5,
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        captured: Dict[str, Any] = {}

        def _batch_encode_plus(seqs, **kwargs):
            captured["seqs"] = seqs
            # seq_len must cover the longest cleaned sequence + EOS.
            longest = max(len(s.split()) for s in seqs)
            return _FakeBatchEncoding(
                {
                    "input_ids": _torch_zeros(len(seqs), longest + 1),
                    "attention_mask": _torch_ones(len(seqs), longest + 1),
                }
            )

        u._tokenizer = SimpleNamespace(batch_encode_plus=_batch_encode_plus)
        u._device = "cpu"
        u._model = _make_fake_model(seq_len=None, hidden_dim=EMBEDDING_DIM)

        u.embed_proteins({"short": "MKV", "long": "MKVLEQRST"})

        assert u.truncated_ids == ["long"]
        # The long sequence was truncated to 5 residues before tokenisation.
        assert "M K V L E" in captured["seqs"]


# ---------------------------------------------------------------------------
# THE PADDING TEST — mean-pool over real residues only
# ---------------------------------------------------------------------------


class TestPaddingPoolingFake:
    """Padding test against a deterministic fake encoder (always runs).

    The fake encoder returns, at each position ``t`` of every sequence, the
    vector ``t * ones(1024)`` — but ONLY for real residue positions. Padding
    positions are filled with a huge sentinel (1e6). If pooling ever touches a
    padded position, the short sequence's vector explodes; if pooling is
    correctly restricted to real residues, the short sequence's vector is
    identical whether embedded alone or beside a long sequence.
    """

    def _install_fake(self, utils):
        def _batch_encode_plus(seqs, **kwargs):
            lengths = [len(s.split()) for s in seqs]
            seq_len = max(lengths) + 1  # + EOS
            attention = np.zeros((len(seqs), seq_len), dtype=np.int64)
            for i, n in enumerate(lengths):
                attention[i, : n + 1] = 1  # residues + EOS marked real
            return _FakeBatchEncoding(
                {
                    "input_ids": _FakeTensor(np.zeros((len(seqs), seq_len))),
                    "attention_mask": _FakeTensor(attention),
                }
            )

        utils._tokenizer = SimpleNamespace(batch_encode_plus=_batch_encode_plus)
        utils._device = "cpu"
        utils._model = _PositionEncoderModel(hidden_dim=EMBEDDING_DIM)

    def test_short_vector_identical_alone_and_batched(self, utils):
        self._install_fake(utils)

        alone = utils.embed_proteins({"short": SHORT_SEQ})
        together = utils.embed_proteins({"short": SHORT_SEQ, "long": LONG_SEQ})

        assert alone["short"].shape == (EMBEDDING_DIM,)
        # Pooling over real residues only => the short vector is unaffected by a
        # padded neighbour. A tiny tolerance covers float arithmetic order.
        np.testing.assert_allclose(
            alone["short"], together["short"], rtol=0, atol=1e-5
        )

    def test_pooling_ignores_padding_sentinel(self, utils):
        # Sanity: the expected short-seq vector is mean of positions 0,1,2
        # (n_res=3) => (0+1+2)/3 = 1.0 in every dimension. If padding leaked in,
        # the value would be enormous.
        self._install_fake(utils)
        together = utils.embed_proteins({"short": SHORT_SEQ, "long": LONG_SEQ})
        np.testing.assert_allclose(
            together["short"], np.ones(EMBEDDING_DIM), rtol=0, atol=1e-5
        )


# ---------------------------------------------------------------------------
# THE PADDING TEST — real model (opt-in, slow, GPU-hungry)
# ---------------------------------------------------------------------------


@pytest.mark.prott5_model
@pytest.mark.slow
def test_padding_pooling_real_model():
    """Embed a short sequence alone and beside a long one; vectors must match.

    This loads the real ProtT5 encoder. Skipped unless PROTT5_LIVE_TESTS=1.
    """
    pytest.importorskip("torch")
    pytest.importorskip("transformers")

    util = ProtT5Utils(
        config_file=False, token_file=None, kbase_token_file=None
    )

    alone = util.embed_proteins({"short": SHORT_SEQ})
    together = util.embed_proteins({"short": SHORT_SEQ, "long": LONG_SEQ})

    assert alone["short"].shape == (EMBEDDING_DIM,)
    assert alone["short"].dtype == np.float32
    # fp16 accumulation on GPU introduces small deltas; keep tolerance modest.
    np.testing.assert_allclose(alone["short"], together["short"], rtol=0, atol=1e-3)


# ---------------------------------------------------------------------------
# Fake torch-tensor / model helpers
# ---------------------------------------------------------------------------


class _FakeTensor:
    """Minimal tensor stand-in supporting the operations embed uses.

    Wraps a numpy array and mimics ``.to(device)``, indexing, ``.mean(dim=0)``,
    ``.detach().cpu().float().numpy()`` and ``.astype`` well enough for the
    embedding path — so tests never import torch.
    """

    def __init__(self, arr: np.ndarray) -> None:
        self._arr = np.asarray(arr)

    # device / dtype no-ops
    def to(self, *_a, **_k) -> "_FakeTensor":
        return self

    def detach(self) -> "_FakeTensor":
        return self

    def cpu(self) -> "_FakeTensor":
        return self

    def float(self) -> "_FakeTensor":
        return _FakeTensor(self._arr.astype(np.float32))

    def half(self) -> "_FakeTensor":
        return self

    def eval(self) -> "_FakeTensor":
        return self

    def numpy(self) -> np.ndarray:
        return self._arr

    def mean(self, dim: int = 0) -> "_FakeTensor":
        return _FakeTensor(self._arr.mean(axis=dim))

    def __getitem__(self, idx) -> "_FakeTensor":
        return _FakeTensor(self._arr[idx])


class _FakeBatchEncoding(dict):
    """dict of tensors; values wrapped so ``.to(device)`` works."""

    def __init__(self, mapping: Dict[str, Any]) -> None:
        super().__init__()
        for k, v in mapping.items():
            self[k] = v if isinstance(v, _FakeTensor) else _FakeTensor(np.asarray(v))


def _torch_zeros(rows: int, cols: int) -> _FakeTensor:
    return _FakeTensor(np.zeros((rows, cols)))


def _torch_ones(rows: int, cols: int) -> _FakeTensor:
    return _FakeTensor(np.ones((rows, cols)))


def _make_fake_model(seq_len, hidden_dim: int):
    """A fake encoder returning zeros of the right shape.

    ``seq_len=None`` means: infer per-call from the attention mask width.
    """

    def _forward(input_ids=None, attention_mask=None):
        n = input_ids._arr.shape[0]
        width = input_ids._arr.shape[1] if seq_len is None else seq_len
        hidden = _FakeTensor(np.zeros((n, width, hidden_dim), dtype=np.float32))
        return SimpleNamespace(last_hidden_state=hidden)

    class _Callable:
        def __call__(self, **kwargs):
            return _forward(**kwargs)

    return _Callable()


class _PositionEncoderModel:
    """Fake encoder whose output at real position ``t`` is ``t * ones``.

    Padded positions (those beyond a sequence's real residues, i.e. where the
    attention mask is 0) are filled with a huge sentinel so that any pooling
    which touches padding is unmistakable.
    """

    def __init__(self, hidden_dim: int) -> None:
        self.hidden_dim = hidden_dim

    def __call__(self, input_ids=None, attention_mask=None):
        mask = attention_mask._arr  # (n, seq_len), 1 for residues+EOS
        n, seq_len = mask.shape
        out = np.full((n, seq_len, self.hidden_dim), 1e6, dtype=np.float32)
        for i in range(n):
            n_real = int(mask[i].sum())  # residues + EOS
            for t in range(n_real):
                out[i, t, :] = float(t)
        return SimpleNamespace(last_hidden_state=_FakeTensor(out))
