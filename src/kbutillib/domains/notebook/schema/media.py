"""Media model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Optional

from pydantic import BaseModel

if TYPE_CHECKING:
    from ..session import NotebookSession


class Media(BaseModel):
    """Growth media specification.

    A Media with ``source="kbase"`` or ``source="msmedia"`` and no
    ``inline_composition`` is perfectly valid — composition is resolved
    lazily via :meth:`resolve_composition` when needed, not at
    registration time.
    """

    id: str
    source: Literal["kbase", "msmedia", "inline"] = "kbase"
    inline_composition: Optional[dict[str, float]] = None  # cpd_id -> mM

    def resolve_composition(
        self,
        session: NotebookSession,
    ) -> dict[str, float]:
        """Return the media composition as ``{cpd_id: mM}``.

        - ``source="inline"``: returns ``inline_composition`` directly.
        - ``source="kbase"``: stub — raises ``NotImplementedError`` until
          the KBase workspace lookup is wired in.
        - ``source="msmedia"``: stub — raises ``NotImplementedError`` until
          the ModelSEED media lookup is wired in.

        Parameters
        ----------
        session : NotebookSession
            The active session (needed for future KBase/msmedia API access).

        Returns
        -------
        dict[str, float]
            Compound-id to millimolar concentration mapping.

        Raises
        ------
        NotImplementedError
            For ``kbase`` and ``msmedia`` sources (stubs).
        ValueError
            If ``source="inline"`` but ``inline_composition`` is ``None``.
        """
        if self.source == "inline":
            if self.inline_composition is None:
                raise ValueError(
                    f"Media {self.id!r} has source='inline' but no inline_composition"
                )
            return dict(self.inline_composition)

        if self.source == "kbase":
            raise NotImplementedError(
                f"KBase media lookup for {self.id!r} is not yet implemented. "
                f"Use source='inline' with inline_composition for now."
            )

        if self.source == "msmedia":
            raise NotImplementedError(
                f"ModelSEED media lookup for {self.id!r} is not yet implemented. "
                f"Use source='inline' with inline_composition for now."
            )

        raise ValueError(f"Unknown media source: {self.source!r}")
