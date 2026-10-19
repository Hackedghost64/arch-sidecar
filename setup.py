from setuptools import setup, find_packages

setup(
    name="arch-sidecar",
    version="0.1.0",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "arch-sidecar=arch_sidecar.cli:main",
            "prefactor=arch_sidecar.cli:main",
        ],
    },
)
