from setuptools import setup, find_packages

setup(
    name="arch-sidecar",
    version="0.4.0",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "arch-sidecar=arch_sidecar.cli:main",
            "prefactor=arch_sidecar.cli:main",
            "devrith=arch_sidecar.cli:main",
        ],
    },
)
