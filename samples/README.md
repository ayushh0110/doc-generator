# Samples

Meridian Freight is a made-up company.

## templates/

Built by `build_templates.py`, and deliberately awkward:

| File | Includes |
|---|---|
| `meridian_quarterly_report.docx` | Logo anchored to the page in the header, page-number field, custom styles and margins, shaded table with custom borders, inline picture |
| `meridian_board_deck.pptx` | Custom theme, logo and rule on the master, footers and page numbers, native chart, table, picture, speaker notes |
| `meridian_depot_budget.xlsx` | Merged title, number formats, formulas, total row, conditional formatting, data validation, named range, chart, image, frozen panes, print setup, second sheet with references into the first |

## outputs/

Built by `generate_outputs.py`. Each file is a template plus a fixed edit plan, run through the same engines and validator the app uses.

No model wrote these, so they only show what the engines preserve. `model_outputs/` has real model output.

| File | What was done |
|---|---|
| `report_rolled_forward_to_q3.docx` | Text edits, a table row, a replacement that also reaches the footer, and a Sources section |
| `new_document_from_the_same_template.docx` | Body replaced with new content, in the template's styles |
| `deck_rolled_forward_to_q3.pptx` | Text edits, chart data replaced, a slide added from a layout, sources in the notes |
| `budget_with_new_depot.xlsx` | Row added above the total, an input changed, a new sheet, a Sources sheet |

`report.json` lists every edit and validator result. `pdf/` holds renders of the templates and the outputs, for side-by-side comparison.

## model_outputs/

Real output from the running app, using Groq's `openai/gpt-oss-120b`. The file names say what was asked.

| File | Request |
|---|---|
| `report_new_title_and_subtitle.docx` | Change the title to Q3 Network Review and the subtitle to say October instead of July |
| `report_added_bullet.docx` | Follow-up: add a bullet under Where we stand saying the Hull depot opened in August |
| `report_summary_with_citation.docx` | Using my Hull notes, rewrite the Summary paragraph to mention how many consignments Hull handled. Cite the notes |
| `budget_added_hull.xlsx` | Add a depot called Hull above the total row: region East, budget 400, actual 412, status Watch |
| `deck_added_slide.pptx` | Add a new slide after the depot scorecard titled What Hull changes, with two short bullets |
| `deck_new_first_slide_title.pptx` | Follow-up: change the title of the first slide to Q3 Network Review |
| `new_document.docx` | Create a short Word document titled Team Offsite Plan with a heading, a short intro paragraph and three bullet points on logistics |
| `new_deck_from_web_research.pptx` | Search the web and make a two-slide deck on the Port of Felixstowe: what it is and how busy it is. Cite your sources |
