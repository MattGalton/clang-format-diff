# clang-format-diff pre-commit hook

Runs `git-clang-format` so that only the lines you changed get reformatted.
If you want whole-file formatting, use
[mirrors-clang-format](https://github.com/pre-commit/mirrors-clang-format)
instead.

## Attribution and license

`clang_format.py` is based on
[jlebar/pre-commit-hooks](https://github.com/jlebar/pre-commit-hooks)
(Copyright 2020 Justin Lebar) and has been modified by Matt Galton. It is
licensed under the Apache License 2.0; see [LICENSE](LICENSE).

`git-clang-format` is part of the LLVM Project, under the Apache License v2.0
with LLVM Exceptions; see https://llvm.org/LICENSE.txt.

## Choosing the clang-format binary

`clang_format.py` takes one of:

- `--binary PATH` – use your own clang-format (a path relative to the repo
  root, an absolute path, or a name on `PATH` such as `clang-format-23`).
- `--version X.Y.Z` – obtain that version via the source registered for it in
  `CLANG_FORMAT_SOURCES` (default: 23.1.1).

Each version maps platform keys (`Linux`, `Windows-AMD64`, `Darwin-arm64`, ...)
to a source strategy:

| Source | What it does |
| --- | --- |
| `ChromiumBucket(sha1)` | Downloads from `commondatastorage.googleapis.com/chromium-clang-format`, verifies sha1, caches. |
| `PyPIWheel(url, sha256)` | Downloads a wheel of the [`clang-format`](https://pypi.org/project/clang-format/) PyPI package (exact LLVM releases), verifies sha256, extracts and caches the binary. |
| `LocalPath(path)` | Uses a binary at a path relative to the checked repo's root; checks its `--version`. |
| `Apt(package, executable)` | Finds an apt-installed binary on `PATH` (never installs); checks its `--version`. |

Downloads are cached under `$XDG_CACHE_HOME/pre-commit-clang-format-diff`
(default `~/.cache/...`).

Available versions: 3.5.0 – 18.0.0 and 23.0.0 (Chromium builds), 23.1.1 (PyPI).
Chromium ships LLVM trunk snapshots, so `23.0.0` reports itself as `23.0.0git`
and can format slightly differently from the 23.1.1 release.

## Usage as a remote hook

```yaml
repos:
-   repo: git@github.com:MattGalton/clang-format-diff.git
    rev: <sha>
    hooks:
    -   id: clang-format-diff
        args: ['--binary', 'ThirdParty/clang-format-23.1.1/Windows/clang-format.exe']
```

## Usage as a local hook

Copy `clang_format.py` and `git-clang-format` next to each other in your repo
(e.g. `Scripts/`), then:

```yaml
repos:
-   repo: local
    hooks:
    -   id: clang-format-diff
        name: clang-format-diff
        entry: python Scripts/clang_format.py --binary clang-format-23
        language: system
        require_serial: true   # git-clang-format is not parallel-safe
        types_or: [c++, c, cuda]
```

`language: python` also works (pre-commit then runs it in its own virtualenv);
the script only uses the standard library.

## Running in CI

pre-commit checks commits, but CI checks pull requests, which may contain
several commits. To check every file a PR touched:

```
pre-commit run --from-ref $(git merge-base origin/master HEAD) --to-ref HEAD
```

Formatting the PR's combined diff can flag changes you didn't see locally,
because clang-format may align neighbouring lines differently when they are
changed together rather than in separate commits.
