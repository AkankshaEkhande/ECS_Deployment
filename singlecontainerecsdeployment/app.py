import os
import socket
from flask import Flask, jsonify, request

app = Flask(__name__)

# In-memory store (resets when container restarts) - fine for learning
notes = []


@app.route("/")
def home():
    return jsonify(
        message="Hello from Flask running on AWS ECS Fargate!",
        container_id=socket.gethostname(),   # changes per task -> shows load balancing
        version=os.getenv("APP_VERSION", "v1"),
    )


@app.route("/health")
def health():
    # ALB / ECS health check hits this
    return jsonify(status="ok"), 200


@app.route("/notes", methods=["GET"])
def list_notes():
    return jsonify(notes)


@app.route("/notes", methods=["POST"])
def add_note():
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    if not text:
        return jsonify(error="'text' is required"), 400
    note = {"id": len(notes) + 1, "text": text}
    notes.append(note)
    return jsonify(note), 201


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
