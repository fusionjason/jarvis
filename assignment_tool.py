import json
import sys
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent / "data" / "assignments.json"


def load():
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def save(data):
    DATA_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def find_matches(data, text):
    text = text.lower()
    return [a for a in data if text in a["title"].lower()]


def cmd_find(text):
    data = load()
    matches = find_matches(data, text)
    if not matches:
        print("no matches found")
        return
    for a in matches:
        print(f"{a['id']} | {a['status']} | {a['class']} | {a['title']} | due {a.get('due_date','')}")


def cmd_set_status(text, status):
    data = load()
    matches = find_matches(data, text)
    if not matches:
        print("no matches found")
        return
    if len(matches) > 1:
        print("multiple matches, be more specific:")
        for a in matches:
            print(f"{a['id']} | {a['class']} | {a['title']}")
        return
    target_id = matches[0]["id"]
    for a in data:
        if a["id"] == target_id:
            a["status"] = status
            print(f"updated: {a}")
    save(data)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: assignment_tool.py <find|done|undone> \"<title text>\"")
        sys.exit(1)

    action = sys.argv[1]
    text = " ".join(sys.argv[2:])

    if action == "find":
        cmd_find(text)
    elif action == "done":
        cmd_set_status(text, "done")
    elif action == "undone":
        cmd_set_status(text, "not_started")
    else:
        print("unknown action:", action)
        sys.exit(1)
