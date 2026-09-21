"""Compatibility shim for old pip/setuptools editable installs.

Modern setuptools (>=64) reads everything from pyproject.toml; the macOS system
Python ships an older toolchain that needs parameters spelled out here.
"""

from setuptools import setup

setup(
    name="qpool",
    version="0.1.0",
    description="Quota pooling + cost-aware routing control plane for coding agents",
    packages=["qp"],
    entry_points={"console_scripts": ["qpool=qp.cli:main"]},
    python_requires=">=3.9",
)
