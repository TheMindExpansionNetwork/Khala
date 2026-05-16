# Khala on Modal

This fork adds a bounded Modal wrapper for Khala generation.

## Safety / cost posture

- Default path is an on-demand Modal GPU function, **not** a permanently running endpoint.
- Checkpoints are cached in Modal Volume `khala-checkpoints`.
- Outputs are saved in Modal Volume `khala-outputs` and returned to the caller.
- The optional web endpoint uses Modal scale-to-zero semantics, but should still be checked/stopped after tests.
- Model license is `cc-by-nc-4.0`; treat generated samples as non-commercial evaluation unless a different license is obtained.
- Upstream README currently warns inference quality may be unstable due to an unresolved numerical-precision issue.

## One-shot sample

```bash
modal run modal/khala_modal_app.py::generate_cli \
  --prompt "High-energy futuristic rave opener, heavy but clean sub bass, crisp drums, euphoric synth stabs, warehouse laser atmosphere, festival-ready mix, polished modern electronic production." \
  --lyrics-file /tmp/lyrics.txt \
  --duration 1 \
  --out /tmp/khala_sample.mp3
```

## Optional endpoint

```bash
modal deploy modal/khala_modal_app.py
```

POST JSON to `generate_endpoint`:

```json
{
  "prompt": "High-energy futuristic rave opener, heavy but clean sub bass, crisp drums, euphoric synth stabs, warehouse laser atmosphere, festival-ready mix, polished modern electronic production.",
  "lyrics": "[Intro] Jimsky online tonight\n[Drop] Signal in the lights",
  "duration": 1,
  "mode": "vocal",
  "prompt_mode": "natural",
  "top_k_bb": 80,
  "temperature": 1.0
}
```

## Stop / verify idle

```bash
modal app list | grep khala || true
modal app stop khala-music-modal || true
modal volume ls khala-outputs /
```

If a `modal run` command is still active locally, interrupt it first; Modal should terminate the backing task.
