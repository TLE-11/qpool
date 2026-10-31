"""Compatibility shim for old pip/setuptools editable installs.

Modern setuptools (>=64) reads everything from pyproject.toml; the macOS system
Python ships an older toolchain that needs parameters spelled out here.
"""

from setuptools import setup, find_packages

setup(
    name="qpool",
    version="0.1.0",
    description="Quota pooling + cost-aware routing control plane for coding agents",
    packages=find_packages(include=["qp", "qp.*"]),
    package_data={"qp": ["static/*.html", "static/*.js", "static/*.css", "_vendor/hiroute/LICENSE", "_vendor/hiroute/SOURCE.md"]},
    entry_points={"console_scripts": ["qpool=qp.cli:main"]},
    python_requires=">=3.9",
    install_requires=["tomlkit>=0.13,<1", "zstandard>=0.23,<1"],
)
