import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "availability.db"
TIME_STEP_MINUTES = 30

app = Flask(__name__)
app.secret_key = "change-this-to-something-random-and-secret"


def init_db():
    """Create the availability table if it does not exist."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS availability (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            name TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            preferred_event TEXT,
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def insert_slots(date_str, name, slots, preferred_event, note):
    """Insert one row per time slot for a given person and date."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for start_str, end_str in slots:
        c.execute(
            """
            INSERT INTO availability (date, name, start_time, end_time, preferred_event, note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (date_str, name, start_str, end_str, preferred_event, note),
        )
    conn.commit()
    conn.close()


def get_day_records(date_str):
    """Return all availability rows for a given date."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        SELECT name, start_time, end_time, preferred_event, note
        FROM availability
        WHERE date = ?
        ORDER BY name, start_time
        """,
        (date_str,),
    )
    rows = c.fetchall()
    conn.close()

    records = []
    for name, start_time, end_time, preferred_event, note in rows:
        records.append(
            {
                "name": name,
                "start_time": start_time,
                "end_time": end_time,
                "preferred_event": preferred_event or "",
                "note": note or "",
            }
        )
    return records


def get_available_dates():
    """Return all distinct dates that have availability entries."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        SELECT DISTINCT date
        FROM availability
        ORDER BY date
        """
    )
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def compute_suggestion(date_str, records):
    """Compute best overlapping time and majority event for a given date."""
    if not records:
        return None

    # Group slots and events by person
    by_person = {}
    for r in records:
        name = r["name"]
        if name not in by_person:
            by_person[name] = {"slots": [], "events": []}
        by_person[name]["slots"].append((r["start_time"], r["end_time"]))
        if r["preferred_event"]:
            by_person[name]["events"].append(r["preferred_event"].strip())

    if not by_person:
        return None

    # Collect boundaries for the search window
    all_times = []
    for info in by_person.values():
        for s, e in info["slots"]:
            all_times.append(s)
            all_times.append(e)

    if not all_times:
        return None

    all_times_sorted = sorted(all_times)
    start_of_day = datetime.strptime(f"{date_str} {all_times_sorted[0]}", "%Y-%m-%d %H:%M")
    end_of_day = datetime.strptime(f"{date_str} {all_times_sorted[-1]}", "%Y-%m-%d %H:%M")

    if start_of_day >= end_of_day:
        return None

    # Build timeline in fixed steps
    step = timedelta(minutes=TIME_STEP_MINUTES)
    timeline = []
    current = start_of_day
    while current + step <= end_of_day:
        timeline.append(current)
        current += step

    if not timeline:
        return None

    # Count how many people are free in each step
    free_counts = []
    for t in timeline:
        t_end = t + step
        count = 0
        for info in by_person.values():
            for s_str, e_str in info["slots"]:
                s = datetime.strptime(f"{date_str} {s_str}", "%Y-%m-%d %H:%M")
                e = datetime.strptime(f"{date_str} {e_str}", "%Y-%m-%d %H:%M")
                if s <= t and e >= t_end:
                    count += 1
                    break
        free_counts.append(count)

    max_free = max(free_counts)
    if max_free <= 0:
        return None

    # Build continuous blocks where free count is max_free
    best_blocks = []
    i = 0
    while i < len(timeline):
        if free_counts[i] == max_free:
            start_block = timeline[i]
            while i + 1 < len(timeline) and free_counts[i + 1] == max_free:
                i += 1
            end_block = timeline[i] + step
            best_blocks.append((start_block, end_block))
        i += 1

    if not best_blocks:
        return None

    # Pick the longest block
    best_blocks.sort(key=lambda b: (b[1] - b[0]), reverse=True)
    best_start, best_end = best_blocks[0]

    # Event votes (case-insensitive grouping)
    votes = {}
    for info in by_person.values():
        for ev in info["events"]:
            label = ev.strip()
            if not label:
                continue
            key = label.lower()
            votes[key] = votes.get(key, 0) + 1

    if votes:
        # Get the most voted normalized key
        top_key = sorted(votes.items(), key=lambda kv: kv[1], reverse=True)[0][0]
        # Recover a pretty version: first matching original label
        pretty = None
        for info in by_person.values():
            for ev in info["events"]:
                if ev.strip().lower() == top_key:
                    pretty = ev.strip()
                    break
            if pretty:
                break
        preferred_event = pretty or top_key
    else:
        preferred_event = "No clear winner"

    return {
        "max_free": max_free,
        "total_people": len(by_person),
        "start": best_start.strftime("%H:%M"),
        "end": best_end.strftime("%H:%M"),
        "event": preferred_event,
    }


@app.route("/", methods=["GET"])
def index():
    dates = get_available_dates()
    selected_date = request.query_string.decode().split("date=", 1)[1] if "date=" in request.query_string.decode() else ""
    if not selected_date and dates:
        selected_date = dates[0]

    records = get_day_records(selected_date) if selected_date else []
    suggestion = compute_suggestion(selected_date, records) if selected_date and records else None

    return render_template(
        "index.html",
        dates=dates,
        selected_date=selected_date,
        records=records,
        suggestion=suggestion,
    )


@app.route("/submit", methods=["POST"])
def submit():
    name = request.form.get("name", "").strip()
    date_str = request.form.get("date", "").strip()

    # Base event type from dropdown (required)
    preferred_event = request.form.get("preferred_event", "").strip()

    # If "Other event" selected, and custom text provided, override with custom
    preferred_event_custom = request.form.get("preferred_event_custom", "").strip()
    if preferred_event == "Other event" and preferred_event_custom:
        preferred_event = preferred_event_custom

    preferred_location = request.form.get("preferred_location", "").strip()
    preferred_event_detail = request.form.get("preferred_event_detail", "").strip()
    preferred_time_hint = request.form.get("preferred_time_hint", "").strip()
    base_note = request.form.get("note", "").strip()

    starts = request.form.getlist("slot_start")
    ends = request.form.getlist("slot_end")

    # Validation
    if not name or not date_str:
        flash("Name and date are required.")
        return redirect(url_for("index"))

    if not preferred_event:
        flash("Please choose a preferred event type.")
        return redirect(url_for("index", date=date_str))

    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        flash("Invalid date format.")
        return redirect(url_for("index"))

    slots = []
    for s, e in zip(starts, ends):
        s = s.strip()
        e = e.strip()
        if not s and not e:
            continue
        if not s or not e:
            flash("Each time range needs both start and end.")
            return redirect(url_for("index", date=date_str))
        try:
            st = datetime.strptime(s, "%H:%M").time()
            et = datetime.strptime(e, "%H:%M").time()
        except ValueError:
            flash("Time must be in HH:MM format.")
            return redirect(url_for("index", date=date_str))
        if et <= st:
            flash("End time must be after start time.")
            return redirect(url_for("index", date=date_str))
        slots.append((s, e))

    if not slots:
        flash("Add at least one free time range.")
        return redirect(url_for("index", date=date_str))

    # Build combined note
    note_parts = []
    if base_note:
        note_parts.append(base_note)
    if preferred_location:
        note_parts.append(f"Preferred location: {preferred_location}")
    if preferred_event_detail:
        note_parts.append(f"Specific event: {preferred_event_detail}")
    if preferred_time_hint:
        note_parts.append(f"Preferred time: {preferred_time_hint}")

    combined_note = " | ".join(note_parts) if note_parts else ""

    insert_slots(date_str, name, slots, preferred_event, combined_note)

    flash("Availability saved.")
    return redirect(url_for("index", date=date_str))


# Ensure DB exists both locally and on Render import
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
