# FolioImageWorker

Docker service for image editing, Topaz upscaling on a Windows PC, and TIFF
print layouts. The complete caller contract is in [API_CONTRACT.md](API_CONTRACT.md).
One job accepts 1–8 source images and returns one edited print layout. Folio
passes the selected brief photos to the worker in their original order.
For new jobs, the AI Tunnel model selected in Folio edits the references using
the supported aspect ratio nearest to the requested visible painting size.
The preview keeps that size and content.
The print TIFF adds mirrored image edges and an outer white margin on every
side; margin widths follow the longer visible side (see the contract). For a
40 × 60 cm painting, the complete TIFF is 52 × 72 cm at 150 DPI.
After generation, Topaz runs only when the generated image's longest side is
at least 3000 pixels shorter than the print layout's longest side at 150 DPI.
Otherwise the TIFF is prepared directly from the generated image.

## Setup

Budget model profiles (AI Tunnel public catalogue checked 2026-10-13):
`seedream-5-0-lite` / 4K (about 6.30 RUB), `seedream-4.5` / 4K
(about 7.20 RUB), `seedream-5-0-flash` / 2K (about 3.24 RUB), and
`hy-image-v3.5-preview` / 4K (about 4.32–5.76 RUB).
These are catalogue estimates, not a billing guarantee. Actual `usage.cost_rub`
is logged. Old Flux 3, Nano Banana 2.1 and Seedream Pro jobs are blocked before
any billable POST; already saved generated images can still be composed without
another generation. Select and save a budget model in Folio after updating both
components. No existing queued model snapshot is silently rewritten.

1. Copy `.env.example` to `.env` and replace the example values. Russian
   comments in that file explain each setting. Copy `FOLIO_POLL_URL` from
   Folio → Settings → Image worker and choose one
   `WORKER_API_KEY`. Set `IMAGE_PROVIDER=aitunnel` and add your private
   `AITUNNEL_API_KEY`; do not put the key in Folio or a chat. Without the key,
   the worker stays online but does not pull new Folio jobs. If the print share
   is ready, set `PRINT_SHARE_PATH` to its
   UNC path. Leave it blank to keep TIFF files in the local `spool` folder.
   The Windows account running `topaz_agent.py` needs write access to a
   configured share; Docker does not access it.
2. On this PC, run `Start Worker.cmd`. It starts both Docker and the Windows
   Topaz agent. `Stop Worker.cmd` stops both. Nothing starts at Windows login.
   The worker listens on port 8000 inside Docker; this port is not published on
   the PC because Folio never connects to it.
   In Folio, set `FOLIO_PUBLIC_BASE_URL` in its environment, then open
   **Settings → Image worker** and enter the same key. The worker polls Folio
   and uploads its result; Folio does not need the PC address. Outbound HTTPS
   traffic to Folio and AI Tunnel goes directly through the PC connection.
3. When upscaling is needed, the launcher uses the installed Gigapixel CLI.
   If CLI processing fails, it writes manual instructions. In that case, open
   each `spool/<job-id>/input.png` in Gigapixel and export
   its output to `spool/<job-id>/upscaled.png` at or above the required pixel
   dimensions shown in `manual-instructions.txt`.

For a free local pipeline check, set `IMAGE_PROVIDER=mock`. This copies the
uploaded image without editing it; the job
still goes through print preparation and, if needed, Topaz. Do not use mock output as a
finished customer image.

The previous Nano Banana adapter remains only for jobs already stored with
`provider=nanobanana` and for a deliberate rollback using
`IMAGE_PROVIDER=nanobanana`. Each job stores its provider in SQLite so an
in-flight job is not silently moved between services during a restart.
AI Tunnel returns the edited image synchronously as base64. The worker saves
validated source images locally and sends them inline as base64 references,
avoiding provider-side downloads of Folio's protected source URLs. It saves
the response before moving to Topaz and TIFF assembly. If the POST response is
lost or the worker restarts after sending it, the outcome is marked for manual
review; the paid request is **not** repeated automatically.
Connection failures before the AI Tunnel request reaches the server switch to
the official `ru-api.aitunnel.ru` mirror and are retried up to three times.
Set `AITUNNEL_BASE_URL=https://ru-api.aitunnel.ru/v1` to start on the mirror.
The connect timeout defaults to 60 seconds and the response timeout to one hour;
both can be configured separately. Per the configured integration rule,
HTTP 504 is retried up to three times, switching to the
mirror after the first attempt. If all attempts return 504, the job fails with
the exact cause. Other uncertain generation outcomes are reported as `unknown`
and are never replayed automatically. Balance is account-wide and
statistics are cached, so unchanged numbers do not prove a particular request
was not processed. Folio polling and status delivery retry with increasing
delays (5–60 seconds). Delivery retries are scheduled per job: a stale result
rejected by Folio cannot block newer previews. Confirmed obsolete results
(409 result conflict / 404 unknown job) keep their delivery error without
being retried forever or marked delivered. `docker compose logs -f folio-worker` shows job acceptance,
generation, Topaz decisions, print preparation, Folio delivery, and retry reasons.

The Windows agent copies the finished TIFF to the UNC share when configured.
Otherwise the TIFF stays in `spool/<job-id>/print.tif` on this PC.

Notes: Up to one job is processed at a time. Keep `data` and `spool`
persistent across container restarts. Jobs are retained; add a
retention policy suited to your storage and privacy requirements. A task
submitted to AI Tunnel may have incurred a charge even if its response was
lost. Check the AI Tunnel account before deliberately creating a new job.

## Sources

- [AI Tunnel image generation and editing](https://aitunnel.ru/docs/images)
- [AI Tunnel API mirror](https://aitunnel.ru/docs)
- [AI Tunnel balance](https://aitunnel.ru/docs/api/balance) and [cached usage statistics](https://aitunnel.ru/docs/api/stats)
- [AI Tunnel image model catalog](https://api.aitunnel.ru/public/aitunnel/models/images)
- Legacy Nano Banana: [generate/edit](https://docs.nanobananaapi.ai/nanobanana-api/generate-or-edit-image)
- [Nano Banana Pro (up to 8 source images)](https://docs.nanobananaapi.ai/nanobanana-api/generate-image-pro)
- [Nano Banana task status](https://docs.nanobananaapi.ai/nanobanana-api/get-task-details)
- [Nano Banana callbacks](https://docs.nanobananaapi.ai/nanobanana-api/generate-or-edit-image-callbacks)
- [Topaz Gigapixel CLI](https://docs.topazlabs.com/gigapixel-ai/gp-command-line-interface)
