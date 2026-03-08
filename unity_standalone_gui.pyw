from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText


BASE_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = BASE_DIR / "unity_standalone.py"
OUTPUT_DIR_PATTERN = re.compile(r'"output_dir"\s*:\s*"([^"]+)"')
DEFAULT_SIZE = "980x760"


class UnityStandaloneGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Unity Standalone Builder")
        self.geometry(DEFAULT_SIZE)
        self.minsize(860, 620)

        self.mode_var = tk.StringVar(value="entry")
        self.entry_url_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.loader_url_var = tk.StringVar()
        self.framework_url_var = tk.StringVar()
        self.data_url_var = tk.StringVar()
        self.wasm_url_var = tk.StringVar()
        self.overwrite_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Idle")

        self.process: subprocess.Popen[str] | None = None
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.last_output_dir = ""
        self.locked_widgets: list[tk.Widget] = []

        self._build_ui()
        self._sync_mode()
        self.protocol("WM_DELETE_WINDOW", self._handle_close)
        self.after(100, self._poll_events)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = ttk.Frame(self, padding=(16, 16, 16, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(
            header,
            text="Unity Standalone Builder",
            font=("Segoe UI", 17, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Local GUI for entry URLs or direct Unity asset URLs.",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        form = ttk.Frame(self, padding=(16, 0, 16, 8))
        form.grid(row=1, column=0, sticky="nsew")
        form.columnconfigure(0, weight=1)

        mode_frame = ttk.LabelFrame(form, text="Mode", padding=12)
        mode_frame.grid(row=0, column=0, sticky="ew")
        mode_frame.columnconfigure(0, weight=1)
        mode_frame.columnconfigure(1, weight=1)

        entry_radio = ttk.Radiobutton(
            mode_frame,
            text="Entry URL",
            value="entry",
            variable=self.mode_var,
            command=self._sync_mode,
        )
        entry_radio.grid(row=0, column=0, sticky="w")
        direct_radio = ttk.Radiobutton(
            mode_frame,
            text="Direct Unity URLs",
            value="direct",
            variable=self.mode_var,
            command=self._sync_mode,
        )
        direct_radio.grid(row=0, column=1, sticky="w")
        self.locked_widgets.extend([entry_radio, direct_radio])

        self.entry_frame = ttk.LabelFrame(form, text="Entry URL", padding=12)
        self.entry_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.entry_frame.columnconfigure(1, weight=1)
        self._add_labeled_entry(
            self.entry_frame,
            row=0,
            label="Game URL",
            variable=self.entry_url_var,
            example="https://melonplayground.io/",
        )

        self.direct_frame = ttk.LabelFrame(form, text="Direct Unity URLs", padding=12)
        self.direct_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.direct_frame.columnconfigure(1, weight=1)
        self._add_labeled_entry(
            self.direct_frame,
            row=0,
            label="Loader URL",
            variable=self.loader_url_var,
        )
        self._add_labeled_entry(
            self.direct_frame,
            row=1,
            label="Framework URL",
            variable=self.framework_url_var,
        )
        self._add_labeled_entry(
            self.direct_frame,
            row=2,
            label="Data URL",
            variable=self.data_url_var,
        )
        self._add_labeled_entry(
            self.direct_frame,
            row=3,
            label="Wasm URL",
            variable=self.wasm_url_var,
        )

        options_frame = ttk.LabelFrame(form, text="Options", padding=12)
        options_frame.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        options_frame.columnconfigure(1, weight=1)

        self._add_labeled_entry(
            options_frame,
            row=0,
            label="Output folder",
            variable=self.output_dir_var,
            example="Optional. Leave blank to infer from the game.",
        )

        overwrite_check = ttk.Checkbutton(
            options_frame,
            text="Overwrite existing output folder",
            variable=self.overwrite_var,
        )
        overwrite_check.grid(row=2, column=1, sticky="w", pady=(8, 0))
        self.locked_widgets.append(overwrite_check)

        actions = ttk.Frame(form, padding=(0, 12, 0, 0))
        actions.grid(row=4, column=0, sticky="ew")
        actions.columnconfigure(5, weight=1)

        self.build_button = ttk.Button(actions, text="Build", command=self.start_build)
        self.build_button.grid(row=0, column=0, sticky="w")

        self.stop_button = ttk.Button(actions, text="Stop", command=self.stop_build, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="w", padx=(8, 0))

        clear_button = ttk.Button(actions, text="Clear Log", command=self.clear_log)
        clear_button.grid(row=0, column=2, sticky="w", padx=(8, 0))

        open_button = ttk.Button(actions, text="Open Output Folder", command=self.open_output_folder)
        open_button.grid(row=0, column=3, sticky="w", padx=(8, 0))

        ttk.Label(actions, textvariable=self.status_var).grid(row=0, column=5, sticky="e")

        log_frame = ttk.LabelFrame(self, text="Build Log", padding=(12, 10, 12, 12))
        log_frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 16))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = ScrolledText(
            log_frame,
            wrap="word",
            font=("Consolas", 10),
            height=18,
            state="disabled",
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        self.locked_widgets.extend([self.build_button])

    def _add_labeled_entry(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
        example: str = "",
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        self.locked_widgets.append(entry)
        if example:
            ttk.Label(parent, text=example).grid(row=row + 1, column=1, sticky="w", pady=(0, 4))

    def _sync_mode(self) -> None:
        if self.mode_var.get() == "entry":
            self.entry_frame.grid()
            self.direct_frame.grid_remove()
        else:
            self.entry_frame.grid_remove()
            self.direct_frame.grid()

    def _set_running_state(self, is_running: bool) -> None:
        for widget in self.locked_widgets:
            try:
                widget.configure(state="disabled" if is_running else "normal")
            except tk.TclError:
                continue
        self.build_button.configure(state="disabled" if is_running else "normal")
        self.stop_button.configure(state="normal" if is_running else "disabled")

    def append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

        for match in OUTPUT_DIR_PATTERN.finditer(text):
            self.last_output_dir = bytes(match.group(1), "utf-8").decode("unicode_escape")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def build_command(self) -> list[str]:
        command = [sys.executable, "-u", str(SCRIPT_PATH)]

        if self.mode_var.get() == "entry":
            entry_url = self.entry_url_var.get().strip()
            if not entry_url:
                raise ValueError("Entry URL is required.")
            command.append(entry_url)
        else:
            direct_fields = {
                "--loader-url": self.loader_url_var.get().strip(),
                "--framework-url": self.framework_url_var.get().strip(),
                "--data-url": self.data_url_var.get().strip(),
                "--wasm-url": self.wasm_url_var.get().strip(),
            }
            missing = [flag for flag, value in direct_fields.items() if not value]
            if missing:
                raise ValueError("All direct Unity URLs are required in Direct Unity URLs mode.")
            for flag, value in direct_fields.items():
                command.extend([flag, value])

        output_dir = self.output_dir_var.get().strip()
        if output_dir:
            command.extend(["--out", output_dir])

        if self.overwrite_var.get():
            command.append("--overwrite")

        return command

    def start_build(self) -> None:
        if self.process is not None:
            return
        if not SCRIPT_PATH.exists():
            messagebox.showerror("Missing Script", f"Could not find {SCRIPT_PATH}")
            return

        try:
            command = self.build_command()
        except ValueError as exc:
            messagebox.showerror("Missing Input", str(exc))
            return

        self.last_output_dir = ""
        self.status_var.set("Starting build...")
        self.append_log("$ " + self._format_command(command) + "\n")
        self._set_running_state(True)

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            self.process = None
            self._set_running_state(False)
            self.status_var.set("Failed to start")
            messagebox.showerror("Launch Error", str(exc))
            return

        threading.Thread(target=self._read_process_output, daemon=True).start()

    def stop_build(self) -> None:
        if self.process is None:
            return
        self.status_var.set("Stopping...")
        self.append_log("[gui] Stopping build...\n")
        process = self.process
        process.terminate()
        threading.Thread(target=self._force_kill_if_needed, args=(process,), daemon=True).start()

    def open_output_folder(self) -> None:
        candidate = self.last_output_dir or self.output_dir_var.get().strip()
        if not candidate:
            candidate = str(BASE_DIR)
        path = Path(candidate)
        if not path.is_absolute():
            path = (BASE_DIR / path).resolve()
        if not path.exists():
            messagebox.showinfo("Folder Missing", f"Folder does not exist yet:\n{path}")
            return
        os.startfile(path)

    def _read_process_output(self) -> None:
        assert self.process is not None
        process = self.process
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    self.event_queue.put(("log", line))
        finally:
            return_code = process.wait()
            self.event_queue.put(("done", return_code))

    def _force_kill_if_needed(self, process: subprocess.Popen[str]) -> None:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()

    def _poll_events(self) -> None:
        while True:
            try:
                event_type, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break

            if event_type == "log":
                self.append_log(str(payload))
            elif event_type == "done":
                return_code = int(payload)
                self.process = None
                self._set_running_state(False)
                if return_code == 0:
                    self.status_var.set("Build complete")
                    self.append_log("[gui] Build complete.\n")
                else:
                    self.status_var.set(f"Build failed ({return_code})")
                    self.append_log(f"[gui] Build failed with exit code {return_code}.\n")

        self.after(100, self._poll_events)

    def _handle_close(self) -> None:
        if self.process is not None:
            should_close = messagebox.askyesno(
                "Build Running",
                "A build is still running. Stop it and close the GUI?",
            )
            if not should_close:
                return
            self.stop_build()
        self.destroy()

    @staticmethod
    def _format_command(command: list[str]) -> str:
        def quote(part: str) -> str:
            return f'"{part}"' if " " in part else part

        return " ".join(quote(part) for part in command)


def main() -> None:
    app = UnityStandaloneGui()
    app.mainloop()


if __name__ == "__main__":
    main()
