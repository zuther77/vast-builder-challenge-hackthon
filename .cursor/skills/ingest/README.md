# Ingest (vss2)

Two paths:

- **Re-ingest** — re-run detector → reasoner → embedder → writer on content already
  in the team's indexed archive (hackathon default).
- **Upload** — send a new local video through `POST /api/v1/videos/upload` into the
  chunks bucket so the full pipeline (including segmenter) can process it.

## Skills

| Skill | Mechanism | Purpose |
|-------|-----------|---------|
| [upload-video](upload-video/SKILL.md) | `POST /api/v1/videos/upload` | Multipart upload of a new video file + ingest metadata |
| [reingest-videos](reingest-videos/SKILL.md) | `POST /api/v1/dashboard/reingest` | Select an indexed video/stream, choose prompt and metadata behavior, choose latest complete chunks, and monitor replacement |
| [reingest-chunk](reingest-chunk/SKILL.md) | Explore/search → `POST /api/v1/dashboard/reingest` | Find one specific chunk from a filename, metadata, date, or scene description, then re-ingest only that Explore card |

Re-ingest copies existing segment objects to unique S3 keys and atomically replaces
each matching VastDB segment slot. Upload writes a new object to the chunks bucket;
the DataEngine pipeline segments and indexes it from scratch.

Auth is a backend JWT from `POST /api/v1/auth/login`.
Team URL and credentials come only from the single `/config/*.config` file on
the VM. Skills must not search the repository's `team-configs/`.
