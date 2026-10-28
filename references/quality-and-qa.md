# Quality and acceptance

## Separate failure types

1. Static low detail: soft face/cloth even when still. Compare supported higher native resolution or learned latent upscale/refinement at the same display size.
2. Physical motion blur: a rapidly moving thin prop can legitimately blur. Eliminating all blur can look less real.
3. Anatomy/prop failure: melted wrists, extra fingers, detached grip or changing shaft length. Inspect nearby frames and source references; a larger export alone is not a repair.
4. Temporal rewrite: changed order, textbook replacement action, or an invented swing during an idle tail. Use segment-specific descriptions/scene anchors or a genuinely stronger control workflow. A sharp but rewritten action is not faithful replacement.

## Trace LoRA routing

Inspect file, strength source and active branch. In one T8-style workflow a switch selected Float0.5 or1.0 for a Hybrid Turbo LoRA on the HIGH branch. The LOW-only output did not execute that switch. This example is not a universal node65 instruction.

Other workflows can put unrelated operations at that number. Do not disable every motion-related LoRA merely because of its name. Compare one meaningful setting at a time with matching references, seed and schedule. Seeds do not guarantee pixel identity across devices/graphs.

One verified example used576×1024 first-pass4 steps, learned latent1.5x upscale, high-pass5 steps to864×1536. These are examples rather than optimal defaults. Distilled/Turbo schedules may get worse with arbitrary step increases.

A scene-frame prompt in Ref2VA is guidance, not a guarantee of exact pixel/frame reproduction.

## Inspect before delivery

- Identity, age, hair, body type, costume and shoes across turns.
- Target person count; no source hair/accessory residues.
- Actual grip/release/regrip; coherent hand/prop geometry.
- Foot support, shadows and anatomy.
- Real action order and quiet ending.
- Stable background; pixel locking needs mask/compositing.
- Overlap joins: facing, hand height, feet, actor position, prop phase. Image-difference minima are candidates only.
- Full decode, dimensions, fps/frame count, copied-audio payload hash.

Separate tested success, remaining defects and unused experiments. Report experiment total versus selected-clip cost. Preserve previous versions. Keep an approved motion-preservation preference in force until the user changes it.
