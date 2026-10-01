# Implementation checklist — MBOX→EML JS frontend migration

## 0. Baseline (done)
- [x] Inspect `mbox2eml.py`, `gui.py`, `modify_eml.py`, `read_eml.py`, `test_mail_tools.py`
- [x] Run existing tests (4/4 pass)
- [x] Probe real Takeout mbox (146 msgs, 37M) for ordering/indexing behavior

## Key findings driving changes
- `convert()` aborts whole run on first per-message exception (no try/except in loop).
- Rerun silently overwrites (`open(..., "wb")`, index-only uniqueness).
- Progress is `(index, filename, total)` only — no success/failed/elapsed, no error channel.
- `len(mbox)` + full iteration holds mbox lock; preview of large mbox needs bounded inspect.
- `_sanitize_filename` NFKD→ASCII drops CJK to empty (falls back to date/message, OK, keep).
- `read_eml` only shows Subject/From/To/Date + first text/plain; no Cc/Bcc/Reply-To, HTML-only → "No body", attachments invisible, `get_content()` can raise.
- `modify_eml` has no header-name/value validation (CRLF injection possible).
- `gui.py` has no inspect/preview/stats/viewer/modify; result Listbox unbounded; no cancel.
- `message.as_bytes()` (compat32) is correct for extraction; `policy.default` re-serialization only for modify/view.

## 1. Core improvements (`mbox2eml.py`, stdlib only, 3.11 compat)
- [x] Per-message error isolation: record failure, continue; fatal (missing mbox, mkdir, `len()`) still raises
- [x] Keep `convert()` return as list for back-compat; add `error_callback`, `collision`, `should_stop` params
- [x] Add `convert_detailed()` returning report dict `{created, errors, skipped, total, succeeded, failed}` for API layer
- [x] Collision policy: `overwrite` (current) | `skip` | `rename` (`stem (2).eml`, length-aware); exposed to UI
- [x] Broaden header/date decode exception handling (`LookupError`, `AttributeError`, `OverflowError`, etc.)
- [x] Add `inspect_mbox(mbox_path, limit)` → `{size_bytes, total, messages[{index,subject,from,to,date}]}` bounded preview
- [x] Add `suggested_output_dir(mbox_path)` helper
- [x] CLI keeps working; add `--collision`, `--on-error` visibility (print failures, exit 0 if any succeeded? exit 1 only if all fatal/zero succeeded)

## 2. EML read/parse (`read_eml.py`)
- [x] Add `parse_eml(path, max_body_chars)` → JSON-safe dict `{subject,from,to[],cc[],bcc[],reply_to,date,message_id,body_text,body_html,has_html,attachments[{filename,content_type,size_bytes,disposition}], truncated, defects}`
- [x] Per-part `get_content()` guarded; charset/decode fallback via `get_payload(decode=True)` + replace
- [x] HTML-only → provide `body_html` + derived `body_text` via stdlib HTML→text fallback
- [x] Keep `read_eml()` CLI printing, extend to Cc/attachments/HTML note without breaking existing assertions

## 3. EML modify (`modify_eml.py`)
- [x] Validate header name `^[A-Za-z][A-Za-z0-9-]*$` (≤78), value: no `\r`/`\n`, ≤998 chars
- [x] Keep `read_and_modify_eml()` signature; raise `ValueError` on bad header/value, `FileNotFoundError` passthrough

## 4. Backend/API (`server.py`, stdlib `http.server.ThreadingHTTPServer` only — no framework)
- [x] Static serve `web/` + JSON API; no DB; in-memory jobs only
- [x] `POST /api/inspect {mbox_path, limit}` with path validation
- [x] `POST /api/convert {mbox_path, output_dir, collision}` → `{job_id}` (202), background thread
- [x] `GET /api/jobs/{id}` status `{status,total,processed,succeeded,failed,skipped,progress,elapsed_secs,current_filename,error}`
- [x] `GET /api/jobs/{id}/results?offset&limit` paginated `{items[{index,status,subject,filename,error}]}`
- [x] `GET /api/jobs/{id}/events` SSE streaming (polling remains via status endpoint)
- [x] `POST /api/jobs/{id}/cancel`
- [x] `GET /api/eml?job_id&filename` job-scoped, `commonpath` containment check, returns `parse_eml()` JSON
- [x] `POST /api/eml/modify {job_id,filename,header,value,output_filename?}` validated, job-scoped
- [x] Path/filename/header validation everywhere; JSON errors `{error}` with correct codes; no directory listing endpoint

## 5. Frontend (`web/`, vanilla JS, no deps/build)
- [x] Source: mbox path input, Inspect, size/total/preview table (bounded)
- [x] Destination: output input, collision select, Use-default button
- [x] Conversion: Start/Cancel, progress bar, current file, processed/total, ok/failed/skipped, elapsed, completion
- [x] Results: Status/Message/Output table, filter, pagination, click → preview
- [x] Viewer: Subject/From/To/Cc/Date + body_text `<pre>` + attachments list + HTML in `iframe[sandbox]` only
- [x] Modify: header/value inputs with validation, apply, refresh preview
- [x] Clear error banners; no `innerHTML` for untrusted content (use `textContent`, `srcdoc` only for sandboxed iframe)

## 6. Tests (do not reduce coverage; update on intentional change)
- [x] Keep existing 4 tests green (adapt `convert` return-compat as needed)
- [x] New: RFC2047, truncation, malformed dates, empty/CJK subjects, duplicate subjects unique, rerun overwrite/skip/rename
- [x] New: malformed message continues + reported; progress+error callbacks; cancel stops
- [x] New: multipart alt, HTML-only, attachments, multi-To/Cc, broken charset
- [x] New: header validation rejects CRLF/bad names
- [x] New: API layer — inspect/convert/status/results/eml/modify validation + containment (no HTTP flakiness: test job manager + handlers directly, plus one live HTTP smoke)

## 7. Remove Tkinter + final review
- [x] Delete `gui.py`, remove dead imports; core stays CLI-usable (`mbox2eml.py`, `read_eml.py`, `modify_eml.py`, `server.py --serve`)
- [x] Run full suite + live server smoke (inspect→convert→results→eml→modify via real HTTP)
- [x] Review diff: no dead code, no duplicated sanitizers, no FS escape, no unsafe HTML render, no new deps

## 8. Final hardening + date search (done, stdlib only, no redesign)
- [ ] Baseline: 50/50 suite green + Takeout smoke re-verified
- [ ] Attachment memory audit: measure 10/50/250MB fixtures (time, peak RSS, materialization)
- [ ] Attachment metadata without full decode where stdlib allows; keep counts/names/types/sizes exact
- [ ] HTML preview audit: mixed-case, entities, encoded schemes, SVG, CSS url(), srcset/poster, malformed tags; fail closed
- [ ] Decide `data:` URL policy (retain `data:image/*` capped, block executable schemes); cap large data URLs
- [ ] Date search backend: `date_from`/`date_to` inclusive `YYYY-MM-DD`, UTC instant semantics, missing/malformed never match range
- [ ] Result items carry normalized date (`date`, `date_ts`); no EML re-parse per query
- [ ] Results UI: From/To + Clear, server-side, pagination-safe, `date_from > date_to` clean error
- [ ] Tests: attachments, HTML bypasses, 13 date-filter cases via HTTP
- [ ] Regression: full suite + Takeout smoke (search, date filter, view, HTML, modify, download, SSE/poll, cancel, atomic collisions, spill, eviction, traversal, no tracebacks)

## 9. Pick File primary selection (done)
- [x] POST /api/uploads streams raw bytes to private staging (chunked, never fully buffered)
- [x] .mbox extension + From-line magic validation, empty/oversize (413)/bad-type rejected
- [x] Opaque upload_id tokens (no paths); inspect/convert accept upload_id or mbox_path
- [x] Pick MBOX button primary with progress + cancel; drag-and-drop reuses same pipeline
- [x] Manual server path kept as fallback; server-provided default output for uploads
- [x] Staged-file age cleanup; traversal/format validation; 7 upload tests

## 10. Output folder picker (done)
- [x] Scoped server browser: GET /api/browse (home-rooted, folders only, no dotfiles/symlinks/file reads, 500 cap)
- [x] POST /api/browse/mkdir single-level validated creation
- [x] Native dialog UI: navigate, Up, create, Select/Cancel, Esc, focus states
- [x] realpath containment enforced; 3 browse tests (navigation, escape/symlink, mkdir)
