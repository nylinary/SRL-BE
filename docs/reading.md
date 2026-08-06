# Incremental Reading

A SuperMemo-style incremental-reading sub-app grafted onto the card system. You import a
big text, read it, lift the meaningful parts into **extracts** (which nest arbitrarily),
and finally turn an extract into a flashcard whose answer defaults to the extract's text.
Learning those cards happens in the normal spaced-repetition flow.

Backend module: `reading.py`. Model: `gitbook.models.ReadingItem`. Endpoints under
`/api/reading/*` (see [api-reference.md](api-reference.md)).

## The tree

`reading_items` is a per-user, self-referential tree:

- **Document** — `kind="document"`, `parent_id=NULL`, `source_kind` in `text`/`pdf`. Holds
  the imported text in `content`.
- **Extract** — `kind="extract"`, `parent_id` = the item it was lifted from. Extracts can
  parent further extracts, so the hierarchy is unbounded (document → extract → sub-extract → …).

`GET /api/reading/tree` returns the caller's items as a **flat list** ordered by `position`
(a monotonic creation timestamp); the client assembles the parent/child tree. Full text is
fetched per-item via `GET /api/reading/items/{id}` so the tree stays light.

Ownership is enforced on every operation — `get`/`update`/`delete`/`extract` return
`None`/`404` across users. `delete` removes the item **and its whole subtree** (walked in
`ReadingRepository.delete`) and returns the count removed.

## Importing text

`parse_upload(filename, data) -> (title, text, source_kind)` (`reading.py`):

- **PDF** — chosen when the name ends `.pdf` or the bytes start with `%PDF-`. Text is
  extracted page-by-page with `pypdf` on a **best-effort** basis (a scanned/image-only PDF
  yields empty text but is still accepted — it's rendered visually, see below). The upload
  endpoint stores the **original bytes** in `reading_blobs` for that document.
- **Text** — chosen when the name ends `.txt`/`.md` or the bytes "look like text" (no NUL in
  the first 2 KB). Decoding tries UTF-16 **only** with a BOM, then `utf-8 → cp1251 →
  latin-1` (cp1251 before latin-1 so Cyrillic files decode correctly), falling back to
  UTF-8 with replacement.
- Anything else raises `ReadingError` → the endpoint returns `400`.

Uploads are capped at 20 MB. Pasted text skips parsing entirely
(`POST /api/reading/documents`). A text document's `content` is split on blank lines into
paragraphs when rendered to the client (`main._text_doc`).

## Viewing a PDF as-is

A PDF document is **rendered, not transcribed**: the client fetches the raw bytes from
`GET /api/reading/items/{id}/file` (`application/pdf`) and renders the actual pages with
pdf.js (see the frontend `ReadingPage`/`PdfViewer`). pdf.js draws each page to a canvas with
a transparent, selectable **text layer** on top, so selecting a passage and choosing
**Extract** / **Make card** works over the real PDF exactly as it does over plain text — the
resulting extract is stored as text and nests like any other. Extracts themselves are always
text (only the top-level document is a PDF).

## Making a card

`POST /api/reading/items/{id}/card` with `{question, answer?, theme?, subtheme?, tags?}`
creates a normal card via `CardRepository.create`:

- `question`/`answer` are **ProseMirror docs** — the client uses the same rich-text editor
  (with formatting tools) as normal card creation. Plain strings are still accepted and
  wrapped into paragraphs, for callers that don't build docs.
- `answer` **defaults to the extract's `content`** (as paragraphs) when omitted or empty.
  The client seeds the answer editor with it via `textToDoc`.
- `source_extract_id` is stored on the card so it can be traced back to its extract.
- The daily card limit applies (**`429`** when exceeded), same as `POST /api/cards`.

## Tests

`tests/test_reading.py` — parsing checks are DB-free; the repository checks target
`TEST_DATABASE_URL` (name must contain "test") and SKIP if no PostgreSQL is reachable.
Covers txt/cp1251 decode, binary rejection, document/extract/sub-extract creation,
auto-titling, cross-user isolation, rename, and cascade delete.
