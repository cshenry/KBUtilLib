"""KBUtilLib lazy properties turn a missing optional dependency into an
actionable ImportError naming the attribute and the pip extra to install."""

import sys

import pytest

from kbutillib.toolkit import KBUtilLib

_RXNSIM_MODULE = "kbutillib.domains.biochem.ms_reaction_similarity_utils"


class _MissingModuleFinder:
    """meta_path finder making one module's import fail as if *missing* were absent."""

    def __init__(self, target: str, missing: str) -> None:
        self.target = target
        self.missing = missing

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.target:
            raise ModuleNotFoundError(
                f"No module named {self.missing!r}", name=self.missing
            )
        return None


@pytest.fixture
def block_import(monkeypatch):
    def _block(target: str, missing: str) -> None:
        monkeypatch.delitem(sys.modules, target, raising=False)
        monkeypatch.setattr(
            sys, "meta_path", [_MissingModuleFinder(target, missing), *sys.meta_path]
        )

    return _block


def test_missing_mapped_dependency_names_attribute_and_extra(block_import):
    block_import(_RXNSIM_MODULE, "rdkit")
    kbu = KBUtilLib()

    with pytest.raises(ImportError) as excinfo:
        kbu.rxnsim

    message = str(excinfo.value)
    assert "KBUtilLib.rxnsim" in message
    assert "rdkit" in message
    assert "pip install 'KBUtilLib[reaction_similarity]'" in message
    assert isinstance(excinfo.value.__cause__, ModuleNotFoundError)


def test_missing_unmapped_dependency_falls_back_to_plain_pip_hint(block_import):
    block_import(_RXNSIM_MODULE, "some_unlisted_pkg")
    kbu = KBUtilLib()

    with pytest.raises(ImportError, match=r"pip install some_unlisted_pkg"):
        kbu.rxnsim
