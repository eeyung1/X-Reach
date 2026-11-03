# X-Reach Multimodal Extractor

This package encodes draft text, images, and sampled video keyframes with
OpenCLIP ViT-B/32, combines the modality vectors, emits a six-stage RVQ
Semantic ID, and returns Phoenix-shaped action propensity fields.

## Install

```bash
python -m pip install -e ".[inference,test]"
pytest
```

The inference extra installs `open_clip_torch` and PyAV. OpenCLIP downloads its
pretrained checkpoint on first construction unless the model cache is already
populated. For a CPU/GPU-specific PyTorch wheel, install the matching PyTorch
build before this extra.

## Use

```python
from xreach_multimodal import MediaAsset, MultimodalExtractor

extractor = MultimodalExtractor()
result = extractor.extract(
    "A short draft",
    [MediaAsset(data=image_bytes, media_type="image/jpeg", filename="draft.jpg")],
)
payload = result.to_dict()
```

`payload["semantic_id"]` contains six integer codes in the range 0–255.
`payload["predictions"]` contains the action probability heads and continuous
dwell-time fields expected by `xreach-core`. Video requests are sampled at up
to four timestamps (opening, hook, midpoint, and climax); output includes the
actual timestamps and duration. Inputs are limited to four files of at most
50 MiB each, and videos longer than 180 seconds are rejected.

## Codebooks and propensity calibration

The default RVQ codebooks are deterministic bootstrap values so the pipeline
can run without a private checkpoint. They are marked
`quantizer_status="bootstrap_untrained"`; train codebooks on representative
embeddings with `ResidualVectorQuantizer.fit(...)`, then save/load the resulting
NPZ checkpoint before treating Semantic IDs as stable clusters.

Action heads default to a zero-shot OpenCLIP prompt-similarity proxy, marked
`propensity_status="uncalibrated_prompt_proxy"`. This is not a learned Phoenix
model and must not be presented as calibrated engagement probability. A
checkpoint can be supplied with `LinearPropensityHead.load(path)`; that output
is marked `linear_head_uncalibrated` until its training and calibration are
verified. No engagement training data or propensity checkpoint is included in
this repository.
