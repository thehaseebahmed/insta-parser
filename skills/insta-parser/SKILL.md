---
name: insta-parser
description: Call, diagnose, and operate the self-hosted insta-parser HTTP service that turns an Instagram reel/post/carousel URL into text (caption, per-item spoken transcript, per-item on-screen text) and optional place enrichment. Use when asked to summarize, read, transcribe, search, or pull the contents out of an Instagram reel/post/carousel URL, when a workflow needs a post's text as input, or when insta-parser returns errors, hangs, or needs configuration changed.
---

# Using insta-parser

`insta-parser` is a small self-hosted HTTP service that turns an Instagram
reel/post/carousel URL into text: post metadata, a spoken-audio transcript for
each video, and OCR of on-screen text for each image/video — attributed to
which item of the post it came from. It is plain JSON over HTTP with no
authentication and no SDK and no CLI — call it directly with `curl` (or any
HTTP client).

## Reaching the service

Default host port is **8420**.

| Caller | Base URL |
|---|---|
| Same host (shell, cron) | `http://localhost:8420` |
| Another container | `http://<homelab-host-ip>:8420` |
| Over Tailscale | `http://<homelab-host>:8420` |

**Container-to-container name lookup does not work here.** insta-parser runs
with `network_mode: bridge` (Docker's default `docker0` network), while most
other apps sit on their own compose networks. Docker only provides DNS between
containers on the *same user-defined* network, so `http://insta-parser:8000`
will fail to resolve from e.g. n8n. Use the host's IP with the published port
instead — `http://172.17.0.1:8420` (the `docker0` gateway, which is the host)
works from most containers, and the host's LAN or Tailscale address always
works.

Confirm reachability before anything else:

```bash
curl -s http://localhost:8420/health     # => {"status":"ok"}
```

If the service isn't answering, hangs, or errors out, see
[`reference/operations.md`](reference/operations.md) — troubleshooting,
tuning, and Instagram session setup live there, out of the way of the normal
calling flow below.

## Decide which path to take

**Want everything (the normal case)?** Use `POST /process` and poll. It runs
the whole pipeline and returns metadata + transcript + OCR together.

**Only need one piece, or need to skip a step?** Use the per-step endpoints.
They share a `job_id` and must run in order — `/download` first, then
`/extract-audio` before `/transcribe`, and `/extract-frames` before `/ocr`.
Calling one out of order returns `422`.

Prefer `/process` unless you specifically need to skip work (e.g. a silent
reel where the transcript is worthless, or a talking-head reel with no
on-screen text worth OCR-ing).

## The full pipeline: POST /process

`/process` is **asynchronous**. It returns immediately with a `job_id`; you
poll for the result. Do not expect the result in the first response.

```bash
curl -sX POST http://localhost:8420/process \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.instagram.com/reel/ABC123xyz/"}'
```

```json
{ "job_id": "3f1c9a2b...", "status": "queued" }
```

Then poll until `status` is `done` or `error`:

```bash
curl -s http://localhost:8420/jobs/3f1c9a2b...
```

```json
{
  "job_id": "3f1c9a2b...",
  "status": "done",
  "step": null,
  "result": {
    "metadata": { "shortcode": "ABC123xyz", "username": "some_account", "caption": "...",
                  "timestamp": "2026-01-02T03:04:05", "like_count": 1234,
                  "comment_count": 56, "location": "Amsterdam" },
    "media": [
      {
        "index": 1, "type": "video",
        "transcript": { "text": "full transcript ...", "language": "en",
                        "segments": [ { "start": 0.0, "end": 1.5, "text": "..." } ] },
        "ocr": [ { "text": "ON SCREEN TEXT", "confidence": 92.4 } ]
      }
    ],
    "places": [ { "name": "Joe's Pizza", "city": "Rome", "country": "Italy",
                  "rating": 4.6, "maps_url": "https://maps.google.com/?cid=..." } ]
  },
  "error": null,
  "updated_at": 1767322045.12
}
```

A **carousel** (multiple images/videos in one post) returns one entry in
`result.media` per slide, in carousel order, each with its own `index`,
`type` (`"image"` or `"video"`), `transcript` (`null` for an image slide),
and `ocr`. `/process` never includes on-disk file paths (`metadata` has no
`media` list, and `media` entries have no `path`/`frame`) — those files are
deleted once the job completes, so a path here would be dead weight; use the
per-step endpoints below if you need a real path to a file that's still on
disk.

`updated_at` is a Unix timestamp of the last state change. If it stops
advancing while `status` is still `running`, the job is stuck on that step —
useful for deciding to give up rather than polling indefinitely.

**Polling guidance.** Transcription dominates the runtime. A short reel is
usually done in well under a minute, a long one can take several; a carousel
scales roughly with how many video items it has. Poll every **5–10 seconds**
and give up after ~10 minutes. While running, `step` tells you where it is:
`download` → `extract-audio` → `transcribe` → `extract-frames` → `ocr` →
`places`. Do not poll in a tight loop.

`extract-audio` and `transcribe` still run (and report as the current `step`)
for an image-only post — they just do nothing, since there are no video items
to process. That's not a stall; it finishes immediately and moves on.

`status` values: `queued`, `running`, `done`, `error`, and `files-only` (a
folder exists from per-step calls but no `/process` run is tracking it).

### Shell recipe: start, wait, clean up

A full run from a shell, with no client beyond `curl` and `jq`:

```bash
BASE=http://localhost:8420

job_id=$(curl -sX POST "$BASE/process" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.instagram.com/reel/ABC123xyz/"}' | jq -r .job_id)

deadline=$((SECONDS + 600))   # ~10 min, per the polling guidance above
while :; do
  job=$(curl -s "$BASE/jobs/$job_id")
  status=$(echo "$job" | jq -r .status)
  [ "$status" = done ] || [ "$status" = error ] && break
  [ "$SECONDS" -ge "$deadline" ] && { echo "gave up waiting on $job_id" >&2; exit 1; }
  sleep 7
done

echo "$job" | jq .                 # the finished job, success or error
curl -sX DELETE "$BASE/jobs/$job_id" > /dev/null   # see Cleanup below
[ "$status" = done ]                # exit 0 only if the job itself succeeded
```

## Per-step endpoints

All take `{"job_id": "..."}` except `/download`, which starts the job.

| Call | Returns |
|---|---|
| `POST /download` `{"url": "..."}` | `{job_id, metadata, media: [{index, type, path}, ...]}` — `media` is the manifest, in post order |
| `POST /extract-audio` | `{job_id, media: [{index, path}, ...]}` — one entry per video item, `[]` for an image-only post |
| `POST /transcribe` | `{job_id, media: [{index, text, segments, language}, ...]}` — one entry per video item, `[]` for an image-only post |
| `POST /extract-frames` | `{job_id, media: [{index, type, frames: [path, ...]}, ...]}` — one entry per media item |
| `POST /ocr` | `{job_id, media: [{index, type, results: [{frame, text, confidence}, ...]}, ...]}` |
| `DELETE /jobs/{job_id}` | `{job_id, status: "deleted"}` |

Every list here is keyed by the same `index` from `/download`'s manifest, in
carousel order (1 for a plain reel/photo, 1..N for a carousel).

These are synchronous — each returns its result directly. Place-metadata
enrichment (below) only runs as part of `/process` — there's no per-step
equivalent, so the per-step path never produces `places`.

**Clean up when you're done.** Per-step jobs keep their files on disk.
Call `DELETE /jobs/{job_id}` at the end of a workflow. (A sweep eventually
removes folders older than the configured TTL, default 24h, but don't rely on
it.) `/process` cleans up after itself automatically.

## Interpreting the output

- **`username`** is the post's owner. Falls back to `"Unknown"` if Instagram's
  API failed to return it (rare, but the media/transcript/OCR are unaffected).
- **`caption`** is the poster's own text, shared by every item in a carousel.
  Often the single most informative field — check it before assuming you need
  the transcript.
- **`result.media`** is a list with one entry per post item, in carousel
  order (a plain reel or single photo still has exactly one entry). Each
  entry has `index`, `type` (`"video"` or `"image"`), `transcript`, and `ocr`.
- **`transcript`** is `null` for an image item — there's no audio to
  transcribe. For a video item, `transcript.text` is the spoken audio; empty
  means that video had music only, ASMR-style non-speech sound, or no speech
  — a normal result, not a failure. The service filters non-speech audio
  before transcribing specifically to avoid returning hallucinated gibberish;
  if you do see garbled/repetitive text, treat it as an unreliable transcript
  rather than real spoken content.
- **`ocr`** is on-screen text for that one item, one entry per retained
  frame, already deduplicated across near-identical *consecutive* frames
  within that item (identical text repeated on a *different* carousel slide
  is not deduplicated — it's kept and attributed to that slide). `confidence`
  is a mean per-word Tesseract score (0–100) *after* a result below
  `OCR_MIN_CONFIDENCE` (default 50) has already been dropped as noise — so
  everything you see cleared that floor, but still expect some OCR mangling
  from stylized fonts and busy backgrounds; do not present raw OCR as a
  verbatim quote.
- A post often carries the same information in caption, speech, *and*
  on-screen text — and a carousel often repeats it across slides. When
  summarizing, reconcile all of it by meaning rather than concatenating it,
  and attribute slide-specific claims (e.g. "step 3" on-screen text) to their
  `index` if the caller cares which slide something came from.
- `/process` never returns on-disk paths (see the note above) — that's true
  regardless of `KEEP_FILES`, since the response contract just doesn't
  declare those fields there.
- **`result.places`** is optional enrichment (a local model extracts place
  mentions, then Google Maps resolves them) — see
  [`reference/operations.md`](reference/operations.md) for how it's
  configured. It is always a list; if the service isn't configured for it,
  `places` comes back `[]`, not absent. If Maps resolution isn't configured
  but extraction is, entries still appear with `rating`/`maps_url` as `null`.
  Don't treat an empty `places` as proof the post has none — `[]` means "not
  verified" as much as it means "none found."

## Errors

| Code | Meaning | What to do |
|---|---|---|
| `400` | Bad input — URL isn't an Instagram post/reel, or malformed `job_id` | Fix the input. Don't retry as-is. |
| `404` | Unknown `job_id` | The job expired, was deleted, or the service restarted. Start over. |
| `422` | Step called out of order, or ffmpeg/tesseract failed | Run the prerequisite step. |
| `502` | Instagram fetch failed — rate-limited, private, or removed post | See [`reference/operations.md`](reference/operations.md). |

The message is always in `detail`, e.g. `{"detail": "Could not find an
Instagram shortcode in URL: ..."}`. Read it before deciding what to do.

**On `502` (rate limiting).** Instagram throttles anonymous access. Do not
retry in a loop — that makes it worse. Back off for several minutes, and if it
persists report that the service likely needs an authenticated session
configured (`IG_USERNAME` / `IG_SESSION_FILE`) rather than continuing to retry.

**Job state is in memory.** If the container restarts, in-flight and completed
`/process` results are lost and polling returns `404`. Re-submit the URL.

## Constraints worth knowing

- Only **public** posts work unless the service is configured with a logged-in
  session. Private accounts return `502`.
- **All post shapes work**: a single video (reel), a single image, and a
  carousel of any mix of images and videos.
- One transcription runs at a time by default; concurrent requests queue rather
  than fail. Submitting many posts at once is safe but not faster.
- Frames are capped (default 60) **per video item**, downscaled before OCR, so
  a very long video's later scene changes may not be represented. Each image
  item always contributes exactly one frame.

## Operating the service

For anything beyond calling the API — the container won't start, requests
fail or hang, disk is filling up, transcription is slow, or you need to tune
whisper/OCR/place-enrichment settings or set up an authenticated Instagram
session — see [`reference/operations.md`](reference/operations.md).
