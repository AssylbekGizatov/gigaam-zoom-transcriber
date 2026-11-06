# Verified model distribution

The source ZIP includes GigaAM config, original model card, full MIT terms,
Apache-2.0 terms and CC BY 4.0 attribution/license link. Large weights are
**GitHub Release assets**, not Git/LFS blobs:
https://github.com/AssylbekGizatov/gigaam-zoom-transcriber/releases/tag/models-v1

`model-manifest.json` pins the original SHA256 and exact byte count. Upstream
URLs are provenance and are used **only by the maintainer publishing workflow**.
The application and the two user downloaders never fall back to those URLs.
End-user installation downloads from this repository's release and verifies the
same checksums, or copies already-downloaded files with `--source-directory`.
`--check` performs local verification without any network request.

GigaAM source revision: `7447938d791c4f3e643386ee22c33777004293a5`.
GigaAM model revision: `2f8a57144e6ec3adfd32fe0484d9ea9913305bc8`.
The pinned model card declares MIT; upstream has no separate model LICENSE,
so the original GigaAM MIT text is included with the unchanged model card.

CAM++: 3D-Speaker / Tongyi Lab, Hui Wang et al., CAM++, arXiv:2303.00332.
ModelScope `iic/speech_campplus_sv_zh_en_16k-common_advanced`, v1.0.0,
Apache-2.0. ONNX conversion by sherpa-onnx; no additional modifications.

WeSpeaker: WeSpeaker team, Wang et al., ICASSP 2023. Original model revision
`f0c48c298fd835726c27956a5d617bad7115627e`, CC BY 4.0. ONNX conversion by
sherpa-onnx; no additional modifications. Full attribution and license link:
`licenses/CC-BY-4.0.txt`. No endorsement by the model authors is implied.

## Maintainers

`.github/workflows/publish-models.yml` mirrors only the pinned public assets,
checks their hashes and publishes a release with notices. It runs on a change
to its own workflow or manifest, or on manual workflow dispatch. A corporate
user does **not** run the publisher or need an upstream model-hub account.
Already published assets are checked, never silently replaced.
