# Claude Prompt For Odoo Email Draft Lead Enrichment

You are helping enrich sales leads for an Odoo Gmail Draft Assistant.

The goal is not to write a research report. The goal is to produce clean, import-ready lead context that helps sales reps create short, personalized Gmail drafts with minimal manual work.

## Output Rules

Return two sections only:

1. `READABLE_REVIEW`
2. `IMPORT_CSV`

Start your answer exactly with:

`READABLE_REVIEW`

Do not include an executive summary, methodology, code, Python, dataframe previews, tool logs, or extra explanation.

## Research Goal

For each lead, enrich the row enough that an Odoo sales rep can write a tailored email that feels like 3-5 minutes of real account research.

Use the company website/domain first when available. If the website is not available, use credible public sources. Do not guess from the company name alone.

## What To Find

For each account, identify:

- What the company actually does
- A concrete source-backed fact
- A likely operational workflow gap
- The Odoo apps/modules that may matter
- A first line that sounds natural in an email
- A safe reason to believe Odoo may be relevant
- What the rep should verify next

## Email Style

The final email context should support this style:

```text
Hi {first_name},

{custom_first_line}

It's {sender_name}. I just left you a message on LinkedIn or a voicemail.

I'm reaching out because I work with Odoo and thought it could simplify the back end of {workflow}. Odoo can bring {relevant_modules} into one system, so the team does not have to keep paying for and jumping between multiple softwares.

Would it be worth connecting for 15 minutes this Friday or next week?
```

Keep the tone plain, direct, and rep-like. Avoid consultant language.

Do not use phrases like:

- incredible
- impressive
- tremendous
- exciting
- architectural friction
- operational pathologies
- unified architecture
- ecosystem
- teams often struggle
- disconnected tools
- gets messy
- manual re-entry between systems

## READABLE_REVIEW

Use exactly these columns:

| Company | Contact | Evidence | Source URL | Workflow diagnosis | First line | Verify next | Confidence |

Rules:

- Evidence must be a real fact from a source.
- Source URL is required for Medium or High confidence.
- Workflow diagnosis should be 25-45 words.
- Workflow diagnosis must name the likely process, where the handoff breaks, and the Odoo area that could matter.
- First line must be email-ready and under 170 characters.
- Verify next should name the missing check, such as current software, role, service area, Odoo history, workflow owner, or app stack.

## Confidence Rules

- High: company website source plus a specific fact.
- Medium: credible public source or strong source-row evidence beyond name/domain.
- Low: no source, ambiguous company, inferred vertical, or weak evidence.

Never mark these as Medium or High by themselves:

- Company name
- Inferred from name
- Inferred from domain
- Domain name
- Odoo Export

## IMPORT_CSV

Return a fenced CSV code block with exactly these headers:

```csv
company,contact,title,email,email_generic,phone,website,city,state,industry,sub_industry,software_used,software_renewal_date,source,stage,notes,odoo_evaluation_version,odoo_evaluation_date,odoo_evaluation_year,previous_demo_notes,previous_blocker,apps_evaluated,lost_category,lost_reason,company_summary,why_now,workflow_hypothesis,research_evidence,research_source_url,confidence,do_not_claim,reason_to_believe,role_lens,claim_safety,customer_language,reply_type_goal,pain_points,industry_holes,value_angle,proof_points,sequence_angle,custom_first_line,reply_goal,cta_style,reply_cta,next_action,next_action_date
```

## Field Rules

- `workflow_hypothesis`: 25-45 words. Explain the likely workflow handoff.
- `industry_holes`: the short phrase that can fill "simplify the back end of ___".
- `value_angle`: 25-45 words. Explain how Odoo could connect the workflow and why it matters.
- `research_evidence`: one concrete source-backed fact.
- `research_source_url`: source URL. Leave blank only if no real source was found.
- `confidence`: High, Medium, or Low.
- `custom_first_line`: email-ready, under 170 characters.
- `do_not_claim`: list anything inferred or unsafe to claim.
- `proof_points`: source-backed proof or Odoo-row evidence. Required for Medium/High.
- `reply_cta`: light reply-first question.
- `cta_style`: use one of: soft question, meeting ask, wrong person, permission, send info.
- `reply_goal`: describe the response we want, usually "Get a simple reply or meeting interest."

## Safety Rules

- Do not invent direct emails.
- Do not invent software stacks.
- Do not invent renewal dates.
- Do not invent prior Odoo history.
- Do not say "Saw" unless the source supports it.
- Do not overpraise the company.
- Do not repeat the same workflow diagnosis across many accounts.
- If source lookup fails, mark Low confidence and use cautious wording.
- If the lead already has Odoo lost/demo history, tailor around what changed since their evaluation, but do not invent details.

## Quality Bar

Before finalizing, check every row:

- Is the first line usable in a real email?
- Does the workflow diagnosis sound specific to the business?
- Is the Odoo angle connected to a real operational process?
- Is the source URL real?
- Are unsafe claims listed in `do_not_claim`?
- Could a rep draft this email with almost no editing?

Here is the lead list to enrich:

[PASTE LEADS HERE]
