# Media Processor

Self-hosted media service that replaces a narrow Cloudinary workflow surface with controlled uploads, thumbnails, and composite-video generation.

## Language

**Asset**:
A stored uploaded media file addressable by `public_id`.
_Avoid_: File generically

**Named asset**:
An **Asset** stored under a stable semantic name for reuse in workflows.
_Avoid_: Alias only

**Composite request**:
One request to generate a video by combining existing media inputs.
_Avoid_: Render job

**Offload**:
The post-processing step that moves generated composites to object storage instead of keeping them local.
_Avoid_: Backup

**Public URL**:
The returned client-facing link for an **Asset** or generated output.
_Avoid_: Filesystem path

## Relationships

- Every uploaded item becomes an **Asset**.
- A **Named asset** is a reusable subset of **Assets**.
- A **Composite request** depends on existing **Assets** or **Named assets**.
- **Offload** changes where generated outputs live, but the **Public URL** remains the integration contract.

## Example dialogue

> **Dev:** "Why does the workflow refer to `faceintro` instead of a file path?"
> **Domain expert:** "Because `faceintro` is a **Named asset**, and the **Composite request** expects stable reusable inputs rather than ad hoc local files."

## Flagged ambiguities

- "upload" could mean storing source media or producing the final composite — resolved: use **Asset** for stored media and **Composite request** for generation.
