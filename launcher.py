import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import uvicorn

from app_paths import app_data_dir, runtime_file


APP_TITLE = "Kappal Rate Capture"


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_server(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def run_server(port: int) -> None:
    try:
        import server as kappal_server

        os.environ["PORT"] = str(port)
        config = uvicorn.Config(
            kappal_server.app,
            host="127.0.0.1",
            port=port,
            log_level="info",
            log_config=None,
            access_log=False,
        )
        server = uvicorn.Server(config)
        server.run()
    except Exception:
        runtime_file("launcher.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise


def open_app(port: int) -> None:
    webbrowser.open(f"http://127.0.0.1:{port}", new=1)


def show_status_window(port: int, server_started: bool) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception:
        while True:
            time.sleep(3600)

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("420x220")
    root.resizable(False, False)

    url = f"http://127.0.0.1:{port}"
    data_dir = app_data_dir()

    frame = tk.Frame(root, padx=24, pady=22)
    frame.pack(fill="both", expand=True)

    title = tk.Label(frame, text=APP_TITLE, font=("Segoe UI", 16, "bold"))
    title.pack(anchor="w")

    status_text = f"Running locally at:\n{url}" if server_started else "Backend did not start.\nCheck launcher.log in the data folder."
    status = tk.Label(
        frame,
        text=status_text,
        font=("Segoe UI", 10),
        justify="left",
    )
    status.pack(anchor="w", pady=(12, 8))

    data = tk.Label(
        frame,
        text=f"Data folder:\n{data_dir}",
        font=("Segoe UI", 8),
        fg="#666666",
        justify="left",
        wraplength=360,
    )
    data.pack(anchor="w", pady=(0, 14))

    buttons = tk.Frame(frame)
    buttons.pack(anchor="e", fill="x")

    open_button = tk.Button(buttons, text="Open App", width=12, command=lambda: open_app(port))
    open_button.pack(side="right", padx=(8, 0))
    if not server_started:
        open_button.configure(state="disabled")

    def exit_app() -> None:
        if messagebox.askokcancel(APP_TITLE, "Close Kappal Rate Capture?"):
            root.destroy()
            os._exit(0)

    tk.Button(buttons, text="Exit", width=12, command=exit_app).pack(side="right")
    root.protocol("WM_DELETE_WINDOW", exit_app)
    root.mainloop()


def main() -> None:
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).resolve().parent)
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

    app_data_dir()
    port = find_free_port()
    thread = threading.Thread(target=run_server, args=(port,), daemon=True)
    thread.start()

    server_started = wait_for_server(port)
    if server_started:
        open_app(port)

    show_status_window(port, server_started)


if __name__ == "__main__":
    main()
