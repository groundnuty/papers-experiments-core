# The federation's Onedata build, 12 Nov 2026

The two images the federation pins are a **25.0 development build**, not the released 25.0 tag. The oneprovider image
holds op-worker at commit `f118ed90074434045bad5d7bd2611bd206f1a405`, 81 commits after the 25.0 tag and before 25.1;
the onezone image holds oz-worker at `14b5bda7d84bbfe2f25bc9bedb9033af6d1a8b55`, 46 commits after the 25.0 tag and
before 25.1. Wherever an earlier dated file of this directory says "25.0" for the federation, it means this build.
Those files stay as they were; this one amends them by naming them.

Nothing here was measured against a container. Every figure is read from the issue that researched the build (#186,
section 0), which read the package repositories' submodule pointers and compared commits with the tags on GitHub. The
image tags are the ones `fedlayout.py` pins (`IMAGES`).

## The build

| Image | Package commit, which is the image tag | Components it pins |
|---|---|---|
| `docker.onedata.org/oneprovider:ID-765309c84f` | `onedata/oneprovider-pkg@765309c84f2dbb622c726c650a726cede22e51a8` (2026-03-03, "Update refs to origin/develop.") | op-worker `f118ed90074434045bad5d7bd2611bd206f1a405`, onepanel `fbcab3f57e61406b1556760e1dc375aa062f1210`, cluster_manager `7ed38083b4`, bamboos `f51581de4e` |
| `docker.onedata.org/onezone:ID-ba7a778696` | `onedata/onezone-pkg@ba7a778696fd00f42ca0a3e0b9fe0f5801a8637b` (2026-03-03) | oz-worker `14b5bda7d84bbfe2f25bc9bedb9033af6d1a8b55`, onepanel `fbcab3f57e61406b1556760e1dc375aa062f1210`, cluster_manager `3e0e17583e` |

The image tag `ID-<hash>` is the package repository's commit. It was found by matching the submodule pointers in the
oneprovider-pkg and onezone-pkg trees against the components' commits.

## Where the build stands against the tags

| Component at its commit | Against the 25.0 tag | Against 25.1 |
|---|---|---|
| op-worker `f118ed9` (the facts file of 10 Nov calls it "build 81-gf118ed9007") | 81 commits ahead, 0 behind | 88 behind |
| oz-worker `14b5bda` (that file: "build 46-g14b5bda7"; the tag's commit is `03af698e`) | 46 commits ahead | behind 25.1; the count is not recorded |
| onepanel `fbcab3f` | not recorded | not recorded |

## What op-worker pulls in

Read from op-worker's `rebar.config` at `f118ed9`, and from the dependencies' own at the commits it names:

- cluster-worker `0e9cb4037bc51ae79ec3933d0d66d87bcf83b032`, which pins gui `b3b08d5342d57c709f0cea23e8183834e0d9a716`;
- ctool `8ae7baf62f90129de7b9dbda5af9b44d944b7875`, which pins cowboy at tag `2.9.0` and ranch at `af1508c4a729`.

## Which earlier files "25.0" refers to

Every file below says "25.0" and, for the federation's own services, means the build above. For the zone and the
providers the first of them already records the build suffixes `46-g14b5bda7` and `81-gf118ed9007`.

- `2026-11-10-federation-facts.md`
- `2026-11-10-federation-facts-parent-walk.md`
- `2026-11-11-federation-facts-mount.md`
- `2026-11-11-federation-facts-posix-and-fuse.md`
- `2026-11-11-federation-facts-visibility.md`
- `2026-11-12-federation-facts-listing-end.md`
- `2026-11-12-federation-facts-tool-server-write.md`
- `day0-20261110T150215Z.md`
- `day0-20261110T214021Z.md`
- `day0-20261111T003902Z.md`
- `day0-20261111T133619Z.md`
- `day0-20261111T144521Z.md`

Cite the build as "25.0 development build (op-worker f118ed9)", never as "25.0" alone: a behaviour measured here may
differ on the released 25.0 tag or on 25.1.

One 25.0 in those files is not this build. `onedata/oneclient:25.0` is the mount sidecar's image, a separate one that
the pins above do not cover. The mount facts file records its digest and that its log names version `25.0` with an
empty commit; what it was built from is not recorded.

## What is not recorded

- **Image digests.** No facts file or `fedlayout.py` records a digest for either federation image, so the build is
  named by tag and by the package commit that tag is, not by what the image's bytes hash to. A later pass with the
  images on a host can record them.
- **The Erlang/OTP version inside the images.** No builder version appears in op-worker's or oneprovider-pkg's build
  specs at these commits.
- **Full ids of cluster_manager and bamboos,** which the source gives in short form only, and of the 25.0 tag's commit in
  oz-worker, `03af698e`, likewise.
