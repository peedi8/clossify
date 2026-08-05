# Image Generation Price Policy

Updated: 2026-07-02

This policy separates final detail-page image shortages from paid image API calls. Do not use the bare Korean label `1장` in queue, Stage0, Stage3, Stage4, or reports unless the unit is explicitly named.

## Required units

- `needed_cuts`: final usable image cuts still needed for the seven-slot detail page.
- `api_call_count`: paid API calls to the image provider.
- `output_canvas_count`: full images returned by the provider.
- `output_layout`: `single`, `two_panel`, `quad_2x2_2048`, or another explicit layout.
- `panel_count_used`: usable cuts extracted from a generated split canvas.
- `estimated_cost_usd`: estimated paid cost using the current rate table.

Example labels:

- Good: `필요컷 2 / GPT Image 2 단일 2호출 / 예상 $0.012`
- Good: `필요컷 3 / GPT Image 2 단일 3호출 / 예상 $0.018`
- Fallback/experiment only: `필요컷 3 / Gemini 3.1 2K 2x2 1캔버스 / 예상 $0.101`
- Bad: `생성 1장`

## Current rate table

Use these as estimates, not invoices. Text/image input tokens can add cost for edit/reference workflows.

| Provider | Model | Mode | Estimate |
| --- | --- | --- | --- |
| OpenAI | `gpt-image-2` | 1024x1024 low single output | `$0.006` |
| OpenAI | `gpt-image-2` | 1024x1024 medium single output | `$0.053` |
| OpenAI | `gpt-image-2` | 1024x1024 high single output | `$0.211` |
| Google | `gemini-2.5-flash-image` | standard output up to 1024x1024 | `$0.039` |
| Google | `gemini-2.5-flash-image` | batch/flex output up to 1024x1024 | `$0.0195` |
| Google | `gemini-3.1-flash-image` | standard 2K output, including 2048x2048 2x2 canvas | `$0.101` |
| Google | `gemini-3.1-flash-image` | batch 2K output | `$0.050` |
| Google | `gemini-3-pro-image` | standard 1K/2K output | `$0.134` |
| Google | `gemini-3-pro-image` | batch/flex 1K/2K output | `$0.067` |

Sources:

- OpenAI pricing page and image generation guide: `gpt-image-2` image output token pricing and sample output costs.
- Google Gemini API pricing page: Gemini 2.5 Flash Image, Gemini 3.1 Flash Image, and Gemini 3 Pro Image per-image equivalent costs.

## Routing policy

Stage0 is a preflight estimate. It may say generation is likely, but it should not pretend to know final paid calls before Stage3/Stage4 source reselect and role-fit review.

Default price-first route:

1. `needed_cuts == 0`: no paid generation.
2. `needed_cuts == 1`: prefer `gpt-image-2` single output unless a quality or edit requirement explicitly requires another model.
3. `needed_cuts >= 2`: prefer `needed_cuts` separate `gpt-image-2` single outputs.
4. Gemini 3.1 2K 2x2 and Gemini 3 Pro Image are fallback/experiment lanes, not default shortage-fill lanes.

If a split canvas is selected, display both units:

- `needed_cuts=3`
- `api_call_count=3`
- `output_canvas_count=3`
- `output_layout=single`
- `panel_count_used=3`

## Queue display rule

Queue cockpit and reports must display generated work as compact badges:

- `필요컷 n`
- `모델: gpt-image-2` or `모델: gemini-3.1-flash-image`
- `호출 n`
- `레이아웃: 단일` or `레이아웃: 2x2`
- `예상비용 $x.xx`

The UI must not show `1장` when it actually means `one paid split canvas`.
