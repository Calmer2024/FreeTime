"""超星刷课助手 Web UI"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request

app = Flask(__name__, static_folder="static", template_folder="templates")

# 全局状态
process: subprocess.Popen | None = None
is_running = False
log_lines: list[str] = []


def read_config() -> dict:
    """读取配置文件"""
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        return json.loads(config_path.read_text(encoding="utf-8"))
    return {
        "course": {"url": "", "name": ""},
        "playback": {
            "speed": 2.0,
            "poll_interval_seconds": 3,
            "max_retries": 3,
            "load_timeout_seconds": 60,
        },
        "auth": {"state_file": "auth_state.json"},
    }


def save_config(config: dict) -> None:
    """保存配置文件"""
    config_path = Path(__file__).parent / "config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


@app.route("/")
def index():
    """主页"""
    config = read_config()
    return render_template("index.html", config=config)


@app.route("/api/config", methods=["GET"])
def get_config():
    """获取配置"""
    return jsonify(read_config())


@app.route("/api/config", methods=["POST"])
def update_config():
    """更新配置"""
    data = request.json
    config = read_config()

    if "course_url" in data:
        config["course"]["url"] = data["course_url"]
    if "course_name" in data:
        config["course"]["name"] = data["course_name"]
    if "speed" in data:
        config["playback"]["speed"] = float(data["speed"])
    if "poll_interval" in data:
        config["playback"]["poll_interval_seconds"] = int(data["poll_interval"])

    save_config(config)
    return jsonify({"status": "ok"})


@app.route("/api/start", methods=["POST"])
def start_task():
    """启动刷课任务"""
    global process, is_running, log_lines

    if is_running:
        return jsonify({"status": "error", "message": "任务已在运行中"}), 400

    log_lines = []
    is_running = True

    def run_task():
        global process, is_running
        try:
            script_path = Path(__file__).parent / "main.py"
            process = subprocess.Popen(
                [sys.executable, str(script_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for line in process.stdout:
                log_lines.append(line.strip())
                if len(log_lines) > 1000:
                    log_lines.pop(0)
            process.wait()
        except Exception as e:
            log_lines.append(f"错误: {e}")
        finally:
            is_running = False
            process = None

    thread = threading.Thread(target=run_task, daemon=True)
    thread.start()

    return jsonify({"status": "ok", "message": "任务已启动"})


@app.route("/api/stop", methods=["POST"])
def stop_task():
    """停止刷课任务"""
    global process, is_running

    if not is_running or process is None:
        return jsonify({"status": "error", "message": "没有运行中的任务"}), 400

    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        process.kill()

    is_running = False
    process = None
    log_lines.append("任务已手动停止")

    return jsonify({"status": "ok", "message": "任务已停止"})


@app.route("/api/status")
def get_status():
    """获取运行状态"""
    return jsonify({
        "is_running": is_running,
        "log_lines": log_lines[-100:],  # 只返回最近100行
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001, debug=True)
