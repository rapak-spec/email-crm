#!/usr/bin/env python3
import base64
import csv
import html
import imaplib
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
from email.message import EmailMessage
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


APP_NAME = "Odoo Gmail Draft Assistant"
DB_PATH = os.environ.get("CRM_DB", "crm.db")
HOST = "127.0.0.1"
PORT = int(os.environ.get("CRM_PORT", "8765"))
STAGES = ["New", "Contacted", "Follow-up", "Qualified", "Proposal", "Won", "Lost"]
ACTIVE_STAGES = ["New", "Contacted", "Follow-up", "Qualified", "Proposal"]
ACTIVITY_TYPES = ["Call", "Email", "Meeting", "To-do"]
LOST_CATEGORIES = ["Past demo - no move forward", "Bad data", "No response", "No need", "Budget", "Timing", "Competitor", "Not a fit", "Do not contact", "Other"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
GENERIC_PREFIXES = {"info", "sales", "hello", "contact", "support", "admin", "office", "service"}
DEFAULT_EMAIL_PLAN_COUNT = 6


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def today_iso():
    return datetime.now().date().isoformat()


def add_business_days(start_date, days):
    if isinstance(start_date, str):
        current = datetime.strptime(start_date[:10], "%Y-%m-%d").date()
    else:
        current = start_date
    added = 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current.isoformat()


def company_key(value):
    value = (value or "").lower()
    return re.sub(r"[^a-z0-9]+", "", value)


def is_generic_email(email_addr):
    local = (email_addr or "").split("@", 1)[0].lower()
    return local in GENERIC_PREFIXES


def valid_email(email_addr):
    email_addr = (email_addr or "").strip()
    if not EMAIL_RE.match(email_addr):
        return False, "Invalid email syntax"
    domain = email_addr.rsplit("@", 1)[1]
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(2)
    try:
        socket.getaddrinfo(domain, 25)
        return True, ""
    except OSError:
        try:
            socket.getaddrinfo(domain, 443)
            return True, ""
        except OSError:
            return False, "Domain did not resolve"
    finally:
        socket.setdefaulttimeout(old_timeout)


def auto_priority(row):
    email_addr = (row.get("email") or "").strip()
    generic = (row.get("email_generic") or "").strip()
    contact = (row.get("contact") or "").strip()
    phone = (row.get("phone") or "").strip()
    next_action = (row.get("next_action") or "").strip()
    if contact and email_addr and not is_generic_email(email_addr):
        return 3
    if next_action or (contact and (generic or phone)):
        return 2
    if email_addr or generic or phone or contact:
        return 1
    return 0


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def execute(conn, sql, args=()):
    cur = conn.execute(sql, args)
    conn.commit()
    return cur


def row_to_dict(row):
    return dict(row) if row else None


def backup_db():
    if os.path.exists(DB_PATH):
        os.makedirs("backups", exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(DB_PATH, os.path.join("backups", f"crm-{stamp}.db"))


def init_db():
    conn = connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL,
            contact TEXT,
            title TEXT,
            email TEXT,
            email_generic TEXT,
            phone TEXT,
            website TEXT,
            city TEXT,
            state TEXT,
            industry TEXT,
            sub_industry TEXT,
            software_used TEXT,
            software_renewal_date TEXT,
            odoo_evaluation_version TEXT,
            odoo_evaluation_date TEXT,
            odoo_evaluation_year TEXT,
            previous_demo_notes TEXT,
            previous_blocker TEXT,
            apps_evaluated TEXT,
            recovery_last_drafted TEXT,
            source TEXT,
            stage TEXT DEFAULT 'New',
            priority INTEGER DEFAULT 0,
            priority_manual INTEGER DEFAULT 0,
            email_risky INTEGER DEFAULT 0,
            email_risk_reason TEXT,
            notes TEXT,
            next_action TEXT,
            next_action_date TEXT,
            pain_points TEXT,
            industry_holes TEXT,
            value_angle TEXT,
            proof_points TEXT,
            company_summary TEXT,
            why_now TEXT,
            workflow_hypothesis TEXT,
            research_evidence TEXT,
            research_source_url TEXT,
            confidence TEXT,
            do_not_claim TEXT,
            reason_to_believe TEXT,
            role_lens TEXT,
            claim_safety TEXT,
            customer_language TEXT,
            reply_type_goal TEXT,
            lead_email_templates TEXT,
            email_stage INTEGER DEFAULT 1,
            email_plan_count INTEGER DEFAULT 6,
            email_last_stage INTEGER DEFAULT 0,
            lost_category TEXT,
            lost_reason TEXT,
            lost_ts TEXT,
            last_emailed TEXT,
            emailed_count INTEGER DEFAULT 0,
            created TEXT,
            updated TEXT
        );
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER,
            ts TEXT,
            text TEXT,
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        );
        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER,
            type TEXT,
            due TEXT,
            note TEXT,
            done INTEGER DEFAULT 0,
            done_ts TEXT,
            created TEXT,
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS suppressed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_key TEXT,
            company TEXT,
            email TEXT,
            reason TEXT,
            ts TEXT,
            restored INTEGER DEFAULT 0,
            restored_ts TEXT
        );
        CREATE TABLE IF NOT EXISTS timeline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER,
            ts TEXT,
            type TEXT,
            text TEXT,
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        );
        """
    )
    defaults = {
        "gmail_email": "",
        "gmail_app_password": "",
        "sender_name": "Rayhan",
        "email_subject": "A quick idea for {company}",
        "email_body": default_email_templates()[0]["body"],
        "email_plan_count": str(DEFAULT_EMAIL_PLAN_COUNT),
        "email_templates": json.dumps(default_email_templates()),
        "email_signature": "<p>Best,<br>Your Name</p>",
        "daily_target": "20",
        "min_personalization_score": "60",
        "require_contact_name": "1",
        "require_industry": "1",
        "require_custom_first_line": "0",
        "require_software_stack": "0",
        "sequence_presets": json.dumps(default_sequence_presets()),
    }
    for key, value in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))
    ensure_lead_columns(conn)
    conn.commit()
    conn.close()


def ensure_lead_columns(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(leads)")}
    migrations = {
        "email_stage": "ALTER TABLE leads ADD COLUMN email_stage INTEGER DEFAULT 1",
        "email_plan_count": "ALTER TABLE leads ADD COLUMN email_plan_count INTEGER DEFAULT 6",
        "email_last_stage": "ALTER TABLE leads ADD COLUMN email_last_stage INTEGER DEFAULT 0",
        "sub_industry": "ALTER TABLE leads ADD COLUMN sub_industry TEXT",
        "software_used": "ALTER TABLE leads ADD COLUMN software_used TEXT",
        "software_renewal_date": "ALTER TABLE leads ADD COLUMN software_renewal_date TEXT",
        "odoo_evaluation_version": "ALTER TABLE leads ADD COLUMN odoo_evaluation_version TEXT",
        "odoo_evaluation_date": "ALTER TABLE leads ADD COLUMN odoo_evaluation_date TEXT",
        "odoo_evaluation_year": "ALTER TABLE leads ADD COLUMN odoo_evaluation_year TEXT",
        "previous_demo_notes": "ALTER TABLE leads ADD COLUMN previous_demo_notes TEXT",
        "previous_blocker": "ALTER TABLE leads ADD COLUMN previous_blocker TEXT",
        "apps_evaluated": "ALTER TABLE leads ADD COLUMN apps_evaluated TEXT",
        "recovery_last_drafted": "ALTER TABLE leads ADD COLUMN recovery_last_drafted TEXT",
        "pain_points": "ALTER TABLE leads ADD COLUMN pain_points TEXT",
        "industry_holes": "ALTER TABLE leads ADD COLUMN industry_holes TEXT",
        "value_angle": "ALTER TABLE leads ADD COLUMN value_angle TEXT",
        "proof_points": "ALTER TABLE leads ADD COLUMN proof_points TEXT",
        "company_summary": "ALTER TABLE leads ADD COLUMN company_summary TEXT",
        "why_now": "ALTER TABLE leads ADD COLUMN why_now TEXT",
        "workflow_hypothesis": "ALTER TABLE leads ADD COLUMN workflow_hypothesis TEXT",
        "research_evidence": "ALTER TABLE leads ADD COLUMN research_evidence TEXT",
        "research_source_url": "ALTER TABLE leads ADD COLUMN research_source_url TEXT",
        "confidence": "ALTER TABLE leads ADD COLUMN confidence TEXT",
        "do_not_claim": "ALTER TABLE leads ADD COLUMN do_not_claim TEXT",
        "reason_to_believe": "ALTER TABLE leads ADD COLUMN reason_to_believe TEXT",
        "role_lens": "ALTER TABLE leads ADD COLUMN role_lens TEXT",
        "claim_safety": "ALTER TABLE leads ADD COLUMN claim_safety TEXT",
        "customer_language": "ALTER TABLE leads ADD COLUMN customer_language TEXT",
        "reply_type_goal": "ALTER TABLE leads ADD COLUMN reply_type_goal TEXT",
        "lead_email_templates": "ALTER TABLE leads ADD COLUMN lead_email_templates TEXT",
        "sequence_angle": "ALTER TABLE leads ADD COLUMN sequence_angle TEXT",
        "custom_first_line": "ALTER TABLE leads ADD COLUMN custom_first_line TEXT",
        "reply_goal": "ALTER TABLE leads ADD COLUMN reply_goal TEXT",
        "cta_style": "ALTER TABLE leads ADD COLUMN cta_style TEXT",
        "reply_cta": "ALTER TABLE leads ADD COLUMN reply_cta TEXT",
        "reply_outcome": "ALTER TABLE leads ADD COLUMN reply_outcome TEXT",
        "reply_outcome_ts": "ALTER TABLE leads ADD COLUMN reply_outcome_ts TEXT",
        "lost_category": "ALTER TABLE leads ADD COLUMN lost_category TEXT",
        "lost_reason": "ALTER TABLE leads ADD COLUMN lost_reason TEXT",
        "lost_ts": "ALTER TABLE leads ADD COLUMN lost_ts TEXT",
    }
    for column, sql in migrations.items():
        if column not in columns:
            conn.execute(sql)
    conn.execute("UPDATE leads SET email_stage=1 WHERE email_stage IS NULL OR email_stage < 1")
    conn.execute(
        "UPDATE leads SET email_plan_count=? WHERE email_plan_count IS NULL OR email_plan_count < 1",
        (DEFAULT_EMAIL_PLAN_COUNT,),
    )
    conn.execute("UPDATE leads SET email_last_stage=0 WHERE email_last_stage IS NULL")


def default_email_templates(count=DEFAULT_EMAIL_PLAN_COUNT):
    base = [
        {
            "subject": "A quick idea for {company}",
            "body": (
                "<p>Hi {first_name},</p>"
                "<p>{custom_first_line}</p>"
                "<p>I was looking at {company} and noticed you are in {industry_fallback}. "
                "I had a quick thought that may help your team create a steadier flow of qualified conversations.</p>"
                "<p>{reply_cta}</p>"
            ),
        },
        {
            "subject": "Worth a quick look, {first_name}?",
            "body": (
                "<p>Hi {first_name},</p>"
                "<p>Following up on my note about {company}. Based on your market in {city_fallback}, "
                "the useful angle is usually speed to response, cleaner follow-up, and fewer missed opportunities.</p>"
                "<p>{reply_cta}</p>"
            ),
        },
        {
            "subject": "Idea for {company}'s follow-up process",
            "body": (
                "<p>Hi {first_name},</p>"
                "<p>One pattern I see with {industry_fallback} teams is that good leads get expensive when follow-up is scattered. "
                "A simple system for next steps, reminders, and reviewed email drafts can protect the pipeline without adding busywork.</p>"
                "<p>Would a short walkthrough be useful?</p>"
            ),
        },
        {
            "subject": "Should I point this elsewhere?",
            "body": (
                "<p>Hi {first_name},</p>"
                "<p>I may be reaching the wrong person at {company}. I am trying to find who owns sales follow-up, "
                "customer outreach, or local growth for the team.</p>"
                "<p>If that is not you, who would be best?</p>"
            ),
        },
        {
            "subject": "Closing the loop",
            "body": (
                "<p>Hi {first_name},</p>"
                "<p>I do not want to crowd your inbox. I thought {company} might benefit from a cleaner way to manage opportunities, "
                "email follow-up, and do-not-contact handling from one simple local CRM.</p>"
                "<p>Should I close this out, or is it worth a quick reply?</p>"
            ),
        },
        {
            "subject": "Last note for now",
            "body": (
                "<p>Hi {first_name},</p>"
                "<p>Last note from me. If improving sales follow-up or keeping outreach more organized becomes a priority at {company}, "
                "I would be glad to help.</p>"
                "<p>Either way, thanks for reading.</p>"
            ),
        },
    ]
    while len(base) < count:
        n = len(base) + 1
        base.append({
            "subject": "Following up with {company} - note " + str(n),
            "body": (
                "<p>Hi {first_name},</p>"
                "<p>I wanted to keep this short. For {company}, the main opportunity I see is around {industry_fallback} follow-up "
                "and making sure the next step never slips.</p>"
                "<p>Is it worth a short conversation?</p>"
            ),
        })
    return base[:count]


def default_sequence_presets():
    return [
        {
            "name": "Odoo renewal displacement",
            "angle": "Software renewal",
            "cta_style": "Soft question",
            "reply_cta": "Worth comparing before the next renewal locks in?",
            "templates": [
                {
                    "subject": "Before the next renewal at {company}",
                    "body": "<p>Hi {first_name},</p><p>{custom_first_line}</p><p>One reason I wanted to reach {company} now: {renewal_line}</p><p>Odoo may be worth comparing because it can bring {odoo_modules} into one operating layer instead of renewing another patched-together stack.</p><p>{reply_cta}</p>",
                },
                {
                    "subject": "Quick renewal thought",
                    "body": "<p>Hi {first_name},</p><p>The question I would pressure-test is whether {competitor} is still the best fit, or whether Odoo can consolidate more of the workflow across {odoo_modules}.</p><p>{reply_cta}</p>",
                },
            ],
        },
        {
            "name": "Missed follow-up leakage",
            "angle": "Missed follow-up",
            "cta_style": "Send info",
            "reply_cta": "Should I send over the quick version?",
            "templates": [
                {
                    "subject": "Follow-up gaps at {company}",
                    "body": "<p>Hi {first_name},</p><p>{custom_first_line}</p><p>The pattern I am asking about is simple: good opportunities get expensive when quote follow-up, reminders, and handoffs live in too many places.</p><p>Odoo can connect CRM, Sales, activities, and reporting so fewer deals rely on memory.</p><p>{reply_cta}</p>",
                },
                {
                    "subject": "Worth checking?",
                    "body": "<p>Hi {first_name},</p><p>If follow-up is already tight at {company}, ignore me. If not, this is usually where Odoo gets useful: cleaner stages, next actions, quotes, and handoff visibility in one place.</p><p>{reply_cta}</p>",
                },
            ],
        },
        {
            "name": "QuickBooks spreadsheet sprawl",
            "angle": "QuickBooks / spreadsheet sprawl",
            "cta_style": "Permission",
            "reply_cta": "Open to a quick look?",
            "templates": [
                {
                    "subject": "{company} + fewer side systems",
                    "body": "<p>Hi {first_name},</p><p>{custom_first_line}</p><p>When teams grow around QuickBooks, spreadsheets, and point tools, the pain usually shows up between sales, inventory, service, billing, and reporting.</p><p>Odoo is useful when those handoffs need to live in one system.</p><p>{reply_cta}</p>",
                },
            ],
        },
        {
            "name": "Wrong-person routing",
            "angle": "Wrong person",
            "cta_style": "Wrong person",
            "reply_cta": "Am I reaching the right person for this?",
            "templates": [
                {
                    "subject": "Right person at {company}?",
                    "body": "<p>Hi {first_name},</p><p>I may be aiming this at the wrong person, so I will keep it short.</p><p>I am trying to find who owns operations software, follow-up process, or Odoo/ERP evaluation at {company}.</p><p>{reply_cta}</p>",
                },
            ],
        },
    ]


def get_settings(conn):
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}


def set_settings(conn, data):
    allowed = {
        "gmail_email", "gmail_app_password", "sender_name", "email_subject", "email_body", "email_signature",
        "daily_target", "email_plan_count", "email_templates", "min_personalization_score",
        "require_contact_name", "require_industry", "require_custom_first_line", "require_software_stack",
        "sequence_presets",
    }
    for key, value in data.items():
        if key in allowed:
            if key == "email_plan_count":
                value = max(1, min(30, int(value or DEFAULT_EMAIL_PLAN_COUNT)))
            if key == "email_templates":
                value = normalize_templates(value, int(data.get("email_plan_count") or get_settings(conn).get("email_plan_count") or DEFAULT_EMAIL_PLAN_COUNT))
            if key == "sequence_presets":
                value = normalize_sequence_presets(value)
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value or "")),
            )
    conn.commit()


def normalize_templates(raw, count=DEFAULT_EMAIL_PLAN_COUNT):
    try:
        templates = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        templates = []
    defaults = default_email_templates(count)
    clean = []
    for idx in range(count):
        item = templates[idx] if isinstance(templates, list) and idx < len(templates) and isinstance(templates[idx], dict) else {}
        clean.append({
            "subject": str(item.get("subject") or defaults[idx]["subject"]),
            "body": str(item.get("body") or defaults[idx]["body"]),
        })
    return json.dumps(clean)


def normalize_sequence_presets(raw):
    try:
        presets = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        presets = []
    clean = []
    for item in presets if isinstance(presets, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        templates = item.get("templates") if isinstance(item.get("templates"), list) else []
        if not name or not templates:
            continue
        clean.append({
            "name": name[:80],
            "angle": str(item.get("angle") or ""),
            "cta_style": str(item.get("cta_style") or "Soft question"),
            "reply_cta": str(item.get("reply_cta") or reply_cta_for_style(item.get("cta_style"))),
            "templates": [
                {
                    "subject": str(t.get("subject") or "Following up with {company}") if isinstance(t, dict) else "Following up with {company}",
                    "body": str(t.get("body") or "<p>Hi {first_name},</p><p>{custom_first_line}</p><p>{reply_cta}</p>") if isinstance(t, dict) else "<p>Hi {first_name},</p><p>{custom_first_line}</p><p>{reply_cta}</p>",
                }
                for t in templates[:30]
            ],
        })
    if not clean:
        clean = default_sequence_presets()
    return json.dumps(clean)


def get_email_templates(settings):
    count = max(1, min(30, int(settings.get("email_plan_count") or DEFAULT_EMAIL_PLAN_COUNT)))
    return json.loads(normalize_templates(settings.get("email_templates", "[]"), count))


INDUSTRY_TAXONOMY = {
    "Aerospace & Defense": ["component manufacturing", "MRO services", "avionics", "defense contracting", "precision machining", "space systems", "UAV systems", "quality inspection"],
    "Agriculture": ["crop farms", "equipment dealers", "greenhouses", "livestock operations", "agri-distribution", "seed suppliers", "irrigation services", "farm co-ops"],
    "Automotive": ["dealerships", "parts distribution", "repair shops", "fleet services", "EV charging", "body shops", "tire retailers", "aftermarket accessories"],
    "Biotech & Pharma": ["clinical labs", "contract research", "medical devices", "pharma distribution", "diagnostics", "biomanufacturing", "regulatory services", "specialty therapeutics"],
    "Construction": ["general contractors", "specialty contractors", "remodelers", "commercial builders", "home builders", "heavy civil", "concrete contractors", "electrical contractors"],
    "Consumer Services": ["cleaning services", "pest control", "home services", "salons", "wellness studios", "repair services", "moving companies", "security installers"],
    "Distribution & Wholesale": ["industrial distributors", "food distributors", "building materials", "medical supplies", "electronics distributors", "janitorial suppliers", "regional wholesalers", "import/export"],
    "Education": ["private schools", "training providers", "online education", "trade schools", "tutoring centers", "higher education departments", "language schools", "certification providers"],
    "Energy & Utilities": ["solar installers", "oilfield services", "battery storage", "utility contractors", "energy consultants", "wind services", "EV infrastructure", "power equipment"],
    "Engineering Services": ["civil engineering", "mechanical engineering", "environmental engineering", "automation engineering", "testing labs", "surveying", "product design", "systems integrators"],
    "Field Service": ["HVAC", "plumbing", "electrical service", "appliance repair", "garage doors", "landscaping", "fire protection", "water treatment"],
    "Financial Services": ["accounting firms", "wealth management", "insurance agencies", "lenders", "fintech", "bookkeeping firms", "tax practices", "payment services"],
    "Food & Beverage": ["restaurants", "catering", "food manufacturing", "beverage brands", "breweries", "coffee roasters", "meal prep", "specialty foods"],
    "Foundation & Structural Repair": ["foundation repair", "basement waterproofing", "crawlspace encapsulation", "concrete lifting", "structural engineering", "drainage contractors", "seawall repair", "masonry repair"],
    "Government & Public Sector": ["municipal services", "public works", "housing authorities", "transit agencies", "public safety suppliers", "procurement offices", "utilities departments", "parks departments"],
    "Healthcare": ["clinics", "dental practices", "urgent care", "home healthcare", "medical billing", "specialty practices", "physical therapy", "behavioral health"],
    "Hospitality": ["hotels", "event venues", "catering venues", "property management", "short-term rentals", "tour operators", "resorts", "conference centers"],
    "Industrial Manufacturing": ["metal fabrication", "plastics", "packaging", "machining", "electronics assembly", "textiles", "chemical manufacturing", "industrial equipment"],
    "Legal & Professional Services": ["law firms", "consultancies", "marketing agencies", "architecture firms", "recruiting firms", "managed services", "business brokers", "HR consultants"],
    "Logistics & Transportation": ["trucking", "freight brokers", "warehousing", "last-mile delivery", "cold chain", "3PL providers", "courier services", "fleet maintenance"],
    "Nonprofit": ["member associations", "charities", "arts organizations", "community services", "fundraising teams", "faith organizations", "foundations", "advocacy groups"],
    "Real Estate": ["brokerages", "property management", "commercial real estate", "home inspectors", "mortgage teams", "title companies", "real estate investors", "leasing teams"],
    "Retail": ["apparel retail", "specialty retail", "furniture stores", "hardware stores", "jewelry stores", "sporting goods", "pet stores", "multi-location retail"],
    "Robotics & Automation": ["warehouse robotics", "industrial automation", "cobots", "machine vision", "autonomous mobile robots", "robot integrators", "process automation", "robot maintenance"],
    "SaaS & Technology": ["B2B SaaS", "IT services", "cybersecurity", "data platforms", "software agencies", "cloud consultants", "AI tools", "telecom providers"],
    "Trades": ["roofing", "flooring", "painting", "fencing", "windows and doors", "insulation", "pool services", "tree services"],
}


ODOO_MODULE_FOCUS = {
    "Aerospace & Defense": ["Quality", "Inventory", "Manufacturing", "Purchase", "PLM", "Documents"],
    "Agriculture": ["Inventory", "Purchase", "Sales", "Accounting", "Field Service", "Fleet"],
    "Automotive": ["CRM", "Sales", "Inventory", "Appointments", "Helpdesk", "Accounting"],
    "Biotech & Pharma": ["Quality", "Inventory", "Manufacturing", "Documents", "Approvals", "Accounting"],
    "Construction": ["Project", "CRM", "Sales", "Field Service", "Timesheets", "Accounting"],
    "Consumer Services": ["CRM", "Appointments", "Field Service", "Helpdesk", "Invoicing", "Marketing Automation"],
    "Distribution & Wholesale": ["Inventory", "Barcode", "Purchase", "Sales", "Accounting", "Website/eCommerce"],
    "Education": ["CRM", "eLearning", "Events", "Email Marketing", "Accounting", "Subscriptions"],
    "Energy & Utilities": ["Project", "Field Service", "Inventory", "Timesheets", "Helpdesk", "Accounting"],
    "Engineering Services": ["Project", "Timesheets", "Documents", "CRM", "Sales", "Helpdesk"],
    "Field Service": ["Field Service", "CRM", "Sales", "Inventory", "Appointments", "Invoicing"],
    "Financial Services": ["Accounting", "Documents", "CRM", "Approvals", "Sign", "Marketing Automation"],
    "Food & Beverage": ["POS", "Inventory", "Manufacturing", "Purchase", "Accounting", "Website/eCommerce"],
    "Foundation & Structural Repair": ["CRM", "Field Service", "Appointments", "Sales", "Invoicing", "Marketing Automation"],
    "Government & Public Sector": ["Project", "Purchase", "Documents", "Approvals", "Helpdesk", "Accounting"],
    "Healthcare": ["CRM", "Appointments", "Documents", "Helpdesk", "Accounting", "Email Marketing"],
    "Hospitality": ["CRM", "Events", "Website", "POS", "Accounting", "Helpdesk"],
    "Industrial Manufacturing": ["Manufacturing", "Inventory", "Quality", "Maintenance", "Purchase", "PLM"],
    "Legal & Professional Services": ["CRM", "Project", "Timesheets", "Documents", "Sign", "Invoicing"],
    "Logistics & Transportation": ["Fleet", "Inventory", "Barcode", "Accounting", "Helpdesk", "Sales"],
    "Nonprofit": ["CRM", "Email Marketing", "Events", "Accounting", "Website", "Documents"],
    "Real Estate": ["CRM", "Sales", "Documents", "Sign", "Appointments", "Marketing Automation"],
    "Retail": ["POS", "Inventory", "Website/eCommerce", "Loyalty", "Accounting", "Email Marketing"],
    "Robotics & Automation": ["CRM", "Manufacturing", "Inventory", "Project", "Field Service", "Helpdesk"],
    "SaaS & Technology": ["CRM", "Subscriptions", "Helpdesk", "Project", "Email Marketing", "Accounting"],
    "Trades": ["CRM", "Field Service", "Appointments", "Sales", "Invoicing", "Inventory"],
}


INDUSTRY_SOFTWARE_STACKS = {
    "Aerospace & Defense": ["SAP", "Siemens Teamcenter", "Deltek Costpoint", "Arena PLM", "MasterControl", "TipQA", "NetSuite", "Microsoft Dynamics 365"],
    "Agriculture": ["Agworld", "Granular", "John Deere Operations Center", "Conservis", "FarmLogs", "QuickBooks", "AgriWebb", "Bushel"],
    "Automotive": ["CDK Global", "Reynolds and Reynolds", "Mitchell 1", "Tekmetric", "Shop-Ware", "AutoFluent", "QuickBooks", "Dealertrack"],
    "Biotech & Pharma": ["Veeva", "MasterControl", "LabWare LIMS", "Benchling", "SAP", "Oracle NetSuite", "TrackWise", "DocuSign"],
    "Construction": ["Procore", "Buildertrend", "CoConstruct", "JobNimbus", "Sage 100 Contractor", "QuickBooks", "Foundation Software", "ServiceTitan"],
    "Consumer Services": ["Housecall Pro", "Jobber", "ServiceTitan", "Square", "QuickBooks", "Calendly", "GoSite", "Podium"],
    "Distribution & Wholesale": ["NetSuite", "SAP Business One", "Microsoft Dynamics 365", "Fishbowl", "Cin7", "QuickBooks Enterprise", "ShipStation", "Extensiv"],
    "Education": ["Blackbaud", "Canvas", "Moodle", "HubSpot", "Salesforce", "Eventbrite", "QuickBooks", "Stripe"],
    "Energy & Utilities": ["Salesforce", "Aurora Solar", "OpenSolar", "ServiceTitan", "Procore", "QuickBooks", "NetSuite", "Fulcrum"],
    "Engineering Services": ["Deltek Vantagepoint", "Autodesk Construction Cloud", "Ajera", "BQE Core", "Mavenlink", "QuickBooks", "SharePoint", "HubSpot"],
    "Field Service": ["ServiceTitan", "Housecall Pro", "Jobber", "FieldEdge", "Service Fusion", "QuickBooks", "Podium", "CallRail"],
    "Financial Services": ["QuickBooks Online", "Xero", "Bill.com", "TaxDome", "Canopy", "Salesforce", "HubSpot", "DocuSign"],
    "Food & Beverage": ["Toast", "Square", "Restaurant365", "MarketMan", "Craftable", "QuickBooks", "NetSuite", "Shopify"],
    "Foundation & Structural Repair": ["ServiceTitan", "JobNimbus", "Foundation Software", "AccuLynx", "CompanyCam", "Leap", "QuickBooks", "Podium"],
    "Government & Public Sector": ["Tyler Technologies", "OpenGov", "Cityworks", "Laserfiche", "Microsoft Dynamics 365", "SharePoint", "DocuSign", "Oracle"],
    "Healthcare": ["Epic", "athenahealth", "Dentrix", "Open Dental", "Kareo", "SimplePractice", "Salesforce Health Cloud", "QuickBooks"],
    "Hospitality": ["Cloudbeds", "Mews", "Toast", "Event Temple", "Tripleseat", "Square", "QuickBooks", "HubSpot"],
    "Industrial Manufacturing": ["Epicor", "Plex", "JobBOSS", "E2 Shop System", "MRPeasy", "NetSuite", "SAP Business One", "Fishbowl"],
    "Legal & Professional Services": ["Clio", "PracticePanther", "Deltek", "BQE Core", "HubSpot", "QuickBooks", "DocuSign", "SharePoint"],
    "Logistics & Transportation": ["Samsara", "Motive", "Rose Rocket", "Tailwind TMS", "ShipStation", "NetSuite", "QuickBooks", "Fleetio"],
    "Nonprofit": ["Blackbaud", "Bloomerang", "DonorPerfect", "Kindful", "Mailchimp", "QuickBooks", "Eventbrite", "Salesforce Nonprofit Cloud"],
    "Real Estate": ["AppFolio", "Buildium", "Yardi", "kvCORE", "Follow Up Boss", "DocuSign", "QuickBooks", "HubSpot"],
    "Retail": ["Shopify", "Square", "Lightspeed", "Toast", "Cin7", "QuickBooks POS", "NetSuite", "Klaviyo"],
    "Robotics & Automation": ["Salesforce", "HubSpot", "NetSuite", "SAP Business One", "Arena PLM", "Jira", "Autodesk Fusion Manage", "ServiceMax"],
    "SaaS & Technology": ["Salesforce", "HubSpot", "Stripe", "Chargebee", "Zendesk", "Intercom", "Jira", "QuickBooks"],
    "Trades": ["JobNimbus", "ServiceTitan", "Housecall Pro", "Jobber", "AccuLynx", "CompanyCam", "QuickBooks", "Podium"],
}


SUBINDUSTRY_SOFTWARE_HINTS = {
    "HVAC": ["ServiceTitan", "Housecall Pro", "FieldEdge", "CallRail"],
    "plumbing": ["ServiceTitan", "Jobber", "Service Fusion", "Podium"],
    "electrical service": ["ServiceTitan", "Housecall Pro", "QuickBooks", "CompanyCam"],
    "foundation repair": ["ServiceTitan", "JobNimbus", "CompanyCam", "Leap"],
    "basement waterproofing": ["JobNimbus", "CompanyCam", "ServiceTitan", "Podium"],
    "warehouse robotics": ["NetSuite", "Salesforce", "Arena PLM", "Jira"],
    "industrial automation": ["SAP Business One", "NetSuite", "Jira", "ServiceMax"],
    "machine vision": ["HubSpot", "Jira", "Arena PLM", "NetSuite"],
    "metal fabrication": ["JobBOSS", "E2 Shop System", "Epicor", "Fishbowl"],
    "machining": ["JobBOSS", "ProShop ERP", "E2 Shop System", "QuickBooks"],
    "food manufacturing": ["BatchMaster", "NetSuite", "Fishbowl", "QuickBooks"],
    "multi-location retail": ["Shopify POS", "Lightspeed", "Cin7", "Klaviyo"],
    "industrial distributors": ["NetSuite", "SAP Business One", "Fishbowl", "ShipStation"],
    "B2B SaaS": ["Salesforce", "HubSpot", "Stripe", "Chargebee"],
    "commercial builders": ["Procore", "Sage 100 Contractor", "Buildertrend", "QuickBooks"],
    "dental practices": ["Dentrix", "Open Dental", "Kareo", "Solutionreach"],
    "solar installers": ["Aurora Solar", "OpenSolar", "Salesforce", "ServiceTitan"],
    "fleet maintenance": ["Fleetio", "Samsara", "Motive", "QuickBooks"],
}


def software_stack_for(industry, sub_industry=""):
    stack = []
    for item in SUBINDUSTRY_SOFTWARE_HINTS.get(sub_industry, []):
        if item not in stack:
            stack.append(item)
    for item in INDUSTRY_SOFTWARE_STACKS.get(industry, ["Salesforce", "HubSpot", "QuickBooks", "Excel", "Google Workspace", "Microsoft 365"]):
        if item not in stack:
            stack.append(item)
    return stack[:10]


SOFTWARE_COMPETITIVE_ANGLES = [
    {
        "keys": ("servicetitan", "service titan"),
        "name": "ServiceTitan",
        "angle": "ServiceTitan is strong for trade dispatch, but companies often still keep CRM, inventory, accounting, marketing, and reporting patched around it.",
        "odoo_win": "Odoo can bring CRM, Field Service, Inventory, Invoicing, Accounting, Website, and Marketing Automation into one system instead of treating dispatch as the whole operating system.",
        "price": "price and seat expansion can become a real conversation as the team grows",
        "renewal": "ServiceTitan renewals are a natural time to ask whether dispatch, inventory, billing, and customer follow-up should still live in separate places",
    },
    {
        "keys": ("netsuite", "oracle netsuite"),
        "name": "NetSuite",
        "angle": "NetSuite can be powerful, but many mid-market teams feel the weight of customization, admin overhead, partner dependency, and cost as workflows change.",
        "odoo_win": "Odoo gives the team broad ERP coverage with CRM, Sales, Inventory, Manufacturing, Accounting, eCommerce, Projects, and Field Service in a more flexible all-in-one system.",
        "price": "Odoo's $65 max per user pricing can be a meaningful contrast when NetSuite costs are expanding across users, modules, and services",
        "renewal": "NetSuite renewal windows are a good moment to compare what the business actually uses against what it pays to maintain",
    },
    {
        "keys": ("quickbooks", "quickbooks online", "quickbooks enterprise", "quickbooks pos"),
        "name": "QuickBooks",
        "angle": "QuickBooks is useful for accounting, but it usually becomes the place operations work around instead of the system operations run through.",
        "odoo_win": "Odoo keeps accounting connected to CRM, quotes, inventory, field service, projects, purchasing, and invoicing so the team is not stitching the business together after the fact.",
        "price": "the win is less about replacing a cheap accounting tool and more about avoiding the stack of add-ons around it",
        "renewal": "before the next QuickBooks or add-on renewal cycle, it is worth asking whether accounting should stay separate from the operating workflow",
    },
    {
        "keys": ("sap", "sap business one", "s/4hana", "s4hana"),
        "name": "SAP",
        "angle": "SAP is built for serious operations, but it can feel heavy when teams need faster changes, simpler workflows, or less dependency on specialized admin work.",
        "odoo_win": "Odoo can cover ERP, CRM, manufacturing, inventory, quality, purchase, accounting, and service workflows in one system that is easier to shape around the business.",
        "price": "cost, complexity, and implementation speed are usually the comparison points",
        "renewal": "SAP renewal and support cycles are a natural time to evaluate whether the current setup is too heavy for the workflows the team actually needs",
    },
    {
        "keys": ("salesforce",),
        "name": "Salesforce",
        "angle": "Salesforce can be a strong CRM, but many teams still need separate tools for quotes, inventory, service delivery, accounting, and operations.",
        "odoo_win": "Odoo starts with CRM but extends into Sales, Inventory, Project, Field Service, Accounting, eCommerce, and automation in the same system.",
        "price": "the comparison is often total stack cost, not just CRM seat cost",
        "renewal": "Salesforce renewal is a good moment to ask whether CRM should remain separate from the operating system behind it",
    },
    {
        "keys": ("hubspot",),
        "name": "HubSpot",
        "angle": "HubSpot is strong for marketing and CRM, but it usually stops before the operational handoff into fulfillment, inventory, service, and accounting.",
        "odoo_win": "Odoo can connect CRM and marketing follow-up to quotes, invoices, inventory, projects, helpdesk, and delivery.",
        "price": "pricing can climb when contacts, hubs, and seats expand",
        "renewal": "HubSpot renewal is a natural time to compare CRM-only growth against a full operating system",
    },
    {
        "keys": ("shopify", "shopify pos"),
        "name": "Shopify",
        "angle": "Shopify is excellent for commerce, but retailers often outgrow the back-office pieces around purchasing, inventory, accounting, POS, and B2B sales.",
        "odoo_win": "Odoo can connect eCommerce, POS, Inventory, Purchase, Accounting, CRM, and Email Marketing without leaving operations scattered.",
        "price": "the stack cost often lives in apps, add-ons, and disconnected back-office tools",
        "renewal": "before renewing commerce apps and add-ons, it is worth checking whether the back office needs one system",
    },
    {
        "keys": ("procore", "buildertrend", "coconstruct"),
        "name": "construction software",
        "angle": "Construction platforms can manage projects well, but sales follow-up, estimates, purchasing, timesheets, invoicing, and accounting often still live around the edges.",
        "odoo_win": "Odoo can connect CRM, Sales, Project, Timesheets, Purchase, Documents, Sign, and Accounting in one workflow.",
        "price": "the win is reducing the number of systems around project management",
        "renewal": "project software renewal is a good time to ask whether pre-sale and back-office workflows should be connected too",
    },
    {
        "keys": ("epicor", "plex", "jobbss", "jobboss", "e2 shop", "mrpeasy"),
        "name": "manufacturing ERP",
        "angle": "Manufacturing ERPs can handle production, but teams often struggle when CRM, quoting, inventory, purchasing, quality, maintenance, and accounting are not cleanly connected.",
        "odoo_win": "Odoo can connect Manufacturing, Inventory, Quality, Maintenance, Purchase, PLM, Sales, and Accounting in one operating layer.",
        "price": "implementation complexity and ongoing admin cost are often the real comparison",
        "renewal": "ERP renewal is the right time to evaluate whether the current system is still flexible enough for the shop floor and sales team",
    },
]


def software_competitive_angle(lead):
    software = (lead.get("software_used") or "").lower()
    for angle in SOFTWARE_COMPETITIVE_ANGLES:
        if any(key in software for key in angle["keys"]):
            renewal_date = (lead.get("software_renewal_date") or "").strip()
            if renewal_date:
                renewal_line = f"Since your {angle['name']} renewal is around {renewal_date}, this is the right window to compare whether Odoo is a cleaner operating system before another term locks in."
            else:
                renewal_line = f"Even if the renewal date is not on my calendar, I would want to have the Odoo conversation before your next {angle['name']} renewal cycle, not after another term is already locked in."
            return dict(angle, renewal_line=renewal_line)
    renewal_date = (lead.get("software_renewal_date") or "").strip()
    return {
        "name": "current software stack",
        "angle": "the current stack may work in pieces, but the gaps usually show up between CRM, quoting, operations, billing, and reporting",
        "odoo_win": "Odoo's advantage is bringing the operating workflow into one system instead of adding another disconnected app",
        "price": "price matters, but the bigger win is reducing duplicate tools, manual handoffs, and admin drag",
        "renewal": "renewal timing is a natural point to compare the current stack against Odoo",
        "renewal_line": (
            f"Since the renewal is around {renewal_date}, it is worth comparing Odoo before another term locks in."
            if renewal_date else
            "It is worth having the Odoo conversation before the next renewal cycle, not after another term is already locked in."
        ),
    }


def industry_library():
    items = []
    for industry, subs in INDUSTRY_TAXONOMY.items():
        for sub in subs:
            modules = ODOO_MODULE_FOCUS.get(industry, ["CRM", "Sales", "Accounting"])
            items.append({
                "industry": industry,
                "sub_industry": sub,
                "modules": modules,
                "software": software_stack_for(industry, sub),
            })
    return items


def match_industry_entry(lead):
    industry_text = (lead.get("industry") or "").lower()
    sub_text = (lead.get("sub_industry") or "").lower()
    notes_text = (lead.get("notes") or "").lower()
    haystack = " ".join([industry_text, sub_text, notes_text])
    for industry, subs in INDUSTRY_TAXONOMY.items():
        if industry_text and industry.lower() == industry_text:
            sub = next((item for item in subs if item.lower() == sub_text), lead.get("sub_industry") or subs[0])
            return {
                "industry": industry,
                "sub_industry": sub,
                "modules": ODOO_MODULE_FOCUS.get(industry, []),
                "software": software_stack_for(industry, sub),
            }
    best = None
    for industry, subs in INDUSTRY_TAXONOMY.items():
        industry_match = industry.lower() in haystack or any(part in haystack for part in industry.lower().replace("&", " ").split())
        for sub in subs:
            sub_match = sub.lower() in haystack or any(token in haystack for token in sub.lower().replace("/", " ").split() if len(token) > 3)
            if sub_match:
                return {
                    "industry": industry,
                    "sub_industry": sub,
                    "modules": ODOO_MODULE_FOCUS.get(industry, []),
                    "software": software_stack_for(industry, sub),
                }
        if industry_match and not best:
            sub = lead.get("sub_industry") or subs[0]
            best = {
                "industry": industry,
                "sub_industry": sub,
                "modules": ODOO_MODULE_FOCUS.get(industry, []),
                "software": software_stack_for(industry, sub),
            }
    return best or {
        "industry": lead.get("industry") or "General Business",
        "sub_industry": lead.get("sub_industry") or "general operations",
        "modules": ["CRM", "Sales", "Accounting", "Inventory", "Project", "Email Marketing"],
        "software": ["Salesforce", "HubSpot", "QuickBooks", "Excel", "Google Workspace", "Microsoft 365"],
    }


def industry_playbook(lead):
    entry = match_industry_entry(lead)
    modules = ", ".join(entry["modules"][:4]) or "CRM, Sales, Accounting"
    software = lead.get("software_used") or ", ".join(entry.get("software", [])[:5]) or "their current tools"
    text = " ".join([
        lead.get("industry") or "",
        lead.get("sub_industry") or "",
        lead.get("notes") or "",
    ]).lower()
    playbooks = [
        (("robot", "automation", "mechatronic"), {
            "label": "robotics / automation",
            "holes": "long technical buying cycles, unclear ROI language, engineers evaluating quietly before sales knows, hard-to-explain integrations, and prospects worrying about downtime during deployment",
            "pain": "teams often need help turning complex automation value into simple operational outcomes a buyer can approve",
            "angle": "position the conversation around throughput, labor gaps, downtime reduction, integration risk, and faster payback proof",
            "proof": "reference process bottlenecks, cycle time, uptime, safety, and implementation risk",
        }),
        (("foundation", "basement", "crawlspace", "concrete repair"), {
            "label": "foundation repair",
            "holes": "homeowners delay because the problem feels expensive, scary, and hard to compare; many quote requests go cold when follow-up is not fast and trust-building",
            "pain": "foundation teams lose jobs when urgent inquiries are not followed up with enough education, proof, and reassurance",
            "angle": "focus on response speed, inspection follow-up, homeowner trust, financing conversations, and clear next steps after estimates",
            "proof": "reference inspection scheduling, before/after proof, warranty clarity, financing, and post-estimate follow-up",
        }),
        (("hvac", "air conditioning", "heating", "cooling"), {
            "label": "HVAC",
            "holes": "seasonal spikes overwhelm follow-up, replacement leads cool off quickly, maintenance opportunities slip, and urgent callers often choose whoever responds first",
            "pain": "HVAC sales teams need quick response, clean call queues, and follow-up that does not disappear after the first estimate",
            "angle": "focus on speed-to-lead, replacement estimate follow-up, tune-up conversion, and keeping every quote alive",
            "proof": "reference missed calls, aged estimates, seasonal demand, and service-to-replacement handoffs",
        }),
        (("roof", "gutter", "storm"), {
            "label": "roofing",
            "holes": "storm windows create lead floods, insurance conversations stall, homeowners compare many bidders, and old estimates are rarely revived well",
            "pain": "roofing teams need a disciplined way to stay in front of homeowners after inspection and estimate",
            "angle": "focus on inspection follow-up, insurance clarity, quote revival, and trust signals",
            "proof": "reference estimate aging, inspection volume, storm response, and homeowner decision confidence",
        }),
        (("distribution", "distributor", "wholesale", "warehouse", "shipstation", "fishbowl"), {
            "label": "distribution / wholesale",
            "holes": "inside sales promises dates before stock and purchasing reality are visible, warehouse exceptions surface too late, and reordering signals live outside the customer conversation",
            "pain": "distributors need sales, inventory, purchasing, warehouse movement, and invoicing to stay connected before a customer is promised the wrong thing",
            "angle": "focus on stock visibility, purchasing handoffs, warehouse accuracy, quote-to-order speed, and delivery confidence",
            "proof": "reference available-to-promise checks, backorders, reorder points, receiving, barcode movement, and margin leakage",
        }),
        (("manufactur", "machining", "fabrication", "industrial"), {
            "label": "manufacturing",
            "holes": "buyers care about capacity, lead times, tolerance, quality, and reliability, but outreach often sounds generic and misses operational pressure",
            "pain": "manufacturers need outreach that speaks to production realities instead of vague growth language",
            "angle": "focus on capacity, quality, lead-time pressure, repeatable follow-up, and RFQ conversion",
            "proof": "reference quote turnaround, production bottlenecks, quality requirements, and supplier reliability",
        }),
        (("construction", "contractor", "builder", "remodel"), {
            "label": "construction / contracting",
            "holes": "jobs have long decision paths, estimates need repeated follow-up, and good prospects vanish when projects are not ready yet",
            "pain": "contractors need a simple way to keep estimates warm without relying on memory",
            "angle": "focus on estimate follow-up, project timing, financing readiness, and reviving stalled opportunities",
            "proof": "reference proposal aging, site visits, project timelines, and quote follow-up",
        }),
    ]
    for needles, data in playbooks:
        if any(needle in text for needle in needles):
            data = dict(data)
            data["label"] = lead.get("sub_industry") or data["label"]
            data["modules"] = modules
            data["software"] = software
            data["holes"] = data["holes"] + f"; likely software stack friction around {software} and Odoo handoff points"
            return data
    industry = entry["industry"]
    sub_industry = lead.get("sub_industry") or entry["sub_industry"]
    return {
        "label": f"{sub_industry} in {industry}",
        "modules": modules,
        "software": software,
        "holes": (
            f"{sub_industry} teams often run into disconnected CRM notes, quote follow-up living outside the system, "
            f"manual handoffs between sales and operations, likely current tools such as {software}, "
            f"and Odoo apps like {modules} not being used as one clean workflow"
        ),
        "pain": (
            f"{industry} organizations need outreach and pipeline operations that connect the first lead, the quote, "
            f"the operational next step, and the invoice without forcing teams to re-enter the same information"
        ),
        "angle": (
            f"position Odoo around the specific workflow for {sub_industry}: cleaner CRM stages, faster quote follow-up, "
            f"better visibility across {modules}, and fewer stalled opportunities"
        ),
        "proof": (
            f"reference Odoo CRM stage aging, unanswered activities, draft quotes, inventory or project bottlenecks, "
            f"and whether {modules} are connected to the sales process"
        ),
    }


def lead_strategy(lead):
    playbook = industry_playbook(lead)
    competitive = software_competitive_angle(lead)
    return {
        "market": lead.get("sub_industry") or playbook["label"],
        "holes": lead.get("industry_holes") or playbook["holes"],
        "pain": lead.get("pain_points") or playbook["pain"],
        "angle": lead.get("value_angle") or playbook["angle"],
        "proof": lead.get("proof_points") or playbook["proof"],
        "software": lead.get("software_used") or playbook.get("software") or "",
        "modules": playbook.get("modules") or "",
        "competitor": competitive["name"],
        "competitor_angle": competitive["angle"],
        "odoo_win": competitive["odoo_win"],
        "price_angle": competitive["price"],
        "renewal_angle": competitive["renewal"],
        "renewal_line": competitive["renewal_line"],
    }


def odoo_app_pitch(modules_text):
    modules = [item.strip() for item in (modules_text or "").split(",") if item.strip()]
    lines = {
        "CRM": "CRM keeps every opportunity, call, activity, and next step in one pipeline so follow-up does not depend on memory.",
        "Sales": "Sales turns interest into quotes quickly, then keeps those quotes tied to the customer record.",
        "Inventory": "Inventory tracks what is available, where it is, and what needs to be replenished before the team promises something it cannot deliver.",
        "Manufacturing": "Manufacturing connects orders, bills of materials, work orders, capacity, and production status in one flow.",
        "Field Service": "Field Service schedules and dispatches every job, while techs log time, parts, and notes from their phones onsite.",
        "Accounting": "Accounting and Invoicing keep the financial side tied to the work instead of chasing details after the job is done.",
        "Invoicing": "Invoicing can trigger the moment work is completed, so billing does not lag behind operations.",
        "Project": "Project gives the team one place to track delivery milestones, tasks, owners, and client-facing work.",
        "Helpdesk": "Helpdesk keeps support requests, customer issues, and service history attached to the account.",
        "Purchase": "Purchase helps replenishment and vendor orders stay connected to demand.",
        "Quality": "Quality gives checks, defects, and approvals a place inside the operational flow.",
        "POS": "POS ties store sales back into inventory, customers, accounting, and reporting.",
        "Website/eCommerce": "Website and eCommerce connect online demand back into orders, inventory, and customer history.",
        "Subscriptions": "Subscriptions keeps renewals, recurring revenue, and expansion opportunities visible.",
        "Marketing Automation": "Marketing Automation keeps nurture sequences and follow-up tied to CRM behavior.",
        "Appointments": "Appointments lets prospects and customers book time without creating scheduling chaos.",
        "Documents": "Documents keeps estimates, contracts, and proof in the customer record.",
        "Sign": "Sign helps approvals and agreements move without slowing down the sale.",
        "Timesheets": "Timesheets connects labor back to jobs, projects, billing, and margin.",
        "Fleet": "Fleet keeps vehicles, service, and operating costs visible.",
        "Barcode": "Barcode speeds up warehouse, receiving, and inventory movement.",
        "Maintenance": "Maintenance helps prevent equipment and work-center issues from becoming surprises.",
        "PLM": "PLM keeps product changes and engineering handoffs controlled.",
        "Email Marketing": "Email Marketing lets outreach and nurture stay connected to customer data.",
        "Events": "Events keeps registration, communication, and follow-up organized.",
        "eLearning": "eLearning helps training and education delivery stay connected to contacts and revenue.",
        "Approvals": "Approvals moves internal decisions without burying them in email.",
        "Loyalty": "Loyalty helps retail teams bring customers back without another disconnected app.",
    }
    selected = []
    for module in modules:
        selected.append(lines.get(module, f"{module} helps bring that part of the operation into the same Odoo system."))
        if len(selected) == 4:
            break
    if not selected:
        selected = [lines["CRM"], lines["Sales"], lines["Accounting"]]
    return " ".join(selected)


def generated_lead_templates(lead, count=None):
    count = count or max(1, min(30, int(lead.get("email_plan_count") or DEFAULT_EMAIL_PLAN_COUNT)))
    strategy = lead_strategy(lead)
    app_pitch = odoo_app_pitch(strategy["modules"])
    software_phrase = strategy["software"] or "separate tools"
    templates = [
        {
            "subject": "{company} + Odoo",
            "body": (
                "<p>Hi {first_name},</p>"
                "<p>{custom_first_line}</p>"
                "<p>It&apos;s {sender_name}. I had a quick Odoo thought that may be relevant.</p>"
                "<p>The area I would pressure-test is {role_lens}.</p>"
                f"<p>For {html.escape(strategy['market'])} teams, Odoo can help connect the pieces that usually matter first: {html.escape(strategy['modules'])}.</p>"
                "<p>{reply_cta}</p>"
            ),
        },
        {
            "subject": "{customer_language}",
            "body": (
                "<p>Hi {first_name},</p>"
                "<p>Not sure whether this is already handled at {company}.</p>"
                "<p>The phrase that stood out to me was {customer_language}. That usually means the real issue is not one app, but the handoff between sales, operations, billing, and reporting.</p>"
                "<p>If that is even partly true, Odoo is useful because {odoo_win}</p>"
                "<p>{reply_cta}</p>"
            ),
        },
        {
            "subject": "Before the next renewal",
            "body": (
                "<p>Hi {first_name},</p>"
                f"<p>One reason I wanted to reach {{company}} now: {html.escape(strategy['renewal_line'])}</p>"
                "<p>I would not assume a switch makes sense. I would just pressure-test whether the current stack still fits the way the business actually runs today.</p>"
                "<p>{reply_cta}</p>"
            ),
        },
        {
            "subject": "Where Odoo may fit",
            "body": (
                "<p>Hi {first_name},</p>"
                "<p>If I were looking for the Odoo fit at {company}, I would start with this: {reason_to_believe}.</p>"
                f"<p>That usually shows whether tools like {html.escape(software_phrase)} are doing the job, or whether the team is patching together sales, operations, inventory, service, and billing manually.</p>"
                "<p>{reply_cta}</p>"
            ),
        },
        {
            "subject": "Should I aim this at someone else?",
            "body": (
                "<p>Hi {first_name},</p>"
                f"<p>I may be aiming this at the wrong person, but the issue I am asking about is pretty specific: {html.escape(strategy['pain'])}.</p>"
                "<p>Who owns follow-up, estimates, or sales process for {company}?</p>"
            ),
        },
        {
            "subject": "Closing the loop on {company}",
            "body": (
                "<p>Hi {first_name},</p>"
                f"<p>I do not want to keep nudging if this is not useful. I reached out because {html.escape(strategy['market'])} companies often have hidden revenue sitting in {html.escape(strategy['holes'])}.</p>"
                "<p>Should I close this out, or is it worth a quick reply?</p>"
            ),
        },
        {
            "subject": "Last note for now",
            "body": (
                "<p>Hi {first_name},</p>"
                f"<p>Last note from me. If {html.escape(strategy['angle'])} becomes a priority at {{company}}, I would be glad to compare notes.</p>"
                "<p>Either way, thanks for reading.</p>"
            ),
        },
    ]
    while len(templates) < count:
        n = len(templates) + 1
        templates.append({
            "subject": "Follow-up idea for {company} - email " + str(n),
            "body": (
                "<p>Hi {first_name},</p>"
                f"<p>Still thinking about the same possible gap for {{company}}: {html.escape(strategy['holes'])}.</p>"
                f"<p>The useful next step would be to pressure-test whether {html.escape(strategy['angle'])} would move the needle.</p>"
                "<p>Open to a quick reply?</p>"
            ),
        })
    return templates[:count]


ODOO_RELEASE_INTEL = {
    "17": {
        "released": "October/November 2023",
        "label": "Odoo 17",
        "general": [
            "new UI and UX improvements",
            "advanced search and better custom filters",
            "ChatGPT text generation in the editor/powerbox",
            "Odoo PWA for easier access on devices",
            "stage duration tracking and stronger list/search usability",
        ],
        "workflow": {
            "sales": "Sales and CRM workflows improved around stage visibility, quotation flow, down payments, attractive quotations, mass quotation cancellation, and locked sales orders.",
            "inventory": "Inventory and Barcode improved with forecast reservations, multistep route support, stock aging, packaging visibility, manual barcode entry, and manufacturing from Barcode.",
            "manufacturing": "Manufacturing added stronger MO overview, component arrival planning, late component filters, work order dependencies, and a dedicated Shop Floor app.",
            "accounting": "Accounting improved invoice upload/OCR, vendor bill matching, invoice layouts, Peppol, bank reconciliation, accounting reports, and auditability.",
            "website": "Website/eCommerce improved with website conversion, ChatGPT website configurator, B2B/B2C price display settings, shipping without Inventory, and richer website layouts.",
            "service": "Service workflows benefited from stronger appointments, field-service-adjacent website forms, planning, projects, and task generation from sales/project work.",
        },
    },
    "18": {
        "released": "October 2024",
        "label": "Odoo 18",
        "general": [
            "dedicated PWAs for Barcode, PoS, Attendances, Kiosk, Registration Desk, and Shop Floor",
            "many new and updated industry packages",
            "stronger invoicing, vendor bill, and accounting automation",
            "more dynamic purchase and inventory planning flows",
            "broader WhatsApp use in follow-ups and operational communication",
        ],
        "workflow": {
            "sales": "Sales moved forward with better quotation/portal flows, partial payments, customer invoice payment handling, paid-invoice notifications, and more flexible sales/order handoffs.",
            "inventory": "Inventory improved with late availability filters, forecasted demand, multiple routes on sales order lines, simplified physical inventory, WhatsApp shipping notifications, and stronger purchase/forecast links.",
            "manufacturing": "Manufacturing and Shop Floor improved with Gantt views, MO deadlines, serial/lot generation, better subcontracting support, batch-size defaults, valuation choices, and a design update to Shop Floor.",
            "accounting": "Accounting improved with duplicate invoice detection, vendor bill matching/imports, preferred invoice sending methods, draft invoice payments, invoice analysis, and automated Accounting/Documents integration.",
            "website": "Website/eCommerce improved with product page/shop setup, eCommerce filters and product display improvements, Website settings cleanup, and modern building blocks.",
            "service": "Service workflows improved through appointment-to-field-service handoffs, scheduled end dates for dispatch planning, website request capture, and WhatsApp reminders/communication.",
        },
    },
    "19": {
        "released": "September 2025",
        "label": "Odoo 19",
        "general": [
            "AI agents that can learn from documents and act on database records",
            "ChatGPT 5.0 availability",
            "Live Chat AI agents that can generate leads",
            "new and updated industry packages",
            "broad improvements across Inventory, Manufacturing, Sales, Website, WhatsApp, and Accounting",
        ],
        "workflow": {
            "sales": "Sales improved around commissions, paid-invoice notifications, sales order/quotation handling, optional quotation sections, service-product task templates, and portal payment flows.",
            "inventory": "Inventory improved with expiration-aware forecast reporting, simplified valuation, late availability filters, multiple replenishment routes on sales lines, and stronger forecast/MPS planning.",
            "manufacturing": "Manufacturing improved with Gantt views, MO deadline control, lot/serial generation from MOs, better split orders, subcontracting support, Shop Floor design updates, and barcode-compatible component consumption.",
            "accounting": "Accounting improved through better Documents integration, invoice/vendor bill automation, e-invoice/localization improvements, and document-to-journal-entry server actions.",
            "website": "Website/eCommerce improved with simplified website setup, modern blocks, product/shop setup, field validation, WhatsApp sign/request flows, and stronger commerce integrations.",
            "service": "Service workflows improved with appointment details flowing into field service tasks, better dispatch planning, website forms, WhatsApp reminders, and operational follow-through.",
        },
    },
    "19.4": {
        "released": "Odoo 19.4",
        "label": "Odoo 19.4",
        "general": [
            "AI agents connected with external tools",
            "smarter website creation",
            "new sales and eCommerce capabilities",
            "workflow improvements across daily operations",
            "specific updates in Sales, Inventory, Barcode, Manufacturing, Accounting, Website, and WhatsApp",
        ],
        "workflow": {
            "sales": "Sales in 19.4 adds more flexible sales order lines, quotation template reuse, improved sales order emails, and paid-amount commission logic.",
            "inventory": "Inventory and Barcode in 19.4 improve forecast allocation, stock-at-a-past-date reporting, Barcode counts/backorders, stable Barcode button placement, and packaging barcode access.",
            "manufacturing": "Manufacturing in 19.4 improves control over draft versus confirmed MOs and simplifies Produce flows across Manufacturing, Shop Floor, and Barcode.",
            "accounting": "Accounting in 19.4 improves duplicate invoice warnings, vendor bill and purchase order matching, and tax-included/tax-excluded handling across sales, purchasing, and invoicing.",
            "website": "Website, eCommerce, and WhatsApp in 19.4 add AI-powered website positioning choices, eCommerce order/filter improvements, one-click WhatsApp website conversations, and default recipients on WhatsApp templates.",
            "service": "Service workflows in 19.4 benefit from appointment pages, website request capture, WhatsApp, inventory, invoicing, and operational handoffs becoming more connected.",
        },
    },
}


def workflow_keys_for_lead(lead, strategy):
    text = " ".join([
        strategy.get("modules") or "",
        lead.get("industry") or "",
        lead.get("sub_industry") or "",
        lead.get("software_used") or "",
        lead.get("notes") or "",
        lead.get("pain_points") or "",
        lead.get("industry_holes") or "",
    ]).lower()
    keys = []
    if any(word in text for word in ["sales", "crm", "quote", "quotation", "commission", "follow-up", "pipeline"]):
        keys.append("sales")
    if any(word in text for word in ["inventory", "warehouse", "stock", "barcode", "fulfillment", "shipping"]):
        keys.append("inventory")
    if any(word in text for word in ["manufacturing", "shop floor", "production", "work order", "bom", "procurement"]):
        keys.append("manufacturing")
    if any(word in text for word in ["accounting", "invoice", "bill", "purchase order", "payment", "finance", "reconciliation"]):
        keys.append("accounting")
    if any(word in text for word in ["website", "ecommerce", "e-commerce", "online", "whatsapp", "marketing"]):
        keys.append("website")
    if any(word in text for word in ["field service", "service", "appointment", "dispatch", "maintenance"]):
        keys.append("service")
    return keys or ["sales", "accounting"]


def odoo_source_note():
    return "Official Odoo 17, 18, 19, and 19.4 release notes; version is matched from evaluation date/year when available, otherwise from the imported evaluation version."


def inferred_eval_version(lead):
    date_text = str(lead.get("odoo_evaluation_date") or "").strip()
    year_text = str(lead.get("odoo_evaluation_year") or "").strip()
    inferred_from_time = infer_version_from_evaluation_time(date_text, year_text)
    if inferred_from_time:
        return inferred_from_time
    raw = str(lead.get("odoo_evaluation_version") or "").strip().lower().replace("odoo", "").replace("v", "").strip()
    for version in ("17", "18", "19"):
        if raw.startswith(version):
            return version
    return "18"


def infer_version_from_evaluation_time(date_text, year_text=""):
    value = (date_text or "").strip()
    year_hint = (year_text or "").strip()
    if value:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            eval_dt = datetime.strptime(value, "%Y-%m-%d").date()
        elif re.match(r"^\d{4}-\d{2}$", value):
            eval_dt = datetime.strptime(value + "-15", "%Y-%m-%d").date()
        elif re.match(r"^\d{4}$", value):
            eval_dt = datetime.strptime(value + "-07-01", "%Y-%m-%d").date()
        else:
            eval_dt = None
            month_match = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{4})", value.lower())
            if month_match:
                month = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"].index(month_match.group(1)[:3]) + 1
                eval_dt = datetime(int(month_match.group(2)), month, 15).date()
        if eval_dt:
            if eval_dt < datetime(2023, 11, 1).date():
                return "16"
            if eval_dt < datetime(2024, 10, 1).date():
                return "17"
            if eval_dt < datetime(2025, 9, 1).date():
                return "18"
            return "19"
    year = re.sub(r"[^0-9]", "", year_hint)
    if year:
        year_num = int(year[:4])
        if year_num <= 2023:
            return "17"
        if year_num == 2024:
            return "18"
        return "19"
    return ""


def release_delta_versions(eval_version):
    if eval_version == "16":
        return ["17", "18", "19", "19.4"]
    if eval_version == "17":
        return ["18", "19", "19.4"]
    if eval_version == "18":
        return ["19", "19.4"]
    return ["19.4"]


def odoo_release_delta_copy(lead, strategy):
    eval_version = inferred_eval_version(lead)
    releases = release_delta_versions(eval_version)
    workflow_keys = workflow_keys_for_lead(lead, strategy)
    lines = []
    for version in releases:
        info = ODOO_RELEASE_INTEL[version]
        workflow_line = next((info["workflow"][key] for key in workflow_keys if key in info["workflow"]), "")
        general = "; ".join(info["general"][:2])
        if workflow_line:
            lines.append(f"{info['label']} ({info['released']}): {workflow_line}")
        else:
            lines.append(f"{info['label']} ({info['released']}): {general}.")
    return " ".join(lines[:3]), eval_version


def lost_blocker_angle(lead):
    text = " ".join([
        lead.get("previous_blocker") or "",
        lead.get("lost_category") or "",
        lead.get("lost_reason") or "",
        lead.get("previous_demo_notes") or "",
    ]).lower()
    if any(word in text for word in ["budget", "price", "cost", "expensive", "roi"]):
        return "If budget was the blocker, frame the revisit around a narrower first workflow, measurable admin time saved, and fewer disconnected tools instead of a broad ERP purchase."
    if any(word in text for word in ["timing", "bandwidth", "busy", "not ready", "later", "priority"]):
        return "If timing or internal bandwidth killed it, make the ask smaller: one workflow, one owner, and a short check on whether the current version removes the old friction."
    if any(word in text for word in ["competitor", "current system", "netsuite", "quickbooks", "salesforce", "hubspot", "legacy"]):
        return "If they stayed with another system, compare against the handoffs and workarounds that usually appear after months of daily use, not against a generic feature list."
    if any(word in text for word in ["feature", "missing", "fit", "custom", "workflow", "gap"]):
        return "If fit or missing functionality was the blocker, anchor the note on the exact workflow they evaluated and what changed in the later Odoo versions."
    if any(word in text for word in ["implementation", "complex", "migration", "training", "change management"]):
        return "If implementation complexity was the concern, position a phased revisit around the highest-friction workflow first, with less disruption than a full-system restart."
    if any(word in text for word in ["no response", "ghost", "unresponsive", "stale"]):
        return "If the deal simply went quiet, use a clean yes/no reopen ask so the rep can either restart the thread or keep it closed without awkward follow-up."
    return "Lead with what changed since their demo, then ask whether the old blocker still applies before trying to resell the whole platform."


def lost_recovery_templates(lead):
    strategy = lead_strategy(lead)
    category = lead.get("lost_category") or "not the right time"
    reason = lead.get("lost_reason") or "it did not seem like a fit at the time"
    evaluation_year = lead.get("odoo_evaluation_year") or ""
    upgrade_angle, eval_version = odoo_release_delta_copy(lead, strategy)
    blocker_angle = lost_blocker_angle(lead)
    apps_evaluated = lead.get("apps_evaluated") or strategy["modules"]
    demo_notes = lead.get("previous_demo_notes") or ""
    evaluation_version = lead.get("odoo_evaluation_version") or ("pre-Odoo 17" if eval_version == "16" else f"Odoo {eval_version}")
    evaluation_phrase = f"around {evaluation_version} in {evaluation_year}" if evaluation_year else f"around {evaluation_version}"
    return [
        {
            "subject": "Revisiting your Odoo evaluation",
            "body": (
                "<p>Hi {first_name},</p>"
                f"<p>I saw {html.escape(lead.get('company') or 'your team')} went through an Odoo demo/evaluation {html.escape(evaluation_phrase)} and decided not to move forward at the time.</p>"
                f"<p>My note says the reason was: {html.escape(reason)}.</p>"
                f"<p>The smarter revisit angle is: {html.escape(blocker_angle)}</p>"
                f"<p>I would keep it focused on {html.escape(apps_evaluated)} rather than a broad Odoo overview.</p>"
                f"<p>I am not reaching out to restart the same conversation. I am reaching out because the version you saw and Odoo 19.4 are meaningfully different.</p>"
                f"<p>For your workflow, the relevant changes since then are: {html.escape(upgrade_angle)}</p>"
                f"<p>If I were to revisit it, I would focus on {html.escape(strategy['angle'])}, not a broad Odoo overview.</p>"
                "<p>Worth a fresh look, or should I keep this closed?</p>"
            ),
        },
        {
            "subject": "What changed since your Odoo demo?",
            "body": (
                "<p>Hi {first_name},</p>"
                f"<p>When {html.escape(lead.get('company') or 'your team')} looked at Odoo {html.escape(evaluation_phrase)}, it sounds like the timing or fit was not strong enough to move forward.</p>"
                f"{'<p>The prior demo note I have is: ' + html.escape(demo_notes) + '</p>' if demo_notes else ''}"
                f"<p>For {html.escape(strategy['market'])} teams, the thing that usually changes the equation later is {html.escape(strategy['holes'])}.</p>"
                f"<p>What is different now is not just a new pitch. Since the version you evaluated, the areas most likely to matter for you changed in these ways: {html.escape(upgrade_angle)}</p>"
                f"<p>Based on the old blocker, I would pressure-test this first: {html.escape(blocker_angle)}</p>"
                "<p>Has anything changed since the original demo?</p>"
            ),
        },
        {
            "subject": "Should this stay closed?",
            "body": (
                "<p>Hi {first_name},</p>"
                "<p>I do not want to reopen an old Odoo thread if the answer is still no.</p>"
                f"<p>I reached back out because companies often reconsider Odoo months or years later when the original blocker changes: timing, internal bandwidth, budget, current software limits, or a clearer workflow need around {html.escape(strategy['angle'])}.</p>"
                f"<p>The narrow reason I would revisit it now: {html.escape(blocker_angle)}</p>"
                f"<p>Since your prior evaluation, the short version is: {html.escape(upgrade_angle)}</p>"
                "<p>Should I keep {company} closed, or is there a better time/person for a more focused revisit?</p>"
            ),
        },
    ]


def get_lead_email_templates(lead, settings):
    count = max(1, min(30, int(lead.get("email_plan_count") or settings.get("email_plan_count") or DEFAULT_EMAIL_PLAN_COUNT)))
    raw = lead.get("lead_email_templates")
    if raw:
        try:
            templates = json.loads(raw)
            if isinstance(templates, list) and templates:
                return json.loads(normalize_templates(templates, count))
        except (TypeError, json.JSONDecodeError):
            pass
    return generated_lead_templates(lead, count)


def add_timeline(conn, lead_id, type_, text):
    conn.execute(
        "INSERT INTO timeline(lead_id, ts, type, text) VALUES(?, ?, ?, ?)",
        (lead_id, now_iso(), type_, text),
    )
    conn.commit()


def suppression_match(conn, company, email_addr):
    ck = company_key(company)
    email_addr = (email_addr or "").strip().lower()
    rows = conn.execute(
        "SELECT * FROM suppressed WHERE restored=0 AND (company_key=? OR lower(email)=?)",
        (ck, email_addr),
    ).fetchall()
    return len(rows) > 0


def lead_by_id(conn, lead_id):
    return row_to_dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())


def lead_timeline(conn, lead_id):
    rows = conn.execute(
        """
        SELECT ts, type, text FROM timeline WHERE lead_id=?
        UNION ALL SELECT ts, 'Note' AS type, text FROM notes WHERE lead_id=?
        UNION ALL
        SELECT COALESCE(done_ts, created), CASE WHEN done=1 THEN 'Activity done' ELSE 'Activity' END,
               type || CASE WHEN due IS NOT NULL AND due != '' THEN ' due ' || due ELSE '' END ||
               CASE WHEN note IS NOT NULL AND note != '' THEN ': ' || note ELSE '' END
        FROM activities WHERE lead_id=?
        ORDER BY ts DESC
        """,
        (lead_id, lead_id, lead_id),
    ).fetchall()
    return [dict(row) for row in rows]


def personalization_score(lead):
    checks = [
        bool((lead.get("contact") or "").strip()),
        bool((lead.get("email") or "").strip()) and not is_generic_email(lead.get("email")),
        not bool(lead.get("email_risky")),
        bool((lead.get("industry") or "").strip()),
        bool((lead.get("sub_industry") or "").strip()),
        bool((lead.get("software_used") or "").strip()),
        bool((lead.get("custom_first_line") or "").strip()),
        bool((lead.get("research_evidence") or "").strip()),
        bool((lead.get("reason_to_believe") or "").strip()),
        bool((lead.get("customer_language") or "").strip()),
        bool((lead.get("confidence") or "").strip() in ("High", "Medium")),
        bool((lead.get("pain_points") or "").strip() or (lead.get("industry_holes") or "").strip()),
        bool((lead.get("value_angle") or "").strip() or (lead.get("proof_points") or "").strip()),
        bool((lead.get("reply_cta") or "").strip() or (lead.get("cta_style") or "").strip()),
    ]
    return round((sum(checks) / len(checks)) * 100)


def reply_cta_for_style(style):
    style = (style or "").strip().lower()
    options = {
        "soft question": "Worth comparing?",
        "send info": "Should I send over the quick version?",
        "wrong person": "Am I reaching the right person for this?",
        "permission": "Open to a quick look?",
        "direct": "Would a 10 minute walkthrough be useful?",
        "breakup": "Should I close this out for now?",
    }
    return options.get(style, "Worth comparing?")


def infer_sequence_angle(lead):
    blob = " ".join(
        str(lead.get(key) or "")
        for key in ("industry", "sub_industry", "software_used", "notes", "pain_points", "industry_holes")
    ).lower()
    if lead.get("software_renewal_date"):
        return "Software renewal"
    if any(term in blob for term in ("quickbooks", "spreadsheet", "excel", "sheets")):
        return "QuickBooks / spreadsheet sprawl"
    if any(term in blob for term in ("servicetitan", "field service", "hvac", "foundation", "dispatch", "technician")):
        return "Inventory / field service gap"
    if any(term in blob for term in ("follow-up", "follow up", "quote", "estimate", "proposal")):
        return "Missed follow-up"
    if any(term in blob for term in ("netsuite", "salesforce", "hubspot", "procore", "shopify", "sap")):
        return "Competitor replacement"
    return "Operational pain"


def cta_for_angle(angle):
    if angle == "Software renewal":
        return "Worth comparing before the next renewal locks in?"
    if angle == "QuickBooks / spreadsheet sprawl":
        return "Open to a quick look at what this would replace?"
    if angle == "Wrong person":
        return "Am I reaching the right person for this?"
    if angle == "Missed follow-up":
        return "Should I send over the quick version?"
    if angle == "Inventory / field service gap":
        return "Worth a quick look?"
    if angle == "Competitor replacement":
        return "Worth comparing against what you use today?"
    return "Worth comparing?"


def cta_style_for_angle(angle):
    if angle == "Wrong person":
        return "Wrong person"
    if angle in ("Missed follow-up",):
        return "Send info"
    if angle in ("QuickBooks / spreadsheet sprawl", "Inventory / field service gap"):
        return "Permission"
    return "Soft question"


def role_lens_for_title(title):
    text = (title or "").lower()
    if any(x in text for x in ("owner", "founder", "ceo", "president")):
        return "simplicity, cost control, fewer admin headaches, and clearer visibility"
    if any(x in text for x in ("vp operations", "operations", "coo", "general manager", "gm")):
        return "handoffs, throughput, accountability, reporting, and fewer disconnected workflows"
    if any(x in text for x in ("sales", "revenue", "business development")):
        return "quote follow-up, pipeline leakage, activity visibility, and faster handoff to operations"
    if any(x in text for x in ("plant", "manufacturing", "production")):
        return "RFQs, inventory, production status, purchasing, and scheduling visibility"
    if any(x in text for x in ("estimator", "project manager", "project")):
        return "proposal follow-up, job handoff, document flow, and margin visibility"
    if any(x in text for x in ("finance", "controller", "accounting", "cfo")):
        return "invoices, margin, reporting, approvals, and fewer reconciliation gaps"
    return "cleaner follow-up, fewer handoff gaps, and less work living in side tools"


def clean_email_phrase(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"^(question|answer|goal|reply goal|reply type goal)\s*:\s*", "", text, flags=re.I)
    text = re.sub(r"^(owner|operations|sales|plant|project|finance|operator)\s+lens\s*:\s*", "", text, flags=re.I)
    text = re.sub(r"^(workflow diagnosis|workflow angle|value angle)\s*:\s*", "", text, flags=re.I)
    return text.strip(" -:;")


def clean_first_line(value):
    text = clean_email_phrase(value).strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    if text[-1] not in ".!?":
        text += "."
    return text


def claim_safety_for_confidence(confidence):
    value = (confidence or "").strip().lower()
    if value == "high":
        return "Saw"
    if value == "medium":
        return "Looks like"
    return "For teams like yours"


def customer_language_for_lead(lead):
    candidates = [
        lead.get("sub_industry"),
        lead.get("research_evidence"),
        lead.get("notes"),
        lead.get("industry"),
    ]
    for item in candidates:
        text = re.sub(r"\s+", " ", item or "").strip()
        if text:
            return text[:90].rstrip(" ,.;")
    return "operations and follow-up"


def reply_type_goal_for_angle(angle):
    if angle == "Wrong person":
        return "get a referral to the right owner"
    if angle == "Software renewal":
        return "get permission to compare before renewal"
    if angle == "Missed follow-up":
        return "get a yes to send the quick version"
    if angle == "Competitor replacement":
        return "learn what system they use today"
    return "get a simple yes/no reply"


def auto_first_line(lead):
    company = lead.get("company") or "your team"
    market = lead.get("sub_industry") or lead.get("industry") or "your space"
    location = ", ".join([x for x in [lead.get("city"), lead.get("state")] if x])
    software = lead.get("software_used") or lead_strategy(lead)["software"]
    evidence = re.sub(r"\s+", " ", lead.get("research_evidence") or "").strip()
    notes = re.sub(r"\s+", " ", lead.get("notes") or "").strip()
    claim = lead.get("claim_safety") or claim_safety_for_confidence(lead.get("confidence"))
    if evidence:
        clean_evidence = evidence[:135].rstrip(" ,.;")
        if claim == "Saw":
            return f"Saw {clean_evidence if clean_evidence else company}."
        if claim == "Looks like":
            return f"Looks like {clean_evidence if clean_evidence else company}."
        return f"For teams like {company}, {clean_evidence if clean_evidence else market} can create handoff gaps."
    if notes:
        clean_note = notes[:135].rstrip(" ,.;")
        return f"Saw {company} has a few moving pieces around {clean_note.lower()}."
    if software:
        return f"Saw {company} is in {market} and likely has handoffs around tools like {software.split(',')[0].strip()}."
    if location:
        return f"Saw {company} is a {market} team in {location}."
    return f"Saw {company} is in {market}, where follow-up and operational handoffs can get scattered fast."


def auto_tailor_lead(conn, lead_id, force_templates=False):
    lead = lead_by_id(conn, lead_id)
    if not lead:
        return None
    strategy = lead_strategy(lead)
    angle = lead.get("sequence_angle") or infer_sequence_angle(lead)
    cta_style = lead.get("cta_style") or cta_style_for_angle(angle)
    reply_cta = clean_first_line(lead.get("reply_cta") or cta_for_angle(angle))
    custom_first_line = clean_first_line(lead.get("custom_first_line") or auto_first_line(lead))
    reply_goal = lead.get("reply_goal") or "Get a simple reply, not a meeting commitment"
    role_lens = clean_email_phrase(lead.get("role_lens") or role_lens_for_title(lead.get("title")))
    claim_safety = lead.get("claim_safety") or claim_safety_for_confidence(lead.get("confidence"))
    customer_language = clean_email_phrase(lead.get("customer_language") or customer_language_for_lead(lead))
    reply_type_goal = clean_email_phrase(lead.get("reply_type_goal") or reply_type_goal_for_angle(angle))
    reason_to_believe = clean_email_phrase(lead.get("reason_to_believe") or lead.get("research_evidence") or lead.get("proof_points") or customer_language)
    current_templates = lead.get("lead_email_templates") or ""
    should_generate_templates = (
        force_templates
        or not current_templates.strip()
        or "{custom_first_line}" not in current_templates
        or "{reply_cta}" not in current_templates
        or "before I try you by phone" in current_templates
        or "20 million" in current_templates
        or "quick call" in current_templates.lower()
        or "dozens of apps" in current_templates.lower()
        or "odoo angle i would test" in current_templates.lower()
        or "{reply_type_goal}: {role_lens}" in current_templates.lower()
        or "owner lens:" in current_templates.lower()
        or "operations lens:" in current_templates.lower()
    )
    updates = {
        "sequence_angle": angle,
        "custom_first_line": custom_first_line,
        "reply_goal": reply_goal,
        "cta_style": cta_style,
        "reply_cta": reply_cta,
        "pain_points": lead.get("pain_points") or strategy["pain"],
        "industry_holes": lead.get("industry_holes") or strategy["holes"],
        "value_angle": lead.get("value_angle") or strategy["angle"],
        "proof_points": lead.get("proof_points") or strategy["proof"],
        "company_summary": lead.get("company_summary") or "",
        "why_now": lead.get("why_now") or "",
        "workflow_hypothesis": lead.get("workflow_hypothesis") or "",
        "research_evidence": lead.get("research_evidence") or "",
        "research_source_url": lead.get("research_source_url") or "",
        "confidence": lead.get("confidence") or "",
        "do_not_claim": lead.get("do_not_claim") or "",
        "reason_to_believe": reason_to_believe,
        "role_lens": role_lens,
        "claim_safety": claim_safety,
        "customer_language": customer_language,
        "reply_type_goal": reply_type_goal,
        "updated": now_iso(),
    }
    if should_generate_templates:
        tailored_lead = dict(lead, **updates)
        updates["lead_email_templates"] = json.dumps(generated_lead_templates(tailored_lead))
    assignments = ", ".join([f"{key}=?" for key in updates])
    conn.execute(
        f"UPDATE leads SET {assignments} WHERE id=?",
        list(updates.values()) + [lead_id],
    )
    conn.commit()
    if not lead.get("custom_first_line") or not lead.get("reply_cta") or should_generate_templates:
        add_timeline(conn, lead_id, "Auto-tailored", f"Prepared {angle} email angle for Gmail drafting")
    return lead_by_id(conn, lead_id)


def normalize_lead_payload(data, existing=None):
    existing = existing or {}
    fields = [
        "company", "contact", "title", "email", "email_generic", "phone", "website", "city",
        "state", "industry", "sub_industry", "software_used", "software_renewal_date",
        "odoo_evaluation_version", "odoo_evaluation_date", "odoo_evaluation_year",
        "previous_demo_notes", "previous_blocker", "apps_evaluated", "recovery_last_drafted",
        "source", "stage", "notes", "next_action", "next_action_date",
        "pain_points", "industry_holes", "value_angle", "proof_points", "company_summary", "why_now",
        "workflow_hypothesis", "research_evidence", "research_source_url", "confidence", "do_not_claim",
        "reason_to_believe", "role_lens", "claim_safety", "customer_language", "reply_type_goal", "lead_email_templates",
        "sequence_angle", "custom_first_line", "reply_goal", "cta_style", "reply_cta", "reply_outcome", "reply_outcome_ts",
        "lost_category", "lost_reason", "lost_ts",
    ]
    clean = {}
    for field in fields:
        clean[field] = str(data.get(field, existing.get(field, "")) or "").strip()
    clean["company"] = clean["company"] or "Untitled Company"
    clean["stage"] = clean["stage"] if clean["stage"] in STAGES else existing.get("stage", "New")
    priority_manual = int(data.get("priority_manual", existing.get("priority_manual", 0)) or 0)
    if "priority" in data:
        priority = max(0, min(3, int(data.get("priority") or 0)))
        priority_manual = 1
    else:
        priority = int(existing.get("priority", 0) or 0)
    probe = dict(existing, **clean)
    if not priority_manual:
        priority = auto_priority(probe)
    clean["priority"] = priority
    clean["priority_manual"] = priority_manual
    plan_default = int(existing.get("email_plan_count") or DEFAULT_EMAIL_PLAN_COUNT)
    clean["email_plan_count"] = max(1, min(30, int(data.get("email_plan_count", plan_default) or plan_default)))
    stage_default = int(existing.get("email_stage") or 1)
    clean["email_stage"] = max(1, min(clean["email_plan_count"] + 1, int(data.get("email_stage", stage_default) or stage_default)))
    clean["email_last_stage"] = int(existing.get("email_last_stage") or 0)
    ok, reason = valid_email(clean["email"]) if clean["email"] else (False, "Missing direct email")
    clean["email_risky"] = 0 if ok or not clean["email"] else 1
    clean["email_risk_reason"] = "" if ok else reason
    return clean


def insert_lead(conn, data):
    clean = normalize_lead_payload(data)
    if suppression_match(conn, clean["company"], clean["email"]):
        return None, "suppressed"
    existing = conn.execute("SELECT id FROM leads WHERE lower(company)=lower(?)", (clean["company"],)).fetchone()
    if existing:
        return existing["id"], "duplicate"
    ts = now_iso()
    cur = conn.execute(
        """
        INSERT INTO leads(company, contact, title, email, email_generic, phone, website, city, state,
            industry, sub_industry, software_used, software_renewal_date, odoo_evaluation_version, odoo_evaluation_date, odoo_evaluation_year,
            previous_demo_notes, previous_blocker, apps_evaluated, recovery_last_drafted,
            source, stage, priority, priority_manual, email_risky, email_risk_reason,
            notes, next_action, next_action_date, pain_points, industry_holes, value_angle, proof_points,
            company_summary, why_now, workflow_hypothesis, research_evidence, research_source_url, confidence, do_not_claim,
            reason_to_believe, role_lens, claim_safety, customer_language, reply_type_goal,
            lead_email_templates, sequence_angle, custom_first_line, reply_goal, cta_style, reply_cta, reply_outcome, reply_outcome_ts,
            lost_category, lost_reason, lost_ts, email_stage, email_plan_count, email_last_stage, created, updated)
        VALUES(:company, :contact, :title, :email, :email_generic, :phone, :website, :city, :state,
            :industry, :sub_industry, :software_used, :software_renewal_date, :odoo_evaluation_version, :odoo_evaluation_date, :odoo_evaluation_year,
            :previous_demo_notes, :previous_blocker, :apps_evaluated, :recovery_last_drafted,
            :source, :stage, :priority, :priority_manual, :email_risky, :email_risk_reason,
            :notes, :next_action, :next_action_date, :pain_points, :industry_holes, :value_angle, :proof_points,
            :company_summary, :why_now, :workflow_hypothesis, :research_evidence, :research_source_url, :confidence, :do_not_claim,
            :reason_to_believe, :role_lens, :claim_safety, :customer_language, :reply_type_goal,
            :lead_email_templates, :sequence_angle, :custom_first_line, :reply_goal, :cta_style, :reply_cta, :reply_outcome, :reply_outcome_ts,
            :lost_category, :lost_reason, :lost_ts, :email_stage, :email_plan_count, :email_last_stage, :created, :updated)
        """,
        dict(clean, created=ts, updated=ts),
    )
    lead_id = cur.lastrowid
    add_timeline(conn, lead_id, "Created", "Lead created")
    conn.commit()
    return lead_id, "created"


def update_lead(conn, lead_id, data):
    existing = lead_by_id(conn, lead_id)
    if not existing:
        return None
    old_stage = existing["stage"]
    old_reply_outcome = existing.get("reply_outcome") or ""
    clean = normalize_lead_payload(data, existing)
    if clean["reply_outcome"] and clean["reply_outcome"] != old_reply_outcome and not clean["reply_outcome_ts"]:
        clean["reply_outcome_ts"] = now_iso()
    clean["id"] = lead_id
    clean["updated"] = now_iso()
    conn.execute(
        """
        UPDATE leads SET company=:company, contact=:contact, title=:title, email=:email,
            email_generic=:email_generic, phone=:phone, website=:website, city=:city, state=:state,
            industry=:industry, sub_industry=:sub_industry, software_used=:software_used, software_renewal_date=:software_renewal_date,
            odoo_evaluation_version=:odoo_evaluation_version, odoo_evaluation_date=:odoo_evaluation_date,
            odoo_evaluation_year=:odoo_evaluation_year, previous_demo_notes=:previous_demo_notes,
            previous_blocker=:previous_blocker, apps_evaluated=:apps_evaluated, recovery_last_drafted=:recovery_last_drafted,
            source=:source, stage=:stage, priority=:priority,
            priority_manual=:priority_manual, email_risky=:email_risky, email_risk_reason=:email_risk_reason,
            notes=:notes, next_action=:next_action, next_action_date=:next_action_date,
            pain_points=:pain_points, industry_holes=:industry_holes, value_angle=:value_angle,
            proof_points=:proof_points, company_summary=:company_summary, why_now=:why_now,
            workflow_hypothesis=:workflow_hypothesis, research_evidence=:research_evidence,
            research_source_url=:research_source_url, confidence=:confidence, do_not_claim=:do_not_claim,
            reason_to_believe=:reason_to_believe, role_lens=:role_lens, claim_safety=:claim_safety,
            customer_language=:customer_language, reply_type_goal=:reply_type_goal,
            lead_email_templates=:lead_email_templates,
            sequence_angle=:sequence_angle, custom_first_line=:custom_first_line,
            reply_goal=:reply_goal, cta_style=:cta_style, reply_cta=:reply_cta,
            reply_outcome=:reply_outcome, reply_outcome_ts=:reply_outcome_ts,
            lost_category=:lost_category, lost_reason=:lost_reason, lost_ts=:lost_ts,
            email_stage=:email_stage, email_plan_count=:email_plan_count, email_last_stage=:email_last_stage,
            updated=:updated
        WHERE id=:id
        """,
        clean,
    )
    if clean["stage"] != old_stage:
        add_timeline(conn, lead_id, "Stage", f"{old_stage} -> {clean['stage']}")
    if clean["reply_outcome"] and clean["reply_outcome"] != old_reply_outcome:
        add_timeline(conn, lead_id, "Reply outcome", clean["reply_outcome"])
    conn.commit()
    return lead_by_id(conn, lead_id)


def render_template(text, lead):
    first_name = (lead.get("contact") or "").strip().split(" ", 1)[0]
    industry = lead.get("industry") or "your market"
    sub_industry = lead.get("sub_industry") or industry
    city = lead.get("city") or lead.get("state") or "your area"
    notes = clean_email_phrase(lead.get("notes") or "")
    strategy = lead_strategy(lead)
    role_lens = clean_email_phrase(lead.get("role_lens") or role_lens_for_title(lead.get("title")))
    reply_type_goal = clean_email_phrase(lead.get("reply_type_goal") or reply_type_goal_for_angle(lead.get("sequence_angle") or strategy["angle"]))
    custom_first_line = clean_first_line(lead.get("custom_first_line") or auto_first_line(lead))
    reply_cta = clean_first_line(lead.get("reply_cta") or reply_cta_for_style(lead.get("cta_style")))
    values = {
        "contact": lead.get("contact") or "",
        "first_name": first_name or lead.get("contact") or "",
        "sender_name": lead.get("sender_name") or "Rayhan",
        "company": lead.get("company") or "",
        "city": lead.get("city") or "",
        "city_fallback": city,
        "state": lead.get("state") or "",
        "industry": lead.get("industry") or "",
        "sub_industry": lead.get("sub_industry") or "",
        "software_used": lead.get("software_used") or strategy["software"],
        "software_renewal_date": lead.get("software_renewal_date") or "",
        "odoo_evaluation_version": lead.get("odoo_evaluation_version") or "",
        "odoo_evaluation_date": lead.get("odoo_evaluation_date") or "",
        "odoo_evaluation_year": lead.get("odoo_evaluation_year") or "",
        "previous_demo_notes": lead.get("previous_demo_notes") or "",
        "previous_blocker": lead.get("previous_blocker") or "",
        "apps_evaluated": lead.get("apps_evaluated") or "",
        "recovery_last_drafted": lead.get("recovery_last_drafted") or "",
        "likely_software": strategy["software"],
        "odoo_modules": strategy["modules"],
        "competitor": strategy["competitor"],
        "competitor_angle": strategy["competitor_angle"],
        "odoo_win": strategy["odoo_win"],
        "price_angle": strategy["price_angle"],
        "renewal_angle": strategy["renewal_angle"],
        "renewal_line": strategy["renewal_line"],
        "industry_fallback": industry,
        "market": sub_industry,
        "title": lead.get("title") or "",
        "website": lead.get("website") or "",
        "source": lead.get("source") or "",
        "notes": notes,
        "pain_points": clean_email_phrase(lead.get("pain_points") or strategy["pain"]),
        "industry_holes": clean_email_phrase(lead.get("industry_holes") or strategy["holes"]),
        "value_angle": clean_email_phrase(lead.get("value_angle") or strategy["angle"]),
        "proof_points": clean_email_phrase(lead.get("proof_points") or strategy["proof"]),
        "company_summary": clean_email_phrase(lead.get("company_summary") or ""),
        "why_now": clean_email_phrase(lead.get("why_now") or ""),
        "workflow_hypothesis": clean_email_phrase(lead.get("workflow_hypothesis") or ""),
        "research_evidence": clean_email_phrase(lead.get("research_evidence") or ""),
        "research_source_url": lead.get("research_source_url") or "",
        "confidence": lead.get("confidence") or "",
        "do_not_claim": lead.get("do_not_claim") or "",
        "reason_to_believe": clean_email_phrase(lead.get("reason_to_believe") or ""),
        "role_lens": role_lens,
        "claim_safety": lead.get("claim_safety") or claim_safety_for_confidence(lead.get("confidence")),
        "customer_language": clean_email_phrase(lead.get("customer_language") or customer_language_for_lead(lead)),
        "reply_type_goal": reply_type_goal,
        "sequence_angle": lead.get("sequence_angle") or strategy["angle"],
        "custom_first_line": custom_first_line,
        "reply_goal": lead.get("reply_goal") or "Start a low-friction reply conversation",
        "cta_style": lead.get("cta_style") or "Soft question",
        "reply_cta": reply_cta,
        "personalization_score": str(personalization_score(lead)),
        "next_action": lead.get("next_action") or "",
        "lost_category": lead.get("lost_category") or "",
        "lost_reason": lead.get("lost_reason") or "",
        "lost_ts": lead.get("lost_ts") or "",
        "email_stage": str(lead.get("email_stage") or 1),
        "email_plan_count": str(lead.get("email_plan_count") or DEFAULT_EMAIL_PLAN_COUNT),
    }
    out = text or ""
    for key, value in values.items():
        out = out.replace("{" + key + "}", html.escape(value))
    return out


def html_to_plain_email(body):
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", body or "")
    text = re.sub(r"(?i)</\s*p\s*>", "\n\n", text)
    text = re.sub(r"(?i)<\s*p[^>]*>", "", text)
    text = re.sub(r"(?i)</\s*(div|li|h[1-6])\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    collapsed = []
    blank = False
    for line in lines:
        if not line:
            if not blank and collapsed:
                collapsed.append("")
            blank = True
        else:
            collapsed.append(line)
            blank = False
    return "\n".join(collapsed).strip() + "\n"


def make_mime(settings, lead):
    sender = settings.get("gmail_email", "").strip()
    recipient = (lead.get("email") or "").strip()
    lead = dict(lead)
    lead["sender_name"] = settings.get("sender_name") or "Rayhan"
    templates = get_lead_email_templates(lead, settings)
    step = max(1, int(lead.get("email_stage") or 1))
    template = templates[min(step, len(templates)) - 1]
    subject = html.unescape(render_template(template.get("subject", ""), lead))
    body = render_template(template.get("body", ""), lead)
    signature = settings.get("email_signature", "")
    unsubscribe = (
        "<p style=\"font-size:12px;color:#666\">Not interested? Just reply STOP and I won&apos;t reach out again.</p>"
    )
    tracking_note = (
        "<div style=\"display:none;font-size:1px;color:#fff;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden\">"
        f"CRM email sequence {step} for {html.escape(lead.get('company') or '')}</div>"
    )
    html_body = tracking_note + body + signature + unsubscribe
    plain_body = html_to_plain_email(body + signature + unsubscribe)
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["X-Mailer"] = "Local Sales CRM; Gmail IMAP Draft; MailSuite-compatible HTML"
    msg["X-CRM-Email-Stage"] = str(step)
    msg.set_content(plain_body, subtype="plain", charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")
    return msg


def append_gmail_draft(settings, lead):
    sender = settings.get("gmail_email", "").strip()
    password = settings.get("gmail_app_password", "").replace(" ", "")
    if not sender or not password:
        raise ValueError("Gmail address and App Password are required in Settings")
    msg = make_mime(settings, lead)
    with imaplib.IMAP4_SSL("imap.gmail.com", 993) as imap:
        imap.login(sender, password)
        status, _ = imap.append(
            '"[Gmail]/Drafts"',
            r"(\Draft)",
            imaplib.Time2Internaldate(time.time()),
            msg.as_bytes(),
        )
        if status != "OK":
            raise RuntimeError("Gmail did not accept the draft")
        imap.logout()


def eligible_for_draft(lead):
    if lead["stage"] not in ACTIVE_STAGES:
        return False
    if not lead["email"] or is_generic_email(lead["email"]):
        return False
    if lead["email_risky"]:
        return False
    if int(lead.get("email_stage") or 1) > int(lead.get("email_plan_count") or DEFAULT_EMAIL_PLAN_COUNT):
        return False
    if lead.get("last_emailed") and str(lead["last_emailed"])[:10] == today_iso():
        return False
    return True


def draft_quality_warnings(lead, settings=None):
    settings = settings or {}
    warnings = []
    score = personalization_score(lead)
    min_score = int(settings.get("min_personalization_score") or 0)
    if min_score and score < min_score:
        warnings.append(f"Personalization score below {min_score}%")
    if settings.get("require_contact_name") == "1" and not (lead.get("contact") or "").strip():
        warnings.append("Missing contact name")
    if settings.get("require_industry") == "1" and not (lead.get("industry") or "").strip():
        warnings.append("Missing industry")
    if settings.get("require_custom_first_line") == "1" and not (lead.get("custom_first_line") or "").strip():
        warnings.append("Missing custom first line")
    if settings.get("require_software_stack") == "1" and not (lead.get("software_used") or "").strip():
        warnings.append("Missing software stack")
    if not ((lead.get("reason_to_believe") or "").strip() or (lead.get("research_evidence") or "").strip()):
        warnings.append("Missing concrete reason to believe")
    confidence = (lead.get("confidence") or "").strip()
    if confidence == "Low" and (lead.get("custom_first_line") or "").startswith("Saw "):
        warnings.append("Low confidence row uses high-confidence claim wording")
    cta = (lead.get("reply_cta") or "").strip()
    if cta and "?" not in cta:
        warnings.append("Reply CTA should be a question")
    templates = lead.get("lead_email_templates") or ""
    if templates.count("CRM") + templates.count("Inventory") + templates.count("Manufacturing") + templates.count("Accounting") > 10:
        warnings.append("Email may mention too many Odoo modules")
    if any(phrase in templates.lower() for phrase in ("all-in-one system that 20 million", "dozens of apps", "quick call")):
        warnings.append("Email may sound automated or too salesy")
    warnings.extend(gemini_research_warnings(lead))
    if lead.get("email_risky"):
        warnings.append(lead.get("email_risk_reason") or "Email looks risky")
    return warnings


def gemini_research_warnings(row):
    warnings = []
    raw_source_url = (row.get("research_source_url") or "").strip()
    source_url = "" if raw_source_url.lower() in ("no source", "none", "n/a", "na") else raw_source_url
    confidence = (row.get("confidence") or "").strip().lower()
    first_line = (row.get("custom_first_line") or "").strip()
    value_angle = (row.get("value_angle") or "").strip().lower()
    value_angle_raw = (row.get("value_angle") or "").strip()
    workflow_raw = (row.get("workflow_hypothesis") or "").strip()
    pain_points = (row.get("pain_points") or "").strip().lower()
    proof_points = (row.get("proof_points") or "").strip()
    evidence = (row.get("research_evidence") or "").strip()
    do_not_claim = (row.get("do_not_claim") or "").strip().lower()
    weak_evidence = evidence.lower()
    generic_phrases = [
        "consolidate operations into a single platform",
        "disconnected business systems",
        "double data entry",
        "lack of real-time inventory visibility",
        "evaluating operational tools or experiencing system fragmentation",
        "do not claim we know their exact renewal date",
        "teams often struggle",
        "flowing smoothly",
        "manual re-entry between systems",
        "managing handoffs across disconnected tools can get messy",
        "confirm software and workflow",
        "architectural friction",
        "operational pathologies",
        "unified architecture",
        "unified ecosystem",
        "incredible",
        "impressive",
        "tremendous",
        "highly innovative",
    ]
    blob = " ".join([value_angle, workflow_raw.lower(), first_line.lower(), pain_points, do_not_claim]).lower()
    if not source_url and first_line.lower().startswith("saw "):
        warnings.append("Gemini used 'Saw' without a source URL")
    if not source_url and confidence in ("high", "medium"):
        warnings.append("Gemini confidence is too high without a source URL")
    if any(phrase in blob for phrase in generic_phrases):
        warnings.append("Gemini row uses generic boilerplate")
    if "browsing unavailable" in weak_evidence:
        warnings.append("Low-source row needs verification")
    if value_angle_raw and len(value_angle_raw.split()) < 8:
        warnings.append("Workflow angle is too thin")
    if workflow_raw and len(workflow_raw.split()) < 12:
        warnings.append("Workflow hypothesis needs more detail")
    if confidence in ("high", "medium") and weak_evidence in ("company name", "domain name", "odoo export", "inferred from name", "inferred from domain"):
        warnings.append("Gemini confidence is too high for weak evidence")
    if not source_url and first_line.lower().startswith("looks like"):
        warnings.append("Gemini used 'Looks like' without a source URL")
    if confidence in ("high", "medium") and not proof_points:
        warnings.append("Gemini confidence needs proof points")
    if evidence and not source_url and confidence == "high":
        warnings.append("High-confidence evidence needs a source URL")
    if len(first_line) > 170:
        warnings.append("Custom first line is too long")
    return warnings


def passes_draft_quality(lead, settings):
    return not draft_quality_warnings(lead, settings)


def preview_email_for_lead(settings, lead):
    lead = dict(lead)
    lead["sender_name"] = settings.get("sender_name") or "Rayhan"
    templates = get_lead_email_templates(lead, settings)
    step = max(1, int(lead.get("email_stage") or 1))
    template = templates[min(step, len(templates)) - 1]
    subject = html.unescape(render_template(template.get("subject", ""), lead))
    body_html = render_template(template.get("body", ""), lead)
    body_text = re.sub(r"\s+", " ", html_to_plain_email(body_html)).strip()
    return {
        "subject": subject,
        "body_preview": body_text[:360],
    }


def draft_preview_rows(conn, limit, settings):
    rows = conn.execute(
        """
        SELECT * FROM leads
        WHERE stage IN ('New','Contacted','Follow-up','Qualified','Proposal')
          AND email IS NOT NULL AND email != ''
          AND email_stage <= email_plan_count
          AND (last_emailed IS NULL OR last_emailed='' OR date(last_emailed) < date('now','localtime'))
        ORDER BY priority DESC, updated ASC
        LIMIT ?
        """,
        (max(1, min(500, limit)),),
    ).fetchall()
    preview = []
    for row in rows:
        lead = auto_tailor_lead(conn, row["id"]) or dict(row)
        warnings = []
        if suppression_match(conn, lead["company"], lead["email"]):
            warnings.append("Suppressed")
        if not eligible_for_draft(lead):
            warnings.append("Not eligible for draft")
        warnings.extend(draft_quality_warnings(lead, settings))
        email = preview_email_for_lead(settings, lead)
        preview.append({
            "id": lead["id"],
            "company": lead["company"],
            "contact": lead["contact"],
            "email": lead["email"],
            "stage": lead["stage"],
            "email_step": int(lead.get("email_stage") or 1),
            "score": personalization_score(lead),
            "sequence_angle": lead.get("sequence_angle") or "",
            "cta_style": lead.get("cta_style") or "",
            "subject": email["subject"],
            "body_preview": email["body_preview"],
            "warnings": warnings,
            "ready": not warnings,
        })
    return preview


def mark_drafted(conn, lead):
    new_stage = "Contacted" if lead["stage"] == "New" else lead["stage"]
    current_email_stage = max(1, int(lead.get("email_stage") or 1))
    plan_count = max(1, int(lead.get("email_plan_count") or DEFAULT_EMAIL_PLAN_COUNT))
    next_email_stage = min(plan_count + 1, current_email_stage + 1)
    next_action = "Email sequence complete" if current_email_stage >= plan_count else f"Email {next_email_stage}"
    next_action_date = "" if current_email_stage >= plan_count else add_business_days(today_iso(), 3)
    conn.execute(
        """
        UPDATE leads SET last_emailed=?, emailed_count=emailed_count+1, stage=?,
            email_last_stage=?, email_stage=?, next_action=?, next_action_date=?, updated=?
        WHERE id=?
        """,
        (now_iso(), new_stage, current_email_stage, next_email_stage, next_action, next_action_date, now_iso(), lead["id"]),
    )
    add_timeline(conn, lead["id"], "Email draft", f"Email {current_email_stage} draft created for {lead['email']}")
    conn.commit()


def import_csv(conn, content):
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    mapping = {
        "company": ["company", "Company", "Customer", "Customer/Company", "Opportunity", "Opportunity Name", "business", "Business", "name", "Name"],
        "contact": ["contact", "Contact", "Contact Name", "Customer Contact", "person", "Person", "decision maker", "Decision Maker"],
        "title": ["title", "Title", "Job Position", "Function"],
        "email": ["email", "Email", "Email Address", "direct email", "Direct Email"],
        "email_generic": ["email_generic", "generic email", "Generic Email", "info email"],
        "phone": ["phone", "Phone", "Mobile", "telephone", "Telephone"],
        "website": ["website", "Website", "Website Link", "url", "URL"],
        "city": ["city", "City"],
        "state": ["state", "State", "State/Province"],
        "industry": ["industry", "Industry", "Tags", "Odoo Tags"],
        "sub_industry": ["sub_industry", "Sub Industry", "Sub-Industry", "Subindustry", "Gemini Sub Industry"],
        "software_used": ["software_used", "Software Used", "Software Stack", "Likely Software", "Current Software", "Gemini Software Stack"],
        "software_renewal_date": ["software_renewal_date", "Software Renewal Date", "Renewal Date", "Contract Renewal Date"],
        "odoo_evaluation_version": ["odoo_evaluation_version", "Odoo Evaluation Version", "Evaluated Version", "Prior Odoo Version", "Odoo Version Evaluated"],
        "odoo_evaluation_date": ["odoo_evaluation_date", "Odoo Evaluation Date", "Evaluation Date", "Demo Date", "Prior Demo Date"],
        "odoo_evaluation_year": ["odoo_evaluation_year", "Odoo Evaluation Year", "Evaluation Year", "Demo Year"],
        "previous_demo_notes": ["previous_demo_notes", "Previous Demo Notes", "Prior Demo Notes", "Old Demo Notes", "Evaluation Notes"],
        "previous_blocker": ["previous_blocker", "Previous Blocker", "Prior Blocker", "Lost Blocker", "Why They Did Not Move Forward"],
        "apps_evaluated": ["apps_evaluated", "Apps Evaluated", "Odoo Apps Evaluated", "Modules Evaluated", "Prior Apps Evaluated"],
        "source": ["source", "Source", "Lead Source", "Campaign", "Medium"],
        "stage": ["stage", "Stage", "Pipeline Stage"],
        "lost_category": ["lost_category", "Lost Category", "Loss Category", "Lost Reason Category"],
        "lost_reason": ["lost_reason", "Lost Reason", "Reason Lost", "Why Lost"],
        "next_action": ["next_action", "Next Activity", "Next Action", "Activity Summary"],
        "next_action_date": ["next_action_date", "Next Activity Deadline", "Next Action Date", "Due Date"],
        "notes": ["notes", "Notes", "Internal Notes", "Description"],
        "pain_points": ["pain_points", "Pain Points", "Likely Pain Points", "Gemini Pain Points"],
        "industry_holes": ["industry_holes", "Industry Holes", "Workflow Gaps", "Operational Gaps", "Gemini Workflow Gaps"],
        "value_angle": ["value_angle", "Value Angle", "Odoo Value Angle", "Gemini Value Angle"],
        "proof_points": ["proof_points", "Proof Points", "Personalization Proof", "Gemini Proof Points"],
        "company_summary": ["company_summary", "Company Summary", "Account Summary", "Gemini Company Summary"],
        "why_now": ["why_now", "Why Now", "Trigger", "Growth Trigger", "Gemini Why Now"],
        "workflow_hypothesis": ["workflow_hypothesis", "Workflow Hypothesis", "Operating Hypothesis", "Gemini Workflow Hypothesis"],
        "research_evidence": ["research_evidence", "Research Evidence", "Concrete Observation", "Email Opening Evidence", "Gemini Evidence"],
        "research_source_url": ["research_source_url", "Research Source URL", "Source URL", "Evidence URL", "Website Evidence URL"],
        "confidence": ["confidence", "Confidence", "Research Confidence"],
        "do_not_claim": ["do_not_claim", "Do Not Claim", "Avoid Claiming", "Unsupported Claims"],
        "reason_to_believe": ["reason_to_believe", "Reason To Believe", "RTB", "Concrete Proof"],
        "role_lens": ["role_lens", "Role Lens", "Persona Lens", "Buyer Lens"],
        "claim_safety": ["claim_safety", "Claim Safety", "Claim Wording"],
        "customer_language": ["customer_language", "Customer Language", "Mirrored Language", "Website Phrase"],
        "reply_type_goal": ["reply_type_goal", "Reply Type Goal", "Desired Reply Type"],
        "sequence_angle": ["sequence_angle", "Sequence Angle", "Outreach Angle", "Gemini Sequence Angle"],
        "custom_first_line": ["custom_first_line", "Custom First Line", "First Line", "Personalized First Line"],
        "reply_goal": ["reply_goal", "Reply Goal", "Desired Reply"],
        "cta_style": ["cta_style", "CTA Style", "Call To Action Style"],
        "reply_cta": ["reply_cta", "Reply CTA", "CTA", "Reply Call To Action"],
    }
    counts = {"created": 0, "duplicate": 0, "suppressed": 0, "skipped": 0}
    for row in reader:
        if None in row:
            counts["skipped"] += 1
            continue
        data = {}
        for field, aliases in mapping.items():
            data[field] = ""
            for alias in aliases:
                if alias in row and row[alias]:
                    data[field] = row[alias]
                    break
        if not data.get("company"):
            counts["skipped"] += 1
            continue
        _, status = insert_lead(conn, data)
        counts[status] = counts.get(status, 0) + 1
    return counts


def export_odoo_updates_csv(conn, query):
    where, args = filter_where(query)
    rows = conn.execute(f"SELECT * FROM leads {where} ORDER BY last_emailed DESC, updated DESC", args).fetchall()
    output = io.StringIO()
    fields = [
        "Company", "Contact", "Email", "Odoo Suggested Note", "Last Drafted",
        "Email Drafted", "Next Email Step", "Suggested Next Activity",
        "Suggested Next Activity Date", "Stage Suggestion", "Sequence Angle",
        "CTA Style", "Reply CTA", "Reply Outcome", "Personalization Score",
        "Reason To Believe", "Research Evidence", "Research Source URL", "Confidence", "Do Not Claim", "Opt Out Safe",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        email_last_stage = int(row["email_last_stage"] or 0)
        email_stage = int(row["email_stage"] or 1)
        plan_count = int(row["email_plan_count"] or DEFAULT_EMAIL_PLAN_COUNT)
        drafted = "Yes" if email_last_stage else "No"
        next_step = "Sequence complete" if email_stage > plan_count else f"Email {email_stage}"
        note_bits = []
        if email_last_stage:
            note_bits.append(f"Gmail draft created for Email {email_last_stage} on {row['last_emailed']}.")
        else:
            note_bits.append("No Gmail draft has been created from this local assistant yet.")
        if row["next_action"]:
            note_bits.append(f"Next action: {row['next_action']}.")
        if row["next_action_date"]:
            note_bits.append(f"Due: {row['next_action_date']}.")
        if row["email_risky"]:
            note_bits.append(f"Email excluded from drafting: {row['email_risk_reason']}.")
        writer.writerow({
            "Company": row["company"],
            "Contact": row["contact"],
            "Email": row["email"],
            "Odoo Suggested Note": " ".join(note_bits),
            "Last Drafted": row["last_emailed"] or "",
            "Email Drafted": drafted,
            "Next Email Step": next_step,
            "Suggested Next Activity": row["next_action"] or "",
            "Suggested Next Activity Date": row["next_action_date"] or "",
            "Stage Suggestion": row["stage"],
            "Sequence Angle": row["sequence_angle"] or "",
            "CTA Style": row["cta_style"] or "",
            "Reply CTA": row["reply_cta"] or reply_cta_for_style(row["cta_style"]),
            "Reply Outcome": row["reply_outcome"] or "",
            "Personalization Score": personalization_score(dict(row)),
            "Reason To Believe": row["reason_to_believe"] or "",
            "Research Evidence": row["research_evidence"] or "",
            "Research Source URL": row["research_source_url"] or "",
            "Confidence": row["confidence"] or "",
            "Do Not Claim": row["do_not_claim"] or "",
            "Opt Out Safe": "No" if suppression_match(conn, row["company"], row["email"]) else "Yes",
        })
    return output.getvalue().encode("utf-8")


def export_weekly_coaching_csv(conn):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Metric", "Value"])
    rows = conn.execute("SELECT * FROM leads").fetchall()
    leads = [dict(row) for row in rows]
    active = [l for l in leads if l.get("stage") in ACTIVE_STAGES]
    # Calendar-week approximation avoids extra date parsing in the single-file stdlib app.
    drafted_recent = [l for l in leads if l.get("last_emailed") and l["last_emailed"][:10] >= (datetime.now() - timedelta(days=7)).date().isoformat()]
    replies = [l for l in leads if l.get("reply_outcome")]
    positive = [l for l in replies if l.get("reply_outcome") in ("Replied positive", "Booked")]
    opt_outs = conn.execute("SELECT COUNT(*) c FROM suppressed WHERE restored=0").fetchone()["c"]
    overdue = [l for l in active if l.get("next_action_date") and l["next_action_date"] <= today_iso()]
    cleanup = [l for l in active if personalization_score(l) < 60 or not l.get("contact") or not l.get("email") or not l.get("industry")]
    writer.writerow(["Active leads", len(active)])
    writer.writerow(["Drafted last 7 days", len(drafted_recent)])
    writer.writerow(["Replies logged", len(replies)])
    writer.writerow(["Positive/booked replies", len(positive)])
    writer.writerow(["Open opt-outs", opt_outs])
    writer.writerow(["Due or overdue follow-ups", len(overdue)])
    writer.writerow(["Leads needing cleanup", len(cleanup)])
    writer.writerow([])
    writer.writerow(["Breakdown", "Name", "Drafted", "Replies", "Positive/Booked", "Opt-outs/Stops"])
    for label, field in [("Sequence angle", "sequence_angle"), ("CTA style", "cta_style"), ("Industry", "industry")]:
        values = sorted({l.get(field) or "(blank)" for l in leads})
        for value in values:
            subset = [l for l in leads if (l.get(field) or "(blank)") == value]
            writer.writerow([
                label,
                value,
                sum(1 for l in subset if l.get("last_emailed")),
                sum(1 for l in subset if l.get("reply_outcome")),
                sum(1 for l in subset if l.get("reply_outcome") in ("Replied positive", "Booked")),
                sum(1 for l in subset if l.get("reply_outcome") == "Asked to stop"),
            ])
    writer.writerow([])
    writer.writerow(["Recommended focus", "Reason"])
    for lead in sorted(cleanup, key=lambda l: personalization_score(l))[:25]:
        reasons = draft_quality_warnings(lead, {"min_personalization_score": "60", "require_contact_name": "1", "require_industry": "1"})
        writer.writerow([lead.get("company") or "", "; ".join(reasons) or "Improve personalization"])
    return output.getvalue().encode("utf-8")


def playbook_payload(conn):
    settings = get_settings(conn)
    return {
        "version": 1,
        "exported_at": now_iso(),
        "email_plan_count": settings.get("email_plan_count") or str(DEFAULT_EMAIL_PLAN_COUNT),
        "email_templates": json.loads(normalize_templates(settings.get("email_templates", "[]"), int(settings.get("email_plan_count") or DEFAULT_EMAIL_PLAN_COUNT))),
        "email_signature": settings.get("email_signature") or "",
        "daily_target": settings.get("daily_target") or "20",
        "min_personalization_score": settings.get("min_personalization_score") or "60",
        "require_contact_name": settings.get("require_contact_name") or "1",
        "require_industry": settings.get("require_industry") or "1",
        "require_custom_first_line": settings.get("require_custom_first_line") or "0",
        "require_software_stack": settings.get("require_software_stack") or "0",
        "sequence_presets": json.loads(normalize_sequence_presets(settings.get("sequence_presets", "[]"))),
    }


def gemini_prompt_text():
    path = os.path.join(os.getcwd(), "GEMINI_IMPORT_PROMPT.md")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return "Create a CSV for import into the Odoo Gmail Draft Assistant."


def gemini_deep_research_prompt_text():
    path = os.path.join(os.getcwd(), "GEMINI_DEEP_RESEARCH_PROMPT.md")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return "Use Gemini Deep Research to source each account, then return READABLE_REVIEW and IMPORT_CSV."


def csv_template(kind="gemini"):
    base = [
        "company", "contact", "title", "email", "email_generic", "phone", "website", "city", "state",
        "industry", "sub_industry", "software_used", "software_renewal_date", "source", "stage", "notes",
    ]
    gemini = [
        "odoo_evaluation_version", "odoo_evaluation_date", "odoo_evaluation_year",
        "previous_demo_notes", "previous_blocker", "apps_evaluated", "lost_category", "lost_reason",
        "company_summary", "why_now", "workflow_hypothesis", "research_evidence", "research_source_url",
        "confidence", "do_not_claim", "reason_to_believe", "role_lens", "claim_safety", "customer_language",
        "reply_type_goal", "pain_points", "industry_holes", "value_angle", "proof_points", "sequence_angle",
        "custom_first_line", "reply_goal", "cta_style", "reply_cta", "next_action", "next_action_date",
    ]
    output = io.StringIO()
    fields = base + (gemini if kind == "gemini" else ["next_action", "next_action_date"])
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    return output.getvalue().encode("utf-8")


def preview_import_csv(conn, content):
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    counts = {
        "rows": 0, "ready": 0, "missing_company": 0, "missing_direct_email": 0,
        "duplicates": 0, "suppressed": 0, "generic_only": 0, "weak_research": 0, "malformed": 0,
    }
    samples = []
    for row in reader:
        counts["rows"] += 1
        company = (row.get("company") or row.get("Company") or row.get("Customer") or row.get("Opportunity Name") or "").strip()
        email_addr = (row.get("email") or row.get("Email") or row.get("Direct Email") or "").strip()
        generic = (row.get("email_generic") or row.get("Generic Email") or "").strip()
        issues = []
        if None in row:
            counts["malformed"] += 1
            issues.append("malformed CSV row")
        research_issues = gemini_research_warnings(row)
        if not company:
            counts["missing_company"] += 1
            issues.append("missing company")
        if not email_addr:
            counts["missing_direct_email"] += 1
            issues.append("missing direct email")
        elif is_generic_email(email_addr):
            counts["generic_only"] += 1
            issues.append("direct email looks generic")
        if company and conn.execute("SELECT id FROM leads WHERE lower(company)=lower(?)", (company,)).fetchone():
            counts["duplicates"] += 1
            issues.append("duplicate company")
        if company and suppression_match(conn, company, email_addr or generic):
            counts["suppressed"] += 1
            issues.append("suppressed")
        if research_issues:
            counts["weak_research"] += 1
            issues.extend(research_issues[:2])
        if not issues:
            counts["ready"] += 1
        if issues and len(samples) < 12:
            samples.append({"company": company or "(blank)", "email": email_addr or generic, "issues": issues})
    return {"counts": counts, "samples": samples, "headers": reader.fieldnames or []}


def backup_now():
    if not os.path.exists(DB_PATH):
        return ""
    os.makedirs("backups", exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"crm-manual-{stamp}.db"
    shutil.copy2(DB_PATH, os.path.join("backups", name))
    return name


def list_backups():
    if not os.path.isdir("backups"):
        return []
    rows = []
    for name in sorted(os.listdir("backups"), reverse=True):
        if not name.endswith(".db"):
            continue
        path = os.path.join("backups", name)
        rows.append({"name": name, "size": os.path.getsize(path), "modified": datetime.fromtimestamp(os.path.getmtime(path)).isoformat(sep=" ", timespec="seconds")})
    return rows


def restore_backup(name):
    safe = os.path.basename(name or "")
    path = os.path.join("backups", safe)
    if not safe.endswith(".db") or not os.path.exists(path):
        raise ValueError("Backup not found")
    backup_now()
    shutil.copy2(path, DB_PATH)
    return safe


def friendly_error(exc):
    text = str(exc)
    lower = text.lower()
    if "authentication failed" in lower or "invalid credentials" in lower or "login failed" in lower:
        return "Gmail rejected the login. Use a Google App Password, not your regular Gmail password, and make sure 2-Step Verification is on."
    if "gmail address and app password" in lower:
        return text
    if "name or service not known" in lower or "network" in lower or "timed out" in lower:
        return "Could not reach Gmail. Check internet connection and try again."
    if "not eligible for drafting" in lower:
        return "This lead is not ready for drafting. Check direct email, suppression, stage, email risk, and today's draft status."
    return text or "Something went wrong."


def health_report(conn):
    settings = get_settings(conn)
    lead_count = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    ready_count = len(draft_preview_rows(conn, int(settings.get("daily_target") or 20), settings))
    suppressed_count = conn.execute("SELECT COUNT(*) c FROM suppressed WHERE restored=0").fetchone()["c"]
    backups = list_backups()
    return {
        "database": os.path.abspath(DB_PATH),
        "database_exists": os.path.exists(DB_PATH),
        "backups": len(backups),
        "last_backup": backups[0]["modified"] if backups else "",
        "gmail_email": settings.get("gmail_email") or "",
        "gmail_configured": bool(settings.get("gmail_email") and settings.get("gmail_app_password")),
        "leads": lead_count,
        "ready_preview_count": sum(1 for row in draft_preview_rows(conn, int(settings.get("daily_target") or 20), settings) if row["ready"]),
        "suppressed": suppressed_count,
        "gemini_prompt": os.path.exists("GEMINI_IMPORT_PROMPT.md"),
    }


def append_gmail_test_draft(settings):
    sender = settings.get("gmail_email", "").strip()
    password = settings.get("gmail_app_password", "").replace(" ", "")
    if not sender or not password:
        raise ValueError("Gmail address and App Password are required. Use a Google App Password, not your regular Gmail password.")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = sender
    msg["Subject"] = "CRM Gmail draft test"
    msg.set_content("This is a test draft from the Odoo Gmail Draft Assistant. You can delete it.", subtype="plain", charset="utf-8")
    msg.add_alternative("<p>This is a test draft from the Odoo Gmail Draft Assistant. You can delete it.</p>", subtype="html", charset="utf-8")
    with imaplib.IMAP4_SSL("imap.gmail.com", 993) as imap:
        imap.login(sender, password)
        status, _ = imap.append('"[Gmail]/Drafts"', r"(\Draft)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        if status != "OK":
            raise RuntimeError("Gmail rejected the draft append. Check the Gmail account and App Password.")
        imap.logout()


def mock_lead_rows():
    return [
        {"company": "Atlas Robotics Group", "contact": "Maya Chen", "title": "VP Operations", "email": "maya.chen@example.com", "phone": "555-2101", "city": "Austin", "state": "TX", "industry": "Robotics & Automation", "sub_industry": "warehouse robotics", "software_used": "NetSuite, Salesforce, Arena PLM, Jira", "software_renewal_date": "2026-10-15", "source": "Mock", "notes": "Evaluating robot fleet deployment but sales cycle stalls when finance asks for payback proof."},
        {"company": "PierPoint Foundation Repair", "contact": "Caleb Morris", "title": "Owner", "email": "caleb.morris@example.com", "phone": "555-2102", "city": "Tampa", "state": "FL", "industry": "Foundation & Structural Repair", "sub_industry": "foundation repair", "software_used": "ServiceTitan, JobNimbus, CompanyCam, QuickBooks", "software_renewal_date": "2026-09-01", "source": "Mock", "notes": "High inspection volume after storms; estimates are not followed up consistently."},
        {"company": "Northline HVAC Services", "contact": "Jenna Patel", "title": "Sales Manager", "email": "jenna.patel@example.com", "phone": "555-2103", "city": "Phoenix", "state": "AZ", "industry": "Field Service", "sub_industry": "HVAC", "software_used": "ServiceTitan, CallRail, Podium, QuickBooks", "software_renewal_date": "2026-11-30", "source": "Mock", "notes": "Replacement quotes go cold after first visit during seasonal spikes."},
        {"company": "ForgeWorks Manufacturing", "contact": "Drew Lawson", "title": "Plant Director", "email": "drew.lawson@example.com", "phone": "555-2104", "city": "Cleveland", "state": "OH", "industry": "Industrial Manufacturing", "sub_industry": "metal fabrication", "software_used": "JobBOSS, E2 Shop System, Fishbowl, QuickBooks", "software_renewal_date": "2027-01-15", "source": "Mock", "notes": "RFQs, inventory availability, and production handoff are tracked in separate spreadsheets."},
        {"company": "BrightCart Retail", "contact": "Sofia Nguyen", "title": "Director of Stores", "email": "sofia.nguyen@example.com", "phone": "555-2105", "city": "Denver", "state": "CO", "industry": "Retail", "sub_industry": "multi-location retail", "software_used": "Shopify POS, Lightspeed, Cin7, Klaviyo", "source": "Mock", "notes": "POS and inventory are not synced cleanly across locations and eCommerce."},
        {"company": "Summit Supply Wholesale", "contact": "Marcus Reed", "title": "GM", "email": "marcus.reed@example.com", "phone": "555-2106", "city": "Charlotte", "state": "NC", "industry": "Distribution & Wholesale", "sub_industry": "industrial distributors", "software_used": "NetSuite, Fishbowl, ShipStation, QuickBooks Enterprise", "software_renewal_date": "2026-12-31", "source": "Mock", "notes": "Inside sales cannot see stock issues before promising delivery dates."},
        {"company": "Harbor SaaS Labs", "contact": "Priya Shah", "title": "Revenue Ops Lead", "email": "priya.shah@example.com", "phone": "555-2107", "city": "San Diego", "state": "CA", "industry": "SaaS & Technology", "sub_industry": "B2B SaaS", "software_used": "HubSpot, Stripe, Chargebee, Zendesk", "source": "Mock", "notes": "Subscription renewals, support tickets, and expansion opportunities are disconnected."},
        {"company": "Copper Ridge Builders", "contact": "Evan Brooks", "title": "Estimator", "email": "evan.brooks@example.com", "phone": "555-2108", "city": "Nashville", "state": "TN", "industry": "Construction", "sub_industry": "commercial builders", "software_used": "Procore, Sage 100 Contractor, QuickBooks, Buildertrend", "source": "Mock", "notes": "Proposal follow-up depends on individual estimator memory."},
        {"company": "ClearPath Dental Group", "contact": "Amelia Torres", "title": "Practice Administrator", "email": "amelia.torres@example.com", "phone": "555-2109", "city": "Orlando", "state": "FL", "industry": "Healthcare", "sub_industry": "dental practices", "software_used": "Dentrix, Open Dental, Solutionreach, QuickBooks", "source": "Mock", "notes": "New patient inquiries and treatment plan follow-up are not visible in one pipeline."},
        {"company": "GreenRow Solar", "contact": "Noah Bennett", "title": "Sales Director", "email": "noah.bennett@example.com", "phone": "555-2110", "city": "Raleigh", "state": "NC", "industry": "Energy & Utilities", "sub_industry": "solar installers", "software_used": "Aurora Solar, OpenSolar, Salesforce, QuickBooks", "source": "Mock", "notes": "Site surveys, proposals, financing docs, and installation scheduling are disconnected."},
        {"company": "BlueFork Foods", "contact": "Lena Garcia", "title": "Operations Manager", "email": "lena.garcia@example.com", "phone": "555-2111", "city": "Chicago", "state": "IL", "industry": "Food & Beverage", "sub_industry": "food manufacturing", "software_used": "BatchMaster, NetSuite, Fishbowl, QuickBooks", "source": "Mock", "notes": "Ingredient purchasing, batch production, and wholesale orders are hard to reconcile."},
        {"company": "MetroLine Logistics", "contact": "Owen Kim", "title": "Fleet Manager", "email": "owen.kim@example.com", "phone": "555-2112", "city": "Atlanta", "state": "GA", "industry": "Logistics & Transportation", "sub_industry": "fleet maintenance", "software_used": "Fleetio, Samsara, Motive, QuickBooks", "source": "Mock", "notes": "Maintenance work orders and customer delivery commitments are managed in separate tools."},
    ]


def seed_mock_leads(conn):
    counts = {"created": 0, "duplicate": 0, "suppressed": 0, "skipped": 0}
    for idx, row in enumerate(mock_lead_rows()):
        row = dict(row)
        row["priority"] = 3 if idx < 5 else 2
        row["email_plan_count"] = 6
        lead_id, status = insert_lead(conn, row)
        counts[status] = counts.get(status, 0) + 1
        if lead_id and status in ("created", "duplicate"):
            if status == "duplicate":
                existing = lead_by_id(conn, lead_id)
                update_lead(conn, lead_id, dict(existing, **row))
            lead = lead_by_id(conn, lead_id)
            lead_for_generation = dict(lead, pain_points="", industry_holes="", value_angle="", proof_points="")
            strategy = lead_strategy(lead_for_generation)
            conn.execute(
                """
                UPDATE leads SET pain_points=?, industry_holes=?, value_angle=?, proof_points=?,
                    lead_email_templates=?, updated=?
                WHERE id=?
                """,
                (
                    strategy["pain"], strategy["holes"], strategy["angle"], strategy["proof"],
                    json.dumps(generated_lead_templates(lead_for_generation)), now_iso(), lead_id,
                ),
            )
            add_timeline(conn, lead_id, "Mock data", "Mock lead refreshed with tailored Odoo email sequence")
    conn.commit()
    return counts


def export_csv(conn, query):
    where, args = filter_where(query)
    rows = conn.execute(f"SELECT * FROM leads {where} ORDER BY priority DESC, updated DESC", args).fetchall()
    output = io.StringIO()
    fields = [
        "company", "contact", "title", "email", "email_generic", "phone", "website", "city",
        "state", "industry", "sub_industry", "software_used", "software_renewal_date", "source", "stage", "priority", "notes",
        "pain_points", "industry_holes", "value_angle", "proof_points", "next_action",
        "next_action_date", "email_stage", "email_plan_count", "email_last_stage",
        "company_summary", "why_now", "workflow_hypothesis", "research_evidence",
        "research_source_url", "confidence", "do_not_claim", "reason_to_believe",
        "role_lens", "claim_safety", "customer_language", "reply_type_goal",
        "sequence_angle", "custom_first_line", "reply_goal", "cta_style", "reply_cta",
        "reply_outcome", "reply_outcome_ts", "last_emailed", "emailed_count", "created", "updated",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row[field] for field in fields})
    return output.getvalue().encode("utf-8")


def filter_where(query):
    clauses = []
    args = []
    q = query.get("q", [""])[0].strip()
    if q:
        clauses.append("(company LIKE ? OR contact LIKE ? OR email LIKE ? OR email_generic LIKE ? OR notes LIKE ? OR lost_reason LIKE ? OR lost_category LIKE ?)")
        args.extend([f"%{q}%"] * 7)
    for field in ["stage", "state", "city", "industry", "source", "lost_category"]:
        value = query.get(field, [""])[0].strip()
        if value:
            clauses.append(f"{field}=?")
            args.append(value)
    priority = query.get("priority", [""])[0].strip()
    if priority:
        clauses.append("priority=?")
        args.append(int(priority))
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    return where, args


class CRMHandler(BaseHTTPRequestHandler):
    server_version = "LocalCRM/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body, content_type, filename=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def not_found(self):
        self.send_json({"error": "Not found"}, 404)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        conn = connect()
        try:
            if path == "/":
                self.send_bytes(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/bootstrap":
                self.send_json(self.bootstrap(conn))
            elif path == "/api/leads":
                self.send_json({"leads": self.list_leads(conn, query)})
            elif path.startswith("/api/leads/"):
                lead_id = int(path.split("/")[-1])
                lead = lead_by_id(conn, lead_id)
                if not lead:
                    return self.not_found()
                self.send_json({"lead": lead, "timeline": lead_timeline(conn, lead_id)})
            elif path == "/api/activities":
                self.send_json({"activities": self.activities(conn, query)})
            elif path == "/api/suppressed":
                rows = conn.execute(
                    "SELECT * FROM suppressed WHERE restored=0 ORDER BY ts DESC"
                ).fetchall()
                self.send_json({"suppressed": [dict(row) for row in rows]})
            elif path == "/api/export":
                self.send_bytes(export_csv(conn, query), "text/csv; charset=utf-8", "leads.csv")
            elif path == "/api/odoo-updates":
                self.send_bytes(export_odoo_updates_csv(conn, query), "text/csv; charset=utf-8", "odoo-draft-updates.csv")
            elif path == "/api/weekly-coaching":
                self.send_bytes(export_weekly_coaching_csv(conn), "text/csv; charset=utf-8", "weekly-coaching.csv")
            elif path == "/api/playbook":
                body = json.dumps(playbook_payload(conn), indent=2).encode("utf-8")
                self.send_bytes(body, "application/json; charset=utf-8", "odoo-draft-playbook.json")
            elif path == "/api/gemini-prompt":
                self.send_bytes(gemini_prompt_text().encode("utf-8"), "text/markdown; charset=utf-8", "GEMINI_IMPORT_PROMPT.md")
            elif path == "/api/gemini-deep-research-prompt":
                self.send_bytes(gemini_deep_research_prompt_text().encode("utf-8"), "text/markdown; charset=utf-8", "GEMINI_DEEP_RESEARCH_PROMPT.md")
            elif path == "/api/template/odoo":
                self.send_bytes(csv_template("odoo"), "text/csv; charset=utf-8", "odoo-import-template.csv")
            elif path == "/api/template/gemini":
                self.send_bytes(csv_template("gemini"), "text/csv; charset=utf-8", "gemini-enriched-import-template.csv")
            elif path == "/api/health":
                self.send_json({"health": health_report(conn), "backups": list_backups()})
            elif path == "/api/draft-preview":
                settings = get_settings(conn)
                limit = int(query.get("limit", [settings.get("daily_target") or 20])[0] or 20)
                self.send_json({"leads": draft_preview_rows(conn, limit, settings)})
            else:
                self.not_found()
        finally:
            conn.close()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        conn = connect()
        try:
            if path == "/api/leads":
                lead_id, status = insert_lead(conn, self.read_json())
                self.send_json({"id": lead_id, "status": status})
            elif re.match(r"^/api/leads/\d+$", path):
                lead_id = int(path.split("/")[-1])
                lead = update_lead(conn, lead_id, self.read_json())
                if not lead:
                    return self.not_found()
                self.send_json({"lead": lead, "timeline": lead_timeline(conn, lead_id)})
            elif re.match(r"^/api/leads/\d+/delete$", path):
                lead_id = int(path.split("/")[-2])
                lead = lead_by_id(conn, lead_id)
                if not lead:
                    return self.not_found()
                for table in ("notes", "activities", "timeline"):
                    conn.execute(f"DELETE FROM {table} WHERE lead_id=?", (lead_id,))
                conn.execute("DELETE FROM leads WHERE id=?", (lead_id,))
                conn.commit()
                self.send_json({"ok": True})
            elif path.endswith("/note"):
                lead_id = int(path.split("/")[-2])
                data = self.read_json()
                text = str(data.get("text", "")).strip()
                if text:
                    conn.execute("INSERT INTO notes(lead_id, ts, text) VALUES(?, ?, ?)", (lead_id, now_iso(), text))
                    conn.commit()
                self.send_json({"timeline": lead_timeline(conn, lead_id)})
            elif path.endswith("/activity"):
                lead_id = int(path.split("/")[-2])
                data = self.read_json()
                typ = data.get("type") if data.get("type") in ACTIVITY_TYPES else "To-do"
                due = str(data.get("due") or today_iso())
                note = str(data.get("note") or "")
                conn.execute(
                    "INSERT INTO activities(lead_id, type, due, note, created) VALUES(?, ?, ?, ?, ?)",
                    (lead_id, typ, due, note, now_iso()),
                )
                conn.execute(
                    "UPDATE leads SET next_action=?, next_action_date=?, updated=? WHERE id=?",
                    (typ + (f": {note}" if note else ""), due, now_iso(), lead_id),
                )
                conn.commit()
                add_timeline(conn, lead_id, "Activity", f"{typ} scheduled for {due}")
                self.send_json({"lead": lead_by_id(conn, lead_id), "timeline": lead_timeline(conn, lead_id)})
            elif path.endswith("/lost"):
                lead_id = int(path.split("/")[-2])
                data = self.read_json()
                reason = str(data.get("reason") or "").strip()
                if not reason:
                    return self.send_json({"error": "Lost reason is required"}, 400)
                category = str(data.get("category") or "Other").strip()
                if category not in LOST_CATEGORIES:
                    category = "Other"
                lead = lead_by_id(conn, lead_id)
                update_lead(conn, lead_id, dict(
                    lead,
                    stage="Lost",
                    next_action="",
                    next_action_date="",
                    lost_category=category,
                    lost_reason=reason,
                    lost_ts=now_iso(),
                ))
                add_timeline(conn, lead_id, "Lost", f"{category}: {reason}")
                self.send_json({"lead": lead_by_id(conn, lead_id), "timeline": lead_timeline(conn, lead_id)})
            elif path.endswith("/restore"):
                lead_id = int(path.split("/")[-2])
                lead = lead_by_id(conn, lead_id)
                if not lead:
                    return self.not_found()
                update_lead(conn, lead_id, dict(lead, stage="New", lost_category="", lost_reason="", lost_ts=""))
                add_timeline(conn, lead_id, "Restored", "Moved from Lost back to active CRM")
                self.send_json({"lead": lead_by_id(conn, lead_id), "timeline": lead_timeline(conn, lead_id)})
            elif path.endswith("/reopen"):
                lead_id = int(path.split("/")[-2])
                lead = lead_by_id(conn, lead_id)
                if not lead:
                    return self.not_found()
                due = add_business_days(today_iso(), 1)
                update_lead(conn, lead_id, dict(
                    lead,
                    stage="Follow-up",
                    next_action="Follow-up call",
                    next_action_date=due,
                    reply_outcome="Reopened",
                    reply_outcome_ts=now_iso(),
                    lost_category="",
                    lost_reason="",
                    lost_ts="",
                ))
                conn.execute(
                    "INSERT INTO activities(lead_id, type, due, note, created) VALUES(?, 'Call', ?, 'Follow up after lost-lead recovery reply', ?)",
                    (lead_id, due, now_iso()),
                )
                conn.commit()
                add_timeline(conn, lead_id, "Recovered", "Restored from Lost after reply; follow-up call scheduled")
                self.send_json({"lead": lead_by_id(conn, lead_id), "timeline": lead_timeline(conn, lead_id)})
            elif path.endswith("/suppress"):
                lead_id = int(path.split("/")[-2])
                data = self.read_json()
                self.suppress_lead(conn, lead_id, str(data.get("reason") or "Opt-out"))
                self.send_json({"ok": True})
            elif path.endswith("/engaged"):
                lead_id = int(path.split("/")[-2])
                due = add_business_days(today_iso(), 1)
                lead = lead_by_id(conn, lead_id)
                update_lead(conn, lead_id, dict(lead, stage="Follow-up", next_action="Follow-up call", next_action_date=due))
                conn.execute(
                    "INSERT INTO activities(lead_id, type, due, note, created) VALUES(?, 'Call', ?, 'Follow up after email engagement', ?)",
                    (lead_id, due, now_iso()),
                )
                conn.commit()
                add_timeline(conn, lead_id, "Engaged", "Clicked/opened email; follow-up call scheduled")
                self.send_json({"lead": lead_by_id(conn, lead_id), "timeline": lead_timeline(conn, lead_id)})
            elif path.endswith("/draft"):
                lead_id = int(path.split("/")[-2])
                self.draft_one(conn, lead_id)
            elif path.endswith("/lost-recovery-draft"):
                lead_id = int(path.split("/")[-2])
                self.draft_lost_recovery(conn, lead_id)
            elif path.endswith("/generate-emails"):
                lead_id = int(path.split("/")[-2])
                self.generate_lead_emails(conn, lead_id)
            elif re.match(r"^/api/activities/\d+/done$", path):
                activity_id = int(path.split("/")[-2])
                self.finish_activity(conn, activity_id)
            elif path == "/api/settings":
                set_settings(conn, self.read_json())
                self.send_json({"settings": self.safe_settings(conn)})
            elif path == "/api/test-gmail":
                append_gmail_test_draft(get_settings(conn))
                self.send_json({"ok": True, "message": "Test draft created in Gmail Drafts."})
            elif path == "/api/backup-now":
                self.send_json({"ok": True, "backup": backup_now(), "backups": list_backups()})
            elif path == "/api/restore-backup":
                data = self.read_json()
                self.send_json({"ok": True, "restored": restore_backup(data.get("name"))})
            elif path == "/api/playbook":
                data = self.read_json()
                payload = {
                    "email_plan_count": data.get("email_plan_count"),
                    "email_templates": json.dumps(data.get("email_templates") or []),
                    "email_signature": data.get("email_signature"),
                    "daily_target": data.get("daily_target"),
                    "min_personalization_score": data.get("min_personalization_score"),
                    "require_contact_name": data.get("require_contact_name"),
                    "require_industry": data.get("require_industry"),
                    "require_custom_first_line": data.get("require_custom_first_line"),
                    "require_software_stack": data.get("require_software_stack"),
                    "sequence_presets": json.dumps(data.get("sequence_presets") or []),
                }
                set_settings(conn, {k: v for k, v in payload.items() if v is not None})
                self.send_json({"settings": self.safe_settings(conn)})
            elif path == "/api/draft-batch":
                data = self.read_json()
                self.draft_batch(conn, int(data.get("limit") or 20))
            elif path == "/api/import":
                length = int(self.headers.get("Content-Length", "0"))
                counts = import_csv(conn, self.rfile.read(length))
                self.send_json({"counts": counts})
            elif path == "/api/import-preview":
                length = int(self.headers.get("Content-Length", "0"))
                self.send_json(preview_import_csv(conn, self.rfile.read(length)))
            elif path == "/api/mock-leads":
                self.send_json({"counts": seed_mock_leads(conn), "leads": self.list_leads(conn, {})})
            elif re.match(r"^/api/suppressed/\d+/restore$", path):
                sid = int(path.split("/")[-2])
                restored_id = self.restore_suppressed(conn, sid)
                self.send_json({"ok": True, "lead_id": restored_id})
            else:
                self.not_found()
        except Exception as exc:
            self.send_json({"error": friendly_error(exc)}, 500)
        finally:
            conn.close()

    def bootstrap(self, conn):
        today_count = conn.execute(
            "SELECT COUNT(*) c FROM leads WHERE date(last_emailed)=date('now', 'localtime')"
        ).fetchone()["c"]
        return {
            "stages": STAGES,
            "lost_categories": LOST_CATEGORIES,
            "odoo_releases": ODOO_RELEASE_INTEL,
            "activity_types": ACTIVITY_TYPES,
            "industry_library": industry_library(),
            "settings": self.safe_settings(conn),
            "daily": {"drafted": today_count, "target": int(get_settings(conn).get("daily_target") or 20)},
            "leads": self.list_leads(conn, {}),
        }

    def safe_settings(self, conn):
        settings = get_settings(conn)
        settings["gmail_app_password"] = "********" if settings.get("gmail_app_password") else ""
        count = max(1, min(30, int(settings.get("email_plan_count") or DEFAULT_EMAIL_PLAN_COUNT)))
        settings["email_plan_count"] = str(count)
        settings["email_templates"] = normalize_templates(settings.get("email_templates", "[]"), count)
        settings["sequence_presets"] = normalize_sequence_presets(settings.get("sequence_presets", "[]"))
        return settings

    def list_leads(self, conn, query):
        where, args = filter_where(query)
        sort = query.get("sort", ["priority"])[0] if query else "priority"
        order = {
            "priority": "priority DESC, updated DESC",
            "updated": "updated DESC",
            "company": "company COLLATE NOCASE ASC",
            "next_action": "next_action_date IS NULL, next_action_date ASC",
            "lost_category": "lost_category COLLATE NOCASE ASC, lost_ts DESC",
            "lost_ts": "lost_ts DESC, updated DESC",
        }.get(sort, "priority DESC, updated DESC")
        rows = conn.execute(f"SELECT * FROM leads {where} ORDER BY {order}", args).fetchall()
        return [dict(row) for row in rows]

    def activities(self, conn, query):
        view = query.get("view", ["agenda"])[0] if query else "agenda"
        clauses = ["a.done=0", "l.stage IN ('New','Contacted','Follow-up','Qualified','Proposal')"]
        if view == "today":
            clauses.append("date(a.due)<=date('now','localtime')")
        if view == "call":
            clauses.append("l.phone IS NOT NULL AND l.phone != ''")
            clauses.append("a.type='Call'")
        sql = f"""
            SELECT a.*, l.company, l.contact, l.phone, l.email, l.priority
            FROM activities a JOIN leads l ON l.id=a.lead_id
            WHERE {' AND '.join(clauses)}
            ORDER BY a.due ASC, l.priority DESC
        """
        return [dict(row) for row in conn.execute(sql)]

    def suppress_lead(self, conn, lead_id, reason):
        lead = lead_by_id(conn, lead_id)
        if not lead:
            raise ValueError("Lead not found")
        conn.execute(
            "INSERT INTO suppressed(company_key, company, email, reason, ts) VALUES(?, ?, ?, ?, ?)",
            (company_key(lead["company"]), lead["company"], lead["email"], reason, now_iso()),
        )
        conn.execute("DELETE FROM activities WHERE lead_id=?", (lead_id,))
        conn.execute("DELETE FROM leads WHERE id=?", (lead_id,))
        conn.commit()

    def restore_suppressed(self, conn, suppressed_id):
        row = conn.execute("SELECT * FROM suppressed WHERE id=?", (suppressed_id,)).fetchone()
        if not row:
            raise ValueError("Suppression record not found")
        conn.execute("UPDATE suppressed SET restored=1, restored_ts=? WHERE id=?", (now_iso(), suppressed_id))
        existing = conn.execute("SELECT id FROM leads WHERE lower(company)=lower(?)", (row["company"],)).fetchone()
        restored_id = existing["id"] if existing else None
        if not restored_id:
            restored_id, _ = insert_lead(conn, {
                "company": row["company"] or "Restored lead",
                "email": row["email"] or "",
                "source": "Restored from suppression",
            })
            if restored_id:
                add_timeline(conn, restored_id, "Restored", "Restored from suppression list")
        conn.commit()
        return restored_id

    def finish_activity(self, conn, activity_id):
        activity = conn.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
        if not activity:
            return self.not_found()
        due = add_business_days(today_iso(), 3)
        conn.execute("UPDATE activities SET done=1, done_ts=? WHERE id=?", (now_iso(), activity_id))
        conn.execute(
            "INSERT INTO activities(lead_id, type, due, note, created) VALUES(?, ?, ?, ?, ?)",
            (activity["lead_id"], activity["type"], due, "Auto-scheduled next step", now_iso()),
        )
        conn.execute(
            "UPDATE leads SET next_action=?, next_action_date=?, updated=? WHERE id=?",
            (activity["type"], due, now_iso(), activity["lead_id"]),
        )
        conn.commit()
        add_timeline(conn, activity["lead_id"], "Activity done", f"{activity['type']} completed; next step due {due}")
        self.send_json({"ok": True})

    def draft_one(self, conn, lead_id):
        lead = auto_tailor_lead(conn, lead_id)
        if not lead:
            return self.not_found()
        if suppression_match(conn, lead["company"], lead["email"]):
            return self.send_json({"error": "Lead is suppressed"}, 400)
        if not eligible_for_draft(lead):
            return self.send_json({"error": "Lead is not eligible for drafting"}, 400)
        settings = get_settings(conn)
        append_gmail_draft(settings, lead)
        mark_drafted(conn, lead)
        self.send_json({"ok": True, "lead": lead_by_id(conn, lead_id), "timeline": lead_timeline(conn, lead_id)})

    def draft_lost_recovery(self, conn, lead_id):
        lead = lead_by_id(conn, lead_id)
        if not lead:
            return self.not_found()
        if lead["stage"] != "Lost":
            return self.send_json({"error": "Recovery drafts are only for lost leads"}, 400)
        if not lead.get("email"):
            return self.send_json({"error": "Lead needs a direct email for recovery drafting"}, 400)
        if lead.get("email_risky"):
            return self.send_json({"error": lead.get("email_risk_reason") or "Email looks risky"}, 400)
        if suppression_match(conn, lead["company"], lead["email"]):
            return self.send_json({"error": "Lead is suppressed"}, 400)
        if str(lead.get("recovery_last_drafted") or "")[:10] == today_iso():
            return self.send_json({"error": "A recovery draft was already created for this lead today."}, 400)
        recovery_lead = dict(lead)
        recovery_lead["lead_email_templates"] = json.dumps(lost_recovery_templates(lead))
        recovery_lead["email_stage"] = 1
        recovery_lead["email_plan_count"] = 3
        recovery_lead["reply_cta"] = recovery_lead.get("reply_cta") or "Worth reopening this, or should I keep it closed?"
        settings = get_settings(conn)
        append_gmail_draft(settings, recovery_lead)
        conn.execute(
            "UPDATE leads SET last_emailed=?, recovery_last_drafted=?, emailed_count=emailed_count+1, updated=? WHERE id=?",
            (today_iso(), now_iso(), now_iso(), lead_id),
        )
        conn.commit()
        matched = inferred_eval_version(lead)
        label = "pre-Odoo 17" if matched == "16" else f"Odoo {matched}"
        add_timeline(conn, lead_id, "Lost recovery draft", "Recovery email drafted to Gmail while lead remains in Lost")
        add_timeline(conn, lead_id, "Odoo version match", f"Matched prior evaluation to {label}. Source note: {odoo_source_note()}")
        self.send_json({"ok": True, "lead": lead_by_id(conn, lead_id), "timeline": lead_timeline(conn, lead_id)})

    def generate_lead_emails(self, conn, lead_id):
        lead = lead_by_id(conn, lead_id)
        if not lead:
            return self.not_found()
        templates = generated_lead_templates(lead)
        strategy = lead_strategy(lead)
        conn.execute(
            """
            UPDATE leads SET lead_email_templates=?, pain_points=COALESCE(NULLIF(pain_points, ''), ?),
                industry_holes=COALESCE(NULLIF(industry_holes, ''), ?),
                value_angle=COALESCE(NULLIF(value_angle, ''), ?),
                proof_points=COALESCE(NULLIF(proof_points, ''), ?), updated=?
            WHERE id=?
            """,
            (
                json.dumps(templates), strategy["pain"], strategy["holes"], strategy["angle"],
                strategy["proof"], now_iso(), lead_id,
            ),
        )
        conn.commit()
        add_timeline(conn, lead_id, "Email strategy", "Tailored email sequence generated from opportunity industry context")
        self.send_json({"lead": lead_by_id(conn, lead_id), "timeline": lead_timeline(conn, lead_id)})

    def draft_batch(self, conn, limit):
        settings = get_settings(conn)
        rows = conn.execute(
            """
            SELECT * FROM leads
            WHERE stage IN ('New','Contacted','Follow-up','Qualified','Proposal')
              AND email IS NOT NULL AND email != ''
              AND email_risky=0
              AND email_stage <= email_plan_count
              AND (last_emailed IS NULL OR last_emailed='' OR date(last_emailed) < date('now','localtime'))
            ORDER BY priority DESC, updated ASC
            LIMIT ?
            """,
            (max(1, min(500, limit)),),
        ).fetchall()
        drafted, skipped, errors = 0, 0, []
        seen = set()
        for row in rows:
            lead = auto_tailor_lead(conn, row["id"]) or dict(row)
            key = lead["email"].lower()
            if key in seen or suppression_match(conn, lead["company"], lead["email"]) or not eligible_for_draft(lead) or not passes_draft_quality(lead, settings):
                skipped += 1
                continue
            seen.add(key)
            try:
                append_gmail_draft(settings, lead)
                mark_drafted(conn, lead)
                drafted += 1
            except Exception as exc:
                errors.append(f"{lead['company']}: {exc}")
                break
        self.send_json({"drafted": drafted, "skipped": skipped, "errors": errors})


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Odoo Gmail Draft Assistant</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f6f7;
      --panel: #ffffff;
      --panel-2: #f8f9fa;
      --muted: #6b7280;
      --text: #1f2933;
      --line: #d8dadd;
      --odoo: #714b67;
      --odoo-dark: #5f3f58;
      --teal: #00a09d;
      --teal-dark: #008784;
      --gold: #f4b400;
      --green: #28a745;
      --red: #d44c59;
      --blue: #4c8bf5;
      --orange: #f59f00;
      --shadow: 0 12px 28px rgba(31, 41, 51, .14);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 14px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      overflow: hidden;
    }
    button, input, select, textarea {
      font: inherit;
      color: inherit;
    }
    button {
      border: 1px solid var(--line);
      background: #ffffff;
      border-radius: 7px;
      padding: 8px 10px;
      cursor: pointer;
    }
    button:hover { background: #f0f1f2; }
    button.primary { background: var(--teal); color: #fff; border-color: var(--teal); font-weight: 700; }
    button.primary:hover { background: var(--teal-dark); }
    button.danger { border-color: #f1bbc1; color: #9f2633; background: #fff2f3; }
    button.icon { width: 34px; height: 34px; padding: 0; display: inline-grid; place-items: center; }
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line);
      background: #fff;
      border-radius: 7px;
      padding: 8px 9px;
      outline: none;
    }
    textarea { min-height: 78px; resize: vertical; }
    .app {
      height: 100vh;
      display: grid;
      grid-template-rows: auto auto auto auto 1fr;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 14px 18px;
      border-bottom: 1px solid var(--odoo-dark);
      background: var(--odoo);
      color: #fff;
    }
    h1 { margin: 0; font-size: 18px; letter-spacing: 0; }
    .top-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
    .top-actions input { flex: 0 0 86px; }
    .more-menu { position: relative; }
    .more-menu summary {
      list-style: none;
      cursor: pointer;
      color: var(--text);
      background: #fff;
      border: 1px solid #cfd3d8;
      border-radius: 7px;
      padding: 8px 10px;
      font-weight: 650;
      line-height: 1;
    }
    .more-menu summary::-webkit-details-marker { display: none; }
    .more-menu[open] summary { background: #f0f1f2; }
    .more-panel {
      position: absolute;
      right: 0;
      top: calc(100% + 8px);
      z-index: 45;
      width: 230px;
      padding: 8px;
      display: grid;
      gap: 6px;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    .more-panel button { width: 100%; text-align: left; justify-content: flex-start; }
    header .top-actions button,
    header .top-actions input {
      color: var(--text);
      background: #fff;
      border-color: #cfd3d8;
      font-weight: 650;
    }
    header .top-actions button:hover { background: #f0f1f2; }
    header .top-actions button.primary {
      color: #fff;
      background: var(--teal);
      border-color: var(--teal);
    }
    header .top-actions button.primary:hover { background: var(--teal-dark); }
    header .small, header .counter { color: rgba(255,255,255,.78); }
    body.simple .advanced-action { display: none; }
    .counter { color: var(--muted); white-space: nowrap; }
    .crm-actions {
      padding: 10px 18px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      border-bottom: 1px solid var(--line);
      background: #fff;
    }
    .crm-actions b { display: block; font-size: 14px; }
    .scope-tabs button.active { background: var(--odoo); border-color: var(--odoo); color: #fff; }
    .filters {
      padding: 10px 18px;
      display: grid;
      grid-template-columns: 2fr repeat(7, minmax(95px, 1fr)) auto auto;
      gap: 8px;
      border-bottom: 1px solid var(--line);
      background: #edeff1;
    }
    .workbench {
      padding: 10px 18px;
      display: grid;
      grid-template-columns: repeat(4, minmax(150px, 1fr));
      gap: 10px;
      border-bottom: 1px solid var(--line);
      background: #fff;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: var(--panel-2);
      cursor: pointer;
    }
    .metric b { display: block; font-size: 20px; line-height: 1.1; }
    .metric span { color: var(--muted); font-size: 12px; }
    main { min-height: 0; display: grid; grid-template-columns: 1fr; }
    .board {
      display: grid;
      grid-template-columns: repeat(7, minmax(230px, 1fr));
      gap: 10px;
      padding: 12px;
      min-height: 0;
      overflow: auto;
    }
    .column {
      min-height: 0;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      display: grid;
      grid-template-rows: auto 1fr;
    }
    .col-head {
      padding: 11px 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--line);
      font-weight: 700;
    }
    .count { color: var(--muted); font-weight: 500; }
    .cards { padding: 10px; overflow: auto; min-height: 180px; }
    .card {
      background: #20262a;
      background: #fff;
      border: 1px solid #e0e2e5;
      border-left: 4px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      margin-bottom: 9px;
      box-shadow: 0 3px 10px rgba(31, 41, 51, .08);
      cursor: pointer;
    }
    .card.p3 { border-left-color: var(--gold); }
    .card.p2 { border-left-color: var(--orange); }
    .card.p1 { border-left-color: var(--blue); }
    .card-title { display: flex; justify-content: space-between; gap: 8px; font-weight: 750; }
    .stars { color: var(--gold); white-space: nowrap; letter-spacing: 0; }
    .meta, .small { color: var(--muted); font-size: 12px; }
    .card select { margin-top: 8px; padding: 5px 7px; }
    .list { display: none; overflow: auto; padding: 12px 18px; }
    table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }
    th, td { padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; }
    th { color: var(--muted); font-weight: 700; }
    tr { cursor: pointer; }
    .select-cell { width: 34px; text-align: center; }
    .select-cell input { width: 16px; height: 16px; accent-color: var(--teal); }
    .bulk-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      border: 1px solid var(--line);
      border-bottom: 0;
      background: #fbfbfc;
      padding: 10px;
    }
    .bulk-bar .quick { align-items: center; }
    .row-actions {
      display: flex;
      gap: 6px;
      min-width: 130px;
    }
    .row-actions button { padding: 6px 8px; }
    .drawer {
      position: fixed;
      inset: 0 0 0 auto;
      width: min(520px, 100vw);
      background: #fff;
      border-left: 1px solid var(--line);
      box-shadow: var(--shadow);
      transform: translateX(105%);
      transition: transform .18s ease;
      display: grid;
      grid-template-rows: auto 1fr;
      z-index: 20;
    }
    .drawer.open { transform: translateX(0); }
    .drawer-head { padding: 15px; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; gap: 10px; }
    .drawer-body { padding: 15px; overflow: auto; display: grid; gap: 14px; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; }
    label { display: grid; gap: 5px; color: var(--muted); font-size: 12px; }
    label span { color: var(--muted); }
    .full { grid-column: 1 / -1; }
    .quick { display: flex; flex-wrap: wrap; gap: 8px; }
    .star-picker button { border: 0; background: transparent; color: var(--gold); font-size: 21px; padding: 2px; }
    .timeline { display: grid; gap: 8px; }
    .event { border: 1px solid var(--line); background: #fbfbfc; border-radius: 8px; padding: 8px 9px; }
    .event b { display: block; font-size: 12px; color: #4b5563; }
    .prompt-box {
      min-height: 420px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre;
    }
    .sequence {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      align-items: center;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfbfc;
    }
    .pill {
      border: 1px solid #d7c9d4;
      background: #fff;
      color: var(--odoo);
      border-radius: 999px;
      padding: 4px 9px;
      font-size: 12px;
      font-weight: 700;
    }
    .pill.done { background: #eee7ec; color: #5f3f58; }
    .pill.current { background: var(--odoo); color: #fff; border-color: var(--odoo); }
    .fit-box {
      border-left: 4px solid var(--teal);
      background: #eefaf9;
      border-radius: 8px;
      padding: 10px;
      color: #24565a;
    }
    .setup-banner {
      border: 1px solid #f0d08a;
      background: #fff7df;
      color: #6d4a00;
      border-radius: 8px;
      padding: 10px 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
    }
    .optimizer {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fff;
      display: grid;
      gap: 10px;
    }
    .scorebar {
      height: 8px;
      border-radius: 999px;
      background: #e5e7eb;
      overflow: hidden;
    }
    .scorebar span { display: block; height: 100%; background: var(--teal); }
    .outcomes { display: flex; flex-wrap: wrap; gap: 6px; }
    .outcomes button.active { background: var(--odoo); border-color: var(--odoo); color: #fff; }
    .modal {
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,.55);
      display: none;
      place-items: center;
      z-index: 30;
    }
    .modal.open { display: grid; }
    .dialog {
      width: min(760px, calc(100vw - 24px));
      max-height: calc(100vh - 24px);
      overflow: auto;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 16px;
      display: grid;
      gap: 12px;
    }
    .dialog-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
    .tabs { display: flex; gap: 7px; flex-wrap: wrap; }
    .tabs button.active { background: var(--odoo); color: #fff; }
    .hidden { display: none !important; }
    @media (max-width: 1100px) {
      body { overflow: auto; }
      .app { height: auto; min-height: 100vh; }
      .workbench { grid-template-columns: 1fr 1fr; }
      .filters { grid-template-columns: 1fr 1fr; }
      .board { grid-template-columns: repeat(7, 260px); }
    }
    @media (max-width: 640px) {
      header { align-items: flex-start; flex-direction: column; }
      .workbench { grid-template-columns: 1fr; }
      .filters { grid-template-columns: 1fr; }
      .grid { grid-template-columns: 1fr; }
      .setup-banner { align-items: flex-start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <div class="app">
    <header>
      <div>
        <h1>Odoo Gmail Draft Assistant</h1>
        <div class="small">Import Odoo leads, create real Gmail drafts, export update notes back to Odoo</div>
      </div>
      <div class="top-actions">
        <span class="counter" id="dailyCounter">0 drafted today</span>
        <input id="batchLimit" type="number" min="1" max="500" value="20" aria-label="Daily draft target">
        <button class="primary" id="draftBatch">Emails for today</button>
        <button id="importBtn">Import Odoo CSV</button>
        <button id="geminiPromptBtn">Gemini research</button>
        <button id="howToBtn">How to use</button>
        <details class="more-menu">
          <summary>More</summary>
          <div class="more-panel">
            <button id="setupBtn">Setup</button>
            <button id="settingsBtn">Settings</button>
            <button id="healthBtn">Health</button>
            <button id="templatesBtn">CSV templates</button>
            <button id="versionGuideBtn">Odoo version guide</button>
            <button id="odooExportBtn">Odoo update CSV</button>
            <button id="modeBtn">Simple mode</button>
            <button id="weeklyBtn" class="advanced-action">Weekly coaching</button>
            <button id="playbookBtn" class="advanced-action">Playbook</button>
            <button id="exportBtn" class="advanced-action">Full export</button>
            <button id="agendaBtn" class="advanced-action">Agenda</button>
            <button id="suppressionBtn" class="advanced-action">Suppression</button>
          </div>
        </details>
      </div>
    </header>
    <section class="setup-banner hidden" id="setupBanner">
      <div><b>Gmail setup needed</b><div class="small">Add the Gmail address and Google App Password before coworkers draft emails.</div></div>
      <button id="setupBannerBtn">Setup Gmail</button>
    </section>
    <section class="workbench" id="workbench"></section>
    <section class="filters">
      <input id="filterQ" placeholder="Search company or contact">
      <select id="filterPriority"><option value="">Any priority</option><option value="3">★★★</option><option value="2">★★</option><option value="1">★</option><option value="0">0 stars</option></select>
      <input id="filterState" placeholder="State">
      <input id="filterCity" placeholder="City">
      <input id="filterIndustry" placeholder="Industry">
      <input id="filterSource" placeholder="Source">
      <select id="filterLostCategory"><option value="">Any category</option></select>
      <select id="sortBy"><option value="priority">Priority</option><option value="updated">Updated</option><option value="company">Company</option><option value="next_action">Next action</option><option value="category">Category</option><option value="lost_ts">Lost date</option></select>
      <button id="toggleView">List</button>
      <button id="clearFilters">Clear</button>
    </section>
    <section class="crm-actions">
      <div><b>CRM</b><span class="small">Manage leads, stages, follow-ups, and email readiness</span></div>
      <div class="quick scope-tabs"><button id="activeLeadsBtn" class="active">Active CRM</button><button id="lostLeadsBtn">Lost Leads</button><button id="newLead" class="primary">Add lead</button></div>
    </section>
    <main>
      <div id="board" class="board"></div>
      <div id="list" class="list"></div>
    </main>
  </div>

  <aside id="drawer" class="drawer">
    <div class="drawer-head">
      <div>
        <h1 id="drawerTitle">Lead</h1>
        <div class="small" id="drawerSub"></div>
      </div>
      <button class="icon" id="closeDrawer">×</button>
    </div>
    <div class="drawer-body">
      <div class="quick">
        <a id="callLink"><button>Call</button></a>
        <button id="emailLead">Draft email</button>
        <button id="buildLeadEmails">Build tailored emails</button>
        <a id="siteLink" target="_blank"><button>Site</button></a>
        <button id="engagedLead">Clicked email</button>
        <button class="danger" id="lostLead">Mark Lost</button>
        <button class="danger" id="optOutLead">Opt-out</button>
      </div>
      <div class="star-picker" id="starPicker"></div>
      <div id="replyOptimizer"></div>
      <div class="grid" id="leadForm"></div>
      <div id="leadEmailEditor"></div>
      <div>
        <h1 style="font-size:15px">Schedule activity</h1>
        <div class="grid">
          <select id="actType"><option>Call</option><option>Email</option><option>Meeting</option><option>To-do</option></select>
          <input id="actDue" type="date">
          <input id="actNote" class="full" placeholder="Activity note">
          <button id="addActivity" class="primary">Schedule</button>
        </div>
      </div>
      <div>
        <h1 style="font-size:15px">Add note</h1>
        <textarea id="noteText" placeholder="Write a note"></textarea>
        <button id="addNote">Add note</button>
      </div>
      <div>
        <h1 style="font-size:15px">Timeline</h1>
        <div id="timeline" class="timeline"></div>
      </div>
    </div>
  </aside>

  <div class="modal" id="modal"><div class="dialog" id="dialog"></div></div>
  <input type="file" id="csvFile" accept=".csv,text/csv" class="hidden">
  <input type="file" id="playbookFile" accept=".json,application/json" class="hidden">

  <script>
    const stages = ["New", "Contacted", "Follow-up", "Qualified", "Proposal", "Won", "Lost"];
    const state = { leads: [], current: null, timeline: [], view: "board", leadScope: "active", activeCategory: "", metric: "", selectedLeadIds: new Set(), settings: {}, daily: {}, industryLibrary: [], lostCategories: [], odooReleases: {} };
    const $ = (id) => document.getElementById(id);
    const esc = (s) => String(s ?? "").replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
    if (localStorage.getItem("simpleMode") === "1") document.body.classList.add("simple");
    const stars = (n) => "★★★".slice(0, Number(n || 0)) || "0";
    const parseTemplates = () => {
      try { return JSON.parse(state.settings.email_templates || "[]"); } catch { return []; }
    };
    const parsePresets = () => {
      try { return JSON.parse(state.settings.sequence_presets || "[]"); } catch { return []; }
    };
    const emailLabel = (l) => Number(l.email_stage || 1) > Number(l.email_plan_count || 6)
      ? "Sequence done"
      : `Email ${l.email_stage || 1}/${l.email_plan_count || 6}`;
    const activeStages = new Set(["New", "Contacted", "Follow-up", "Qualified", "Proposal"]);
    const isDraftReady = (l) => activeStages.has(l.stage) && l.email && !l.email_risky && Number(l.email_stage || 1) <= Number(l.email_plan_count || 6) && String(l.last_emailed || "").slice(0,10) !== new Date().toISOString().slice(0,10);
    const needsCleanup = (l) => activeStages.has(l.stage) && (!l.contact || !l.email || l.email_risky || !l.industry);
    const dueToday = (l) => activeStages.has(l.stage) && l.next_action_date && l.next_action_date <= new Date().toISOString().slice(0,10);
    const noNextStep = (l) => activeStages.has(l.stage) && !l.next_action && !l.next_action_date;
    const matchedOdooVersion = (l) => {
      const manual = String(l.odoo_evaluation_version || "").trim().replace(/^odoo\s*/i, "");
      const dateText = String(l.odoo_evaluation_date || "").trim();
      const yearText = String(l.odoo_evaluation_year || "").trim();
      let dt = "";
      if (/^\d{4}-\d{2}-\d{2}$/.test(dateText)) dt = dateText;
      else if (/^\d{4}-\d{2}$/.test(dateText)) dt = `${dateText}-15`;
      else if (/^\d{4}$/.test(dateText)) dt = `${dateText}-07-01`;
      if (dt) {
        if (dt < "2023-11-01") return "pre-Odoo 17";
        if (dt < "2024-10-01") return "Odoo 17";
        if (dt < "2025-09-01") return "Odoo 18";
        return "Odoo 19";
      }
      const y = (yearText.match(/\d{4}/) || [manual.match(/\d{2}/)?.[0] || ""])[0];
      if (String(y).startsWith("17")) return "Odoo 17";
      if (String(y).startsWith("18")) return "Odoo 18";
      if (String(y).startsWith("19")) return "Odoo 19";
      if (Number(y) && Number(y) <= 2023) return "Odoo 17";
      if (Number(y) === 2024) return "Odoo 18";
      if (Number(y) >= 2025) return "Odoo 19";
      return manual ? `Odoo ${manual}` : "Odoo 18";
    };
    const odooVersionSourceNote = (l) => `${matchedOdooVersion(l)} matched from ${l.odoo_evaluation_date ? "evaluation date" : l.odoo_evaluation_year ? "evaluation year" : l.odoo_evaluation_version ? "imported version" : "default fallback"}. Release claims use official Odoo 17, 18, 19, and 19.4 notes.`;
    const activeCategories = [
      ["ready", "Ready to draft", "Safe direct emails ready for today"],
      ["cleanup", "Needs cleanup", "Missing contact, email, industry, or safe domain"],
      ["due_today", "Due today", "Follow-ups or activities due now"],
      ["no_next_step", "No next step", "Active leads without an Odoo activity"],
      ["high_priority", "High priority", "Three-star leads"],
      ["new_unworked", "New / unworked", "New leads that have not been emailed"],
      ["in_follow_up", "In follow-up", "Contacted or follow-up stage"],
      ["proposal", "Proposal", "Proposal-stage opportunities"],
      ["won", "Won", "Closed-won records"],
    ];
    const activeCategoryMatch = (l, category) => ({
      ready: isDraftReady(l),
      cleanup: needsCleanup(l),
      due_today: dueToday(l),
      no_next_step: noNextStep(l),
      high_priority: Number(l.priority || 0) >= 3,
      new_unworked: l.stage === "New" && !l.last_emailed,
      in_follow_up: ["Contacted", "Follow-up"].includes(l.stage),
      proposal: l.stage === "Proposal",
      won: l.stage === "Won",
    }[category] || true);
    const activeCategoryLabel = (l) => (activeCategories.find(([key]) => activeCategoryMatch(l, key)) || ["", "Other"])[1];
    const sortClientLeads = (leads) => {
      if ($("sortBy")?.value !== "category") return leads;
      return [...leads].sort((a, b) => {
        const left = state.leadScope === "lost" ? (a.lost_category || "Other") : activeCategoryLabel(a);
        const right = state.leadScope === "lost" ? (b.lost_category || "Other") : activeCategoryLabel(b);
        return left.localeCompare(right) || String(a.company || "").localeCompare(String(b.company || ""));
      });
    };
    const visibleLeads = () => {
      const scoped = state.leadScope === "lost"
        ? state.leads.filter(l => l.stage === "Lost")
        : state.leads.filter(l => activeStages.has(l.stage) || l.stage === "Won");
      if (state.leadScope === "lost") return sortClientLeads(scoped);
      const categorized = state.activeCategory ? scoped.filter(l => activeCategoryMatch(l, state.activeCategory)) : scoped;
      if (state.metric === "ready") return sortClientLeads(categorized.filter(isDraftReady));
      if (state.metric === "cleanup") return sortClientLeads(categorized.filter(needsCleanup));
      if (state.metric === "today") return sortClientLeads(categorized.filter(dueToday));
      if (state.metric === "stale") return sortClientLeads(categorized.filter(noNextStep));
      return sortClientLeads(categorized);
    };
    const visibleLeadIds = () => visibleLeads().map(l => String(l.id));
    const selectedLeads = () => state.leads.filter(l => state.selectedLeadIds.has(String(l.id)));
    const clearSelection = () => { state.selectedLeadIds.clear(); renderList(); };
    const leadTemplates = (l) => {
      try {
        const parsed = JSON.parse(l.lead_email_templates || "[]");
        if (Array.isArray(parsed) && parsed.length) return parsed;
      } catch {}
      return parseTemplates();
    };
    const fitSummary = (l) => {
      const parts = [];
      if (l.sub_industry) parts.push(`${l.sub_industry} sub-industry`);
      else if (l.industry) parts.push(`${l.industry} buyer`);
      if (l.software_used) parts.push(`software: ${l.software_used}`);
      else {
        const hint = state.industryLibrary.find(x => x.industry === l.industry && x.sub_industry === l.sub_industry);
        if (hint?.software?.length) parts.push(`likely software: ${hint.software.slice(0,4).join(", ")}`);
      }
      if (l.software_renewal_date) parts.push(`renewal: ${l.software_renewal_date}`);
      if (l.city || l.state) parts.push(`local market: ${[l.city, l.state].filter(Boolean).join(", ")}`);
      if (l.source) parts.push(`source: ${l.source}`);
      if (l.industry_holes) parts.push(`holes: ${l.industry_holes}`);
      if (l.value_angle) parts.push(`angle: ${l.value_angle}`);
      if (l.notes) parts.push(`notes available for personalization`);
      return parts.length ? parts.join(" · ") : "Add industry, location, source, and notes to make drafts more tailored.";
    };
    const replyCtaForStyle = (style) => ({
      "Soft question": "Worth comparing?",
      "Send info": "Should I send over the quick version?",
      "Wrong person": "Am I reaching the right person for this?",
      "Permission": "Open to a quick look?",
      "Direct": "Would a 10 minute walkthrough be useful?",
      "Breakup": "Should I close this out for now?"
    }[style] || "Worth comparing?");
    const personalizationScore = (l) => {
      const checks = [
        !!l.contact,
        !!l.email && !String(l.email).match(/^(info|sales|hello|contact|support|admin|office|service)@/i),
        !Number(l.email_risky || 0),
        !!l.industry,
        !!l.sub_industry,
        !!l.software_used,
        !!l.custom_first_line,
        !!(l.pain_points || l.industry_holes),
        !!(l.value_angle || l.proof_points),
        !!(l.reply_cta || l.cta_style),
      ];
      return Math.round((checks.filter(Boolean).length / checks.length) * 100);
    };

    async function api(path, options = {}) {
      const res = await fetch(path, options);
      const text = await res.text();
      const data = text ? JSON.parse(text) : {};
      if (!res.ok) throw new Error(data.error || "Request failed");
      return data;
    }
    async function load() {
      const data = await api("/api/bootstrap");
      state.leads = data.leads;
      state.settings = data.settings;
      state.daily = data.daily;
      state.industryLibrary = data.industry_library || [];
      state.lostCategories = data.lost_categories || [];
      state.odooReleases = data.odoo_releases || {};
      $("batchLimit").value = data.daily.target || 20;
      render();
    }
    function renderCategoryOptions() {
      if (state.leadScope === "lost") {
        const current = $("filterLostCategory").value;
        $("filterLostCategory").innerHTML = `<option value="">Any lost category</option>${state.lostCategories.map(x => `<option>${esc(x)}</option>`).join("")}`;
        $("filterLostCategory").value = state.lostCategories.includes(current) ? current : "";
        return;
      }
      $("filterLostCategory").innerHTML = `<option value="">Any active category</option>${activeCategories.map(([key, label]) => `<option value="${esc(key)}">${esc(label)}</option>`).join("")}`;
      $("filterLostCategory").value = state.activeCategory;
    }
    function filters() {
      const p = new URLSearchParams();
      if ($("filterQ").value) p.set("q", $("filterQ").value);
      if ($("filterPriority").value) p.set("priority", $("filterPriority").value);
      if ($("filterState").value) p.set("state", $("filterState").value);
      if ($("filterCity").value) p.set("city", $("filterCity").value);
      if ($("filterIndustry").value) p.set("industry", $("filterIndustry").value);
      if ($("filterSource").value) p.set("source", $("filterSource").value);
      if (state.leadScope === "lost" && $("filterLostCategory").value) p.set("lost_category", $("filterLostCategory").value);
      if (state.leadScope === "active") state.activeCategory = $("filterLostCategory").value;
      p.set("sort", $("sortBy").value === "category" ? "priority" : $("sortBy").value);
      return p;
    }
    async function refreshLeads() {
      const data = await api("/api/leads?" + filters().toString());
      state.leads = data.leads;
      const known = new Set(state.leads.map(l => String(l.id)));
      [...state.selectedLeadIds].forEach(id => { if (!known.has(id)) state.selectedLeadIds.delete(id); });
      render();
    }
    function render() {
      $("dailyCounter").textContent = `${state.daily.drafted || 0} drafted / ${state.daily.target || 20} target today`;
      $("setupBanner").classList.toggle("hidden", Boolean(state.settings.gmail_email && state.settings.gmail_app_password));
      $("activeLeadsBtn").classList.toggle("active", state.leadScope === "active");
      $("lostLeadsBtn").classList.toggle("active", state.leadScope === "lost");
      renderCategoryOptions();
      renderWorkbench();
      renderBoard();
      renderList();
      $("board").style.display = state.view === "board" && state.leadScope !== "lost" ? "grid" : "none";
      $("list").style.display = state.view === "list" || state.leadScope === "lost" ? "block" : "none";
      $("toggleView").textContent = state.view === "board" ? "List" : "Board";
    }
    function renderWorkbench() {
      if (state.leadScope === "lost") {
        const lost = state.leads.filter(l => l.stage === "Lost");
        const categoryCounts = state.lostCategories
          .map(category => [category, lost.filter(l => (l.lost_category || "Other") === category).length])
          .filter(([, count]) => count)
          .slice(0, 4);
        $("workbench").innerHTML = [
          `<div class="metric"><b>${lost.length}</b><span>Lost leads</span><div class="small">Removed from active CRM</div></div>`,
          ...categoryCounts.map(([category, count]) => `<div class="metric" data-lost-category="${esc(category)}"><b>${count}</b><span>${esc(category)}</span><div class="small">Click to filter lost leads</div></div>`),
        ].join("");
        document.querySelectorAll("[data-lost-category]").forEach(card => card.onclick = () => {
          $("filterLostCategory").value = card.dataset.lostCategory;
          refreshLeads();
        });
        return;
      }
      const cards = [
        ["setup", state.settings.gmail_email && state.settings.gmail_app_password ? "Setup ready" : "Setup needed", state.settings.gmail_email && state.settings.gmail_app_password ? "OK" : "!", "Gmail, app password, and sender settings"],
        ["ready", "Ready to draft", state.leads.filter(isDraftReady).length, "Direct email, safe, next sequence step"],
        ["cleanup", "Needs cleanup", state.leads.filter(needsCleanup).length, "Missing contact, email, industry, or safe domain"],
        ["today", "Due today", state.leads.filter(dueToday).length, "Follow-ups colleagues should handle now"],
        ["stale", "No next step", state.leads.filter(noNextStep).length, "Active leads that need an Odoo activity"],
      ];
      $("workbench").innerHTML = cards.map(([key, label, count, hint]) => `<div class="metric" data-metric="${key}" style="${state.metric === key ? "border-color: var(--teal); background: #eefaf9" : ""}"><b>${count}</b><span>${esc(label)}</span><div class="small">${esc(hint)}</div></div>`).join("");
      document.querySelectorAll("[data-metric]").forEach(card => card.onclick = () => applyMetric(card.dataset.metric));
    }
    function applyMetric(metric) {
      state.metric = state.metric === metric ? "" : metric;
      if (metric === "setup") return openSetupModal();
      if (metric === "today" || metric === "stale") $("sortBy").value = "next_action";
      state.view = "list";
      render();
    }
    function renderBoard() {
      const boardStages = stages.filter(stage => stage !== "Lost");
      $("board").innerHTML = boardStages.map(stage => {
        const leads = visibleLeads().filter(l => l.stage === stage);
        return `<section class="column" data-stage="${esc(stage)}">
          <div class="col-head"><span>${esc(stage)}</span><span class="count">${leads.length}</span></div>
          <div class="cards" data-stage="${esc(stage)}">${leads.map(cardHtml).join("")}</div>
        </section>`;
      }).join("");
      document.querySelectorAll(".card").forEach(card => {
        card.addEventListener("click", (e) => { if (e.target.tagName !== "SELECT") openLead(card.dataset.id); });
        card.addEventListener("dragstart", e => e.dataTransfer.setData("text/plain", card.dataset.id));
      });
      document.querySelectorAll(".stageSelect").forEach(sel => sel.addEventListener("change", async e => {
        e.stopPropagation();
        const lead = state.leads.find(l => l.id == e.target.dataset.id);
        await saveLead(lead.id, {...lead, stage: e.target.value});
      }));
      document.querySelectorAll(".cards").forEach(zone => {
        zone.addEventListener("dragover", e => e.preventDefault());
        zone.addEventListener("drop", async e => {
          e.preventDefault();
          const id = e.dataTransfer.getData("text/plain");
          const lead = state.leads.find(l => l.id == id);
          await saveLead(id, {...lead, stage: zone.dataset.stage});
        });
      });
    }
    function cardHtml(l) {
      return `<article class="card p${l.priority}" draggable="true" data-id="${l.id}">
        <div class="card-title"><span>${esc(l.company)}</span><span class="stars">${stars(l.priority)}</span></div>
        <div>${esc(l.contact || "No contact")}</div>
        <div class="meta">${esc(l.phone || "")} ${l.phone && l.email ? " · " : ""}${esc(l.email || l.email_generic || "")}</div>
        <div class="meta">${esc([l.city, l.industry].filter(Boolean).join(" · "))}</div>
        <div class="small"><span class="pill ${Number(l.email_stage || 1) > Number(l.email_plan_count || 6) ? "done" : "current"}">${esc(emailLabel(l))}</span></div>
        <div class="small">${esc(l.next_action || "No next action")} ${l.next_action_date ? " · " + esc(l.next_action_date) : ""}</div>
        <select class="stageSelect" data-id="${l.id}">${stages.map(s => `<option ${s===l.stage?"selected":""}>${s}</option>`).join("")}</select>
      </article>`;
    }
    function renderList() {
      const lostView = state.leadScope === "lost";
      const selectedCount = selectedLeads().length;
      const visibleIds = visibleLeadIds();
      const allVisibleSelected = visibleIds.length > 0 && visibleIds.every(id => state.selectedLeadIds.has(id));
      const bulkBar = selectedCount ? `<div class="bulk-bar">
        <div><b>${selectedCount}</b> selected <button id="clearSelected">Clear</button></div>
        <div class="quick">
          <button id="bulkActivity">Schedule activity</button>
          ${lostView ? `<button id="bulkRestore">Restore</button>` : `<button id="bulkLost">Mark lost</button>`}
          <button class="danger" id="bulkDelete">Delete</button>
        </div>
      </div>` : "";
      const head = lostView
        ? `<tr><th class="select-cell"><input id="selectAllRows" type="checkbox" ${allVisibleSelected ? "checked" : ""} aria-label="Select all visible leads"></th><th>Company</th><th>Contact</th><th>Email</th><th>Category</th><th>Matched</th><th>Reason</th><th>Lost date</th><th>Actions</th></tr>`
        : `<tr><th class="select-cell"><input id="selectAllRows" type="checkbox" ${allVisibleSelected ? "checked" : ""} aria-label="Select all visible leads"></th><th>Priority</th><th>Category</th><th>Company</th><th>Contact</th><th>Email step</th><th>Email</th><th>Phone</th><th>Stage</th><th>Next action</th><th>Actions</th></tr>`;
      const rows = visibleLeads().map(l => lostView
        ? `<tr data-id="${l.id}"><td class="select-cell"><input type="checkbox" data-select-lead="${l.id}" ${state.selectedLeadIds.has(String(l.id)) ? "checked" : ""} aria-label="Select ${esc(l.company)}"></td><td>${esc(l.company)}</td><td>${esc(l.contact)}</td><td>${esc(l.email || l.email_generic)}</td><td>${esc(l.lost_category || "Other")}</td><td>${esc(matchedOdooVersion(l))}</td><td>${esc(l.lost_reason || "")}</td><td>${esc((l.lost_ts || "").slice(0,10))}</td><td class="row-actions"><button data-list-recover="${l.id}">Recovery email</button><button data-list-reopen="${l.id}">Reopen</button><button data-list-restore="${l.id}">Restore</button><button class="danger" data-list-delete="${l.id}">Delete</button></td></tr>`
        : `<tr data-id="${l.id}"><td class="select-cell"><input type="checkbox" data-select-lead="${l.id}" ${state.selectedLeadIds.has(String(l.id)) ? "checked" : ""} aria-label="Select ${esc(l.company)}"></td><td>${stars(l.priority)}</td><td>${esc(activeCategoryLabel(l))}</td><td>${esc(l.company)}</td><td>${esc(l.contact)}</td><td>${esc(emailLabel(l))}</td><td>${esc(l.email || l.email_generic)}</td><td>${esc(l.phone)}</td><td>${esc(l.stage)}</td><td>${esc(l.next_action || "")}</td><td class="row-actions"><button data-list-lost="${l.id}">Lost</button><button class="danger" data-list-delete="${l.id}">Delete</button></td></tr>`
      ).join("");
      $("list").innerHTML = `${bulkBar}<table><thead>${head}</thead><tbody>${rows}</tbody></table>`;
      document.querySelectorAll("tr[data-id]").forEach(row => row.addEventListener("click", (e) => {
        if (e.target.closest("button") || e.target.closest("input")) return;
        openLead(row.dataset.id);
      }));
      $("selectAllRows")?.addEventListener("change", e => {
        visibleIds.forEach(id => e.target.checked ? state.selectedLeadIds.add(id) : state.selectedLeadIds.delete(id));
        renderList();
      });
      document.querySelectorAll("[data-select-lead]").forEach(box => box.onchange = e => {
        const id = String(e.target.dataset.selectLead);
        e.target.checked ? state.selectedLeadIds.add(id) : state.selectedLeadIds.delete(id);
        renderList();
      });
      $("clearSelected")?.addEventListener("click", clearSelection);
      $("bulkActivity")?.addEventListener("click", bulkScheduleActivity);
      $("bulkLost")?.addEventListener("click", bulkMarkLost);
      $("bulkRestore")?.addEventListener("click", bulkRestoreLeads);
      $("bulkDelete")?.addEventListener("click", bulkDeleteLeads);
      document.querySelectorAll("[data-list-lost]").forEach(btn => btn.onclick = () => markLeadLost(btn.dataset.listLost));
      document.querySelectorAll("[data-list-recover]").forEach(btn => btn.onclick = () => draftLostRecovery(btn.dataset.listRecover));
      document.querySelectorAll("[data-list-reopen]").forEach(btn => btn.onclick = () => reopenLead(btn.dataset.listReopen));
      document.querySelectorAll("[data-list-restore]").forEach(btn => btn.onclick = () => restoreLead(btn.dataset.listRestore));
      document.querySelectorAll("[data-list-delete]").forEach(btn => btn.onclick = () => deleteLead(btn.dataset.listDelete));
    }
    async function openLead(id) {
      const data = await api(`/api/leads/${id}`);
      state.current = data.lead;
      state.timeline = data.timeline;
      renderDrawer();
      $("drawer").classList.add("open");
    }
    function renderDrawer() {
      const l = state.current;
      $("drawerTitle").textContent = l.company;
      $("drawerSub").textContent = [l.contact, l.stage, l.stage === "Lost" ? matchedOdooVersion(l) : emailLabel(l), l.source].filter(Boolean).join(" · ");
      $("callLink").href = l.phone ? `tel:${l.phone}` : "#";
      $("siteLink").href = l.website ? (l.website.startsWith("http") ? l.website : `https://${l.website}`) : "#";
      $("emailLead").textContent = l.stage === "Lost"
        ? "Recovery email"
        : (Number(l.email_stage || 1) > Number(l.email_plan_count || 6) ? "Sequence done" : `Draft Email ${l.email_stage || 1}`);
      $("engagedLead").textContent = l.stage === "Lost" ? "Reopen + call" : "Clicked email";
      $("starPicker").innerHTML = [1,2,3].map(n => `<button data-star="${n}">${n <= l.priority ? "★" : "☆"}</button>`).join("") + `<button data-star="0">0</button>`;
      document.querySelectorAll("[data-star]").forEach(b => b.onclick = () => saveLead(l.id, {...l, priority: Number(b.dataset.star), priority_manual: 1}));
      renderReplyOptimizer();
      const fields = [
        ["company","Company"],["contact","Contact"],["title","Title"],["email","Direct Email"],["email_generic","Generic Email"],
        ["phone","Phone"],["website","Website"],["city","City"],["state","State"],["industry","Industry"],["sub_industry","Sub-industry"],["software_used","Software used / likely stack"],["software_renewal_date","Software renewal date"],["odoo_evaluation_date","Odoo evaluation date"],["odoo_evaluation_version","Odoo evaluation version"],["odoo_evaluation_year","Odoo evaluation year"],["previous_blocker","Previous blocker"],["apps_evaluated","Apps evaluated"],["previous_demo_notes","Previous demo notes"],["recovery_last_drafted","Last recovery drafted"],["source","Source"],
        ["stage","Stage"],["email_stage","Current email #"],["email_plan_count","Planned emails"],
        ["company_summary","Company summary"],["why_now","Why now"],["workflow_hypothesis","Workflow hypothesis"],
        ["research_evidence","Research evidence"],["research_source_url","Research source URL"],["confidence","Research confidence"],["do_not_claim","Do not claim"],
        ["reason_to_believe","Reason to believe"],["role_lens","Role lens"],["claim_safety","Claim safety"],["customer_language","Customer language"],["reply_type_goal","Reply type goal"],
        ["pain_points","Pain points"],["industry_holes","Industry holes"],["value_angle","Value angle"],["proof_points","Proof points"],
        ["lost_category","Lost category"],["lost_reason","Lost reason"],["lost_ts","Lost date"],
        ["next_action","Next action"],["next_action_date","Next action date"],["notes","Notes"]
      ];
      $("leadForm").innerHTML = fields.map(([key,label]) => {
        if (key === "stage") return `<label><span>${label}</span><select data-field="${key}">${stages.map(s => `<option ${s===l.stage?"selected":""}>${s}</option>`).join("")}</select></label>`;
        if (["notes","pain_points","industry_holes","value_angle","proof_points","company_summary","why_now","workflow_hypothesis","research_evidence","do_not_claim","reason_to_believe","role_lens","customer_language","reply_type_goal","previous_demo_notes","previous_blocker"].includes(key)) return `<label class="full"><span>${label}</span><textarea data-field="${key}">${esc(l[key] || "")}</textarea></label>`;
        const type = key === "next_action_date" || key === "software_renewal_date" || key === "odoo_evaluation_date" ? "date" : (key === "email_stage" || key === "email_plan_count" ? "number" : "text");
        const list = key === "industry" ? ' list="industryOptions"' : key === "sub_industry" ? ' list="subIndustryOptions"' : "";
        return `<label><span>${label}</span><input type="${type}"${list} data-field="${key}" value="${esc(l[key] || "")}"></label>`;
      }).join("");
      $("leadForm").insertAdjacentHTML("beforeend", `<datalist id="industryOptions">${[...new Set(state.industryLibrary.map(x => x.industry))].map(x => `<option value="${esc(x)}"></option>`).join("")}</datalist><datalist id="subIndustryOptions">${state.industryLibrary.map(x => `<option value="${esc(x.sub_industry)}">${esc(x.industry)}</option>`).join("")}</datalist>`);
      const industryInput = document.querySelector('[data-field="industry"]');
      const subIndustryInput = document.querySelector('[data-field="sub_industry"]');
      const softwareInput = document.querySelector('[data-field="software_used"]');
      function fillSoftwareHint() {
        if (!softwareInput || softwareInput.value.trim()) return;
        const match = state.industryLibrary.find(x => x.industry === industryInput?.value && x.sub_industry === subIndustryInput?.value)
          || state.industryLibrary.find(x => x.sub_industry === subIndustryInput?.value)
          || state.industryLibrary.find(x => x.industry === industryInput?.value);
        if (match?.software?.length) softwareInput.value = match.software.slice(0, 6).join(", ");
      }
      industryInput?.addEventListener("change", fillSoftwareHint);
      subIndustryInput?.addEventListener("change", fillSoftwareHint);
      const lostVersionBox = l.stage === "Lost" ? `<div class="full fit-box"><b>Lost recovery match</b><br>${esc(odooVersionSourceNote(l))}${l.previous_blocker ? `<br><b>Old blocker:</b> ${esc(l.previous_blocker)}` : ""}</div>` : "";
      $("leadForm").insertAdjacentHTML("afterbegin", `<div class="full sequence">${Array.from({length:Number(l.email_plan_count || 6)}, (_,i) => {
        const n = i + 1;
        const cls = n < Number(l.email_stage || 1) ? "done" : n === Number(l.email_stage || 1) ? "current" : "";
        return `<span class="pill ${cls}">Email ${n}</span>`;
      }).join("")}</div>${lostVersionBox}<div class="full fit-box"><b>Personalization brief</b><br>${esc(fitSummary(l))}</div>`);
      document.querySelectorAll("[data-field]").forEach(input => input.addEventListener("change", () => {
        const next = {...state.current};
        document.querySelectorAll("[data-field]").forEach(el => next[el.dataset.field] = el.value);
        saveLead(next.id, next, true);
      }));
      renderLeadEmailEditor();
      $("timeline").innerHTML = state.timeline.map(e => `<div class="event"><b>${esc(e.type)} · ${esc(e.ts)}</b>${esc(e.text)}</div>`).join("") || `<div class="small">No activity yet.</div>`;
    }
    function renderReplyOptimizer() {
      const l = state.current;
      const score = personalizationScore(l);
      const angles = ["Operational pain", "Software renewal", "Missed follow-up", "Inventory / field service gap", "QuickBooks / spreadsheet sprawl", "Competitor replacement", "Wrong person"];
      const ctaStyles = ["Soft question", "Send info", "Wrong person", "Permission", "Direct", "Breakup"];
      const outcomes = l.stage === "Lost"
        ? ["Recovery drafted", "Reopened", "Not now", "Still closed", "Wrong person", "Meeting booked", "Asked to stop"]
        : ["Replied positive", "Replied neutral", "Not interested", "No reply", "Booked", "Wrong person", "Asked to stop"];
      $("replyOptimizer").innerHTML = `<div class="optimizer">
        <div>
          <div class="dialog-head" style="padding:0">
            <div><h1 style="font-size:15px">Reply Optimizer</h1><div class="small">Tune this draft for replies, then track what happened.</div></div>
            <span class="pill ${score >= 80 ? "done" : score >= 60 ? "current" : ""}">${score}% ready</span>
          </div>
          <div class="scorebar"><span style="width:${score}%"></span></div>
        </div>
        <div class="grid">
          <label><span>Sequence angle</span><select data-field="sequence_angle">${angles.map(x => `<option ${x===(l.sequence_angle || "")?"selected":""}>${x}</option>`).join("")}</select></label>
          <label><span>CTA style</span><select data-field="cta_style">${ctaStyles.map(x => `<option ${x===(l.cta_style || "Soft question")?"selected":""}>${x}</option>`).join("")}</select></label>
          <label class="full"><span>Custom first line</span><input data-field="custom_first_line" value="${esc(l.custom_first_line || "")}" placeholder="Saw your team handles field service across Phoenix."></label>
          <label><span>Reply goal</span><input data-field="reply_goal" value="${esc(l.reply_goal || "Start a low-friction reply conversation")}"></label>
          <label><span>Reply CTA</span><input data-field="reply_cta" value="${esc(l.reply_cta || replyCtaForStyle(l.cta_style || "Soft question"))}"></label>
        </div>
        <div>
          <div class="small">Outcome</div>
          <div class="outcomes">${outcomes.map(x => `<button data-outcome="${esc(x)}" class="${x===(l.reply_outcome || "")?"active":""}">${esc(x)}</button>`).join("")}</div>
          <div class="small">${l.reply_outcome ? `Last outcome: ${esc(l.reply_outcome)} ${l.reply_outcome_ts ? " · " + esc(l.reply_outcome_ts) : ""}` : "No reply outcome recorded yet."}</div>
        </div>
      </div>`;
      document.querySelectorAll("[data-outcome]").forEach(b => b.onclick = async () => {
        const outcome = b.dataset.outcome;
        if (outcome === "Reopened") return reopenLead(l.id);
        await saveLead(l.id, {...state.current, reply_outcome: outcome, reply_outcome_ts: ""}, true);
      });
      const ctaSelect = document.querySelector('[data-field="cta_style"]');
      ctaSelect?.addEventListener("change", () => {
        const ctaInput = document.querySelector('[data-field="reply_cta"]');
        if (ctaInput && !ctaInput.value.trim()) ctaInput.value = replyCtaForStyle(ctaSelect.value);
      });
    }
    function renderLeadEmailEditor() {
      const l = state.current;
      const count = Math.max(1, Math.min(30, Number(l.email_plan_count || state.settings.email_plan_count || 6)));
      const templates = leadTemplates(l);
      const presets = parsePresets();
      $("leadEmailEditor").innerHTML = `<div class="dialog-head" style="padding:0">
          <div><h1 style="font-size:15px">Opportunity email sequence</h1><div class="small">Tailored to this lead's sub-industry, holes, pain points, and value angle.</div></div>
          <button id="saveLeadEmails" class="primary">Save emails</button>
        </div>
        <div class="grid">
          <label><span>Sequence preset</span><select id="presetSelect"><option value="">Choose a play</option>${presets.map((p, i) => `<option value="${i}">${esc(p.name)}</option>`).join("")}</select></label>
          <label><span>&nbsp;</span><button id="applyPreset">Apply preset</button></label>
        </div>
        <div class="fit-box"><b>What this sequence should hammer:</b><br>${esc(fitSummary(l))}</div>
        <div class="timeline">${Array.from({length: count}, (_, i) => {
          const n = i + 1;
          const tpl = templates[i] || {subject:`Follow-up for {company} - email ${n}`, body:`<p>Hi {first_name},</p><p>I wanted to follow up about {industry_holes}.</p>`};
          return `<div class="event">
            <b>Email ${n}${n === Number(l.email_stage || 1) ? " · current" : ""}</b>
            <label><span>Subject</span><input data-lead-subject="${i}" value="${esc(tpl.subject || "")}"></label>
            <label><span>Body HTML</span><textarea data-lead-body="${i}">${esc(tpl.body || "")}</textarea></label>
          </div>`;
        }).join("")}</div>`;
      $("saveLeadEmails").onclick = async () => {
        const nextTemplates = Array.from({length: count}, (_, i) => ({
          subject: document.querySelector(`[data-lead-subject="${i}"]`)?.value || "",
          body: document.querySelector(`[data-lead-body="${i}"]`)?.value || "",
        }));
        await saveLead(l.id, {...state.current, lead_email_templates: JSON.stringify(nextTemplates)}, true);
      };
      $("applyPreset").onclick = async () => {
        const idx = $("presetSelect").value;
        if (idx === "") return;
        const preset = presets[Number(idx)];
        if (!preset) return;
        const presetTemplates = (preset.templates || []).slice(0, count);
        while (presetTemplates.length < count) presetTemplates.push(presetTemplates[presetTemplates.length - 1] || {subject:"Following up with {company}", body:"<p>Hi {first_name},</p><p>{custom_first_line}</p><p>{reply_cta}</p>"});
        await saveLead(l.id, {
          ...state.current,
          sequence_angle: preset.angle || state.current.sequence_angle,
          cta_style: preset.cta_style || state.current.cta_style,
          reply_cta: preset.reply_cta || state.current.reply_cta,
          lead_email_templates: JSON.stringify(presetTemplates),
        }, true);
      };
    }
    async function saveLead(id, data, stayOpen=false) {
      const res = await api(`/api/leads/${id}`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(data)});
      state.current = res.lead;
      state.timeline = res.timeline;
      await refreshLeads();
      if (stayOpen) renderDrawer();
    }
    async function newLead() {
      const values = await friendlyForm({
        title: "Add Lead",
        body: "Start with the company name. You can add contact details, research, and email angles after the lead opens.",
        confirmText: "Add lead",
        fields: [{id:"company", label:"Company name", required:true, placeholder:"Example: Acme Distribution"}]
      });
      if (!values) return;
      const res = await api("/api/leads", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({company: values.company})});
      await refreshLeads();
      if (res.id) openLead(res.id);
    }
    async function markLeadLost(id) {
      const lead = state.leads.find(l => String(l.id) === String(id));
      const values = await lostLeadForm(lead, 1);
      if (!values) return;
      await api(`/api/leads/${id}/lost`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(values)});
      if (state.current && String(state.current.id) === String(id)) $("drawer").classList.remove("open");
      state.leadScope = "active";
      await refreshLeads();
    }
    async function bulkMarkLost() {
      const leads = selectedLeads().filter(l => l.stage !== "Lost");
      if (!leads.length) return friendlyAlert("No Active Leads Selected", "Select one or more active leads first, then choose Mark lost.");
      const values = await lostLeadForm(null, leads.length);
      if (!values) return;
      for (const lead of leads) {
        await api(`/api/leads/${lead.id}/lost`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(values)});
        state.selectedLeadIds.delete(String(lead.id));
      }
      await refreshLeads();
    }
    async function bulkScheduleActivity() {
      const leads = selectedLeads();
      if (!leads.length) return friendlyAlert("No Leads Selected", "Select the leads you want to update first, then choose Schedule activity.");
      const values = await friendlyForm({
        title: `Schedule Activity For ${leads.length} Lead${leads.length === 1 ? "" : "s"}`,
        body: "This will add the same next step to every selected lead.",
        confirmText: "Schedule activity",
        fields: [
          {id:"type", label:"Activity type", type:"select", value:"Call", options:["Call","Email","Meeting","To-do"], required:true},
          {id:"due", label:"Due date", type:"date", value:new Date().toISOString().slice(0,10), required:true},
          {id:"note", label:"Activity note", type:"textarea", value:"Follow up", required:false}
        ]
      });
      if (!values) return;
      const cleanType = ["Call","Email","Meeting","To-do"].includes(values.type) ? values.type : "To-do";
      for (const lead of leads) {
        await api(`/api/leads/${lead.id}/activity`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({type: cleanType, due: values.due, note: values.note})});
      }
      state.selectedLeadIds.clear();
      await refreshLeads();
    }
    async function bulkRestoreLeads() {
      const leads = selectedLeads().filter(l => l.stage === "Lost");
      if (!leads.length) return friendlyAlert("No Lost Leads Selected", "Select one or more lost leads first, then choose Restore.");
      const ok = await friendlyConfirm({
        title: "Restore Leads",
        body: `Move ${leads.length} selected lead${leads.length === 1 ? "" : "s"} back into Active CRM?`,
        confirmText: "Restore"
      });
      if (!ok) return;
      for (const lead of leads) {
        await api(`/api/leads/${lead.id}/restore`, {method:"POST"});
        state.selectedLeadIds.delete(String(lead.id));
      }
      state.leadScope = "active";
      await refreshLeads();
    }
    async function restoreLead(id) {
      const lead = state.leads.find(l => String(l.id) === String(id));
      const ok = await friendlyConfirm({title:"Restore Lead", body:`Move ${lead?.company || "this lead"} back into Active CRM?`, confirmText:"Restore"});
      if (!ok) return;
      await api(`/api/leads/${id}/restore`, {method:"POST"});
      state.leadScope = "active";
      await refreshLeads();
    }
    async function reopenLead(id) {
      const lead = state.leads.find(l => String(l.id) === String(id)) || state.current;
      const ok = await friendlyConfirm({title:"Reopen Lead", body:`Move ${lead?.company || "this lead"} back into Active CRM and schedule a follow-up call?`, confirmText:"Reopen"});
      if (!ok) return;
      await api(`/api/leads/${id}/reopen`, {method:"POST"});
      state.leadScope = "active";
      if (state.current && String(state.current.id) === String(id)) await openLead(id);
      await refreshLeads();
    }
    async function draftLostRecovery(id) {
      const lead = state.leads.find(l => String(l.id) === String(id)) || state.current;
      const ok = await friendlyConfirm({title:"Draft Recovery Email", body:`Create a Gmail recovery draft for ${lead?.company || "this lost lead"}?`, confirmText:"Draft to Gmail"});
      if (!ok) return;
      try {
        await api(`/api/leads/${id}/lost-recovery-draft`, {method:"POST"});
        await friendlyAlert("Draft Created", "Recovery email drafted in Gmail.");
      } catch (err) {
        await friendlyAlert("Draft Failed", err.message || "Could not create recovery draft.");
      }
      if (state.current && String(state.current.id) === String(id)) await openLead(id);
      await refreshLeads();
    }
    async function deleteLead(id) {
      const lead = state.leads.find(l => String(l.id) === String(id));
      const name = lead?.company || "this lead";
      const ok = await friendlyConfirm({title:"Delete Lead", body:`Delete ${name}? This removes the lead, notes, and activities from this app.`, confirmText:"Delete", danger:true});
      if (!ok) return;
      await api(`/api/leads/${id}/delete`, {method:"POST"});
      if (state.current && String(state.current.id) === String(id)) $("drawer").classList.remove("open");
      await refreshLeads();
    }
    async function bulkDeleteLeads() {
      const leads = selectedLeads();
      if (!leads.length) return friendlyAlert("No Leads Selected", "Select one or more leads first, then choose Delete.");
      const ok = await friendlyConfirm({
        title: "Delete Selected Leads",
        body: `Delete ${leads.length} selected lead${leads.length === 1 ? "" : "s"}? This removes their notes and activities from this app.`,
        confirmText: "Delete",
        danger: true
      });
      if (!ok) return;
      for (const lead of leads) {
        await api(`/api/leads/${lead.id}/delete`, {method:"POST"});
        state.selectedLeadIds.delete(String(lead.id));
      }
      if (state.current && !state.leads.some(l => String(l.id) === String(state.current.id) && state.selectedLeadIds.has(String(l.id)))) {
        $("drawer").classList.remove("open");
      }
      await refreshLeads();
    }
    let modalBackdropAction = null;
    function openModal(html, onBackdrop=null) {
      modalBackdropAction = onBackdrop;
      $("dialog").innerHTML = html;
      $("modal").classList.add("open");
    }
    function closeModal() {
      modalBackdropAction = null;
      $("modal").classList.remove("open");
    }
    function friendlyConfirm({title, body, confirmText="Confirm", cancelText="Cancel", danger=false}) {
      return new Promise(resolve => {
        let done = false;
        const finish = value => {
          if (done) return;
          done = true;
          closeModal();
          resolve(value);
        };
        openModal(`<div class="dialog-head"><h1>${esc(title)}</h1><button class="icon" id="modalX">×</button></div>
          <div class="fit-box">${esc(body)}</div>
          <div class="quick"><button id="modalCancel">${esc(cancelText)}</button><button class="${danger ? "danger" : "primary"}" id="modalConfirm">${esc(confirmText)}</button></div>`, () => finish(false));
        $("modalX").onclick = () => finish(false);
        $("modalCancel").onclick = () => finish(false);
        $("modalConfirm").onclick = () => finish(true);
      });
    }
    function friendlyAlert(title, body, buttonText="OK") {
      return new Promise(resolve => {
        let done = false;
        const finish = () => {
          if (done) return;
          done = true;
          closeModal();
          resolve();
        };
        openModal(`<div class="dialog-head"><h1>${esc(title)}</h1><button class="icon" id="modalX">×</button></div>
          <div class="fit-box">${esc(body)}</div>
          <div class="quick"><button class="primary" id="modalConfirm">${esc(buttonText)}</button></div>`, finish);
        $("modalX").onclick = finish;
        $("modalConfirm").onclick = finish;
      });
    }
    function friendlyField(field) {
      const id = `modalField_${field.id}`;
      const required = field.required ? " data-required='1'" : "";
      if (field.type === "textarea") {
        return `<label class="full"><span>${esc(field.label)}</span><textarea id="${id}"${required} placeholder="${esc(field.placeholder || "")}">${esc(field.value || "")}</textarea></label>`;
      }
      if (field.type === "select") {
        const options = (field.options || []).map(opt => `<option value="${esc(opt)}" ${String(opt) === String(field.value || "") ? "selected" : ""}>${esc(opt)}</option>`).join("");
        return `<label><span>${esc(field.label)}</span><select id="${id}"${required}>${options}</select></label>`;
      }
      return `<label><span>${esc(field.label)}</span><input id="${id}" type="${esc(field.type || "text")}" value="${esc(field.value || "")}"${required} placeholder="${esc(field.placeholder || "")}"></label>`;
    }
    function friendlyForm({title, body="", fields=[], confirmText="Save", cancelText="Cancel", danger=false}) {
      return new Promise(resolve => {
        let done = false;
        const finish = value => {
          if (done) return;
          done = true;
          closeModal();
          resolve(value);
        };
        openModal(`<div class="dialog-head"><h1>${esc(title)}</h1><button class="icon" id="modalX">×</button></div>
          ${body ? `<div class="fit-box">${esc(body)}</div>` : ""}
          <div class="grid">${fields.map(friendlyField).join("")}</div>
          <div id="modalFormError" class="small" style="color:#9f2633"></div>
          <div class="quick"><button id="modalCancel">${esc(cancelText)}</button><button class="${danger ? "danger" : "primary"}" id="modalConfirm">${esc(confirmText)}</button></div>`, () => finish(null));
        $("modalX").onclick = () => finish(null);
        $("modalCancel").onclick = () => finish(null);
        $("modalConfirm").onclick = () => {
          const values = {};
          for (const field of fields) {
            const el = $(`modalField_${field.id}`);
            const value = (el?.value || "").trim();
            if (field.required && !value) {
              $("modalFormError").textContent = `${field.label} is required.`;
              el?.focus();
              return;
            }
            values[field.id] = value;
          }
          finish(values);
        };
      });
    }
    async function lostLeadForm(lead, count=1) {
      const name = lead?.company || `${count} selected lead${count === 1 ? "" : "s"}`;
      const values = await friendlyForm({
        title: "Mark Lost",
        body: `Move ${name} into Lost Leads with enough context for a future recovery email.`,
        confirmText: "Mark lost",
        fields: [
          {id:"category", label:"Lost category", type:"select", value:"Past demo - no move forward", options:state.lostCategories, required:true},
          {id:"reason", label:"Short reason", type:"textarea", placeholder:"Example: Evaluated Odoo last year but stayed with current system.", required:true}
        ]
      });
      if (!values) return null;
      return {
        category: state.lostCategories.includes(values.category) ? values.category : "Other",
        reason: values.reason
      };
    }
    function extractCsvFromGemini(text) {
      const raw = String(text || "").trim();
      const fenced = raw.match(/```csv\s*([\s\S]*?)```/i) || raw.match(/```\s*([\s\S]*?)```/);
      if (fenced) return fenced[1].trim();
      const lines = raw.split(/\r?\n/);
      const start = lines.findIndex(line => {
        const clean = line.trim().replace(/^\uFEFF/, "");
        return clean.startsWith('"company","contact"') || clean.startsWith("company,contact");
      });
      return start >= 0 ? lines.slice(start).join("\n").trim() : raw;
    }
    function looksLikeImportCsv(csvText) {
      const first = String(csvText || "").trim().split(/\r?\n/)[0] || "";
      const normalized = first.replace(/\s+/g, "").replaceAll('"', "").toLowerCase();
      return normalized.startsWith("company,contact,title,email,email_generic")
        || normalized.includes("company,contact,title,email")
        || normalized.includes("email_generic,phone,website");
    }
    function csvDataRowCount(csvText) {
      return Math.max(0, String(csvText || "").trim().split(/\r?\n/).filter(Boolean).length - 1);
    }
    function likelySourceRowCount(rawText) {
      const text = String(rawText || "");
      const numbered = text.match(/^\s*\d+\s+.+@.+$/gm);
      if (numbered && numbered.length) return numbered.length;
      const readableRows = text.match(/^\|\s*[^|\n]+\s*\|\s*[^|\n]+\s*\|/gm);
      if (readableRows && readableRows.length > 2) return readableRows.length - 2;
      return 0;
    }
    async function openImportPreviewFromText(rawText) {
      const csvText = extractCsvFromGemini(rawText);
      if (!csvText) return friendlyAlert("Nothing To Import", "Paste Gemini output or CSV first.");
      if (!looksLikeImportCsv(csvText)) {
        openModal(`<div class="dialog-head"><h1>Import CSV Missing</h1><button class="icon" onclick="closeModal()">×</button></div>
          <div class="fit-box">Gemini gave a readable review, but not the app import CSV block. Ask Gemini: "Now provide the IMPORT_CSV fenced csv block using the exact headers from the prompt."</div>
          <textarea class="prompt-box">${esc(String(rawText || "").slice(0, 5000))}</textarea>
          <div class="quick"><button id="backToGeminiPaste">Back to paste</button><button onclick="closeModal()">Close</button></div>`);
        $("backToGeminiPaste").onclick = openGeminiPasteModal;
        return;
      }
      const buffer = new TextEncoder().encode(csvText);
      const preview = await api("/api/import-preview", {method:"POST", body: buffer});
      const c = preview.counts;
      const csvRows = csvDataRowCount(csvText);
      const sourceRows = likelySourceRowCount(rawText);
      const partialWarning = sourceRows && csvRows < sourceRows
        ? `<div class="event" style="border-color:#f0d08a;background:#fff7df"><b>Possible partial Gemini output</b>Extracted ${csvRows} import rows, but the pasted response appears to mention about ${sourceRows} rows. Ask Gemini to provide IMPORT_CSV for every input account.</div>`
        : "";
      openModal(`<div class="dialog-head"><h1>Import Preview</h1><button class="icon" onclick="closeModal()">×</button></div>
        <div class="fit-box">The app extracted ${csvText.split(/\r?\n/).length} CSV lines from the pasted Gemini output. Weak rows can import, but they will be blocked from drafting until fixed.</div>
        ${partialWarning}
        <div class="grid">
          <div class="event"><b>Total rows</b>${c.rows}</div>
          <div class="event"><b>Looks ready</b>${c.ready}</div>
          <div class="event"><b>Missing direct email</b>${c.missing_direct_email}</div>
          <div class="event"><b>Duplicates</b>${c.duplicates}</div>
          <div class="event"><b>Suppressed</b>${c.suppressed}</div>
          <div class="event"><b>Weak Gemini research</b>${c.weak_research || 0}</div>
          <div class="event"><b>Malformed rows</b>${c.malformed || 0}</div>
          <div class="event"><b>Missing company</b>${c.missing_company}</div>
        </div>
        <div class="timeline">${(preview.samples || []).map(s => `<div class="event"><b>${esc(s.company)}</b>${esc(s.email || "")}<br><span class="small">${esc(s.issues.join(", "))}</span></div>`).join("") || `<div class="small">No obvious issues found.</div>`}</div>
        <div class="quick"><button class="primary" id="confirmPastedImport">Import extracted rows</button><button id="backToGeminiPaste">Back to paste</button></div>`);
      $("confirmPastedImport").onclick = async () => {
        const data = await api("/api/import", {method:"POST", body: buffer.slice(0)});
        closeModal();
        await friendlyAlert("Import Complete", `Created ${data.counts.created}, duplicates ${data.counts.duplicate}, suppressed ${data.counts.suppressed}, skipped ${data.counts.skipped}.`);
        await refreshLeads();
      };
      $("backToGeminiPaste").onclick = openGeminiPasteModal;
    }
    function openGeminiPasteModal() {
      openModal(`<div class="dialog-head"><h1>Paste Gemini Output</h1><button class="icon" onclick="closeModal()">×</button></div>
        <div class="fit-box">Paste the whole Gemini answer here. It can include a readable table plus a CSV block, or just raw CSV like the screenshot. The app will extract the import rows.</div>
        <textarea id="geminiPasteText" class="prompt-box" placeholder="Paste Gemini output here"></textarea>
        <div class="quick"><button class="primary" id="previewGeminiPaste">Preview import</button><button onclick="closeModal()">Close</button></div>`);
      $("previewGeminiPaste").onclick = () => openImportPreviewFromText($("geminiPasteText").value);
    }
    const yesNo = (v) => v ? "Ready" : "Needs attention";

    async function openHealthModal() {
      const data = await api("/api/health");
      const h = data.health;
      openModal(`<div class="dialog-head"><h1>Health Check</h1><button class="icon" onclick="closeModal()">×</button></div>
        <div class="grid">
          <div class="event"><b>Gmail</b>${esc(yesNo(h.gmail_configured))}<br><span class="small">${esc(h.gmail_email || "No Gmail configured")}</span></div>
          <div class="event"><b>Leads</b>${h.leads} imported · ${h.ready_preview_count} ready in today's batch</div>
          <div class="event"><b>Backups</b>${h.backups} backups<br><span class="small">${esc(h.last_backup || "No backup yet")}</span></div>
          <div class="event"><b>Gemini prompts</b>${esc(yesNo(h.gemini_prompt))}<br><span class="small">Use normal Gemini for cleanup; use Deep Research for source-backed account research.</span></div>
        </div>
        <div class="quick">
          <button class="primary" id="testGmailBtn">Test Gmail draft</button>
          <button id="backupNowBtn">Backup now</button>
          <button id="restoreBtn">Restore backup</button>
          <button id="troubleshootBtn">Troubleshooting</button>
        </div>
        <div id="healthResult" class="small"></div>`);
      $("testGmailBtn").onclick = async () => {
        $("healthResult").textContent = "Testing Gmail...";
        const res = await api("/api/test-gmail", {method:"POST"});
        $("healthResult").textContent = res.message || "Test draft created.";
      };
      $("backupNowBtn").onclick = async () => {
        const res = await api("/api/backup-now", {method:"POST"});
        $("healthResult").textContent = `Backup created: ${res.backup}`;
      };
      $("restoreBtn").onclick = () => openRestoreModal(data.backups || []);
      $("troubleshootBtn").onclick = openTroubleshootingModal;
    }

    async function openSetupModal() {
      const data = await api("/api/health");
      const h = data.health;
      openModal(`<div class="dialog-head"><h1>First-Time Setup</h1><button class="icon" onclick="closeModal()">×</button></div>
        <div class="event"><b>1. Gmail settings</b>${esc(yesNo(h.gmail_configured))}<br><span class="small">Add Gmail address, sender name, signature, and Google App Password.</span></div>
        <div class="event"><b>2. Test Gmail</b>Create one harmless test draft to confirm Gmail is connected.</div>
        <div class="event"><b>3. Import leads</b>Use an Odoo CSV, normal Gemini cleanup, or Gemini Deep Research for stronger source-backed lead context.</div>
        <div class="event"><b>4. Create today's drafts</b>Click Emails for today, review warnings, then Draft to Gmail.</div>
        <div class="event"><b>5. Update Odoo</b>Export Odoo update CSV after drafting.</div>
        <div class="quick">
          <button id="setupSettings">Open settings</button>
          <button id="setupTest" class="primary">Test Gmail draft</button>
          <button id="setupTemplates">CSV templates</button>
          <button id="setupImport">Import CSV</button>
        </div>
        <div id="setupResult" class="small"></div>`);
      $("setupSettings").onclick = () => $("settingsBtn").click();
      $("setupTemplates").onclick = () => $("templatesBtn").click();
      $("setupImport").onclick = () => $("csvFile").click();
      $("setupTest").onclick = async () => {
        $("setupResult").textContent = "Testing Gmail...";
        const res = await api("/api/test-gmail", {method:"POST"});
        $("setupResult").textContent = res.message || "Test draft created.";
      };
    }

    function openRestoreModal(backups) {
      openModal(`<div class="dialog-head"><h1>Restore Backup</h1><button class="icon" onclick="closeModal()">×</button></div>
        <div class="fit-box">Restoring first creates a fresh backup of the current database, then replaces it with the selected backup.</div>
        <div class="timeline">${backups.map(b => `<div class="event"><b>${esc(b.name)}</b>${esc(b.modified)}<br><button data-restore-backup="${esc(b.name)}">Restore this backup</button></div>`).join("") || `<div class="small">No backups found.</div>`}</div>`);
      document.querySelectorAll("[data-restore-backup]").forEach(btn => btn.onclick = async () => {
        const ok = await friendlyConfirm({
          title: "Restore Backup",
          body: `Restore ${btn.dataset.restoreBackup}? The app will make a fresh backup first, then reload.`,
          confirmText: "Restore"
        });
        if (!ok) return;
        await api("/api/restore-backup", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name:btn.dataset.restoreBackup})});
        await friendlyAlert("Backup Restored", "The app will reload now.");
        location.reload();
      });
    }

    function openTroubleshootingModal() {
      openModal(`<div class="dialog-head"><h1>Troubleshooting</h1><button class="icon" onclick="closeModal()">×</button></div>
        <div class="event"><b>Gmail drafts do not appear</b>Use a Google App Password, not the normal Gmail password. Make sure 2-Step Verification is on.</div>
        <div class="event"><b>No leads ready</b>Check for direct emails, suppressed companies, risky email warnings, low personalization score, or leads already drafted today.</div>
        <div class="event"><b>CSV import failed</b>Download a CSV template, keep the headers, or paste Gemini's full answer into Paste Gemini Output so the app can extract the CSV block.</div>
        <div class="event"><b>App closes</b>Keep the launcher/terminal window open while using the app.</div>
        <div class="event"><b>Wrong data imported</b>Use Health → Restore backup. The app backs up on startup and before manual restore.</div>`);
    }

    function openVersionGuideModal() {
      const rows = ["17", "18", "19", "19.4"].map(version => {
        const item = state.odooReleases[version] || {};
        const general = (item.general || []).slice(0, 5).map(x => `<li>${esc(x)}</li>`).join("");
        return `<div class="event">
          <b>${esc(item.label || "Odoo " + version)} · ${esc(item.released || "")}</b>
          <ul>${general}</ul>
          <div class="small">Recovery emails auto-match from the evaluated version/year to the newer releases most relevant to the lead's workflow.</div>
        </div>`;
      }).join("");
      openModal(`<div class="dialog-head"><h1>Odoo Version Guide</h1><button class="icon" onclick="closeModal()">×</button></div>
        <div class="fit-box">Use <b>Odoo evaluation date</b> when possible. The app maps the date to the likely Odoo version available then. If date is blank, it falls back to year, then manual version.</div>
        <div class="timeline">${rows}</div>`);
    }

    $("closeDrawer").onclick = () => $("drawer").classList.remove("open");
    $("setupBtn").onclick = openSetupModal;
    $("setupBannerBtn").onclick = openSetupModal;
    $("healthBtn").onclick = openHealthModal;
    $("versionGuideBtn").onclick = openVersionGuideModal;
    $("templatesBtn").onclick = () => {
      openModal(`<div class="dialog-head"><h1>CSV Templates</h1><button class="icon" onclick="closeModal()">×</button></div>
        <div class="fit-box">Use these when someone is unsure what columns to provide. Gemini-enriched is best for higher-quality emails.</div>
        <div class="quick"><button onclick="location.href='/api/template/odoo'">Download basic Odoo template</button><button class="primary" onclick="location.href='/api/template/gemini'">Download Gemini-enriched template</button></div>`);
    };
    $("modeBtn").onclick = () => {
      const next = document.body.classList.toggle("simple");
      localStorage.setItem("simpleMode", next ? "1" : "0");
      $("modeBtn").textContent = next ? "Advanced mode" : "Simple mode";
    };
    $("modeBtn").textContent = document.body.classList.contains("simple") ? "Advanced mode" : "Simple mode";
    $("newLead").onclick = newLead;
    $("activeLeadsBtn").onclick = async () => {
      state.leadScope = "active";
      state.metric = "";
      state.activeCategory = "";
      $("filterLostCategory").value = "";
      if ($("sortBy").value === "lost_ts") $("sortBy").value = "priority";
      await refreshLeads();
    };
    $("lostLeadsBtn").onclick = async () => {
      state.leadScope = "lost";
      state.metric = "";
      state.activeCategory = "";
      state.view = "list";
      $("sortBy").value = "lost_ts";
      await refreshLeads();
    };
    $("toggleView").onclick = () => { state.view = state.view === "board" ? "list" : "board"; render(); };
    $("clearFilters").onclick = () => { state.metric = ""; state.activeCategory = ""; ["filterQ","filterPriority","filterState","filterCity","filterIndustry","filterSource","filterLostCategory"].forEach(id => $(id).value = ""); refreshLeads(); };
    ["filterQ","filterPriority","filterState","filterCity","filterIndustry","filterSource","filterLostCategory","sortBy"].forEach(id => $(id).addEventListener("change", refreshLeads));
    $("filterQ").addEventListener("input", () => { clearTimeout(window.qTimer); window.qTimer = setTimeout(refreshLeads, 180); });
    $("exportBtn").onclick = () => location.href = "/api/export?" + filters().toString();
    $("odooExportBtn").onclick = () => location.href = "/api/odoo-updates?" + filters().toString();
    $("howToBtn").onclick = () => {
      openModal(`<div class="dialog-head"><h1>How To Use</h1><button class="icon" onclick="closeModal()">×</button></div>
        <div class="event">
          <b>Fast daily workflow</b>
          <ol>
            <li>Import an Odoo or Gemini-enriched CSV.</li>
            <li>Click <b>Emails for today</b>.</li>
            <li>Review the preview. Rows with warnings will be skipped.</li>
            <li>Click <b>Draft to Gmail</b>.</li>
            <li>Open Gmail Drafts, review, and send.</li>
            <li>Use <b>Odoo update CSV</b> to bring notes/status back into Odoo.</li>
          </ol>
        </div>
        <div class="event">
          <b>Best research workflow with Gemini Deep Research</b>
          <ol>
            <li>Export about 25 accounts from Odoo.</li>
            <li>Click <b>Gemini research</b>, choose <b>Deep Research</b>, and copy the prompt.</li>
            <li>Open Gemini Deep Research.</li>
            <li>Paste the prompt and the account list into Deep Research.</li>
            <li>Have Gemini return the readable review plus import CSV block for every row.</li>
            <li>Paste Gemini's full answer back into <b>Paste Gemini Output</b>.</li>
            <li>Click <b>Emails for today</b> and draft to Gmail.</li>
          </ol>
        </div>
        <div class="event">
          <b>When to use normal Gemini</b>
          <p>Inside Gemini research, use Quick cleanup when Odoo already has decent context. Use Deep Research when the leads need real account research and source-backed personalization.</p>
        </div>
        <div class="event">
          <b>What the app does automatically</b>
          <p>It auto-tailors first lines, reply angles, role lens, claim safety, customer language, CTAs, sequence emails, and QA warnings before any Gmail drafts are created.</p>
        </div>
        <div class="event">
          <b>When a row gets skipped</b>
          <p>Rows can be skipped for missing required fields, risky email, suppression, low personalization score, weak proof, unsafe claims, or already drafted today.</p>
        </div>`);
    };
    async function openGeminiPromptModal(initialMode="deep") {
      const modes = {
        deep: {
          path: "/api/gemini-deep-research-prompt",
          label: "Deep Research",
          title: "Gemini Research",
          description: "Use Deep Research for real account research. It requires source-backed evidence, richer workflow diagnosis, and an import CSV block for every row."
        },
        quick: {
          path: "/api/gemini-prompt",
          label: "Quick cleanup",
          title: "Gemini Research",
          description: "Use Quick cleanup when Odoo already has decent context and you mainly need formatting, cautious enrichment, or CSV cleanup."
        }
      };
      let mode = modes[initialMode] ? initialMode : "deep";
      openModal(`<div class="dialog-head"><h1>Gemini Research</h1><button class="icon" onclick="closeModal()">×</button></div>
        <div class="tabs"><button id="geminiModeDeep">Deep Research</button><button id="geminiModeQuick">Quick cleanup</button></div>
        <div class="fit-box" id="geminiModeHelp"></div>
        <textarea id="geminiPromptText" class="prompt-box"></textarea>
        <div class="quick"><button class="primary" id="copyGeminiPrompt">Copy prompt</button><button id="pasteGeminiOutput">Paste Gemini output</button><button onclick="closeModal()">Close</button></div>`);
      async function loadGeminiMode(nextMode) {
        mode = nextMode;
        const config = modes[mode];
        $("geminiModeHelp").textContent = config.description;
        $("geminiModeDeep").classList.toggle("active", mode === "deep");
        $("geminiModeQuick").classList.toggle("active", mode === "quick");
        $("geminiPromptText").value = "Loading prompt...";
        const res = await fetch(config.path);
        $("geminiPromptText").value = await res.text();
        $("copyGeminiPrompt").textContent = "Copy prompt";
      }
      $("geminiModeDeep").onclick = () => loadGeminiMode("deep");
      $("geminiModeQuick").onclick = () => loadGeminiMode("quick");
      $("copyGeminiPrompt").onclick = async () => {
        await navigator.clipboard.writeText($("geminiPromptText").value);
        $("copyGeminiPrompt").textContent = "Copied";
      };
      $("pasteGeminiOutput").onclick = openGeminiPasteModal;
      await loadGeminiMode(mode);
    }
    $("geminiPromptBtn").onclick = async () => openGeminiPromptModal("deep");
    $("weeklyBtn").onclick = () => location.href = "/api/weekly-coaching";
    $("playbookBtn").onclick = () => {
      openModal(`<div class="dialog-head"><h1>Playbook</h1><button class="icon" onclick="closeModal()">×</button></div>
        <div class="fit-box">Export/import templates, quality gates, and sequence presets for colleagues. Gmail credentials are never included.</div>
        <div class="quick"><button onclick="location.href='/api/playbook'">Export playbook</button><button id="pbImport">Import playbook</button></div>
        <div class="timeline">${parsePresets().map(p => `<div class="event"><b>${esc(p.name)}</b>${esc(p.angle || "")} · ${esc(p.cta_style || "")}<br><span class="small">${(p.templates || []).length} emails</span></div>`).join("")}</div>`);
      $("pbImport").onclick = () => $("playbookFile").click();
    };
    $("importBtn").onclick = () => $("csvFile").click();
    $("csvFile").onchange = async () => {
      const file = $("csvFile").files[0]; if (!file) return;
      const buffer = await file.arrayBuffer();
      const preview = await api("/api/import-preview", {method:"POST", body: buffer.slice(0)});
      const c = preview.counts;
      openModal(`<div class="dialog-head"><h1>Import Preview</h1><button class="icon" onclick="closeModal()">×</button></div>
        <div class="grid">
          <div class="event"><b>Total rows</b>${c.rows}</div>
          <div class="event"><b>Looks ready</b>${c.ready}</div>
          <div class="event"><b>Missing direct email</b>${c.missing_direct_email}</div>
          <div class="event"><b>Duplicates</b>${c.duplicates}</div>
          <div class="event"><b>Suppressed</b>${c.suppressed}</div>
          <div class="event"><b>Weak Gemini research</b>${c.weak_research || 0}</div>
          <div class="event"><b>Malformed rows</b>${c.malformed || 0}</div>
          <div class="event"><b>Missing company</b>${c.missing_company}</div>
        </div>
        <div class="timeline">${(preview.samples || []).map(s => `<div class="event"><b>${esc(s.company)}</b>${esc(s.email || "")}<br><span class="small">${esc(s.issues.join(", "))}</span></div>`).join("") || `<div class="small">No obvious issues found.</div>`}</div>
        <button class="primary" id="confirmImport">Import rows</button>`);
      $("confirmImport").onclick = async () => {
        const data = await api("/api/import", {method:"POST", body: buffer.slice(0)});
        closeModal();
        await friendlyAlert("Import Complete", `Created ${data.counts.created}, duplicates ${data.counts.duplicate}, suppressed ${data.counts.suppressed}, skipped ${data.counts.skipped}.`);
        await refreshLeads();
      };
      $("csvFile").value = "";
    };
    $("playbookFile").onchange = async () => {
      const file = $("playbookFile").files[0]; if (!file) return;
      const text = await file.text();
      const data = JSON.parse(text);
      const res = await api("/api/playbook", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(data)});
      state.settings = res.settings;
      $("playbookFile").value = "";
      closeModal();
      await friendlyAlert("Playbook Imported", "Your saved email settings and playbook are now loaded.");
      await load();
    };
    $("settingsBtn").onclick = () => {
      const s = state.settings;
      const templates = parseTemplates();
      openModal(`<div class="dialog-head"><h1>Settings</h1><button class="icon" onclick="closeModal()">×</button></div>
        <div class="grid">
          <label><span>Gmail address</span><input id="setEmail" value="${esc(s.gmail_email || "")}"></label>
          <label><span>Sender name</span><input id="setSenderName" value="${esc(s.sender_name || "Rayhan")}"></label>
          <label><span>Gmail App Password</span><input id="setPass" type="password" placeholder="${s.gmail_app_password ? "Saved" : "16-character password"}"></label>
          <label><span>Daily target</span><input id="setTarget" type="number" value="${esc(s.daily_target || 20)}"></label>
          <label><span>Planned email sequence</span><input id="setPlanCount" type="number" min="1" max="30" value="${esc(s.email_plan_count || 6)}"></label>
          <label><span>Minimum personalization score</span><input id="setMinScore" type="number" min="0" max="100" value="${esc(s.min_personalization_score || 60)}"></label>
          <label><span>Require contact name</span><select id="setReqContact"><option value="1" ${s.require_contact_name !== "0" ? "selected" : ""}>Yes</option><option value="0" ${s.require_contact_name === "0" ? "selected" : ""}>No</option></select></label>
          <label><span>Require industry</span><select id="setReqIndustry"><option value="1" ${s.require_industry !== "0" ? "selected" : ""}>Yes</option><option value="0" ${s.require_industry === "0" ? "selected" : ""}>No</option></select></label>
          <label><span>Require first line</span><select id="setReqFirstLine"><option value="0" ${s.require_custom_first_line !== "1" ? "selected" : ""}>No</option><option value="1" ${s.require_custom_first_line === "1" ? "selected" : ""}>Yes</option></select></label>
          <label><span>Require software stack</span><select id="setReqSoftware"><option value="0" ${s.require_software_stack !== "1" ? "selected" : ""}>No</option><option value="1" ${s.require_software_stack === "1" ? "selected" : ""}>Yes</option></select></label>
          <label class="full"><span>HTML signature</span><textarea id="setSig">${esc(s.email_signature || "")}</textarea></label>
        </div>
        <div class="fit-box">Quality gates control which leads can be drafted in batch. Leads that fail gates still appear in Draft Preview with warnings.</div>
        <div class="quick"><button id="testGmailSettings">Test Gmail draft</button><button id="exportPlaybook">Export playbook</button><button id="importPlaybook">Import playbook</button></div>
        <div id="settingsResult" class="small"></div>
        <div class="fit-box">Merge fields: {sender_name}, {first_name}, {contact}, {company}, {city}, {city_fallback}, {state}, {industry}, {sub_industry}, {industry_fallback}, {software_used}, {software_renewal_date}, {odoo_evaluation_date}, {odoo_evaluation_version}, {odoo_evaluation_year}, {likely_software}, {odoo_modules}, {competitor}, {competitor_angle}, {odoo_win}, {price_angle}, {renewal_angle}, {renewal_line}, {title}, {website}, {source}, {notes}, {company_summary}, {why_now}, {workflow_hypothesis}, {research_evidence}, {research_source_url}, {confidence}, {do_not_claim}, {reason_to_believe}, {role_lens}, {claim_safety}, {customer_language}, {reply_type_goal}, {pain_points}, {industry_holes}, {value_angle}, {proof_points}, {sequence_angle}, {custom_first_line}, {reply_goal}, {cta_style}, {reply_cta}, {lost_category}, {lost_reason}, {lost_ts}, {personalization_score}, {email_stage}, {email_plan_count}</div>
        <div id="templateRows"></div>
        <button class="primary" id="saveSettings">Save settings</button>`);
      function defaultTemplate(n) {
        return {
          subject: n === 1 ? "A quick idea for {company}" : `Following up with {company} - email ${n}`,
          body: `<p>Hi {first_name},</p><p>I wanted to follow up with a quick idea for {company}. For a ${"{industry_fallback}"} team in ${"{city_fallback}"}, the goal is usually cleaner follow-up and fewer missed opportunities.</p><p>Worth a quick conversation?</p>`
        };
      }
      function drawTemplateRows() {
        const count = Math.max(1, Math.min(30, Number($("setPlanCount").value || 6)));
        const rows = Array.from({length: count}, (_, i) => {
          const n = i + 1;
          const existing = templates[i] || defaultTemplate(n);
          return `<div class="event">
            <b>Email ${n}</b>
            <label><span>Subject</span><input data-template-subject="${i}" value="${esc(existing.subject || "")}"></label>
            <label><span>HTML body</span><textarea data-template-body="${i}">${esc(existing.body || "")}</textarea></label>
          </div>`;
        });
        $("templateRows").innerHTML = rows.join("");
      }
      $("setPlanCount").addEventListener("change", drawTemplateRows);
      drawTemplateRows();
      $("saveSettings").onclick = async () => {
        const count = Math.max(1, Math.min(30, Number($("setPlanCount").value || 6)));
        const nextTemplates = Array.from({length: count}, (_, i) => ({
          subject: document.querySelector(`[data-template-subject="${i}"]`)?.value || defaultTemplate(i + 1).subject,
          body: document.querySelector(`[data-template-body="${i}"]`)?.value || defaultTemplate(i + 1).body,
        }));
        const payload = {
          gmail_email:$("setEmail").value,
          sender_name:$("setSenderName").value,
          email_signature:$("setSig").value,
          daily_target:$("setTarget").value,
          min_personalization_score:$("setMinScore").value,
          require_contact_name:$("setReqContact").value,
          require_industry:$("setReqIndustry").value,
          require_custom_first_line:$("setReqFirstLine").value,
          require_software_stack:$("setReqSoftware").value,
          email_plan_count:String(count),
          email_templates:JSON.stringify(nextTemplates),
          email_subject:nextTemplates[0]?.subject || "",
          email_body:nextTemplates[0]?.body || ""
        };
        if ($("setPass").value) payload.gmail_app_password = $("setPass").value;
        const data = await api("/api/settings", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
        state.settings = data.settings; state.daily.target = Number(payload.daily_target || 20); closeModal(); await refreshLeads();
      };
      $("exportPlaybook").onclick = () => location.href = "/api/playbook";
      $("importPlaybook").onclick = () => $("playbookFile").click();
      $("testGmailSettings").onclick = async () => {
        $("settingsResult").textContent = "Testing Gmail...";
        const res = await api("/api/test-gmail", {method:"POST"});
        $("settingsResult").textContent = res.message || "Test draft created.";
      };
    };
    $("draftBatch").onclick = async () => openDraftPreview();
    async function openDraftPreview() {
      const data = await api(`/api/draft-preview?limit=${encodeURIComponent($("batchLimit").value || 20)}`);
      const ready = data.leads.filter(l => l.ready).length;
      openModal(`<div class="dialog-head"><h1>Emails For Today</h1><button class="icon" onclick="closeModal()">×</button></div>
        <div class="fit-box"><b>${ready}</b> tailored and ready · <b>${data.leads.length - ready}</b> need cleanup. The app auto-prepares first lines, angles, CTAs, and lead-specific sequences before this screen opens.</div>
        <div class="timeline">${data.leads.map(l => `<div class="event">
          <b>${esc(l.company)} · Email ${l.email_step} · ${l.score}%</b>
          <div class="small">${esc(l.email)} ${l.sequence_angle ? " · " + esc(l.sequence_angle) : ""}</div>
          <div><b>Subject:</b> ${esc(l.subject)}</div>
          <div class="small">${esc(l.body_preview)}</div>
          ${l.warnings.length ? `<div class="small" style="color:#9f2633">Warnings: ${esc(l.warnings.join("; "))}</div>` : `<span class="pill done">Ready</span>`}
        </div>`).join("") || `<div class="small">No eligible leads found.</div>`}</div>
        <button class="primary" id="confirmDrafts" ${ready ? "" : "disabled"}>Draft ${ready} to Gmail</button>`);
      $("confirmDrafts").onclick = async () => {
        const ok = await friendlyConfirm({
          title: "Draft To Gmail",
          body: `Create ${ready} real Gmail draft${ready === 1 ? "" : "s"} now?`,
          confirmText: "Draft to Gmail"
        });
        if (!ok) return;
        const res = await api("/api/draft-batch", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({limit:$("batchLimit").value})});
        closeModal();
        await friendlyAlert("Gmail Drafts Created", `Drafted ${res.drafted}. Skipped ${res.skipped}.${res.errors.length ? " " + res.errors.join(" ") : ""}`);
        await load();
      };
    }
    $("emailLead").onclick = async () => {
      if (state.current.stage === "Lost") return draftLostRecovery(state.current.id);
      await api(`/api/leads/${state.current.id}/draft`, {method:"POST"});
      await openLead(state.current.id);
      await load();
    };
    $("buildLeadEmails").onclick = async () => {
      const next = {...state.current};
      document.querySelectorAll("[data-field]").forEach(el => next[el.dataset.field] = el.value);
      await saveLead(next.id, next, true);
      const res = await api(`/api/leads/${state.current.id}/generate-emails`, {method:"POST"});
      state.current = res.lead;
      state.timeline = res.timeline;
      await refreshLeads();
      renderDrawer();
    };
    $("engagedLead").onclick = async () => {
      if (state.current.stage === "Lost") return reopenLead(state.current.id);
      await api(`/api/leads/${state.current.id}/engaged`, {method:"POST"});
      await openLead(state.current.id);
      await refreshLeads();
    };
    $("lostLead").onclick = async () => {
      await markLeadLost(state.current.id);
    };
    $("optOutLead").onclick = async () => {
      const values = await friendlyForm({
        title: "Suppress Lead",
        body: "This keeps the person out of future imports and draft batches.",
        confirmText: "Suppress",
        danger: true,
        fields: [{id:"reason", label:"Reason", type:"textarea", value:"Asked to unsubscribe", required:true}]
      });
      if (!values) return;
      const reason = values.reason;
      await api(`/api/leads/${state.current.id}/suppress`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({reason})});
      $("drawer").classList.remove("open"); await refreshLeads();
    };
    $("addNote").onclick = async () => {
      if (!$("noteText").value.trim()) return;
      await api(`/api/leads/${state.current.id}/note`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({text:$("noteText").value})});
      $("noteText").value = ""; await openLead(state.current.id);
    };
    $("addActivity").onclick = async () => {
      await api(`/api/leads/${state.current.id}/activity`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({type:$("actType").value, due:$("actDue").value, note:$("actNote").value})});
      $("actNote").value = ""; await openLead(state.current.id); await refreshLeads();
    };
    $("agendaBtn").onclick = async () => {
      const [agenda, today, call] = await Promise.all(["agenda","today","call"].map(v => api(`/api/activities?view=${v}`)));
      openModal(`<div class="dialog-head"><h1>Activities</h1><button class="icon" onclick="closeModal()">×</button></div>
        <div class="tabs"><button class="active" data-tab="agenda">Agenda</button><button data-tab="today">Today</button><button data-tab="call">Call Queue</button></div>
        <div id="activityRows"></div>`);
      const sets = {agenda: agenda.activities, today: today.activities, call: call.activities};
      function draw(tab) {
        $("activityRows").innerHTML = (sets[tab] || []).map(a => `<div class="event"><b>${esc(a.due)} · ${esc(a.type)} · ${esc(a.company)}</b>${esc(a.note || "")}<br><button data-done="${a.id}">Done</button></div>`).join("") || `<div class="small">Nothing due.</div>`;
        document.querySelectorAll("[data-done]").forEach(b => b.onclick = async () => { await api(`/api/activities/${b.dataset.done}/done`, {method:"POST"}); closeModal(); await refreshLeads(); });
      }
      draw("agenda");
      document.querySelectorAll("[data-tab]").forEach(b => b.onclick = () => { document.querySelectorAll("[data-tab]").forEach(x => x.classList.remove("active")); b.classList.add("active"); draw(b.dataset.tab); });
    };
    $("suppressionBtn").onclick = async () => {
      const data = await api("/api/suppressed");
      openModal(`<div class="dialog-head"><h1>Suppression List</h1><button class="icon" onclick="closeModal()">×</button></div>
        <input id="supSearch" placeholder="Search suppressed leads">
        <div id="supRows"></div>`);
      function draw() {
        const q = $("supSearch").value.toLowerCase();
        $("supRows").innerHTML = data.suppressed.filter(s => `${s.company} ${s.email} ${s.reason}`.toLowerCase().includes(q)).map(s => `<div class="event"><b>${esc(s.company)} · ${esc(s.email || "")}</b>${esc(s.reason)}<br><span class="small">${esc(s.ts)}</span><br><button data-restore="${s.id}">Restore</button></div>`).join("") || `<div class="small">No suppressed leads.</div>`;
        document.querySelectorAll("[data-restore]").forEach(b => b.onclick = async () => { await api(`/api/suppressed/${b.dataset.restore}/restore`, {method:"POST"}); closeModal(); });
      }
      $("supSearch").oninput = draw; draw();
    };
    $("modal").addEventListener("click", e => {
      if (e.target.id !== "modal") return;
      if (modalBackdropAction) modalBackdropAction();
      else closeModal();
    });
    window.closeModal = closeModal;
    load().catch(err => friendlyAlert("App Could Not Load", err.message || "Something went wrong while loading the CRM."));
  </script>
</body>
</html>
"""


def main():
    backup_db()
    init_db()
    httpd = ThreadingHTTPServer((HOST, PORT), CRMHandler)
    print(f"{APP_NAME} running at http://{HOST}:{PORT}")
    print(f"Database: {os.path.abspath(DB_PATH)}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
