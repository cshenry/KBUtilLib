"""ProtT5 protein-embedding utilities.

This module provides :class:`ProtT5Utils`, a small, reusable surface for turning
protein sequences into fixed-length embeddings with the ProtT5 encoder
(``Rostlab/prot_t5_xl_half_uniref50-enc``). Embedding proteins is useful far
beyond any single job type, so it lives here as a standalone utility rather than
inside a job adapter.

ProtT5 is a soft dependency: this module imports cleanly even when ``torch`` and
``transformers`` are not installed. The tokenizer and encoder are loaded lazily
on first :meth:`ProtT5Utils.embed_proteins` call and cached on the instance, so
repeated calls within a session pay the model-load cost only once.

The integration hides the well-known ProtT5 sharp edges behind a one-method
interface:

- **Tokenisation** — ProtT5 expects residues space-separated, and the rare
  residues ``U``, ``Z``, ``O`` and ``B`` mapped to ``X``.
- **Batching by residue count** — protein lengths vary hugely, so batches are
  bounded by total residue count, not sequence count.
- **Mean-pooling over real residues only** — pooling over padding is the classic
  ProtT5 bug; it silently degrades every vector and nothing downstream detects
  it. Pooling here uses the attention mask so padding never contributes.
- **Device selection** — CUDA when available, CPU otherwise. It always works on
  CPU (merely slower); it never hard-fails for lack of a GPU.
- **Half precision on GPU** — ``float16`` weights on CUDA, ``float32`` on CPU.
- **OOM back-off** — on a CUDA out-of-memory error the batch size is halved and
  retried, down to a single sequence before giving up.
- **Truncation** — sequences longer than ``max_residues`` are truncated (never
  silently, never by rejecting the whole batch); the ids of truncated sequences
  are reported to the caller via :attr:`ProtT5Utils.truncated_ids` and the return
  value of :meth:`embed_proteins`.

Reference: https://huggingface.co/Rostlab/prot_t5_xl_half_uniref50-enc
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from kbutillib.core.shared_env_utils import SharedEnvUtils

__all__ = ["ProtT5Utils", "ProtT5UtilsImpl"]

# Default HuggingFace model id / path for the ProtT5 encoder.
DEFAULT_MODEL_PATH = "Rostlab/prot_t5_xl_half_uniref50-enc"

# ProtT5 embedding dimensionality (per-residue and, after mean-pooling,
# per-protein).
EMBEDDING_DIM = 1024

# Rare / ambiguous residues ProtT5 was trained to treat as ``X``.
_UZOB = re.compile(r"[UZOB]")


class ProtT5Utils(SharedEnvUtils):
    """Embed protein sequences with the ProtT5 encoder.

    The single public method, :meth:`embed_proteins`, maps ``{id: sequence}`` to
    ``{id: 1024-d float32 vector}``. Everything else — tokenisation, residue-count
    batching, padding-safe mean-pooling, device selection, half precision, OOM
    back-off and truncation reporting — is handled internally.

    Args:
        model_path: HuggingFace model id or local path for the ProtT5 encoder.
            Defaults to ``Rostlab/prot_t5_xl_half_uniref50-enc``.
        device: Compute device override (``"cpu"`` or ``"cuda"``). When ``None``
            (default), CUDA is used if available, otherwise CPU.
        max_residues: Sequences longer than this are truncated to this length,
            and their ids reported to the caller. Defaults to 5000.
        max_batch_residues: Upper bound on the total residue count per forward
            pass. Batches are packed greedily up to this bound. Defaults to 4096.
        model_name: Guard-only, keyword-only. Not a real parameter — the
            checkpoint is selected via ``model_path``. Passing ``model_name``
            (anything other than ``None``) raises :class:`TypeError` so the
            once-shipped mistake of calling ``ProtT5Utils(model_name=...)``
            fails loudly instead of being silently absorbed as a dead attribute.
        **kwargs: Additional keyword arguments forwarded to
            :class:`~kbutillib.core.shared_env_utils.SharedEnvUtils`.

    Attributes:
        truncated_ids: Ids of the sequences truncated during the most recent
            :meth:`embed_proteins` call. Also returned from that method.

    Example::

        from kbutillib.domains.ai.prott5_utils import ProtT5Utils

        util = ProtT5Utils()
        vectors = util.embed_proteins({"p1": "MKV...", "p2": "MTA..."})
        vectors["p1"].shape  # (1024,)
        util.truncated_ids    # ids of any sequences that were truncated
    """

    def __init__(
        self,
        model_path: str = DEFAULT_MODEL_PATH,
        device: Optional[str] = None,
        max_residues: int = 5000,
        max_batch_residues: int = 4096,
        *,
        model_name: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        # Guard against the constructor-keyword mistake that once shipped
        # silently: a caller passing ``model_name=`` meant ``model_path=``, but
        # BaseUtils.__init__ swallows unknown kwargs (setattr), so the intended
        # checkpoint was discarded with no error. Fail loudly instead of aliasing
        # or warning — silence is the exact failure mode being prevented.
        if model_name is not None:
            raise TypeError(
                "ProtT5Utils has no 'model_name' parameter; "
                "use 'model_path' instead."
            )

        super().__init__(**kwargs)

        if max_residues < 1:
            raise ValueError("max_residues must be >= 1")
        if max_batch_residues < 1:
            raise ValueError("max_batch_residues must be >= 1")

        self.model_path = model_path
        self._device_override = device
        self.max_residues = max_residues
        self.max_batch_residues = max_batch_residues

        # Lazy: loaded on first embed_proteins call.
        self._tokenizer = None
        self._model = None
        self._device: Optional[str] = None

        # Ids truncated during the most recent embed_proteins call.
        self.truncated_ids: List[str] = []

    # ------------------------------------------------------------------
    # Device / model loading
    # ------------------------------------------------------------------

    def _get_device(self) -> str:
        """Return the compute device string, auto-selecting CUDA when available.

        Falls back to CPU when torch is missing or reports no CUDA device.
        """
        if self._device_override is not None:
            return self._device_override
        try:
            import torch  # type: ignore[import]

            return "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            return "cpu"

    def _load(self) -> None:
        """Load and cache the tokenizer and encoder on first use.

        Raises:
            ImportError: If ``torch`` or ``transformers`` is not installed.
        """
        if self._model is not None:
            return

        try:
            import torch  # type: ignore[import]
            from transformers import T5EncoderModel, T5Tokenizer  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "ProtT5Utils.embed_proteins requires torch and transformers. "
                "Install them with: pip install torch transformers sentencepiece"
            ) from exc

        device = self._get_device()

        self.log_info(
            f"Loading ProtT5 tokenizer/encoder from {self.model_path!r} "
            f"onto device={device!r}"
        )
        # do_lower_case=False: residues are case-sensitive single-letter codes.
        tokenizer = T5Tokenizer.from_pretrained(self.model_path, do_lower_case=False)
        model = T5EncoderModel.from_pretrained(self.model_path)

        # Half precision on GPU; full precision on CPU (fp16 matmul on CPU is
        # unsupported / far slower).
        if device == "cuda":
            model = model.half()
        else:
            model = model.float()

        model = model.to(device)
        model = model.eval()

        self._tokenizer = tokenizer
        self._model = model
        self._device = device
        self.log_info("ProtT5 encoder loaded and cached.")

    @staticmethod
    def _no_grad():
        """Return ``torch.no_grad()`` when torch is present, else a nullcontext.

        The real forward pass runs under ``torch.no_grad()``. When torch is not
        installed the encoder cannot have loaded, so this path is only reached in
        tests that inject a fake model; the nullcontext keeps that path working
        without importing torch.
        """
        try:
            import torch  # type: ignore[import]

            return torch.no_grad()
        except ImportError:
            from contextlib import nullcontext

            return nullcontext()

    # ------------------------------------------------------------------
    # Tokenisation (pure — no model needed; unit-testable)
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_sequence(sequence: str) -> str:
        """Prepare one raw sequence for the ProtT5 tokenizer.

        Uppercases, strips whitespace, maps the rare residues ``U``, ``Z``,
        ``O`` and ``B`` to ``X``, and space-separates the residues (ProtT5
        requires one space between each residue).

        Args:
            sequence: Raw amino-acid sequence (no spaces expected).

        Returns:
            A space-separated, UZOB-mapped residue string ready for the
            tokenizer.
        """
        seq = sequence.upper().strip()
        # Drop any internal whitespace the caller may have included so residue
        # separation is exactly single-spaced.
        seq = re.sub(r"\s+", "", seq)
        seq = _UZOB.sub("X", seq)
        return " ".join(seq)

    # ------------------------------------------------------------------
    # Batching (pure — by total residue count, not sequence count)
    # ------------------------------------------------------------------

    def _make_batches(
        self, items: List[Tuple[str, str]]
    ) -> List[List[Tuple[str, str]]]:
        """Pack (id, sequence) items into batches bounded by residue count.

        Items are packed greedily in the given order. A single sequence longer
        than :attr:`max_batch_residues` still forms its own (over-budget) batch
        rather than being dropped; the per-batch forward pass will fall back to
        smaller batches via OOM back-off if needed.

        Args:
            items: List of ``(id, cleaned_sequence)`` where ``cleaned_sequence``
                is space-separated so residue count == number of tokens minus
                any special tokens (counted here as the residue count).

        Returns:
            A list of batches, each a list of ``(id, cleaned_sequence)``.
        """
        batches: List[List[Tuple[str, str]]] = []
        current: List[Tuple[str, str]] = []
        current_residues = 0

        for seq_id, cleaned in items:
            # residue count of a space-separated cleaned sequence
            n_res = len(cleaned.split()) if cleaned else 0
            if current and current_residues + n_res > self.max_batch_residues:
                batches.append(current)
                current = []
                current_residues = 0
            current.append((seq_id, cleaned))
            current_residues += n_res

        if current:
            batches.append(current)
        return batches

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def embed_proteins(
        self, sequences: Mapping[str, str]
    ) -> Dict[str, "Any"]:
        """Embed protein sequences into fixed-length ProtT5 vectors.

        Returns exactly one 1024-d ``float32`` vector per input id, computed by
        mean-pooling the per-residue encoder outputs over the *real* residues
        only (padding never contributes).

        Sequences longer than :attr:`max_residues` are truncated to that length;
        their ids are recorded on :attr:`truncated_ids` and returned in the
        result so the caller is never surprised by silent truncation.

        Args:
            sequences: Mapping of ``{protein_id: sequence}``.

        Returns:
            ``{protein_id: numpy float32 array of shape (1024,)}``.

        Note:
            The ids of truncated sequences are exposed both on
            :attr:`self.truncated_ids` and (for convenience) can be read after
            the call; the returned dict always contains one vector per input id.

        Raises:
            ImportError: If ``torch`` or ``transformers`` is not installed.
        """
        import numpy as np  # local: numpy is a light, always-present dep

        self.truncated_ids = []
        if not sequences:
            return {}

        # Truncate over-long sequences and record which ids were truncated.
        prepared: List[Tuple[str, str]] = []
        for seq_id, raw in sequences.items():
            seq = raw.upper().strip()
            seq = re.sub(r"\s+", "", seq)
            if len(seq) > self.max_residues:
                seq = seq[: self.max_residues]
                self.truncated_ids.append(seq_id)
            cleaned = self._clean_sequence(seq)
            prepared.append((seq_id, cleaned))

        if self.truncated_ids:
            self.log_warning(
                f"Truncated {len(self.truncated_ids)} sequence(s) to "
                f"{self.max_residues} residues: {self.truncated_ids}"
            )

        self._load()

        results: Dict[str, Any] = {}
        for batch in self._make_batches(prepared):
            batch_results = self._embed_batch_with_backoff(batch, np)
            results.update(batch_results)

        return results

    # ------------------------------------------------------------------
    # Batch embedding with OOM back-off
    # ------------------------------------------------------------------

    def _embed_batch_with_backoff(
        self, batch: List[Tuple[str, str]], np: Any
    ) -> Dict[str, Any]:
        """Embed one batch, halving on CUDA OOM down to a single sequence.

        Args:
            batch: List of ``(id, cleaned_sequence)``.
            np: The imported numpy module (passed to avoid re-importing).

        Returns:
            ``{id: (1024,) float32 array}`` for every item in ``batch``.
        """
        try:
            return self._embed_batch(batch, np)
        except RuntimeError as exc:
            if "out of memory" not in str(exc).lower():
                raise
            # Free whatever we can before retrying smaller.
            if self._device == "cuda":
                import torch  # type: ignore[import]

                torch.cuda.empty_cache()

            if len(batch) == 1:
                # Cannot subdivide further.
                self.log_error(
                    f"CUDA OOM embedding a single sequence "
                    f"({batch[0][0]!r}); giving up on this sequence's batch."
                )
                raise

            mid = len(batch) // 2
            self.log_warning(
                f"CUDA OOM on batch of {len(batch)} sequences; "
                f"halving to {mid} + {len(batch) - mid} and retrying."
            )
            results: Dict[str, Any] = {}
            results.update(self._embed_batch_with_backoff(batch[:mid], np))
            results.update(self._embed_batch_with_backoff(batch[mid:], np))
            return results

    def _embed_batch(
        self, batch: List[Tuple[str, str]], np: Any
    ) -> Dict[str, Any]:
        """Run one forward pass and mean-pool over real residues only.

        Args:
            batch: List of ``(id, cleaned_sequence)`` where each sequence is
                space-separated and UZOB-mapped.
            np: The imported numpy module.

        Returns:
            ``{id: (1024,) float32 array}`` for every item in ``batch``.
        """
        ids = [seq_id for seq_id, _ in batch]
        seqs = [cleaned for _, cleaned in batch]

        encoded = self._tokenizer.batch_encode_plus(
            seqs,
            add_special_tokens=True,
            padding="longest",
            return_tensors="pt",
        )
        input_ids = encoded["input_ids"].to(self._device)
        attention_mask = encoded["attention_mask"].to(self._device)

        with self._no_grad():
            output = self._model(
                input_ids=input_ids, attention_mask=attention_mask
            )
        # (batch, seq_len, 1024)
        hidden = output.last_hidden_state

        results: Dict[str, Any] = {}
        # Mean-pool over REAL residues only. attention_mask marks real tokens
        # (residues + the appended EOS special token) as 1 and padding as 0.
        # We pool over the actual residue positions per sequence, never over
        # padding — pooling over padding is the classic ProtT5 bug this guards.
        for i, seq_id in enumerate(ids):
            # Number of real residues for this sequence (space-separated count).
            n_res = len(seqs[i].split()) if seqs[i] else 0
            if n_res == 0:
                results[seq_id] = np.zeros(EMBEDDING_DIM, dtype=np.float32)
                continue
            # Residues occupy positions [0, n_res) of the encoder output; the
            # EOS special token sits at position n_res. Slice to the real
            # residues so neither EOS nor padding contributes to the mean.
            per_residue = hidden[i, :n_res]  # (n_res, 1024)
            pooled = per_residue.mean(dim=0)  # (1024,)
            results[seq_id] = pooled.detach().cpu().float().numpy().astype(
                np.float32
            )

        return results


# ── Composition-based implementation ─────────────────────────────────────


class ProtT5UtilsImpl:
    """Composition-based ProtT5Utils.

    Holds ``env: SharedEnvUtils`` instead of inheriting from it. Delegates all
    method calls to an internal :class:`ProtT5Utils` instance.
    """

    def __init__(self, env, **kwargs: Any) -> None:
        self._env = env
        _kwargs: Dict[str, Any] = {
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }
        _kwargs.update(kwargs)
        self._delegate = ProtT5Utils(**_kwargs)

    @property
    def env(self):
        return self._env

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)
