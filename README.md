# Todoist for ARM64 Flatpak

This personal fork of the Flathub package publishes Todoist for AArch64, including
Pixel 3a / Sargo. It uses Doist's official ARM64 AppImage as install-time extra
data. The application is downloaded directly from Doist; this repository hosts
the Flatpak wrapper. This package is not affiliated with or supported by Doist.

## Install and update

```sh
flatpak remote-add --user --if-not-exists samcday-todoist \
  https://samcday.github.io/com.todoist.Todoist/todoist.flatpakrepo
flatpak install --user samcday-todoist com.todoist.Todoist
```

Updates arrive through the same Flatpak update mechanism used for Flathub apps:
GNOME Software or `flatpak update --user`. Automatic installation depends on the
device's updater settings. Freedesktop runtime dependencies still come from
Flathub. The published application branch is `master`, architecture `aarch64`.
The original x86_64 Snap recipe remains in the manifest but is not published here.

On Sargo, browser login and real-world task syncing have been tested. Todoist's
minimum window width still causes some right-side clipping on a phone display.
If browser login does not return to the app, run these within the graphical session:

```sh
gio mime x-scheme-handler/todoist com.todoist.Todoist.desktop
gio mime x-scheme-handler/com.todoist com.todoist.Todoist.desktop
```

## Release maintenance

[GitHub Actions](https://github.com/samcday/com.todoist.Todoist/actions/workflows/publish.yml)
checks the official stable ARM64 feed every day. A new release is downloaded and
its advertised size and SHA512 are verified before its SHA256 is pinned in the
manifest. Unexpected URLs, downgrades, mismatched checksums and invalid ARM64
executables stop the update. Runtime versions follow the upstream Todoist
packaging, and a new Electron BaseApp commit also triggers a rebuild.

A native ARM64 runner builds the wrapper, signs it, installs from the resulting
repository (including extra-data extraction), checks its shared libraries and
launches it with a temporary profile in a headless display. Publishing happens
only after these checks pass. The previous signed repository is mirrored and
verified before new commits are added. Failed checks or builds leave the last
successful Pages deployment available. They are visible in Actions and use your
GitHub Actions notification preferences.

The updater records successful checks monthly, including months without a new
release, to keep the public fork active. GitHub's scheduled jobs are best-effort
and can be delayed or disabled; check Actions if updates stop. A manual run with
`force` rebuilds the current release. Changes beyond runtime version upgrades
in the upstream packaging still need review in this fork.

The public signing key is [packaging/repo.gpg](packaging/repo.gpg), fingerprint
`299958F6C6EC614C841D059030C81C62E1AF5BA2`. The private key is held in the
`FLATPAK_SIGNING_KEY` Actions secret as an ASCII-armored OpenPGP key. Keep a private
backup: clients trust this key across updates. No private key belongs in Git.

The online metadata is available at
[release.json](https://samcday.github.io/com.todoist.Todoist/release.json).
Do not use a single-file `.flatpak` bundle for this package: it does not carry the
required extra-data attachment. Install from the signed repository instead.

## Local checks

Install Python 3, PyYAML and ShellCheck, then run:

```sh
python3 -m unittest discover -s tests -v
shellcheck scripts/build-repository.sh
python3 scripts/update-todoist.py --check-only
```

The workflow documents the native ARM64 build dependencies. The build script
expects `SIGNING_HOME` to contain the dedicated signing key and `PUBLISH_URL`
to point at the Pages site. `BOOTSTRAP=true` permits only a missing initial
repository; an existing repository must always pass signature verification.
