# media-processor

Self-hosted Cloudinary replacement for the "Lead Interested - Create Video" n8n workflow.
Built with FastAPI + FFmpeg. Handles file uploads, thumbnail extraction, and composite video creation.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/upload` | Upload a file. Returns `{public_id, url, secure_url}`. Form fields: `file` (required), `folder` (optional), `name` (optional — store as named asset). |
| `GET` | `/thumb/{public_id}.jpg` | Extract a JPEG thumbnail. Query params: `w`, `h`, `c` (fill/crop/scale), `g` (north/south/center…), `t` (seek seconds). |
| `POST` | `/composite` | Create composite video (intro + splice). See body schema below. |
| `DELETE` | `/asset/{public_id}` | Delete an uploaded file and its thumbnail cache. |
| `GET` | `/files/{path}` | Serve a raw uploaded file. |
| `GET` | `/health` | Health check. |

All write endpoints require `X-Api-Key: <API_KEY>` header when `API_KEY` env var is set.

### POST /composite body

```json
{
  "base_id": "<public_id of screenshot>",
  "overlay_name": "faceintro",
  "splice_name": "video",
  "overlay_w": 300,
  "overlay_h": 300,
  "overlay_x": 60,
  "overlay_y": 60,
  "output_w": 1280,
  "output_h": 720
}
```

**Output**: screenshot looped for the duration of `faceintro` (with circular face overlay in SE corner), then `video` appended.

## Setup

### Environment variables

| Var | Default | Description |
|-----|---------|-------------|
| `BASE_URL` | `https://media.luxeillum.com` | Public base URL for file links |
| `UPLOAD_DIR` | `/data/uploads` | Where files are stored |
| `API_KEY` | *(empty = no auth)* | Secret for write endpoints |

### Pre-load named assets (one-time)

Upload the `faceintro` and `video` assets once before the workflow runs:

```bash
# faceintro.mp4 — circular face overlay video
curl -X POST https://media.luxeillum.com/upload \
  -H "X-Api-Key: <API_KEY>" \
  -F "file=@faceintro.mp4" \
  -F "name=faceintro"

# video.mp4 — main video to splice after the intro
curl -X POST https://media.luxeillum.com/upload \
  -H "X-Api-Key: <API_KEY>" \
  -F "file=@video.mp4" \
  -F "name=video"
```

## n8n workflow changes

Replace these nodes in the "Lead Interested - Create Video" workflow:

### Upload Screenshot / Upload Video (Cloudinary nodes → HTTP Request)
- Method: `POST`
- URL: `https://media.luxeillum.com/upload`
- Auth: Header `X-Api-Key`
- Body: Send as Form Data — `file` = binary input, `folder` = `prospect` (for video)
- Response map is identical: `.public_id`, `.url`

### Cloudinary transformation (HTTP Request → HTTP Request)
- Method: `POST`
- URL: `https://media.luxeillum.com/composite`
- Body (JSON):
  ```json
  {
    "base_id": "{{ $('Upload Screenshot').item.json.public_id }}",
    "overlay_name": "faceintro",
    "splice_name": "video"
  }
  ```

### Delete Screenshot Asset (HTTP Request → HTTP Request)
- Method: `DELETE`
- URL: `https://media.luxeillum.com/asset/{{ $('Upload Screenshot').item.json.public_id }}`
- Auth: Header `X-Api-Key`

### Thumbnail URLs (in PDFMonkey and Edit Fields nodes)

| Old (Cloudinary) | New |
|---|---|
| `https://res.cloudinary.com/djo2waiya/video/upload/so_0,f_jpg,q_auto,w_1280,h_400,c_crop,g_north/{id}.jpg` | `https://media.luxeillum.com/thumb/{id}.jpg?w=1280&h=400&c=crop&g=north&t=0` |
| `https://res.cloudinary.com/djo2waiya/video/upload/so_0,f_jpg,q_auto,w_1280,h_720,c_fill/{id}.jpg` | `https://media.luxeillum.com/thumb/{id}.jpg?w=1280&h=720&c=fill&t=0` |
