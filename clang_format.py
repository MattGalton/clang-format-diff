#!/usr/bin/env python3

"""Runs git-clang-format on the lines changed in the given files.

The clang-format binary is either supplied by the caller (--binary) or
obtained via the source registered for a version in CLANG_FORMAT_SOURCES
(--version).

Usage examples:

  clang_format.py --version 23.1.1 foo.cpp bar.h
  clang_format.py --binary ThirdParty/clang-format/clang-format.exe foo.cpp
  clang_format.py --binary clang-format-23 foo.cpp     # looked up on PATH

Based on clang_format.py from https://github.com/jlebar/pre-commit-hooks
(Copyright 2020 Justin Lebar, Apache License 2.0).  Modified by Matt Galton,
2026: diff-only mode, --binary option, pluggable binary sources
(Chromium bucket, PyPI wheel, local path, apt), clang-format 23.
"""

import argparse
import hashlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, NoReturn, Optional, Sequence, Tuple

Version = Tuple[int, int, int]

EXE_SUFFIX = ".exe" if platform.system() == "Windows" else ""


def fatal(message: str) -> NoReturn:
    print(f"FATAL: {message}", file=sys.stderr)
    sys.exit(1)


def cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    path = base / "pre-commit-clang-format-diff"
    path.mkdir(parents=True, exist_ok=True)
    return path


def file_digest(algorithm: str, file: Path) -> str:
    d = hashlib.new(algorithm)
    with open(file, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 64), b""):
            d.update(chunk)
    return d.hexdigest()


def check_digest(algorithm: str, expected: str, file: Path) -> None:
    actual = file_digest(algorithm, file)
    if actual != expected:
        fatal(
            f"{algorithm} mismatch on {file}: expected {expected}, got {actual}. "
            "Maybe the file is corrupted?  Try deleting it."
        )


def install_executable(src: Path, dest: Path) -> None:
    """Marks `src` executable and atomically moves it to `dest`.

    pre-commit may run this script in parallel, so never leave a
    half-written file at `dest`.  `src` must be on the same filesystem.
    """
    os.chmod(src, os.stat(src).st_mode | stat.S_IEXEC)
    os.replace(src, dest)


def download(url: str, dest: Path) -> None:
    print(f"Downloading {url} ...")
    with urllib.request.urlopen(url) as response, dest.open("wb") as out:
        shutil.copyfileobj(response, out)


def check_version(binary: str, version: Version) -> None:
    """Fails unless `binary --version` reports exactly `version`."""
    wanted = ".".join(str(v) for v in version)
    output = subprocess.run(
        (binary, "--version"), capture_output=True, text=True
    ).stdout
    # e.g. "Ubuntu clang-format version 23.1.1 (++20250101...)"
    words = output.split()
    reported = words[words.index("version") + 1] if "version" in words else ""
    if reported != wanted:
        fatal(f"{binary} is clang-format {reported or '?'}, expected {wanted}")


# --- Sources: strategies for obtaining a clang-format binary ---------------


class Source(ABC):
    @abstractmethod
    def resolve(self, version: Version) -> Path:
        """Returns the path to a usable clang-format binary."""


@dataclass(frozen=True)
class ChromiumBucket(Source):
    """A binary from gs://chromium-clang-format, named by its sha1.

    For recent versions, look up `object_name` in the `src/buildtools/*-format`
    entries of https://chromium.googlesource.com/chromium/src/+/main/DEPS.
    """

    sha1: str

    def resolve(self, version: Version) -> Path:
        dest = cache_dir() / f"clang-format-{self.sha1}{EXE_SUFFIX}"
        if not dest.exists():
            with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
                tmpfile = Path(tmp) / "clang-format"
                download(
                    "https://commondatastorage.googleapis.com/"
                    f"chromium-clang-format/{self.sha1}",
                    tmpfile,
                )
                check_digest("sha1", self.sha1, tmpfile)
                install_executable(tmpfile, dest)
        check_digest("sha1", self.sha1, dest)
        return dest


@dataclass(frozen=True)
class PyPIWheel(Source):
    """The binary inside a wheel of https://pypi.org/project/clang-format/.

    These are exact LLVM release builds.  Get `url` and `sha256` from
    https://pypi.org/pypi/clang-format/<version>/json.
    """

    url: str
    sha256: str

    def resolve(self, version: Version) -> Path:
        dest = cache_dir() / f"clang-format-{self.sha256[:16]}{EXE_SUFFIX}"
        if dest.exists():
            return dest
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
            wheel = Path(tmp) / "clang_format.whl"
            download(self.url, wheel)
            check_digest("sha256", self.sha256, wheel)
            member_name = f"bin/clang-format{EXE_SUFFIX}"
            with zipfile.ZipFile(wheel) as zf:
                members = [n for n in zf.namelist() if n.endswith(member_name)]
                if len(members) != 1:
                    fatal(f"expected one {member_name} in {self.url}, found {members}")
                tmpfile = Path(tmp) / "clang-format"
                with zf.open(members[0]) as src, tmpfile.open("wb") as out:
                    shutil.copyfileobj(src, out)
            install_executable(tmpfile, dest)
        return dest


@dataclass(frozen=True)
class LocalPath(Source):
    """A binary at a path, relative to the root of the repo being checked."""

    path: str

    def resolve(self, version: Version) -> Path:
        binary = Path(self.path)
        if not binary.is_file():
            fatal(f"clang-format not found at {binary.resolve()}")
        check_version(str(binary), version)
        return binary


@dataclass(frozen=True)
class Apt(Source):
    """A binary installed by an apt package; we only find it, never install."""

    package: str
    executable: str

    def resolve(self, version: Version) -> Path:
        found = shutil.which(self.executable)
        if found is None:
            fatal(
                f"{self.executable} not found on PATH. "
                f"Install it with: sudo apt-get install {self.package}"
            )
        check_version(found, version)
        return Path(found)


# --- Version table ----------------------------------------------------------

# Platform keys are "<platform.system()>-<platform.machine()>" or, as a
# fallback, just "<platform.system()>".  Machine names differ per OS: Linux
# "x86_64"/"aarch64", macOS "x86_64"/"arm64", Windows "AMD64"/"ARM64".
CLANG_FORMAT_SOURCES: Mapping[Version, Mapping[str, Source]] = {
    (3, 5, 0): {
        "Linux": ChromiumBucket("b26f74f07f51a99d79d34be57a28bc82dee42854"),
        "Darwin": ChromiumBucket("ce0718a133a059aca5da5f307a36bbc310df3e12"),
        "Windows": ChromiumBucket("fc8a7cd2219eaa70daa01173844fad4c815394d7"),
    },
    (3, 6, 0): {
        "Linux": ChromiumBucket("f237f50fab9ceca4066788b7bf936ef2aa366239"),
        "Darwin": ChromiumBucket("eb3fd19492128421c3eab80f4cdeed7b07428988"),
        "Windows": ChromiumBucket("e93c345e8d4d003632cabae8ff4f64c3b74f0c16"),
    },
    (3, 7, 0): {
        "Linux": ChromiumBucket("acc9e19e04ad4cf6be067366d2522348cd160ae1"),
        "Darwin": ChromiumBucket("e66adcd1631b8d80650e7b15106033b04c9c2212"),
        "Windows": ChromiumBucket("2d5e931765ee1c7c4465fd6d77c8b7606c487b3f"),
    },
    (3, 9, 0): {
        "Linux": ChromiumBucket("8b68e8093516183b8f38626740eeaff97f112f7e"),
        "Darwin": ChromiumBucket("afe0942b94fe33619361efe1510ae081c3070dc1"),
        "Windows": ChromiumBucket("f80b6ab38d7c7e0903c25e968028c1eaa25bb874"),
    },
    (4, 0, 0): {
        "Linux": ChromiumBucket("06b8b3e315c1b55b58459d61fe3297e0988c6c63"),
        "Darwin": ChromiumBucket("e0cfdaf63938e06d05a986a0038658ec6b7cad17"),
        "Windows": ChromiumBucket("a15d5130e787633a119e8e0ae9b267696c4c2863"),
    },
    (5, 0, 0): {
        "Linux": ChromiumBucket("5349d1954e17f6ccafb6e6663b0f13cdb2bb33c8"),
        "Darwin": ChromiumBucket("0679b295e2ce2fce7919d1e8d003e497475f24a3"),
        "Windows": ChromiumBucket("c8455d43d052eb79f65d046c6b02c169857b963b"),
    },
    (8, 0, 0): {
        "Linux": ChromiumBucket("327721c99d40602c1829b4b682771d52e1d5f1b8"),
        "Darwin": ChromiumBucket("025ca7c75f37ef4a40f3a67d81ddd11d7d0cdb9b"),
        "Windows": ChromiumBucket("b5f5d8d5f8a8fcd2edb5b6cae37c0dc3e129c945"),
    },
    (11, 0, 0): {
        "Linux": ChromiumBucket("1baf0089e895c989a311b6a38ed94d0e8be4c0a7"),
        "Darwin": ChromiumBucket("62bde1baa7196ad9df969fc1f06b66360b1a927b"),
        "Windows": ChromiumBucket("d4afd4eba27022f5f6d518133aebde57281677c9"),
    },
    (15, 0, 0): {
        "Linux": ChromiumBucket("dd736afb28430c9782750fc0fd5f0ed497399263"),
        "Darwin": ChromiumBucket("a1b33be85faf2578f3101d7806e443e1c0949498"),
        "Windows": ChromiumBucket("66882fadbf9e99cc00b8677d8c1e7e8b3cfdf4fe"),
    },
    (16, 0, 0): {
        "Linux": ChromiumBucket("6ef2183a178a53e47e4448dbe192b1d8d5290222"),
        "Darwin": ChromiumBucket("1c125f1d882babd1a930fcd5b4fd80bba3f5321e"),
        "Windows": ChromiumBucket("d88796ff22d70c7d5ede62636daa220ccd238f06"),
    },
    (18, 0, 0): {
        "Linux": ChromiumBucket("b42097ca924d1f1736a5a7806068fed9d7345eb4"),
        "Darwin": ChromiumBucket("96c34e77259c4cc1fc7bdf067fc058bfd341ab85"),
        "Windows": ChromiumBucket("49458d4c1e884a38308f8dc6a2c7eb55fc478755"),
    },
    # Chromium's LLVM trunk snapshot: reports "23.0.0git" (llvm-project
    # 20b6ec66967ac2a8f932863c1abf251e5b17a843), not a release.
    (23, 0, 0): {
        "Linux": ChromiumBucket("f5485c39451137ee943021a0fb63738b306c5026"),
        "Darwin": ChromiumBucket("35ddc571ab654a4f58d7b35f394b04835d0b98f9"),
        "Darwin-arm64": ChromiumBucket("945e45cbcb5443c3e569d5e0783fd012154fc4a7"),
        "Windows": ChromiumBucket("fb0bcaf406ad6f0bd6b4a6347d2abe78a94fb13e"),
    },
    # The LLVM 23.1.1 release.  Other sources work too, e.g.
    #   "Windows": LocalPath("ThirdParty/clang-format-23.1.1/Windows/clang-format.exe"),
    #   "Linux": Apt(package="clang-format-23", executable="clang-format-23"),
    (23, 1, 1): {
        "Linux-x86_64": PyPIWheel(
            "https://files.pythonhosted.org/packages/42/ef/3f8e215916e79ecd435b5bc20810443f80fce3fb8bbfe6d2783ceb775c1b/clang_format-23.1.1-py2.py3-none-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
            "64900462c1203cee2fec364536c2dda708baf0619e15b52ceda926648cc82f56",
        ),
        "Linux-aarch64": PyPIWheel(
            "https://files.pythonhosted.org/packages/0e/b6/1a162e427d912b88653a66e99a22414d798eb885b761ffddb74dbc13963f/clang_format-23.1.1-py2.py3-none-manylinux_2_26_aarch64.manylinux_2_28_aarch64.whl",
            "09af54d2ef51680b34e36ed71b9f510bd399fb7b10b29a2a40843cd0e0344228",
        ),
        "Darwin-x86_64": PyPIWheel(
            "https://files.pythonhosted.org/packages/45/a3/e45ab75ae0fbe8f90a637af146978d1ef3799c26252163a8068b19a46e03/clang_format-23.1.1-py2.py3-none-macosx_10_9_x86_64.whl",
            "8f8ea1e02f7cab8dace82a0d56733e532ffc3e6e41e658edc28261029e94c499",
        ),
        "Darwin-arm64": PyPIWheel(
            "https://files.pythonhosted.org/packages/29/da/f354b637650ae04854d9096c252d08f03ccda7044b1f005861a4ddba28cf/clang_format-23.1.1-py2.py3-none-macosx_11_0_arm64.whl",
            "d64a1788759c4cbc08a0aca21dd2bd38605c0758543aa15b8c0636508f0ebae7",
        ),
        "Windows-AMD64": PyPIWheel(
            "https://files.pythonhosted.org/packages/4e/98/bcf1d46d62b63f92e8ee513dc2c3caa5c3168206af3785ada76ce994c96b/clang_format-23.1.1-py2.py3-none-win_amd64.whl",
            "eaf3075d7e2bf477c150c6d53bfff040eca851365c0429a06b9bec0de65186d5",
        ),
        "Windows-ARM64": PyPIWheel(
            "https://files.pythonhosted.org/packages/90/4c/3ae3671f91d181bfda6148c684c3514c878767c0fbbb3793d4d804103d3a/clang_format-23.1.1-py2.py3-none-win_arm64.whl",
            "7f81856d845b6274b888d0d5f51a1f819f38f080a5b9637a38873f6e0e2cde3b",
        ),
    },
}

DEFAULT_VERSION: Version = (23, 1, 1)


def version_str(version: Version) -> str:
    return ".".join(str(v) for v in version)


def parse_version(text: str) -> Version:
    try:
        major, minor, patch = (int(v) for v in text.split("."))
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid version {text!r}")
    version = (major, minor, patch)
    if version not in CLANG_FORMAT_SOURCES:
        choices = ", ".join(version_str(v) for v in CLANG_FORMAT_SOURCES)
        raise argparse.ArgumentTypeError(
            f"unknown version {text!r} (choose from {choices})"
        )
    return version


def source_for(version: Version) -> Source:
    sources: Dict[str, Source] = dict(CLANG_FORMAT_SOURCES[version])
    system, machine = platform.system(), platform.machine()
    source = sources.get(f"{system}-{machine}") or sources.get(system)
    if source is None:
        fatal(
            f"no clang-format {version_str(version)} source for {system}-{machine}; "
            "pass --binary instead."
        )
    return source


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run clang-format on the changed lines of the given files."
    )
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument(
        "--binary",
        help="clang-format executable to use (a path, or a name on PATH).",
    )
    choice.add_argument(
        "--version",
        type=parse_version,
        help=f"clang-format version to fetch (default: {version_str(DEFAULT_VERSION)}).",
    )
    parser.add_argument(
        "files", nargs="*", type=Path, help="Files pre-commit believes are changed."
    )
    args = parser.parse_args(argv)

    if not args.files:
        return 0

    if args.binary is not None:
        found = shutil.which(args.binary)
        if found is None:
            fatal(f"cannot find clang-format binary {args.binary!r}")
        binary = found
    else:
        version = args.version or DEFAULT_VERSION
        binary = str(source_for(version).resolve(version))

    git_cf_path = Path(__file__).resolve().parent / "git-clang-format"
    print("Formatting changed lines in " + " ".join(str(x) for x in args.files))
    return subprocess.run(
        (
            sys.executable,
            str(git_cf_path),
            "-f",
            "--binary",
            binary,
            "--",
            *(str(f) for f in args.files),
        )
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
