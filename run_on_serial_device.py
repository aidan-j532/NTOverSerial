import json
import sys

import serial


def load_available(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    if len(sys.argv) < 3:
        print("usage: run_on_serial_device.py <PORT> <BAUD> [topics.json]")
        sys.exit(1)
    port, baud = sys.argv[1], int(sys.argv[2])
    want = load_available(sys.argv[3]) if len(sys.argv) > 3 else None

    with serial.Serial(port, baud, timeout=1) as ser:
        print(f"Opened {port} @ {baud}")

        # 1 this is where like run.py sends the full list of topics
        topics = None
        while topics is None:
            line = ser.readline().decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                print(f"ignoring non-JSON: {line}")
                continue
            if "topics" in msg:
                topics = msg["topics"]
                print(f"Available topics ({len(topics)}): {topics}")

        # 2 this is where the code decides which topics it wants to "subscribe" to
        if want is None:
            # basically just for example wants topics whose key mentions 'example' or 'defualt'.
            wanted = [t for t in topics if "example" in t or "defualt" in t]
        else:
            wanted = [t for t in topics if any(w in t for w in want)]
        print(f"Subscribing to: {wanted}") # nice debuggign print dont remove
        ser.write((json.dumps({"subscribe": wanted}) + "\n").encode("utf-8"))

        # 3 print the full data run.py sends for BUT only the topics wtv the code subscribed to
        print("Receiving data for subscribed topics:")
        while True:
            line = ser.readline().decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                print(json.dumps(json.loads(line)))
            except json.JSONDecodeError:
                print(line)


if __name__ == "__main__":
    main()
