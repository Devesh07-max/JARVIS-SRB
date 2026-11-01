"""Minimal health endpoint for the single Render web service hosting the bot.

The public website and all checkout/payment endpoints have been removed.
"""
import os
from flask import Flask, jsonify

app = Flask(__name__)


@app.get("/")
def service_status():
    return "JARVIS SRB bot service is running. Use the Telegram bot to access features.", 200


@app.get("/health")
def health():
    return jsonify(status="ok", service="jarvis-srb-bot"), 200


def run_web_server():
    port = int(os.environ.get("PORT", 5000))
    from waitress import serve
    serve(app, host="0.0.0.0", port=port, threads=4, channel_timeout=30)


if __name__ == "__main__":
    run_web_server()
