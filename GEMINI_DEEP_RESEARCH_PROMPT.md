# Gemini Deep Research Prompt For Odoo Account Research

Use this prompt only in Gemini Deep Research mode. This is for real account research, not simple formatting.

## Goal

Turn an Odoo export or pasted lead list into research-backed account context that imports into the Odoo Gmail Draft Assistant.

Deep Research must inspect sources before writing personalization. Company-name guessing is not enough.

## Batch Size

- Default batch: 25 accounts.
- Process every account row provided.
- Do not stop at the first 10 rows.
- If the batch is too large for Deep Research, ask the user to split it before producing output.

## Output Format

Return two sections only:

1. `READABLE_REVIEW`: a compact markdown table for humans.
2. `IMPORT_CSV`: a fenced `csv` code block for the app.

Your final answer must begin exactly with:

`READABLE_REVIEW`

Do not include an executive summary, research report, industry analysis, macro trends, methodology, Python, pandas, tool logs, debug output, analysis notes, or dataframe previews.

## Research Instructions

For each account:

- Open the company website/domain first.
- Inspect likely pages: About, Services, Products, Industries, Solutions, Catalog, Projects, Careers, Case Studies, Support, or Locations.
- Use credible public sources only if the website is unavailable or too thin.
- Capture one concrete source-backed fact.
- Prefer specific workflow evidence over generic company descriptions.
- Every Medium or High confidence row must include a source URL.
- If no source can be inspected, mark Low confidence and use cautious wording only.

## READABLE_REVIEW

Use exactly these columns:

| Company | Contact | Evidence | Source URL | Workflow diagnosis | First line | Verify next | Confidence |

Rules:

- Evidence must be a real fact from a source, not just company name/domain.
- Workflow diagnosis should be 25-45 words.
- Workflow diagnosis must name the likely process, where the handoff breaks, and the Odoo area that could matter.
- First line should be usable in an email and should not overclaim.
- First line should sound like a real rep, not a consultant report. Avoid flattery like `incredible`, `impressive`, `tremendous`, `exciting`, or `highly innovative`.
- Verify next should name the missing check, such as current software, role, service area, Odoo history, workflow owner, or app stack.

## Confidence Rules

- High: source URL plus a specific fact from the company website.
- Medium: credible public source or strong Odoo-row evidence beyond name/domain.
- Low: no source, ambiguous company, inferred vertical, or weak evidence.

Never mark `Company name`, `Inferred from name`, `Inferred from domain`, `Domain name`, or `Odoo Export` as Medium or High by itself.

## Import CSV Headers

Use exactly these headers, in this order:

```csv
company,contact,title,email,email_generic,phone,website,city,state,industry,sub_industry,software_used,software_renewal_date,source,stage,notes,odoo_evaluation_version,odoo_evaluation_date,odoo_evaluation_year,previous_demo_notes,previous_blocker,apps_evaluated,lost_category,lost_reason,company_summary,why_now,workflow_hypothesis,research_evidence,research_source_url,confidence,do_not_claim,reason_to_believe,role_lens,claim_safety,customer_language,reply_type_goal,pain_points,industry_holes,value_angle,proof_points,sequence_angle,custom_first_line,reply_goal,cta_style,reply_cta,next_action,next_action_date
```

## Field Quality Rules

- `workflow_hypothesis`: 25-45 words. Explain the likely operational handoff.
- `value_angle`: 25-45 words. Explain how Odoo could connect that workflow and why it matters.
- `research_evidence`: one concrete fact from the source.
- `research_source_url`: source URL. Leave blank if no real source.
- `confidence`: High, Medium, or Low.
- `custom_first_line`: email-ready, under 170 characters, source-backed when specific.
- `do_not_claim`: list anything inferred or unsafe to claim.
- `proof_points`: source-backed proof or Odoo-row evidence. Required for Medium/High.
- `reply_cta`: light reply-first question.

## Valid CSV Rules

The `IMPORT_CSV` block must be valid RFC 4180 CSV.

- Every data row must have exactly the same number of columns as the header.
- Do not include line breaks inside any CSV cell.
- Escape double quotes inside cells by doubling them. Example: `The ""No Surprises"" Guarantee`.
- Wrap any field containing commas, quotes, or semicolons in double quotes.
- Leave unknown fields blank, but keep the comma position.
- Do not use smart quotes.
- Do not add extra commas at the end of rows.
- Do not output a raw CSV file separately; put the import CSV only inside one fenced `csv` block.

Before finalizing, silently self-check:

- Header column count equals every row column count.
- Number of CSV data rows equals number of input accounts.
- No row is collapsed into another row.
- No cell contains an unescaped quote.

If you cannot pass this CSV self-check, return `CSV_VALIDATION_FAILED` and explain the issue in one sentence. Do not return malformed CSV.

## Safety Rules

- Do not invent direct emails.
- Do not invent software stacks.
- Do not invent renewal dates.
- Do not invent prior Odoo history.
- Do not say `Saw` unless the source supports it.
- Avoid overpraise and hype. Prefer plain observed facts over compliments.
- Avoid consulting-report language in email fields, such as `architectural friction`, `operational pathologies`, `unified architecture`, or `ecosystem` unless the account context truly requires it.
- If source lookup fails, use Low confidence and softer wording.
- Do not repeat the same workflow diagnosis across many accounts.
- Do not use filler phrases like `teams often struggle`, `disconnected tools`, `gets messy`, or `manual re-entry between systems`.

Here is the lead list to research and convert:

[PASTE ODOO EXPORT OR LEAD LIST HERE]
