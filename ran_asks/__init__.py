"""Installed entry package for a Ran-ASKS workspace."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ran-asks")
except PackageNotFoundError:
    __version__ = "0+unknown"

__all__ = ["__version__"]
