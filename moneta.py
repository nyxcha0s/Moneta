#!/usr/bin/env python3
"""
MONETA - Financial survival AI for Dragons-Landing
Port 5001 | Flask + Claude Haiku + SQLite
"""

import os
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, render_template, session
import anthropic

app = Flask(__name__)
app.secret_key = os.environ.get("MONETA_SECRET", "moneta-sto-plains-lair-2026")

DB_PATH = os.path.expanduser("~/moneta/moneta.db")
SCHEMA_PATH = os.path.expanduser("~/moneta/schema.sql")
PACIFIC = ZoneInfo("America/Los_Angeles")

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# ── DB ──────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with open(SCHEMA_PATH) as f:
        schema = f.read()
    conn = get_db()
    conn.executescript(schema)
    conn.commit()
    conn.close()

def clean_duplicates():
    """Remove duplicate bills and subscriptions — keeps lowest id."""
    conn = get_db()
    conn.execute("""
        DELETE FROM bills WHERE id NOT IN (
            SELECT MIN(id) FROM bills GROUP BY name, due_day
        )
    """)
    conn.execute("""
        DELETE FROM subscriptions WHERE id NOT IN (
            SELECT MIN(id) FROM subscriptions GROUP BY name
        )
    """)
    conn.commit()
    conn.close()

# ── TIME ─────────────────────────────────────────────────────────────────────

def get_current_time():
    now = datetime.now(PACIFIC)
    return {
        "datetime": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "day_of_week": now.strftime("%A"),
        "timestamp": now.timestamp()
    }

# ── FINANCIAL BRAIN ──────────────────────────────────────────────────────────

def get_next_payday(from_date=None):
    """Calculate next payday Wednesday from a given date."""
    conn = get_db()
    cfg = dict(conn.execute("SELECT key, value FROM config").fetchall())
    conn.close()
    
    next_pd = cfg.get("next_payday", "2026-04-22")
    next_date = date.fromisoformat(next_pd)
    today = from_date or date.today()
    
    # Walk forward by 14-day intervals until we find next future payday
    while next_date <= today:
        next_date += timedelta(days=14)
    
    return next_date.isoformat()

def get_days_until_payday():
    today = date.today()
    next_pd = date.fromisoformat(get_next_payday())
    return (next_pd - today).days

def get_current_balance():
    conn = get_db()
    row = conn.execute(
        "SELECT amount, snapshot_date FROM balance_snapshots ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row:
        return {"amount": row["amount"], "date": row["snapshot_date"]}
    return {"amount": None, "date": None}

def get_known_hits_before_payday():
    """All confirmed financial hits between now and next payday."""
    today = date.today().isoformat()
    next_pd = get_next_payday()
    conn = get_db()
    
    hits = []
    
    # Autopay bills due before payday (HIGH RISK)
    bills = conn.execute("""
        SELECT name, amount_current, amount_high, due_day, autopay, late_fee, priority
        FROM bills WHERE active=1
    """).fetchall()
    
    today_day = date.today().day
    next_pd_day = date.fromisoformat(next_pd).day
    current_month = date.today().month
    
    for b in bills:
        due = b["due_day"]
        if due is None:
            continue
        amount = b["amount_high"] or b["amount_current"] or 0
        # Is this bill due between today and next payday?
        if today_day <= due <= next_pd_day or (next_pd_day < today_day and due >= today_day):
            hits.append({
                "name": b["name"],
                "amount": amount,
                "due_day": due,
                "autopay": bool(b["autopay"]),
                "priority": b["priority"],
                "type": "bill",
                "risk": "HIGH" if b["autopay"] else "NORMAL"
            })
    
    # Subscriptions confirmed/suspected
    subs = conn.execute("""
        SELECT name, amount, billing_day, status
        FROM subscriptions WHERE status IN ('confirmed','suspected')
    """).fetchall()
    
    for s in subs:
        if s["billing_day"] and today_day <= s["billing_day"] <= int(next_pd_day):
            hits.append({
                "name": s["name"],
                "amount": s["amount"] or 0,
                "due_day": s["billing_day"],
                "autopay": True,
                "priority": 6,
                "type": "subscription",
                "status": s["status"],
                "risk": "SUSPECTED" if s["status"] == "suspected" else "NORMAL"
            })
    
    # Flex membership if unpaid
    conn2 = get_db()
    flex_unpaid = conn2.execute("""
        SELECT flex_membership, flex_membership_paid, flex_payment2, flex_payment2_paid,
               flex_payment2_date, month
        FROM rent WHERE month = ?
    """, (date.today().strftime("%Y-%m"),)).fetchone()
    conn2.close()
    
    if flex_unpaid:
        if not flex_unpaid["flex_membership_paid"]:
            hits.append({
                "name": "Flex membership",
                "amount": flex_unpaid["flex_membership"] or 6.99,
                "autopay": True,
                "priority": 1,
                "type": "flex",
                "risk": "CRITICAL — Flex suspension = full rent exposure"
            })
        if not flex_unpaid["flex_payment2_paid"] and flex_unpaid["flex_payment2_date"]:
            p2_date = flex_unpaid["flex_payment2_date"]
            if today <= p2_date <= next_pd:
                hits.append({
                    "name": "Flex Payment 2 (rent)",
                    "amount": (flex_unpaid["flex_payment2"] or 303),
                    "due_day": date.fromisoformat(p2_date).day,
                    "autopay": True,
                    "priority": 1,
                    "type": "flex",
                    "risk": "HIGH"
                })
    
    conn.close()
    return hits

def calculate_safe_to_spend():
    """The core number: what can actually be spent right now."""
    balance = get_current_balance()
    if balance["amount"] is None:
        return None
    
    hits = get_known_hits_before_payday()
    total_committed = sum(h["amount"] for h in hits)
    
    # Always keep a $20 buffer minimum
    safe = balance["amount"] - total_committed - 20
    return max(0, safe)

def get_financial_snapshot():
    """Full picture for the AI context."""
    now = get_current_time()
    balance = get_current_balance()
    safe = calculate_safe_to_spend()
    hits = get_known_hits_before_payday()
    days_to_pay = get_days_until_payday()
    next_pd = get_next_payday()
    
    conn = get_db()
    
    # Recent expenses
    recent_expenses = conn.execute("""
        SELECT description, amount, expense_date FROM expenses
        ORDER BY expense_date DESC LIMIT 10
    """).fetchall()
    
    # Current month rent status
    rent = conn.execute("""
        SELECT * FROM rent WHERE month = ?
    """, (date.today().strftime("%Y-%m"),)).fetchone()
    
    # ALL subscriptions
    all_subs = conn.execute("""
        SELECT name, amount, billing_day, status FROM subscriptions
        ORDER BY status, billing_day
    """).fetchall()

    # ALL bills
    all_bills = conn.execute("""
        SELECT name, amount_current, amount_high, amount_type, due_day,
               autopay, priority, grace_period_days, late_fee, active
        FROM bills WHERE active=1 ORDER BY priority ASC, due_day ASC
    """).fetchall()

    # Drift
    drift = conn.execute("""
        SELECT drift, logged_date FROM drift_log
        ORDER BY created_at DESC LIMIT 1
    """).fetchone()
    
    conn.close()
    
    return {
        "current_datetime": now["datetime"],
        "current_date": now["date"],
        "day_of_week": now["day_of_week"],
        "days_until_payday": days_to_pay,
        "next_payday": next_pd,
        "next_payday_time": "~7:00 PM Pacific",
        "balance": balance,
        "safe_to_spend": safe,
        "committed_hits_before_payday": hits,
        "total_committed": sum(h["amount"] for h in hits),
        "rent_this_month": dict(rent) if rent else None,
        "all_subscriptions": [dict(s) for s in all_subs],
        "all_bills": [dict(b) for b in all_bills],
        "recent_expenses": [dict(e) for e in recent_expenses],
        "drift": dict(drift) if drift else None,
        "flex_membership_outstanding": any(
            h["name"] == "Flex membership" for h in hits
        ),
        "payment_plan": get_payment_plan()
    }

def get_payment_plan():
    """
    THE AUTHORITATIVE PAYMENT PLAN.
    Built entirely in code from the DB. The AI displays this, never guesses it.
    Every bill and sub is assigned to the correct payday. Nothing is omitted.
    """
    today = date.today()
    conn = get_db()

    # Get all payday dates in order
    cfg = dict(conn.execute("SELECT key, value FROM config").fetchall())
    base_pd = date.fromisoformat(cfg.get("next_payday", "2026-04-22"))
    
    # Walk base_pd back if needed to find the anchor
    while base_pd > today:
        base_pd -= timedelta(days=14)
    while base_pd <= today:
        base_pd += timedelta(days=14)
    # base_pd is now the next payday
    
    payday1 = base_pd                          # next payday
    payday2 = base_pd + timedelta(days=14)     # one after that
    payday3 = base_pd + timedelta(days=28)     # two after that

    # Determine period types - alternating rent/bill starting from config
    # April 22 = rent, May 6 = bill, May 20 = rent, June 3 = bill
    # Anchor: April 22 2026 is a rent payday
    anchor_rent = date(2026, 4, 22)
    def period_type(pd):
        diff = (pd - anchor_rent).days
        periods = diff // 14
        return "RENT" if periods % 2 == 0 else "BILL"

    pd1_type = period_type(payday1)
    pd2_type = period_type(payday2)

    # Get all bills (deduplicated by name - use lowest id)
    bills = conn.execute("""
        SELECT name, amount_current, amount_high, amount_type, due_day,
               autopay, priority, grace_period_days, late_fee, notes
        FROM bills WHERE active=1 AND id IN (
            SELECT MIN(id) FROM bills WHERE active=1 GROUP BY name
        )
        ORDER BY priority ASC, due_day ASC
    """).fetchall()

    # Get all subscriptions (deduplicated)
    subs = conn.execute("""
        SELECT name, amount, billing_day, status
        FROM subscriptions WHERE status != 'cancelled' AND id IN (
            SELECT MIN(id) FROM subscriptions GROUP BY name
        )
        ORDER BY billing_day ASC NULLS LAST
    """).fetchall()

    # Get rent for this month and next
    this_month = today.strftime("%Y-%m")
    next_month = (today.replace(day=1) + timedelta(days=32)).strftime("%Y-%m")
    
    rent_this = conn.execute("SELECT * FROM rent WHERE month=?", (this_month,)).fetchone()
    rent_next = conn.execute("SELECT * FROM rent WHERE month=?", (next_month,)).fetchone()
    conn.close()

    def assign_bill_to_payday(due_day, pd1, pd1_type, pd2, pd2_type):
        """
        Assign a bill to the correct payday based on due date.
        A bill is paid from the FIRST payday that comes BEFORE or ON its due date,
        giving us time to pay it. If due after pd1 but before pd2, goes to pd1 BILL period
        or pd2 if pd1 is RENT period.
        """
        if due_day is None:
            # No due date - goes to next BILL payday
            if pd1_type == "BILL":
                return 1
            return 2
        
        # Build actual due dates for this month and next
        try:
            due_this = date(today.year, today.month, due_day)
        except ValueError:
            due_this = date(today.year, today.month, 28)
        
        next_m = (today.replace(day=1) + timedelta(days=32))
        try:
            due_next = date(next_m.year, next_m.month, due_day)
        except ValueError:
            due_next = date(next_m.year, next_m.month, 28)

        # Which due date is relevant (past this month -> use next month)
        due = due_this if due_this >= today else due_next

        # Assignment rule:
        # A bill is paid from the BILL payday whose period CONTAINS the due date.
        # A pay period runs from payday day+1 through the next payday day.
        # If due date falls BEFORE pd1 it is urgent -> pd1 regardless.
        # If due date falls BETWEEN pd1 and pd2 -> pay from whichever of pd1/pd2 is BILL type.
        # If due date falls AFTER pd2 -> pay from pd2 (and flag if needed later).
        if due <= pd1:
            return 1  # past due or due before next payday - urgent
        elif due <= pd2:
            # In the window between pd1 and pd2
            # Pay from the BILL payday in this window
            if pd1_type == "BILL":
                return 1
            else:
                return 2  # pd2 is BILL
        else:
            # Due after pd2 - pay from pd2 for now
            return 2

    # Build the two payment buckets
    bucket = {1: [], 2: []}

    # RENT PAYDAY always gets Flex Payment 1 (the big one)
    rent_pd = payday1 if pd1_type == "RENT" else payday2
    rent_bucket = 1 if pd1_type == "RENT" else 2
    
    # Estimate next Flex Payment 1
    flex_p1_estimate = 2404.32  # worst case from history
    if rent_this and rent_this["flex_payment1"]:
        flex_p1_estimate = rent_this["flex_payment1"]
    
    bucket[rent_bucket].append({
        "name": "Flex Payment 1 (RENT — biggest hit)",
        "amount": flex_p1_estimate,
        "type": "RENT",
        "priority": 1,
        "note": "Estimate based on history. Actual amount confirmed near end of month.",
        "autopay": True
    })

    # Flex membership always goes to rent payday
    flex_mem_paid = rent_this and rent_this["flex_membership_paid"]
    if not flex_mem_paid:
        bucket[rent_bucket].append({
            "name": "Flex membership",
            "amount": 6.99,
            "type": "RENT",
            "priority": 1,
            "note": "CRITICAL — lapse = full rent exposure",
            "autopay": True
        })

    # Flex Payment 2 always goes to bill payday
    bill_bucket = 2 if pd1_type == "RENT" else 1
    flex_p2_paid = rent_this and rent_this["flex_payment2_paid"]
    if not flex_p2_paid:
        bucket[bill_bucket].append({
            "name": "Flex Payment 2",
            "amount": 303.00,
            "type": "BILL",
            "priority": 1,
            "note": "$300 + $3 fee back to Flex",
            "autopay": False
        })

    # Assign all bills
    for b in bills:
        amount = b["amount_high"] or b["amount_current"] or 0
        which = assign_bill_to_payday(b["due_day"], payday1, pd1_type, payday2, pd2_type)
        bucket[which].append({
            "name": b["name"],
            "amount": amount,
            "due_day": b["due_day"],
            "type": "BILL",
            "priority": b["priority"],
            "autopay": bool(b["autopay"]),
            "note": b["notes"] or ""
        })

    # Assign all subscriptions
    for s in subs:
        amount = s["amount"] or 0
        which = assign_bill_to_payday(s["billing_day"], payday1, pd1_type, payday2, pd2_type)
        bucket[which].append({
            "name": s["name"],
            "amount": amount,
            "due_day": s["billing_day"],
            "type": "SUB",
            "status": s["status"],
            "priority": 6,
            "autopay": True,
            "note": "suspected — date TBD" if s["status"] == "suspected" else ""
        })

    total1 = sum(i["amount"] for i in bucket[1])
    total2 = sum(i["amount"] for i in bucket[2])
    assumed_deposit = float(cfg.get("income_range_low", 2300))

    return {
        "payday_1": {
            "date": payday1.isoformat(),
            "type": pd1_type,
            "assumed_deposit": assumed_deposit,
            "items": sorted(bucket[1], key=lambda x: x["priority"]),
            "total": round(total1, 2),
            "remaining_for_food_and_living": round(assumed_deposit - total1, 2)
        },
        "payday_2": {
            "date": payday2.isoformat(),
            "type": pd2_type,
            "assumed_deposit": assumed_deposit,
            "items": sorted(bucket[2], key=lambda x: x["priority"]),
            "total": round(total2, 2),
            "remaining_for_food_and_living": round(assumed_deposit - total2, 2)
        }
    }

# ── TOOLS ─────────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_current_time",
        "description": "Get the current date and time in Pacific timezone. ALWAYS call this first.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_financial_snapshot",
        "description": "Get the complete current financial picture: balance, safe-to-spend, upcoming hits, rent status, drift.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "update_balance",
        "description": "Record a new balance snapshot when the user tells you their current balance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Current account balance in dollars"},
                "notes": {"type": "string", "description": "Optional context"}
            },
            "required": ["amount"]
        }
    },
    {
        "name": "add_bill",
        "description": "Add a bill to track. Use amount_high for variable bills to budget conservatively.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "amount_type": {"type": "string", "enum": ["fixed", "variable", "variable_seasonal"]},
                "amount_current": {"type": "number"},
                "amount_high": {"type": "number"},
                "due_day": {"type": "integer"},
                "autopay": {"type": "boolean"},
                "priority": {"type": "integer", "description": "1=critical, 2=utilities, 3=car, 4=food, 5=debt, 6=optional"},
                "grace_period_days": {"type": "integer"},
                "late_fee": {"type": "number"},
                "due_date_moveable": {"type": "boolean"},
                "creditor_phone": {"type": "string"},
                "notes": {"type": "string"}
            },
            "required": ["name", "amount_type"]
        }
    },
    {
        "name": "update_bill_amount",
        "description": "Update a bill's amount when it changes (variable bills like power, gas, water).",
        "input_schema": {
            "type": "object",
            "properties": {
                "bill_name": {"type": "string"},
                "new_amount": {"type": "number"},
                "notes": {"type": "string"}
            },
            "required": ["bill_name", "new_amount"]
        }
    },
    {
        "name": "add_subscription",
        "description": "Add a subscription. Use status='suspected' if user isn't sure it still exists.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "amount": {"type": "number"},
                "billing_day": {"type": "integer"},
                "status": {"type": "string", "enum": ["confirmed", "suspected", "bounced", "cancelled"]}
            },
            "required": ["name", "status"]
        }
    },
    {
        "name": "update_subscription_status",
        "description": "Update subscription status when user finds out it bounced, was cancelled, or is confirmed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "status": {"type": "string", "enum": ["confirmed", "suspected", "bounced", "cancelled"]},
                "notes": {"type": "string"}
            },
            "required": ["name", "status"]
        }
    },
    {
        "name": "record_payday",
        "description": "Record that payday hit with the actual amount received.",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "period_type": {"type": "string", "enum": ["rent", "bill"]},
                "notes": {"type": "string"}
            },
            "required": ["amount", "period_type"]
        }
    },
    {
        "name": "pre_purchase_check",
        "description": "Check if a purchase is safe to make right now. Returns approved/denied with reasoning.",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "amount": {"type": "number"}
            },
            "required": ["description", "amount"]
        }
    },
    {
        "name": "log_expense",
        "description": "Record that an expense was made.",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "amount": {"type": "number"},
                "category": {"type": "string", "enum": ["food", "transport", "medical", "other"]},
                "pre_approved": {"type": "boolean"}
            },
            "required": ["description", "amount"]
        }
    },
    {
        "name": "update_rent",
        "description": "Update rent information for current or upcoming month. Use when landlord confirms amount.",
        "input_schema": {
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "YYYY-MM format"},
                "base_amount": {"type": "number"},
                "amount_confirmed": {"type": "boolean"},
                "flex_payment1": {"type": "number"},
                "flex_payment1_paid": {"type": "boolean"},
                "flex_payment2_date": {"type": "string"},
                "flex_payment2_paid": {"type": "boolean"},
                "flex_membership_paid": {"type": "boolean"},
                "mystery_charges": {"type": "number"},
                "notes": {"type": "string"}
            },
            "required": ["month"]
        }
    },
    {
        "name": "run_triage",
        "description": "When there isn't enough money to cover everything, calculate priority payment order and what to defer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "available_amount": {"type": "number"}
            },
            "required": ["available_amount"]
        }
    },
    {
        "name": "get_payment_plan",
        "description": "Get the complete authoritative payment plan — every bill and subscription assigned to the correct payday by the code. ALWAYS call this when user asks what they are paying when, or wants a payment plan. Never guess — always use this tool.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_drift",
        "description": "Calculate how far ahead or behind user is compared to same point last pay period.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    }
]

def execute_tool(tool_name, tool_input):
    conn = get_db()
    now = get_current_time()
    today = now["date"]

    try:
        if tool_name == "get_current_time":
            return now

        elif tool_name == "get_financial_snapshot":
            return get_financial_snapshot()

        elif tool_name == "update_balance":
            conn.execute(
                "INSERT INTO balance_snapshots (amount, snapshot_date, notes) VALUES (?, ?, ?)",
                (tool_input["amount"], today, tool_input.get("notes", ""))
            )
            conn.commit()
            # Log drift
            safe = calculate_safe_to_spend()
            return {
                "recorded": True,
                "balance": tool_input["amount"],
                "safe_to_spend": safe,
                "date": today
            }

        elif tool_name == "add_bill":
            conn.execute("""
                INSERT INTO bills (name, amount_type, amount_current, amount_high,
                    due_day, autopay, priority, grace_period_days, late_fee,
                    due_date_moveable, creditor_phone, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tool_input["name"],
                tool_input.get("amount_type", "variable"),
                tool_input.get("amount_current"),
                tool_input.get("amount_high"),
                tool_input.get("due_day"),
                1 if tool_input.get("autopay") else 0,
                tool_input.get("priority", 5),
                tool_input.get("grace_period_days", 0),
                tool_input.get("late_fee", 0),
                1 if tool_input.get("due_date_moveable") else 0,
                tool_input.get("creditor_phone", ""),
                tool_input.get("notes", "")
            ))
            conn.commit()
            return {"added": True, "bill": tool_input["name"]}

        elif tool_name == "update_bill_amount":
            # Save old as amount_last, update current
            old = conn.execute(
                "SELECT amount_current, amount_high FROM bills WHERE name LIKE ?",
                (f"%{tool_input['bill_name']}%",)
            ).fetchone()
            if old:
                new_high = max(old["amount_high"] or 0, tool_input["new_amount"])
                conn.execute("""
                    UPDATE bills SET amount_last=amount_current,
                    amount_current=?, amount_high=? WHERE name LIKE ?
                """, (tool_input["new_amount"], new_high, f"%{tool_input['bill_name']}%"))
                conn.commit()
            return {"updated": True, "bill": tool_input["bill_name"], "new_amount": tool_input["new_amount"]}

        elif tool_name == "add_subscription":
            conn.execute("""
                INSERT INTO subscriptions (name, amount, billing_day, status)
                VALUES (?, ?, ?, ?)
            """, (
                tool_input["name"],
                tool_input.get("amount"),
                tool_input.get("billing_day"),
                tool_input.get("status", "suspected")
            ))
            conn.commit()
            return {"added": True, "subscription": tool_input["name"], "status": tool_input.get("status")}

        elif tool_name == "update_subscription_status":
            conn.execute("""
                UPDATE subscriptions SET status=?,
                last_bounce_date=CASE WHEN ? = 'bounced' THEN ? ELSE last_bounce_date END,
                last_confirmed_date=CASE WHEN ? = 'confirmed' THEN ? ELSE last_confirmed_date END
                WHERE name LIKE ?
            """, (
                tool_input["status"],
                tool_input["status"], today,
                tool_input["status"], today,
                f"%{tool_input['name']}%"
            ))
            conn.commit()
            return {"updated": True, "subscription": tool_input["name"], "status": tool_input["status"]}

        elif tool_name == "record_payday":
            conn.execute("""
                INSERT INTO paydays (expected_date, actual_date, amount, period_type, notes)
                VALUES (?, ?, ?, ?, ?)
            """, (
                get_next_payday(),
                today,
                tool_input["amount"],
                tool_input["period_type"],
                tool_input.get("notes", "")
            ))
            # Update next_payday config
            current_next = date.fromisoformat(get_next_payday())
            new_next = current_next + timedelta(days=14)
            conn.execute(
                "UPDATE config SET value=?, updated_at=? WHERE key='next_payday'",
                (new_next.isoformat(), today)
            )
            conn.commit()
            return {
                "recorded": True,
                "amount": tool_input["amount"],
                "period_type": tool_input["period_type"],
                "next_payday_now": new_next.isoformat()
            }

        elif tool_name == "pre_purchase_check":
            snap = get_financial_snapshot()
            safe = snap.get("safe_to_spend") or 0
            amount = tool_input["amount"]
            balance = snap["balance"]["amount"] or 0
            days = snap["days_until_payday"]

            if safe <= 0:
                decision = "denied"
                reasoning = f"Safe-to-spend is ${safe:.2f} after accounting for all known hits before payday ({days} days away). Do not spend."
            elif amount > safe:
                decision = "denied"
                reasoning = f"This ${amount:.2f} purchase would exceed safe-to-spend of ${safe:.2f}. You have ${balance:.2f} but ${snap['total_committed']:.2f} is committed to bills/subs before payday."
            elif amount > safe * 0.5 and days > 5:
                decision = "approved_with_warning"
                reasoning = f"Approved but this is ${amount:.2f} of your ${safe:.2f} safe-to-spend with {days} days until payday. Leaves only ${safe-amount:.2f} buffer."
            else:
                decision = "approved"
                reasoning = f"Safe. ${safe-amount:.2f} remains after this purchase with {days} days until payday."

            conn.execute("""
                INSERT INTO purchase_checks
                (description, amount, check_date, decision, balance_at_check,
                runway_days_at_check, safe_amount_at_check, reasoning)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tool_input["description"], amount, today,
                decision, balance, days, safe, reasoning
            ))
            conn.commit()
            return {"decision": decision, "reasoning": reasoning, "safe_to_spend": safe}

        elif tool_name == "log_expense":
            conn.execute("""
                INSERT INTO expenses (description, amount, expense_date, category, pre_approved)
                VALUES (?, ?, ?, ?, ?)
            """, (
                tool_input["description"],
                tool_input["amount"],
                today,
                tool_input.get("category", "other"),
                1 if tool_input.get("pre_approved") else 0
            ))
            conn.commit()
            return {"logged": True}

        elif tool_name == "update_rent":
            month = tool_input["month"]
            existing = conn.execute("SELECT id FROM rent WHERE month=?", (month,)).fetchone()
            
            if existing:
                updates = []
                vals = []
                for field in ["base_amount", "flex_payment1", "flex_payment1_date",
                              "flex_payment2_date", "mystery_charges", "notes"]:
                    if field in tool_input:
                        updates.append(f"{field}=?")
                        vals.append(tool_input[field])
                for bool_field in ["amount_confirmed", "flex_payment1_paid",
                                   "flex_payment2_paid", "flex_membership_paid"]:
                    if bool_field in tool_input:
                        updates.append(f"{bool_field}=?")
                        vals.append(1 if tool_input[bool_field] else 0)
                if updates:
                    vals.append(month)
                    conn.execute(f"UPDATE rent SET {', '.join(updates)} WHERE month=?", vals)
            else:
                # Calculate flex_payment1 if base_amount provided
                base = tool_input.get("base_amount")
                flex_p2 = 303  # default $300 + $3 fee
                flex_p1 = None
                if base:
                    flex_p1 = round((base - 300) * 1.01, 2)
                conn.execute("""
                    INSERT INTO rent (month, base_amount, amount_confirmed,
                    flex_payment1, flex_payment2, flex_membership)
                    VALUES (?, ?, ?, ?, ?, 6.99)
                """, (month, base, 1 if tool_input.get("amount_confirmed") else 0,
                      flex_p1, flex_p2))
            conn.commit()
            return {"updated": True, "month": month}

        elif tool_name == "run_triage":
            available = tool_input["available_amount"]
            conn2 = get_db()
            bills = conn2.execute(
                "SELECT name, amount_high, amount_current, priority, late_fee, grace_period_days FROM bills WHERE active=1 ORDER BY priority ASC"
            ).fetchall()
            conn2.close()

            plan = []
            remaining = available
            deferred = []

            for b in bills:
                amount = b["amount_high"] or b["amount_current"] or 0
                if remaining >= amount:
                    plan.append({"bill": b["name"], "action": "PAY", "amount": amount})
                    remaining -= amount
                else:
                    grace = b["grace_period_days"] or 0
                    late_fee = b["late_fee"] or 0
                    deferred.append({
                        "bill": b["name"],
                        "amount": amount,
                        "action": "DEFER",
                        "grace_days": grace,
                        "late_fee": late_fee,
                        "risk": "HIGH" if grace == 0 else "MODERATE"
                    })

            triage_plan = json.dumps({"pay": plan, "defer": deferred})
            conn.execute("""
                INSERT INTO triage_events (event_date, shortfall, total_due, available, triage_plan)
                VALUES (?, ?, ?, ?, ?)
            """, (
                today,
                sum(b["amount_high"] or b["amount_current"] or 0 for b in bills) - available,
                sum(b["amount_high"] or b["amount_current"] or 0 for b in bills),
                available,
                triage_plan
            ))
            conn.commit()
            return {"pay": plan, "defer": deferred, "remaining_after_pay": remaining}

        elif tool_name == "get_payment_plan":
            return get_payment_plan()

        elif tool_name == "get_drift":
            # Compare today's balance to same day-offset last period
            snap = get_financial_snapshot()
            balance = snap["balance"]["amount"]
            if not balance:
                return {"drift": None, "reason": "No balance recorded"}
            
            # Find last period's balance at same day offset
            # Get last payday record
            last_pay = conn.execute(
                "SELECT actual_date, amount FROM paydays ORDER BY created_at DESC LIMIT 2"
            ).fetchall()
            
            if len(last_pay) < 2:
                return {"drift": None, "reason": "Need at least 2 pay periods to calculate drift"}
            
            current_pd = date.fromisoformat(last_pay[0]["actual_date"])
            prev_pd = date.fromisoformat(last_pay[1]["actual_date"])
            today_d = date.today()
            day_offset = (today_d - current_pd).days
            
            # Find balance snapshot near same offset in prev period
            prev_target = prev_pd + timedelta(days=day_offset)
            prev_balance_row = conn.execute("""
                SELECT amount FROM balance_snapshots
                WHERE snapshot_date <= ?
                ORDER BY snapshot_date DESC LIMIT 1
            """, (prev_target.isoformat(),)).fetchone()
            
            if not prev_balance_row:
                return {"drift": None, "reason": "No historical balance data for comparison"}
            
            drift = balance - prev_balance_row["amount"]
            
            conn.execute("""
                INSERT INTO drift_log (period_start, period_end, day_offset, balance,
                prev_period_balance, drift, logged_date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                current_pd.isoformat(),
                get_next_payday(),
                day_offset,
                balance,
                prev_balance_row["amount"],
                drift,
                today
            ))
            conn.commit()
            
            return {
                "drift": drift,
                "current_balance": balance,
                "prev_period_balance": prev_balance_row["amount"],
                "day_offset": day_offset,
                "direction": "AHEAD" if drift > 0 else "BEHIND"
            }

    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()

# ── SYSTEM PROMPT ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Moneta — a financial survival AI. Named for the Roman goddess of money, keeper of the mint.

Your job is not to judge. Your job is to do the math and tell the truth.

## WHO YOU SERVE
Someone living paycheck to paycheck who is actively trying to get out. They get paid every other Wednesday around 7pm Pacific. Their rent is handled through Flex. Income varies — default assumption is $2,300 per deposit until told the actual amount.

## THE TWO PAYDAY STRUCTURE — THIS IS CRITICAL, GET IT RIGHT EVERY TIME
Paydays alternate every 14 days between two types. ALWAYS check today's date and calculate which payday is which before showing any payment plan.

RENT PAYDAY — how it actually works:
The deposit lands Wednesday ~7pm. The RENT PAYDAY deposit must fund Flex Payment 1 which Flex charges around the 1st of the following month. This is the single biggest hit of the entire month and it comes FROM this deposit.

RENT PAYDAY covers:
- Flex Payment 1 (~$2,125 currently, budgeted to $2,404.32 worst case) — COMES FROM THIS DEPOSIT
- Flex membership $6.99 (CRITICAL — must not lapse)
- Past-due utilities in shutoff danger (e.g. PGE $80.68 currently)
- Food and bare survival for the 14 days until bill payday
- NOTHING ELSE

BILL PAYDAY covers EVERYTHING ELSE:
- ALL regular bills: AT&T, PGE current month, SMUD, and any others
- ALL subscriptions: Claude, ChatGPT, Gemini, Apple One, Apple Creator Studio, YouMail, Wondery+, Discord, Whatbox
- Flex Payment 2 ($303)
- Santander and any other debt payments
- Food and living expenses for the period

CURRENT CALENDAR (as of April 2026):
- April 22 = RENT payday: Flex Payment 1 (~$2,125) + Flex membership $6.99 + PGE $80.68 + food
- May 6 = BILL payday: ALL bills, ALL subs, Flex Payment 2 $303, Santander $345.31
- May 20 = RENT payday
- June 3 = BILL payday

NEVER assign bills to April 6 — already passed.
NEVER forget Flex Payment 1 on rent payday — it is always the biggest item.

## YOUR CORE RULES
1. ALWAYS call get_current_time first in every conversation turn. Dates are not optional.
2. ALWAYS call get_financial_snapshot when discussing money, balance, what to pay, or whether to spend.
3. Every piece of financial data gets written to the database. Memory is not enough. If it happened, it's recorded.
4. Budget to the HIGH estimate for variable bills — always. Never the average, never the low.
5. The safe-to-spend number is not the balance. It is balance minus all known/suspected hits before payday minus a $20 buffer. This is the only number that matters for spending decisions.
6. Flex membership ($6.99) is CRITICAL priority. If it lapses, the rent split fails, and full rent hits the account. Flag this loudly.
7. Autopay bills near payday Wednesday are HIGH RISK — Chase can deny them before the 7pm deposit lands.
8. The $20 buffer is sacred. Never approve a purchase that eats the buffer.

## TRIAGE PRIORITY (when there isn't enough)
1. Flex membership (protects rent split — losing this is catastrophic)
2. Flex Payment 1 (actual rent — eviction risk)  
3. Flex Payment 2 ($300 portion)
4. Utilities (shutoff has reconnection fees and deposits)
5. Car payment if needed for work
6. Food
7. Minimum debt payments
8. Everything else — subscriptions, optional, defer or cancel

## THE DRIFT NUMBER
Track how far ahead or behind the user is compared to the same point last pay period. Show this whenever you have enough data. A positive drift means the exit ramp is working. Celebrate it — not loudly, just factually. "You're $47 further ahead than this day last period."

## VOICE
- Direct. No fluff.
- Never lecture. Never moralize. Never say "great job" or "I'm proud of you."
- State the math. State the risk. State the recommendation.
- When it's tight: be honest but not hopeless.
- When something is dangerous: say it's dangerous and exactly why.
- When a purchase is fine: say it's fine and move on.
- Short answers for simple checks. Fuller answers for triage situations.

## RENT SITUATION
- Rent is variable — landlord can add charges near month end, user doesn't know until late
- After the 25th of each month, rent for next month is UNKNOWN until confirmed
- Flex pays full rent on the 1st; user pays Flex back in two parts
- Payment 1: bulk of rent + 1% fee, hits around the 1st
- Payment 2: $300 + $3 fee, user-chosen date aligned to payday
- Membership: $6.99/month

## SUBSCRIPTIONS
Many subscriptions may be forgotten. When user mentions a charge that bounced or they just discovered, add it with appropriate status. Never shame. Just record and incorporate into the math.

## WHAT YOU ARE BUILDING TOWARD
Every pay period that ends with more money than the last one is a win. The goal is the day they check their account mid-week before payday and there's still something there. That day will come if they ask before they spend. Your job is to make that day happen faster."""

# ── CHAT ENGINE ───────────────────────────────────────────────────────────────

def run_moneta(user_message, session_id):
    conn = get_db()
    
    # Save user message
    conn.execute(
        "INSERT INTO conversations (role, content, session_id) VALUES (?, ?, ?)",
        ("user", user_message, session_id)
    )
    conn.commit()
    
    # Load recent history
    history = conn.execute("""
        SELECT role, content FROM conversations
        WHERE session_id = ?
        ORDER BY created_at ASC
    """, (session_id,)).fetchall()
    conn.close()
    
    messages = [{"role": r["role"], "content": r["content"]} for r in history]
    
    # Agentic loop
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )
        
        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if hasattr(b, "text")), "")
            conn2 = get_db()
            conn2.execute(
                "INSERT INTO conversations (role, content, session_id) VALUES (?, ?, ?)",
                ("assistant", text, session_id)
            )
            conn2.commit()
            conn2.close()
            return text
        
        if response.stop_reason == "tool_use":
            # Add assistant message with tool calls
            messages.append({"role": "assistant", "content": response.content})
            
            # Execute all tool calls
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result)
                    })
            
            messages.append({"role": "user", "content": tool_results})
            continue
        
        break
    
    return "Something went wrong with the response."

# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
    return render_template("index.html")

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "Empty message"}), 400
    
    session_id = session.get("session_id", str(uuid.uuid4()))
    response = run_moneta(message, session_id)
    return jsonify({"response": response})

@app.route("/snapshot")
def snapshot():
    return jsonify(get_financial_snapshot())

@app.route("/new-session", methods=["POST"])
def new_session():
    session["session_id"] = str(uuid.uuid4())
    return jsonify({"ok": True})

if __name__ == "__main__":
    init_db()
    clean_duplicates()
    app.run(host="0.0.0.0", port=5001, debug=False)
