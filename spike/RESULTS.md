# Spike results (2026-10-17): A = Gander JS in Chromium, B = LibreOffice -> PDF/HTML + MuPDF

Corpus and method: see PLAN.md. Raw data: results/*.json, results/*.jsonl, results/compare-summary.txt.
Grader: compare.py (truth words taken from each file's own XML).

## Accuracy

| Metric | A | B |
|---|---|---|
| Word: share of the file's words shown (n=40) | 82.8% (12 files < 90%) | 95.8% (5 files < 90%) |
| Word: page count equals Word's own (n=48) | 94% | 94% |
| Excel: workbooks whose cell colours show (n=14 with colours) | 0 | 14 |
| PowerPoint: share of words shown (n=21) | 95.5% | 95.3% |
| PowerPoint: elements left out | 16 in 8 of 33 decks | none reported |
| Excel: words shown, B measured on PDF export (n=19) | 100% | 90.6% (PDF cuts wide sheets; B's grid view is HTML) |
| Opens (all 149 non-PDF files, 60 s cap) | 133 | 131 (+5 more in page-1 mode) |
| .odp / .odg / legacy .ppt | no renderer | yes |

Visual check, line-chart.pptx: A's chart has a corrupted axis label and no title; B matches PowerPoint.

## Speed, time to first page (best of 2, same machine, sequential runs)

| File | A warm | B warm, page 1 first |
|---|---|---|
| Field Survey Report.docx | 0.48 s | 0.41 s |
| word-pages.docx | 0.51 s | 0.29 s |
| drawing.docx (20 pages) | 0.64 s | 2.24 s |
| Willowmere Kickoff.pptx | 0.94 s | 2.63 s |
| Q3 Operating Budget.xlsx | 0.32 s | 0.58 s |
| PDF 756 pages (MuPDF) | - | 0.05 s warm, 0.25-0.35 s cold |

B cold (fresh LibreOffice per file): 7-16 s. B engine start, once per app launch: 7-10 s.

## Caveats

- LibreOffice is the snap build with ~100 language packs; every start parses 72 MB of config. A trimmed
  build (en + ru) should start faster: not measured.
- Machine under memory pressure (swap 100% used, ~2 GB RAM free) during all runs.
- A's Word page counts partly reuse Word's own recorded breaks (the answer key) for 10 of 48 files.
