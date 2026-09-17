"""setup.py for the standalone airunner-native package.

Extracted from Capsize-Games/airunner's native/ directory (issue #2196,
part of the repo-split tracker #2185). Its release cadence is not
Python's -- the sidecar build changes when a pinned native runtime
changes, rarely and for reasons unrelated to application code -- which
is the whole reason it now lives in its own repository rather than a
subdirectory of the Qt application's.
"""

from pathlib import Path

from setuptools import find_packages, setup

VERSION = "0.1.0"

# The project is GPL-3.0-only, matching the source application it
# launches (Capsize-Games/airunner's own LICENSE and classifiers).
LICENSE_CLASSIFIERS = [
    "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
]

# Supply-chain hardening (issue #2036 in the source repository). A
# hash-pinned GitHub archive URL was the original pin, but PyPI rejects
# any distribution carrying a PEP 440 direct reference ("400 Can't have
# direct dependency"). facehuggershield 1.0.0 is on PyPI, so depend on
# it by version instead: a PyPI release is immutable once uploaded and
# can only be yanked, never replaced, which is a stronger guarantee
# than a movable git tag ever was.
FACEHUGGERSHIELD_REQUIREMENT = "facehuggershield==1.0.0"

README = (Path(__file__).resolve().parent / "README.md").read_text(
    encoding="utf-8"
)

DEVELOPMENT_REQUIREMENTS = [
    "pytest",
    "pytest-timeout",
]

NATIVE_CONSOLE_SCRIPTS = [
    "airunner-native=airunner_native.launcher:main",
]

# airunner-common only: the launcher's app-specific imports (airunner,
# airunner_services) are all function-scoped, deferred until the
# launcher actually runs, not hard install-time dependencies -- see
# the "services"/"daemon" and "gui"/"desktop" extras below for the two
# ways this package actually gets used together with the rest of the
# application.
NATIVE_BASE_REQUIREMENTS = [
    "airunner-common~=6.1",
    FACEHUGGERSHIELD_REQUIREMENT,
]


def build_native_extras_require() -> dict[str, list[str]]:
    """Return optional extras for the native package surface."""
    return {
        "development": DEVELOPMENT_REQUIREMENTS,
        "dev": DEVELOPMENT_REQUIREMENTS,
        # A headless/daemon-role install: this launcher plus the
        # services package, no GUI.
        "services": ["airunner-services~=6.1"],
        "daemon": ["airunner-services~=6.1"],
        # A GUI-client-role install. airunner itself already depends
        # on airunner-services, so this extra alone is enough for a
        # full desktop install.
        "gui": ["airunner~=6.1"],
        "desktop": ["airunner~=6.1"],
    }


def build_native_setup_kwargs(*, package_source_dir: str) -> dict[str, object]:
    """Return the setuptools metadata for the native package surface."""
    return {
        "name": "airunner-native",
        "version": VERSION,
        "author": "Capsize LLC",
        "description": "AIRunner native launcher and runtime sidecar tooling",
        "long_description": README,
        "long_description_content_type": "text/markdown",
        "license": "GPL-3.0-only",
        "classifiers": LICENSE_CLASSIFIERS,
        "author_email": "contact@capsizegames.com",
        "url": "https://github.com/Capsize-Games/airunner-native",
        "package_dir": {"": package_source_dir},
        "packages": find_packages(package_source_dir),
        "python_requires": ">=3.13.3",
        "install_requires": NATIVE_BASE_REQUIREMENTS,
        "extras_require": build_native_extras_require(),
        "include_package_data": True,
        "entry_points": {"console_scripts": NATIVE_CONSOLE_SCRIPTS},
    }


setup(**build_native_setup_kwargs(package_source_dir="src"))
