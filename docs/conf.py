"""Sphinx configuration."""

project = "KBUtilLib"
author = "Christopher Henry"
copyright = "2025, Christopher Henry"
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx_click",
    "myst_parser",
]
autodoc_typehints = "description"
html_theme = "furo"
