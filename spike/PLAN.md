# Spike: Gander port (A) vs "everything becomes PDF" via LibreOffice (B)

Throwaway measurement for the design decision of `.kiro/specs/offline-document-viewer`.
Question: which engine gives the most accurate view at acceptable speed?

## Corpus (`corpus/`, untrusted downloaded files)

| Folder | What | Count |
|---|---|---|
| docx | Apache POI test-data, real files, 15 KB–3 MB | 61 |
| pptx | same source | 30 |
| xlsx | same source | 30 |
| doc | same source | 15 |
| gander | Gander's own fixtures and sample files (its home turf) | 18 |
| pdf | Gander PDFs + PDF 32000-2008 spec (756 pages) | 6 |

Ground truth for pagination: `results/word-truth.json`, the page count Word itself recorded in
`docProps/app.xml` of 51 Word-saved .docx files.

Fonts: Liberation (Arial/Times/Courier metrics) and Carlito/Caladea (Calibri/Cambria metrics)
are installed for both engines, as the product would bundle them.

## Metrics

1. Robustness: share of files rendered without error or timeout (60 s cap).
2. Pagination: rendered page count vs Word's recorded count (exact, within ±1).
3. Text recall: share of the words in the file's own text that appear in the render.
4. Spreadsheet styling: does a file with cell fills show them.
5. Slides: slide count, elements left out (Gander's "missing" boxes).
6. Speed: time to first page, cold and warm, run sequentially by one process
   so the two engines never compete for CPU.
7. Visual: first-page screenshots side by side for a sample.

## Environment caveats

- LibreOffice 26.2.5.2 from snap (no sudo here). A .deb build starts faster, so B's cold times
  are pessimistic.
- 12 cores, 15 GB RAM with about 2 GB free: one heavy process at a time.
