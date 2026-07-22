# Odoo Gmail Draft Assistant

A lightweight local tool for preparing outbound Odoo follow-up emails as real Gmail drafts. Odoo remains the internal CRM and source of truth; this app exists to help each salesperson safely turn an Odoo export into reviewed Gmail drafts, then export update notes back to Odoo.

It runs on `localhost`, stores data in a local SQLite file, and creates Gmail drafts using IMAP plus a Google App Password. It does not send emails.

## Run

For most teammates:

```text
Linux ThinkPad: open Terminal in this folder and run sh start-crm.sh
Windows: double-click start-crm.bat
Mac: open Terminal in this folder and run sh start-crm.sh
```

Keep the launcher window open while using the app.

If the browser does not open automatically, manually open `http://127.0.0.1:8766`.

Mac one-time auto-start setup:

```bash
sh install-mac-autostart.command
```

After that, the CRM starts when the Mac logs in and restarts if the local server quits. If the laptop was shut down or restarted, open `http://127.0.0.1:8766` after logging in.

Mac auto-start uses a local running copy here:

```text
~/Library/Application Support/Odoo Gmail Draft Assistant
```

That avoids macOS privacy blocks on background apps inside Documents or Downloads. The installer copies the current `crm.db` the first time so existing leads come with it. After pulling a GitHub update, run `sh install-mac-autostart.command` again to refresh the auto-start copy without overwriting its existing database.

To remove Mac auto-start:

```bash
sh uninstall-mac-autostart.command
```

If Mac says **start-crm.command Not Opened**, click **Done** and use Terminal instead:

```bash
sh start-crm.sh
```

If Linux says the command is not found, make sure they are in the unzipped app folder and run:

```bash
sh start-crm.sh
```

If Linux says Python is missing, install Python 3 through the software center or ask IT to install `python3`.

If Windows says the site cannot be connected, keep the black terminal window open and refresh the browser after a few seconds. If the terminal says Python is missing, install Python for Windows and check **Add python.exe to PATH** during install.

To update later after your manager publishes a new version:

```text
Linux ThinkPad: open Terminal in this folder and run sh update-crm.sh
Windows: double-click update-crm.bat
Mac: open Terminal in this folder and run sh update-crm.sh
```

Updates pull the newest app files from GitHub. They do not upload or overwrite the teammate's local `crm.db` data.

Manual start:

```bash
CRM_PORT=8766 python3 crm.py
```

Open:

```text
http://127.0.0.1:8766
```

Optional environment variables:

```bash
CRM_PORT=8787 python3 crm.py
CRM_DB=my-crm.db python3 crm.py
```

On startup, the app backs up the current database into `backups/`.

## First-Time Setup

Click **Setup** or **How to use** in the app. Setup walks through:

```text
Gmail settings
Gmail test draft
CSV template/import
Emails for today
Odoo update CSV
```

Use **Health** to test Gmail, create a backup, restore a backup, and open troubleshooting.

Use **Simple mode** for colleagues who only need the core workflow. Advanced buttons are hidden until they switch back.

## Intended Workflow

1. Export leads or opportunities from Odoo.
2. Optional but recommended: click **LLM research**, choose Claude, Gemini, or GPT, paste the prompt and Odoo list into that tool, and have it return the enriched CSV.
3. Use **Import Odoo CSV** in this app.
4. Click **Emails for today**.
5. Review the auto-tailored queue and click **Draft to Gmail**.
6. Review and send the drafts from Gmail.
7. Use **Odoo update CSV** to bring draft notes, next activity dates, and email sequence status back into Odoo.

The normal sales-rep workflow should be nearly no-touch. When **Emails for today** opens, the app automatically fills missing reply angles, CTAs, first lines, pain points, value angles, proof points, and lead-specific email sequences from the Odoo/imported lead context. Reps can still edit a lead, but they should not need to manually personalize every email.

## Import From Odoo

Use **Import Odoo CSV** in the top bar. Recognized columns include both simple names and common Odoo export names:

Minimum useful CSV:

```text
company, industry, email
```

`name` or `Company` can be used instead of `company`. With only those fields, the app writes a safe industry-based Odoo pitch using the relevant Odoo apps and workflow patterns. More context is better, but it is not required.

During import, choose whether the file contains:

```text
Active/new leads
Odoo lost leads to re-engage
```

Odoo lost imports stay draftable, but they are labeled as prior Odoo lost/demo leads so reps can use recovery-style outreach.

```text
company, Company, Customer, Opportunity, Opportunity Name
contact, Contact, Contact Name, Customer Contact
title, Job Position, Function
email, Email, Email Address, Direct Email
phone, Phone, Mobile
website, Website, URL
city, state, State/Province
industry, Tags, Odoo Tags
source, Lead Source, Campaign
stage, Pipeline Stage
next_action, Next Activity, Next Activity Deadline
notes, Internal Notes, Description
pain_points, industry_holes, value_angle, proof_points
sequence_angle, custom_first_line, reply_goal, cta_style, reply_cta
```

The importer dedupes by company name and skips any company or email currently on the suppression list.

## LLM-Enriched Import

Use **LLM research** in the app, or open `CLAUDE_LEAD_ENRICHMENT_PROMPT.md`, `GPT_LEAD_ENRICHMENT_PROMPT.md`, `GEMINI_IMPORT_PROMPT.md`, or `GEMINI_DEEP_RESEARCH_PROMPT.md`. The prompts tell Claude, GPT, or Gemini to turn a raw Odoo export or pasted lead list into an import-ready CSV with:

```text
company summary
why now
workflow hypothesis
research evidence
source URL
confidence
do-not-claim guardrails
reason to believe
role lens
claim safety
customer language
reply-type goal
sub-industry
likely/current software stack
pain points
workflow gaps
Odoo value angle
proof points
sequence angle
custom first line
reply goal
CTA style
reply CTA
```

The importer reads those columns directly. If Gemini leaves something blank, the app still auto-tailors missing fields when **Emails for today** opens.

For best account-research quality, use a source-backed/deep-research mode in batches of about **25 accounts**. Use larger batches only when the Odoo export already has strong websites/domains and you are comfortable reviewing the output before import.

## Gmail Drafts

This app creates real messages in Gmail's Drafts folder. It does not send emails.

1. Turn on 2-Step Verification for the Gmail account.
2. Create a Google App Password.
3. Open **Settings**.
4. Enter the Gmail address, sender name, and App Password.
5. Edit the sequence templates and signature.

Use **Emails for today** to review and create a batch for eligible leads. Eligible leads must be active, have a direct non-generic email, have an industry for safe Odoo positioning, pass basic validation, not be suppressed, not be complete in the sequence, and not already have a draft created today. Contact names, website research, and software stack help, but are optional.

The preview includes an **Email template** picker. Built-in options include the tailored default, a coworker-proven direct template, a short price/consolidation template, a vertical gap audit comparison template, and an Odoo lost-lead reactivation template. Set the default template in **Settings**. You can also change the template on an individual email card before drafting, which lets reps mix angles in one Gmail batch.

## Workbench Queues

The top workbench gives colleagues a fast starting point:

```text
Ready to draft    direct email, safe domain, next email due
Needs cleanup     missing direct email, industry, or safe email status
Due today         follow-ups due now
No next step      active leads that need an Odoo activity
```

Clicking a queue narrows the visible list.

## Quality-Gated Drafting

**Emails for today** now opens a **Draft Preview** first. Before the preview appears, the app auto-tailors each candidate lead. The preview shows:

```text
company
email step
personalization score
subject
body preview
warnings
ready/not ready status
```

Only ready rows are drafted. Quality gates live in **Settings**:

```text
minimum personalization score
require industry
optional contact-name requirement
optional custom-first-line requirement
optional software-stack requirement
```

This lets a teammate review the batch before any real Gmail drafts are created. The final button is **Draft to Gmail**.

## Sequence Library

The lead email editor includes a **Sequence preset** picker. Built-in presets include:

```text
Odoo renewal displacement
Missed follow-up leakage
QuickBooks spreadsheet sprawl
Wrong-person routing
```

Applying a preset updates the lead's sequence angle, CTA style, reply CTA, and email templates. The templates still use merge fields like `{custom_first_line}` and `{reply_cta}` so each lead can stay personalized.

## Team Playbook

Use **Playbook** or **Settings → Export playbook** to share reusable outreach setup with colleagues. The playbook includes:

```text
email templates
sequence presets
quality gate settings
signature
daily target
```

It does **not** include Gmail credentials or lead data.

Use **Import playbook** to load another teammate's playbook JSON.

## Weekly Coaching Export

Use **Weekly coaching** to export a CSV summary for team review:

```text
active leads
drafts created in the last 7 days
replies logged
positive/booked replies
opt-outs
overdue follow-ups
leads needing cleanup
breakdowns by sequence angle, CTA style, and industry
recommended cleanup focus
```

## Reply Optimizer

Each lead has a **Reply Optimizer** panel in the side drawer. Use it before drafting to improve reply odds:

```text
Sequence angle       Operational pain, software renewal, missed follow-up, competitor replacement, etc.
Custom first line    A short lead-specific opener
Reply goal           What kind of response you want
CTA style            Soft question, send info, wrong person, permission, direct, or breakup
Reply CTA            The actual low-friction ask used in the email
Outcome              Positive reply, neutral reply, booked, wrong person, no reply, etc.
```

The panel shows a personalization readiness score. A simple lead can draft with company, industry, and direct email. Higher scores mean the lead has richer context for a stronger draft: contact, sub-industry, software stack, first line, pain point, value angle, source-backed evidence, and CTA.

The following merge fields can be used in templates:

```text
{sequence_angle}
{custom_first_line}
{reply_goal}
{cta_style}
{reply_cta}
{reason_to_believe}
{role_lens}
{claim_safety}
{customer_language}
{reply_type_goal}
{personalization_score}
```

The generated Odoo-focused email sequence uses `{custom_first_line}` and `{reply_cta}` so teammates can tune the reply angle without rewriting the whole email.

## Email Sequence

Settings includes a default sequence editor. Choose how many emails you plan to send, then edit each default step:

```text
Email 1, Email 2, Email 3, Email 4, Email 5, Email 6, ...
```

Each lead also has its own opportunity-level email editor. Open a lead and fill in:

```text
industry
sub-industry
software used / likely stack
software renewal date
pain points
industry holes
value angle
proof points
notes
```

Then use **Build tailored emails** inside the opportunity. The app generates a lead-specific sequence from local industry playbooks and frames the email around Odoo workflow gaps such as stage aging, quote follow-up, Inventory, Manufacturing, Field Service, Project, Accounting, POS, eCommerce, Helpdesk, Subscriptions, and Marketing Automation. No web scraping or enrichment is performed.

Each lead tracks its current email number and planned total. When a draft is created, the app advances the lead to the next email step and schedules the next email follow-up 3 business days later.

Every template automatically receives an unsubscribe line.

## Odoo Update CSV

Use **Odoo update CSV** after drafting. It exports:

```text
Company
Contact
Email
Odoo Suggested Note
Last Drafted
Email Drafted
Next Email Step
Suggested Next Activity
Suggested Next Activity Date
Stage Suggestion
Sequence Angle
CTA Style
Reply CTA
Reply Outcome
Personalization Score
Opt Out Safe
```

This gives each colleague a clean way to paste or import status back into Odoo without treating this tool as the CRM of record.

## Lost, Remove, And Opt-Out

**Mark Lost** keeps the lead locally, moves it to `Lost`, and requires a reason. Lost leads are excluded from email batches.

**Opt-out** adds the company and email to the suppression list, removes it from the active local queue, and blocks future drafts and re-imports for that company/email.

Use **Suppression** to search suppressed records and restore a record if it was added by mistake.

## Files

```text
crm.py           # launcher; double-click/run this file through the scripts
crm_app_source/  # compressed source payload for the app
start-crm.bat    # Windows / ThinkPad start file
update-crm.bat   # Windows / ThinkPad update file
CLAUDE_LEAD_ENRICHMENT_PROMPT.md
GPT_LEAD_ENRICHMENT_PROMPT.md
GEMINI_IMPORT_PROMPT.md
GEMINI_DEEP_RESEARCH_PROMPT.md
crm.db           # local database, ignored by Git
backups/         # automatic database backups, ignored by Git
```
