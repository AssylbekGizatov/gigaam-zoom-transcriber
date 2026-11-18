# Verified GigaAM distribution

The source ZIP includes the GigaAM configuration, original model card and full
MIT license. The large `pytorch_model.bin` weight file is a GitHub Release asset,
not a Git/LFS blob:
https://github.com/AssylbekGizatov/gigaam-zoom-transcriber/releases/tag/models-v1

`model-manifest.json` pins its SHA256 and exact byte count. The upstream URL is
provenance and is used only by the maintainer publishing tool. User installation
uses `scripts/acquire_model.py`, which downloads only from this repository's
GitHub release or copies an approved local file with `--source-directory`.
`--check` verifies local files without network requests. There is no upstream
fallback during installation or inference.

GigaAM code revision: `7447938d791c4f3e643386ee22c33777004293a5`.
GigaAM model revision: `2f8a57144e6ec3adfd32fe0484d9ea9913305bc8`.
The pinned model card declares MIT; the original GigaAM MIT text is included
with the unchanged model card. Large weights are not part of the source ZIP.

## Maintainers

The publishing workflow is manual-only. It stages only GigaAM, verifies bytes,
and refuses to replace existing release files with different contents.
GigaAM-only metadata uses `model-manifest-gigaam.json`, `GIGAAM-NOTICES.md`
and `GIGAAM-SHA256SUMS.txt`, preserving previously published release metadata.
This source change does not remove or modify any existing release assets.
Corporate users do not run the publisher or need an upstream model-hub account.
