# Visual findings for `SKM_C250i26070816150.pdf`

## Pages reviewed

The following pages were visually inspected after raw detection was run with the existing module and no code changes.

| Page | Observation |
|---|---|
| 8 | Photo page labeled `Original` with overlaid `Group 1`; no visible ship ticket number. Several other numeric strings appear on graded-note slabs and note faces, but they are not ticket markers. |
| 9 | Photo page labeled `Original` with overlaid `Group 2`; no visible ship ticket number. Contains other numeric strings on notes/slabs that are not ticket markers. |
| 10 | Photo page labeled `Original` with overlaid `Group 3`; no visible ship ticket number. Contains other numeric strings on notes/slabs that are not ticket markers. |
| 11 | Photo page labeled `Original` with overlaid `Group 4`; no visible ship ticket number. Contains other numeric strings on notes/slabs that are not ticket markers. |
| 13 | Sticker in top-right clearly shows `299198`. Detection returned `299188`, so this page appears to contain a one-digit misread. The sticker also includes non-ticket fields such as `CID: 475545` and `26 Oct HK`, which should be ignored as non-ticket strings. A crossed-out handwritten name appears across the top, which is not a ticket number. |

## Detection behavior notes

- The current detector correctly ignored non-ticket numeric strings on visually inspected photo pages 8–11.
- The current detector also appears to ignore the crossed-out handwritten name on page 13, which is correct.
- The main raw detection concern on the visually inspected pages is page 13, where `299198` appears to have been read as `299188`.

Source: direct visual inspection of rendered PNG pages from `/home/ubuntu/upload/SKM_C250i26070816150.pdf`.
